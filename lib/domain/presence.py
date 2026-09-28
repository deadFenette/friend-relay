"""PresenceBoard - эфемерное состояние присутствия: кто онлайн и кто сейчас
печатает. Вынесено из RelayServer (раньше это были 4 метода god-object'а,
смешанные с журналом событий).

Правила домена:
  - "Онлайн" = отмечался на /events (или /ping) позже PRESENCE_TIMEOUT_SECONDS.
  - "Печатает" = прислал /typing позже TYPING_TIMEOUT_SECONDS, в историю не
    пишется (эфемерно, смысла хранить вечно нет).
  - Пустое имя и "?" не отмечаются (это мусор из старых клиентов)."""
from __future__ import annotations

import threading
import time

from lib.constants import PRESENCE_TIMEOUT_SECONDS, TYPING_TIMEOUT_SECONDS


class PresenceBoard:
    def __init__(
        self,
        presence_timeout: int = PRESENCE_TIMEOUT_SECONDS,
        typing_timeout: int = TYPING_TIMEOUT_SECONDS,
    ):
        self._presence_timeout = presence_timeout
        self._typing_timeout = typing_timeout
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}  # имя -> когда последний раз опрашивал /events
        self._typing: dict[str, float] = {}  # имя -> когда последний раз прислал "печатает"

    def touch(self, name: str) -> None:
        if not name or name == "?":
            return
        with self._lock:
            self._last_seen[name] = time.time()

    def online_names(self) -> list[str]:
        cutoff = time.time() - self._presence_timeout
        with self._lock:
            return sorted(n for n, ts in self._last_seen.items() if ts >= cutoff)

    def set_typing(self, name: str) -> None:
        """Отмечает, что name сейчас печатает - эфемерно, не пишется в
        history.jsonl."""
        if not name or name == "?":
            return
        with self._lock:
            self._typing[name] = time.time()

    def typing_names(self, exclude: str = "") -> list[str]:
        cutoff = time.time() - self._typing_timeout
        with self._lock:
            return sorted(n for n, ts in self._typing.items() if ts >= cutoff and n != exclude)

    def details(self) -> dict[str, dict]:
        """(v3.4.0) Снапшот присутствия для GUI сервера:
        {имя: {"last_seen_ago": сек, "typing": bool}} — только живые."""
        now = time.time()
        pres_cut = now - self._presence_timeout
        typ_cut = now - self._typing_timeout
        with self._lock:
            return {
                n: {
                    "last_seen_ago": int(now - ts),
                    "typing": self._typing.get(n, 0.0) >= typ_cut,
                }
                for n, ts in self._last_seen.items()
                if ts >= pres_cut
            }
