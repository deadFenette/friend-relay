"""LinkPreviewService - сетевой сервис извлечения превью ссылок (og:title,
og:description, og:image). Раньше жил приватными функциями в relay_server.py,
но это чистый Application/Service-слой: "как достать превью" - а не домен
("что такое событие") и не транспорт ("как отдать по HTTP").

SSRF-защита: сервер-хост по запросу друга не должен сходить на
localhost/внутреннюю сеть под видом "превью ссылки" - только http(s) и
только адреса, которые резолвятся в публичные IP."""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from lib.util import TTLCache

_URL_RE = re.compile(r"https?://[^\s<>\"]+\.[^\s<>\"]+")


def find_first_url(text: str) -> str | None:
    """Первый URL в тексте сообщения (для прикрепления превью)."""
    urls = _URL_RE.findall(text)
    return urls[0] if urls else None


def is_safe_preview_url(url: str) -> bool:
    """Не даём серверу-хосту по запросу друга сходить на localhost/внутреннюю
    сеть под видом 'превью ссылки' (SSRF): только http(s) и только адреса,
    которые резолвятся в обычные публичные IP."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


# v1.9.5: кап на чтение страницы и семафор на параллельные превью-потоки.
# Раньше resp.read() читал БЕЗ предела: страница-стрим (или drip-feed по
# байту в секунду) держала поток и раздувала RAM; а RelayServer.add_text
# заводит НОВЫЙ daemon-поток на КАЖДОЕ сообщение с URL — без семафора
# чат-шторм ссылками плодил десятки потоков.
_PREVIEW_MAX_BYTES = 256 * 1024  # заголовки og:* живут в <head>, 256К хватает
_PREVIEW_CONCURRENCY = 2

import threading as _threading

_preview_sem = _threading.BoundedSemaphore(_PREVIEW_CONCURRENCY)

# (v2.0.4) Кеш превью по URL: одну и ту же ссылку в чате друзей постят
# повторно часто (мем, новость, задача) - без кеша КАЖДОЕ сообщение с
# этим URL заново качало страницу (секунды сети + трафик на хосте).
# 10 минут жизни достаточно, чтобы поймать повторы, и мало, чтобы
# заметно устареть. Кешируются только УСПЕШНЫЕ превью - неудача (сайт
# упал, SSRF-фильтр, занятый семафор) не запоминается, следующее
# сообщение честно повторит попытку. Сам кеш - lib/util.TTLCache,
# потокобезопасный: превью качаются в фоновых потоках (см. add_text).
_PREVIEW_CACHE = TTLCache(max_entries=128, ttl=600.0)


def fetch_link_preview(url: str, timeout: int = 5) -> dict | None:
    """Превью ссылки с кешем (v2.0.4): свежий результат - из RAM за
    микросекунды, устаревший/отсутствующий - честно по сети (см.
    _fetch_link_preview_net). None НЕ кешируется - неудавшаяся попытка
    не должна блокировать повторную на следующие 10 минут."""
    cached = _PREVIEW_CACHE.get(url)
    if cached is not None:
        return cached
    preview = _fetch_link_preview_net(url, timeout)
    if preview is not None:
        _PREVIEW_CACHE.put(url, preview)
    return preview


def _fetch_link_preview_net(url: str, timeout: int = 5) -> dict | None:
    """Сетевой путь fetch_link_preview (без кеша) - прежнее поведение
    1:1: SSRF-фильтр, семафор на 2 параллельных скачивания, кап 256К."""
    if not is_safe_preview_url(url):
        return None
    if not _preview_sem.acquire(timeout=0.1):
        # Уже два превью качаются — третье не критично, пропускаем,
        # следующее сообщение с той же ссылкой всё равно повторит попытку.
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            # Только первые _PREVIEW_MAX_BYTES: og-теги в <head>, а стрим
            # «бесконечной страницы» больше этого объёма не съест RAM.
            raw = resp.read(_PREVIEW_MAX_BYTES + 1)
            if len(raw) > _PREVIEW_MAX_BYTES:
                raw = raw[:_PREVIEW_MAX_BYTES]
            content = raw.decode("utf-8", errors="ignore")

        # Извлекаем og:title
        title_match = re.search(
            r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
            content,
            re.IGNORECASE,
        )
        title = title_match.group(1) if title_match else None

        # Если нет og:title, пробуем обычный title
        if not title:
            title_match = re.search(r"<title>([^<]+)</title>", content, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else None

        # Извлекаем og:description
        desc_match = re.search(
            r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
            content,
            re.IGNORECASE,
        )
        description = desc_match.group(1) if desc_match else None

        # Извлекаем og:image
        image_match = re.search(
            r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            content,
            re.IGNORECASE,
        )
        image_url = image_match.group(1) if image_match else None

        if not title and not description and not image_url:
            return None

        return {"url": url, "title": title, "description": description, "image": image_url}
    except (urllib.error.URLError, TimeoutError, OSError, Exception):
        return None
    finally:
        # Семафор освобождается на ЛЮБОМ выходе (return/except) — раньше
        # точки возврата внутри try (status != 200) могли его утекать.
        _preview_sem.release()
