"""Ядро сервера v2.0.4: новые utility + антифлуд + метрики латентности.

Регресс на расширение ядра (docs/PERFORMANCE.md §v2.0.4):
  1. lib/util.fmt_uptime — секунды -> «3д 2ч 5м» (uptime_human в /server/stats).
  2. lib/util.TTLCache — потокобезопасный кеш с TTL и FIFO-вытеснением.
  3. lib/util.TokenBucket — токен-ведро: burst, отказ, retry_after, пополнение.
  4. lib/util.RateLimiter — вёдра по ключам: изоляция, LRU, active().
  5. lib/util.LatencyMeter — счётчик запросов + окно латентности (avg/p95).
  6. link_preview — TTL-кеш превью: повторный URL не качает страницу заново,
     неудачные попытки НЕ кешируются.
  7. Антифлуд: RelayServer.allow_message (границы ведра 1:1 с FLOOD_MSG_*),
     429 с retry_after на /send_text и /dm/send, изоляция отправителей,
     восстановление после паузы; ведро общее для чата и ЛС.
  8. GET /server/stats — новые поля: uptime_human, requests_total, avg_ms,
     p95_ms, flood_buckets.
  9. Совместимость: библиотека lib.client (её импортирует PySide6-приложение)
     продолжает импортироваться; формы /send_text не изменились.
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
from lib.constants import FLOOD_MSG_BURST, FLOOD_MSG_RATE
from lib.relay_server import RelayServer
from lib.util import (
    LatencyMeter,
    RateLimiter,
    TokenBucket,
    TTLCache,
    fmt_uptime,
    header_encode,
)

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


def post_json(base: str, path: str, headers: dict, body: dict):
    """POST JSON с полным набором заголовков; возвращает (status, json|None).
    429 НЕ бросает исключение — это ожидаемый ответ антифлуда."""
    data = json.dumps(body).encode()
    h = dict(headers)
    h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


# ── 1. fmt_uptime ───────────────────────────────────────────────────────
print("— lib.util.fmt_uptime —")
check("секунды", fmt_uptime(42) == "42с")
check("минуты+секунды", fmt_uptime(320) == "5м 20с")
check("часы+минуты", fmt_uptime(7500) == "2ч 5м")
check("дни+часы+минуты", fmt_uptime(269_100) == "3д 2ч 45м")
check("дни+часы без минут", fmt_uptime(266_400) == "3д 2ч")
check("ровно день", fmt_uptime(86_400) == "1д")
check("ноль -> «0с»", fmt_uptime(0) == "0с")
check("мусор -> «0с» (не падает)", fmt_uptime("абракадабра") == "0с")
check("отрицательное -> «0с»", fmt_uptime(-5) == "0с")

# ── 2. TTLCache ─────────────────────────────────────────────────────────
print("— lib.util.TTLCache —")
cache = TTLCache(max_entries=4, ttl=60.0)
cache.put("a", 1)
cache.put("b", {"x": "кириллица"})
check("put/get", cache.get("a") == 1)
check("значение живое (тот же объект)", cache.get("b") == {"x": "кириллица"})
check("нет ключа -> None", cache.get("c") is None)
cache.put("a", 2)
check("перезапись", cache.get("a") == 2)
check("__len__ считает записи", len(cache) == 2)

# TTL: короткий срок жизни -> запись протухает
ttl_cache = TTLCache(max_entries=4, ttl=0.05)
ttl_cache.put("k", "v")
check("свежая запись читается", ttl_cache.get("k") == "v")
time.sleep(0.07)
check("протухла -> None", ttl_cache.get("k") is None)

# FIFO-вытеснение при переполнении
fifo = TTLCache(max_entries=2, ttl=60.0)
fifo.put("1", "первый")
fifo.put("2", "второй")
fifo.put("3", "третий")
check("переполнение вытесняет самую старую", fifo.get("1") is None)
check("свежие живут", fifo.get("2") == "второй" and fifo.get("3") == "третий")
fifo.put("2", "второй-обновлённый")
fifo.put("4", "четвёртый")
check("обновлённый ключ не вытесняется как старый (LRU-перезапись)",
      fifo.get("2") == "второй-обновлённый")
check("вытеснился реально самый старый (3), а не обновлённый",
      fifo.get("3") is None and fifo.get("4") == "четвёртый")
check("clear() опустошает", (fifo.clear(), len(fifo) == 0)[1])

# потокобезопасность: параллельные put/get не роняют и не теряют запись
mt = TTLCache(max_entries=8, ttl=60.0)
errors = []


def _hammer(i: int) -> None:
    try:
        for _ in range(200):
            mt.put(f"k{i % 4}", i)
            mt.get(f"k{i % 4}")
    except Exception as e:  # pragma: no cover - сюда попадать не должны
        errors.append(e)


threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("нити без исключений", not errors)

# ── 3. TokenBucket ──────────────────────────────────────────────────────
print("— lib.util.TokenBucket —")
bucket = TokenBucket(rate=50.0, burst=5.0)
taken = sum(bucket.take() for _ in range(10))
check("burst=5: ровно 5 снимается, остальное отказ", taken == 5)
check("retry_after положительный", bucket.retry_after() > 0)
check("retry_after сходится с rate", bucket.retry_after() <= 1 / 50.0 * 1.01 + 0.001)
time.sleep(0.05)  # 50 ток/с * 0.05с = ~2.5 токена
check("после паузы ведро пополнилось", bucket.take() is True)
big = TokenBucket(rate=1.0, burst=3.0)
check("take(2) двумя токенами", big.take(2) is True)
check("take(2) при одном токене -> отказ", big.take(2) is False)
check("take(1) при одном токене -> успех", big.take(1) is True)

# ── 4. RateLimiter ──────────────────────────────────────────────────────
print("— lib.util.RateLimiter —")
rl = RateLimiter(rate=1.0, burst=2.0, max_keys=4)
check("Вася: два подряд можно", rl.allow("Вася") and rl.allow("Вася"))
check("Вася: третье сразу нельзя", rl.allow("Вася") is False)
check("Аня: своё ведро, не тронуто", rl.allow("Аня") is True)
check("retry_after только у флудера", rl.retry_after("Вася") > 0)
check("active() считает ключи", rl.active() == 2)
for i in range(6):  # выталкивание LRU: ключей больше max_keys
    rl.allow(f"к{i}")
check("LRU не даёт словарю расти", rl.active() <= 4)
check("после вытеснения старый ключ получил новое ведро",
      rl.allow("Вася") is True)

# ── 5. LatencyMeter ─────────────────────────────────────────────────────
print("— lib.util.LatencyMeter —")
meter = LatencyMeter(window=8)
for ms in (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 999.0):
    meter.note(ms)
snap = meter.snapshot()
check("total считает ВСЕ запросы, а не только окно", snap["requests_total"] == 9)
check("p95 окна с выбросом = выброс (999)", snap["p95_ms"] == 999.0)
check("avg в разумных пределах", 0 < snap["avg_ms"] < 1000)
meter2 = LatencyMeter(window=4)
for ms in (10.0, 20.0, 30.0, 40.0):
    meter2.note(ms)
s2 = meter2.snapshot()
check("p95 = максимум окна при 4 замерах", s2["p95_ms"] == 40.0)
check("avg корректен", s2["avg_ms"] == 25.0)
meter2.note(-1)
check("отрицательный замер игнорируется",
      meter2.snapshot()["requests_total"] == 4)
empty = LatencyMeter().snapshot()
check("пустой метр: нули и не падает",
      empty["requests_total"] == 0 and empty["avg_ms"] == 0.0)

# ── 6. кеш превью ссылок ────────────────────────────────────────────────
print("— lib.link_preview: TTL-кеш превью —")
import lib.link_preview as lp

net_calls = 0
_real_net = lp._fetch_link_preview_net


def _fake_net(url, timeout=5):  # подменяем ТОЛЬКО сеть, кеш живой
    global net_calls
    net_calls += 1
    return {"url": url, "title": f"страница {net_calls}",
            "description": None, "image": None}


lp._fetch_link_preview_net = _fake_net
try:
    p1 = lp.fetch_link_preview("https://example.com/mem")
    p2 = lp.fetch_link_preview("https://example.com/mem")
    check("повторный URL не ходит в сеть", net_calls == 1 and p1 is p2)
    lp._PREVIEW_CACHE.clear()
    lp.fetch_link_preview("https://example.com/mem")
    check("после clear() снова качает", net_calls == 2)
    # None не кешируется: неудача повторяется честно
    lp._fetch_link_preview_net = lambda url, timeout=5: None
    check("неудача -> None", lp.fetch_link_preview("https://fail.example/x") is None)
    check("неудача НЕ запомнилась (len кеша не вырос)",
          lp._PREVIEW_CACHE.get("https://fail.example/x") is None)
finally:
    lp._fetch_link_preview_net = _real_net  # чиним модуль для других тестов
    lp._PREVIEW_CACHE.clear()

# ── 7. антифлуд на фасаде (без сети, детерминированно) ──────────────────
print("— RelayServer.allow_message (фасад) —")
with tempfile.TemporaryDirectory(prefix="frelay_v204_flood_") as td:
    server = RelayServer(Path(td), host_name="Хост")
    burst = int(FLOOD_MSG_BURST)
    ok = sum(server.allow_message("Флудер") for _ in range(burst))
    check(f"burst={burst} пропускает ровно {burst}", ok == burst)
    check("следующее сообщение — отказ", server.allow_message("Флудер") is False)
    check("retry_after > 0", server.flood_retry_after("Флудер") > 0)
    check("другой отправитель не задет", server.allow_message("Аня") is True)
    check("пустое имя — отдельный ключ, не ведро «Флудера»",
          server.allow_message("") is True)
    time.sleep(1.0 / FLOOD_MSG_RATE * 1.2)  # ~1.2 токена пополнения
    check("после паузы — снова можно", server.allow_message("Флудер") is True)

    # ── 8. живой сервер: 429, /server/stats, совместимость клиента ──────
    print("— живой сервер: антифлуд 429, метрики в /server/stats —")
    KEY = "ключ-2040"
    server.access_key = KEY
    assert server.start("127.0.0.1", 8861), "server not started"
    time.sleep(0.2)
    base = "http://127.0.0.1:8861"
    kh = {"X-Relay-Key": header_encode(KEY)}

    # сессия для подписанных POST. «Штормист» — СВЕЖЕЕ имя: ведро «Флудера»
    # уже опустошено фасадным тестом выше, а тут нужен полный burst.
    _st, pj = get_json(base, "/ping", dict(kh, **{"X-Relay-From":
                                                  header_encode("Штормист")}))
    tok = (pj or {}).get("session_token", "")
    flood_h = {"X-Relay-Key": kh["X-Relay-Key"],
               "X-Relay-From": header_encode("Штормист")}

    def signed_flood_post(path: str, body: dict):
        raw = json.dumps(body).encode()
        return post_json(base, path, dict(flood_h,
                          **{"X-Relay-Auth": tok + ":" + sign_request(tok, raw)}),
                         body)

    codes = [signed_flood_post("/send_text", {"text": f"ку {i}"})[0]
             for i in range(burst + 12)]
    n200 = codes.count(200)
    check("часть всплеска прошла 200", n200 >= burst // 2)
    check("хвост всплеска получил 429", codes[-1] == 429 and 429 in codes)
    st, j = signed_flood_post("/send_text", {"text": "ещё"})
    check("429 несёт retry_after", st == 429 and (j or {}).get("retry_after", 0) > 0)
    check("в журнале ровно те, что прошли (флуд не пишется)",
          server.get_server_stats()["next_seq"] == n200 + 1)

    # общее ведро чата и ЛС: после шторма ЛС тоже под фильтром
    st_dm, _ = signed_flood_post("/dm/send", {"to": "Аня", "text": "спам"})
    check("429 на /dm/send тем же флудером (общее ведро)", st_dm == 429)

    # другой отправитель спокоен
    _st, pj2 = get_json(base, "/ping", dict(kh, **{"X-Relay-From":
                                                   header_encode("Аня")}))
    tok2 = (pj2 or {}).get("session_token", "")
    raw2 = json.dumps({"text": "привет"}).encode()
    st2, _ = post_json(base, "/send_text",
                       {"X-Relay-Key": kh["X-Relay-Key"],
                        "X-Relay-From": header_encode("Аня"),
                        "X-Relay-Auth": tok2 + ":" + sign_request(tok2, raw2)},
                       {"text": "привет"})
    check("Аня шлёт как ни в чём не бывало (изоляция ведер)", st2 == 200)

    # восстановление после паузы
    time.sleep(1.0 / FLOOD_MSG_RATE * 1.2)
    raw3 = json.dumps({"text": "отошёл, вернулся"}).encode()
    st3, _ = post_json(base, "/send_text",
                       dict(flood_h, **{"X-Relay-Auth":
                                        tok + ":" + sign_request(tok, raw3)}),
                       {"text": "отошёл, вернулся"})
    check("после паузы Штормист снова может писать", st3 == 200)

    # /server/stats: новые поля на месте
    st4, stats = get_json(base, "/server/stats", kh)
    check("/server/stats -> 200", st4 == 200 and (stats or {}).get("ok") is True)
    check("uptime_human строка и согласована с uptime_s",
          isinstance(stats.get("uptime_human"), str)
          and stats["uptime_human"] == fmt_uptime(stats.get("uptime_s", -1)))
    check("requests_total считает замеры",
          isinstance(stats.get("requests_total"), int)
          and stats["requests_total"] >= burst)
    check("avg_ms/p95_ms числа >= 0",
          isinstance(stats.get("avg_ms"), (int, float))
          and isinstance(stats.get("p95_ms"), (int, float))
          and stats["avg_ms"] >= 0 and stats["p95_ms"] >= 0)
    check("flood_buckets живёт", stats.get("flood_buckets", 0) >= 2)

    server.stop()

# ── 9. совместимость с PySide6-клиентом ─────────────────────────────────
print("— совместимость: lib.client импортируется (его тянет Qt-приложение) —")
import importlib

client_mod = importlib.import_module("lib.client")
check("lib.client импортируется без ошибок",
      hasattr(client_mod, "send_text"))
check("send_text остался с прежней сигнатурой",
      client_mod.send_text.__code__.co_argcount >= 3)
from lib.client_core import chat_session, social

check("lib.client_core (ядро Qt-чата) жив", chat_session and social)

print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
