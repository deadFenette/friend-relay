from __future__ import annotations

import hashlib
import http.client
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

from lib.constants import (
    CHUNK_RETRIES,
    CHUNK_SIZE,
    DEFAULT_CONNECTIONS,
    HEADER_FILENAME,
    HEADER_FROM,
    HEADER_KEY,
    HEADER_SHA256,
    HEADER_TO,
    PARALLEL_MIN_CHUNK,
    PARALLEL_THRESHOLD_BYTES,
    PROGRESS_UPDATE_EVERY_BYTES,
    REQUEST_TIMEOUT,
)
from lib.crypto import CryptoManager
from lib.util import header_encode

_USER_AGENT = "friend-relay-client/1.0 (personal use)"

# ── keep-alive пул соединений (оптимизация v9.3) ──────────────────────────
# urllib.request.urlopen открывал НОВОЕ TCP-соединение на каждый запрос:
# при поллинге /events раз в секунду это постоянные handshake'и,
# TLS (если https), TIME_WAIT-сокеты и лишняя латентность на каждый тик
# и каждое переключение канала. urllib3 (лицензия MIT) держит пул
# постоянных соединений: handshake один, дальше запросы летят по готовому
# каналу. Если пакет не установлен — прозрачный fallback на прежний
# urllib-путь (приложение продолжает работать, просто без keep-alive).
try:
    import urllib3

    _HTTP_POOL: urllib3.PoolManager | None = urllib3.PoolManager(
        maxsize=8,  # держать до 8 постоянных соединений на хост
        block=False,
        retries=False,  # прежнее поведение: без авторетраев, ошибки наверх
        socket_options=[
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),  # чат не должен ждать Nagle
        ],
    )
except ImportError:  # pragma: no cover - окружение без urllib3
    _HTTP_POOL = None

# ── умная раздача (multi-stream) ───────────────────────────────────────────
# Сервер умеет HTTP Range/206 (см. lib/server/http_api.py), поэтому большие
# файлы качаем несколькими параллельными потоками (как торрент качает куски:
# узкое место одиночного TCP-потока - окно congestion - обходится, реальная
# скорость на драй-каналах растёт в 2-4 раза). Константы - в lib/constants.py:
# PARALLEL_THRESHOLD_BYTES, PARALLEL_MIN_CHUNK, DEFAULT_CONNECTIONS,
# CHUNK_RETRIES.

# Глобальный менеджер криптографии
_crypto_manager = CryptoManager()

# Глобальная клиентская сессия — получаем token при /ping, потом подписываем
# каждый запрос (X-Relay-Auth). Это закрывает дыру с подменой sender:
# никто не может отправить сообщение "от alice" не имея её session_token.
# См. lib/auth.py для деталей протокола.
from lib.auth import ClientSession

_session = ClientSession()


def get_session() -> ClientSession:
    """Возвращает глобальную сессию клиента."""
    return _session


def set_session_token(token: str) -> None:
    """Устанавливает session_token (вызывается после успешного /ping)."""
    _session.set_token(token)


def clear_session() -> None:
    """Сбрасывает сессию (например при отключении)."""
    _session.clear()


def get_crypto_manager() -> CryptoManager:
    """Получает глобальный менеджер криптографии."""
    return _crypto_manager


def set_crypto_secret_key(key: str) -> None:
    """Устанавливает секретный ключ для криптографических операций."""
    _crypto_manager.set_secret_key(key)


def set_crypto_salt(salt: str) -> None:
    """Устанавливает per-installation соль (PBKDF2). Вызывается из
    main_window при смене настроек шифрования — CryptoManager живёт здесь,
    в application-слое, а не во вьюхах."""
    _crypto_manager.set_salt(salt)


class RelayError(Exception):
    pass


def _sign_request_headers(method: str, url: str, headers: dict, body: bytes | None) -> dict:
    """Добавляет X-Relay-Auth к заголовкам, если есть валидная сессия
    (v1.9.5, централизованно — раньше подписывали только send_text,
    profile/update, friends и бот-команды: ~15 POST-маршрутов уходили
    БЕЗ подписи, и ужесточённый сервер их отвергал).

    POST: подпись = HMAC по телу (как lib/auth.py sign_request).
    GET: подпись = HMAC по «path?query» (как auth_header_for_get) —
    сервер проверяет её на приватных маршрутах (/dm/*). Подписываем только
    ASCII-URL: url с не-ASCII параметрами urllib кодирует по-своему, и
    байтовая точность подписи пропала бы (все реальные вызовы уже
    прогоняют параметры через quote()).

    Возвращает НОВЫЙ словарь заголовков (исходный не трогает)."""
    if not _session.is_valid() or "X-Relay-Auth" in headers:
        return {"User-Agent": _USER_AGENT, **headers}
    auth = ""
    if method == "POST" and body is not None:
        auth = _session.auth_header(body)
    elif method == "GET":
        try:
            if url.isascii():
                parts = urlsplit(url)
                target = parts.path + (f"?{parts.query}" if parts.query else "")
                auth = _session.auth_header_for_get(target)
        except Exception:
            auth = ""
    if auth:
        return {"User-Agent": _USER_AGENT, **headers, "X-Relay-Auth": auth}
    return {"User-Agent": _USER_AGENT, **headers}


