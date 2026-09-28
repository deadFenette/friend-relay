from __future__ import annotations

import json
from typing import Any

from lib.constants import DATA_DIR, SETTINGS_FILE

# Только настройки и локальные данные приложения (см. ARCHITECTURE.md).
# JSONL-хелперы журнала событий (load_history/save_dm/...) переехали в
# lib/domain/log_io.py — их единственным потребителем был доменный EventStore,
# и формат журнала является частью домена, а не прикладного storage.

_DEFAULTS: dict[str, Any] = {
    "name": "",
    "is_host": False,
    "host_url": "http://",
    "port": 8420,
    "access_key": "",
    "download_dir": "",
    "auto_connect": False,
    "max_file_size_mb": 2048,
    "sound_on_message": True,
    "sound_volume": 0.5,
    "has_avatar": False,
    "check_updates": True,
    "update_manifest_url": "",
    "last_active_tab": "chat",  # Сохранение последней активной вкладки
    "encryption_enabled": False,  # Шифрование сообщений
    "secret_key": "",  # Секретный ключ для шифрования
    "access_key_salt": "",  # Per-installation соль (генерируется при первом запуске)
    # ── Голос: выбор устройств и громкость (v1.9.6) ──
    # Имена устройств (не индексы PortAudio — индексы плывут между
    # перезагрузками/хотплагом). "" = системное устройство по умолчанию.
    "voice_input_device": "",
    "voice_output_device": "",
    "voice_output_volume": 1.0,  # 0.0–2.0, 1.0 = без изменений
    "voice_noise_suppression": True,  # high-pass + noise gate в VoiceClient
    "voice_browser_dsp": True,  # (web) echoCancellation/noiseSuppression/AGC браузера
}


def load_settings() -> dict[str, Any]:
    # Сначала всегда обеспечиваем per-installation соль — даже для свежей
    # установки, у которой ещё нет settings.json. Иначе новый юзер получит
    # пустую соль, и она останется пустой пока кто-то не сохранит настройки.
    need_persist_salt = not SETTINGS_FILE.exists()

    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in _DEFAULTS})

    # Авто-генерация per-installation соли при первом запуске (или если файл
    # settings.json существует, но salt пустая — миграция со старой фикс-соли).
    if not merged.get("access_key_salt"):
        from lib.crypto import generate_salt
        merged["access_key_salt"] = generate_salt()
        # Сохраняем сразу, чтобы при следующем запуске соль была той же —
        # иначе access_key_hash будет пересчитываться каждый раз.
        need_persist_salt = True

    if need_persist_salt:
        # save_settings фильтрует по _DEFAULTS, так что лишнего не запишет
        save_settings(merged)
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    """Атомарная запись (v1.9.5): обрыв посреди write_text оставлял битый
    settings.json, load_settings откатывался к дефолтам — access_key
    обнулялся и сервер поднимался ОТКРЫТЫМ для всех, соль перегенерировалась
    (шифрование старых сообщений переставало сходиться). Теперь на диске
    всегда либо старый, либо новый валидный файл.

    v1.9.9: файл содержит access_key в открытом виде (это осознанный
    дизайн — хост должен показывать/раздавать ключ друзьям), поэтому
    ставим права 0600 как у tls_key.pem: на Linux/macOS файл читает
    только владелец, на Windows chmod молча игнорируется."""
    import os
    import stat

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    to_save = {k: settings.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # не POSIX или файловый ресурс — не критично
