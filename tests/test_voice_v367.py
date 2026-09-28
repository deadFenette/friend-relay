#!/usr/bin/env python3
"""Тесты v3.6.7 — «собеседник не слышит» + «бурундук»: три корня.

Баги подтверждены замерами на живом конвейере (не на глаз):
  1. «БУРУНДУК» (веб): VcCap в voice-worklet.js копил ССЫЛКИ на входные
     буферы AudioWorklet, а копировал позже — по спеке эти буферы валидны
     только внутри process(), Chrome переиспользует память. Замер в
     Chromium: 7 из 8 чанков каждого батча ИДЕНТИЧНЫ (dup_ratio 0.875) —
     собеседник слышал 128 сэмплов, повторённые 8 раз.
     Фикс: копия чанка сразу (new Float32Array(ch)).
  2. «НЕ СЛЫШНО» (Qt): _audio_in_callback шлёт блок PortAudio ОДНИМ
     кадром. blocksize = ХИНТ: ALSA/Pulse/часть драйверов дают 240/480/
     1024 сэмплов. Кадр не 1920 байт микшер отбрасывает (odd) — полная
     тишина. Замер: 200 блоков по 480 сэмплов → 0 голосовых кадров.
     Фикс: аккумулятор захвата, режущий поток строго по 20мс-кадрам.
  3. «НЕ СЛЫШНО» (Qt, тихий микрофон): речь RMS 150-250 ниже абсолютного
     порога VAD 300 — гейт закрыт, все кадры silence. Замер: речь RMS
     150/200/250 → 0 кадров с audible-звуком у слушателя.
     Фикс: AutoLevel (AGC-lite) в MicChain ДО гейта; тихая речь тянется
     к ~3000 RMS, нормальный микрофон не трогается (gain=1).
  + защиты: мост не пропустит PCM не 1920 байт к браузеру; voice.js
    при протухшем deviceId пробует системный микрофон; края ресемплера
    44.1к больше не интерполируются «в ноль».

Запуск: python tests/test_voice_v367.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import voice.client.engine as _engine_mod
import voice.devices as _devices_mod
from voice import codec
from voice.client.capture import MicChain
from voice.client.engine import VoiceClient
from voice.dsp import AutoLevel, HighPass
from voice.mixer import VoiceMixer
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    PCM_FRAME_BYTES,
    make_frame,
)

PASS = FAIL = 0
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web_client"


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def speech_pcm(level_rms: float, n: int = 960,
               seed: int | None = None) -> np.ndarray:
    """Речь-подобный шум заданного RMS в шкале int16.

    seed=None — НОВЫЙ сигнал на каждый вызов: повтор одного и того же
    блока выглядит для HighPass квази-постоянным током и давится им,
    что ломает замеры (в реальном микрофоне каждый кадр свой).
    """
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    x = x / np.sqrt(np.mean(x * x)) * level_rms
    return np.clip(x, -32768, 32767).astype(np.int16)


def speech_bursts(level_rms: float, speech_frames: int,
                  gap_frames: int = 6, n: int = 960) -> list[np.ndarray]:
    """Реалистичная речь: всплески с паузами между словами/фразами.

    Непрерывный шум для SoftGate — нереалистичный сигнал: естественные
    паузы между словами дают полу мгновенно падать вниз и не подниматься
    на уровне речи (на этом держится адаптивный гейт).
    """
    out: list[np.ndarray] = []
    while len(out) < speech_frames:
        for _ in range(min(10, speech_frames - len(out))):
            out.append(speech_pcm(level_rms, n))
        for _ in range(gap_frames):
            out.append(speech_pcm(20, n))  # фон между словами
    return out[:speech_frames]


# ══════════════════════════════════════════════════════════════════
# 1. Статические проверки фиксов веб-клиента
# ══════════════════════════════════════════════════════════════════

def test_static_fixes() -> None:
    print("\n== 1. Фиксы веб-клиента в исходниках ==")
    worklet = (WEB / "voice-worklet.js").read_text(encoding="utf-8")
    check("VcCap копирует чанк сразу (new Float32Array(ch))",
          "this.b.push(new Float32Array(ch))" in worklet)
    check("старый паттерн «this.b.push(ch)» удалён",
          "this.b.push(ch)" not in worklet)

    voice_js = (WEB / "voice.js").read_text(encoding="utf-8")
    check("voice.js: фолбэк на системный микрофон при протухшем deviceId",
          "выбранный микрофон недоступен — включён системный" in voice_js)
    check("voice.js: край ресемплера не интерполируется в ноль (capture)",
          "samples[Math.min(j + 1, samples.length - 1)] * frac" in voice_js)
    check("voice.js: край ресемплера не интерполируется в ноль (playback)",
          "f[Math.min(j + 1, 959)] * frac" in voice_js)

    bridge = (ROOT / "voice" / "bridge.py").read_text(encoding="utf-8")
    check("мост: PCM-кадр не 1920 байт не уходит браузеру",
          "len(payload) != PCM_FRAME_BYTES" in bridge)


# ══════════════════════════════════════════════════════════════════
# 2. AutoLevel (юнит)
# ══════════════════════════════════════════════════════════════════

def test_autolevel_unit() -> None:
    print("\n== 2. AutoLevel: тихий микрофон вверх, нормальный не трогаем ==")
    agc = AutoLevel()
    hp = HighPass()
    # «фоновый шум» RMS 30 — 2с
    for _ in range(100):
        s = speech_pcm(30).astype(np.float32)
        hp.process(s)
        agc.process(s)
    check("в тишине усиление не растёт бесконтрольно (gain ≤ 2)",
          agc.gain <= 2.0, f"gain={agc.gain:.2f}")

    # «тихая речь» RMS 200 — 3с (150 кадров)
    gains = []
    for _ in range(150):
        s = speech_pcm(200).astype(np.float32)
        hp.process(s)
        agc.process(s)
        gains.append(agc.gain)
    check("тихая речь (RMS 200) поднимает усиление (gain > 5)",
          agc.gain > 5.0, f"gain={agc.gain:.2f}")
    out_rms = float(np.sqrt(np.mean(
        (speech_pcm(200).astype(np.float32) * agc.gain) ** 2)))
    check("усиленная речь выше порога VAD 300", out_rms > 1000,
          f"rms≈{out_rms:.0f}")

    # нормальный микрофон: RMS 3000 — gain обязан остаться ~1
    agc2 = AutoLevel()
    for _ in range(60):
        s = speech_pcm(30).astype(np.float32)
        agc2.process(s)
    for _ in range(150):
        s = speech_pcm(3000).astype(np.float32)
        agc2.process(s)
    check("нормальный микрофон (RMS 3000) не усиливается (gain < 1.3)",
          agc2.gain < 1.3, f"gain={agc2.gain:.2f}")

    # громкий: gain не ослабляет
    agc3 = AutoLevel()
    for _ in range(100):
        s = speech_pcm(9000).astype(np.float32)
        agc3.process(s)
    check("громкий микрофон не ослабляется (gain >= 1.0)",
          agc3.gain >= 1.0, f"gain={agc3.gain:.2f}")

    # потолок усиления
    agc4 = AutoLevel()
    for _ in range(400):
        s = speech_pcm(80).astype(np.float32)
        agc4.process(s)
    check("усиление ограничено MAX_GAIN=32", agc4.gain <= 32.0,
          f"gain={agc4.gain:.2f}")


# ══════════════════════════════════════════════════════════════════
# 3. MicChain: тихая речь проходит сквозь цепочку
# ══════════════════════════════════════════════════════════════════

def test_mic_chain() -> None:
    print("\n== 3. MicChain (HP→AGC→гейт): тихая речь выживает ==")
    chain = MicChain()
    # прогрев фоном
    for _ in range(50):
        chain.process(speech_pcm(30).tobytes())
    # тихая речь 6с всплесками с паузами (как реальная)
    audible = 0
    frames = 300
    for blk in speech_bursts(200, frames):
        pcm, gain = chain.process(blk.tobytes())
        if dsp_rms(pcm) > 300 and gain > 0.0:
            audible += 1
    check("MicChain: тихая речь даёт audible-кадры (>100 из 300)",
          audible > 100, f"audible={audible}/{frames}")

    chain2 = MicChain()
    silent_ok = True
    for _ in range(100):
        pcm, gain = chain2.process(speech_pcm(10).tobytes())
        if gain > 0.0 and dsp_rms(pcm) > 300:
            silent_ok = False
    check("MicChain: явная тишина остаётся тишиной", silent_ok)


def dsp_rms(pcm: bytes) -> float:
    arr = np.frombuffer(pcm, dtype=np.int16)
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


# ══════════════════════════════════════════════════════════════════
# 4. Аккумулятор захвата: PortAudio дал нестандартный blocksize
# ══════════════════════════════════════════════════════════════════

class _GrowingSock:
    """Собирает отправленные кадры, как это видел бы микшер."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self.lock = threading.Lock()

    def sendall(self, data: bytes) -> None:
        with self.lock:
            self.chunks.append(data)

    def shutdown(self, how):
        pass

    def close(self):
        pass


