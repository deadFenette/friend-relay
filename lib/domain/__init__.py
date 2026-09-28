"""Доменный слой Friend Relay.

Domain отвечает на вопрос "ЧТО это за сущность" (не "КАК показать" - это
транспорт, и не "КАК сделать" - это application-сервисы):
  - EventStore  - журнал событий чата (текст/файлы/дельты/tombstone/DM),
                  seq-нумерация, персистентность JSONL, индекс файлов.
  - PresenceBoard - эфемерное состояние "кто онлайн / кто печатает".

Здесь НЕ должно быть HTTP (BaseHTTPRequestHandler), Qt и сетевых запросов
к внешним сайтам - только чистая логика сущностей и их хранение."""
from lib.domain.event_store import EventStore
from lib.domain.presence import PresenceBoard

__all__ = ["EventStore", "PresenceBoard"]
