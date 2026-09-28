"""
ProfileManager — управление профилями пользователей (вынесено из RelayServer).

Профиль — это JSON-файл profiles/{name}.json с полями:
  name, display_name, bio, status, friends, created_at.

Методы: get_profile / save_profile / add_friend / remove_friend / get_friends.
Все потокобезопасны (через лок). RelayServer делегирует сюда вызовы.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

try:
    from lib.util import safe_name as _safe_name
except ImportError:
    import re
    _SAFE_NAME_RE = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9_\-. ]+")

    def _safe_name(name: str) -> str:
        return _SAFE_NAME_RE.sub("", name).strip()[:100]

# Импортируем лимиты — они живут в constants.py
from lib.constants import (
    MAX_BIO_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_FRIENDS_COUNT,
    MAX_STATUS_LENGTH,
)


class ProfileManager:
    """Менеджер профилей: bio, status, display_name, список друзей.

    Один экземпляр на RelayServer. Хранит profiles_dir, не имеет состояния
    между вызовами (только лок для потокобезопасности).
    """

    def __init__(self, profiles_dir: Path):
        self.profiles_dir = profiles_dir
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        # RLock (v1.9.5): save_profile держит лок на весь read-modify-write,
        # а get_profile/_write_profile внутри берут его повторно — обычный
        # Lock дедлочился на первом же /profile/update (тест test_rename_live
        # повесил серверный поток намертво).
        self._lock = threading.RLock()

    def _profile_path(self, name: str) -> Path | None:
        safe = _safe_name(name)
        if not safe:
            return None
        return self.profiles_dir / f"{safe}.json"

    def get_profile(self, name: str) -> dict:
        """Возвращает профиль пользователя. Если файла нет — возвращает
        профиль с дефолтами (пустые bio/status, display_name=name)."""
        path = self._profile_path(name)
        default = {
            "name": name,
            "display_name": name,
            "bio": "",
            "status": "",
            "friends": [],
            "created_at": int(time.time()),
        }
        if path is None or not path.exists():
            return default
        try:
            with self._lock:
                data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
        # Нормализуем поля на случай старых/неполных файлов.
        data.setdefault("name", name)
        data.setdefault("display_name", name)
        data.setdefault("bio", "")
        data.setdefault("status", "")
        data.setdefault("friends", [])
        data.setdefault("created_at", default["created_at"])
        return data

    def save_profile(self, name: str, **fields) -> bool:
        """Обновляет указанные поля профиля (bio, status, display_name).
        Только владелец может менять свой профиль (проверяется на уровне
        эндпоинта, тут только запись)."""
        path = self._profile_path(name)
        if path is None:
            return False
        # v1.9.5: RMW под одним локом — раньше get_profile (лок на чтение)
        # и запись (лок на запись) были разнесены: два одновременных
        # запроса (/friends/add + /profile/update) перезатирали друг друга.
        with self._lock:
            profile = self.get_profile(name)
        if "bio" in fields:
            profile["bio"] = (fields["bio"] or "")[:MAX_BIO_LENGTH]
        if "status" in fields:
            profile["status"] = (fields["status"] or "")[:MAX_STATUS_LENGTH]
        if "display_name" in fields:
            dn = (fields["display_name"] or name).strip()[:MAX_DISPLAY_NAME_LENGTH]
            profile["display_name"] = dn or name
            return self._write_profile(name, profile)

    def add_friend(self, name: str, friend_name: str) -> bool:
        """Добавляет friend_name в список друзей name (одностороннее)."""
        if not name or not friend_name or _safe_name(name) == _safe_name(friend_name):
            return False
        profile = self.get_profile(name)
        friends = profile.get("friends", [])
        fsafe = _safe_name(friend_name)
        if fsafe in friends or len(friends) >= MAX_FRIENDS_COUNT:
            return False
        friends.append(fsafe)
        profile["friends"] = friends
        return self._write_profile(name, profile)

    def remove_friend(self, name: str, friend_name: str) -> bool:
        """Удаляет friend_name из списка друзей name."""
        if not name or not friend_name:
            return False
        profile = self.get_profile(name)
        friends = profile.get("friends", [])
        fsafe = _safe_name(friend_name)
        if fsafe not in friends:
            return False
        friends.remove(fsafe)
        profile["friends"] = friends
        return self._write_profile(name, profile)

    def get_friends(self, name: str) -> list[str]:
        """Возвращает список имён друзей пользователя."""
        return list(self.get_profile(name).get("friends", []))

    def _write_profile(self, name: str, profile: dict) -> bool:
        """Внутренний: атомарно записывает профиль на диск (с локом).

        v1.9.5: tmp + os.replace — раньше прямой write_text при обрыве
        (питание/диск) оставлял битый JSON, и get_profile молча возвращал
        ДЕФОЛТ: пропадали друзья/bio безвозвратно. Атомарная замена
        гарантирует: на диске либо старый, либо новый валидный файл."""
        import os

        path = self._profile_path(name)
        if path is None:
            return False
        try:
            with self._lock:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(profile, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            return True
        except OSError:
            path.with_suffix(".json.tmp").unlink(missing_ok=True)
            return False
