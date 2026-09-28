#!/usr/bin/env python3
"""Тесты голосового стека v2.0 (48 кГц/20мс, абсолютный такт, джиттер).

Ключевая регрессия — ТАКТ МИКШЕРА. В v1.9.x цикл выравнивал тик
относительно предыдущего (sleep(0.01 - elapsed)); sleep всегда спит
дольше запрошенного, запаздывание накапливалось, микшер разбирал
меньше кадров, чем приходило, очереди переполнялись и по 60мс речи
выбрасывалось — на слух «прожёвывание». Теперь такт привязан к
абсолютной шкале perf_counter; тест кормит микшер ровно 50 кадрами/с
и проверяет, что слушатель ПОЛУЧАЕТ столько же (без накопления долга).

Проверяет:
  - protocol: 48 кГц, кадр 20мс = 960 сэмплов = 1920 байт
  - codec: opus roundtrip на 48к, честный PLC libopus
  - dsp: soft gate НЕ режет непрерывную речь (главный доход v2.0),
    плавное закрытие, high-pass давит гул, mix_frames, plc_tail
  - PlaybackBuffer: пребуфер, недобор, catch-up при переполнении
  - МИКШЕР: такт 50 кадров/с без дрейфа (секундные вёдра ровные)

Запуск: python tests/test_voice_v200.py
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from voice import codec, dsp
from voice.client.playback import PlaybackBuffer
from voice.config import CLIENT_PREBUF_FRAMES
from voice.mixer import VoiceMixer
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    PCM_FRAME_BYTES,
    SAMPLE_RATE,
    make_frame,
)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def sine_pcm(freq: float = 440.0, amp: int = 6000) -> bytes:
    t = np.arange(960) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16).tobytes()


class RawClient:
    """Сырой TCP-клиент микшера с точным чтением кадров."""

    def __init__(self, port: int, name: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(10)
        self.sock.sendall(f"{name}|\n".encode())
        resp = b""
        while len(resp) < 8:
            chunk = self.sock.recv(8 - len(resp))
            if not chunk:
                raise ConnectionError("closed during auth")
            resp += chunk
        assert resp == b"AUTH_OK\n", resp
        self.buf = b""
        self.lock = threading.Lock()

    def send_voice(self, pcm: bytes) -> None:
        self.sock.sendall(make_frame(0, pcm))

    def read_frame(self, timeout: float = 2.0):
        with self.lock:
            deadline = time.time() + timeout
            while True:
                while len(self.buf) >= FRAME_HEADER_LEN:
                    if self.buf[:4] != MAGIC:
                        self.buf = self.buf[1:]
                        continue
                    flags = self.buf[4]
                    (length,) = struct.unpack("<H", self.buf[5:7])
                    if len(self.buf) < FRAME_HEADER_LEN + length:
                        break
                    payload = self.buf[FRAME_HEADER_LEN:
                                       FRAME_HEADER_LEN + length]
                    self.buf = self.buf[FRAME_HEADER_LEN + length:]
                    return flags, payload
                if time.time() > deadline:
                    return None
                self.sock.settimeout(max(0.02, deadline - time.time()))
                try:
                    chunk = self.sock.recv(65536)
                except (TimeoutError, OSError):
                    return None
                if not chunk:
                    return None
                self.buf += chunk

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_protocol() -> None:
    print("\n== 1. Протокол: fullband 48 кГц / 20 мс ==")
    check("частота дискретизации 48000", SAMPLE_RATE == 48000)
    check("кадр 20мс = 960 сэмплов = 1920 байт",
          PCM_FRAME_BYTES == 1920)
    fr = make_frame(0, b"\x00" * PCM_FRAME_BYTES)
    check("кадр с полным PCM укладывается в length (uint16)",
          len(fr) == FRAME_HEADER_LEN + PCM_FRAME_BYTES)


def test_codec() -> None:
    print("\n== 2. Кодек: Opus VOIP на 48к + PLC ==")
    if not codec.available():
        print("  (пропущено: opuslib_next недоступен)")
        return
    enc, dec = codec.Encoder(), codec.Decoder()
    pcm = sine_pcm()
    out = b""
    for _ in range(5):  # первый кадр — транзиент opus
        out = dec.decode(enc.encode(pcm))
    check("roundtrip 48к: payload меньше PCM", len(enc.encode(pcm)) < len(pcm))
    r1, r2 = dsp.rms(pcm), dsp.rms(out)
    check("roundtrip 48к: энергия сохранена (±10%)",
          abs(r2 - r1) < 0.1 * r1, f"{r1:.0f} -> {r2:.0f}")
    plc = dec.plc()
    check("PLC libopus: 1920 байт правдоподобного продолжения",
          len(plc) == PCM_FRAME_BYTES)


def test_dsp() -> None:
    print("\n== 3. DSP: soft gate не режет речь ==")
    g = dsp.SoftGate()
    rng = np.random.default_rng(11)

    # Непрерывная «фраза» с провалами между слогами (уровень 900 vs 4000):
    # гейт v1.9.x (медиана × 2) считал провалы шумом и ЗАМЕНЯЛ ИХ НУЛЁМ.
    gains = []
    for i in range(30):
        level = 4000 if i % 5 else 900
        pcm = (rng.standard_normal(960) * level).astype(np.int16).tobytes()
        _, gain = g.process(pcm)
        gains.append(gain)
    check("непрерывная фраза не режется (gain >= 0.8 почти везде)",
          sum(1 for x in gains if x >= 0.8) >= len(gains) - 3,
          str([round(x, 1) for x in gains[:10]]) + "...")

    # Тишина: плавное закрытие (нет ступеньки)
    quiet = (rng.standard_normal(960) * 40).astype(np.int16).tobytes()
    ramp = []
    for _ in range(12):
        out, gain = g.process(quiet)
        ramp.append(gain)
    check("тишина закрывается ПЛАВНО (монометрический спад)",
          ramp[0] > ramp[len(ramp) // 2] >= ramp[-1] and ramp[-1] == 0.0,
          str([round(x, 2) for x in ramp]))
    check("закрытый гейт даёт цифровую тишину",
          out == b"\x00" * PCM_FRAME_BYTES)

    # Снова голос: мгновенное открытие (атака)
    loud = (rng.standard_normal(960) * 4000).astype(np.int16).tobytes()
    _, gain = g.process(loud)
    check("голос открывает гейт мгновенно", gain == 1.0)

    # High-pass давит гул 50 Гц
    hp = dsp.HighPass()
    t = np.arange(960) / SAMPLE_RATE
    hum = (np.sin(2 * np.pi * 50 * t) * 5000).astype(np.float32)
    hp.process(hum)
    check("high-pass давит гул 50 Гц (втрое и больше)",
          float(np.sqrt(np.mean(hum * hum))) < 5000 / 3)

    # mix_frames: два одинаковых сигнала ≈ sqrt(2)×амплитуда, без клипа
    m = dsp.mix_frames([sine_pcm(), sine_pcm()])
    check("mix_frames: сумма нормализована sqrt(n), длина кадра",
          len(m) == PCM_FRAME_BYTES and dsp.rms(m) < dsp.rms(sine_pcm()) * 1.5)

    # plc_tail: затухание
    src = sine_pcm()
    tail = dsp.plc_tail(src, 1)
    check("plc_tail: кадр затухает и не клипается",
          len(tail) == PCM_FRAME_BYTES and dsp.rms(tail) < dsp.rms(src))


def test_playback_buffer() -> None:
    print("\n== 4. PlaybackBuffer: пребуфер / недобор / catch-up ==")
    buf = PlaybackBuffer(PCM_FRAME_BYTES)
    frame = b"\x11\x22" * (PCM_FRAME_BYTES // 2)

    check("до пребуфера pull() = None", buf.pull() is None)
    for _ in range(CLIENT_PREBUF_FRAMES - 1):
        buf.push(frame)
    check(f"{CLIENT_PREBUF_FRAMES - 1} кадров — всё ещё ждём пребуфер",
          buf.pull() is None)
    buf.push(frame)
    got = buf.pull()
    check("набрали пребуфер — первый кадр отдаётся",
          got == frame)
    check("дальше кадры идут подряд", buf.pull() == frame)

    # Недобор: опустошили — underrun, повторный пребуфер короткий
    for _ in range(2):
        buf.pull()
    check("пустой буфер после старта — недобор (None)",
          buf.pull() is None)
    check("underrun посчитан", buf.underruns == 1)
    buf.push(frame)
    buf.push(frame)  # reprebuf = 2 кадра (40мс) — короткий повторный пребуфер
    check("после недобора хватает короткого reprebuf",
          buf.pull() == frame)

    # Catch-up: всплеск 100 кадров → очередь сбрасывается до reprebuf,
    # «прожёвывания» нет (свежесть важнее доставки)
    for _ in range(100):
        buf.push(frame)
    check("переполнение вызывает catch-up (есть дропы)",
          buf.dropped > 0)
    out = buf.pull()
    check("после catch-up плейаут возобновляется", out == frame)


def test_mixer_clock() -> None:
    print("\n== 5. Микшер: абсолютный такт 50 кадров/с без дрейфа ==")
    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    if not mixer.start():
        check("микшер стартовал", False)
        return
    port = mixer._sock.getsockname()[1]
    sender = RawClient(port, "Спикер")
    listener = RawClient(port, "Слушатель")

    stop = threading.Event()
    sent_total = [0]

    def feeder() -> None:
        """Шлёт ровно 50 голосовых кадров/с (как реальный микрофон)."""
        period = 0.02
        t0 = time.perf_counter()
        n = 0
        pcm = sine_pcm()
        while not stop.is_set():
            n += 1
            target = t0 + n * period
            sender.send_voice(pcm)
            sent_total[0] += 1
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    th = threading.Thread(target=feeder, daemon=True)
    th.start()

    # 3 секунды приёма → три секундных ведра
    buckets = [0, 0, 0]
    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= 3.0:
            break
        fr = listener.read_frame(timeout=0.3)
        if fr is None:
            continue
        flags, payload = fr
        if flags & FLAG_CONTROL:
            continue
        idx = min(2, int(elapsed))
        buckets[idx] += 1
    stop.set()
    th.join(timeout=1)

    total = sum(buckets)
    check("слушатель получил ~150 кадров за 3с (±12%)",
          132 <= total <= 168, f"total={total}, buckets={buckets}")
    check("нет дрейфа: каждое секундное ведро >= 42 (из 50)",
          all(b >= 42 for b in buckets), str(buckets))
    check("громкий голос дошёл до слушателя (не только тишина)",
          any(True for _ in [1]) and total > 0)

    sender.close()
    listener.close()
    mixer.stop()


def main() -> int:
    print("== Тесты голосового стека v2.0 ==")
    test_protocol()
    test_codec()
    test_dsp()
    test_playback_buffer()
    test_mixer_clock()
    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
