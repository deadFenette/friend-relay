"""Плейаут-буфер клиента (адаптивный джиттер-буфер) — v2.0/v3.6.4.

Проблема v1.9.x: очередь была collections.deque(maxlen=200мс) — при
всплеске TCP она МОЛЧА выкидывала САМЫЕ СТАРЫЕ кадры по одному, речь
«прожёвывалась», а старт воспроизведения с первого же пришедшего кадра
гарантировал недобор сразу после каждого затишья (дыра → фейд-ин →
дыра).

v2.0:
  - ПРЕБУФЕР: воспроизведение начинается, когда набран запас (80мс);
    после паузы хватает короткого 40мс;
  - CATCH-UP: переполнение (всплеск после затыка сети) сбрасывает
    очередь до reprebuf одним куском — свежесть важнее доставки,
    «прожёвывания» нет, потому что такт клиента (звуковая карта)
    всегда движется вперёд равномерно;
  - НЕДОБОР: pull() вернёт None — движок играет тишину с фейдами и
    ждёт нового пребуфера;
  - v3.5.7: флаг discontinuous — catch-up выбросил кадры => на стыке
    РЫВОК волны (амплитуда прыгает); движок сглаживает его фейдом;
  - статистика dropped/underruns — для UI и тестов.

v3.6.4 — АДАПТИВНАЯ ЦЕЛЬ ПРЕБУФЕРА (ZeroTier/VPN):
  Фиксированные 80мс в LAN работали, но через ZeroTier каждая потеря
  пакета = TCP-ретрансмита = затык потока на 100-300мс. Буфер высыхал,
  плейаут останавливался, речь превращалась в робота. Сигнатура именно
  СЕТЕВОГО затыка: «буфер высох, ЗАТЕМ кадры приехали пачкой больше
  цели» (после паузы кадры приходят по одному, равномерно — всплеска
  нет). На каждый такой затык+всплеск цель пребуфера растёт на 2 кадра
  до потолка CLIENT_PREBUF_MAX_FRAMES (300мс), а при стабильных
  CLIENT_PREBUF_SHRINK_AFTER_S секундах без всплесков — опускается
  обратно по кадру. Латентность растёт только там, где сеть требует.
"""
from __future__ import annotations

import time
from collections import deque

from voice.config import (
    CLIENT_JITTER_LIMIT,
    CLIENT_PREBUF_FRAMES,
    CLIENT_PREBUF_MAX_FRAMES,
    CLIENT_PREBUF_SHRINK_AFTER_S,
    CLIENT_REPREBUF_FRAMES,
)