def _make_engine_with_fake_sd() -> VoiceClient:
    class _FakeStream:
        def __init__(self, **kw):
            self.kw = kw
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    class _FakeSD:
        InputStream = _FakeStream
        OutputStream = _FakeStream

    _devices_mod.sd = _FakeSD()
    _engine_mod._devices.sd = _FakeSD()
    _engine_mod.HAS_AUDIO = True
    vc = VoiceClient(noise_suppression=True)
    vc.on_error = lambda msg: None
    # без сети: только поля, нужные колбэку захвата
    vc._running = True
    vc._sock = _GrowingSock()
    return vc


def _sent_voice_payloads(vc: VoiceClient) -> list[tuple[int, bytes]]:
    """Кадры, поставленные движком в очередь отправки (sendall-поток
    в этих тестах не запущен — connect() не звался)."""
    out = []
    with vc._send_q_lock:
        buf = b"".join(vc._ctl_q) + b"".join(vc._audio_q)
        vc._ctl_q.clear()
        vc._audio_q.clear()
    i = 0
    while i + FRAME_HEADER_LEN <= len(buf):
        if buf[i:i + 4] != MAGIC:
            break
        flags = buf[i + 4]
        (ln,) = struct.unpack("<H", buf[i + 5:i + 7])
        payload = buf[i + FRAME_HEADER_LEN:i + FRAME_HEADER_LEN + ln]
        if not flags & FLAG_CONTROL and not flags & FLAG_SILENCE:
            out.append((flags, payload))
        i += FRAME_HEADER_LEN + ln
    return out