def _request(
    method: str, url: str, headers: dict, body: bytes | None = None, timeout: int = REQUEST_TIMEOUT
) -> tuple[int, bytes, dict]:
    """Единая точка HTTP-транспорта клиента.

    Через keep-alive пул (urllib3, MIT) когда доступен — это убирает
    TCP-handshake с каждого poll-тика; иначе прежний urllib-путь.
    Контракт прежний: (status, body_bytes, headers_dict).

    v1.9.5: все POST (и DM-GET) автоматически подписываются X-Relay-Auth
    (см. _sign_request_headers). При 403 «неверная сессия» — одна попытка
    авторекавери: re-ping + повтор запроса с новой подписью."""
    req_headers = _sign_request_headers(method, url, headers, body)

    status, data, resp_headers = _send_once(method, url, req_headers, body, timeout)

    # 403 из-за протухшей сессии → re-ping + один повтор
    if status == 403 and _session.is_valid():
        err = _error_text(data, status)
        if "сессия" in err or "сесси" in err:
            retry_headers = _rebuild_signed_headers(method, url, headers, body)
            if retry_headers is not None:
                status, data, resp_headers = _send_once(
                    method, url, retry_headers, body, timeout
                )
    return status, data, resp_headers


def _send_once(
    method: str, url: str, req_headers: dict, body: bytes | None, timeout: int
) -> tuple[int, bytes, dict]:
    if _HTTP_POOL is not None:
        try:
            resp = _HTTP_POOL.request(
                method,
                url,
                body=body,
                headers=req_headers,
                timeout=timeout,
                preload_content=True,
                redirect=True,  # как у urllib по умолчанию
                retries=False,
            )
        except (
            urllib3.exceptions.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as e:
            raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
        return resp.status, resp.data, dict(resp.headers)

    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _rebuild_signed_headers(method: str, url: str, headers: dict, body: bytes | None) -> dict | None:
    """Re-ping + пересборка подписи для повторной попытки (см. _request)."""
    _session.clear()
    name = ""
    raw_from = headers.get(HEADER_FROM, "")
    if raw_from:
        from lib.util import header_decode

        name = header_decode(raw_from)
    if not name:
        return None
    try:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        # ping без ключа: /ping открыт ДО проверки access_key
        ping(base, timeout=5, name=name)
    except RelayError:
        return None
    if not _session.is_valid():
        return None
    return _sign_request_headers(method, url, headers, body)


def _get_json(
    url: str, name: str = "", access_key: str = "", timeout: int = 5
) -> dict:
    """Общий GET-запрос с протокольными заголовками -> разобранный JSON.

    Раньше каждый экран, которому нужно было спросить у сервера
    /voice/info, /voice/participants или /voxel/info, собирал заголовки
    HEADER_FROM/HEADER_KEY и дёргал urllib сам — транспорт протёк во View.
    Теперь все такие запросы идут через эту точку.
    """
    headers: dict = {}
    if name:
        headers[HEADER_FROM] = header_encode(name)
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("GET", url, headers, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"неверный ответ сервера: {e}")


def ping(base_url: str, timeout: int = 5, name: str = "") -> dict:
    """Быстрая проверка 'хост вообще жив и слушает'. Бросает RelayError,
    если не достучались - вызывающая сторона решает, ретраить или нет.

    Если передано name — сервер зарегистрирует новую сессию и вернёт
    session_token. Он сохранится в глобальной сессии и будет использоваться
    для подписи последующих запросов (защита от подмены sender).
    """
    headers = {}
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/ping", headers, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(f"HTTP {status}")
    try:
        result = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")
    # Сохраняем session_token если сервер его прислал
    token = result.get("session_token", "")
    if token:
        set_session_token(token)
    return result


def poll_events(
    base_url: str, since: int, access_key: str = "", name: str = "", tail: int | None = None
) -> dict:
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        # заодно это heartbeat "я на связи" - хост построит по нему список
        # тех, кто сейчас реально держит программу открытой.
        headers[HEADER_FROM] = header_encode(name)
    url = f"{base_url}/events?since={since}"
    if tail is not None:
        url += f"&tail={tail}"
    try:
        status, data, _ = _request("GET", url, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(f"HTTP {status}")
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def poll_events_before(
    base_url: str, before_seq: int, access_key: str = "", name: str = "", count: int = 50
) -> dict:
    """Запрашивает старые сообщения (с seq меньше before_seq) для подгрузки при скролле вверх.

    Возвращает словарь с полями:
    - events: список событий в обратном порядке (от новых к старым)
    - next_since: минимальный seq в батче (для следующего запроса)
    - online: список онлайн пользователей
    - has_more: есть ли ещё старые сообщения
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    url = f"{base_url}/events?before={before_seq}&count={count}"
    try:
        status, data, _ = _request("GET", url, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(f"HTTP {status}")
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def send_text(
    base_url: str, name: str, text: str, access_key: str = "", encrypt: bool = False
) -> int:
    """Отправляет текстовое сообщение с опциональным шифрованием."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    # Шифруем текст если включено шифрование
    message_data = {"text": text}
    if encrypt and _crypto_manager._secret_key:
        encrypted_text, iv = _crypto_manager.encrypt(text)
        if encrypted_text and iv:
            message_data = {"text": encrypted_text, "encrypted": True, "iv": iv}

    body = json.dumps(message_data, ensure_ascii=False).encode("utf-8")
    # Подписываем запрос session_token'ом — сервер проверит что sender это
    # действительно тот, кто зарегистрировал сессию.
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status, data, _ = _request("POST", f"{base_url}/send_text", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def decrypt_message(message: dict) -> dict:
    """Расшифровывает сообщение, если оно зашифровано (единая точка расшифровки
    клиента — вьюхи только зовут её и никогда не разбирают конверты сами).

    Понимает оба формата:
    - структурированный (актуальный): event["encrypted"]=True, шифротекст в
      event["text"], вектор — в event["iv"];
    - legacy JSON-конверт (старые сообщения в истории): весь event["text"] —
      json {"text":"", "encrypted_text":..., "iv":...}, который раньше
      собирался прямо во вьюхах.

    Возвращает копию сообщения с расшифрованным текстом ("encrypted"=False).
    Если это точно шифрованное сообщение, но расшифровать не удалось
    (сменён ключ/соль, нет ключа) — в копии ставится "_decrypt_failed": True,
    текст остаётся как есть: вьюха покажет плейсхолдер вместо сырого JSON.
    """
    text = message.get("text", "")
    if not text:
        return message

    key_ready = bool(_crypto_manager._secret_key)

    def _failed() -> dict:
        result = message.copy()
        result["_decrypt_failed"] = True
        return result

    # ── Актуальный формат: сервер пометил событие флагом encrypted ──
    if message.get("encrypted"):
        iv = message.get("iv", "")
        if not iv or not key_ready:
            return _failed()
        decrypted_text = _crypto_manager.decrypt(text, iv)
        if decrypted_text:
            result = message.copy()
            result["text"] = decrypted_text
            result["encrypted"] = False
            return result
        return _failed()

    # ── Legacy формат: JSON-конверт внутри текста (старая история) ──
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return message
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return message
    if not (isinstance(data, dict) and "encrypted_text" in data and "iv" in data):
        return message

    encrypted_text = data.get("encrypted_text", "")
    iv = data.get("iv", "")
    if not encrypted_text or not iv or not key_ready:
        return _failed()
    decrypted_text = _crypto_manager.decrypt(encrypted_text, iv)
    if decrypted_text:
        result = message.copy()
        result["text"] = decrypted_text
        return result
    return _failed()


def send_typing(base_url: str, name: str, access_key: str = "") -> None:
    """Шлёт эфемерный сигнал "я сейчас печатаю". Fire-and-forget: сетевой
    сбой тут не критичен, поэтому исключение просто гасится вызывающим
    кодом (см. chat_screen._on_input_text_changed)."""
    headers = {HEADER_FROM: header_encode(name)}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("POST", f"{base_url}/typing", headers, b"")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))


def edit_text(base_url: str, name: str, seq: int, new_text: str,
               access_key: str = "", encrypt: bool = False) -> int:
    """Правка своего сообщения. encrypt=True (v1.9.5): новый текст
    шифруется ПЕРЕД отправкой — раньше правка зашифрованного сообщения
    уходила plaintext, сервер хранил её с encrypted=True (флаг не
    снимался), и у всех получателей сообщение навсегда ломалось («не
    удалось расшифровать») + открытый текст лежал на сервере."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    payload_text = new_text
    encrypted_flag = False
    iv = ""
    if encrypt and _crypto_manager._secret_key:
        enc_text, iv = _crypto_manager.encrypt(new_text)
        if enc_text and iv:
            # v2-блоб несёт nonce внутри себя: старое поле iv события не
            # участвует в расшифровке (см. decrypt_message), но непустой
            # iv обязателен — иначе клиент пометит _decrypt_failed.
            payload_text = enc_text
            encrypted_flag = True

    body = json.dumps(
        {"seq": seq, "text": payload_text, "encrypted": encrypted_flag, "iv": iv}
        if encrypted_flag else {"seq": seq, "text": payload_text},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/edit_text", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def delete_event(base_url: str, name: str, seq: int, access_key: str = "") -> int:
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"seq": seq}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/delete_event", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def add_reaction(base_url: str, name: str, seq: int, emoji: str, access_key: str = "") -> int:
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"seq": seq, "emoji": emoji}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/add_reaction", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def pin_message(base_url: str, name: str, seq: int, access_key: str = "") -> tuple[int, bool]:
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"seq": seq}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/pin_message", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    result = json.loads(data.decode("utf-8"))
    return result["seq"], result["pinned"]


def get_pinned_messages(base_url: str, name: str = "", access_key: str = "") -> list[dict]:
    """Все закреплённые сообщения в общем чате (не только из последних 50)."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/pinned", headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["pinned"]


# -- каналы -----------------------------------------------------------


def get_channels(base_url: str, name: str = "", access_key: str = "") -> list[dict]:
    """Получает список всех каналов."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/channels", headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["channels"]


def create_channel(base_url: str, name: str, channel_name: str, access_key: str = "") -> dict:
    """Создаёт новый канал."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"name": channel_name}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/channel/create", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["channel"]


def send_channel_message(
    base_url: str, name: str, channel_name: str, text: str, access_key: str = "",
    encrypt: bool = False,
) -> int:
    """Отправляет сообщение в канал (с опциональным шифрованием)."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    message_data = {"channel": channel_name, "text": text}
    if encrypt and _crypto_manager._secret_key:
        encrypted_text, iv = _crypto_manager.encrypt(text)
        if encrypted_text and iv:
            message_data = {
                "channel": channel_name,
                "text": encrypted_text,
                "encrypted": True,
                "iv": iv,
            }

    body = json.dumps(message_data, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/channel/send", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def get_channel_messages(
    base_url: str, channel_name: str, limit: int = 50, name: str = "", access_key: str = ""
) -> list[dict]:
    """Получает сообщения из канала."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    url = f"{base_url}/channel/messages?channel={quote(channel_name)}&limit={limit}"
    try:
        status, data, _ = _request("GET", url, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["messages"]


def join_channel(base_url: str, name: str, channel_name: str, access_key: str = "") -> dict:
    """Присоединяется к каналу."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"channel": channel_name}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/channel/join", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["result"]


def delete_channel(base_url: str, name: str, channel_name: str, access_key: str = "") -> dict:
    """Удаляет канал. Удалить может только его создатель.
    Поднимает RelayError с понятным сообщением, если пользователь не создатель.
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"channel": channel_name}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/channel/delete", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))


def _sha256_of_file(path: Path) -> str:
    """Hex-дайджест содержимого файла (стриминговое чтение, RAM не тратится).
    Используется перед upload: сервер сверяет X-Relay-SHA256 и хранит хеш
    в событии файла (ETag при скачивании)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _hmac_of_file(path: Path) -> str:
    """HMAC-SHA256(session_token, содержимое файла) hex — подпись для
    стриминговых загрузок (сервер проверяет её инкрементально, кусок за
    куском, см. http_api._post_send_file). Пустая строка, если сессии нет
    (сервер без ключа примет и без подписи)."""
    import hashlib
    import hmac as _hmac

    token = _session.token
    if not token:
        return ""
    mac = _hmac.new(token.encode("utf-8"), b"", hashlib.sha256)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            mac.update(chunk)
    return mac.hexdigest()


def _stream_auth_header(path: Path) -> str:
    """Готовый X-Relay-Auth для файловой загрузки: '<token>:<hex>'."""
    sig = _hmac_of_file(path)
    if not sig:
        return ""
    return f"{_session.token}:{sig}"


def send_file(
    base_url: str, name: str, file_path: Path, access_key: str = "", progress_cb=None
) -> tuple[int, str]:
    """Потоковая отправка - читаем и шлём кусками, не грузя файл целиком в
    память, и дёргаем progress_cb(sent_bytes, total_bytes, elapsed_seconds)
    по ходу дела, чтобы UI мог показать реальный процент и скорость (важно
    для тех, у кого канал не быстрый - без этого человек просто смотрит в
    зависшее окно несколько минут)."""
    size = file_path.stat().st_size
    parts = urlsplit(base_url)
    is_https = parts.scheme == "https"
    conn_cls = http.client.HTTPSConnection if is_https else http.client.HTTPConnection
    timeout = max(REQUEST_TIMEOUT, size // (128 * 1024) + 15)  # с запасом на медленный канал
    conn = conn_cls(parts.hostname, parts.port or (443 if is_https else 80), timeout=timeout)

    headers = {
        "User-Agent": _USER_AGENT,
        HEADER_FROM: header_encode(name),
        HEADER_FILENAME: header_encode(file_path.name),
        HEADER_SHA256: _sha256_of_file(file_path),  # сервер сверит и сохранит
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }
    if access_key:
        # header_encode: ключ с кириллицей иначе не лезет в HTTP-заголовок
        # (latin-1) — GET-запросы с таким ключом молча умирали.
        headers[HEADER_KEY] = header_encode(access_key)
    auth = _stream_auth_header(file_path)
    if auth:
        headers["X-Relay-Auth"] = auth

    try:
        conn.putrequest("POST", "/send_file")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()

        sent = 0
        last_reported = 0
        start = time.monotonic()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                try:
                    conn.send(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # Сервер отверг запрос ДО того, как дочитал тело (неверный
                    # ключ / файл больше лимита) и уже закрыл сокет со своей
                    # стороны, пока мы ещё дописывали файл. Не падаем с сырым
                    # системным исключением - просто прекращаем слать и идём
                    # читать ответ сервера ниже, там будет человеческая причина.
                    break
                sent += len(chunk)
                if progress_cb and (
                    sent - last_reported >= PROGRESS_UPDATE_EVERY_BYTES or sent == size
                ):
                    progress_cb(sent, size, time.monotonic() - start)
                    last_reported = sent

        resp = conn.getresponse()
        status = resp.status
        resp_data = resp.read()
    except (http.client.HTTPException, OSError, TimeoutError) as e:
        raise RelayError(str(e) or type(e).__name__)
    finally:
        conn.close()

    if status != 200:
        raise RelayError(_error_text(resp_data, status))
    parsed = json.loads(resp_data.decode("utf-8"))
    return parsed["seq"], parsed["file_id"]


def send_dm_file(
    base_url: str,
    name: str,
    recipient: str,
    file_path: Path,
    access_key: str = "",
    progress_cb=None,
) -> tuple[int, str]:
    """Приватная отправка файла в ЛС - физически лежит на хосте отдельно от
    общей витрины и скачать его может только отправитель/получатель (см.
    RelayServer.add_dm_file и /dm_download на сервере)."""
    size = file_path.stat().st_size
    parts = urlsplit(base_url)
    is_https = parts.scheme == "https"
    conn_cls = http.client.HTTPSConnection if is_https else http.client.HTTPConnection
    timeout = max(REQUEST_TIMEOUT, size // (128 * 1024) + 15)
    conn = conn_cls(parts.hostname, parts.port or (443 if is_https else 80), timeout=timeout)

    headers = {
        "User-Agent": _USER_AGENT,
        HEADER_FROM: header_encode(name),
        HEADER_TO: header_encode(recipient),
        HEADER_FILENAME: header_encode(file_path.name),
        HEADER_SHA256: _sha256_of_file(file_path),  # сервер сверит и сохранит
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    auth = _stream_auth_header(file_path)
    if auth:
        headers["X-Relay-Auth"] = auth

    try:
        conn.putrequest("POST", "/send_dm_file")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()

        sent = 0
        last_reported = 0
        start = time.monotonic()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                try:
                    conn.send(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                sent += len(chunk)
                if progress_cb and (
                    sent - last_reported >= PROGRESS_UPDATE_EVERY_BYTES or sent == size
                ):
                    progress_cb(sent, size, time.monotonic() - start)
                    last_reported = sent

        resp = conn.getresponse()
        status = resp.status
        resp_data = resp.read()
    except (http.client.HTTPException, OSError, TimeoutError) as e:
        raise RelayError(str(e) or type(e).__name__)
    finally:
        conn.close()

    if status != 200:
        raise RelayError(_error_text(resp_data, status))
    parsed = json.loads(resp_data.decode("utf-8"))
    return parsed["seq"], parsed["file_id"]


def download_dm_file(
    base_url: str, name: str, file_id: str, dest_path: Path, access_key: str = "", progress_cb=None
) -> None:
    """Скачивает приватный DM-файл. name обязателен - сервер проверяет, что
    качает отправитель или получатель, иначе 403."""
    headers = {HEADER_FROM: header_encode(name)}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    _download_resumable(f"{base_url}/dm_download/{file_id}", headers, dest_path, progress_cb)


def download_file(
    base_url: str, file_id: str, dest_path: Path, access_key: str = "", progress_cb=None,
    name: str = "",
) -> None:
    """Скачивает файл из общей витрины.

    progress_cb(received_bytes, total_bytes_or_None, elapsed_seconds) может
    вызываться из РАБОЧЕГО потока (при параллельной докачке) - вызывающая
    сторона обязана сама обеспечить потокобезопасность (сейчас UI не передаёт
    сюда колбэки). После успеха тихо сообщает хосту "у меня есть файл"
    (report_have -> /file/have) - фундамент multi-source раздачи.
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    _download_resumable(f"{base_url}/download/{file_id}", headers, dest_path, progress_cb)
    if name:
        report_have(base_url, name, file_id, access_key)


def _sha256_of_part(path: Path) -> str:
    """Хеш скачанного .part-файла для сверки с ETag сервера."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _etag_to_sha(etag: str | None) -> str:
    """ETag сервера -> hex sha256 (у нас ETag это именно sha256) или ""."""
    if not etag:
        return ""
    return etag.strip().strip('"').lower()


def _verify_downloaded(tmp: Path, expected_sha: str, total: int | None) -> None:
    """Финальная проверка целостности: размер обязателен (если известен),
    sha256 - если сервер его сообщил (ETag). Расхождение = битая передача,
    .part удаляется, наружу идёт RelayError."""
    if total is not None and tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        raise RelayError(f"размер файла не совпал: {tmp.stat().st_size} != {total}")
    if expected_sha and _sha256_of_part(tmp) != expected_sha:
        tmp.unlink(missing_ok=True)
        raise RelayError("хеш скачанного файла не совпал - передача была битой")


def _download_resumable(url: str, headers: dict, dest_path: Path, progress_cb=None) -> None:
    """Общий движок скачивания.

    Стратегия "умной раздачи v2":
      1. Range-проба (bytes=0-0) даёт размер файла и ETag (sha256 ревизии).
      2. Достаточно большой файл качаем DEFAULT_CONNECTIONS параллельными
         кусками через очередь задач: упавший кусок перезапрашивается
         CHUNK_RETRIES раз (раньше один сбой отменял всю загрузку и
         отбрасывал её на одиночный поток с нуля).
      3. Иначе/после неудачи параллельной ветки - одиночный поток.
      4. После сборки сверяем размер и sha256 (ETag) - битая передача не
         дойдёт до пользователя под видом готового файла.
    """
    total, expected_sha = _probe_range_support(url, headers)
    if total is not None and total >= PARALLEL_THRESHOLD_BYTES:
        try:
            _download_parallel(url, headers, dest_path, total, expected_sha, progress_cb)
            return
        except RelayError:
            pass  # откатываемся на одиночный поток - .part пересоздастся
    _download_simple(url, headers, dest_path, progress_cb, expected_sha)


def _probe_range_support(url: str, headers: dict) -> tuple[int | None, str]:
    """Range-проба: (размер файла, sha256 из ETag). Размер None - сервер не
    умеет Range/206 (или файл недоступен). Тело ответа не читаем."""
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **h})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 206:
                return None, ""
            cr = resp.headers.get("Content-Range", "")
            if "/" not in cr:
                return None, ""
            total = cr.rsplit("/", 1)[1].strip()
            return (int(total) if total.isdigit() else None), _etag_to_sha(
                resp.headers.get("ETag")
            )
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError):
        return None, ""


def _download_simple(
    url: str, headers: dict, dest_path: Path, progress_cb=None, expected_sha: str = ""
) -> None:
    """Одиночный поток - историческое поведение (1:1 со старым client.py),
    плюс сверка размера и хеша после скачивания."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RelayError(f"HTTP {resp.status}")
            total = resp.headers.get("Content-Length")
            total = int(total) if total is not None else None
            if not expected_sha:
                expected_sha = _etag_to_sha(resp.headers.get("ETag"))
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            received = 0
            last_reported = 0
            start = time.monotonic()
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if progress_cb and (
                        received - last_reported >= PROGRESS_UPDATE_EVERY_BYTES or received == total
                    ):
                        progress_cb(received, total, time.monotonic() - start)
                        last_reported = received
            _verify_downloaded(tmp, expected_sha, total)
            tmp.replace(dest_path)
    except urllib.error.HTTPError as e:
        raise RelayError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)


