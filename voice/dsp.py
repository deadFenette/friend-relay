"""Цифровая обработка сигнала голосового стека (v2.0).

Только чистые функции и маленькие классы состояния — без сети и Qt,
покрываются юнит-тестами напрямую.

  rms()            — уровень сигнала (VAD, метры)
  HighPass         — IIR 1-го порядка 80 Гц: гул, кулер, ветер
  SoftGate         — АДАПТИВНЫЙ гейт с атакой/спадом вместо жёсткого нуля:
                     в v1.9.x кадры ниже порога заменялись тишиной, и
                     тихие окончания слов «обкусывались». Теперь gain
                     плавно едет 1.0 → 0.0 за 200мс (release) и назад
                     за 20мс (attack) — слово дохлопывает целиком
  plc_tail()       — затухающий хвост последнего кадра (PLC для PCM)
  mix_frames()     — сумма int16 кадров с нормализацией sqrt(n) и клипом
"""
from __future__ import annotations

import math
import struct

from voice.config import (
    GATE_ATTACK_FRAMES,
    GATE_RELEASE_FRAMES,
    HIGHPASS_CUTOFF_HZ,
    MIXER_PLC_DECAY,
    NOISE_GATE_RATIO,
)
from voice.protocol import (
    PCM_FRAME_BYTES,
    SAMPLE_RATE,
    VAD_THRESHOLD,
)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover — numpy есть везде, где есть звук
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


def rms(pcm: bytes) -> float:
    """RMS (root mean square) int16-фрейма, шкала 0..32767."""
    if not pcm or len(pcm) < 2:
        return 0.0
    if _HAS_NUMPY:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(arr * arr)))
    # Медленный fallback
    n = len(pcm) // 2
    acc = 0
    for i in range(0, len(pcm) - 1, 2):
        v = struct.unpack_from("<h", pcm, i)[0]
        acc += v * v
    return math.sqrt(acc / n) if n else 0.0


