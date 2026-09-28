"""Аудиоустройства (sounddevice/PortAudio) — единственный дом для всего,
что связано с перечислением и выбором устройств.

СЕЙМ ДЛЯ ТЕСТОВ: движок (voice/client/engine.py) обращается к `sd`
только через этот модуль — тесты подменяют `voice.devices.sd` фейком и
проверяют весь путь без настоящей звуковой карты.
"""
from __future__ import annotations

HAS_AUDIO = False
try:
    import numpy as np
    import sounddevice as sd

    HAS_AUDIO = True
except ImportError:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]


def list_audio_devices() -> dict:
    """Список устройств записи/воспроизведения для UI.

    Возвращает {"inputs": [{name, index, channels}], "outputs": [...]}.
    Имя — для сохранения в настройки и сопоставления; индекс — только
    для текущей сессии (может меняться после перезагрузки/хотплага).
    Без sounddevice — пустые списки (UI покажет «по умолчанию»)."""
    result: dict = {"inputs": [], "outputs": []}
    if not HAS_AUDIO:
        return result
    try:
        devices = sd.query_devices()
    except Exception:
        return result
    for i, d in enumerate(devices):
        name = (d.get("name") or "").strip()
        if not name:
            continue
        # PortAudio возвращает дубликат вида «HDA Intel (hw:0,0)» или
        # спец-устройства без каналов — пропускаем пустые по направлению.
        if int(d.get("max_input_channels") or 0) > 0:
            result["inputs"].append(
                {"name": name, "index": i,
                 "channels": int(d.get("max_input_channels") or 0)}
            )
        if int(d.get("max_output_channels") or 0) > 0:
            result["outputs"].append(
                {"name": name, "index": i,
                 "channels": int(d.get("max_output_channels") or 0)}
            )
    return result


def resolve_device(preferred_name: str, kind: str) -> int | str | None:
    """Имя устройства из настроек → аргумент device= для sounddevice.

    Ищет ТОЧНОЕ совпадение имени, потом вхождение (PortAudio на Windows
    любит суффиксы вроде « (2- USB Audio Device)»). Не нашлось или имя
    пустое — None = системное устройство по умолчанию (не гадаем)."""
    if not preferred_name or not HAS_AUDIO:
        return None
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for i, d in enumerate(devices):
        if (d.get("name") or "").strip() == preferred_name:
            channels_key = ("max_input_channels" if kind == "input"
                            else "max_output_channels")
            if int(d.get(channels_key) or 0) > 0:
                return i
    for i, d in enumerate(devices):
        name = (d.get("name") or "").strip()
        channels_key = ("max_input_channels" if kind == "input"
                        else "max_output_channels")
        if preferred_name in name and int(d.get(channels_key) or 0) > 0:
            return i
    return None
