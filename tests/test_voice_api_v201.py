"""voice v2.0.1 — публичный API корня пакета + импорт-цепочка UI.

Регрессия к багу 2.0.0: qt_app/widgets/voice_channel_dialog.py падал на
старте приложения «ImportError: cannot import name 'list_audio_devices'
from 'voice'» — функция переехала в voice/devices.py, но не была
реэкспортирована в корне пакета. Qt-тесты в песочнице не запускались
(нет libEGL/PySide6), отдельного теста на публичный API не было.

Проверяет:
  1. Каждое имя, обещанное докстрингом voice/__init__.py, реально
     импортируется из корня (в т.ч. list_audio_devices/resolve_device/
     HAS_AUDIO — тот самый баг).
  2. __all__ не содержит фантомов (каждое имя существует).
  3. Полная импорт-цепочка qt_app.widgets.voice_channel_dialog и
     qt_app.screens.profile_settings работает БЕЗ PySide6 — PySide6
     подменяется заглушками в sys.modules (как фейковый sounddevice в
     test_qt_uix_v196). Ловит любой пропущенный реэкспорт при будущем
     рефакторинге voice/.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ── 1. Публичный API корня voice (чистая часть, без Qt) ─────────────
print("1. Публичный API пакета voice")

import voice
import voice.devices
from voice import (
    FLAG_CONTROL,
    FLAG_MUTED,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    HAS_AUDIO,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    PCM_FRAME_BYTES,
    PCM_FRAME_SAMPLES,
    SAMPLE_RATE,
    VAD_THRESHOLD,
    VoiceBridge,
    VoiceClient,
    VoiceMixer,
    codec,
    config,
    devices,
    dsp,
    list_audio_devices,
    make_frame,
    parse_frame_header,
    protocol,
    resolve_device,
)

check("from voice import list_audio_devices (баг 2.0.0)",
      callable(list_audio_devices))
check("list_audio_devices — та же функция, что voice.devices",
      list_audio_devices is voice.devices.list_audio_devices)
check("from voice import resolve_device",
      callable(resolve_device)
      and resolve_device is voice.devices.resolve_device)
check("HAS_AUDIO в корне совпадает с voice.devices",
      HAS_AUDIO == voice.devices.HAS_AUDIO)
check("классы VoiceClient/VoiceMixer/VoiceBridge",
      isinstance(VoiceClient, type) and isinstance(VoiceMixer, type)
      and isinstance(VoiceBridge, type))
check("константы протокола FRVC",
      SAMPLE_RATE == 48000 and PCM_FRAME_SAMPLES == 960
      and PCM_FRAME_BYTES == 1920 and FRAME_HEADER_LEN == 7
      and MAGIC == b"FRVC" and MAX_PAYLOAD_BYTES > PCM_FRAME_BYTES)
check("субмодули codec/config/devices/dsp/protocol",
      all(hasattr(m, "__name__") for m in
          (codec, config, devices, dsp, protocol)))

check("__all__ без фантомов",
      all(hasattr(voice, n) for n in voice.__all__),
      extra=str([n for n in voice.__all__ if not hasattr(voice, n)]))
check("__all__ покрывает device-API",
      {"list_audio_devices", "resolve_device", "HAS_AUDIO"}
      <= set(voice.__all__))

# list_audio_devices без sounddevice обязан вернуть пустые списки, а не
# падать (HAS_AUDIO=False в песочнице — проверяем честный фолбэк)
if not voice.devices.HAS_AUDIO:
    empty = list_audio_devices()
    check("list_audio_devices без sounddevice -> {'inputs': [], 'outputs': []}",
          empty == {"inputs": [], "outputs": []})
    check("resolve_device без sounddevice -> None",
          resolve_device("Blue Yeti", "input") is None)

# ── 2. Импорт-цепочка UI без PySide6 (заглушки в sys.modules) ───────
print("2. Импорт-цепочка Qt-виджетов (PySide6 подменён заглушками)")

if "PySide6" in sys.modules:
    check("PySide6 не был предзагружен", False,
          "тест рассчитан на чистый процесс без PySide6")
else:
    # Пермиссивная заглушка: любое имя = объект-«всё-в-одном». Годится и
    # как базовый класс (__mro_entries__, PEP 560), и как вызываемый
    # декоратор/фабрика (Signal(str), QColor(...)), и как источник
    # произвольных атрибутов и перечислений (Qt.AlignCenter | Qt.TextX).
    class _QtAnything:
        def __mro_entries__(self, bases):
            return (_QtAnything,)

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, *args, **kwargs):
            return _QtAnything()

        def __getattr__(self, name):
            return _QtAnything()

        def __or__(self, other):
            return self

        def __ror__(self, other):
            return self

        def __and__(self, other):
            return self

        def __rand__(self, other):
            return self

    class _QtStubModule(types.ModuleType):
        def __getattr__(self, name: str) -> _QtAnything:
            if name.startswith("__"):
                raise AttributeError(name)
            obj = _QtAnything()
            setattr(self, name, obj)  # кэш: каждый раз один и тот же
            return obj

    for sub in ("PySide6", "PySide6.QtCore", "PySide6.QtGui",
                "PySide6.QtWidgets"):
        sys.modules[sub] = _QtStubModule(sub)

    try:
        # Путь ровно из трейсбека пользователя:
        # main_qt -> main_window -> chat_screen -> voice_channel_dialog
        from qt_app.widgets import voice_channel_dialog as _dlg

        check("import qt_app.widgets.voice_channel_dialog",
              hasattr(_dlg, "VoiceChannelDialog"))
        check("диалог использует публичный API voice (не devices напрямую)",
              _dlg.VoiceClient is voice.VoiceClient
              and _dlg.list_audio_devices is voice.list_audio_devices)

        from qt_app.screens import profile_settings as _ps

        check("import qt_app.screens.profile_settings",
              hasattr(_ps, "ProfileSettingsScreen"))
        check("экран настроек тоже берёт list_audio_devices из корня",
              _ps.list_audio_devices is voice.list_audio_devices)
    except ImportError as e:
        check(f"импорт-цепочка UI сломана: {e}", False)
    except Exception as e:  # другие ошибки заглушки — тоже провал
        check(f"импорт-цепочка UI упала не на ImportError: "
              f"{type(e).__name__}: {e}", False)

# ─────────────────────────────────────────────────────────────────────
print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