def _download_parallel(
    url: str, headers: dict, dest_path: Path, total: int,
    expected_sha: str = "", progress_cb=None,
) -> None:
    """Параллельная докачка кусками по Range (206) с retry на кусок.

    План: файл заранее выделяется (.part с truncate(total)), воркеры забирают
    диапазоны из очереди и пишут СВОЙ диапазон через seek - области не
    пересекаются, блокировки файла не нужны. Упавший кусок (сеть/5xx)
    возвращается в очередь, пока не исчерпает CHUNK_RETRIES; фатальные коды
    (403/404/416) и исчерпание ретраев останавливают всех - наружу RelayError,
    _download_resumable откатится на одиночный поток.
    """
    n = min(DEFAULT_CONNECTIONS, max(1, total // PARALLEL_MIN_CHUNK))
    if n <= 1:
        raise RelayError("файл слишком мал для параллельной докачки")

    # Границы кусков (включительно)
    bounds = []
    chunk = total // n
    start = 0
    for i in range(n):
        end = total - 1 if i == n - 1 else start + chunk - 1
        bounds.append((start, end))
        start = end + 1

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".part")
    with open(tmp, "wb") as f:
        f.truncate(total)  # предвыделяем - потом каждый воркер пишет свой кусок

    tasks: queue.Queue[tuple[int, int, int]] = queue.Queue()
    for seg_id, (first, last) in enumerate(bounds):
        tasks.put((seg_id, first, last))

    received = [0]          # суммарно уникально записанных байт
    written = [0] * len(bounds)  # сколько байт уже засчитано в каждом куске
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[Exception] = []
    started = time.monotonic()

    def _progress(seg_id: int, just_written: int) -> None:
        """Прогресс считает только новый байт: перезапрос куска сначала
        перезаписывает уже засчитанные байты - их в счётчик не добавляем."""
        with lock:
            prev = written[seg_id]
            delta = max(0, just_written - prev)
            written[seg_id] = max(prev, just_written)
            received[0] += delta
            if progress_cb:
                progress_cb(received[0], total, time.monotonic() - started)

    def _fetch_range(first: int, last: int, seg_id: int) -> None:
        h = dict(headers)
        h["Range"] = f"bytes={first}-{last}"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **h})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 206:
                raise RelayError(f"HTTP {resp.status}")
            with open(tmp, "r+b") as f:
                f.seek(first)
                pos = first
                while True:
                    if stop.is_set():
                        raise RelayError("докачка остановлена (другой кусок упал фатально)")
                    data = resp.read(CHUNK_SIZE)
                    if not data:
                        break
                    f.write(data)
                    pos += len(data)
                    _progress(seg_id, pos - first)
                if pos != last + 1:
                    raise RelayError("кусочек недокачан")

    def worker() -> None:
        while not stop.is_set():
            try:
                seg_id, first, last = tasks.get_nowait()
            except queue.Empty:
                return
            # CHUNK_RETRIES перезапросов куска; фатальные ошибки - сразу
            for attempt in range(CHUNK_RETRIES + 1):
                if stop.is_set():
                    return
                try:
                    _fetch_range(first, last, seg_id)
                    break  # кусок готов
                except urllib.error.HTTPError as e:
                    if e.code in (403, 404, 416):
                        errors.append(RelayError(f"HTTP {e.code}"))
                        stop.set()
                        return
                    if attempt == CHUNK_RETRIES:
                        errors.append(RelayError(f"кусок {first}-{last}: HTTP {e.code}"))
                except Exception as e:  # сеть/таймаут/недокачан
                    if attempt == CHUNK_RETRIES:
                        errors.append(RelayError(f"кусок {first}-{last}: {e}"))
                # бэкофф перед повтором куска
                time.sleep(0.4 * (attempt + 1))
            else:
                stop.set()
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RelayError(str(errors[0]))
    _verify_downloaded(tmp, expected_sha, total)
    tmp.replace(dest_path)


