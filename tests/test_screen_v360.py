#!/usr/bin/env python3
"""Тесты модуля «Экран» v3.6.0 — демонстрация экрана со звуком.

Серверная часть (WebRTC-сигналинг через HTTP, медиа P2P — вне сервера):
  - ScreenHub как юнит: publish/heartbeat/unpublish, watch, snapshot;
  - сигналинг-почта: seq-курсор, опустошение, переполнение (выкидывает
    самое старое), лимит размера, TTL по фейковому времени;
  - живой RelayServer: /screen/info, /screen/publish (анонс в чат!),
    /screen/signal, /screen/poll (подпись X-Relay-Auth обязательна),
    /screen/watch (счётчик зрителей), ливность (показ гаснет сам);
  - раздача клиента: /static/screen.js, вкладка в index.html,
    каркас в main.js (TAB_NAMES), карточка в nochnik.js (AMBIENT),
    иконка в спрайте, стили в style.css.

Запуск: python tests/test_screen_v360.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auth import sign_request
from lib.relay_server import RelayServer
from lib.util import header_encode
from screen.hub import ScreenHub

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


# ═══════════════════════ 1. ScreenHub как юнит ═══════════════════════
print("— ScreenHub: реестр показов —")
hub = ScreenHub()
check("пустой snapshot", hub.snapshot() == [])
check("publish: новый показ → True", hub.publish("Вася", "игра") is True)
check("publish: heartbeat → False (не новый)", hub.publish("Вася", "игра") is False)
snap = hub.snapshot()
check("snapshot: один живой показ с заголовком",
      len(snap) == 1 and snap[0]["from"] == "Вася" and snap[0]["title"] == "игра")
check("watch: зритель зарегистрирован",
      hub.watch("Петя", "Вася", True) is True)
check("watch: неуда́чный показ → False", hub.watch("Петя", "НезнаюКто", True) is False)
snap = hub.snapshot()
check("snapshot: зритель в списке", snap[0]["viewers"] == ["Петя"])
check("watch off: зритель ушёл",
      hub.watch("Петя", "Вася", False) is True and hub.snapshot()[0]["viewers"] == [])
check("unpublish: показ снят", hub.unpublish("Вася") is True)
check("unpublish: повторно → False", hub.unpublish("Вася") is False)

print("— ScreenHub: сигналинг-почта —")
hub = ScreenHub()
s1 = hub.send_signal("Аня", "Боря", {"type": "hello"})
s2 = hub.send_signal("Аня", "Боря", {"type": "offer", "sdp": {"type": "offer", "sdp": "x"}})
check("seq монотонный", isinstance(s1, int) and isinstance(s2, int) and s2 > s1)
msgs, last = hub.poll("Боря", 0)
check("poll: оба письма по порядку",
      len(msgs) == 2 and msgs[0]["data"]["type"] == "hello"
      and msgs[1]["frm"] == "Аня" and last == s2)
msgs, last = hub.poll("Боря", s2)
check("poll: курсор опустошил почту", msgs == [] and last == s2)
msgs, _ = hub.poll("Боря", s1)
check("poll: since=s1 отдаёт только хвост", len(msgs) == 1
      and msgs[0]["data"]["type"] == "offer")
check("poll: чужая почта пуста", hub.poll("Кто-то", 0) == ([], 0))
# переполнение: MAILBOX_MAX писем → самое старое выбрасывается
from screen.config import MAILBOX_MAX, MAX_SIGNAL_BYTES

hub = ScreenHub()
for i in range(MAILBOX_MAX + 5):
    hub.send_signal("Флудер", "Жертва", {"i": i})
msgs, _ = hub.poll("Жертва", 0)
check(f"переполнение: почта зажата до {MAILBOX_MAX}, старое выброшено",
      len(msgs) == MAILBOX_MAX and msgs[0]["data"]["i"] == 5
      and msgs[-1]["data"]["i"] == MAILBOX_MAX + 4
      and hub.dropped_signals == 5)
big = {"sdp": "x" * (MAX_SIGNAL_BYTES + 1)}
check("лимит размера: слишком большой сигнал отброшен",
      hub.send_signal("Аня", "Боря", big) is None and hub.dropped_signals >= 1)

print("— ScreenHub: ливность (TTL по фейковому времени) —")
hub = ScreenHub()
_real_time = time.time
_t = _real_time()
try:
    import screen.hub as hub_mod
    hub_mod.time.time = lambda: _t  # фейковые часы
    hub.publish("Лентяй", "")
    _t += 10.0
    check("показ жив до STREAM_LIVE_S", len(hub.snapshot()) == 1)
    _t += 40.0  # 50с после последнего heartbeat (STREAM_LIVE_S = 30)
    snap = hub.snapshot()
    check("показ сам погас без heartbeat", snap == [])
finally:
    hub_mod.time.time = _real_time

# ═══════════════════════ 2. Живой сервер ═══════════════════════
print("— живой сервер: маршруты /screen/* —")
tmp = Path(tempfile.mkdtemp(prefix="screen_v360_"))
KEY = "ключ-экрана"
relay = RelayServer(tmp, host_name="Хост", access_key=KEY,
                    max_file_size=10 * 1024 * 1024)
port = 18587
assert relay.start("127.0.0.1", port), "сервер не поднялся"
base = f"http://127.0.0.1:{port}"
kh = {"X-Relay-Key": header_encode(KEY)}


def login(name: str) -> dict:
    st, pj = get_json(base, "/ping", dict(kh, **{"X-Relay-From": header_encode(name)}))
    assert st == 200 and pj and pj.get("session_token"), f"ping {name}: {st}"
    tok = pj["session_token"]

    def signed(path: str, body: dict):
        raw = json.dumps(body).encode()
        h = dict(kh, **{"X-Relay-From": header_encode(name),
                        "X-Relay-Auth": tok + ":" + sign_request(tok, raw)})
        return post_json(base, path, h, body)

    def poll(since: int = 0):
        path = f"/screen/poll?since={since}"
        h = dict(kh, **{"X-Relay-From": header_encode(name)})
        # подпись GET: ровно как lib/auth.auth_header_for_get (path+query)
        h["X-Relay-Auth"] = tok + ":" + sign_request(tok, path.encode("utf-8"))
        return get_json(base, path, h)

    return {"signed": signed, "poll": poll}


try:
    a = login("Ведущий")
    b = login("Зритель")

    st, j = get_json(base, "/screen/info", dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    check("/screen/info: пусто на старте",
          st == 200 and j and j["ok"] is True and j["streams"] == [])

    # publish без подписи → 403 (центральная аутентификация POST)
    st, _ = post_json(base, "/screen/publish",
                      dict(kh, **{"X-Relay-From": header_encode("Ведущий")}), {"on": True})
    check("/screen/publish без X-Relay-Auth → 403", st == 403)

    # poll без подписи → 403 (AUTH_REQUIRED_GET)
    st, _ = get_json(base, "/screen/poll?since=0",
                     dict(kh, **{"X-Relay-From": header_encode("Ведущий")}))
    check("/screen/poll без подписи → 403", st == 403)

    st, j = a["signed"]("/screen/publish", {"on": True, "title": "игра"})
    check("publish: 200 ok", st == 200 and j and j["ok"] is True)
    st, j = get_json(base, "/screen/info", dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    check("info: показ виден всем",
          st == 200 and len(j["streams"]) == 1
          and j["streams"][0]["from"] == "Ведущий")

    st, j = a["signed"]("/screen/publish", {"on": True})
    st, j = get_json(base, "/screen/info", dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    check("heartbeat: не плодит второй показ", len(j["streams"]) == 1)

    # сигналинг Ведущий → Зритель и обратно
    st, j = a["signed"]("/screen/signal",
                        {"to": "Зритель", "data": {"type": "offer", "sdp": "sdp3"}})
    check("signal: 200 с seq", st == 200 and j and j["ok"] is True)
    st, j = b["poll"]()
    check("poll: адресат получил письмо",
          st == 200 and len(j["signals"]) == 1
          and j["signals"][0]["frm"] == "Ведущий"
          and j["signals"][0]["data"]["type"] == "offer"
          and j["since"] == j["signals"][0]["seq"])
    st, j = b["poll"](j["since"])
    check("poll: повторно пусто", st == 200 and j["signals"] == [])

    st, j = b["signed"]("/screen/watch", {"to": "Ведущий", "on": True})
    check("watch: 200", st == 200 and j and j["ok"] is True)
    st, j = get_json(base, "/screen/info", dict(kh, **{"X-Relay-From": header_encode("Ведущий")}))
    check("info: счётчик зрителей ведётся",
          j["streams"][0]["viewers"] == ["Зритель"])

    # объявление о показе попало в общий журнал (видно в /events)
    st, j = get_json(base, "/events?since=0",
                     dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    texts = json.dumps(j, ensure_ascii=False)
    check("анонс о показе — в общем чате", "начал показ экрана" in texts)

    st, j = a["signed"]("/screen/publish", {"on": False})
    st, j = get_json(base, "/screen/info", dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    check("unpublish: показ исчез из info", j["streams"] == [])
    st, j = get_json(base, "/events?since=0",
                     dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    texts = json.dumps(j, ensure_ascii=False)
    check("«закончил показ» — в общем чате", "закончил показ" in texts)

    # сигнал без data → 400
    st, _ = a["signed"]("/screen/signal", {"to": "Зритель"})
    check("signal без data → 400", st == 400)

    # /server/stats несёт счётчики экрана
    st, j = get_json(base, "/server/stats", dict(kh, **{"X-Relay-From": header_encode("Зритель")}))
    check("/server/stats: счётчики screen",
          st == 200 and isinstance(j.get("screen"), dict)
          and "streams" in j["screen"])
finally:
    relay.stop()

# ═══════════════════════ 3. Клиент: файлы и каркас ═══════════════════════
print("— веб-клиент: файлы и проводка —")
web = Path(__file__).resolve().parent.parent / "web_client"
scr = (web / "screen.js").read_text(encoding="utf-8")
idx = (web / "index.html").read_text(encoding="utf-8")
main_js = (web / "main.js").read_text(encoding="utf-8")
nk_js = (web / "nochnik.js").read_text(encoding="utf-8")
icons = (web / "icons.js").read_text(encoding="utf-8")
sty = (web / "style.css").read_text(encoding="utf-8")
pal = (web / "palette.js").read_text(encoding="utf-8")

check("index.html: вкладка «Экран» в панели табов",
      'data-tab="screen"' in idx and 'id="tab-screen"' in idx
      and "/static/screen.js" in idx)
check("index.html: порядок скриптов (screen.js до appearance.js)",
      idx.find("/static/screen.js") < idx.find("/static/appearance.js")
      and idx.find("/static/screen.js") > idx.find("/static/voice.js"))
check("screen.js: getDisplayMedia + системный звук + RTCPeerConnection",
      "getDisplayMedia" in scr and 'audio: true' in scr
      and "RTCPeerConnection" in scr and "systemAudio" in scr)
check("screen.js: хендшейк hello/offer/answer/bye",
      '"hello"' in scr and '"offer"' in scr
      and '"answer"' in scr and '"bye"' in scr)
check("screen.js: heartbeat ведущего и non-trickle ICE",
      "SCREEN_BEAT_MS" in scr and "iceGatheringState" in scr
      and "STUN" in scr)
check("screen.js: публичный хук для скинов", "function screenState()" in scr)
check("main.js: вкладка в TAB_NAMES и тик в pollTick",
      '"screen"' in main_js.split("TAB_NAMES", 1)[1][:120]
      and "typeof screenTick === \"function\"" in main_js)
check("nochnik.js: карточка «Экран» в AMBIENT + CARD_META",
      '"screen"' in nk_js.split("const AMBIENT", 1)[1][:80]
      and 'screen: {icon: "screen", title: "Экран"}' in nk_js)
check("icons.js: спрайт i-screen и карта эмодзи",
      'symbol id="i-screen"' in icons and '"🖥": "screen"' in icons)
check("style.css: стили панели экрана",
      ".screenvideo" in sty and ".screenrow" in sty
      and "aspect-ratio:16/9" in sty.replace(" ", ""))
check("palette.js: вкладка в Ctrl+K с горячей клавишей",
      'tab: "screen"' in pal)

# живая раздача статики
tmp2 = Path(tempfile.mkdtemp(prefix="screen_v360_web_"))
relay2 = RelayServer(tmp2, host_name="Хост", access_key="",
                     max_file_size=10 * 1024 * 1024)
port2 = 18588
assert relay2.start("127.0.0.1", port2), "сервер 2 не поднялся"
base2 = f"http://127.0.0.1:{port2}"
try:
    st, j = get_json(base2, "/screen/info", {"X-Relay-From": header_encode("Гость")})
    check("без ключа /screen/info тоже отвечает (ключ пустой)", st == 200)
    r = urllib.request.Request(base2 + "/static/screen.js")
    with urllib.request.urlopen(r, timeout=5) as resp:
        body = resp.read()
        check("/static/screen.js отдаётся как javascript",
              "javascript" in resp.headers.get("Content-Type", "")
              and b"getDisplayMedia" in body)
    r = urllib.request.Request(base2 + "/")
    with urllib.request.urlopen(r, timeout=5) as resp:
        page = resp.read().decode("utf-8")
        check("страница содержит вкладку «Экран» и панель",
              'data-tab="screen"' in page and 'id="tab-screen"' in page
              and 'id="btnScreenShare"' in page)
finally:
    relay2.stop()

# ═══════════════════════ итог ═══════════════════════
print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
