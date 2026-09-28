#!/usr/bin/env python3
"""v3.6.6: R4-телеметрия качества (пинг/потери) + «голос насмерть».

Два корня этой итерации:

1. «НАСМЕРТЬ» — последний блокирующий sendall в голосовом стеке.
   До v3.6.6 mixer._tick_once звал c.flush() → conn.sendall() ПРЯМО в
   потоке такта: у одного клиента с замиравшим TCP (ZeroTier-ретрансмиты,
   нулевое окно) sendall вставал на сотни мс — и такт микшера стоял
   ВМЕСТЕ с ним: ТИШИНА У ВСЕХ УЧАСТНИКОВ. Теперь у каждого клиента свой
   поток-отправитель, flush() только будит событие.

2. «R4» — качество связи в ВЕБ-клиенте. В Qt строка пинг/потерь есть с
   v3.5.7, в вебе — не было: мост считал {t:ping} heartbeat-алиасом и до
   микшера он не доходил. Теперь мост проксирует ping микшеру, pong со
   счётчиками (in/out/odd) возвращается браузеру текстом, voice.js
   считает RTT и честные потери ↑↓ — как Qt-движок.

Проверки:
  A. Микшер — отправка не в такте:
     A1) flush() при 128КБ в очереди и нечитающем пире < 20мс (раньше —
         блокировка навсегда);
     A2) заблокированный на sendall клиент НЕ мешает такту: второй
         клиент продолжает получать кадры (живой микшер, 3 клиента);
     A3) после разблокировки пира очередь доходит (порядок цел).
  B. Мост — ping браузера доходит до микшера:
     B1) auth→ok;
     B2) {t:ping,id} → текстовый pong с in/out/odd (id эхом);
     B3) pong.in >= числа отправленных браузером голосовых кадров;
     B4) hb-echo НЕ сломан (регресс v3.6.5);
     B5) мусорный текст не рвёт мост.
  C. Исходники:
     C1) node --check voice.js;
     C2) voice.js: ping-таймер 2с, обработчик pong, потери ↑↓ в строке,
         пороги 80/200/2/8, сброс при реконнекте/отключении, счётчики
         sent/recv;
     C3) bridge.py: ping проксируется микшеру FRVC-control кадром.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("[OK] " if cond else "[FAIL] ") + name + ((" — " + extra) if extra else ""))


# ═════════════════════ A. Микшер: отправка не в такте ═════════════════════

def part_a_unit() -> None:
    """A1: flush() не блокируется при полном буфере и молчащем пиру."""
    from voice.mixer import _Client
    from voice.protocol import PCM_FRAME_BYTES

    a, peer = socket.socketpair()
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4608)
    c = _Client(a, ("127.0.0.1", 0), "Stall")
    frame = b"\x00" * PCM_FRAME_BYTES
    for _ in range(400):          # 400×1920=768КБ → обрежется до лимита 128КБ
        c.enqueue_outgoing(frame)
    t0 = time.perf_counter()
    c.flush()
    dt = time.perf_counter() - t0
    check("A1. flush() при полном буфере и молчащем пире < 20мс",
          dt < 0.02, f"{dt * 1000:.1f}мс")
    check("A1b. очередь не выкинута flush'ем (шлёт поток, не flush)",
          len(c._send_buf) > 0, f"{len(c._send_buf)} байт в очереди")

    # Поток застревает в sendall (пир не читает), но жив; пир читает —
    # данные доходят, порядок цел (кадры 1920 байт).
    c.start_sender()
    time.sleep(0.3)
    check("A1c. поток-отправитель жив (заблокирован на sendall)",
          c._sender_thread.is_alive())
    peer.settimeout(3)
    got = peer.recv(65536)
    check("A1d. разблокировали пира — данные пошли", len(got) > 0,
          f"{len(got)} байт")
    c.stop_sender()
    a.close()
    peer.close()


def _mixer_connect(port: int, name: str, rcvbuf: int = 0) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.sendall(f"{name}|\n".encode())
    s.settimeout(5)
    resp = s.recv(64)
    if resp != b"AUTH_OK\n":
        raise RuntimeError(f"{name}: auth fail: {resp!r}")
    s.settimeout(None)
    return s


def _voice_sender(s: socket.socket, stop: threading.Event) -> None:
    """Шлёт голосовые кадры 20мс с RMS выше VAD (реальная речь)."""
    from voice.protocol import make_frame
    payload = (b"\x64\x10" * 960)      # ~4100 по шкале int16 — голос
    frm = make_frame(0x00, payload)
    while not stop.is_set():
        try:
            s.sendall(frm)
        except OSError:
            return
        time.sleep(0.02)


def _reader(s: socket.socket, counter: list) -> None:
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return
            counter[0] += len(chunk)
    except OSError:
        return


def part_a_live() -> None:
    """A2: замиравший клиент не останавливает такт — другие получают звук."""
    from voice.mixer import VoiceMixer
    from voice.protocol import PCM_FRAME_BYTES

    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    if not mixer.start():
        check("A2. микшер стартовал", False)
        return
    try:
        port = mixer._sock.getsockname()[1]
        a = _mixer_connect(port, "А")
        b = _mixer_connect(port, "В_Залип", rcvbuf=4096)   # НИКОГДА не читает
        c = _mixer_connect(port, "С")

        # Зажимаем и СЕРВЕРНУЮ сторону B: kernel-буферы наполнятся за ~0.1с
        bobj = next(x for x in mixer._clients if x.name == "В_Залип")
        bobj.conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4608)

        ca = [0]; cc = [0]
        ta = threading.Thread(target=_reader, args=(a, ca), daemon=True)
        tc = threading.Thread(target=_reader, args=(c, cc), daemon=True)
        ta.start(); tc.start()

        stop = threading.Event()
        ts = threading.Thread(target=_voice_sender, args=(a, stop), daemon=True)
        ts.start()

        # Ждём, пока очередь B упрётся в приложение (залипание доказано)
        deadline = time.time() + 6
        while time.time() < deadline and len(bobj._send_buf) < 16384:
            time.sleep(0.05)
        stalled = len(bobj._send_buf) >= 16384
        check("A2a. клиент B реально залип (очередь растёт, пир не читает)",
              stalled, f"{len(bobj._send_buf)} байт")
        if not stalled:
            stop.set()
            return

        # Пока B залип: flush() мгновенен И C продолжает получать кадры
        t0 = time.perf_counter()
        bobj.flush()
        dt = time.perf_counter() - t0
        check("A2b. flush() залипшего клиента < 20мс (такт не ждёт)",
              dt < 0.02, f"{dt * 1000:.1f}мс")

        c1 = cc[0]
        time.sleep(0.6)
        growth = cc[0] - c1
        # 0.6с × 50 кадров × 1920Б = 57.6КБ максимум; порог — 5 кадров
        check("A2c. клиент C получает звук ПОКА B залип (>5 кадров за 0.6с)",
              growth > 5 * PCM_FRAME_BYTES, f"+{growth} байт за 0.6с")

        stop.set()
        # A3: разблокируем B — накопленное должно уйти (не потеряно)
        got_total = 0
        b.settimeout(2)
        try:
            while got_total < 8 * PCM_FRAME_BYTES:
                chunk = b.recv(65536)
                if not chunk:
                    break
                got_total += len(chunk)
        except OSError:
            pass
        check("A3. после разблокировки B очередь доходит (кадры не потеряны)",
              got_total >= 8 * PCM_FRAME_BYTES, f"{got_total} байт")

        ta.join(timeout=1); tc.join(timeout=1)
        for s in (a, b, c):
            try: s.close()
            except OSError: pass
    finally:
        mixer.stop()


# ═════════════════════ B. Мост: ping браузера → pong микшера ═════════════════════

async def _bridge_probe(mixer_port: int, bridge_port: int) -> None:
    import websockets

    async with websockets.connect(
            f"ws://127.0.0.1:{bridge_port}", max_size=2 ** 22) as ws:
        # B1: auth → ok
        await ws.send(json.dumps({"name": "ВебР4", "access_key": ""}))
        deadline = time.monotonic() + 3
        ok = None
        while time.monotonic() < deadline and ok is None:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, str) and json.loads(m).get("ok"):
                ok = json.loads(m)
        check("B1. auth→ok", ok is not None)

        # Голосовые кадры от браузера (RMS выше VAD) — для честного in
        for _ in range(4):
            await ws.send(b"\x64\x10" * 960)     # 1920 байта PCM
        await asyncio.sleep(0.3)

        # B2: ping с id → pong текстом
        await ws.send(json.dumps({"t": "ping", "id": 777}))
        pong = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pong is None:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, str):
                j = json.loads(m)
                if j.get("t") == "pong":
                    pong = j
        check("B2. {t:ping,id:777} → текстовый pong с id эхом",
              pong is not None and pong.get("id") == 777, str(pong))
        check("B2b. pong несёт счётчики in/out/odd",
              pong is not None
              and all(k in pong for k in ("in", "out", "odd")))
        # B3: микшер получил ВСЕ наши голосовые кадры (in >= 4)
        check("B3. pong.in >= 4 (все голосовые кадры дошли до микшера)",
              pong is not None and pong.get("in", 0) >= 4,
              f"in={pong.get('in') if pong else None}")

        # B4: hb-echo не сломан (регресс v3.6.5)
        await ws.send(json.dumps({"t": "hb", "ts": 424242}))
        echo = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and echo is None:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, str):
                j = json.loads(m)
                if j.get("t") == "hb" and j.get("ok"):
                    echo = j
        check("B4. hb-echo жив (регресс v3.6.5)",
              echo is not None and echo.get("ts") == 424242, str(echo))

        # B5: мусор не рвёт мост; ping после мусора всё ещё работает
        await ws.send("это не json {{{")
        await ws.send(json.dumps({"t": "ping", "id": 778}))
        pong2 = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pong2 is None:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, str):
                j = json.loads(m)
                if j.get("t") == "pong" and j.get("id") == 778:
                    pong2 = j
        check("B5. мусор не рвёт мост, ping после мусора работает",
              pong2 is not None and pong2.get("id") == 778)


def part_b_bridge() -> None:
    from voice.mixer import VoiceMixer
    from voice.bridge import VoiceBridge

    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    if not mixer.start():
        check("B. микшер стартовал", False)
        return
    try:
        mixer_port = mixer._sock.getsockname()[1]
        bridge = VoiceBridge(host="127.0.0.1", port=0,
                             mixer_port=mixer_port, access_key="")
        if not bridge.start():
            check("B. мост стартовал (есть websockets?)", False)
            return
        try:
            asyncio.run(_bridge_probe(mixer_port, bridge.bound_port))
        finally:
            bridge.stop()
    finally:
        mixer.stop()


# ═════════════════════ C. Исходники ═════════════════════

def part_c_sources() -> None:
    r = subprocess.run(["node", "--check", str(ROOT / "web_client" / "voice.js")],
                       capture_output=True, text=True)
    check("C1. node --check voice.js", r.returncode == 0, r.stderr[:200])

    js = (ROOT / "web_client" / "voice.js").read_text(encoding="utf-8")
    checks = [
        ("C2a. ping-таймер раз в 2с", '}, 2000);' in js and 't:"ping", id:id' in js),
        ("C2b. обработчик pong → onPong", 'if (j.t === "pong"){ onPong(j);' in js),
        ("C2c. потери ↑↓ в строке статуса", "потери ↑" in js and "↓" in js),
        ("C2d. пороги 80/200мс и 2/8% зеркалят config",
           "QUALITY_RTT_BAD_MS = 200" in js and "QUALITY_LOSS_BAD_PCT = 8" in js),
        ("C2e. EMA 0.7/0.3 как в Qt-движке", js.count("* 0.7 +") >= 3),
        ("C2f. resetQuality при ok и при disconnect",
           js.count("resetQuality();") >= 2),
        ("C2g. счётчики sent/recv на аудио-путях",
           "VC.sentFrames++" in js and "VC.recvFrames += Math.floor(i16.length / 960)" in js),
        ("C2h. odd-предупреждение о старой версии", "твои кадры не принимаются" in js),
        ("C2i. цвет вердикта var(--ok)/--err/--warn",
           "var(--ok)" in js and "var(--err)" in js and "var(--warn)" in js),
    ]
    for name, cond in checks:
        check(name, cond)

    br = (ROOT / "voice" / "bridge.py").read_text(encoding="utf-8")
    check("C3a. мост: ping проксируется микшеру FRVC-control кадром",
          'cmd.get("t") == "ping"' in br
          and "make_frame(\n                                FLAG_CONTROL, msg.encode(\"utf-8\"))" in br)
    check("C3b. мост: hb/heartbeat — локальное эхо (без микшера)",
          'cmd.get("t") in ("hb", "heartbeat")' in br)


def main() -> int:
    part_c_sources()   # быстрые — сначала
    part_a_unit()
    part_a_live()
    part_b_bridge()
    fails = [n for n, c, _ in RESULTS if not c]
    print("\n" + "=" * 60)
    print(f"Итог: {len(RESULTS) - len(fails)} OK, {len(fails)} FAIL")
    if fails:
        print("FAIL:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
