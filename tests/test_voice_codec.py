#!/usr/bin/env python3
"""Тест кодека голоса (v1.9.8): opuslib_next + переговоры кодека.

Проверяет:
  - codec.available(): библиотека грузится (или честный фолбэк на PCM)
  - roundtrip: PCM int16 → opus → PCM с близкой формой волны
  - переговоры: control {"t":"codec","codec":"opus"} → ACK от микшера
  - spk_all-снапшот несёт поле "codec" (возможность микшера)
  - голос opus-клиента декодируется микшером и слышен PCM-клиенту
  - микс для opus-клиента приходит opus-датаграммой и декодируется
  - отказ кодека: запрос неизвестного кодека → ACK "pcm"
  - comfort noise УДАЛЁН: silence-кадры не превращаются в белый шум
  - старый клиент без переговоров получает PCM как раньше (совместимость)

Запуск: python tests/test_voice_codec.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from voice import codec
from voice.client import VoiceClient
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


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def sine_pcm(freq: float = 440.0, amp: int = 8000) -> bytes:
    """960 сэмплов синуса int16 (20мс при 48к) — стабильная энергия."""
    t = np.arange(960) / 48000.0
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16).tobytes()


def pcm_rms(pcm: bytes) -> float:
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(arr ** 2)))


class MixerClient:
    """Сырой TCP-клиент микшера с точным чтением кадров (как VoiceClient)."""

    def __init__(self, port: int, name: str, key: str = ""):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(10)
        self.sock.sendall(f"{name}|{key}\n".encode())
        resp = b""
        while len(resp) < len(b"AUTH_OK\n"):
            chunk = self.sock.recv(8 - len(resp))
            if not chunk:
                raise ConnectionError("closed during auth")
            resp += chunk
        assert resp == b"AUTH_OK\n", resp
        self.buf = b""

    def send_control(self, obj: dict) -> None:
        self.sock.sendall(
            make_frame(FLAG_CONTROL, json.dumps(obj).encode("utf-8")))

    def send_pcm_voice(self, n_frames: int, pcm: bytes | None = None) -> None:
        body = pcm if pcm is not None else sine_pcm()
        self.sock.sendall(make_frame(0, body) * n_frames)

    def read_frame(self, timeout: float = 3.0):
        """Следующий кадр → (flags, payload); None по таймауту."""
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
                payload = self.buf[FRAME_HEADER_LEN:FRAME_HEADER_LEN + length]
                self.buf = self.buf[FRAME_HEADER_LEN + length:]
                return flags, payload
            if time.time() > deadline:
                return None
            self.sock.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.sock.recv(4096)
            except (TimeoutError, OSError):
                return None
            if not chunk:
                return None
            self.buf += chunk

    def read_control(self, timeout: float = 3.0) -> dict | None:
        for _ in range(200):
            fr = self.read_frame(timeout)
            if fr is None:
                return None
            flags, payload = fr
            if flags & FLAG_CONTROL:
                try:
                    return json.loads(payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        return None

    def read_control_of(self, t: str, timeout: float = 3.0) -> dict | None:
        """Первый control-кадр КОНКРЕТНОГО типа: spk_all/roster и прочие
        события, стоящие в очереди перед нужным кадром (например, перед
        ACK переговоров кодека), пропускаются."""
        for _ in range(200):
            msg = self.read_control(timeout)
            if msg is None or msg.get("t") == t:
                return msg
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    print("== Тест кодека голоса: opus + переговоры + фолбэк ==\n")

    # ── 1. Кодек доступен и работает ─────────────────────────────────
    has_opus = codec.available()
    if has_opus:
        print("  opuslib_next: доступен (BSD-3-Clause), libopus живая")
        enc, dec = codec.Encoder(), codec.Decoder()
        pcm = sine_pcm()
        data = enc.encode(pcm)
        back = b""
        for _ in range(4):  # первый кадр — транзиент opus, меряем в устоявшемся
            back = dec.decode(enc.encode(pcm))
        check("1. PCM→opus→PCM: payload меньше сырого PCM",
              0 < len(data) < len(pcm))
        # Декодированная энергия близка к исходной (форма волны сохранена;
        # opus слегка понижает уровень — допускаем ±10% в устоявшемся режиме)
        check("2. PCM→opus→PCM: энергия сохранена (±10%)",
              abs(pcm_rms(back) - pcm_rms(pcm)) < 0.1 * pcm_rms(pcm))
    else:
        print("  opuslib_next: НЕ доступен — проверяем честный фолбэк")

    # ── 2. Микшер + переговоры ───────────────────────────────────────
    port = 18777
    mixer = VoiceMixer(host="127.0.0.1", port=port, access_key="k")
    if not mixer.start():
        print("  [FAIL] микшер не стартовал (порт занят?)")
        return 1
    try:
        expected = "opus" if has_opus else "pcm"
        opus_guy = MixerClient(port, "ОпусГай", key="k")
        snap = opus_guy.read_control_of("spk_all")
        check("3. spk_all-снапшот несёт поле codec микшера",
              snap is not None and snap.get("t") == "spk_all"
              and snap.get("codec") == expected)

        # Запрос opus → ACK (roster/спк-события в очереди пропускаем)
        opus_guy.send_control({"t": "codec", "codec": "opus"})
        ack = opus_guy.read_control_of("codec")
        check("4. переговоры: ACK микшера на запрос opus",
              ack is not None and ack.get("t") == "codec"
              and ack.get("codec") == expected)

        # Отказ: неизвестный кодек → pcm
        opus_guy.send_control({"t": "codec", "codec": "flac"})
        ack2 = opus_guy.read_control_of("codec")
        check("5. отказ: неизвестный кодек → ACK pcm",
              ack2 is not None and ack2.get("t") == "codec"
              and ack2.get("codec") == "pcm")

        if has_opus:
            # Вернём opus обратно для дальнейших проверок
            opus_guy.send_control({"t": "codec", "codec": "opus"})
            opus_guy.read_control_of("codec")

            # ── 3. Голос opus-клиента слышен PCM-клиенту ─────────────
            pcm_guy = MixerClient(port, "ПцмГай", key="k")
            pcm_guy.read_control_of("spk_all")  # снапшот + roster до него
            time.sleep(0.1)  # переговоры ОпусГая уже завершены
            my_enc = codec.Encoder()
            opus_frames = b"".join(
                make_frame(0, my_enc.encode(sine_pcm())) for _ in range(12))
            opus_guy.sock.sendall(opus_frames)
            heard = False
            deadline = time.time() + 5
            while time.time() < deadline and not heard:
                fr = pcm_guy.read_frame(max(0.1, deadline - time.time()))
                if fr is None:
                    break
                flags, payload = fr
                if flags & FLAG_CONTROL or flags & FLAG_SILENCE:
                    continue
                if len(payload) == PCM_FRAME_BYTES and pcm_rms(payload) > 1000:
                    heard = True
            check("6. голос opus-клиента декодирован и слышен PCM-клиенту",
                  heard)

            # ── 4. Микс для opus-клиента приходит opus-датаграммой ───
            pcm_guy.send_pcm_voice(12)
            got_opus_mix = False
            deadline = time.time() + 5
            my_dec = codec.Decoder()
            while time.time() < deadline and not got_opus_mix:
                fr = opus_guy.read_frame(max(0.1, deadline - time.time()))
                if fr is None:
                    break
                flags, payload = fr
                if flags & FLAG_CONTROL or flags & FLAG_SILENCE:
                    continue
                if payload and len(payload) != PCM_FRAME_BYTES:
                    try:
                        decoded = my_dec.decode(payload)
                        if pcm_rms(decoded) > 1000:
                            got_opus_mix = True
                    except Exception:
                        pass
            check("7. микс для opus-клиента: opus-датаграмма декодируется",
                  got_opus_mix)

            # ── 5. Silence-кадры для opus-клиента — нулевой PCM ──────
            # (payload тишины НЕ кодируется opus — клиенты играют нули)
            silence_ok = False
            deadline = time.time() + 3
            while time.time() < deadline:
                fr = opus_guy.read_frame(max(0.1, deadline - time.time()))
                if fr is None:
                    continue
                flags, payload = fr
                if flags & FLAG_SILENCE:
                    silence_ok = payload == b"\x00" * PCM_FRAME_BYTES
                    break
            check("8. silence-кадры приходят нулевым PCM (не кодируются)",
                  silence_ok)
            pcm_guy.close()
        else:
            print("  (пропущено: opus недоступен — фолбэк покрыт "
                  "тестами test_web_voice)")

        # ── 6. Comfort noise удалён из клиента ───────────────────────
        check("9. VoiceClient._comfort_noise больше не существует",
              not hasattr(VoiceClient, "_comfort_noise"))

        vc = VoiceClient()  # без connect() — чистая логика кадров
        # Silence-кадр → в очереди плейаута ЧИСТАЯ тишина
        vc._on_mixer_frame(FLAG_SILENCE, b"\x00" * PCM_FRAME_BYTES)
        vc._on_mixer_frame(FLAG_SILENCE, b"garbage-not-pcm" * 8)
        queued = list(vc._play_queue)
        check("10. silence-кадры играются чистой тишиной (нули)",
              queued and all(q == b"\x00" * PCM_FRAME_BYTES for q in queued))

        if has_opus:
            # Голосовой opus-кадр от микшера декодируется клиентом
            vc._codec = "opus"
            vc._opus_decoder = codec.Decoder()
            vc._opus_encoder = None
            mix_pcm = sine_pcm()
            mix_opus = codec.Encoder().encode(mix_pcm)
            vc._on_mixer_frame(0, mix_opus)
            got = vc._play_queue[-1] if vc._play_queue else b""
            check("11. opus-микс декодируется клиентом в PCM",
                  len(got) == PCM_FRAME_BYTES and pcm_rms(got) > 1000)

        opus_guy.close()
    finally:
        mixer.stop()

    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
