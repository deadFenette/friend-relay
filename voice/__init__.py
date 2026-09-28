"""voice — ГОЛОСОВОЙ СТЕК Friend Relay v2.0 (отдельный архитектурный модуль).

САМОДОСТАТОЧНЫЙ пакет: ноль зависимостей от lib/ и qt_app/ — только
стандартная библиотека + numpy/sounddevice/opuslib_next/websockets.
Всё, что касается голоса (протокол, кодек, DSP, микшер, мост, клиент),
живёт здесь; остальное приложение consumes публичный API ниже.

Публичный API:
    from voice import VoiceClient, VoiceMixer, VoiceBridge
    from voice import SAMPLE_RATE, PCM_FRAME_BYTES, make_frame
    from voice import list_audio_devices, resolve_device, HAS_AUDIO
    from voice import codec, dsp, devices, config, app_version

ПРИМЕЧАНИЕ (v2.0.1): UI-код (qt_app) потребляет ТОЛЬКО корень пакета —
каждое имя из этого списка должно реально реэкспортироваться ниже
(покрыто тестом tests/test_voice_api_v201.py — он ловит пропущенный
реэкспорт, как баг с list_audio_devices в 2.0.0).

Структура:
    protocol.py      — проводной протокол FRVC (магия, флаги, кадры)
    config.py        — ВСЕ тюн-константы (джиттер, битрейт, гейты)
    codec.py         — Opus (VOIP/FEC/PLC) + честный фолбэк на raw PCM
    dsp.py           — RMS, high-pass, soft gate, PLC-хвост, микс
    devices.py       — аудиоустройства (sounddevice), сейм для тестов
    mixer.py         — VoiceMixer: сервер, абсолютный такт, PLC
    bridge.py        — VoiceBridge: WS-мост браузер ↔ микшер
    client/          — VoiceClient: engine + capture + playback
"""
def app_version() -> str:
    """Версия ПРИЛОЖЕНИЯ из version.json (корень релиза).

    v3.6.4: рукопожатие версий голоса — микшер кладёт её в spk_all,
    клиент сравнивает со своей и предупреждает о несовпадении
    (разные версии = «бурундук»/тишина из-за смены аудио-формата).
    "?" — version.json не найден (странная сборка): сравнение тогда
    не проводится.

    ВАЖНО: определена ДО под-импортов ниже — voice/client/engine.py и
    voice/mixer.py импортируют её из частично инициализированного
    пакета voice (обычный порядок дал бы circular import).
    """
    import json
    import pathlib
    try:
        p = pathlib.Path(__file__).resolve().parent.parent / "version.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("version")
                   or "?")
    except Exception:
        return "?"


__version__ = "2.0.1"

from voice import codec, config, devices, dsp, protocol  # noqa: E402
from voice.bridge import VoiceBridge  # noqa: E402
from voice.client import VoiceClient  # noqa: E402
from voice.devices import HAS_AUDIO, list_audio_devices, resolve_device  # noqa: E402
from voice.mixer import VoiceMixer  # noqa: E402
from voice.protocol import (  # noqa: E402
    FLAG_CONTROL,
    FLAG_MUTED,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    PCM_FRAME_BYTES,
    PCM_FRAME_SAMPLES,
    SAMPLE_RATE,
    VAD_THRESHOLD,
    make_frame,
    parse_frame_header,
)


__all__ = [
    "FLAG_CONTROL",
    "FLAG_MUTED",
    "FLAG_SILENCE",
    "FRAME_HEADER_LEN",
    "HAS_AUDIO",
    "MAGIC",
    "MAX_PAYLOAD_BYTES",
    "PCM_FRAME_BYTES",
    "PCM_FRAME_SAMPLES",
    "SAMPLE_RATE",
    "VAD_THRESHOLD",
    "VoiceBridge",
    "VoiceClient",
    "VoiceMixer",
    "__version__",
    "app_version",
    "codec",
    "config",
    "devices",
    "dsp",
    "list_audio_devices",
    "make_frame",
    "parse_frame_header",
    "protocol",
    "resolve_device",
]
