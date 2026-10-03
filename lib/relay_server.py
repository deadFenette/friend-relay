from __future__ import annotations

import logging
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from lib import json_fast
from lib.constants import (
    AVATARS_DIR_NAME,
    DEFAULT_MAX_FILE_SIZE,
    FLOOD_MSG_BURST,
    FLOOD_MSG_RATE,
    PROFILES_DIR_NAME,
    PROTOCOL_VERSION,
)
from lib.crypto import CryptoManager, generate_salt
from lib.domain import EventStore, PresenceBoard
from lib.link_preview import fetch_link_preview, find_first_url
from lib.server_log import get_logger  # (v3.4.0) журнал сервера (файл, консоль — только ошибки)
from lib.telemetry import Telemetry  # (v2.0.5) самонаблюдение хоста
from lib.util import LatencyMeter, RateLimiter, fmt_uptime

# Импортируем менеджер ботов (отложенный импорт чтобы избежать циклических зависимостей)
_bot_manager = None


def get_bot_manager(data_dir: Path):
    global _bot_manager
    if _bot_manager is None:
        from lib.bots import BotManager

        _bot_manager = BotManager(data_dir)
    return _bot_manager


# Слои проекта (см. ARCHITECTURE.md):
#
#   domain/     - "ЧТО это за сущность": EventStore (журнал событий, дельты,
#                 tombstone, DM-лог), PresenceBoard (онлайн/печатает).
#   relay_server- "КАК сделать" (application): фасад-юзкейсы над доменом и
#                 сервисами (каналы, профили, боты, крипто, сессии).
#   server/     - "КАК показать/отдать": HTTP-транспорт (маршруты, парсинг,
#                 коды ответов). Не знает про EventStore - только про фасад.
#
# Правило зависимостей: transport -> application -> domain. Обратно - никогда.
# (Из домена запрещены импорты http.server/PySide6, из фасада - разбор URL-ей
# и заголовков запроса.)

# «go» в списке (v2.0.6): GoBot — рефери онлайн-матчей Го; ответы команд
# host/join/move уходят в чат, а state/getscores/meta — поллинговые
# (silent=True в самом боте), поэтому повторение «ночи-снейка» не грозит.
_SILENT_BOT_IDS = ("night_shift", "snake", "orbital", "voxel_shooter",
                   "chess", "go", "music")


def _create_default_bot(bot_id: str, data_dir: Path):
    """Фабрика ботов по умолчанию: возвращает готовый экземпляр или None.
    (Раньше пять почти одинаковых try/except-блоков жили в
    _register_default_bots; собраны в один список.)"""
    if bot_id == "night_shift":
        from lib.bots.night_shift import NightShiftBot

        return NightShiftBot(data_dir)
    if bot_id == "snake":
        from lib.bots.snake import SnakeBot

        return SnakeBot(data_dir)
    if bot_id == "orbital":
        from lib.bots.orbital import OrbitalBot

        return OrbitalBot(data_dir)
    if bot_id == "voxel_shooter":
        # VoxelShooterBot — мультиплеерный шутер
        from lib.bots.voxel import VoxelShooterBot

        return VoxelShooterBot(data_dir)
    if bot_id == "chess":
        # ChessBot — шахматы с ИИ
        from lib.bots.chess import ChessBot

        return ChessBot(data_dir)
    if bot_id == "go":
        # (v2.0.6) GoBot — Го: онлайн-матчи по коду + ELO. Движок правил
        # lib/games/go_engine.py — общий с Qt-диалогом и веб-версией.
        from lib.bots.go import GoBot

        return GoBot(data_dir)
    if bot_id == "music":
        # MusicBot — управление музыкой в голосовом канале
        from lib.bots.music import MusicBot

        # host_name передаётся позже через set_host_name, но для ботов
        # по умолчанию используем пустую строку (хост определяется динамически)
        return MusicBot(data_dir, host_name="")
    return None


