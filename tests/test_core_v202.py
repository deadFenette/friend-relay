"""Ядро сервера v2.0.2: утилиты + ускорение горячих точек + /server/stats.

Регресс на изменения производительности (docs/PERFORMANCE.md §v2.0.2):
  1. lib/util.parse_int / clamp — единая точка безопасного int (бывшие
     _header_int/_int_arg из транспорта) + зажим лимитов.
  2. lib/util.MtimeFileCache — кеш статики / и /static/*: отдаёт байты
     из RAM, инвалидируется по (mtime, size), пропажа файла = None.
  3. lib/json_fast — быстрый JSON (orjson, опционально) с фолбэком на
     stdlib: раундтрип кириллицы/вложенности, байты наружу, паритет
     json.loads на выходе dumps.
  4. EventStore.events_since — bisect вместо полного скана кеша на каждый
     /events poll: результат совпадает с эталонным списком, tail-режим
     для свежих подключений, tombstone-удаления не теряются.
  5. EventStore._by_seq — O(1)-индекс для edit/reaction/pin/delete:
     после вытеснения из кеша (_trim_cache) старые seq править нельзя,
     свежие можно; удалённые — нельзя (как раньше).
  6. GET /server/stats — сводка хоста за access_key (чужой ключ = 403).
  7. SessionStore.count() — публичный счётчик сессий.
  8. ЛС по HTTP (dm/send -> dm/history) — путь веб-вкладки «ЛС».
  9. Статика и страница веб-клиента отдаются через кеш байт-в-байт.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer
from lib.util import MtimeFileCache, clamp, header_encode, parse_int

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def get_json(base: str, path: str, headers: dict):
    r = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, None


def get_bytes(base: str, path: str, headers: dict):
    r = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, None


def post_json(base: str, path: str, headers: dict, body: dict):
    data = json.dumps(body).encode()
    h = dict(headers)
    h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, None


# ── 1. parse_int / clamp ────────────────────────────────────────────────
print("— lib.util.parse_int / clamp —")
check("число -> int", parse_int("42") == 42)
check("пусто -> default", parse_int("", 7) == 7)
check("None -> default", parse_int(None, 3) == 3)
check("мусор -> None", parse_int("abc") is None)
check("мусор с default -> None (не default!)",
      parse_int("abc", 5) is None)
check("отрицательное число -> int", parse_int("-5") == -5)
check("clamp в границах", clamp(5, 1, 10) == 5)
check("clamp ниже", clamp(-3, 1, 10) == 1)
check("clamp выше", clamp(99, 1, 10) == 10)

# ── 2. MtimeFileCache ───────────────────────────────────────────────────
print("— lib.util.MtimeFileCache —")
with tempfile.TemporaryDirectory(prefix="frelay_cache_") as td:
    p = Path(td) / "f.txt"
    p.write_text("привет", encoding="utf-8")
    cache = MtimeFileCache()
    b1 = cache.get(p)
    check("первое чтение отдаёт байты", b1 == "привет".encode())
    b2 = cache.get(p)
    check("второе чтение — из кеша (тот же объект)", b2 is b1)
    p.write_text("привет мир!", encoding="utf-8")
    check("смена размера -> инвалидация", cache.get(p) == "привет мир!".encode())
    p.write_text("тояет же длина", encoding="utf-8")
    os.utime(p, ns=(p.stat().st_mtime_ns + 1_000_000,) * 2)
    check("смена mtime при том же размере -> инвалидация",
          cache.get(p) == "тояет же длина".encode())
    cache.invalidate(p)
    check("ручная инвалидация не роняет чтение",
          cache.get(p) == "тояет же длина".encode())
    check("нет файла -> None", cache.get(Path(td) / "missing.txt") is None)

# ── 3. json_fast ────────────────────────────────────────────────────────
print("— lib.json_fast —")
from lib import json_fast

sample = {"from": "Вася", "text": "привет 👋", "n": 3, "f": 1.5,
          "nested": {"list": [1, 2, 3], "none": None, "flag": True}}
raw_out = json_fast.dumps(sample)
check("dumps отдаёт байты", isinstance(raw_out, bytes))
check("кириллица не экранирована", "Вася".encode() in raw_out)
check("json.loads читает наш dumps", json.loads(raw_out) == sample)
check("loads(bytes) раундтрип", json_fast.loads(raw_out) == sample)
check("loads(str) раундтрип", json_fast.loads(raw_out.decode("utf-8")) == sample)
line = json_fast.dumps_str(sample)
check("dumps_str отдаёт str без переносов внутри",
      isinstance(line, str) and "\n" not in line)
check("BACKEND валиден", json_fast.BACKEND in ("orjson", "stdlib"))
print(f"      (бэкенд: {json_fast.BACKEND})")

# ── 4-5. EventStore: bisect + индекс по seq ─────────────────────────────
print("— EventStore: events_since/events_before/_by_seq —")
from lib.constants import EVENT_KIND_TEXT
from lib.domain.event_store import EventStore

with tempfile.TemporaryDirectory(prefix="frelay_ev_") as td:
    store = EventStore(Path(td), cache_limit=10_000)
    senders = ("Вася", "Аня", "Боб")
    for i in range(300):
        store.add_text(senders[i % 3], f"msg {i}")
    with store._lock:
        snapshot = list(store._events)

    ok_since = True
    for since in (0, 1, 57, 299, 300, 1000):
        expect = [e for e in snapshot if e["seq"] > since]
        got = store.events_since(since)
        ok_since = ok_since and got == expect
    check("events_since совпадает с эталонным сканом (6 точек)", ok_since)

    check("tail при since=0 отдаёт последние N",
          [e["seq"] for e in store.events_since(0, tail=50)] == list(range(251, 301)))
    check("tail при since>0 игнорируется (честный инкремент)",
          len(store.events_since(250, tail=5)) == 50)

    ok_before = True
    for before in (50, 200, 301):
        expect = [e for e in snapshot
                  if e["seq"] < before and e.get("kind") == EVENT_KIND_TEXT]
        expect = expect[-25:]
        got = store.events_before(before, 25)
        ok_before = ok_before and [e["seq"] for e in got] == [e["seq"] for e in expect]
    check("events_before (память) совпадает с эталоном (3 точки)", ok_before)

    # правка/реакция/удаление через индекс
    ev = store.edit_text("Вася", 10, "новый текст")
    check("edit_text по seq находит цель", ev is not None)
    check("чужой текст править нельзя",
          store.edit_text("Боб", 11, "взлом") is None)
    rx = store.add_reaction("Аня", 12, "🔥")
    check("реакция по seq ставится", rx is not None and rx["added"] is True)
    # seq 15 принадлежит Бобу (senders[i%3], i=14 → Боб)
    tomb = store.delete_event("Боб", 15)
    check("удаление по seq пишет tombstone", tomb is not None)
    check("удалённое больше не редактируется",
          store.edit_text("Боб", 15, "зомби") is None)
    check("удалённое исчезло из events_since",
          all(e["seq"] != 15 for e in store.events_since(0)))

    # вытеснение из кеша: старые seq уходят из индекса
    small = EventStore(Path(td) / "small", cache_limit=30)
    for i in range(100):
        small.add_text("Вася", f"s {i}")
    check("вытесненное из кеша НЕ редактируется (раньше находилось сканом)",
          small.edit_text("Вася", 5, "старьё") is None)
    check("живое в кеше редактируется",
          small.edit_text("Вася", 95, "свежак") is not None)
    check("events_since(0) после вытеснения = хвост кеша",
          len(small.events_since(0)) == 30)
    check("events_before достаёт ВЫТЕСНЕННОЕ с диска (переигрывание дельт)",
          [e["seq"] for e in small.events_before(80, 10)] == list(range(70, 80)))
    st = small.stats()
    check("stats(): счётчики согласованы",
          st["events_cached"] == 30 and st["next_seq"] == 102
          and st["deleted_cached"] == 0)
    store.close()
    small.close()

# ── 6-9. живой сервер: /server/stats, статика, ЛС ────────────────────────
print("— живой сервер: /server/stats, статики кеш, ЛС —")
KEY = "ключ-2020"
with tempfile.TemporaryDirectory(prefix="frelay_stats_") as td:
    server = RelayServer(Path(td), host_name="Хост", access_key=KEY)
    assert server.start("127.0.0.1", 8837), "server not started"
    time.sleep(0.2)
    base = "http://127.0.0.1:8837"
    kh = {"X-Relay-Key": header_encode(KEY)}

    st, j = get_json(base, "/server/stats", kh)
    check("/server/stats с ключом -> 200", st == 200 and j and j["ok"] is True)
    check("/server/stats: running=True, uptime>=0",
          j.get("running") is True and isinstance(j.get("uptime_s"), int)
          and j["uptime_s"] >= 0)
    check("/server/stats: счётчики на месте",
          all(isinstance(j.get(k), int) for k in
              ("events_cached", "next_seq", "sessions", "channels",
               "files_tracked")))
    check("/server/stats: json_backend помечен",
          j.get("json_backend") in ("orjson", "stdlib"))
    st2, _ = get_json(base, "/server/stats",
                      {"X-Relay-Key": header_encode("не-тот")})
    check("/server/stats с чужим ключом -> 403", st2 == 403)

    # сессия: count() и статистика согласованы
    _st_ping, pj = get_json(base, "/ping", dict(kh, **{"X-Relay-From":
                                                       header_encode("Вася")}))
    token = (pj or {}).get("session_token", "")
    check("SessionStore.count() растёт после /ping",
          server._sessions.count() >= 1 and j["sessions"] == 0)

    # статика через кеш: байты совпадают с файлом на диске
    css = Path("web_client/style.css").read_bytes()
    st3, body = get_bytes(base, "/static/style.css", {})
    check("/static/style.css через кеш байт-в-байт", st3 == 200 and body == css)
    idx = Path("web_client/index.html").read_bytes()
    st4, body4 = get_bytes(base, "/", {})
    check("GET / через кеш байт-в-байт", st4 == 200 and body4 == idx)
    st5, _ = get_bytes(base, "/static/../relay_server.py", {})
    check("traversal по-прежнему закрыт", st5 == 404)

    # ЛС: отправка и чтение (путь веб-вкладки «ЛС»)
    from lib.auth import sign_request
    dm_body = {"to": "Аня", "text": "личное привет"}
    dm_raw = json.dumps(dm_body).encode()
    st6, j6 = post_json(base, "/dm/send",
                        {"X-Relay-Key": header_encode(KEY),
                         "X-Relay-From": header_encode("Вася"),
                         "X-Relay-Auth": token + ":" +
                             sign_request(token, dm_raw)},
                        dm_body)
    check("POST /dm/send -> ok", st6 == 200 and j6 and j6["ok"] is True)
    st7, j7 = get_json(base, "/dm/history?user=" +
                       urllib.request.quote("Аня"),
                       {"X-Relay-Key": header_encode(KEY),
                        "X-Relay-From": header_encode("Вася"),
                        "X-Relay-Auth": token + ":" + sign_request(
                            token, ("/dm/history?user="
                                    + urllib.request.quote("Аня")).encode())})
    check("GET /dm/history отдаёт диалог",
          st7 == 200 and j7 and len(j7.get("messages", [])) == 1
          and j7["messages"][0]["text"] == "личное привет")

    server.stop()
    check("stop() прошёл, статистика жива",
          server.get_server_stats()["running"] is False)

print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