def report_have(base_url: str, name: str, file_id: str, access_key: str = "") -> None:
    """Сообщает хосту "у меня есть этот файл, могу раздавать" (multi-source v1).
    Тихая операция: любая ошибка глушится - реестр владельцев это подсказка,
    а не критичный путь."""
    try:
        _post_json(
            f"{base_url}/file/have",
            {"file_id": file_id},
            name=name,
            access_key=access_key,
        )
    except Exception:
        pass


def fetch_file_bytes(
    base_url: str, file_id: str, access_key: str = "", max_bytes: int | None = None
) -> bytes:
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    req = urllib.request.Request(
        f"{base_url}/download/{file_id}", headers={"User-Agent": _USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RelayError(f"HTTP {resp.status}")
            cl = resp.headers.get("Content-Length")
            if max_bytes and cl is not None and int(cl) > max_bytes:
                raise RelayError("файл слишком большой для превью")
            chunks = []
            received = 0
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if max_bytes and received > max_bytes:
                    raise RelayError("файл слишком большой для превью")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise RelayError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)


def upload_avatar(base_url: str, name: str, png_bytes: bytes, access_key: str = "") -> None:
    headers = {
        HEADER_FROM: header_encode(name),
        "Content-Type": "image/png",
        "Content-Length": str(len(png_bytes)),
    }
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    # Центральная подпись в _request посчитает HMAC по png_bytes
    try:
        status, data, _ = _request("POST", f"{base_url}/avatar", headers, png_bytes)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))


