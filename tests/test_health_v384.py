#!/usr/bin/env python3
"""v3.8.4 — /health и отбраковка Transfer-Encoding.

A. /health      — открытая проба живости: 200 без ключа на сервере С
                  ключом и без, поля на месте (name/protocol/running/
                  uptime_s), сессию НЕ регистрирует (в ответе нет
                  session_token, счётчик сессий не растёт), uptime не
                  откатывается между запросами.
B. TE-кадрирование — запросы с Transfer-Encoding (chunked и экзотика)
                  получают 400 и ЗАКРЫТОЕ соединение: непрочитанное тело
                  не должно оставаться в keep-alive сокете и прикидываться
                  следующим запросом (desync/request smuggling за прокси).
                  После всех проб сервер жив и обычный POST с
                  Content-Length работает как раньше.
"""
from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import time
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def req(base: str, path: str, sender: str, body: dict | None = None,
        headers: dict | None = None, timeout: int = 8) -> tuple[int, dict]:
    data = None
    hdrs = {"X-Relay-From": header_encode(sender)}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    import urllib.error
    import urllib.request
    r = urllib.request.Request(base + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def raw_exchange(port: int, payload: bytes) -> tuple[bytes, bool]:
    """Один сокет - один запрос: шлёт байты, читает ответ до закрытия.
    Второе возвращаемое значение - закрыл ли сервер соединение сам
    (recv вернул пустоту до таймаута), что для TE-проб и ожидаемо."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=4) as s:
            s.sendall(payload)
            s.settimeout(2.5)
            chunks = []
            closed = False
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        closed = True
                        break
                    chunks.append(b)
            except TimeoutError:
                # тишина после ответа: сервер держит keep-alive
                pass
            return b"".join(chunks), closed
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        return str(e).encode(), True


def alive(base: str) -> bool:
    try:
        st, data = req(base, "/ping", "ПробаЖивости", timeout=5)
        return st == 200 and bool(data.get("ok"))
    except Exception:
        return False


# ── A. /health ────────────────────────────────────────────────────────
def block_a(base: str) -> None:
    print("\n-- A. /health: живость без побочных эффектов --\n")

    st, data = req(base, "/health", "Аня")
    check("A1. 200 и ok=true", st == 200 and data.get("ok") is True)
    check("A2. name совпадает с хостом", data.get("name") == "ХостЗдоровья")
    check("A3. protocol/running на месте",
          isinstance(data.get("protocol"), int) and data.get("running") is True)
    check("A4. uptime_s неотрицательное число",
          isinstance(data.get("uptime_s"), int) and data.get("uptime_s") >= 0)
    check("A5. online - число", isinstance(data.get("online"), int))
    check("A6. session_token НЕ выдаётся", "session_token" not in data)
    check("A7. telemetry/session.kind не пишется",
          "kind" not in data)  # ответ вообще без лишних полей

    up1 = data.get("uptime_s", 0)
    time.sleep(1.1)
    st2, data2 = req(base, "/health", "Боря")
    check("A8. uptime не откатывается", data2.get("uptime_s", 0) >= up1)

    # счётчик сессий не растёт от опросов /health (в отличие от /ping)
    st3, data3 = req(base, "/health", "Аня")
    st4, data4 = req(base, "/health", "Аня")
    check("A9. повторные опросы стабильны",
          st3 == 200 and st4 == 200 and data3 == data4)


def block_a_keyed() -> None:
    print("\n-- A-keyed. /health за access_key --\n")
    tmp = Path(tempfile.mkdtemp(prefix="fr_health_key_"))
    kport = free_port()
    srv = RelayServer(tmp, host_name="ХостСКлючом", access_key="секрет123")
    assert srv.start("127.0.0.1", kport), "keyed server not started"
    time.sleep(0.2)
    kbase = f"http://127.0.0.1:{kport}"
    try:
        st, data = req(kbase, "/health", "Аня")  # БЕЗ X-Relay-Key
        check("AK1. без ключа всё равно 200", st == 200 and data.get("ok") is True)
        check("AK2. имя хоста отдаётся", data.get("name") == "ХостСКлючом")
        check("AK3. session_token нет и тут", "session_token" not in data)
        # контроль: /events без ключа по-прежнему закрыт
        st2, _ = req(kbase, "/events?since=0", "Аня")
        check("AK4. остальной API по-прежнему за ключом", st2 == 403)
    finally:
        srv.stop()


# ── B. Transfer-Encoding ─────────────────────────────────────────────
def block_b(base: str, port: int) -> None:
    print("\n-- B. Transfer-Encoding: 400 + закрытие соединения --\n")

    body = json.dumps({"text": "chunked-сообщение"}, ensure_ascii=False).encode()
    chunked = (
        b"POST /send_text HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"X-Relay-From: " + header_encode("Аня").encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        + hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    )
    reply, closed = raw_exchange(port, chunked)
    check("B1. chunked POST -> 400", b" 400 " in reply.split(b"\r\n")[0])
    check("B2. сервер сам закрыл соединение", closed)
    check("B3. понятная ошибка в теле", b"Transfer-Encoding" in reply)

    # GET с TE тоже отклоняется (любое тело с неясным кадрированием)
    get_te = (
        b"GET /events?since=0 HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
    )
    reply2, closed2 = raw_exchange(port, get_te)
    check("B4. GET с TE -> 400", b" 400 " in reply2.split(b"\r\n")[0])
    check("B5. соединение закрыто и тут", closed2)

    # экзотика: TE поверх Content-Length - противоречивое кадрирование,
    # по RFC 7230 побеждает TE, сервер обязан отвергнуть
    both = (
        b"POST /send_text HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Transfer-Encoding: gzip\r\n\r\n" + body
    )
    reply3, closed3 = raw_exchange(port, both)
    check("B6. TE + Content-Length -> 400", b" 400 " in reply3.split(b"\r\n")[0])
    check("B7. соединение закрыто", closed3)

    # сервер пережил все пробы, обычный путь не пострадал
    check("B8. сервер жив после TE-проб", alive(base))
    st, data = req(base, "/send_text", "Аня", {"text": "обычное сообщение"})
    check("B9. POST с Content-Length работает как раньше",
          st == 200 and data.get("ok") is True)
    st2, data2 = req(base, "/events?since=0", "Аня")
    evs = data2.get("events", []) if isinstance(data2, dict) else []
    texts = [e.get("text", "") for e in evs if e.get("kind") == "text"]
    check("B10. сообщение записано", texts == ["обычное сообщение"])


def main() -> int:
    print("=" * 70)
    print("ЗДОРОВЬЕ v3.8.4: /health для мониторинга, отбраковка TE-кадрирования")
    print("=" * 70)
    tmp = Path(tempfile.mkdtemp(prefix="fr_health_main_"))
    mport = free_port()
    srv = RelayServer(tmp, host_name="ХостЗдоровья")
    assert srv.start("127.0.0.1", mport), "server not started"
    time.sleep(0.2)
    base = f"http://127.0.0.1:{mport}"
    try:
        block_a(base)
        block_a_keyed()
        block_b(base, mport)
    finally:
        srv.stop()
    print("\n" + "=" * 70)
    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
