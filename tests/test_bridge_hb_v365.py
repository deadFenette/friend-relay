#!/usr/bin/env python3
"""v3.6.5: фикс веб-моста голоса (/voice/ws) — hb-кадры и конкурентные send.

Что сломалось и как искали (история):
  - Живая проверка веба после v3.6.4: hb-echo от моста не приходил.
  - Диагноз 1: bridge.py отвечал на {«t»:«hb»} через ws.send(json.dumps(...).encode()).
    encode → БИНАРНЫЙ WS-кадр: браузер получает JSON как «аудио», парсить
    его не может (control-путь у voice.js смотрит typeof ev.data === "string").
  - Диагноз 2: при активном аудио второй насос непрерывно шлёт PCM в тот же
    websocket; websockets 14+ запрещает конкурентные send() из двух задач
    (ConcurrencyError) — echo молча терялся ещё чаще (except Exception: pass).

Фикс v3.6.5: общий asyncio.Lock на отправку в обеих насос-тасках + все
контрольные кадры моста (hb-echo, keepalive) шлются str (текстовый кадр).

Проверки здесь:
  A. Живой сервер (RelayServer целиком, как в бою):
     1) auth→ok, spk_all, roster приходят ТЕКСТОМ;
     2) hb-echo — ТЕКСТ {«t»:«hb»,«ts»,«ok»:true} (без аудио);
     3) hb-echo — ТЕКСТ и при потоке аудио (лок работает), бинарь идёт;
     4) mute не ломает мост (echo после mute);
     5) мусорный текст не рвёт соединение;
     6) {«t»:«ping»,«id»} проксируется МИКШЕРУ → текстовый pong со
        счётчиками (семантика изменена в v3.6.6 — R4-качество веба;
        раньше ping был алиасом heartbeat'а и до микшера не доходил);
     7) spk_all несёт версию хоста «v» (динамически из version.json).
  B. По исходнику voice/bridge.py (только кодовые строки):
     8) общий лок существует и используется в ОБОИХ насосах;
     9) ни один ws.send больше не кодирует JSON в байты.
"""
from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("[OK] " if cond else "[FAIL] ") + name + ((" — " + extra) if extra else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def ws_probe(base: int, key: str, version: str) -> None:
    import websockets

    uctx = ssl._create_unverified_context()
    info: dict | None = None
    try:
        import urllib.request
        resp = urllib.request.urlopen(urllib.request.Request(
            f"https://127.0.0.1:{base}/voice/info",
            headers={"X-Relay-From": "Test", "X-Relay-Key": key}),
            context=uctx, timeout=5)
        info = json.loads(resp.read())
    except Exception as e:
        check("ws: /voice/info", False, repr(e))
        return
    check("ws: /voice/info отдаёт voice_ws_path",
          bool(info and info.get("voice_ws_path")))
    url = f"wss://127.0.0.1:{base}" + info["voice_ws_path"]

    async with websockets.connect(url, ssl=uctx, max_size=2 ** 22) as ws:
        texts: list[dict] = []
        binary = 0
        stop = False

        async def sender() -> None:
            # «Аудио»: int16-паттерн заметно выше VAD-порога
            frame = (b"\x64\x10" * 960)  # ~4100 по шкале int16
            while not stop:
                try:
                    await ws.send(frame)
                except Exception:
                    return
                await asyncio.sleep(0.02)

        async def drain(until: float, want_binary: bool) -> None:
            nonlocal binary
            while time.monotonic() < until:
                try:
                    msg = await asyncio.wait_for(ws.recv(),
                                                 timeout=max(0.05, until - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, str):
                    try:
                        texts.append(json.loads(msg))
                    except json.JSONDecodeError:
                        pass
                elif want_binary:
                    binary += 1

        # 1) auth → ok
        await ws.send(json.dumps({"name": "ВебТест", "access_key": key}))
        await drain(time.monotonic() + 1.5, False)
        ok_msg = next((j for j in texts if j.get("ok")), None)
        check("1. auth→ok приходит ТЕКСТОМ", ok_msg is not None)
        spk = next((j for j in texts if j.get("t") == "spk_all"), None)
        check("2. spk_all — текст, states+codec+«v»",
              spk is not None and isinstance(spk.get("states"), dict)
              and spk.get("codec") in ("pcm", "opus") and spk.get("v") == version,
              f"v={spk.get('v') if spk else None}")
        check("3. roster — текст, имя в составе",
              any(j.get("t") == "roster" and "ВебТест" in j.get("names", [])
                  for j in texts))

        # 2) hb-echo БЕЗ аудио — текст, ts эхом
        texts.clear()
        await ws.send(json.dumps({"t": "hb", "ts": 777}))
        await drain(time.monotonic() + 1.5, False)
        echo = next((j for j in texts
                     if j.get("t") == "hb" and j.get("ok")), None)
        check("4. hb-echo — ТЕКСТ с ok:true и ts=777",
              echo is not None and echo.get("ts") == 777,
              str(echo))

        # 3) hb-echo ПРИ потоке аудио (лок против ConcurrencyError)
        texts.clear()
        binary = 0
        stop = False
        task = asyncio.create_task(sender())
        await asyncio.sleep(1.0)          # PCM-поток уже идёт
        await ws.send(json.dumps({"t": "hb", "ts": 888}))
        await drain(time.monotonic() + 2.0, True)
        stop = True
        try:
            await asyncio.wait_for(task, 2)
        except Exception:
            pass
        echo2 = next((j for j in texts
                      if j.get("t") == "hb" and j.get("ok")), None)
        check("5. hb-echo ПРИ аудио — ТЕКСТ (лок работает)",
              echo2 is not None and echo2.get("ts") == 888, str(echo2))
        check("6. входящий бинарь (микшер шлёт PCM) при аудио",
              binary > 20, f"кадров: {binary}")

        # 4) mute + мусор + ping — мост жив. v3.6.6: ping больше НЕ алиас
        #    heartbeat'а — он уходит микшеру и возвращается pong'ом (R4).
        texts.clear()
        await ws.send("это не json {{{")
        await ws.send(json.dumps({"t": "mute", "on": True}))
        await ws.send(json.dumps({"t": "ping", "id": 999}))
        await drain(time.monotonic() + 2.0, False)
        pong = next((j for j in texts
                     if j.get("t") == "pong" and j.get("id") == 999), None)
        check("7. mute+мусор не рвут мост; ping→pong от микшера (v3.6.6)",
              pong is not None, str(pong))


def source_checks() -> None:
    src = (ROOT / "voice/bridge.py").read_text(encoding="utf-8")
    check("8. общий send_lock создаётся в _handler",
          "send_lock = asyncio.Lock()" in src)
    check("9. лок используется в обоих насосах (>=2 ws_send-определения + async with)",
          src.count("async with send_lock:") >= 2
          and src.count("async def ws_send(data):") == 2)
    # Ни один ws_send/ws.send не получает json.dumps(...).encode(...)
    bad = [ln for ln in src.splitlines()
           if ("ws.send" in ln or "ws_send(" in ln)
           and '.encode("utf-8")' in ln]
    check("10. контрольные кадры в ws больше не кодируются в байты", not bad,
          "; ".join(bad[:2]))
    keepalive_ok = ('{"t": "hb", "ts": int(time.time())}))' in src)
    echo_ok = ('"ok": True}))' in src)
    check("11. keepalive и hb-echo шлются str (текстовый кадр)",
          keepalive_ok and echo_ok)


def main() -> int:
    version = json.loads((ROOT / "version.json").read_text(
        encoding="utf-8")).get("version", "?")
    import websockets  # noqa: F401 — ранний фейл, если библиотеки нет
    source_checks()

    base = free_port()
    with tempfile.TemporaryDirectory(prefix="fr_bridge_hb_") as tmp:
        from lib.relay_server import RelayServer
        relay = RelayServer(Path(tmp), host_name="Хост",
                            access_key="k3", max_file_size=1024 * 1024)
        if not relay.start("127.0.0.1", base):
            check("сервер стартовал", False, "порт занят?")
            return _summary()
        try:
            asyncio.run(ws_probe(base, "k3", version))
        finally:
            relay.stop()
    return _summary()


def _summary() -> int:
    fails = [n for n, c, _ in RESULTS if not c]
    print("\n" + "=" * 60)
    print(f"Итог: {len(RESULTS) - len(fails)} OK, {len(fails)} FAIL")
    if fails:
        print("FAIL:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
