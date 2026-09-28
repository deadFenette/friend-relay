"""Цепочка обработки микрофона (v2.0 / v3.6.7).

MicChain — HighPass → AutoLevel (v3.6.7) → SoftGate. Держится ОДИН
экземпляр на активный клиент: фильтры имеют состояние между кадрами.

Почему отдельный модуль: движку (engine.py) нужна только фабрика и
process(); так проще тестировать DSP без сокетов и sounddevice.

v3.6.7: между high-pass и гейтом встал AutoLevel (AGC-lite). Тихие
микрофоны (речь RMS ниже VAD_THRESHOLD=300) раньше полностью гейтились
в тишину — собеседник не слышал НИЧЕГО. Теперь AGC поднимает уровень
такой речи к ~3000 RMS (нормальные микрофоны не трогает), и гейт/VAD
работают как задумано. Порядок цепочки важен: AGC обязан стоять ДО
гейта (см. docstring voice.dsp.AutoLevel).
"""
from __future__ import annotations

import numpy as np

from voice import dsp
from voice.dsp import _HAS_NUMPY
from voice.protocol import SAMPLE_RATE


class MicChain:
    """HighPass + AutoLevel + SoftGate. process() → (PCM, gain гейта)."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self._hp = dsp.HighPass(sample_rate)
        self._agc = dsp.AutoLevel()
        self._gate = dsp.SoftGate(sample_rate)

    def process(self, pcm: bytes) -> tuple[bytes, float]:
        """Кадр микрофона → фильтрованный кадр + gain гейта.

        HighPass прогоняется здесь (для честного уровня AGC без гула
        50 Гц), затем AGC, затем SoftGate — у него свой HighPass внутри,
        т.е. звук фильтруется дважды (1-й порядок → фактически 2-й): для
        гула 50 Гц это только лучше, на речь 100+ Гц влияния нет.
        """
        if not pcm or not _HAS_NUMPY:
            return pcm, 1.0
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        self._hp.process(samples)
        self._agc.process(samples)          # v3.6.7: тихий микрофон — вверх
        # SoftGate.process читает int16-байты: после AGC (усиление до ×32
        # и возможные пики за пределами шкалы) обязаны вернуть массив в
        # int16 С КЛИПОМ — иначе tobytes() float32 перечитается как int16
        # и даст мусорный «уровень» в тысячи (поймано тестом v3.6.7).
        gated_pcm = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
        return self._gate.process(gated_pcm)

    @property
    def agc_gain(self) -> float:
        """Текущее усиление AGC (для диагностики/UI)."""
        return self._agc.gain

    @staticmethod
    def rms_of(pcm: bytes) -> float:
        """RMS обработанного кадра (для VAD движка)."""
        return dsp.rms(pcm)