def test_capture_accumulator() -> None:
    print("\n== 4. Захват: PortAudio отдаёт блоки 240/480/1024 сэмплов ==")
    for block in (240, 480, 1024, 960):
        vc = _make_engine_with_fake_sd()
        try:
            n_frames = 20
            total_samples = block * n_frames
            rng = np.random.default_rng(7)
            wave = (rng.standard_normal(total_samples)
                    .astype(np.float32))
            wave = wave / np.sqrt(np.mean(wave * wave)) * 3000
            wave = np.clip(wave, -32768, 32767).astype(np.int16)
            vc._noise_suppression = False  # чистый путь: только режем
            for k in range(n_frames):
                blk = wave[k * block:(k + 1) * block]
                if len(blk) < block:
                    break
                vc._audio_in_callback(blk.reshape(-1, 1), block, None, None)
            payloads = _sent_voice_payloads(vc)
            sizes_ok = all(len(p) == PCM_FRAME_BYTES for _, p in payloads)
            expected = total_samples * 2 // PCM_FRAME_BYTES
            check(f"blocksize={block}: все кадры ровно 1920 байт",
                  sizes_ok and len(payloads) == expected,
                  f"кадров={len(payloads)}/{expected}")
        finally:
            vc._running = False
            vc._sender_alive = False

    # смешанные блоки в одном потоке (реальность ALSA)
    vc = _make_engine_with_fake_sd()
    try:
        vc._noise_suppression = False
        rng = np.random.default_rng(9)
        wave = (rng.standard_normal(960 * 20).astype(np.float32))
        wave = wave / np.sqrt(np.mean(wave * wave)) * 3000
        wave = np.clip(wave, -32768, 32767).astype(np.int16)
        off = 0
        for block in (480, 240, 1024, 512, 960, 300, 960, 960, 960, 960):
            blk = wave[off:off + block]
            off += block
            vc._audio_in_callback(blk.reshape(-1, 1), block, None, None)
        payloads = _sent_voice_payloads(vc)
        sizes_ok = all(len(p) == PCM_FRAME_BYTES for _, p in payloads)
        total_bytes = 480 + 240 + 1024 + 512 + 960 * 5 + 300
        expected = total_bytes * 2 // PCM_FRAME_BYTES  # 7 полных кадров
        check("смешанные блоки: поток кадров остаётся выровненным",
              sizes_ok and len(payloads) == expected,
              f"кадров={len(payloads)}/{expected}")
    finally:
        vc._running = False
        vc._sender_alive = False