def fetch_avatar(base_url: str, name: str, access_key: str = "") -> bytes | None:
    """Возвращает PNG-байты аватарки человека, или None если у него её нет
    (или сеть моргнула) - вызывающая сторона просто остаётся на цветном
    кружке с буквой в этом случае, ничего страшного."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("GET", f"{base_url}/avatar/{quote(name)}", headers, timeout=8)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if status != 200:
        return None
    return data


# ─── Профили пользователей ──────────────────────────────────────────────

def get_profile(base_url: str, name: str, access_key: str = "") -> dict | None:
    """Возвращает публичный профиль пользователя: {name, display_name, bio,
    status, friends, online, created_at}. None если сеть моргнула."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("GET", f"{base_url}/profile/{quote(name)}", headers, timeout=8)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if status != 200:
        return None
    try:
        return json.loads(data.decode("utf-8")).get("profile")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def update_profile(base_url: str, name: str, *, bio: str | None = None,
                   status: str | None = None, display_name: str | None = None,
                   access_key: str = "") -> dict | None:
    """Обновляет bio/status/display_name в своём профиле. Возвращает обновлённый
    профиль или None при ошибке."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body_dict = {}
    if bio is not None:
        body_dict["bio"] = bio
    if status is not None:
        body_dict["status"] = status
    if display_name is not None:
        body_dict["display_name"] = display_name
    body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status_code, data, _ = _request("POST", f"{base_url}/profile/update", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status_code != 200:
        raise RelayError(_error_text(data, status_code))
    try:
        return json.loads(data.decode("utf-8")).get("profile")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_friends(base_url: str, name: str, access_key: str = "") -> list[dict]:
    """Возвращает список друзей с их онлайн-статусом:
    [{name, display_name, status, online}, ...]."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("GET", f"{base_url}/friends?name={quote(name)}", headers, timeout=8)
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    if status != 200:
        return []
    try:
        return json.loads(data.decode("utf-8")).get("friends", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def add_friend(base_url: str, name: str, friend_name: str, access_key: str = "") -> list[str] | None:
    """Добавляет пользователя в друзья. Возвращает обновлённый список имён друзей
    или None при ошибке."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"name": friend_name}, ensure_ascii=False).encode("utf-8")
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status, data, _ = _request("POST", f"{base_url}/friends/add", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("friends", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def remove_friend(base_url: str, name: str, friend_name: str, access_key: str = "") -> list[str] | None:
    """Удаляет пользователя из друзей. Возвращает обновлённый список имён друзей
    или None при ошибке."""
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"name": friend_name}, ensure_ascii=False).encode("utf-8")
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status, data, _ = _request("POST", f"{base_url}/friends/remove", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("friends", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_leaderboard(base_url: str, access_key: str = "") -> list[dict]:
    """Возвращает топ игроков по ELO из ChessBot:
    [{name, score, wins, losses, draws}, ...]."""
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    try:
        status, data, _ = _request("GET", f"{base_url}/leaderboard", headers, timeout=8)
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    if status != 200:
        return []
    try:
        return json.loads(data.decode("utf-8")).get("scores", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def get_voice_info(base_url: str, name: str = "", access_key: str = "") -> dict:
    """Спрашивает /voice/info: поднят ли голосовой TCP-сервер и его порт.

    Вызывается перед открытием VoiceChannelDialog (см. chat_screen)."""
    return _get_json(f"{base_url}/voice/info", name, access_key)


def get_voice_participants(base_url: str, name: str = "", access_key: str = "") -> list[str]:
    """Список участников голосового канала (/voice/participants).

    Опрашивается периодически, поэтому таймаут короче обычного."""
    data = _get_json(f"{base_url}/voice/participants", name, access_key, timeout=3)
    participants = data.get("participants", [])
    return participants if isinstance(participants, list) else []


def get_voxel_info(base_url: str, name: str = "", access_key: str = "") -> dict:
    """Информация о TCP-сервере воксельных сессий (/voxel/info): поднят
    ли и на каком порту (порт = HTTP+2)."""
    return _get_json(f"{base_url}/voxel/info", name, access_key, timeout=3)


def _post_json(
    url: str,
    payload: dict,
    name: str = "",
    access_key: str = "",
    timeout: int = 5,
) -> dict:
    """Общий POST-запрос с JSON-телом и протокольными заголовками ->
    разобранный JSON-ответ. Парный к _get_json: держит транспорт в lib,
    а не во вьюхах."""
    headers: dict = {"Content-Type": "application/json; charset=utf-8"}
    if name:
        headers[HEADER_FROM] = header_encode(name)
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", url, headers, body, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"неверный ответ сервера: {e}")


def get_voxel_sessions(base_url: str, name: str = "", access_key: str = "") -> list:
    """Список активных воксельных сессий (лобби диалога)."""
    data = _get_json(f"{base_url}/voxel/sessions", name, access_key, timeout=3)
    sessions = data.get("sessions", [])
    return sessions if isinstance(sessions, list) else []


def voxel_create_session(
    base_url: str,
    name: str,
    session_id: str,
    *,
    game_mode: str = "pve",
    wall_hp: int = 100,
    team_red_size: int = 4,
    team_blue_size: int = 4,
    access_key: str = "",
) -> dict:
    """Создаёт воксельную сессию (POST /voxel/create)."""
    return _post_json(
        f"{base_url}/voxel/create",
        {
            "session_id": session_id,
            "game_mode": game_mode,
            "wall_hp": wall_hp,
            "team_red_size": team_red_size,
            "team_blue_size": team_blue_size,
        },
        name,
        access_key,
        timeout=3,
    )


def voxel_add_bot(
    base_url: str, name: str, session_id: str, team: str = "none", access_key: str = ""
) -> dict:
    """Добавляет бота в воксельную сессию (POST /voxel/addbot)."""
    return _post_json(
        f"{base_url}/voxel/addbot",
        {"session_id": session_id, "team": team},
        name,
        access_key,
        timeout=3,
    )


def voxel_fill_bots(
    base_url: str, name: str, session_id: str, access_key: str = ""
) -> dict:
    """Заполняет пустые слоты сессии ботами (POST /voxel/fillbots)."""
    return _post_json(
        f"{base_url}/voxel/fillbots",
        {"session_id": session_id},
        name,
        access_key,
        timeout=3,
    )


def voxel_start_game(
    base_url: str, name: str, session_id: str, access_key: str = ""
) -> dict:
    """Запускает игру в сессии (POST /voxel/start)."""
    return _post_json(
        f"{base_url}/voxel/start",
        {"session_id": session_id},
        name,
        access_key,
        timeout=3,
    )


def send_bot_command(
    base_url: str, bot_id: str, command: str, args: list[str], name: str, access_key: str = ""
) -> str:
    """Отправляет команду боту на сервер.

    Args:
        base_url: URL сервера
        bot_id: ID бота (например, "night_shift")
        command: команда (например, "!hunt")
        args: аргументы команды
        name: имя отправителя
        access_key: ключ доступа (опционально)

    Returns:
        Ответ бота или пустая строка
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    body = json.dumps(
        {"bot_id": bot_id, "command": command, "args": args}, ensure_ascii=False
    ).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/bot_command", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("bot_response", "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""


def send_bot_command_full(
    base_url: str, bot_id: str, command: str, args: list[str], name: str, access_key: str = ""
) -> dict:
    """То же что send_bot_command, но возвращает полный ответ сервера:
    {"ok": True, "seq": int | None, "bot_response": str, "bot_action": dict | None}
    Нужно для команд, которые возвращают action (например, savescore).
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    body = json.dumps(
        {"bot_id": bot_id, "command": command, "args": args}, ensure_ascii=False
    ).encode("utf-8")
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status, data, _ = _request("POST", f"{base_url}/bot_command", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def dispatch_command(base_url: str, name: str, text: str, access_key: str = "") -> dict:
    """Диспетчер !-команд: отправляет текст на /dispatch, сервер сам находит
    бота, который матчит команду, и выполняет её.

    Возвращает:
        {"matched": False}                              — никто не сматчил, отправить как обычное сообщение
        {"matched": True, "bot_id": ..., "bot_name": ...,
         "bot_response": str, "bot_action": dict | None, "seq": int | None}
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    if _session.is_valid():
        headers["X-Relay-Auth"] = _session.auth_header(body)
    try:
        status, data, _ = _request("POST", f"{base_url}/dispatch", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"matched": False}


def send_dm(base_url: str, name: str, recipient: str, text: str, access_key: str = "",
            encrypt: bool = False) -> int:
    """Отправляет личное сообщение пользователю.

    Args:
        base_url: URL сервера
        name: имя отправителя
        recipient: имя получателя
        text: текст сообщения
        access_key: ключ доступа
        encrypt: зашифровать сообщение перед отправкой

    Returns:
        seq отправленного сообщения
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)

    message_data = {"to": recipient, "text": text}
    if encrypt and _crypto_manager._secret_key:
        encrypted_text, iv = _crypto_manager.encrypt(text)
        if encrypted_text and iv:
            message_data = {"to": recipient, "text": encrypted_text, "encrypted": True, "iv": iv}

    body = json.dumps(message_data, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/dm/send", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8"))["seq"]


def get_dm_history(
    base_url: str, name: str, other_user: str, access_key: str = "", limit: int | None = None
) -> list[dict]:
    """Получает историю личных сообщений с пользователем.

    Args:
        base_url: URL сервера
        name: имя текущего пользователя
        other_user: имя собеседника
        access_key: ключ доступа
        limit: максимальное количество сообщений

    Returns:
        Список сообщений
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    url = f"{base_url}/dm/history?user={quote(other_user)}"
    if limit is not None:
        url += f"&limit={limit}"
    try:
        status, data, _ = _request("GET", url, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("messages", [])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def get_dm_conversations(base_url: str, name: str, access_key: str = "") -> list[dict]:
    """Получает список диалогов пользователя с последними сообщениями.

    Args:
        base_url: URL сервера
        name: имя пользователя
        access_key: ключ доступа

    Returns:
        Список диалогов [{user, last_message, last_time, unread}, ...]
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/dm/conversations", headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("conversations", [])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def list_bots(base_url: str, name: str = "", access_key: str = "") -> list[str]:
    """Получает список активных ботов на сервере.

    Args:
        base_url: URL сервера
        name: имя пользователя (для авторизации)
        access_key: ключ доступа

    Returns:
        Список ID ботов
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/bots/list", headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("bots", [])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def add_bot(base_url: str, bot_id: str, name: str = "", access_key: str = "") -> bool:
    """Добавляет бота на сервер по имени/ID.

    Args:
        base_url: URL сервера
        bot_id: ID бота для добавления
        name: имя пользователя (для авторизации)
        access_key: ключ доступа

    Returns:
        True если бот успешно добавлен
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"bot_id": bot_id}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/bots/add", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8")).get("ok", False)


def remove_bot(base_url: str, bot_id: str, name: str = "", access_key: str = "") -> bool:
    """Удаляет бота с сервера.

    Args:
        base_url: URL сервера
        bot_id: ID бота для удаления
        name: имя пользователя (для авторизации)
        access_key: ключ доступа

    Returns:
        True если бот успешно удалён
    """
    headers = {HEADER_FROM: header_encode(name), "Content-Type": "application/json; charset=utf-8"}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    body = json.dumps({"bot_id": bot_id}, ensure_ascii=False).encode("utf-8")
    try:
        status, data, _ = _request("POST", f"{base_url}/bots/remove", headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status != 200:
        raise RelayError(_error_text(data, status))
    return json.loads(data.decode("utf-8")).get("ok", False)


def list_files(base_url: str, name: str = "", access_key: str = "") -> list[dict]:
    """Получает список всех файлов с сервера (файловая витрина).

    Args:
        base_url: URL сервера
        name: имя пользователя (для авторизации)
        access_key: ключ доступа

    Returns:
        Список файловых событий [{seq, kind, from, ts, file_id, name, size}, ...]
    """
    headers = {}
    if access_key:
        headers[HEADER_KEY] = header_encode(access_key)
    if name:
        headers[HEADER_FROM] = header_encode(name)
    try:
        status, data, _ = _request("GET", f"{base_url}/files", headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RelayError(str(getattr(e, "reason", e)) or type(e).__name__)
    if status == 403:
        raise RelayError("неверный ключ доступа")
    if status != 200:
        raise RelayError(_error_text(data, status))
    try:
        return json.loads(data.decode("utf-8")).get("files", [])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RelayError(f"плохой ответ хоста: {e}")


def _error_text(data: bytes, status: int) -> str:
    try:
        parsed = json.loads(data.decode("utf-8"))
        return parsed.get("error", f"HTTP {status}")
    except Exception:
        return f"HTTP {status}"
