"""Opus-кодек голосового канала — открытая библиотека (v2.0).

Режим v2.0: APPLICATION_VOIP (оптимизация под речь, а не под музыку),
48 кбит/с fullband, внутриполосный FEC + честный PLC libopus. На
20мс-кадре это даёт чистый разговорный голос и восстановление после
потерь вместо «прожёвывания».

ГРАЦИОЗНЫЙ ФОЛБЭК: библиотека опциональна. Если opuslib_next (или сама
libopus на Windows) не найдена — codec_available() = False и весь стек
работает на raw PCM. Ничего не падает, голос не исчезает.

Windows: libopus не бандлится в wheel — положи libopus-0.dll (x64) рядом
с RUN.bat (в папку приложения) или в PATH. Этот модуль ищет DLL сам
(папка приложения → соседние папки → PATH) и подсовывает её загрузчику;
если не нашёл — PCM.

Использование:
    from voice import codec

    if codec.available():
        enc = codec.Encoder()   # 48кГц, моно, 20мс-кадры
        data = enc.encode(pcm_bytes)        # 1920 байт → ~60-160 байт
        pcm  = codec.Decoder().decode(data) # обратно 1920 байт
        pcm  = codec.Decoder().plc()        # честный PLC libopus

Экземпляры Encoder/Decoder СОСТОЯТЕЛЬНЫ (внутреннее состояние потока) —
один экземпляр на одно направление одного клиента. Не шарить между
потоками без лока.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading

from voice.config import OPUS_BITRATE_BPS, OPUS_FEC_LOSS_PCT
from voice.protocol import CHANNELS, PCM_FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger("friend_relay.voice.codec")

_windows_dll_dirs: list[str] = []


def _candidate_dll_dirs() -> list[str]:
    """Папки, где на Windows имеет смысл искать libopus-0.dll:
    корень приложения (рядом с RUN.bat / main_qt.py), его подпапка lib,
    папка текущего процесса, PATH."""
    dirs: list[str] = []
    try:
        # Корень приложения: voice/codec.py → два уровня вверх.
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(here, ".."))
        for d in (root, os.path.join(root, "lib"),
                  os.path.dirname(os.path.abspath(sys.argv[0])) or root,
                  os.getcwd()):
            d = os.path.abspath(d)
            if d and os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        for p in (os.environ.get("PATH") or "").split(os.pathsep):
            p = os.path.abspath(p)
            if p and os.path.isdir(p) and p not in dirs:
                dirs.append(p)
    except Exception:
        pass
    return dirs


def _preload_windows_dll() -> None:
    """Windows: явно грузим libopus, чтобы opuslib_next её нашёл.

    ctypes.util.find_library("opus") на Windows не всегда видит
    libopus-0.dll (зависит от PATH); надёжнее найти файл самим и
    загрузить через WinDLL + add_dll_directory. Загруженная в процесс
    библиотека закрывает и «обычный» поиск загрузчиком.
    """
    names = ("libopus-0.dll", "opus.dll", "libopus.dll")
    for d in _candidate_dll_dirs():
        for n in names:
            path = os.path.join(d, n)
            if os.path.isfile(path):
                try:
                    os.add_dll_directory(d)
                except (AttributeError, OSError):
                    pass
                try:
                    ctypes.WinDLL(path)
                    log.info("Opus: загружена %s", path)
                    return
                except OSError:
                    continue
    # Не нашли в известных папках — пусть find_library попробует PATH сам
    # (вдруг DLL стоит в системном месте) — больше ничего не делаем.


_lock = threading.Lock()
_initialized = False
_opus = None  # модуль opuslib_next, если доступен


def _init() -> None:
    global _initialized, _opus
    with _lock:
        if _initialized:
            return
        _initialized = True
        if sys.platform == "win32":
            try:
                _preload_windows_dll()
            except Exception:
                pass
        try:
            import opuslib_next as _opus_mod
            # Дым-проверка: конструкторы реально работают (DLL живая).
            _opus_mod.Encoder(SAMPLE_RATE, CHANNELS,
                              _opus_mod.APPLICATION_VOIP)
            _opus = _opus_mod
        except Exception as e:
            _opus = None
            log.info("Opus недоступен (%s) — голос пойдёт как raw PCM. "
                     "Для opus: pip install opuslib_next%s",
                     e, " + libopus-0.dll в папку приложения"
                     if sys.platform == "win32" else "")


def available() -> bool:
    """True если Opus реально работает (библиотека загружена)."""
    _init()
    return _opus is not None


class Encoder:
    """PCM int16 mono 48k → Opus (VOIP, FEC). Один на направление «в сеть»."""

    def __init__(self) -> None:
        if not available():
            raise RuntimeError("Opus недоступен — проверь codec.available()")
        self._enc = _opus.Encoder(SAMPLE_RATE, CHANNELS,
                                  _opus.APPLICATION_VOIP)
        # VOIP-профиль: скорость под речь + устойчивость к потерям.
        # FEC кодирует «скелет» предыдущего кадра внутри следующего —
        # декодер с decode_fec достраивает голос при потерях.
        self._enc.bitrate = OPUS_BITRATE_BPS
        self._enc.inband_fec = True
        self._enc.packet_loss_perc = OPUS_FEC_LOSS_PCT
        try:
            self._enc.signal = _opus.SIGNAL_VOICE
        except (AttributeError, TypeError):
            pass  # старый биндинг без signal — не критично

    def encode(self, pcm: bytes) -> bytes:
        """Кодирует ровно 1920 байт PCM (960 сэмплов = 20мс)."""
        return self._enc.encode(pcm, PCM_FRAME_SAMPLES)


class Decoder:
    """Opus → PCM int16 mono 48k. Один на направление «из сети»."""

    def __init__(self) -> None:
        if not available():
            raise RuntimeError("Opus недоступен — проверь codec.available()")
        self._dec = _opus.Decoder(SAMPLE_RATE, CHANNELS)

    def decode(self, data: bytes) -> bytes:
        """Декодирует opus-кадр → 1920 байт PCM."""
        return self._dec.decode(data, PCM_FRAME_SAMPLES)

    def decode_fec(self, data: bytes) -> bytes:
        """Декодирует кадр с использованием FEC-данных предыдущих кадров.
        Вызывается ПОСЛЕ пропуска кадра: libopus достраивает потерянный
        звук из избыточности следующего."""
        return self._dec.decode(data, PCM_FRAME_SAMPLES, decode_fec=True)

    def plc(self) -> bytes:
        """Packet Loss Concealment libopus: 1920 байт «правдоподобного»
        продолжения речи вместо обрыва волны. Вызывается, когда кадр
        потерян/опоздал (больше ничего не пришло)."""
        # Пустой payload = opus_decode(NULL) внутри libopus → PLC.
        return self._dec.decode(b"", PCM_FRAME_SAMPLES)