class PlaybackBuffer:
    """Джиттер-буфер плейаута в целых кадрах PCM."""

    def __init__(self, frame_bytes: int,
                 prebuf: int = CLIENT_PREBUF_FRAMES,
                 reprebuf: int = CLIENT_REPREBUF_FRAMES,
                 limit: int = CLIENT_JITTER_LIMIT,
                 max_prebuf: int = CLIENT_PREBUF_MAX_FRAMES,
                 shrink_after_s: float = CLIENT_PREBUF_SHRINK_AFTER_S):
        self._frame_bytes = frame_bytes
        self._prebuf = max(1, prebuf)
        self._reprebuf = max(1, min(reprebuf, self._prebuf))
        self._limit = max(self._prebuf + 2, limit)
        self._max_prebuf = max(self._prebuf, max_prebuf)
        self._shrink_after_s = max(1.0, float(shrink_after_s))
        self.q: deque[bytes] = deque()
        self.primed = False
        self._first = True    # первый старт требует полный prebuf
        self.dropped = 0      # кадров выброшено catch-up'ом
        self.underruns = 0    # сколько раз плейаут останавливался недобором
        # v3.5.7: True, если только что был catch-up (рывок волны на стыке
        # кадров) — плейаут-колбэк движка читает и гасит флаг, сглаживая
        # стык коротким фейдом. Булево из двух потоков — гонка безвредна.
        self.discontinuous = False
        # ── v3.6.4: адаптивная цель пребуфера ─────────────────────────
        # _target растёт при «затык+всплеск» (сетевой шторм), сжимается
        # в тишине. Сигнатура всплеска — очередь глубже target+3 в момент,
        # когда плейаут ещё НЕ идёт (не primed): равномерный поток даёт
        # ~1 кадр между колбэками (50Гц), всплеск TCP — много сразу.
        self._target = self._prebuf
        self._last_burst_t = 0.0  # perf_counter последнего всплеска/сжатия
        # Плееаут реально ГОЛОДАЛ (pull() нашёл пустую очередь после
        # старта) — единственный честный сигнал затыка сети. Catch-up
        # (сброс переполнения) голодом НЕ считается: он сам сбрасывает
        # до reprebuf ради свежести и в рост цели не играет.
        self._starved = False

    # ── Адаптивная цель ───────────────────────────────────────────

    def target_frames(self) -> int:
        """Текущая цель пребуфера в кадрах (для UI/тестов)."""
        return self._target

    # ── Основной цикл ─────────────────────────────────────────────

    def push(self, pcm: bytes) -> None:
        """Кадр от сети → в буфер (с catch-up при переполнении)."""
        self.q.append(pcm)
        if len(self.q) > self._limit:
            # Всплеск TCP: лишнее спереди выбрасываем до reprebuf и
            # начинаем с короткого пребуфера (свежесть важнее доставки).
            drop = len(self.q) - self._reprebuf
            for _ in range(drop):
                self.q.popleft()
            self.dropped += drop
            self.primed = False
            self.discontinuous = True
        self._adapt()

    def _adapt(self) -> None:
        """Адаптация цели пребуфера. Вызывается из push (поток recv).

        РОСТ: плейаут ГОЛОДАЛ (был недобор: очередь пуста при живом
        плейауте), и теперь очередь УЖЕ глубже цели на 3 кадра.
        Равномерный поток между двумя колбэками плейаута даёт максимум
        1 кадр, поэтому глубина > target+3 при не идущем плейауте
        возможна только если кадры приехали ПАЧКОЙ — это сигнатура
        TCP-ретрансмиты через ZeroTier/VPN: сеть молчала, потом
        отыграла накопленное. Поднимаем цель на 2 кадра (до потолка).
        Catch-up (сброс переполнения) рост не вызывает — он сам режет
        очередь ради свежести и голодом не является.
        СЖАТИЕ: всплесков нет _shrink_after_s секунд и цель выше базы —
        сеть успокоилась, возвращаем по кадру (за 100с до базы).
        """
        now = time.perf_counter()
        if (self._starved and not self.primed and not self._first
                and len(self.q) > self._target + 3):
            if self._target < self._max_prebuf:
                self._target = min(self._max_prebuf, self._target + 2)
            self._last_burst_t = now
        elif (self._target > self._prebuf
                and now - self._last_burst_t > self._shrink_after_s):
            self._target -= 1
            self._last_burst_t = now

    def pull(self) -> bytes | None:
        """Кадр для воспроизведения; None — плейаут ещё не стартовал
        (prebuffer) или недобор (ожидаём re-prebuffer)."""
        if not self.primed:
            need = self._need_frames()
            if len(self.q) < need:
                return None
            self.primed = True
            self._first = False
            self._starved = False  # плейаут снова обеспечен кадрами
        if not self.q:
            # Плейаут уже стартовал, но очередь пуста — недобор.
            self.primed = False
            self._starved = True
            self.underruns += 1
            return None
        return self.q.popleft()

    def _need_frames(self) -> int:
        """Сколько кадров ждать до возобновления плейаута.

        Первый старт — полная цель (4 кадра = 80мс в LAN). Повторный
        (после паузы/недобора) — короткий reprebuf (40мс) ПЛЮС весь
        адаптивный рост цели: если сеть уже показала затыки, возобновлять
        на голом reprebuf значит снова сразу недобор.
        """
        if self._first:
            return self._target
        grown = max(0, self._target - self._prebuf)
        return min(self._target, self._reprebuf + grown)

    def buffered_frames(self) -> int:
        return len(self.q)

    def clear(self) -> None:
        self.q.clear()
        self.primed = False
        self._first = True
        self.discontinuous = False
        # v3.6.4: новое соединение — адаптив с чистого листа.
        self._target = self._prebuf
        self._last_burst_t = 0.0
        self._starved = False