# ══════════════════════════════════════════════════════════════════
# 5. Сквозной: тихий микрофон (АГС) и кривой blocksize — до микшера
# ══════════════════════════════════════════════════════════════════

class _RawListener:
    """Сырой TCP-клиент микшера со счётчиками."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.sendall(b"Ltn|key\n")
        assert self._rx_exact(8) == b"AUTH_OK\n"
        self.sock.settimeout(0.05)
        self.ctl: list[dict] = []
        self.opus_pcm: list[bytes] = []
        self.alive = True
        self.dec = codec.Decoder() if codec.available() else None
        self.t = threading.Thread(target=self._rx, daemon=True)
        self.t.start()

    def request_opus(self) -> None:
        self.sock.sendall(make_frame(
            FLAG_CONTROL, json.dumps({"t": "codec", "codec": "opus"}).encode()))

    def _rx_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _rx(self) -> None:
        while self.alive:
            try:
                hdr = self._rx_exact(FRAME_HEADER_LEN)
            except (socket.timeout, TimeoutError, OSError):
                continue
            if hdr is None or hdr[:4] != MAGIC:
                continue
            flags = hdr[4]
            (ln,) = struct.unpack("<H", hdr[5:7])
            payload = self._rx_exact(ln) if ln else b""
            if flags & FLAG_CONTROL:
                try:
                    self.ctl.append(json.loads(payload.decode()))
                except Exception:
                    pass
                continue
            if flags & FLAG_SILENCE:
                self.opus_pcm.append(b"\x00" * PCM_FRAME_BYTES)
                continue
            if self.dec is not None:
                try:
                    self.opus_pcm.append(self.dec.decode(payload))
                except Exception:
                    pass
            else:
                self.opus_pcm.append(payload)

    def wait_opus_ack(self, timeout: float = 2.0) -> str | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            for c in self.ctl:
                if c.get("t") == "codec":
                    return str(c.get("codec"))
            time.sleep(0.02)
        return None

    def close(self) -> None:
        self.alive = False
        self.sock.close()


def test_e2e_quiet_mic_and_blocksize() -> None:
    print("\n== 5. Сквозной до микшера: тихий микрофон + blocksize 480 ==")
    mixer = VoiceMixer(host="127.0.0.1", port=28447, access_key="key")
    if not mixer.start():
        print("  [SKIP] порт занят")
        return
    try:
        time.sleep(0.2)

        # ── 5a. blocksize 480: кадры ДОЛЖНЫ дойти выровненными ──
        vc = VoiceClient(noise_suppression=False)
        vc.on_error = lambda msg: None
        _engine_mod._devices.sd = _FakeSDForEngine()
        _engine_mod.HAS_AUDIO = True
        ok = vc.connect("127.0.0.1", 28447, "QtTalker", "key")
        check("движок подключился к микшеру", ok)
        listener = _RawListener(28447)
        listener.request_opus()
        time.sleep(0.4)
        n0 = len(listener.opus_pcm)
        rng = np.random.default_rng(5)
        wave = (rng.standard_normal(480 * 200).astype(np.float32))
        wave = wave / np.sqrt(np.mean(wave * wave)) * 3000
        wave = np.clip(wave, -32768, 32767).astype(np.int16)
        for k in range(200):
            vc._audio_in_callback(
                wave[k * 480:(k + 1) * 480].reshape(-1, 1), 480, None, None)
            time.sleep(0.01)
        time.sleep(1.0)
        got = listener.opus_pcm[n0:]
        audible = sum(
            1 for p in got
            if float(np.sqrt(np.mean(
                np.frombuffer(p, np.int16).astype(np.float64) ** 2))) > 500)
        check("blocksize=480: голос ДОШЁЛ до микшера (раньше — 0 кадров)",
              audible > 100, f"audible={audible}/~{len(got)}")
        vc.disconnect()
        listener.close()
        time.sleep(0.3)

        # ── 5b. тихий микрофон: АГС вытягивает речь выше порога ──
        vc = VoiceClient(noise_suppression=True)
        vc.on_error = lambda msg: None
        ok = vc.connect("127.0.0.1", 28447, "QtQuiet", "key")
        check("тихий клиент подключился", ok)
        listener2 = _RawListener(28447)
        listener2.request_opus()
        time.sleep(0.4)
        n0 = len(listener2.opus_pcm)
        # фон 1с + тихая речь 4с всплесками с паузами (RMS 200 — вдвое
        # ниже порога 300), как реальный говорящий
        for _ in range(50):
            vc._audio_in_callback(speech_pcm(30).reshape(-1, 1), 960,
                                  None, None)
            time.sleep(0.004)
        for blk in speech_bursts(200, 200):
            vc._audio_in_callback(blk.reshape(-1, 1), 960, None, None)
            time.sleep(0.004)
        time.sleep(1.0)
        got = listener2.opus_pcm[n0:]
        audible = sum(
            1 for p in got
            if float(np.sqrt(np.mean(
                np.frombuffer(p, np.int16).astype(np.float64) ** 2))) > 500)
        check("тихая речь (RMS 200) СЛЫШНА у микшера (раньше — 0)",
              audible > 40, f"audible={audible}")
        check("АГС отчитывается усиление в get_stats",
              vc.get_stats().get("mic_gain", 0) > 3.0,
              f"gain={vc.get_stats().get('mic_gain')}")
        vc.disconnect()
        listener2.close()
    finally:
        mixer.stop()


class _FakeSDForEngine:
    class _S:
        def __init__(self, **kw):
            pass
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    InputStream = _S
    OutputStream = _S


# ══════════════════════════════════════════════════════════════════
# 6. Мост: фреймы микшера → браузер строго по 1920 байт
# ══════════════════════════════════════════════════════════════════

def test_bridge_size_guard() -> None:
    print("\n== 6. Мост: браузер получает PCM только 1920-байтными кусками ==")
    from voice.bridge import VoiceBridge

    mixer = VoiceMixer(host="127.0.0.1", port=28448, access_key="key")
    if not mixer.start():
        print("  [SKIP] порт занят")
        return
    bridge = VoiceBridge("127.0.0.1", 0, 28448, "key")
    assert bridge.start()
    time.sleep(0.3)

    async def scenario() -> tuple[int, int, int]:
        import websockets
        talker = socket.create_connection(("127.0.0.1", 28448), timeout=5)
        talker.sendall(b"A|key\n")
        assert _drain(talker, 8) == b"AUTH_OK\n"
        # talker НЕ просит opus → PCM-цель, мост тоже PCM
        ws = await websockets.connect(f"ws://127.0.0.1:{bridge.bound_port}")
        await ws.send(json.dumps({"name": "WebG", "access_key": "key"}))
        assert json.loads(await ws.recv()).get("ok")
        pcm = (np.sin(np.arange(960) * 0.1) * 5000).astype(np.int16).tobytes()
        sizes: list[int] = []
        n_pcm = 0
        for _ in range(100):
            talker.sendall(make_frame(0x00, pcm))
            await asyncio.sleep(0.01)
        t0 = time.time()
        while time.time() - t0 < 3.0 and n_pcm < 60:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            if isinstance(msg, bytes):
                sizes.append(len(msg))
                n_pcm += 1
        bad = sum(1 for s in sizes if s != PCM_FRAME_BYTES)
        await ws.close()
        talker.close()
        return n_pcm, bad, len(sizes)

    n_pcm, bad, total = asyncio.run(scenario())
    check("браузер получил голос от TCP-клиента", n_pcm >= 40,
          f"кадров={n_pcm}")
    check("все WS-binary ровно 1920 байт", bad == 0, f"bad={bad}/{total}")
    mixer.stop()
    bridge.stop()


def _drain(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        buf += sock.recv(n - len(buf))
    return buf


# ══════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 66)
    print("ГОЛОС v3.6.7 — регресс «не слышно» + «бурундук»")
    print("=" * 66)
    test_static_fixes()
    test_autolevel_unit()
    test_mic_chain()
    test_capture_accumulator()
    if codec.available():
        test_e2e_quiet_mic_and_blocksize()
        test_bridge_size_guard()
    else:
        print("\n  [SKIP] opus недоступен — сетевые сценарии пропущены")
    print("\n" + "=" * 66)
    print(f"ИТОГ: PASS={PASS}  FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
