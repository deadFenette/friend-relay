"""Транспортный слой Friend Relay ("КАК показать/отдать наружу").

  - http_api.RelayHTTPHandler - HTTP-хендлер: маршруты, разбор запросов,
    коды ответов. Доменной логики нет - только транспорт.

Кто кого может импортировать:
  transport -> application (RelayServer) -> domain (EventStore) + сервисы.
  Обратно - НИКОГДА."""
from lib.server.http_api import RelayHTTPHandler, make_handler_class

__all__ = ["RelayHTTPHandler", "make_handler_class"]