class HighPass:
    """Фильтр верхних частот 1-го порядка (IIR), состояние между кадрами.

    y[n] = alpha * (y[n-1] + x[n] - x[n-1])
    alpha = RC / (RC + dt), RC = 1 / (2·π·cutoff)
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE,
                 cutoff_hz: float = HIGHPASS_CUTOFF_HZ):
        dt = 1.0 / sample_rate
        rc = 1.0 / (2 * math.pi * cutoff_hz)
        self._alpha = rc / (rc + dt)
        self._prev_x = 0.0
        self._prev_y = 0.0

    def process(self, samples) -> None:
        """Применяет фильтр к float32-массиву IN PLACE."""
        if not _HAS_NUMPY or samples.size == 0:
            return
        alpha = self._alpha
        prev_x = self._prev_x
        prev_y = self._prev_y
        # Python-цикл по 960 сэмплам на 20мс-кадр — доля процента CPU,
        # зато ноль зависимостей. (scipy не тянем ради одного IIR.)
        out = samples
        for i in range(out.shape[0]):
            x = out[i]
            y = alpha * (prev_y + x - prev_x)
            out[i] = y
            prev_x = x
            prev_y = y
        self._prev_x = float(prev_x)
        self._prev_y = float(prev_y)


class SoftGate:
    """Адаптивный шумовой гейт с плавной атакой/спадом.

    Шумовой пол оценивается min-tracking трекером (классика шумогава):
        - тишина → пол падает МГНОВЕННО (вниз он честный);
        - голос → пол поднимается МЕДЛЕННО (~1с), поэтому непрерывная
          фраза не поднимает порог на себя.

    Именно это была вторая причина «прожёванной» речи в v1.9.x: порог
    считался как медиана RMS за окно × 2 — во время непрерывной фразы
    медиана = уровень самой речи, и любой провал между слогами
    ОКАЗЫВАЛСЯ НИЖЕ порога и жёстко заменялся нулём.

    Между кадрами хранится ТЕКУЩИЙ gain:
        атака (голос начался):  gain → 1.0 за GATE_ATTACK_FRAMES
        спад  (тишина):         gain → 0.0 за GATE_RELEASE_FRAMES

    Поскольку gain плавный, кадры на границе слов уходят в сеть
    ОСЛАБЛЕННЫМИ, а не нулевыми — окончания слов слышны целиком.
    """

    # Скорость подъёма пола за кадр (доля до текущего уровня):
    # 0.02 на 50 кадрах/с ≈ постоянная времени ~1с.
    _FLOOR_RISE = 0.02

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self._hp = HighPass(sample_rate)
        self._floor = 0.0
        self._gain = 1.0
        self._attack_step = 1.0 / max(1, GATE_ATTACK_FRAMES)
        self._release_step = 1.0 / max(1, GATE_RELEASE_FRAMES)

    def process(self, pcm: bytes) -> tuple[bytes, float]:
        """High-pass + гейт → (обработанный PCM, gain гейта 0..1)."""
        if not pcm or not _HAS_NUMPY:
            return pcm, 1.0
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        self._hp.process(samples)
        level = float(np.sqrt(np.mean(samples * samples))) \
            if samples.size else 0.0

        # ── min-tracking шумовой пол ──
        if level < self._floor:
            self._floor = level
        else:
            self._floor += (level - self._floor) * self._FLOOR_RISE
        threshold = max(self._floor * NOISE_GATE_RATIO, VAD_THRESHOLD)
        want_open = level >= threshold

        # ── плавный gain (атака/спад) ──
        if want_open:
            self._gain = min(1.0, self._gain + self._attack_step)
        else:
            self._gain = max(0.0, self._gain - self._release_step)

        if self._gain >= 1.0:
            out = np.clip(samples, -32768, 32767).astype(np.int16)
            return out.tobytes(), self._gain
        if self._gain <= 0.0:
            return b"\x00" * len(pcm), 0.0
        gated = samples * self._gain
        out = np.clip(gated, -32768, 32767).astype(np.int16)
        return out.tobytes(), self._gain


def plc_tail(last_pcm: bytes, gap: int, decay: float = MIXER_PLC_DECAY) -> bytes:
    """PLC-кадр для PCM: последний голосовой кадр, затухающий на `decay`
    за каждый кадр пропуска. Плавный хвост вместо ступеньки к нулю."""
    gain = decay ** gap
    if gain <= 0.01 or not last_pcm:
        return b"\x00" * PCM_FRAME_BYTES
    if _HAS_NUMPY:
        arr = np.frombuffer(last_pcm, dtype=np.int16)
        return (arr * gain).astype(np.int16).tobytes()
    out = bytearray(len(last_pcm))
    for i in range(0, len(last_pcm) - 1, 2):
        v = int(struct.unpack_from("<h", last_pcm, i)[0] * gain)
        struct.pack_into("<h", out, i, max(-32768, min(32767, v)))
    return bytes(out)


def mix_frames(pcms: list[bytes]) -> bytes:
    """Смешивает несколько int16-фреймов в один.

    Сумма + нормализация на sqrt(n) (говорят двое — громкость сохраняется,
    перегруза нет) + клиппинг. Без numpy — медленный fallback.
    """
    if not pcms:
        return b"\x00" * PCM_FRAME_BYTES
    if len(pcms) == 1:
        return pcms[0]
    if _HAS_NUMPY:
        arrays = [np.frombuffer(p, dtype=np.int16) for p in pcms
                  if len(p) == PCM_FRAME_BYTES]
        if not arrays:
            return b"\x00" * PCM_FRAME_BYTES
        mixed = np.sum(arrays, axis=0, dtype=np.int32)
        n = len(arrays)
        if n > 1:
            mixed = (mixed / (n ** 0.5)).astype(np.int32)
        np.clip(mixed, -32768, 32767, out=mixed)
        return mixed.astype(np.int16).tobytes()
    # Медленный fallback без numpy
    result = bytearray(PCM_FRAME_BYTES)
    n = len(pcms)
    for i in range(0, PCM_FRAME_BYTES, 2):
        s = 0
        for p in pcms:
            if i + 1 < len(p):
                s += struct.unpack_from("<h", p, i)[0]
        if n > 1:
            s = int(s / math.sqrt(n))
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        struct.pack_into("<h", result, i, s)
    return bytes(result)