class RelayServer:
    """Фасад application-слоя: хаб, хранящий историю сообщений/файлов на диске
    (JSONL + просто файлы на диске рядом) и раздающий всем через
    GET /events?since=N. Поллинг вместо постоянного сокета - та же логика,
    что уже проверена на раздаче модов: дырявый ZeroTier/Tailscale/Porthole-
    тоннель переживает обычный HTTP-запрос с повтором гораздо надёжнее, чем
    висящее соединение.

    Слушает на 0.0.0.0 - доступен друзьям по твоему tailnet-адресу точно
    так же, как раздача модов.

    Раньше этот класс был god-object'ом на 2100 строк (56 методов + HTTP-монолит
    в start()). Теперь: домен - в lib/domain/event_store.py, HTTP - в
    lib/server/http_api.py, каналы/профили - в своих сервисах. Здесь остались
    только юзкейсы ("КАК сделать") и композиция зависимостей. Публичный API
    не менялся - клиент (lib/client.py), боты и тесты работают как раньше."""

    def __init__(
        self,
        data_dir: Path,
        host_name: str,
        access_key: str = "",
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        admin_key: str = "",
    ):
        self.data_dir = data_dir
        self.avatars_dir = data_dir / AVATARS_DIR_NAME
        self.profiles_dir = data_dir / PROFILES_DIR_NAME
        self.host_name = host_name
        self.access_key = access_key
        self.max_file_size = max_file_size
        # (v3.3.1) Ключ админа: имя host_name («Жуж») можно занять только
        # с ним (или с loopback — свой же GUI-клиент). Закрывает перехват
        # личности: раньше любой /ping?name=<host_name> становился админом
        # и аннулировал сессию настоящего хоста (SessionStore.register
        # стирает старые сессии того же имени).
        self.admin_key = admin_key

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        # (v3.4.0) Журнал сервера: если процесс (server_main.py / GUI) ещё
        # не настроил его — настраиваем сами (консоль не трогаем: немая).
        # Лаунчеры настраивают журнал ДО создания RelayServer — их
        # параметры (консоль/GUI-колбэк) здесь не перетираются.
        from lib import server_log as _slog

        if not _slog.is_configured():
            _slog.setup_server_logging(Path(data_dir), console_errors=False)

        # ── домен: журнал событий + присутствие ──────────────────────────
        # EventStore владеет файлами files_dir/dm/dm_files и history.jsonl.
        self._store = EventStore(data_dir)
        self._presence = PresenceBoard()

        # Каналы для групповых чатов
        self.channels_dir = data_dir / "channels"
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        # ChannelManager вынесен в lib/channel_manager.py — сервис-слой.
        # RelayServer делегирует сюда все channel-операции.
        from lib.channel_manager import ChannelManager

        self._channels = ChannelManager(self.channels_dir)
        # seq-провайдер: ChannelManager вызывает когда нужно выдать seq
        # (глобальный, чтобы не пересекался с seq событий основного чата)
        self._channels.set_seq_provider(self._store.next_seq)

        # ── сервисы ──────────────────────────────────────────────────────
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._base_port = 0  # фактический порт HTTP (см. start())
        # (v2.0.2) Монотонная метка старта для GET /server/stats (uptime).
        # monotonic, а не time.time(): перевод часов не должен уводить
        # uptime в минус — это единственное назначение метки.
        self._started_at: float = 0.0
        # TLS-мультиплексор (http и https на одном порту) и WS-мост голоса
        # (см. _start_tls_and_bridge). Отдельного HTTPS-порта больше нет:
        # браузер ходит по https://IP:ТОТ_ЖЕ_ПОРТ, Qt — по http.
        self._httpsd = None
        self._https_thread: threading.Thread | None = None
        self._https_port = 0
        self._mux = None
        self._voice_bridge = None

        # Менеджер ботов (инициализируется лениво)
        self._bot_manager = None

        # Менеджер профилей (инициализируется лениво, см. _get_profile_manager)
        self._profile_mgr = None

        # Менеджер криптографии
        self._crypto = CryptoManager()
        if access_key:
            self._crypto.set_secret_key(access_key)

        # Per-installation соль (PBKDF2) для /crypto/info: веб-клиент выводит
        # AES-ключ по той же соли, что и Qt-клиент хоста (обе стороны читают
        # один settings.json). Читаем файл напрямую без автозаписи; если его
        # нет (голый тестовый сервер) — эфемерная соль на процесс.
        self._crypto_salt = ""
        try:
            import json as _json

            from lib.constants import SETTINGS_FILE

            if SETTINGS_FILE.exists():
                _data = _json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self._crypto_salt = (_data or {}).get("access_key_salt", "") or ""
            if not self._crypto_salt:
                self._crypto_salt = generate_salt()
        except Exception:
            self._crypto_salt = generate_salt()

        # Хранилище сессий — закрывает дыру с подменой sender.
        # Клиент получает token при /ping, подписывает каждый запрос,
        # сервер проверяет что X-Relay-From совпадает с тем кто зарегистрировал
        # сессию. См. lib/auth.py.
        from lib.auth import SessionStore

        self._sessions = SessionStore()

        # (v2.0.4) Наблюдаемость и антифлуд.
        # _req_stats: транспорт (http_api) замеряет время каждого запроса
        # и зовёт note_request() — фасад только агрегирует, чтобы
        # /server/stats показывал requests_total/avg_ms/p95_ms («как
        # дышится серверу», а не только «жив ли он»).
        # _flood: токен-ведро на отправителя для /send_text и /dm/send
        # (политика — allow_message(), механизм — lib/util.RateLimiter).
        # Лимиты — в constants.FLOOD_MSG_*; с запасом выше любого
        # человеческого темпа, режется только скриптовый флуд.
        self._req_stats = LatencyMeter(window=256)
        self._flood = RateLimiter(rate=FLOOD_MSG_RATE, burst=FLOOD_MSG_BURST)

        # (v2.0.5) Телеметрия — локальное самонаблюдение хоста (lib/
        # telemetry.py): счётчики/латентности/пики ТОЛЬКО в data_dir/
        # telemetry/, без контента и имён, отключается одним POST
        # /telemetry/disable. Здесь только создание; события дёргают
        # фасадные методы и транспорт по ходу работы.
        self.telemetry = Telemetry(data_dir)

        # Голосовой сервер (TCP, на port+1 по умолчанию). Запускается вместе
        # с HTTP-сервером — клиенты могут подключаться к голосовому каналу
        # если хост активен. См. lib/voice_server.py.
        from voice.mixer import VoiceMixer

        self._voice: VoiceMixer | None = None  # поднимается в start()

        # (v3.6.0) Модуль «Экран» — демонстрация экрана со звуком.
        # Сигналинг-хаб WebRTC: БЕЗ портов и потоков (медиа идёт P2P
        # между браузерами, сервер релеит только offer/answer-JSON).
        # См. screen/__init__.py — самодостаточный пакет по образцу voice/.
        from screen.hub import ScreenHub

        self._screen = ScreenHub()

        # Voxel Shooter — TCP-сервер мультиплеерных сессий (порт = HTTP+2).
        # Запускается в start(). См. lib/voxel_server.py.
        from lib.voxel_server import VoxelSessionServer

        self._voxel: VoxelSessionServer | None = None

        # Автоматически регистрируем бота игры при старте
        self._register_default_bots()

    # ── менеджер ботов ────────────────────────────────────────────────────
    def get_bot_manager(self):
        """Получает менеджер ботов (ленивая инициализация)."""
        if self._bot_manager is None:
            from lib.bots import BotManager

            self._bot_manager = BotManager(self.data_dir)
        return self._bot_manager

    @staticmethod
    def _bot_response_to_chat(response: str | dict) -> tuple[str | None, dict | None]:
        """Разбирает ответ бота на (text_in_chat, bot_action).

        Используется в /bot_command и /dispatch — раньше эта логика
        дублировалась в обоих эндпоинтах.

        Правила:
          - str → текст идёт в чат, bot_action=None
          - dict с полем 'text' → текст идёт в чат (если не silent)
          - dict с 'silent: True' → text НЕ идёт в чат (поллинговые команды
            state/getscores/matchlist/meta, иначе чат заспамливается)
          - dict без 'text' → ничего не идёт в чат

        v3: ответы с action='error' автоматически silent — ошибки бота
        касаются только запрашивающего, в общий чат их писать не нужно.
        Также silent для match_joined/match_finished/match_left —
        жизненный цикл матча видят игроки в их диалогах через state-poll,
        общий чат не засоряется.
        """
        if isinstance(response, str):
            return response, None
        if isinstance(response, dict):
            # silent-ответы (поллинг) — не спамим чат
            if response.get("silent"):
                return None, response
            # v3: ошибки и приватные жизненные события — не в общий чат
            action = response.get("action")
            if action in ("error", "match_joined", "match_finished",
                          "match_left", "match_state", "move_accepted"):
                return None, response
            t = response.get("text")
            if isinstance(t, str) and t:
                return t, response
            return None, response
        return None, None

    # Публичный синоним (используется транспортом; старое имя оставлено
    # для обратной совместимости).
    bot_response_to_chat = _bot_response_to_chat

    def _register_default_bots(self) -> None:
        """Регистрирует ботов по умолчанию при старте сервера."""
        for bot_id in _SILENT_BOT_IDS:
            try:
                bot_manager = self.get_bot_manager()
                if bot_manager.get_bot(bot_id) is not None:
                    continue  # уже зарегистрирован (повторный start())
                bot = _create_default_bot(bot_id, self.data_dir)
                if bot is not None:
                    # Для MusicBot устанавливаем имя хоста
                    if bot_id == "music" and hasattr(bot, "set_host_name"):
                        bot.set_host_name(self.host_name)
                    bot_manager.register_bot(bot)
            except Exception:
                # Если не удалось зарегистрировать бота - не критично,
                # сервер продолжит работу без него
                pass

    # ── сессии и аутентификация (application-политика) ────────────────────
    def issue_session(self, name: str) -> str:
        """Регистрирует сессию и выдаёт token (вызывается из /ping).
        Пустое имя - пустой token (как и раньше)."""
        if not name:
            return ""
        # (v2.0.5) Телеметрия: факт входа (без имени — имена в телеметрию
        # не идут, тип клиента считает транспорт по User-Agent).
        self.telemetry.record("session.created")
        # Заодно (не под каждый /ping, а при переполнении) чистим протухшие сессии: cleanup_expired()
        # существовал с самого начала, но его никто не вызывал — словарь
        # сессий рос 7 дней без предела (спамящий /ping случайными именами
        # скам-клиент раздувал память хоста).
        if self._sessions.needs_cleanup(1024):
            self._sessions.cleanup_expired()
        token = self._sessions.register(name)
        # (v3.4.0) Факт входа — в журнал (файл/GUI), в отличие от телеметрии
        # имя здесь НЕ приватно: это журнал самого хоста, он и так их видит.
        get_logger().info("вход: %s", name)
        return token

    def online_details(self) -> list[dict]:
        """(v3.4.0) Кто в сети — с деталями для GUI сервера.

        Слияние двух источников: живые сессии (факт входа, время подключения)
        и presence (живость поллинга, «печатает»). Показываем объединение:
        сессия без presence — зашёл, но ещё не опрашивает; presence без
        сессии — протухает по таймеру сам, вскоре исчезнет из списка.

        Возвращает список dict: name, admin (хост?), age_s (в сети, сек),
        typing, last_seen_ago, has_session."""
        pres = self._presence.details()
        sess = {d["name"]: d for d in self._sessions.details()}
        out = []
        for n in sorted(set(pres) | set(sess)):
            p = pres.get(n, {})
            s = sess.get(n, {})
            out.append({
                "name": n,
                "admin": n == self.host_name,
                "age_s": int(s.get("age_s", 0) or 0),
                "typing": bool(p.get("typing")),
                "last_seen_ago": int(p.get("last_seen_ago", -1)),
                "has_session": n in sess,
            })
        return out

    def kick_user(self, name: str, by: str = "") -> bool:
        """(v3.4.0) Кик участника (кнопка в GUI сервера / будущие команды).

        Аннулирует ВСЕ сессии имени — следующий запрос клиента получит
        отказ авторизации и клиент покажет «сессия недействительна» (это
        уже умеют все клиенты — та же механика, что при рестарте хоста).
        В чат пишется системное сообщение — все видят факт кика.

        Хоста кикнуть нельзя: он админ по определению. Возвращает True,
        если хотя бы одна сессия была аннулирована."""
        if not name or name == self.host_name:
            return False
        before = self._sessions.count()
        self._sessions.revoke_user(name)
        kicked = self._sessions.count() < before
        try:
            self._store.add_text(by or "⚖ сервер",
                                 f"⚖ {name} отключён(а) администратором")
        except Exception:
            pass  # журнал чата — не причина отказывать в кике
        self.telemetry.record("admin.kick")
        get_logger().info("кик: %s (кем: %s)", name, by or "сервер")
        return kicked

    def next_free_guest_name(self, base: str, limit: int = 99) -> str | None:
        """(v3.3.2) Дискриминаторы как в Discord: база «Жуж» занята —
        подбираем первый свободный «Жуж#2», «Жуж#3», … Имя занято, если это
        host_name (всегда зарезервирован) или есть живая сессия с таким
        именем. Возвращает None, если лимит исчерпан.

        Дискриминатор — часть строки имени, поэтому вся остальная система
        (журнал событий, профили, аватары, DM, админы музыки) работает без
        изменений: «Жуж#2» — просто ещё одно уникальное имя."""
        from lib.util import safe_name

        clean = safe_name(base)
        if not clean:
            return None
        taken = self._sessions.names()
        taken.add(self.host_name)
        for n in range(2, limit + 1):
            candidate = f"{clean}#{n}"
            if candidate not in taken:
                return candidate
        return None

    def authenticate(self, raw_body: bytes, claimed_sender: str, auth_header: str = "") -> str | None:
        """Валидирует отправителя: возвращает проверенное имя или None.

        Если есть X-Relay-Auth: проверяем подпись, потом сверяем что
        claimed_sender совпадает с тем, кто зарегистрировал сессию.
        Если auth-заголовка нет: при включённом access_key запрос
        ОТКАЗЫВАЕТСЯ (раньше пропускался - любой, знающий ключ, мог
        слать сообщения чужим именем, править и удалять чужое, менять
        чужие аватарки и профили - имперсонация). Сервер без ключа
        остаётся открытым как раньше (ключа нет - защиты всё равно нет).

        Транспорт передаёт сюда сырые заголовки и тело; правила проверки
        живут здесь, в application-слое."""
        if auth_header:
            # Новый формат — есть подпись
            from lib.auth import parse_auth_header, verify_signature

            parsed = parse_auth_header(auth_header)
            if parsed is None:
                return None
            token, signature = parsed
            session_user = self._sessions.validate(token)
            if session_user is None:
                return None  # протух или не существует
            if not verify_signature(token, raw_body, signature):
                return None  # подпись не совпала — подмена
            # Сессия валидна. Проверяем что отправитель совпадает
            if claimed_sender and claimed_sender != session_user:
                return None  # подмена имени!
            return session_user
        # Без подписи: только на сервере без ключа доступа (doc-контракт
        # lib/auth.py: «запросы без X-Relay-Auth работают, только если
        # нет access_key» — теперь это правда и в коде, а не только в
        # докстринге).
        if self.access_key:
            return None
        return claimed_sender if claimed_sender else None

    def begin_stream_auth(self, claimed_sender: str, auth_header: str = ""):
        """Аутентификация для СТРИМИНГОВЫХ тел (загрузка файлов/аватарок):
        тело может быть гигабайтом, в RAM его не прочтёшь — подпись
        (HMAC-SHA256 по полному телу) проверяется инкрементально, кусок
        за куском, тем же ключом, что и sha256.

        Возвращает (session_user, mac, signature) — mac это объект
        hmac, который транспорт кормит кусками тела, а signature —
        ожидаемая hex-подпись. (None, None, None) — аутентификация
        невозможна (нет подписи при включённом ключе / битый заголовок /
        чужая сессия): запрос должен быть отвергнут ДО записи на диск.

        На сервере без ключа без X-Relay-Auth — старое доверчивое
        поведение (claimed_sender, None, "") для совместимости."""
        from lib.auth import parse_auth_header

        if not auth_header:
            if self.access_key:
                return (None, None, None)
            return (claimed_sender or None, None, "")
        parsed = parse_auth_header(auth_header)
        if parsed is None:
            return (None, None, None)
        token, signature = parsed
        session_user = self._sessions.validate(token)
        if session_user is None:
            return (None, None, None)
        if claimed_sender and claimed_sender != session_user:
            return (None, None, None)  # подмена имени
        import hashlib
        import hmac as _hmac

        mac = _hmac.new(token.encode("utf-8"), b"", hashlib.sha256)
        return (session_user, mac, signature)

    # ── личные сообщения (делегирование в домен) ──────────────────────────
    def send_dm(self, sender: str, recipient: str, text: str,
                encrypted: bool = False, iv: str = "") -> dict:
        # (v2.0.5) Телеметрия: только факт отправки и флаг шифрования —
        # адресат и текст в телеметрию не передаются.
        self.telemetry.record("dm.sent")
        if encrypted:
            self.telemetry.record("dm.sent.encrypted")
        return self._store.send_dm(sender, recipient, text, encrypted=encrypted, iv=iv)

    def get_dm_history(self, user1: str, user2: str, limit: int | None = None) -> list[dict]:
        return self._store.get_dm_history(user1, user2, limit)

    def get_dm_conversations(self, username: str) -> list[dict]:
        return self._store.get_dm_conversations(username)

    def add_dm_file(self, sender: str, recipient: str, filename: str, data: bytes,
                    sha256: str = "") -> dict:
        ev = self._store.add_dm_file(sender, recipient, filename, data, sha256=sha256)
        self.telemetry.record("dm.files")
        self._note_file_upload(ev)
        return ev

    def add_dm_file_from_path(self, sender: str, recipient: str, filename: str,
                              source: Path, sha256: str = "") -> dict:
        ev = self._store.add_dm_file_from_path(sender, recipient, filename,
                                               source, sha256=sha256)
        self.telemetry.record("dm.files")
        self._note_file_upload(ev)
        return ev

    def get_dm_file_event(self, file_id: str) -> dict | None:
        return self._store.get_dm_file_event(file_id)

    def dm_file_bytes_path(self, file_id: str) -> Path:
        return self._store.dm_file_bytes_path(file_id)

    # ── каналы (делегирование в ChannelManager) ───────────────────────────
    def _next_seq_inc(self) -> int:
        """Выдаёт следующий глобальный seq. Оставлено для обратной
        совместимости: ChannelManager теперь получает EventStore.next_seq
        через seq-provider."""
        return self._store.next_seq()

    def create_channel(self, name: str, creator: str) -> dict:
        # (v2.0.5) Телеметрия: факт создания (имя канала не записываем).
        self.telemetry.record("channel.created")
        return self._channels.create_channel(name, creator)

    def get_channels(self) -> list[dict]:
        return self._channels.get_channels()

    def send_channel_message(self, channel_name: str, sender: str, text: str,
                             encrypted: bool = False, iv: str = "") -> dict:
        self.telemetry.record("channel.messages")
        return self._channels.send_channel_message(
            channel_name, sender, text, encrypted=encrypted, iv=iv
        )

    def get_channel_messages(self, channel_name: str, limit: int = 50) -> list[dict]:
        return self._channels.get_channel_messages(channel_name, limit)

    def add_channel_member(self, channel_name: str, username: str) -> dict:
        return self._channels.add_channel_member(channel_name, username)

    def delete_channel(self, channel_name: str, requester: str) -> dict:
        self.telemetry.record("channel.deleted")
        return self._channels.delete_channel(channel_name, requester)

    # ── аватарки -----------------------------------------------------------
    def set_avatar(self, name: str, png_bytes: bytes) -> bool:
        from lib.util import safe_name

        safe = safe_name(name)
        if not safe:
            return False
        # Пустое тело = УДАЛЕНИЕ аватарки (v1.9.6): раньше пустые байты
        # падали на проверке магии PNG — кнопка «Убрать аватар» в клиенте
        # всегда получала 400. Магии достаточно и для установки — PNG-файл
        # без заголовка всё равно не мог быть валидным изображением.
        if not png_bytes:
            return self.delete_avatar(name)
        # Проверяем магию PNG: раньше сюда можно было положить ЛЮБЫЕ байты
        # (вплоть до HTML/скрипта) — они раздавались с Content-Type:
        # image/png, но без nosniff браузер мог догадаться иначе.
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        (self.avatars_dir / f"{safe}.png").write_bytes(png_bytes)
        # (v2.0.5) Телеметрия: факт установки (имя не записываем).
        self.telemetry.record("avatar.set")
        return True

    def delete_avatar(self, name: str) -> bool:
        """Удаляет аватарку (отсутствующий файл = тоже успех — идемпотентно)."""
        from lib.util import safe_name

        safe = safe_name(name)
        if not safe:
            return False
        path = self.avatars_dir / f"{safe}.png"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def get_avatar_bytes(self, name: str) -> bytes | None:
        from lib.util import safe_name

        safe = safe_name(name)
        if not safe:
            return None
        path = self.avatars_dir / f"{safe}.png"
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                return None
        return None

    # ── профили пользователей (bio / status / display_name / friends) ─────
    # Делегирует в ProfileManager — логика живёт в lib/profile_manager.py.
    def _get_profile_manager(self):
        if self._profile_mgr is None:
            from lib.profile_manager import ProfileManager

            self._profile_mgr = ProfileManager(self.profiles_dir)
        return self._profile_mgr

    def get_profile(self, name: str) -> dict:
        return self._get_profile_manager().get_profile(name)

    def save_profile(self, name: str, **fields) -> bool:
        return self._get_profile_manager().save_profile(name, **fields)

    def add_friend(self, name: str, friend_name: str) -> bool:
        return self._get_profile_manager().add_friend(name, friend_name)

    def remove_friend(self, name: str, friend_name: str) -> bool:
        return self._get_profile_manager().remove_friend(name, friend_name)

    def get_friends(self, name: str) -> list[str]:
        return self._get_profile_manager().get_friends(name)

    # ── события чата (делегирование в домен) ──────────────────────────────
    def get_crypto_salt(self) -> str:
        """Per-installation соль для вывода AES-ключа (GET /crypto/info).
        Соль — не секрет (параметр PBKDF2), секрет — сам ключ шифрования."""
        return self._crypto_salt

    def add_text(self, sender: str, text: str, encrypted: bool = False, iv: str = "") -> dict:
        ev = self._store.add_text(sender, text, encrypted=encrypted, iv=iv)
        # (v2.0.5) Телеметрия: только факт и флаг шифрования, сам текст
        # сюда не передаётся (см. докстринг lib/telemetry.py).
        self.telemetry.record("chat.text")
        if encrypted:
            self.telemetry.record("chat.text.encrypted")

        # Превью ссылки тянем в фоне (application-политика, не домен):
        # раньше это делалось синхронно прямо тут, и /send_text у отправителя
        # мог зависать на секунды (таймаут запроса к чужому сайту), пока
        # остальные ждали. Ответ на send_text уходит сразу, а превью (если
        # получится его достать) приезжает отдельным событием чуть позже -
        # клиент подхватит его на следующем обновлении ленты.
        url = find_first_url(text)
        if url:
            threading.Thread(
                target=self._fetch_and_attach_preview, args=(ev["seq"], url), daemon=True
            ).start()

        return ev

    def _fetch_and_attach_preview(self, target_seq: int, url: str) -> None:
        preview = fetch_link_preview(url)
        if not preview:
            return
        self._store.attach_link_preview(target_seq, preview, self.host_name)

    # ── каталоги файлов (транспорт стримит тела запросов сюда) ───────────
    @property
    def files_dir(self) -> Path:
        return self._store.files_dir

    @property
    def dm_files_dir(self) -> Path:
        return self._store.dm_files_dir

    def add_file(self, sender: str, filename: str, data: bytes,
                 sha256: str = "") -> dict:
        """Регистрация файла из памяти (мелкие файлы/тесты). Для больших
        используй add_file_from_path - транспорт стримит тело в temp-файл."""
        ev = self._store.add_file(sender, filename, data, sha256=sha256)
        self._note_file_upload(ev)
        return ev

    def add_file_from_path(self, sender: str, filename: str, source: Path,
                           sha256: str = "") -> dict:
        """Регистрация файла из temp-файла на диске (стриминг, без RAM)."""
        ev = self._store.add_file_from_path(sender, filename, source, sha256=sha256)
        self._note_file_upload(ev)
        return ev

    def _note_file_upload(self, ev: dict) -> None:
        """(v2.0.5) Телеметрия загрузки файла: факт + суммарные байты.
        Имя файла и его содержимое в телеметрию не попадают — только
        размер (число). ev мог прийти без поля size (старые пути) —
        тогда учитываем только факт."""
        self.telemetry.record("file.uploaded")
        try:
            size = int(ev.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            self.telemetry.record("file.uploaded_bytes", size)

    def register_file_holder(self, file_id: str, user: str) -> list[str] | None:
        """Отметить пользователя как владельца файла (скачал - может раздавать).
        None - файл неизвестен."""
        return self._store.register_file_holder(file_id, user)

    def edit_text(self, sender: str, target_seq: int, new_text: str,
                  encrypted: bool = False, iv: str = "") -> dict | None:
        # (v2.0.5) Телеметрия: только факт правки.
        self.telemetry.record("chat.edited")
        return self._store.edit_text(sender, target_seq, new_text,
                                     encrypted=encrypted, iv=iv)

    def add_reaction(self, sender: str, target_seq: int, emoji: str) -> dict | None:
        self.telemetry.record("chat.reactions")
        return self._store.add_reaction(sender, target_seq, emoji)

    def pin_message(self, sender: str, target_seq: int) -> dict | None:
        self.telemetry.record("chat.pins")
        return self._store.pin_message(sender, target_seq)

    def delete_event(self, sender: str, target_seq: int) -> dict | None:
        self.telemetry.record("chat.deleted")
        return self._store.delete_event(sender, target_seq)

    def events_since(self, since: int, tail: int | None = None) -> list[dict]:
        return self._store.events_since(since, tail)

    def events_before(self, before_seq: int, count: int = 50) -> list[dict]:
        return self._store.events_before(before_seq, count)

    def get_pinned_messages(self) -> list[dict]:
        return self._store.get_pinned_messages()

    def search_history(self, query: str, limit: int = 50,
                       author: str = "") -> dict:
        """Поиск по всей истории (v3.8.0): общий чат + все каналы одной
        выборкой, отсортированной по глобальному seq. Зашифрованные
        сообщения не ищутся (E2E) - это отражено в ответах обоих сторов."""
        limit = max(1, min(int(limit), 100))
        store_hits = self._store.search_events(query, limit=limit, author=author)
        chan_hits = self._channels.search_messages(query, limit=limit,
                                                   author=author)
        merged = sorted(store_hits + chan_hits, key=lambda e: e.get("seq", 0))
        return {"results": merged[-limit:],
                "total": len(store_hits) + len(chan_hits)}

    def get_file_event(self, file_id: str) -> dict | None:
        return self._store.get_file_event(file_id)

    def list_all_files(self) -> list[dict]:
        return self._store.list_all_files()

    def file_bytes_path(self, file_id: str) -> Path:
        return self._store.file_bytes_path(file_id)

    # ── присутствие (делегирование в PresenceBoard) ───────────────────────
    def touch_presence(self, name: str) -> None:
        self._presence.touch(name)

    def online_names(self) -> list[str]:
        return self._presence.online_names()

    def set_typing(self, name: str) -> None:
        """Отмечает, что name сейчас печатает - эфемерно, не пишется в
        history.jsonl (нет смысла хранить это вечно)."""
        self._presence.set_typing(name)

    def typing_names(self, exclude: str = "") -> list[str]:
        return self._presence.typing_names(exclude=exclude)

    # ── агрегированные юзкейсы для транспорта ─────────────────────────────
    def note_request(self, elapsed_ms: float, path: str = "",
                     status: int = 0) -> None:
        """(v2.0.4) Транспорт отмечает завершение каждого запроса (мс) —
        исключая долгоживущие соединения вроде WS-туннеля голоса, чтобы
        они не портили p95 ложными «минутными латентностями». Вызывается
        из безопасных wrapper'ов do_GET/do_POST; ошибки замера не должны
        влиять на обработку — LatencyMeter.note молча игнорирует мусор.
        (v2.0.5) Телеметрия: маршрут маскируется через route_key (без
        имён/id), статус и латентность — числами. path/status необязательны
        для совместимости со старыми вызовами."""
        self._req_stats.note(elapsed_ms)
        if path:
            self.telemetry.record("http.route." +
                                  Telemetry.route_key(path))
            if status:
                self.telemetry.record(f"http.status.{int(status)}")
        self.telemetry.observe("http.latency_ms", elapsed_ms)

    def allow_message(self, sender: str) -> bool:
        """(v2.0.4) Антифлуд: разрешён ли этому отправителю ещё один чат-
        текст (/send_text, /dm/send). Человек свои 30 мгновенных сообщений
        никогда не исчерпает (см. FLOOD_MSG_*), скрипт упирается в 429.
        Пустое имя — тоже честный ключ «?»: безымянный спам не должен
        делить ведро с «Васей» (иначе Вася платит за безымянного)."""
        return self._flood.allow(sender or "?")

    def flood_retry_after(self, sender: str) -> float:
        """(v2.0.4) Сколько секунд этому отправителю ждать до разрешения
        (уходит в поле retry_after ответа 429 — клиент покажет честный
        таймер вместо «сервер сломался")."""
        return self._flood.retry_after(sender or "?")

    def get_server_stats(self) -> dict:
        """(v2.0.2) Служебная сводка хоста для GET /server/stats — «жив ли
        сервер и что у него под капотом». Только счётчики, никаких имён и
        текстов (эндпоинт за access_key, как и весь API, но лишний раз
        контент наружу не выносим). Потребитель — хост в браузере/curl:
        uptime, размер кеша событий, сессии, файлы, каналы, голос/voxel.
        Каждый источник ошибок ловится отдельно: статистика НЕ должна
        падать из-за, например, отсутствующего ChessBot."""
        uptime = 0
        if self._started_at and self.is_running():
            uptime = int(time.monotonic() - self._started_at)
        stats = {
            "ok": True,
            "name": self.host_name,
            "protocol": PROTOCOL_VERSION,
            "running": self.is_running(),
            "uptime_s": uptime,
            "uptime_human": fmt_uptime(uptime),  # (v2.0.4) «3д 2ч 5м»
            "online": len(self.online_names()),
            "sessions": self._sessions.count(),
            "voice": self._voice is not None,
            "voxel": self._voxel is not None,
            "screen": self.screen_stats(),  # (v3.6.0) счётчики хаба «Экран»
            "json_backend": json_fast.BACKEND,
        }
        try:
            stats["channels"] = len(self.get_channels())
        except Exception:
            stats["channels"] = 0
        try:
            stats.update(self._store.stats())
        except Exception:
            pass  # журнал мог быть уже закрыт (stop()) — счётчики не критичны
        # (v2.0.4) Как дышится: счётчик запросов, средняя и p95 латентность
        # (окно 256), и сколько антифлуд-вёдер сейчас живёт. Каждый блок
        # отдельно под try — статистика не должна падать из-за мелочей.
        try:
            stats.update(self._req_stats.snapshot())
        except Exception:
            pass
        try:
            stats["flood_buckets"] = self._flood.active()
        except Exception:
            pass
        return stats

    def get_telemetry_summary(self) -> dict:
        """(v2.0.5) Снимок телеметрии для GET /telemetry/summary. Ошибки
        не должны ломать эндпоинт — при проблеме честный минимальный ответ."""
        try:
            return self.telemetry.snapshot()
        except Exception:
            return {"ok": False, "error": "телеметрия недоступна"}

    def set_telemetry_enabled(self, on: bool) -> bool:
        """(v2.0.5) Включить/выключить телеметрию (POST /telemetry/enable
        и /telemetry/disable). Возвращает итоговое состояние."""
        self.telemetry.set_enabled(bool(on))
        return self.telemetry.is_enabled()

    def get_leaderboard(self, limit: int = 50) -> list[dict]:
        """Топ игроков по ELO из ChessBot. Ошибки/отсутствие бота = пустой
        список (транспорт не должен знать внутренности ботов)."""
        try:
            chess_bot = self.get_bot_manager().get_bot("chess")
            if chess_bot is None:
                return []
            from lib.bots.chess import ChessBot

            if isinstance(chess_bot, ChessBot):
                return chess_bot._top_players(limit)
        except Exception:
            pass
        return []

    def get_voice_info(self) -> dict:
        """Инфо для GET /voice/info (порт голосового TCP-сервера = HTTP+1).
        Браузерная голосовая инфраструктура живёт на ОСНОВНОМ порту: https
        (TLS-мультиплексор) и WS-туннель /voice/ws. https_port теперь равен
        базовому порту (0, если TLS не поднялся — нет cryptography).
        wss_port оставлен для совместимости старых веб-клиентов — всегда 0."""
        voice_port = self.get_voice_port() if self._voice else 0
        return {
            "ok": True,
            "enabled": voice_port > 0,
            "port": voice_port,
            "host": self.host_name,  # клиенты подставят реальный host из base_url
            "https_port": self._https_port,
            "wss_port": 0,
            "voice_ws_path": "/voice/ws" if self._voice_bridge else "",
        }

    def get_voice_ws_port(self) -> int:
        """Порт WS-моста для голоса (если есть)."""
        if self._voice_bridge:
            return self._voice_bridge.bound_port
        return 0

    def get_music_sync_state(self) -> dict | None:
        """(v3.3.0) Легаси: состояние только когда что-то играет.
        Новые маршруты используют music_state() — он отдаёт состояние всегда."""
        music_bot = self._music_bot()
        if music_bot and hasattr(music_bot, "get_sync_state"):
            return music_bot.get_sync_state()
        return None

    def get_music_library(self) -> list[dict]:
        """Возвращает библиотеку доступных треков."""
        music_bot = self._music_bot()
        if music_bot and hasattr(music_bot, "get_library"):
            return music_bot.get_library()
        return []

    # ── музыка v3.3.0: фасад-юзкейсы для веб-плеера ──────────────────────
    # Методы возвращают dict бота {ok, ...} как есть; None = бот не запущен
    # (транспорт переводит это в 503). sender ПРИХОДИТ ПРОВЕРЕННЫМ из
    # authenticate() — права open_dj/admins считает сам MusicBot.

    def _music_bot(self):
        """MusicBot из менеджера ботов (или None, если его нет)."""
        try:
            bot_manager = self.get_bot_manager()
            music_bot = bot_manager._bots.get("music")
            if music_bot and hasattr(music_bot, "api_state"):
                return music_bot
        except Exception:
            pass
        return None

    def music_state(self) -> dict | None:
        """Полное состояние плеера для GET /music/sync (всегда не None,
        если бот зарегистрирован)."""
        music_bot = self._music_bot()
        return music_bot.api_state() if music_bot else None

    def music_play(self, sender: str, track_id: str = "",
                   position: float | None = None) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_play(sender, track_id or None, position) \
            if music_bot else None

    def music_pause(self, sender: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_pause(sender) if music_bot else None

    def music_skip(self, sender: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_skip(sender) if music_bot else None

    def music_seek(self, sender: str, position: float) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_seek(sender, position) if music_bot else None

    def music_queue_add(self, sender: str, track_id: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_queue_add(sender, track_id) if music_bot else None

    def music_queue_remove(self, sender: str, index: int) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_queue_remove(sender, index) if music_bot else None

    def music_stream_path(self, track_id: str) -> dict | None:
        """(v3.3.0) Путь к файлу трека для GET /music/stream/<id>.

        Путь НЕ попадает в публичную библиотеку (get_music_library отдаёт
        треки без локальных путей) — стрим-роут резолвит файл только через
        этот метод. None = бота нет/трек не найден."""
        music_bot = self._music_bot()
        if not music_bot:
            return None
        track = music_bot._track(track_id)  # внутренний, тот же app-слой
        if not track:
            return None
        return {"path": track["path"], "filename": track["filename"]}

    def music_ended(self, sender: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_ended(sender) if music_bot else None

    def music_rescan(self, sender: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_rescan(sender) if music_bot else None

    def music_settings(self, sender: str, music_dir: str | None = None,
                       open_dj: bool | None = None) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_settings(sender, music_dir=music_dir,
                                      open_dj=open_dj) if music_bot else None

    def music_search(self, sender: str, query: str,
                     provider: str = "") -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_search(sender, query, provider) \
            if music_bot else None

    def music_import(self, sender: str, provider: str,
                     track_id: str) -> dict | None:
        music_bot = self._music_bot()
        return music_bot.api_import(sender, provider, track_id) \
            if music_bot else None

    def get_voxel_sessions(self) -> list:
        return self._voxel.list_sessions() if self._voxel else []

    # Ниже методы возвращают None, когда voxel-сервер не поднят (транспорт
    # переводит это в 503), bool/int - результат операции:
    def voxel_create_session(
        self,
        sid: str,
        game_mode: str = "pve",
        wall_hp: int = 0,
        team_red_size: int = 5,
        team_blue_size: int = 5,
    ) -> bool | None:
        if self._voxel is None:
            return None
        return self._voxel.create_session(
            sid,
            game_mode=game_mode,
            wall_hp=wall_hp,
            team_red_size=team_red_size,
            team_blue_size=team_blue_size,
        )

    def voxel_start_game(self, sid: str) -> bool | None:
        if self._voxel is None:
            return None
        return self._voxel.start_game(sid)

    def voxel_fill_bots(self, sid: str) -> int | None:
        if self._voxel is None:
            return None
        return self._voxel.fill_bots(sid)

    def voxel_add_bot(self, sid: str, team: str = "none") -> bool | None:
        if self._voxel is None:
            return None
        return self._voxel.add_bot_to_session(sid, team=team)

    # ── HTTP-сервер (транспорт подключается здесь) ────────────────────────
    def start(self, host: str, port: int) -> bool:
        if self._httpd is not None:
            return True

        # Транспорт - отдельный слой (lib/server/http_api.py): фасад не
        # знает про BaseHTTPRequestHandler, хендлер - про EventStore.
        from lib.server.http_api import make_handler_class

        # (v3.4.1) Диагностика ZeroTier + VPN конфликта и бинда.
        # Делаем ДО открытия сокета — иначе в журнале нет подсказки почему
        # «друзья не заходят по веб-версии».
        try:
            from lib.net_utils import detect_vpn_zt_conflict, get_interfaces
            _ifs = get_interfaces()
            _has_overlay = any(
                i.kind in ("zerotier", "tailscale") for i in _ifs
            )
            # Предупреждение 1: конфликт VPN+ZT — на входящие не пройти
            # (v3.4.2 fix): передаём уже полученные интерфейсы — раньше
            # detect_vpn_zt_conflict звал get_interfaces() второй раз подряд.
            conflict, tip = detect_vpn_zt_conflict(_ifs)
            if conflict:
                get_logger().warning(
                    "ZeroTier/Tailscale + VPN одновременно. На входящие обычно "
                    "не зайти — диагностика: python scripts/fix_vpn_zt.py "
                    "diagnose (без админа), фикс: scripts/fix_zerotier_vpn.bat "
                    "от админа или кнопка «Починить VPN+ZT» в server_gui "
                    "(безопасно, до перезагрузки). %s", tip)
            # Предупреждение 2: есть ZT, но bind не 0.0.0.0/[ZT-IP]
            if _has_overlay and host not in ("0.0.0.0", "::"):
                _zt_ips = {i.ip for i in _ifs if i.kind in ("zerotier", "tailscale")}
                if host not in _zt_ips:
                    get_logger().warning(
                        "Bind на %s, но в системе есть ZeroTier/Tailscale — "
                        "друзья по overlay-сети %s НЕ СМОГУТ зайти! "
                        "Рекомендация: поставить bind 0.0.0.0 (слушать все интерфейсы).",
                        host, ", ".join(sorted(_zt_ips)) or "?")
        except Exception:
            pass  # диагностика не должна ломать старт сервера

        handler = make_handler_class(self)
        # Базовый порт запоминаем: QueuedHTTPServer не имеет собственного
        # адреса (слушатель принадлежит TLS-мультиплексору)
        self._base_port = port
        # (v2.0.2) Метка старта для uptime в /server/stats
        self._started_at = time.monotonic()
        # Основной путь: TLS-мультиплексор (http и https на одном порту).
        # Если cryptography недоступна — обычный plain-сервер, как раньше.
        if not self._start_tls_mux(host, port, handler):
            try:
                self._httpd = ThreadingHTTPServer((host, port), handler)
            except OSError:
                self._httpd = None
                return False
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        # Поднимаем голосовой TCP-сервер на port+1
        from voice.mixer import VoiceMixer

        self._voice = VoiceMixer(host=host, port=port + 1, access_key=self.access_key)
        # (v2.0.5) Телеметрия голоса: факты входа/выхода (без имён —
        # колбэки без параметров, см. voice/mixer.py).
        self._voice.on_join = lambda: self.telemetry.record("voice.join")
        self._voice.on_leave = lambda: self.telemetry.record("voice.leave")
        if not self._voice.start():
            # Не критично — чат работает и без голосового
            self._voice = None
            get_logger().warning("голосовой сервер не поднялся (порт %s) — чат работает без голоса", port + 1)

        # Поднимаем Voxel Shooter TCP-сервер на port+2
        from lib.voxel_server import VoxelSessionServer

        self._voxel = VoxelSessionServer(host=host, port=port + 2, access_key=self.access_key)
        if not self._voxel.start():
            # Не критично — остальные функции работают
            self._voxel = None
            get_logger().warning("voxel-сервер не поднялся (порт %s) — игры недоступны", port + 2)

        # WS-мост голоса браузер↔микшер — внутри основного порта (/voice/ws).
        # Не критично: без него чат/файлы/шахматы работают как раньше.
        self._start_voice_bridge()

        # (v2.0.5) Флушер телеметрии: раз в 15с пишет snapshot+дельты на
        # диск. Запуск после всех серверов — до этого телеметрия честно
        # копится в памяти с самого __init__.
        self.telemetry.start_flusher()

        # (v3.4.0) Факт старта — в журнал (файл + GUI).
        get_logger().info(
            "сервер запущен: порт %s · голос: %s · voxel: %s · хост: %s",
            port, port + 1, port + 2, self.host_name,
        )
        return True

    def _start_tls_mux(self, host: str, port: int, handler) -> bool:
        """http И https на одном порту (см. lib/server/tls_mux.py).

        Зачем TLS: браузер выдаёт микрофон (getUserMedia) и crypto.subtle
        только в secure context. Сертификат самоподписанный, генерируется
        один раз в data_dir (lib/server/tls_util.py) — браузер один раз
        покажет предупреждение («Дополнительно → Перейти на сайт»), после
        чего работает и страница, и голос (в т.ч. WS-туннель /voice/ws —
        сертификат уже принят для этого origin).

        True — мультиплексор поднят (self._httpd = QueuedHTTPServer);
        False — TLS недоступен/порт не взят: вызывающий откатится на
        обычный plain ThreadingHTTPServer.
        """
        from lib.server.tls_util import ensure_tls_files, make_server_ssl_context

        pair = ensure_tls_files(self.data_dir)
        if pair is None:
            return False  # нет cryptography — уже залогировано в tls_util
        ctx = make_server_ssl_context(*pair)
        if ctx is None:
            return False

        from lib.server.http_api import QueuedHTTPServer
        from lib.server.tls_mux import TlsPlainMux

        mux = TlsPlainMux(host, port, ctx)
        if not mux.start():
            return False
        self._httpd = QueuedHTTPServer(handler, mux.queue)
        self._mux = mux
        self._https_port = port  # https теперь на том же порту, что и http
        logging.getLogger("friend_relay").info(
            "Веб-клиент: http И https на одном порту %s "
            "(голос: wss://%s:%s/voice/ws)", port, host, port)
        return True

    def _start_voice_bridge(self) -> None:
        """WS-мост браузер↔микшер ТОЛЬКО на 127.0.0.1 с ephemeral-портом.

        Наружу он не торчит: TLS терминирует основной HTTP-сервер, который
        туннелирует /voice/ws на этот loopback-порт. Браузеру достаточно
        одного адреса https://IP:8420 — никаких дополнительных портов."""
        if self._voice is None or self._httpd is None:
            return
        mixer_port = self.get_voice_port()
        if not mixer_port:
            return
        try:
            from voice.bridge import VoiceBridge

            bridge = VoiceBridge(host="127.0.0.1", port=0,
                                 mixer_port=mixer_port,
                                 access_key=self.access_key,
                                 ssl_context=None)
            if bridge.start():
                self._voice_bridge = bridge
        except Exception as e:  # мост не критичен для остального
            logging.getLogger("friend_relay").warning(
                "Голосовой мост не поднят: %s", e)

    def stop(self) -> None:
        # (v3.4.0) Факт остановки — в журнал (до закрытия хранилища).
        get_logger().info("сервер остановлен")
        # (v2.0.5) Телеметрию останавливаем ПЕРВЫЙ: финальный флуш должен
        # застать все события этого запуска (запросы/файлы до остановки).
        try:
            self.telemetry.stop_flusher()
        except Exception:
            pass
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None
        # TLS-мультиплексор (слушающий сокет) и WS-мост голоса
        if self._mux is not None:
            self._mux.stop()
            self._mux = None
        if self._voice_bridge is not None:
            self._voice_bridge.stop()
            self._voice_bridge = None
        self._https_port = 0
        self._https_thread = None
        # Голосовой сервер опускаем вместе с HTTP
        if self._voice is not None:
            self._voice.stop()
            self._voice = None
        # Voxel Shooter TCP-сервер
        if self._voxel is not None:
            self._voxel.stop()
            self._voxel = None
        # (v3.4.2 fix) Останавливаем ботов: их set_data теперь пишет на диск
        # отложенно (debounce до 1 c), без флаша при остановке последние
        # действия (play/pause/очередь/очки игр) терялись бы при выходе.
        try:
            self.get_bot_manager().shutdown()
        except Exception:
            pass
        self.close()

    def is_running(self) -> bool:
        return self._httpd is not None

    def close(self) -> None:
        """Закрывает журнал событий (файловый дескриптор history.jsonl)."""
        self._store.close()

    # ── голосовой канал ───────────────────────────────────────────────────
    def get_voice_port(self) -> int:
        """Порт голосового TCP-сервера (на 1 больше, чем HTTP)."""
        if self._httpd is None:
            return 0
        return self._base_port + 1

    def get_voice_participants(self) -> list[str]:
        """Имена клиентов в голосовом канале прямо сейчас."""
        if self._voice is None:
            return []
        return self._voice.participants()

    # ── демонстрация экрана (v3.6.0, модуль screen/) ─────────────────────
    def get_screen_info(self) -> dict:
        """Инфо для GET /screen/info: кто сейчас показывает экран.
        Медиа не через сервер (WebRTC P2P), поэтому кроме списка — пусто."""
        return {"ok": True, "streams": self._screen.snapshot()}

    def screen_publish(self, sender: str, on: bool, title: str = "") -> dict:
        """Ведущий: объявить показ (on=True; повторно — heartbeat живости)
        или закончить (on=False). Начало/конец — строкой в общий чат,
        чтобы друзья видели без обновления вкладки «Экран»."""
        if on:
            if self._screen.publish(sender, title):
                try:
                    self._store.add_text(
                        "🖥 экран",
                        f"🖥 {sender} начал показ экрана — вкладка «Экран»",
                    )
                except Exception:
                    pass  # журнал чата — не причина ломать показ
        else:
            if self._screen.unpublish(sender):
                try:
                    self._store.add_text(
                        "🖥 экран", f"🖥 {sender} закончил показ экрана")
                except Exception:
                    pass
        return {"ok": True}

    def screen_signal(self, frm: str, to: str, data: dict) -> dict:
        """Релей одного сигнала WebRTC (hello/offer/answer/bye) адресату.
        Отправитель уже аутентифицирован HTTP-слоем."""
        seq = self._screen.send_signal(frm, to, data)
        if seq is None:
            return {"ok": False, "error": "сигнал отброшен (лимит размера)"}
        return {"ok": True, "seq": seq}

    def screen_poll(self, who: str, since: int) -> dict:
        """Забрать новые сигналы адресата (курсор since — с клиента)."""
        msgs, last = self._screen.poll(who, since)
        return {"ok": True, "signals": msgs, "since": last}

    def screen_watch(self, viewer: str, sharer: str, on: bool) -> dict:
        """Зритель сообщил о фактическом подключении/уходе — для счётчика."""
        ok = self._screen.watch(viewer, sharer, on)
        return {"ok": True, "registered": ok}

    def screen_stats(self) -> dict:
        """Счётчики хаба для /server/stats."""
        return self._screen.stats()
