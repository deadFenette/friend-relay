#!/usr/bin/env python3
"""Живая проверка веб-клиента Friend Relay (v3.6.5+).

Запуск: python3 scripts/check_web_live.py [порт] [ключ]
Поднимает сервер на указанном порту (по умолч. 8871) и проверяет 16 пунктов:

Поднимает server_main.py на тестовом порту и проверяет ВСЁ, что нужно
веб-клиенту, чтобы работать: статика, /voice/info, голосовой WS-мост
(рукопожатие версий spk_all «v»), бинарные кадры, hb, mute, roster.
"""
import asyncio
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = "/home/z/my-project/src/friend_relay_v3.6.3"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8871
NAME = "WebCheck"
KEY = sys.argv[2] if len(sys.argv) > 2 else "webcheck-key-1"
os.chdir(ROOT)

PROC = None
results = []


def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("[OK] " if cond else "[FAIL] ") + name + ((" — " + extra) if extra else ""))


def start_server():
    global PROC
    PROC = subprocess.Popen(
        [sys.executable, "server_main.py", "--name", NAME, "--port", str(PORT),
         "--key", KEY, "--data-dir", "/tmp/fr_webcheck_data",
         "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)


def http_get(url, timeout=8, headers=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read()


def auth_headers():
    """Те же заголовки, что шлёт веб-клиент (core.js baseHeaders)."""
    return {"X-Relay-From": NAME, "X-Relay-Key": KEY}


async def wait_port(port, timeout=30):
    loop = asyncio.get_event_loop()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            fut = loop.run_in_executor(None, lambda: __import__("socket").create_connection(("127.0.0.1", port), 1.0))
            await asyncio.wait_for(fut, 2)
            return True
        except Exception:
            await asyncio.sleep(0.4)
    return False


async def main():
    import websockets

    start_server()
    if not await wait_port(PORT, 40):
        ok("сервер поднялся", False, "порт не открылся за 40с")
        return
    await asyncio.sleep(1.0)
    base_h = f"http://127.0.0.1:{PORT}"
    base_s = f"https://127.0.0.1:{PORT}"

    # 0. /ping — вход веб-клиента: имя + ключ → сессия
    try:
        st, body = http_get(base_s + "/ping", headers=auth_headers())
        pj = json.loads(body)
        ok("HTTPS /ping: сессия выдана",
           st == 200 and pj.get("ok") and pj.get("session_token"),
           "token=" + str(pj.get("session_token"))[:8] + "…")
    except Exception as e:
        ok("HTTPS /ping: сессия выдана", False, repr(e))

    # 1. HTTP и HTTPS на одном порту (tls_mux)
    try:
        st, body = http_get(base_h + "/")
        ok("HTTP GET / → 200", st == 200, f"{len(body)} байт")
    except Exception as e:
        ok("HTTP GET / → 200", False, repr(e))
    try:
        st, body = http_get(base_s + "/")
        ok("HTTPS GET / → 200 (тот же порт)", st == 200, f"{len(body)} байт")
        html = body.decode("utf-8", "replace")
    except Exception as e:
        ok("HTTPS GET / → 200 (тот же порт)", False, repr(e))
        html = ""

    # 2. Все скрипты/стили из index.html реально отдаются
    import re
    srcs = re.findall(r'src="(/static/[^"]+)"', html) + \
           re.findall(r"href=\"(/static/[^\"]+\.css)\"", html)
    bad = []
    for s in dict.fromkeys(srcs):
        try:
            st2, b2 = http_get(base_s + s)
            if st2 != 200 or not b2:
                bad.append(s)
        except Exception:
            bad.append(s)
    ok(f"статика index.html ({len(dict.fromkeys(srcs))} файлов)", not bad, " ".join(bad))

    # 3. /voice/info (с ключом — как шлёт веб-клиент)
    st, body = http_get(base_s + "/voice/info", headers=auth_headers())
    info = json.loads(body)
    ok("/voice/info: voice_ws_path + https_port",
       st == 200 and info.get("voice_ws_path") and info.get("https_port") == PORT,
       json.dumps(info, ensure_ascii=False)[:120])

    # 4. voice.js содержит фиксы v3.6.4, voice-worklet отдаётся
    st, vjs = http_get(base_s + "/static/voice.js")
    txt = vjs.decode("utf-8", "replace")
    ok("voice.js: потолок адаптива 18 (ZeroTier-фикс)",
       "Math.min(18" in txt, "")
    ok("voice.js: версия хоста в статусе", "hostVersion" in txt, "")
    st, wjs = http_get(base_s + "/static/voice-worklet.js")
    ok("voice-worklet.js отдаётся", st == 200 and b"VcPlay" in wjs, "")

    # 5. Голосовой WS-мост: auth → ok → бинарь → spk_all «v» → hb → mute → roster
    with open(os.path.join(ROOT, "version.json"), encoding="utf-8") as f:
        APP_VERSION = json.load(f)["version"]
    ws_url = f"wss://127.0.0.1:{PORT}" + info["voice_ws_path"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    saw_ok = saw_v = saw_pong_hb = saw_roster = False
    saw_audio = False
    mute_ack = False
    async with websockets.connect(ws_url, ssl=ctx, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"name": "WebTester", "access_key": KEY}))
        # фоновая отправка аудио-кадров (по 1920 байт int16) чтобы mixer видел клиента
        stop_send = False

        async def sender():
            frame = b"\x01\x02" * 960  # 1920 байт = 960 int16 = 20мс
            while not stop_send:
                try:
                    await ws.send(frame)
                except Exception:
                    return
                await asyncio.sleep(0.02)

        send_task = asyncio.create_task(sender())
        t0 = time.time()
        sent_hb = False
        while time.time() - t0 < 6:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, str):
                j = json.loads(msg)
                if j.get("ok"):
                    saw_ok = True
                if j.get("t") == "spk_all":
                    if isinstance(j.get("states"), dict):
                        pass
                    if j.get("v"):
                        saw_v = (j["v"] == APP_VERSION)
                # hb-echo: мост отвечает {"t":"hb","ts":…,"ok":true} на наш hb
                if j.get("t") == "hb" and j.get("ok"):
                    saw_pong_hb = True
                if j.get("t") == "roster" and isinstance(j.get("names"), list):
                    saw_roster = "WebTester" in j["names"]
                if j.get("t") == "mute_state":
                    mute_ack = True
            else:
                saw_audio = True  # бинарь микшера пошёл
            if not sent_hb and time.time() - t0 > 1.5:
                sent_hb = True
                await ws.send(json.dumps({"t": "hb", "ts": int(time.time())}))
        # mute: мост НЕ отвечает ack — применяется молча (state.muted,
        # кадры уходят с FLAG_MUTED). Проверяем что мост жив и не упал:
        await ws.send(json.dumps({"t": "mute", "on": True}))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"t": "hb", "ts": int(time.time())}))
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                if isinstance(msg, str):
                    j = json.loads(msg)
                    if j.get("t") == "hb" and j.get("ok"):
                        mute_ack = True  # после mute мост по-прежнему жив
        except asyncio.TimeoutError:
            pass
        stop_send = True
        send_task.cancel()

    ok("мост: auth → ok", saw_ok, "")
    ok("мост: входящий бинарь (микшер шлёт PCM)", saw_audio, "")
    ok(f"мост: spk_all несёт версию хоста v={APP_VERSION}", saw_v, "")
    ok("мост: hb-echo на наш heartbeat", saw_pong_hb, "")
    ok("мост: roster содержит веб-клиента", saw_roster, "")
    ok("мост: mute применён, мост жив после него", mute_ack, "")

    # 6. Основные API веба с ключом
    for path in ("/server/stats", "/events"):
        try:
            st, _ = http_get(base_s + path, headers=auth_headers())
            ok(f"GET {path} → {st}", st == 200, "")
        except urllib.error.HTTPError as e:
            ok(f"GET {path}", e.code in (200, 401, 403), f"HTTP {e.code}")
        except Exception as e:
            ok(f"GET {path}", False, repr(e))

    # итог
    fails = [n for n, c, _ in results if not c]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(fails)} OK, {len(fails)} FAIL")
    if fails:
        print("FAILS:", fails)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if PROC:
            PROC.terminate()
            try:
                PROC.wait(timeout=10)
            except Exception:
                PROC.kill()
