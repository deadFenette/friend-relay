"""
Аутентификация пользователей — закрывает главную дыру безопасности проекта.

Раньше сервер верил заголовку X-Relay-From — это просто текст. Любой мог
открыть Postman и отправить сообщение "от alice". Теперь это невозможно:

1. При первом /ping клиент получает session_token (случайные 32 байта, base64).
   Сервер хранит соответствие session_token → username.

2. Каждый последующий запрос клиент подписывает:
   X-Relay-Auth: "<session_token>:<HMAC-SHA256(session_secret, body)>"
   где body — сериализованное тело запроса (для GET — путь+query).

3. Сервер проверяет:
   - HMAC совпадает (тело не подменено)
   - session_token валидный
   - X-Relay-From совпадает с тем, кто зарегистрировал сессию

Если что-то не так — 403 Forbidden.

Для обратной совместимости: запросы без X-Relay-Auth работают, но только
если нет access_key. Это нужно для /ping (который получает token), и для
старых клиентов (пока не мигрируют).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

# Время жизни сессии — 7 дней. После этого клиент должен пере-пинговать
# и получить новый token.
_SESSION_TTL = 7 * 24 * 3600  # секунд


def generate_session_token() -> str:
    """Случайный session token (32 байта → base64, 44 символа)."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def sign_request(session_token: str, body: bytes) -> str:
    """Вычисляет HMAC-SHA256(session_token, body).
    Возвращает hex-строку (64 символа).
    """
    if not session_token:
        return ""
    return hmac.new(
        session_token.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(session_token: str, body: bytes, signature: str) -> bool:
    """Проверяет подпись запроса в постоянном времени (защита от timing-атак).

    Сравниваем БАЙТЫ: hmac.compare_digest на str принимает только ASCII,
    и мусорная не-ASCII подпись из заголовка (битый клиент/сканер) ронял
    проверку TypeError'ом — сервер отвечал 500 вместо честного 403.
    """
    if not session_token or not signature:
        return False
    expected = sign_request(session_token, body)
    return hmac.compare_digest(
        expected.encode("utf-8"), signature.encode("utf-8"))


def parse_auth_header(header_value: str) -> tuple[str, str] | None:
    """Разбирает 'X-Relay-Auth: <token>:<signature>' → (token, signature).
    Возвращает None если формат неправильный.
    """
    if not header_value:
        return None
    parts = header_value.split(":", 1)
    if len(parts) != 2:
        return None
    token, signature = parts[0].strip(), parts[1].strip()
    if not token or not signature:
        return None
    return token, signature


# ── Серверная сторона: хранилище сессий ──────────────────────────────


class SessionStore:
    """Хранилище активных сессий: session_token → (username, created_ts).

    Не персистится на диск — при перезапуске хоста все клиенты должны
    пере-пинговать. Это нормально и даже хорошо: сессии короткоживущие,
    утечка token'а ограничена TTL.

    Thread-safe: методы под блокировкой.
    """

    def __init__(self):
        import threading
        self._sessions: dict[str, tuple[str, float]] = {}  # token → (name, ts)
        self._lock = threading.Lock()

    def register(self, username: str) -> str:
        """Регистрирует новую сессию для пользователя, возвращает token.
        Если у пользователя уже была сессия — она аннулируется (новый token).
        """
        token = generate_session_token()
        now = time.time()
        with self._lock:
            # Аннулируем старые сессии этого пользователя
            self._sessions = {
                t: (n, ts) for t, (n, ts) in self._sessions.items()
                if n != username
            }
            self._sessions[token] = (username, now)
        return token

    def validate(self, token: str) -> str | None:
        """Проверяет токен. Возвращает username если валиден, иначе None.
        Автоматически чистит протухшие сессии.
        """
        if not token:
            return None
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, created = entry
            if now - created > _SESSION_TTL:
                del self._sessions[token]
                return None
            return username

    def revoke(self, token: str) -> None:
        """Аннулирует конкретную сессию (например при disconnect)."""
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, username: str) -> None:
        """Аннулирует все сессии пользователя (например при кике)."""
        with self._lock:
            self._sessions = {
                t: (n, ts) for t, (n, ts) in self._sessions.items()
                if n != username
            }

    def cleanup_expired(self) -> int:
        """Чистит все протухшие сессии. Возвращает сколько удалил.
        Вызывается периодически сервером.
        """
        now = time.time()
        with self._lock:
            before = len(self._sessions)
            self._sessions = {
                t: (n, ts) for t, (n, ts) in self._sessions.items()
                if now - ts <= _SESSION_TTL
            }
            return before - len(self._sessions)

    def needs_cleanup(self, threshold: int) -> bool:
        """True, если сессий больше threshold — пора чистить протухшие.

        Публичный метод вместо лазания снаружи в приватный _sessions
        (relay.issue_session раньше читал чужой приватный атрибут).
        """
        with self._lock:
            return len(self._sessions) > threshold

    def count(self) -> int:
        """(v2.0.2) Сколько сессий сейчас живёт — для GET /server/stats.
        Публичный читатель размера по той же причине, что и needs_cleanup:
        наружу торчит API, а не чужие приватные поля."""
        with self._lock:
            return len(self._sessions)

    def names(self) -> set:
        """(v3.3.2) Имена всех активных сессий — для подбора свободного
        дискриминатора («Жуж#2»): имя занято, если есть живая сессия."""
        with self._lock:
            return {n for (n, _ts) in self._sessions.values()}

    def details(self) -> list[dict]:
        """(v3.4.0) Снапшот активных сессий для GUI сервера: имя и сколько
        секунд назад подключился. Один пользователь = одна сессия (register
        аннулирует старую), так что дублей имён не будет."""
        now = time.time()
        with self._lock:
            out = []
            for (n, ts) in self._sessions.values():
                if now - ts > _SESSION_TTL:
                    continue  # протухшая — считается несуществующей
                out.append({"name": n, "since": ts, "age_s": int(now - ts)})
            return out


# ── Клиентская сторона: менеджер сессии ──────────────────────────────


class ClientSession:
    """Клиентский менеджер сессии — хранит token и подписывает запросы.

    Хранится в settings.json (см. lib/storage.py), переживает перезапуск.
    """

    def __init__(self, token: str = ""):
        self._token = token

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = token

    def clear(self) -> None:
        self._token = ""

    def is_valid(self) -> bool:
        return bool(self._token)

    def auth_header(self, body: bytes) -> str:
        """Формирует значение для X-Relay-Auth: '<token>:<signature>'.
        Если token'а нет — возвращает пустую строку (запрос уйдёт без подписи).
        """
        if not self._token:
            return ""
        sig = sign_request(self._token, body)
        return f"{self._token}:{sig}"

    def auth_header_for_get(self, path: str, query: str = "") -> str:
        """Для GET-запросов подписываем path + query (нет body)."""
        body = (path + (f"?{query}" if query else "")).encode("utf-8")
        return self.auth_header(body)
