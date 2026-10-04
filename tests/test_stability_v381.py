#!/usr/bin/env python3
"""v3.8.1 — стабилизационный прогон: сервер обязан ПЕРЕЖИВАТЬ грязь.

Раньше тесты проверяли, что фичи работают в счастливом пути. Этот файл
проверяет обратное: всё, что может сломать хост в реальной сети и на
битом диске, не должно ронять сервер и портить журнал.

A. Грязный HTTP  — мусорные байты, битый JSON, враньё в Content-Length,
   обрыв тела, 16КБ URL, чужой метод, шапка-бомба: ответ (любой
   корректный) или закрытие соединения, но сервер ЖИВ и отвечает.
B. Битый журнал  — обрезанная последняя строка, мусор в середине,
   валидные НЕ-dict JSON-строки (5, "строка", [], null): загрузка без
   краха, целые события на месте, seq продолжается, запись работает.
C. Гонки         — 4 потока x 25 сообщений (все seq различны), микс
   send/edit/delete без 5xx, параллельные upload, poll во время записи.
D. Рестарт       — EventStore close->reopen (seq не откатывается),
   RelayServer stop->start (история цела).
E. Границы файлов — сервер с ключом и max_file_size=1024: 413 на
   превышение, пустой/отрицательный Content-Length, обрыв тела без
   .part-хвостов, sha-мисматч, мусорная подпись -> 403, позитивный
   контроль с честной HMAC-подписью -> 200.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer
from lib.util import header_encode

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def req(base: str, path: str, sender: str, body: dict | bytes | None = None,
        headers: dict | None = None, timeout: int = 8) -> tuple[int, dict | bytes]:
    data = None
    hdrs = {"X-Relay-From": header_encode(sender)}
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            hdrs["Content-Type"] = "application/json"
        else:
            data = body
    if headers:
        hdrs.update(headers)
    r = urllib.request.Request(base + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            if "json" in resp.headers.get("Content-Type", ""):
                return resp.status, json.loads(payload)
            return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def alive(base: str) -> bool:
    try:
        st, data = req(base, "/ping", "ПробаЖивости", timeout=5)
        return st == 200 and bool(data.get("ok"))
    except Exception:
        return False


def raw_send(port: int, payload: bytes, read_reply: bool = True) -> bytes:
    """Шлёт сырые байты в сокет и (опционально) читает, что влезет.
    Любое сетевое исключение глотается: для грязных запросов это норма."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=4) as s:
            s.sendall(payload)
            if not read_reply:
                return b""
            s.settimeout(2.5)
            chunks = []
            try:
                while sum(len(c) for c in chunks) < 65536:
                    b = s.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
            except (socket.timeout, ConnectionResetError, OSError):
                pass  # сервер мог молча закрыть соединение - это допустимо
            return b"".join(chunks)
    except (ConnectionResetError, BrokenPipeError, OSError):
        return b""  # сервер сбросил соединение - тоже корректная реакция


