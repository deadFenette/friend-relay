#!/usr/bin/env python3
"""Тесты v3.5.7 — качество связи (пинг/потери) + анти-щелчки + надёжность.

Что покрываем:
  1. Ping/pong микшера: control {"t":"ping"} → pong со счётчиками кадров
     (in = принял от нас, out = отправил нам) — работает и в полной
     тишине (flush каждый тик).
  2. Счётчики frames_in/frames_out: silence/muted кадры считаются
     (иначе мьют давал бы фальшивые 100% потерь вверх).
  3. РЕГРЕССИЯ keepalive: _last_keepalive существует с момента старта
     микшера — keepalive шлётся даже если НИКТО не говорил с запуска
     (раньше attribute появлялся только после первого голоса, и
     «свежий» канал не слал keepalive вообще → NAT резал соединения →
     ложные «соединение разорвано»).
  4. _on_pong: RTT и проценты потерь вверх/вниз из дельт счётчиков.
  5. Анти-щелчки плейаута: фейд-аут от последнего сыгравшего сэмпла
     при недоборе (раньше — жёсткий прыжок в ноль = «тцк»).
  6. Рамп-гейн громкости: слайдер громкости не даёт ступеньки.
  7. PlaybackBuffer.discontinuous: catch-up помечает рывок волны.
  8. Интеграция: реальный VoiceClient (фейковый sounddevice) + реальный
     микшер — ping уходит, pong приходит, rtt_ms > 0.

Запуск: python tests/test_voice_quality_v357.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import voice.client.engine as _engine_mod
import voice.devices as _vdev
import voice.mixer as _mixer_mod
from voice.client.engine import VoiceClient
from voice.client.playback import PlaybackBuffer
from voice.config import CLIENT_FADE_SAMPLES, VOICE_PING_EVERY_S
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

    def send_control(self, obj: dict) -> None:
        self.sock.sendall(make_frame(
            FLAG_CONTROL, json.dumps(obj).encode("utf-8")))

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

    def read_control(self, timeout: float = 2.0,
                     want: str | None = None):
        """Следующий control-кадр (JSON dict) или None.

        want="pong": пропускаем служебные кадры регистрации
        (spk_all/roster/spk) и ждём именно pong."""
        skip = {"spk_all", "roster", "spk"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            fr = self.read_frame(max(0.05, deadline - time.time()))
            if fr is None:
                return None
            flags, payload = fr
            if flags & FLAG_CONTROL:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if want is not None and msg.get("t") in skip \
                        and msg.get("t") != want:
                    continue
                return msg
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _start_mixer(**kwargs) -> VoiceMixer:
    m = VoiceMixer(host="127.0.0.1", **kwargs)
    assert m.start(), "микшер не поднялся"
    port = m._sock.getsockname()[1]
    return m, port


# ══════════════════════════════════════════════════════════════════

def test_ping_pong_mixer() -> None:
    print("\n== 1. Микшер: ping → pong со счётчиками кадров ==")
    m, port = _start_mixer()
    try:
        cl = RawClient(port, "ТестПинг")
        # 5 голосовых + 2 silence + 1 muted: ВСЕ должны попасть в frames_in
        for _ in range(5):
            cl.send_voice(sine_pcm())
        cl.send_voice(make_frame(FLAG_SILENCE, b"\x00" * PCM_FRAME_BYTES))
        cl.send_voice(make_frame(0x01 | 0x02, b"\x00" * PCM_FRAME_BYTES))
        time.sleep(0.15)  # recv-поток микшера успевает разобрать
        cl.send_control({"t": "ping", "id": 42})
        pong = cl.read_control(3.0, want="pong")
        check("pong пришёл", isinstance(pong, dict) and pong.get("t") == "pong",
              f"{pong!r}")
        if isinstance(pong, dict) and pong.get("t") == "pong":
            check("id эхом вернулся", pong.get("id") == 42)
            check("frames_in == 7 (голос+silence+muted)",
                  pong.get("in") == 7, f"in={pong.get('in')}")
            check("frames_out >= 0 (тишина тоже считается)",
                  isinstance(pong.get("out"), int) and pong["out"] >= 0,
                  f"out={pong.get('out')}")
        # Пинг в ПОЛНОЙ тишине (ни одного отправленного кадра): pong всё
        # равно доходит — flush идёт каждый тик независимо от активности.
        cl.send_control({"t": "ping", "id": 43})
        pong2 = cl.read_control(3.0, want="pong")
        check("pong доходит в полной тишине",
              isinstance(pong2, dict) and pong2.get("t") == "pong"
              and pong2.get("id") == 43, f"{pong2!r}")
        cl.close()
    finally:
        m.stop()


def test_keepalive_from_start() -> None:
    print("\n== 2. РЕГРЕССИЯ: keepalive шлётся с самого старта микшера ==")
    old = _mixer_mod.MIXER_KEEPALIVE_EVERY_S
    _mixer_mod.MIXER_KEEPALIVE_EVERY_S = 0.3
    try:
        m, port = _start_mixer()
        try:
            # РЕГРЕССИЯ v3.5.7: атрибут обязан существовать СРАЗУ после
            # __init__ (раньше появлялся только после первого голоса).
            check("_last_keepalive инициализирован в __init__",
                  hasattr(m, "_last_keepalive"))
            cl = RawClient(port, "Тишина")
            # НИЧЕГО не отправляем. Ждём keepalive (silence-кадр).
            got_silence = False
            deadline = time.time() + 4.0
            while time.time() < deadline and not got_silence:
                fr = cl.read_frame(max(0.1, deadline - time.time()))
                if fr is None:
                    continue
                if fr[0] & FLAG_SILENCE:
                    got_silence = True
            check("keepalive-silence дошёл без единого входящего кадра",
                  got_silence)
            cl.close()
        finally:
            m.stop()
    finally:
        _mixer_mod.MIXER_KEEPALIVE_EVERY_S = old


def test_on_pong_math() -> None:
    print("\n== 3. Клиент: _on_pong — RTT и потери из дельт ==")
    vc = VoiceClient()
    # Регистрируем "отправленный" ping id=7 и считаем ответы.
    vc._ping_outstanding[7] = time.perf_counter() - 0.1  # ~100мс назад
    vc._frames_sent = 100
    vc._frames_recv = 45
    vc._on_pong({"id": 7, "in": 98, "out": 50})
    check("RTT ~100мс (EMA на первом замере = замер)",
          50.0 <= vc._rtt_ms <= 150.0, f"rtt={vc._rtt_ms:.1f}")
    # Первый pong только ставит базу потерь (дельты нулевые)
    check("первый pong не рисует фальшивых потерь",
          vc._uplink_pct == 0.0 and vc._downlink_pct == 0.0,
          f"up={vc._uplink_pct} down={vc._downlink_pct}")

    # Второй замер: послали ещё 50 (sent=150), микшер принял только 2
    # (in=100); получили 49 из 50 отправленных нам (out 50→100, recv 45→94)
    vc._ping_outstanding[8] = time.perf_counter() - 0.05
    vc._frames_sent = 150
    vc._frames_recv = 94
    vc._on_pong({"id": 8, "in": 100, "out": 100})
    check("потери вверх = 96% (послали 50 — дошло 2)",
          abs(vc._uplink_pct - 96.0) < 0.1, f"up={vc._uplink_pct:.1f}")
    check("потери вниз = 2% (послано 50 — принято 49)",
          abs(vc._downlink_pct - 2.0) < 0.1, f"down={vc._downlink_pct:.1f}")

    # Вердикт: rtt ~67мс (EMA 0.7*100+0.3*50), up=96% > 8% → bad
    vc._running = True
    vc._sock = object()  # is_connected() = True (без сети)
    q = vc.get_quality()
    check("вердикт bad при потерях 96%", q["verdict"] == "bad", f"{q}")
    check("get_quality отдаёт rtt/losses",
          q["rtt_ms"] > 0 and q["uplink_pct"] == 96.0
          and q["downlink_pct"] == 2.0, f"{q}")

    # Отключён — вердикт пуст, вкладка скроет строку
    vc._running = False
    vc._sock = None
    q2 = vc.get_quality()
    check("без соединения connected=False и вердикт пуст",
          q2["connected"] is False and q2["verdict"] == "", f"{q2}")

    # Чужой/устаревший pong не трогает метрики
    rtt_before = vc._rtt_ms
    vc._on_pong({"id": 999, "in": 1, "out": 1})
    check("pong с чужим id игнорируется", vc._rtt_ms == rtt_before)


def test_playback_fade_out() -> None:
    print("\n== 4. Анти-щелчки: фейд-аут при недоборе буфера ==")
    vc = VoiceClient()
    vc._playback = PlaybackBuffer(PCM_FRAME_BYTES, prebuf=1, reprebuf=1,
                                  limit=20)
    vc._playback.push(sine_pcm(amp=6000))
    out1 = np.zeros((960, 1), dtype=np.int16)
    vc._audio_out_callback(out1, 960, None, None)
    last_amp = out1[-1, 0]
    check("первый колбэк сыграл кадр (хвост != 0)", last_amp != 0,
          f"last={last_amp}")
    # Буфер пуст → недобор: тишина начинается С фейд-аута от last_amp
    out2 = np.zeros((960, 1), dtype=np.int16)
    vc._audio_out_callback(out2, 960, None, None)
    check("стык без щелчка: первый сэмпл тишины = последний сэмпл звука",
          int(out2[0, 0]) == int(last_amp),
          f"out2[0]={out2[0, 0]} vs {last_amp}")
    check("к концу фейда тишина", int(out2[CLIENT_FADE_SAMPLES - 1, 0]) == 0
          and int(out2[-1, 0]) == 0,
          f"mid={out2[CLIENT_FADE_SAMPLES - 1, 0]}")
    check("внутренний _last_out_amp сброшен", vc._last_out_amp == 0.0)


def test_volume_ramp() -> None:
    print("\n== 5. Анти-щелчки: рамп громкости (слайдер без ступеньки) ==")
    vc = VoiceClient()
    vc._playback = PlaybackBuffer(PCM_FRAME_BYTES, prebuf=1, reprebuf=1,
                                  limit=20)
    vc._playback.push(sine_pcm(amp=6000))
    vc.set_output_volume(0.5)
    out = np.zeros((960, 1), dtype=np.int16)
    vc._audio_out_callback(out, 960, None, None)
    src = np.frombuffer(sine_pcm(amp=6000), dtype=np.int16)
    first_expect = float(src[0]) * 1.0   # рамп начинается с ПРЕДЫДУЩЕЙ громк.
    mid_expect = float(src[479]) * (1.0 - 0.5 * 479 / 959)  # ~0.75
    last_expect = float(src[-1]) * 0.5   # к концу кадра доезжает до целевой
    check("первый сэмпл на прежней громкости (ступеньки нет)",
          abs(int(out[0, 0]) - int(first_expect)) <= 2,
          f"{out[0, 0]} vs {first_expect:.1f}")
    check("середина кадра на промежуточной громкости (~0.75)",
          abs(int(out[479, 0]) - int(mid_expect)) <= 3,
          f"{out[479, 0]} vs {mid_expect:.1f}")
    check("последний сэмпл уже на целевой громкости",
          abs(int(out[-1, 0]) - int(last_expect)) <= 2,
          f"{out[-1, 0]} vs {last_expect:.1f}")
    check("_applied_vol обновлён", vc._applied_vol == 0.5)
    # Возврат на 1.0 — тоже рамп (из 0.5 в 1.0), без скачка
    vc._playback.push(sine_pcm(amp=6000))
    vc.set_output_volume(1.0)
    out2 = np.zeros((960, 1), dtype=np.int16)
    vc._audio_out_callback(out2, 960, None, None)
    first_expect2 = float(src[0]) * 0.5
    check("обратный рамп тоже плавный",
          abs(int(out2[0, 0]) - int(first_expect2)) <= 2,
          f"{out2[0, 0]} vs {first_expect2:.1f}")


def test_playback_discontinuity() -> None:
    print("\n== 6. PlaybackBuffer: catch-up помечает рывок волны ==")
    buf = PlaybackBuffer(PCM_FRAME_BYTES, prebuf=2, reprebuf=1, limit=4)
    for _ in range(6):
        buf.push(sine_pcm())
    check("переполнение выбросило кадры", buf.dropped > 0)
    check("флаг discontinuous поднят", buf.discontinuous is True)
    buf.clear()
    check("clear() гасит флаг", buf.discontinuous is False)


def test_engine_live_ping() -> None:
    print("\n== 7. ИНТЕГРАЦИЯ: реальный клиент + микшер, ping/pong ==")
    # Фейковый sounddevice (как во всех Qt-тестах) — колбэки не зовём,
    # важна только сетевая часть.
    fake_sd = types.ModuleType("sounddevice")

    class _FakeStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake_sd.query_devices = lambda: []  # noqa: PIE807 — фейк, не конструктор
    fake_sd.InputStream = _FakeStream
    fake_sd.OutputStream = _FakeStream
    old_sd, old_has = _vdev.sd, _vdev.HAS_AUDIO
    _vdev.sd = fake_sd
    _vdev.HAS_AUDIO = True
    # ВАЖНО: движок импортирует HAS_AUDIO «by value» при import — патчим
    # и его собственную binding, иначе в песочнице без sounddevice
    # connect() молча отказывает ещё до сети.
    old_eng_has = _engine_mod.HAS_AUDIO
    _engine_mod.HAS_AUDIO = True
    try:
        m, port = _start_mixer()
        vc = VoiceClient()
        try:
            ok = vc.connect("127.0.0.1", port, "КачествоТест", "")
            check("клиент подключился", ok)
            if ok:
                # Первый ping уходит через VOICE_PING_EVERY_S — ждём с запасом
                deadline = time.time() + VOICE_PING_EVERY_S + 2.0
                while time.time() < deadline and vc._last_mix_in is None:
                    time.sleep(0.1)
                q = vc.get_quality()
                check("pong получен (last_mix_in снят)",
                      vc._last_mix_in is not None)
                check("rtt_ms > 0", q["rtt_ms"] > 0, f"{q}")
                check("verdict good/ok (локальный RTT отличен)",
                      q["verdict"] in ("good", "ok"), f"{q}")
        finally:
            vc.disconnect()
            m.stop()
    finally:
        _vdev.sd, _vdev.HAS_AUDIO = old_sd, old_has
        _engine_mod.HAS_AUDIO = old_eng_has


def main() -> int:
    print("=== Тесты v3.5.7: качество связи + анти-щелчки ===")
    test_ping_pong_mixer()
    test_keepalive_from_start()
    test_on_pong_math()
    test_playback_fade_out()
    test_volume_ramp()
    test_playback_discontinuity()
    test_engine_live_ping()
    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
