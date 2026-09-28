"""Звук уведомления о новом сообщении (v1.9.6).

Приятный короткий «динь» (два тона с фейдом), генерируется на месте —
никаких файлов-ресурсов в бандле. Играет через sounddevice.play()
в отдельном потоке PortAudio: GUI не блокируется даже при серии
сообщений (повторный play перезапускает звук — не накапливается).

Настройки (settings.json, редактируются в Профиле → Уведомления):
  sound_on_message  — вкл/выкл
  sound_volume      — 0.0–1.0 громкость относительно пиковой

Без sounddevice (голос не ставили) — тихий системный QApplication.beep()
как fallback; если и Qt нет под рукой (тесты) — просто ничего.
"""
from __future__ import annotations

import threading

try:
    import numpy as np
    import sounddevice as sd
    _HAS_SD = True
except ImportError:  # голос не устанавливали — уведомления молча выключены
    _HAS_SD = False
    np = None  # type: ignore[assignment]

# Два мягких тона: E6 → A6 (легкая «дверь открылась», не резкий бип)
_TONE_FREQS = (1318.5, 1760.0)
_TONE_DUR = 0.09          # сек на тон
_SAMPLE_RATE = 32000      # уведомлению хватит и 32к — меньше данных
_PLAY_LOCK = threading.Lock()


def _build_ding(volume: float):
    """Собирает int16-массив «динь-динь» с экспоненциальным затуханием."""
    if not _HAS_SD:
        return None
    total = int(_SAMPLE_RATE * _TONE_DUR * len(_TONE_FREQS))
    out = np.zeros(total, dtype=np.float64)
    for i, freq in enumerate(_TONE_FREQS):
        start = int(_SAMPLE_RATE * _TONE_DUR * i)
        end = int(_SAMPLE_RATE * _TONE_DUR * (i + 1))
        n = end - start
        if n <= 0:
            continue
        t = np.arange(n) / _SAMPLE_RATE
        # затухание: мягкая атака 5мс и экспоненциальный хвост
        env = np.minimum(t / 0.005, 1.0) * np.exp(-t * 18.0)
        out[start:end] += np.sin(2 * np.pi * freq * t) * env
    # нормализация до громкости (пик = volume)
    peak = float(np.max(np.abs(out))) or 1.0
    out = out / peak * (0.55 * max(0.0, min(1.0, volume)))
    return (out * 32767).astype(np.int16)


def play_message_sound(volume: float = 0.5) -> bool:
    """Играет уведомление. Возвращает True если звук реально пошёл.

    sd.play() крутит свой поток и неблокирующий; lock защищает от
    одновременного старта двух (серия сообщений) — второй заменяет
    первый, а не склеивается с ним."""
    if not _HAS_SD:
        return _fallback_beep()
    try:
        data = _build_ding(volume)
        if data is None or data.size == 0:
            return _fallback_beep()
        with _PLAY_LOCK:
            sd.play(data, _SAMPLE_RATE)
        return True
    except Exception:
        # Порт-аудио занят голосом/устройство отвалилось — не шумим
        return False


def _fallback_beep() -> bool:
    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            QApplication.beep()
            return True
    except Exception:
        pass
    return False
