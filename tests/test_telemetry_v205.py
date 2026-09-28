#!/usr/bin/env python3
"""Тесты телеметрии v2.0.5: lib/telemetry.py + /telemetry/* эндпоинты.

Главный блок — «ЛЕГАЛЬНОСТЬ»: телеметрия собирает только счётчики и
числа, КОНТЕНТ (тексты сообщений, имена, имена файлов) не попадает в
файлы телеметрии НИКОГДА. Проверяем в лоб: отправляем в чат/ЛС/файлы
уникальные строки и убеждаемся, что их нет ни в одном файле
telemetry/.

Плюс: счётчики/наблюдения/гейджи, opt-out (persist в config.json),
route_key-маскировка, живой сервер: /telemetry/summary, enable/disable,
http.route./http.status/счётчики чата, голос (только если поднят).
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auth import sign_request
from lib.relay_server import RelayServer
from lib.telemetry import Telemetry

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def fetch(req: urllib.request.Request) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def signed_post(base: str, token: str, user: str, path: str,
                payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    auth = token + ":" + sign_request(token, body)
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"X-Relay-From": user, "X-Relay-Auth": auth,
                 "Content-Type": "application/json; charset=utf-8"},
    )
    code, raw, _ = fetch(req)
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, {"ok": False, "error": raw[:120].decode("utf-8", "replace")}


def telemetry_files(data_dir: Path) -> list[Path]:
    tdir = data_dir / "telemetry"
    if not tdir.exists():
        return []
    return [p for p in tdir.rglob("*") if p.is_file()]


# ───────────────────────── юнит-уровень ─────────────────────────
def unit_tests(tmp: Path) -> None:
    print("── Юнит: счётчики/наблюдения/гейджи ──")
    t = Telemetry(tmp, enabled=True)
    t.record("chat.text", 3)
    t.record("chat.text")
    t.record("dm.sent")
    snap = t.snapshot()
    check("счётчик складывается", snap["counters"]["chat.text"] == 4)
    check("другие ключи независимы", snap["counters"]["dm.sent"] == 1)
    check("подпись privacy на месте",
          snap["privacy"]["local_only"] is True
          and snap["privacy"]["no_content"] is True)
    check("окружение без путей",
          "platform" in snap["env"] and "app_version" in snap["env"])

    t.observe("http.latency_ms", 10.0)
    t.observe("http.latency_ms", 30.0)
    tm = t.snapshot()["timings"]["http.latency_ms"]
    check("observe: min/avg/max/count",
          tm["count"] == 2 and tm["min_ms"] == 10.0
          and tm["max_ms"] == 30.0 and tm["avg_ms"] == 20.0)

    t.set_gauge("server.online_now", 2)
    t.set_gauge("server.online_now", 7)
    g = t.snapshot()["gauges"]["server.online_now"]
    check("гейдж: текущее + пик", g == {"v": 7.0, "peak": 7.0})

    print("── Юнит: маскировка маршрутов ──")
    check("имя в /avatar/<имя> скрыто",
          Telemetry.route_key("/avatar/Вася") == "/avatar/*")
    check("id в /download/<id> скрыт",
          Telemetry.route_key("/download/abc123") == "/download/*")
    check("точный маршрут проходит как есть",
          Telemetry.route_key("/telemetry/summary") == "/telemetry/summary")
    check("мусорный глубокий путь сворачивается",
          Telemetry.route_key("/evil/a/b/c") == "/evil/*")
    check("query отрезается", Telemetry.route_key("/events?since=5") == "/events")

    print("── Юнит: мусорные ключи и переполнение ──")
    t2 = Telemetry(tmp / "t2", enabled=True)
    t2.record("../etc/passwd")          # путь — не валидный ключ
    t2.record("BOT.COMMANDS.Ха-Ха")     # регистр/алфавит гасятся
    snap2 = t2.snapshot()
    check("мусорные ключи отвергнуты",
          snap2["counters"] == {})
    for i in range(600):                # попытка раздуть словарь ключей
        t2.record(f"key.{i:04d}")
    snap2 = t2.snapshot()
    check("лимит ключей сработал (512 + overflow)",
          len(snap2["counters"]) <= 512 and snap2["overflow"] > 0)

    print("── Юнит: отключаемость и персистентность ──")
    t.record("chat.text")
    t.set_enabled(False)
    t.record("chat.text")
    check("после disable счётчик не растёт",
          t.snapshot()["counters"]["chat.text"] == 5)
    check("config.json записан",
          (tmp / "telemetry" / "config.json").exists())
    t3 = Telemetry(tmp)   # «рестарт»: читаем конфиг с диска
    check("выбор переживает рестарт", t3.is_enabled() is False)
    t3.set_enabled(True)
    check("enable возвращает сбор",
          Telemetry(tmp).is_enabled() is True)

    print("── Юнит: флуш пишет файлы ──")
    t3.record("session.created", 2)
    t3.flush()
    check("summary.json появился",
          (tmp / "telemetry" / "summary.json").exists())
    days = list((tmp / "telemetry").glob("events-*.jsonl"))
    check("дневной events-файл появился", len(days) == 1)
    t3.flush()
    check("повторный флуш без дельт не дописывает строки",
          len(days[0].read_text(encoding="utf-8").strip().splitlines()) == 1)

    print("── Юнит: потоки ──")
    t4 = Telemetry(tmp / "t4", enabled=True)
    def spam():
        for _ in range(2000):
            t4.record("chat.text")
            t4.observe("http.latency_ms", 1.0)
    ths = [threading.Thread(target=spam) for _ in range(8)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    snap4 = t4.snapshot()
    check("нити: 16000 событий не потерялись",
          snap4["counters"]["chat.text"] == 16000)
    check("нити: наблюдений тоже 16000",
          snap4["timings"]["http.latency_ms"]["count"] == 16000)


# ───────────────────────── живой сервер ─────────────────────────
def live_tests(base: str, data_dir: Path) -> None:
    print("── Живой сервер: /telemetry/* ──")

    # пинг → session.created (+kind.python: urllib UA)
    req = urllib.request.Request(base + "/ping",
                                 headers={"X-Relay-From": "Alice"})
    code, body, _ = fetch(req)
    check("ping работает", code == 200)
    tok_a = json.loads(body)["session_token"]
    tok_b = json.loads(fetch(urllib.request.Request(
        base + "/ping", headers={"X-Relay-From": "Bob"}))[1])["session_token"]

    # чат-сообщение с уникальной строкой (для лего-блока ниже)
    secret = "SEKRET-NET-V-TELEMETRII-7f3a91"
    code, j = signed_post(base, tok_a, "Alice", "/send_text",
                          {"text": "привет " + secret})
    check("send_text прошёл", code == 200 and j.get("ok"))

    # активность: ЛС, реакция, файл, игры, типинг + опрос ленты (гейдж онлайна)
    fetch(urllib.request.Request(base + "/events?since=0",
                                 headers={"X-Relay-From": "Alice"}))
    signed_post(base, tok_a, "Alice", "/dm/send",
                {"to": "Bob", "text": "личка " + secret})
    signed_post(base, tok_b, "Bob", "/typing", {})
    code, j = signed_post(base, tok_a, "Alice", "/send_file",
                          {"name": "заметка-" + secret + ".txt",
                           "data_b64": "0J/RgNC40LLQtdGC"})  # "Привет"
    check("send_file прошёл", code == 200 and j.get("ok"))
    code, j = signed_post(base, tok_a, "Alice", "/dispatch",
                          {"text": "!help chess"})
    # телеметрия игр: лаунчер + открытие
    fetch(urllib.request.Request(base + "/games"))
    fetch(urllib.request.Request(base + "/games/minesweeper"))

    code, body, _ = fetch(urllib.request.Request(
        base + "/telemetry/summary", headers={"X-Relay-From": "Alice"}))
    snap = json.loads(body)
    check("summary: ok и структура", code == 200 and snap.get("ok")
          and "counters" in snap and "timings" in snap and "gauges" in snap)
    c = snap["counters"]
    check("session.created посчитан", c.get("session.created", 0) >= 2)
    check("session.kind.python посчитан",
          c.get("session.kind.python", 0) >= 2)
    check("chat.text посчитан", c.get("chat.text", 0) >= 1)
    check("dm.sent посчитан", c.get("dm.sent", 0) >= 1)
    check("file.uploaded посчитан", c.get("file.uploaded", 0) >= 1)
    check("http.route./ping посчитан", c.get("http.route./ping", 0) >= 2)
    check("http.status.200 копится", c.get("http.status.200", 0) >= 5)
    check("games.opened.minesweeper посчитан",
          c.get("games.opened.minesweeper", 0) >= 1)
    check("games.list посчитан", c.get("games.list", 0) >= 1)
    check("латентности наблюдаются",
          snap["timings"].get("http.latency_ms", {}).get("count", 0) >= 5)
    check("гейдж онлайна жив", snap["gauges"].get(
        "server.online_now", {}).get("v", 0) >= 1)

    # повторный запрос summary: предыдущий вызов уже должен был записать
    # свой маршрут в счётчики (запрос не видит счётчик самого себя —
    # note_request срабатывает в finally после отправки ответа)
    code, body_rt, _ = fetch(urllib.request.Request(
        base + "/telemetry/summary", headers={"X-Relay-From": "Alice"}))
    snap_rt = json.loads(body_rt)
    check("http.route./telemetry/summary виден (со 2-го запроса)",
          snap_rt["counters"].get("http.route./telemetry/summary", 0) >= 1)

    print("── Живой сервер: ЛЕГАЛЬНОСТЬ (нет контента) ──")
    # флушим, чтобы всё легло на диск, и ищем секреты В ФАЙЛАХ
    snap["__direct_force"] = True  # (не влияет — просто метка для читателя)
    # (флушер живёт на сервере; для теста дёргаем фасад напрямую нельзя —
    #  но summary пишется тем же flush(); ждём такт и читаем файлы сами)
    # прямой flush через снапшот-файл недоступен по HTTP — используем то,
    # что флушер пишет раз в 15с: подождём чуть и проверим, а ДО этого
    # проверим главный источник утечки — ЭНДПОИНТ summary.
    raw_text = body.decode("utf-8", "replace")
    check("секрета чата НЕТ в /telemetry/summary", secret not in raw_text)
    check("секрета ЛС НЕТ в /telemetry/summary", "личка " + secret not in raw_text)
    check("имени файла НЕТ в /telemetry/summary",
          "заметка-" + secret not in raw_text)
    check("имени игрока НЕТ в значении телеметрии",
          '"Alice"' not in raw_text.replace('{"Alice":', ''))

    print("── Живой сервер: opt-out по HTTP ──")
    code, j = signed_post(base, tok_a, "Alice", "/telemetry/disable", {})
    check("disable отвечает enabled:false",
          code == 200 and j.get("ok") and j.get("enabled") is False)
    code, body2, _ = fetch(urllib.request.Request(
        base + "/ping", headers={"X-Relay-From": "Carol"}))
    code, body3, _ = fetch(urllib.request.Request(
        base + "/telemetry/summary", headers={"X-Relay-From": "Alice"}))
    snap2 = json.loads(body3)
    check("summary помечен enabled:false", snap2.get("enabled") is False)
    check("после disable новые сессии не считаются",
          snap2["counters"].get("session.created", 0) == c.get(
              "session.created", 0))
    code, j = signed_post(base, tok_a, "Alice", "/telemetry/enable", {})
    check("enable включает обратно",
          j.get("ok") and j.get("enabled") is True)

    print("── Живой сервер: файлы телеметрии не содержат контент ──")
    # к этому моменту флушер уже мог писать; проверяем ВСЕ файлы телеметрии
    files = telemetry_files(data_dir)
    leaks = []
    for p in files:
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        if secret.encode() in blob or "заметка-".encode() in blob:
            leaks.append(p.name)
    check("в telemetry/ нет ни чата, ни имён файлов", not leaks)
    check("telemetry/ вообще пишет файлы (флушер жив)", len(files) >= 1)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="telemetry_v205_"))
    unit_tests(tmp)
    print("── Живой сервер ──")
    live_dir = tmp / "live"
    relay = RelayServer(live_dir, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18541
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.2)
        live_tests(base, live_dir)
    finally:
        relay.stop()   # stop() = финальный флуш телеметрии
    # после остановки финальный флуш обязан был дописать файлы
    files = telemetry_files(live_dir)
    secret = "SEKRET-NET-V-TELEMETRII-7f3a91"
    leaks = [p.name for p in files
             if secret.encode() in (p.read_bytes() if p.is_file() else b"")]
    check("финальный флуш: файлы есть", len(files) >= 1)
    check("финальный флуш: секрета по-прежнему нет", not leaks)

    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