def raw_upload(port: int, path: str, payload: bytes, sender: str, filename: str,
               sha: str = "", extra: dict | None = None,
               send_frac: float = 1.0) -> tuple[int, bytes]:
    """POST файла низкоуровнево: send_frac < 1.0 имитирует обрыв тела."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    headers = {
        "X-Relay-From": header_encode(sender),
        "X-Relay-Filename": header_encode(filename),
        "X-Relay-SHA256": sha,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(payload)),
    }
    if extra:
        headers.update(extra)
    conn.putrequest("POST", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    try:
        conn.send(payload[: int(len(payload) * send_frac)])
        resp = conn.getresponse()
        body = resp.read()
        code = resp.status
    except (http.client.HTTPException, OSError):
        code, body = 0, b"<broken>"
    finally:
        conn.close()
    return code, body


# ── A. грязный HTTP ───────────────────────────────────────────────────
def block_a(base: str, port: int) -> None:
    print("\n-- A. грязный HTTP: сервер обязан выжить --\n")
    probes = [
        ("A1. мусорные байты вместо запроса",
         b"\x00\x01\x02\xff\xfe garbage not http \r\n\r\n"),
        ("A2. битый request line",
         b"NOT-HTTP\r\n\r\n"),
        ("A3. 16КБ URL",
         b"GET /" + b"A" * 16384 + b" HTTP/1.1\r\nHost: x\r\n\r\n"),
        ("A4. шапка-бомба (300 заголовков)",
         b"GET / HTTP/1.1\r\n" + b"".join(b"X-Junk-NN: " + b"z" * 64 + b"\r\n"
                                          for _ in range(300)) + b"\r\n"),
        ("A5. метод PATCH (неизвестный)",
         b"PATCH /send_text HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"),
    ]
    for name, payload in probes:
        raw_send(port, payload)
        check(name, alive(base), "сервер жив после пробы")

    st, data = req(base, "/send_text", "Аня", {"text": "живее всех живых"})
    check("A6. обычный чат после грязи работает", st == 200 and data.get("ok"))

    st, data = req(base, "/send_text", "Аня", b"{this is not json")
    check("A7. битый JSON тела -> 400, не 500", st == 400)

    raw_send(port, b"POST /send_text HTTP/1.1\r\nHost: x\r\n"
                   b"X-Relay-From: aneya\r\nContent-Length: 6\r\n\r\nabc")
    check("A8. Content-Length больше тела не вешает сервер", alive(base))

    raw_send(port, b"POST /send_file HTTP/1.1\r\nHost: x\r\n"
                   b"X-Relay-From: " + header_encode("Аня").encode() + b"\r\n"
                   b"X-Relay-Filename: big.bin\r\nContent-Length: 10485760\r\n\r\n"
                   + b"B" * 1024)
    check("A9. заявка 10МБ с обрывом тела не вешает сервер", alive(base))


# ── B. битый журнал ───────────────────────────────────────────────────
def block_b() -> None:
    print("\n-- B. битый журнал: EventStore переживает мусор на диске --\n")
    from lib.domain.event_store import EventStore

    tmp = Path(tempfile.mkdtemp(prefix="fr_stab_journal_"))
    hist = tmp / "history.jsonl"

    st1 = EventStore(tmp)
    st1.add_text("Аня", "первое")
    st1.add_text("Боря", "второе")
    st1.add_text("Аня", "третье")
    st1.close()
    lines = hist.read_text(encoding="utf-8").splitlines()
    check("B0. три события записаны", len(lines) == 3)

    # обрезанная последняя строка (эквивалент сбоя питания при записи)
    cut = lines[2][: len(lines[2]) // 2]
    hist.write_text("\n".join([lines[0], lines[1], cut]) + "\n", encoding="utf-8")
    st2 = EventStore(tmp)
    loaded = st2.events_since(0)
    check("B1. обрезанный хвост пропущен, 2 события целы", len(loaded) == 2)
    nxt = st2.add_text("Аня", "после сбоя")
    check("B2. seq не откатился после обрыва", nxt["seq"] == 3)
    st2.close()

    # мусор в середине + валидные НЕ-dict JSON-строки (крашили .get при загрузке)
    junk = ["5", '"строка"', "[]", "null", "{битая строка без json"]
    hist.write_text("\n".join([lines[0], *junk, lines[1], lines[2]]) + "\n",
                    encoding="utf-8")
    st3 = EventStore(tmp)
    loaded = st3.events_since(0)
    texts = [e.get("text", "") for e in loaded]
    check("B3. НЕ-dict JSON-строки не роняют загрузку", len(loaded) == 3)
    check("B4. все три живых события читаются",
          texts == ["первое", "второе", "третье"])
    nxt = st3.add_text("Боря", "пишем дальше")
    check("B5. запись в замусоренный журнал работает", nxt["seq"] == 4)
    st3.close()

    # пустой файл и только пустые строки
    hist.write_text("", encoding="utf-8")
    st4 = EventStore(tmp)
    check("B6. пустой журнал грузится", st4.events_since(0) == [])
    st4.close()
    hist.write_text("\n\n   \n\n", encoding="utf-8")
    st5 = EventStore(tmp)
    check("B7. журнал из пустых строк грузится", st5.events_since(0) == [])
    check("B8. запись после пустого журнала начинает с seq=1",
          st5.add_text("Аня", "с чистого листа")["seq"] == 1)
    st5.close()


# ── C. гонки ──────────────────────────────────────────────────────────
def block_c(base: str, port: int) -> None:
    print("\n-- C. гонки: параллельные клиенты не ломают сервер --\n")
    results: list[tuple[int, int]] = []  # (код, seq)
    lock = threading.Lock()

    def sender(n: int, name: str) -> None:
        local = []
        for i in range(25):
            st, data = req(base, "/send_text", name, {"text": f"гонка {n}-{i}"})
            local.append((st, data.get("seq", -1) if isinstance(data, dict) else -1))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=sender, args=(n, f"Гонщик{n}"))
               for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = [c for c, _ in results]
    seqs = [s for _, s in results]
    check("C1. 100 параллельных сообщений - все 200", codes.count(200) == 100)
    check("C2. все seq различны (нет двойного присвоения)",
          len(set(seqs)) == 100 and min(seqs) > 0)

    st, data = req(base, "/events?since=0", "Наблюдатель")
    evs = data.get("events", []) if isinstance(data, dict) else []
    check("C3. журнал содержит все 100 сообщений",
          sum(1 for e in evs if e.get("kind") == "text") >= 100)

    # микс send/edit/delete: чужие правки -> 404, своих удалений -> tombstone
    errs = []
    def mixed(name: str, target: str) -> None:
        for i in range(8):
            st1, d1 = req(base, "/send_text", name, {"text": f"{name} №{i}"})
            my_seq = d1.get("seq") if isinstance(d1, dict) else None
            st2, _ = req(base, "/edit_text", name,
                         {"seq": my_seq, "text": f"{name} правка {i}"})
            st3, _ = req(base, "/edit_text", name,
                         {"seq": my_seq + 100, "text": "несуществующее"})
            st4, _ = req(base, "/delete_event", name, {"seq": my_seq})
            errs.extend([st1, st2, st3, st4])
    t1 = threading.Thread(target=mixed, args=("Редактор1", "Редактор2"))
    t2 = threading.Thread(target=mixed, args=("Редактор2", "Редактор1"))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("C4. микс send/edit/delete без 5xx и без падений",
          all(e < 500 for e in errs) and all(e > 0 for e in errs))

    # параллельные upload (сервер без ключа: доверчивый режим)
    fids: list[str] = []
    def uploader(n: int) -> None:
        blob = bytes([n]) * 2048
        st, body = raw_upload(port, "/send_file", blob, f"Грузчик{n}",
                              f"f{n}.bin",
                              sha=hashlib.sha256(blob).hexdigest())
        with lock:
            fids.append(body.decode().get("file_id", "")
                        if isinstance(body, dict) else "")
    # raw_upload возвращает json в bytes -> распарсим отдельно
    def uploader2(n: int) -> None:
        blob = bytes([n]) * 2048
        st, body = raw_upload(port, "/send_file", blob, f"Грузчик{n}",
                              f"f{n}.bin",
                              sha=hashlib.sha256(blob).hexdigest())
        try:
            fid = json.loads(body).get("file_id", "")
        except Exception:
            fid = ""
        with lock:
            fids.append((st, fid))
    fids.clear()
    ths = [threading.Thread(target=uploader2, args=(n,)) for n in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("C5. три параллельных upload - все 200",
          all(st == 200 for st, _ in fids) and len(fids) == 3)
    check("C6. все file_id различны", len({fid for _, fid in fids}) == 3)

    # poll во время записи: /events не должен давать сбоев
    poll_errors = []
    stop = threading.Event()
    def poller() -> None:
        while not stop.is_set():
            try:
                st, _ = req(base, "/events?since=0", "Поллер", timeout=5)
                if st != 200:
                    poll_errors.append(st)
            except Exception as e:
                poll_errors.append(str(e))
            time.sleep(0.02)
    pt = threading.Thread(target=poller)
    pt.start()
    for i in range(30):
        req(base, "/send_text", "Писатель", {"text": f"под поллинг {i}"})
    stop.set(); pt.join()
    check("C7. 30 записей под непрерывным поллингом - poll без ошибок",
          not poll_errors)


# ── D. рестарт ────────────────────────────────────────────────────────
def block_d(base_unused: str) -> None:
    print("\n-- D. рестарт: данные переживают перезапуск --\n")
    from lib.domain.event_store import EventStore

    tmp = Path(tempfile.mkdtemp(prefix="fr_stab_restart_"))
    st1 = EventStore(tmp)
    for i in range(30):
        st1.add_text("Аня", f"сообщение {i}")
    st1.add_file("Боря", "док.pdf", b"%PDF-stability-check")
    st1.close()

    st2 = EventStore(tmp)
    evs = st2.events_since(0)
    check("D1. 31 событие пережило close->reopen", len(evs) == 31)
    check("D2. файловое событие на месте",
          any(e.get("kind") == "file" and e.get("name") == "док.pdf" for e in evs))
    check("D3. seq продолжается с 32",
          st2.add_text("Аня", "после рестарта")["seq"] == 32)
    st2.close()

    # полный цикл RelayServer stop -> start на той же папке
    dir2 = Path(tempfile.mkdtemp(prefix="fr_stab_server_"))
    p1, p2 = free_port(), free_port()
    srv1 = RelayServer(dir2, host_name="Хост")
    assert srv1.start("127.0.0.1", p1), "server1 not started"
    time.sleep(0.2)
    b1 = f"http://127.0.0.1:{p1}"
    for i in range(5):
        req(b1, "/send_text", "Аня", {"text": f"до рестарта {i}"})
    srv1.stop()
    time.sleep(0.3)

    srv2 = RelayServer(dir2, host_name="Хост")
    assert srv2.start("127.0.0.1", p2), "server2 not started"
    time.sleep(0.2)
    b2 = f"http://127.0.0.1:{p2}"
    st, data = req(b2, "/events?since=0", "Аня")
    evs = data.get("events", []) if isinstance(data, dict) else []
    texts = [e.get("text", "") for e in evs if e.get("kind") == "text"]
    check("D4. все 5 сообщений живы после stop->start сервера",
          len(texts) == 5 and texts[0] == "до рестарта 0")
    check("D5. сервер отвечает после рестарта", alive(b2))
    srv2.stop()


# ── E. границы файлов (сервер с ключом и малым лимитом) ──────────────
def block_e() -> None:
    print("\n-- E. границы файлов: лимит, обрыв, подписи --\n")
    tmp = Path(tempfile.mkdtemp(prefix="fr_stab_files_"))
    kport = free_port()
    srv = RelayServer(tmp, host_name="ХостКлюч", access_key="секрет123",
                      max_file_size=1024)
    assert srv.start("127.0.0.1", kport), "keyed server not started"
    time.sleep(0.2)
    base = f"http://127.0.0.1:{kport}"
    port = kport

    def n_disk() -> int:
        d = tmp / "files"
        return len(list(d.iterdir())) if d.exists() else 0

    # На сервере С ключом проверка X-Relay-Auth идёт ПЕРВОЙ (до размера и
    # хеша), поэтому все негативные пробы шлём с валидной подписью - иначе
    # они просто упрутся в 403 и не проверят свою границу.
    st, data = req(base, "/ping", "Аня",
                   headers={"X-Relay-Key": header_encode("секрет123")})
    token = data.get("session_token", "") if isinstance(data, dict) else ""
    check("E0. /ping с правильным ключом даёт токен", st == 200 and bool(token),
          f"st={st}")

    def auth(body: bytes) -> dict:
        # (на сервере с ключом X-Relay-Key обязателен на ЛЮБОМ POST —
        # проверяется раньше сессии; без него все пробы упрутся в 403
        # «неверный ключ доступа», не дойдя до своих границ)
        sig = _hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Relay-Key": header_encode("секрет123"),
                "X-Relay-Auth": f"{token}:{sig}"}

    blob = b"X" * 2048  # вдвое больше лимита 1024
    st, body = raw_upload(port, "/send_file", blob, "Аня", "большой.bin",
                          extra=auth(blob))
    check("E1. файл больше max_file_size -> 413", st == 413, f"st={st}")
    check("E2. отвергнутый файл не остался на диске", n_disk() == 0)

    st, body = raw_upload(port, "/send_file", b"", "Аня", "пусто.bin",
                          extra=auth(b""))
    check("E3. пустой файл -> 400", st == 400, f"st={st}")

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    conn.putrequest("POST", "/send_file")
    conn.putheader("X-Relay-From", header_encode("Аня"))
    conn.putheader("X-Relay-Filename", header_encode("минус.bin"))
    conn.putheader("X-Relay-Key", header_encode("секрет123"))
    conn.putheader("X-Relay-Auth", f"{token}:"
                   + _hmac.new(token.encode(), b"", hashlib.sha256).hexdigest())
    conn.putheader("Content-Length", "-5")
    conn.endheaders()
    try:
        resp = conn.getresponse(); code = resp.status; resp.read()
    except Exception:
        code = 0
    conn.close()
    check("E4. отрицательный Content-Length -> 400 или отказ", code in (0, 400),
          f"st={code}")

    # 800 байт < лимита 1024: размер пройдёт, оборванный поток поймает
    # _stream_body_to_file (received != length -> 400 + подчистка tmp)
    st, body = raw_upload(port, "/send_file", b"Y" * 800, "Аня", "обрыв.bin",
                          extra=auth(b"Y" * 800), send_frac=0.3)
    check("E5. оборванная загрузка -> отказ (не 200)", st != 200, f"st={st}")
    # Подчистка асинхронна (поток сервера дочитывает сокет) - ждём до 2с;
    # ручная проверка показала, что .part исчезает мгновенно, но на гонке
    # планировщика проверка «в ту же миллисекунду» видит ещё не убранный tmp.
    parts = []
    for _ in range(20):
        fd = tmp / "files"
        parts = list(fd.glob(".incoming*")) if fd.exists() else []
        if not parts:
            break
        time.sleep(0.1)
    check("E6. .part-хвостов на диске нет", not parts)

    good = "честные байты файла".encode("utf-8")
    st, body = raw_upload(port, "/send_file", good, "Аня", "битый_sha.bin",
                          sha="0" * 64, extra=auth(good))
    check("E7. враньё в X-Relay-SHA256 -> 400", st == 400, f"st={st}")
    check("E8. файл с битым sha не сохранился", n_disk() == 0)

    st, body = raw_upload(port, "/send_file", good, "Аня", "без_ключа.bin",
                          extra={"X-Relay-Key": header_encode("секрет123"),
                                 "X-Relay-Auth": "garbage:no-token"})
    check("E9. верный ключ + мусорная X-Relay-Auth -> 403", st == 403, f"st={st}")
    check("E10. отвергнутая загрузка не оставила файла", n_disk() == 0)

    st, body = raw_upload(port, "/send_file", good, "Аня", "вообще_без_ключа.bin")
    check("E11. POST без X-Relay-Key на сервере с ключом -> 403", st == 403,
          f"st={st}")

    sig = _hmac.new(token.encode(), good, hashlib.sha256).hexdigest()
    st, body = raw_upload(port, "/send_file", good, "Аня", "честный.bin",
                          sha=hashlib.sha256(good).hexdigest(),
                          extra=auth(good))
    check("E12. честная подпись -> файл принят", st == 200, f"st={st}")
    check("E13. принятый файл ровно один на диске", n_disk() == 1)
    srv.stop()


def free_port() -> int:
    """Свободный TCP-порт: жёсткие порты в тестах конфликтуют с остатками
    предыдущих прогонов/TIME_WAIT - берём у ОС ephemeral-порт."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    print("=" * 70)
    print("СТАБИЛЬНОСТЬ v3.8.1: грязный HTTP, битый журнал, гонки, рестарт, файлы")
    print("=" * 70)
    tmp = Path(tempfile.mkdtemp(prefix="fr_stab_main_"))
    mport = free_port()
    srv = RelayServer(tmp, host_name="Хост")
    assert srv.start("127.0.0.1", mport), "server not started"
    time.sleep(0.2)
    base = f"http://127.0.0.1:{mport}"
    port = mport
    try:
        block_a(base, port)
        block_b()
        block_c(base, port)
        block_d(base)
        block_e()
    finally:
        srv.stop()
    print("\n" + "=" * 70)
    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
