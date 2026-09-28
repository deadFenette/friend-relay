"""Цепочка обработки микрофона (v2.0).

MicChain — это high-pass (80 Гц) + адаптивный мягкий гейт из voice/dsp.
Держится ОДИН экземпляр на активный клиент: фильтры имеют состояние
между кадрами.

Почему отдельный модуль: движку (engine.py) нужна только фабрика и
process(); так проще тестировать DSP без сокетов и sounddevice.
"""
from __future__ import annotations

from voice import dsp
from voice.protocol import SAMPLE_RATE


class MicChain:
    """High-pass + soft gate. process() → (обработанный PCM, gain 0..1)."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self._gate = dsp.SoftGate(sample_rate)

    def process(self, pcm: bytes) -> tuple[bytes, float]:
        """Кадр микрофона → фильтрованный кадр + gain гейта."""
        return self._gate.process(pcm)

    @staticmethod
    def rms_of(pcm: bytes) -> float:
        """RMS обработанного кадра (для VAD движка)."""
        return dsp.rms(pcm)
