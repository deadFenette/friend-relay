"""HTTP-транспорт Friend Relay.

Отвечает на вопрос "КАК показать/отдать наружу": маршрутизация URL, разбор
запросов, коды ответов. НИКАКОЙ доменной логики здесь нет - каждый хендлер
валидирует форму запроса и делегирует фасаду RelayServer (application-слой),
который в свою очередь работает с доменом (EventStore) и сервисами.

Раньше весь этот транспорт жил внутри RelayServer.start() - god-метода на
1104 строки, где роутинг, валидация и домен были сплавлены в одну кучу.
Теперь:
  - каждый маршрут = маленький метод _get_* / _post_* (один юз-кейс);
  - диспетчеризация - по таблицам GET_ROUTES / GET_PREFIX_ROUTES / POST_ROUTES;
  - RelayServer вообще не знает про HTTP (проверка: relay_server.py не
    импортирует http.server).

Контракт поведения сохранён 1:1 со старым монолитом: те же коды ответов,
те же тексты ошибок, тот же порядок проверок (/ping открыт до access_key,
потом ключ, потом маршруты).

Range-запросы (Range/206) в /download и /dm_download - аддитивная фича
"умной раздачи v1": старые клиенты (без Range) получают файл целиком как
раньше, новые могут качать кусками параллельно."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import queue
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlparse

from lib.constants import (
    EVENT_KIND_TEXT,  # noqa: F401 - переиспользуется в доктестах формата
    HEADER_FILENAME,
    HEADER_FROM,
    HEADER_KEY,
    HEADER_SHA256,
    HEADER_TO,
    MAX_AVATAR_UPLOAD_SIZE,
    PROTOCOL_VERSION,
)
from lib.json_fast import dumps as json_dumps
from lib.json_fast import loads as json_loads
from lib.util import MtimeFileCache, header_decode, parse_int

if TYPE_CHECKING:  # только для аннотаций - циклический импорт не нужен
    from lib.relay_server import RelayServer

# Лимит JSON-тел (всё, что читается _read_body): раньше Content-Length:
# 1000000000 на /send_text читался ЦЕЛИКОМ в RAM потока, а ThreadingHTTP
# Server плодит поток на соединение — N параллельных запросов = N×RAM.
# Отрицательный Content-Length вообще вешал поток вечно (read(-5) = до EOF).
MAX_JSON_BODY = 2 * 1024 * 1024  # 2 МБ с запасом

# (v2.0.2) Кеш статики веб-клиента (/, /static/*.js|css): читаем с диска
# только когда изменился файл (mtime+size), иначе отдаём из RAM. Экземпляр
# на классе — статика общая для всех соединений; ключ (mtime, size) честно
# подхватывает правки без рестарта (прежнее поведение «файл читаем на
# каждый запрос» сохранено семантически, минус дисковый I/O).
_STATIC_CACHE = MtimeFileCache(max_entries=64)

# (v2.0.5) Реестр веб-игр («активити» как в Discord) — белой список:
# id → файл в web_client/games/. Ровно этот список отдаёт GET /games
# (лаунчер вкладки «Игры»), и ТОЛЬКО эти файлы отдаёт GET /games/<id> —
# путь строится по реестру, а не из запроса, поэтому path traversal
# и подмена расширения невозможны по построению (сильнее, чем фильтры).
# Новая игра = положить <файл>.html в web_client/games/ и добавить строку.
# mode: "multi" — матч судит сервер (бот), "single" — игра целиком в браузере.
_GAMES_DIR = Path(__file__).resolve().parent.parent.parent / "web_client" / "games"
_GAMES: dict[str, dict] = {
    # (v2.0.6) Го — ПЕРВАЯ игра реестра (порядок dict = порядок в лаунчере).
    # Правила целиком в браузере (go.html несёт собственный движок на JS),
    # а с v2.0.7 ещё и сетевые матчи: судья — GoBot, ходы через /bot_command.
    "go": {
        "file": "go.html", "emoji": "⚫", "title": "Го",
        "desc": "Древняя игра в окружение: доски 9/13/19, взятия, ко, "
                "подсчёт по площади. ИИ, hot-seat и онлайн-матчи (судья — "
                "серверный бот, как в шахматах).",
        # (v2.0.7) multi: в вебе появился сетевой матч через GoBot
        "mode": "multi",
    },
    "g2048": {
        "file": "2048.html", "emoji": "🔢", "title": "2048",
        "desc": "Свайпай/стрелки, складывай плитки до 2048. Рекорд — в браузере.",
        "mode": "single",
    },
    "minesweeper": {
        "file": "minesweeper.html", "emoji": "💣", "title": "Сапёр",
        "desc": "Классический сапёр: три размера поля, флаги — правой кнопкой.",
        "mode": "single",
    },
}

# Маршруты со стриминговым телом (файлы/аватарки): тело НЕ читается в RAM,
# подпись X-Relay-Auth проверяется инкрементально по кускам (см. relay.
# begin_stream_auth) — читать их дважды (для подписи и для диска) нельзя.
STREAM_BODY_ROUTES = frozenset({"/send_file", "/send_dm_file", "/avatar"})

# GET-маршруты с приватными данными: подпись X-Relay-Auth (path+query,
# как в lib/auth.py auth_header_for_get) обязательна при включённом ключе.
# Иначе любой, знающий ключ, читал чужую переписку GET /dm/history с
# подделанным X-Relay-From. Остальные GET (события, витрина, профили)
# публичны в пределах ключа — их не трогаем.
AUTH_REQUIRED_GET = frozenset({"/dm/history", "/dm/conversations", "/screen/poll"})

# Серверный журнал: неожиданные исключения в хендлерах раньше исчезали
# без следа (клиент получал 500, хост не видел НИЧЕГО) — диагностировать
# проблему на чужой машине было невозможно.
log = logging.getLogger("friend_relay.http")


class RelayHTTPHandler(BaseHTTPRequestHandler):
    """Базовый хендлер. Экземпляр привязывается к серверу через
    make_handler_class(relay) - подкласс с выставленным relay, чтобы два
    сервера в одном процессе не делили состояние."""

    protocol_version = "HTTP/1.1"
    relay: RelayServer = None  # type: ignore[assignment]

    # v3.7.0 ФИКС «ПРЕРЫВИСТОГО ЗВУКА» (даунлинк голоса): TCP_NODELAY на
    # сокете КЛИЕНТА. Голосовой WS-туннель /voice/ws гонит 20мс-кадры по
    # ~2КБ; с включённым Nagle (умолчание http.server) мелкий сегмент
    # ЗАДЕРЖИВАЕТСЯ в ядре до ACK предыдущего (RTT ZeroTier 30-150мс +
    # delayed ack) — ровный поток 50 кадров/с превращается во всплески,
    # джиттер-буфер браузера сохнет, звук «прерывистый, как старый скайп».
    # Ровно тот же фикс микшер давно сделал для своих TCP-клиентов
    # (см. mixer._register_client), но веб-тракт через HTTP-сервер
    # пропустили. SSLSocket.setsockopt проксирует на сырой сокет, поэтому
    # работает и для TLS-мультиплексора (tls_mux оборачивает сокет до
    # передачи хендлеру), и для plain-подключений.
    disable_nagle_algorithm = True

    # Таймаут на сокет (BaseHTTPRequestHandler по умолчанию None): без него
    # idle keep-alive соединение и «по байту в секунду» (slowloris) держали
    # поток в rfile.readline() навсегда — потоков сока не было. 30 секунд
    # достаточно для нормальных клиентов (поллинг ходит каждые 1.5-2с):
    # BaseHTTPRequestHandler переводит socket.timeout в close_connection.
    timeout = 30

    # Проверенный отправитель JSON-POST (заполняется _do_POST_impl ДО
    # вызова хендлера маршрута) и сырое тело (уже прочитано для проверки
    # подписи — хендлеры не читают его повторно). Обнуляются на каждый
    # запрос: экземпляр хендлера живёт на keep-alive соединении.
    _auth_sender: str = ""
    _body_raw: bytes = b""
    _last_status: int = 0   # (v2.0.5) код последнего ответа — для телеметрии

    def log_message(self, *a):
        pass  # свой лог наружу не пишем, шумно

    def send_response(self, code, message=None):
        # (v2.0.5) Запоминаем код ответа для телеметрии: wrapper do_GET/
        # do_POST в finally отдаёт его вместе с латентностью. Это ЕДИНСТВЕННОЕ
        # место, где видно, чем кончился запрос (200/403/429/500…).
        self._last_status = code
        super().send_response(code, message)

    # ── helpers ───────────────────────────────────────────────────────────
    def _check_key(self) -> bool:
        if not self.relay.access_key:
            return True
        # header_decode для ключей с не-ASCII (кириллица: клиент кодирует
        # через header_encode; ASCII-ключи decode проходит насквозь).
        # compare_digest — постоянное время сравнения (timing-атаки).
        # Сравниваем БАЙТЫ: compare_digest на str принимает только ASCII,
        # и кириллический ключ доступа ронял КАЖДЫЙ запрос TypeError'ом
        # (500 «internal error») вместо честной проверки.
        import hmac as _hmac

        got = header_decode(self.headers.get(HEADER_KEY, ""))
        return _hmac.compare_digest(
            got.encode("utf-8"), self.relay.access_key.encode("utf-8"))

    @staticmethod
    def _header_int(value: str | None, default: int = 0) -> int | None:
        """Content-Length и подобные числовые заголовки без сюрпризов:
        None — мусор (вызывающий отвечает 400), иначе int >= default.
        (v2.0.2) Семантика переехала в lib/util.parse_int — она нужна и
        домену; здесь тонкий делегат, чтобы не ломать вызовы."""
        return parse_int(value, default)

    @staticmethod
    def _int_arg(value, default: int = 0) -> int | None:
        """Числовое поле JSON-тела: None — не число (вызывающий шлёт 400).
        (v2.0.2) Тот же parse_int из lib/util — одна точка правды."""
        return parse_int(value, default)

    def _send_json(self, status: int, payload: dict) -> None:
        # (v2.0.2) json_fast: orjson (если есть) даёт готовые байты без
        # промежуточной строки — это самый частый вызов транспорта.
        # Формат тот же: компактный UTF-8 без ASCII-эскейпов.
        data = json_dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sender_from_header(self, default: str = "") -> str:
        return header_decode(self.headers.get(HEADER_FROM, default) or default)

    def _valid_dm_name(self, name: str) -> str | None:
        """Имя участника DM, если оно «чистое» (safe_name(name) == name и не
        пусто), иначе None. Имена с path-разделителями/точками-переходами
        раньше собирались в путь файла диалога КАК ЕСТЬ — теперь и имя
        файла безопасно (safe_name в EventStore), и сам запрос отвергается
        с понятной ошибкой, а не создаёт диалог-мусор «....evil»."""
        from lib.util import safe_name

        s = safe_name(name or "")
        if not s or s != (name or ""):
            return None
        return s

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        # Отрицательный/гигантский Content-Length не читаем вообще (см.
        # MAX_JSON_BODY): раньше read(-1) висел до EOF, а read(10**9) съедал
        # гигабайты RAM. Соединение закрывается — клиент переподключится
        # с нормальным запросом.
        if length <= 0:
            return b""
        if length > MAX_JSON_BODY:
            self.close_connection = True
            return b""
        return self.rfile.read(length)

    @staticmethod
    def _parse_json(raw: bytes) -> dict:
        # (v2.0.2) json_fast.loads: orjson ест bytes без .decode; мусор
        # ловим как ValueError (и json.JSONDecodeError, и
        # orjson.JSONDecodeError, и UnicodeDecodeError — все его наследники).
        try:
            return json_loads(raw) if raw else {}
        except ValueError:
            return {}

    def _parse_seq(self, body: dict) -> int:
        try:
            return int(body.get("seq", 0))
        except (TypeError, ValueError):
            return 0

    def _read_chunked(self, length: int) -> bytes:
        """Читает тело запроса кусками по 256К (файлы могут быть большими).
        Устаревший путь для мелких JSON-тел; для файлов используй
        _stream_body_to_file - он не держит файл в RAM."""
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 256, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _stream_body_to_file(self, length: int, dest_dir: Path,
                              mac=None) -> tuple[Path, str, int] | None:
        """Стримит тело запроса сразу в temp-файл на диске, попутно считая
        sha256 (фундамент torrent-ядра: хеш нужен для верификации, дедупа и
        ETag). Возвращает (path, hexdigest, received) или None, если тело
        оборвалось (temp-файл подчищен). Раньше файл читался списком кусков
        в RAM - 2 ГБ файл съедал 2 ГБ памяти хоста на каждый upload.

        mac (v1.9.5) — опциональный объект hmac: теми же кусками
        инкрементально считается подпись X-Relay-Auth по ПОЛНОМУ телу
        (файлы могут быть гигабайтными — прочитать их в RAM для проверки
        подписи целиком нельзя, поэтому HMAC кормится на лету).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".incoming-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        received = 0
        try:
            with open(tmp, "wb") as f:
                while received < length:
                    chunk = self.rfile.read(min(1024 * 256, length - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    if mac is not None:
                        mac.update(chunk)
                    received += len(chunk)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        if received != length:
            tmp.unlink(missing_ok=True)
            return None
        return tmp, digest.hexdigest(), received

    def _send_file_bytes(self, path: Path, name: str, etag: str = "") -> None:
        """Раздаёт файл с диска. Поддерживает HTTP Range (умная раздача v1):
        старые клиенты без Range получают файл целиком как раньше (200),
        новые могут качать кусками параллельно (206).

        etag - hex sha256 содержимого (если известен): анонсируется в ETag,
        клиент может сверить целостность после скачивания, а If-Range
        защищает от смешения кусков двух ревизий (файл заменён/удалён
        посреди докачки - тогда отдаём целиком, 200, как требует RFC 7233)."""
        size = path.stat().st_size
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        disposition = f"attachment; filename*=UTF-8''{quote(name)}"

        # If-Range: кусок имеет смысл только от той же ревизии файла
        # (кавычки ETag с обеих сторон снимаем - клиент может прислать
        # как сырвой hex, так и в RFC-формате с кавычками)
        rng_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if rng_header and if_range and etag and if_range.strip().strip('"') != etag:
            rng_header = None  # ревизия изменилась - отдаём целиком

        rng = self._parse_range(rng_header, size)
        if rng == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if rng is not None:
            start, end = rng  # включительно
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            if etag:
                self.send_header("ETag", f'"{etag}"')
            self.send_header("Content-Disposition", disposition)
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        if etag:
            self.send_header("ETag", f'"{etag}"')
        self.send_header("Content-Disposition", disposition)
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                self.wfile.write(chunk)

    @staticmethod
    def _parse_range(header: str | None, size: int):
        """Разбирает Range-заголовок.

        Возвращает: None - диапазона нет/не поддерживаемая форма (отдать
        целиком, 200); "invalid" - невыполнимый диапазон (416);
        (start, end) включительно - отрезать кусок (206).
        Несколько диапазонов через запятую не поддерживаем (отдаём целиком) -
        клиент Friend Relay их и не просит."""
        if not header:
            return None
        header = header.strip()
        if not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):].strip()
        if not spec or "," in spec or "-" not in spec:
            return None
        first, last = (part.strip() for part in spec.split("-", 1))
        try:
            if first == "":
                # Суффиксная форма bytes=-N: последние N байт
                if size == 0:
                    return "invalid"
                n = int(last)
                if n <= 0:
                    return "invalid"
                return (max(0, size - n), size - 1)
            start = int(first)
            if start < 0:
                return None
            if start >= size:
                # Для пустого файла любой диапазон невыполним
                return "invalid"
            if last == "":
                return (start, size - 1)
            end = int(last)
            if end < start:
                return None
            return (start, min(end, size - 1))
        except ValueError:
            return None

    # ── HTTP methods (с защитным wrapper'ом - см. багфикс в истории) ─────
    # (v2.0.4) Оба wrapper'а замеряют время обработки и отдают его фасаду
    # (relay.note_request) — /server/stats показывает requests_total/avg_ms/
    # p95_ms. Долгоживущие соединения (WS-туннель голоса) помечаются
    # _skip_meter=True: туннель живёт до отключения клиента, и его
    # «минутные латентности» ломали бы честные p95 обычных запросов.
    def do_GET(self):
        # Тот же безопасный wrapper, что и для do_POST - необработанное
        # исключение всегда долетает до клиента как понятный 500-ответ.
        self._skip_meter = False
        self._last_status = 0
        t0 = time.perf_counter()  # monotonic — перевод часов не врёт
        try:
            self._do_GET_impl()
        except Exception as e:
            # (v2.0.5) Телеметрия падений: тип исключения без стека/пути —
            # хост видит «сколько и чем» болит сервер.
            self.relay.telemetry.record(
                "http.errors." + type(e).__name__)
            log.warning("GET %s упал: %s", self.path, e, exc_info=True)
            try:
                self._send_json(500, {"ok": False, "error": f"internal error: {e}"})
            except Exception:
                pass  # клиент уже ушёл — писать больше некуда
        finally:
            if not self._skip_meter:
                # (v2.0.5) в телеметрию маршрут без query и без хвостовых
                # id/имён (см. Telemetry.route_key) + код ответа
                self.relay.note_request(
                    (time.perf_counter() - t0) * 1000.0,
                    path=self.path.split("?", 1)[0],
                    status=self._last_status)

    def do_POST(self):
        # Раньше необработанное исключение внутри любого POST-эндпоинта
        # (например voxel/fillbots при переполнении слотов) роняло обработку
        # запроса молча - клиент просто не получал ответа, кнопка в UI
        # выглядела "неработающей", а ошибка нигде не была видна.
        self._skip_meter = False
        self._last_status = 0
        t0 = time.perf_counter()
        try:
            self._do_POST_impl()
        except Exception as e:
            # (v2.0.5) телеметрия падений — как в do_GET
            self.relay.telemetry.record(
                "http.errors." + type(e).__name__)
            log.warning("POST %s упал: %s", self.path, e, exc_info=True)
            try:
                self._send_json(500, {"ok": False, "error": f"internal error: {e}"})
            except Exception:
                pass  # см. do_GET — клиент ушёл
        finally:
            if not self._skip_meter:
                self.relay.note_request(
                    (time.perf_counter() - t0) * 1000.0,
                    path=self.path.split("?", 1)[0],
                    status=self._last_status)

    # ── WebSocket-туннель голоса ──────────────────────────────────────────
    def _tunnel_voice_ws(self):
        """Прозрачный туннель: этот TLS-сокет → 127.0.0.1:<порт моста>.

        Браузер подключается к wss://IP:8420/voice/ws — тот же порт, что и
        страница (сертификат принят один раз на origin). Мы воспроизводим
        исходный HTTP-запрос внутреннему VoiceBridge (он и выполняет
        WS-рукопожатие по RFC 6455 — ответ 101 уходит браузеру через нас),
        а дальше просто перекачиваем байты в обе стороны до отключения.

        Браузер не шлёт WS-кадры до получения 101, поэтому потерять байты
        в rfile-буфере BaseHTTPRequestHandler нельзя (запрос — только
        строка + заголовки)."""
        # Туннель блокируется до отключения клиента — в метрики латентности
        # его время не попадает (см. комментарий к do_GET/do_POST).
        self._skip_meter = True
        self.close_connection = True
        port = self.relay.get_voice_ws_port()
        if not port:
            self._send_json(503, {"ok": False,
                                  "error": "голосовой мост недоступен"})
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            # ВАЖНО: снимаем таймаут ПОСЛЕ подключения. create_connection
            # оставляет 5-секундный таймаут на сокете — раньше из-за этого
            # pump(upstream, conn) умирал по socket.timeout, как только
            # мост молчал 5 секунд (все замьючены/никто не говорит —
            # аудио-потока нет) → туннель рвался → браузер получал
            # «соединение с голосовым мостом разорвано» прямо посреди
            # тихого голосового канала. Теперь сокет блокируется навсегда
            # и живёт, пока жива WS-связка браузер↔мост.
            upstream.settimeout(None)
            # v3.7.0: TCP_NODELAY на upstream — аплинк голоса (браузер →
            # мост) идёт мелкими 20мс-порциями; Nagle на loopback обычно
            # безвреден (ACK мгновенный), но под нагрузкой/на Windows
            # delayed-ack склеивает кадры во всплески — на всякий случай
            # тракт полностью без Nagle в обе стороны.
            try:
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        except OSError as e:
            self._send_json(503, {"ok": False,
                                  "error": f"голосовой мост недоступен: {e}"})
            return
        try:
            # Воспроизводим запрос (заголовки уже разобраны парсером)
            lines = [f"{self.command} {self.path} {self.request_version}"]
            for k, v in self.headers.items():
                lines.append(f"{k}: {v}")
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
            upstream.sendall(raw)

            conn = self.connection

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(16384)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t = threading.Thread(target=pump, args=(upstream, conn),
                                 daemon=True)
            t.start()
            pump(conn, upstream)  # свой поток — браузер → мост
            t.join(timeout=3)
        finally:
            for s in (upstream, self.connection):
                try:
                    s.close()
                except OSError:
                    pass

    # ── GET: диспетчер ────────────────────────────────────────────────────
    def _do_GET_impl(self):
        parsed = urlparse(self.path)

        # WebSocket-туннель голоса (/voice/ws) — ДО всех маршрутов: это не
        # HTTP-запрос в обычном смысле, после рукопожатия соединение
        # перекачивается байт в байт на внутренний порт моста. Туннель живёт
        # на ТОМ ЖЕ порту, что и веб-клиент: сертификат браузер уже принял
        # для этого origin, отдельного подтверждения для wss не требуется.
        if (parsed.path == "/voice/ws"
                and "websocket" in (self.headers.get("Upgrade") or "").lower()
                and "upgrade" in (self.headers.get("Connection") or "").lower()):
            self._tunnel_voice_ws()
            return

        # /ping - единственный открытый маршрут (нужен для регистрации
        # сессии ДО проверки access_key, иначе новый клиент не сможет
        # получить session_token).
        if parsed.path == "/ping":
            self._get_ping(parsed)
            return

        # Каркас веб-клиента (страница + статика) — без проверки ключа.
        # Браузер НЕ может приложить заголовок X-Relay-Key к обычной
        # навигации по адресу хоста, поэтому при включённом ключе доступа
        # страница возвращала 403 вместо интерфейса — веб-версия была
        # недоступна целиком, хотя ключ вводится уже НА странице (поле
        # «Ключ доступа» в форме входа). Само приложение — просто
        # статический HTML/JS, ключ по-прежнему требуется ко всем
        # API-маршрутам ниже.
        # (v2.0.5) /games и /games/<id> — часть каркаса веб-клиента
        # (лаунчер + HTML самих игр), ключ не нужен; игровые ходы при
        # этом идут через /bot_command и там проверяются как обычно.
        if (parsed.path in ("/", "/web")
                or parsed.path.startswith("/static/")
                or parsed.path.startswith("/games")):
            exact = self.GET_ROUTES.get(parsed.path)
            if exact is not None:
                exact(self, parsed)
                return
            for prefix, handler in self.GET_PREFIX_ROUTES:
                if parsed.path.startswith(prefix):
                    handler(self, parsed)
                    return
            self._send_json(404, {"ok": False, "error": "неизвестный путь"})
            return

        if not self._check_key():
            self._send_json(403, {"ok": False, "error": "неверный ключ доступа"})
            return

        # Приватные GET-маршруты (личные сообщения): подпись X-Relay-Auth
        # по path+query (как lib/auth.py auth_header_for_get). Раньше любой,
        # знающий ключ, подделывал X-Relay-From и читал ЧУЖУЮ переписку.
        if parsed.path in AUTH_REQUIRED_GET:
            signed_target = self.path.encode("utf-8", "replace")
            sender = self.relay.authenticate(
                signed_target, self._sender_from_header(),
                self.headers.get("X-Relay-Auth", ""),
            )
            if sender is None:
                self._send_json(403, {
                    "ok": False,
                    "error": "неверная сессия или подмена имени",
                })
                return

        exact = self.GET_ROUTES.get(parsed.path)
        if exact is not None:
            exact(self, parsed)
            return
        for prefix, handler in self.GET_PREFIX_ROUTES:
            if parsed.path.startswith(prefix):
                handler(self, parsed)
                return
        self._send_json(404, {"ok": False, "error": "неизвестный путь"})

    # ── GET: маршруты ─────────────────────────────────────────────────────
    def _get_ping(self, parsed):
        # /ping выдаёт session_token для последующей подписи запросов.
        # sender берётся из X-Relay-From - клиент при первом пинге
        # сообщает кто он. Сервер запоминает token -> sender.
        #
        # (v3.3.1) Резервирование имени хоста. Имя relay.host_name («Жуж»)
        # — это админская личность: music-бот проверяет права через
        # sender == host_name, а SessionStore.register() при повторном
        # /ping с тем же именем аннулирует старую сессию. Итог: раньше
        # любой гость, введя имя хоста, перехватывал админку и выкидывал
        # настоящего хоста. Теперь имя host_name можно занять только:
        #   1) с правильным X-Relay-Admin (= relay.admin_key), или
        #   2) с loopback (127.0.0.1/::1) — это свой GUI-клиент хоста,
        #      GUI-режим работает как раньше без лишних настроек.
        # Остальные имена не трогаем — гость «Петя» регистрируется свободно.
        sender = self._sender_from_header()
        if sender and sender == self.relay.host_name:
            admin_hdr = header_decode(self.headers.get("X-Relay-Admin", ""))
            import hmac as _hmac

            key_ok = bool(self.relay.admin_key) and _hmac.compare_digest(
                admin_hdr.encode("utf-8"),
                self.relay.admin_key.encode("utf-8"),
            )
            try:
                # ВАЖНО: за TLS-мультиплексором client_address — заглушка
                # ("tls-mux", 0) (см. QueuedHTTPServer.get_request), реальный
                # пир достаём из самого соединения (getpeername).
                try:
                    peer = self.request.getpeername()[0]
                except (OSError, IndexError, TypeError, AttributeError):
                    peer = self.client_address[0] if self.client_address else ""
            except Exception:
                peer = ""
            loopback = peer in ("127.0.0.1", "::1")
            if not (key_ok or loopback):
                # (v3.3.2) Дискриминаторы как в Discord: вместо отказа
                # выдаём «Жуж#2». Человек, который ничего не понимает,
                # просто входит и видит честное уведомление, а личность
                # хоста остаётся уникальной (перехвата больше нет).
                #
                # (v3.5.1) Если клиент ПЫТАЛСЯ ввести ключ админа, но тот
                # не совпал — помечаем ответ admin_key_rejected. Раньше
                # это выглядело как молчаливое «Жуж#2» без объяснений:
                # человек был уверен, что ключ принят, и не понимал,
                # почему админства нет. Теперь веб-клиент показывает
                # «ключ админа неверный» (см. main.js connectInner).
                assigned = self.relay.next_free_guest_name(sender)
                if assigned:
                    payload = {
                        "ok": True,
                        "name": self.relay.host_name,
                        "protocol": PROTOCOL_VERSION,
                        "session_token": self.relay.issue_session(assigned),
                        "requested_name": sender,
                        "name_assigned": assigned,
                        "reserved_name": True,
                    }
                    if admin_hdr:
                        payload["admin_key_rejected"] = True
                        self.relay.telemetry.record("session.admin_key_rejected")
                    self._send_json(200, payload)
                    return
                self._send_json(403, {
                    "ok": False,
                    "error": (
                        f"Имя «{sender}» зарезервировано за хостом сервера. "
                        "Возьми другое имя или введи ключ админа."
                    ),
                    "error_code": "name_reserved",
                })
                return
        session_token = self.relay.issue_session(sender)
        # (v2.0.5) Телеметрия: тип клиента по User-Agent — браузер шлёт
        # Mozilla/…, Qt/скрипты — Python-urllib/…. Только факт «кто ходит»,
        # без имён; UA — стандартная служебная информация запроса.
        try:
            ua = self.headers.get("User-Agent", "")
        except Exception:
            ua = ""
        kind = "browser" if "Mozilla" in ua or "Chrome" in ua else "python"
        self.relay.telemetry.record("session.kind." + kind)
        self._send_json(200, {
            "ok": True,
            "name": self.relay.host_name,
            "protocol": PROTOCOL_VERSION,
            "session_token": session_token,
        })

    def _get_events(self, parsed):
        sender = self._sender_from_header()
        qs = parse_qs(parsed.query)
        try:
            since = int(qs.get("since", ["0"])[0])
        except ValueError:
            since = 0
        tail = None
        if "tail" in qs:
            try:
                tail = int(qs["tail"][0])
            except ValueError:
                tail = None
        # Параметр before для подгрузки старых сообщений (скролл вверх)
        before = None
        if "before" in qs:
            try:
                before = int(qs["before"][0])
            except ValueError:
                before = None
        count = 50  # дефолтное количество старых сообщений
        if "count" in qs:
            try:
                count = int(qs["count"][0])
                count = max(10, min(count, 200))  # лимит 10-200
            except ValueError:
                count = 50
        poller = sender
        if poller:
            self.relay.touch_presence(poller)

        # (v2.0.5) Гейдж онлайна: сейчас + пик (пик считает set_gauge сам).
        # Считается на каждом поллинге — это самый частый источник правды
        # о живости сервера.
        self.relay.telemetry.set_gauge(
            "server.online_now", len(self.relay.online_names()))

        # Если задан before - запрашиваем старые сообщения
        if before is not None:
            events = self.relay.events_before(before, count)
            # Для старых сообщений next_since - это минимальный seq в батче
            next_since = events[0]["seq"] if events else before
            has_more = bool(events) and events[0]["seq"] > 0  # есть ли ещё старее
        else:
            events = self.relay.events_since(since, tail=tail)
            next_since = events[-1]["seq"] if events else since
            has_more = False

        self._send_json(
            200,
            {
                "events": events,
                "next_since": next_since,
                "online": self.relay.online_names(),
                "typing": self.relay.typing_names(exclude=poller),
                "has_more": has_more,
            },
        )

    def _get_web_app(self, parsed):
        # GET / и GET /web - отдаём одностраничный веб-клиент
        # (web_client/index.html). Раздаём с того же origin: браузерный
        # fetch ходит без CORS и получает те же куки-политики, что хост.
        # (v2.0.2) Чтение через MtimeFileCache: правки файла по-прежнему
        # подхватываются без рестарта (ключ — mtime+size), но на горячем
        # пути (каждый рефреш страницы у каждого друга) дискового чтения
        # больше нет.
        web_index = (
            Path(__file__).resolve().parent.parent.parent
            / "web_client" / "index.html"
        )
        data = _STATIC_CACHE.get(web_index)
        if data is None:
            self._send_json(404, {
                "ok": False,
                "error": "веб-клиент не собран: нет web_client/index.html",
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _get_crypto_info(self, parsed):
        # GET /crypto/info — per-installation соль PBKDF2 для веб-клиента.
        # Веб-клиент выводит AES-ключ по той же соли, что и Qt-клиент хоста,
        # иначе расшифровка не сойдётся. Соль — не секрет (параметр KDF);
        # секрет — сам ключ шифрования, который вводит пользователь.
        self._send_json(200, {"ok": True, "salt": self.relay.get_crypto_salt()})

    def _get_static(self, parsed):
        # GET /static/<файл> — статика веб-клиента (.js модули, .css,
        # библиотеки из web_client/). Только имя файла, никаких подпутей.
        # v1.8.2: клиент разбит на модули — сюда добавился style.css
        # (text/css), раньше отдавались только .js.
        # v3.7.2: .wasm для шумодава RNNoise в аудио-ворклете
        # (application/wasm — правильный mime разрешает streaming-компиляцию).
        name = parsed.path[len("/static/"):]
        ext = Path(name).suffix.lower()
        mime = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".wasm": "application/wasm",
        }.get(ext)
        if (not name or "/" in name or "\\" in name or ".." in name
                or mime is None):
            self._send_json(404, {"ok": False, "error": "файл не найден"})
            return
        static_file = (
            Path(__file__).resolve().parent.parent.parent / "web_client" / name
        )
        # (v2.0.2) MtimeFileCache вместо чтения с диска на каждый запрос:
        # правки подхватываются честно (ключ mtime+size), но рефреш страницы
        # больше не дёргает диск.
        data = _STATIC_CACHE.get(static_file)
        if data is None:
            self._send_json(404, {"ok": False, "error": "файл не найден"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _get_games(self, parsed):
        # GET /games — список игр для лаунчера (v2.0.5). Данные берём из
        # _GAMES: добавляем exist_ok (файл мог не доехать до хоста при
        # кривом обновлении), чтобы вкладка не предлагала мёртвые плитки.
        games = []
        for gid, meta in _GAMES.items():
            games.append({
                "id": gid,
                "emoji": meta["emoji"],
                "title": meta["title"],
                "desc": meta["desc"],
                "mode": meta["mode"],
                "url": "/games/" + gid,
                "available": (_GAMES_DIR / meta["file"]).is_file(),
            })
        self._send_json(200, {"ok": True, "games": games})
        # (v2.0.5) Телеметрия: лаунчер открывали (после успешного ответа).
        self.relay.telemetry.record("games.list")

    def _get_game(self, parsed):
        # GET /games/<id> — HTML-страница самой игры (v2.0.5), аналог
        # окна «активити» в Discord: это такой же самостоятельный веб-апп,
        # веб-клиент встраивает его в <iframe sandbox>. id обязан быть
        # ключом _GAMES — по реестру (не по URL) строим путь к файлу.
        gid = unquote(parsed.path[len("/games/"):]).strip().lower()
        meta = _GAMES.get(gid)
        if meta is None:
            self._send_json(404, {"ok": False, "error": "игра не найдена"})
            return
        game_file = _GAMES_DIR / meta["file"]
        # (v2.0.5) Тот же MtimeFileCache, что у /static — рефреш страницы
        # не дёргает диск, правки подхватываются по mtime+size.
        data = _STATIC_CACHE.get(game_file)
        if data is None:
            self._send_json(404, {"ok": False, "error": "игра не найдена"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)
        # (v2.0.5) Телеметрия: игру ОТКРЫЛИ (id уже валидирован реестром —
        # чужой мусор сюда не дойдёт, ключи ограничены _GAMES).
        self.relay.telemetry.record("games.opened." + gid)

    def _get_avatar(self, parsed):
        name = unquote(parsed.path[len("/avatar/"):])
        data = self.relay.get_avatar_bytes(name)
        if data is None:
            self._send_json(404, {"ok": False, "error": "нет аватарки"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _get_profile(self, parsed):
        # GET /profile/{name} - публичный профиль пользователя.
        name = unquote(parsed.path[len("/profile/"):])
        if not name:
            self._send_json(400, {"ok": False, "error": "не указано имя"})
            return
        profile = self.relay.get_profile(name)
        online_names = self.relay.online_names()
        self._send_json(200, {
            "ok": True,
            "profile": {
                "name": profile.get("name", name),
                "display_name": profile.get("display_name", name),
                "bio": profile.get("bio", ""),
                "status": profile.get("status", ""),
                "friends": profile.get("friends", []),
                "online": name in online_names,
                "created_at": profile.get("created_at", 0),
                # v1.9.6: веб-клиент показывает картинку, только если она есть
                "has_avatar": self.relay.get_avatar_bytes(name) is not None,
            },
        })

    def _get_friends(self, parsed):
        # GET /friends?name=X - список друзей с их онлайн-статусом.
        qs = parse_qs(parsed.query)
        name = qs.get("name", [""])[0]
        if not name:
            self._send_json(400, {"ok": False, "error": "не указано имя"})
            return
        friend_names = self.relay.get_friends(name)
        online_names = self.relay.online_names()
        friends_info = []
        for fn in friend_names:
            fp = self.relay.get_profile(fn)
            friends_info.append({
                "name": fn,
                "display_name": fp.get("display_name", fn),
                "status": fp.get("status", ""),
                "online": fn in online_names,
            })
        self._send_json(200, {"ok": True, "friends": friends_info})

    def _get_leaderboard(self, parsed):
        # GET /leaderboard - топ игроков по ELO из ChessBot.
        # Используется экраном профиля для отображения ранга.
        scores = self.relay.get_leaderboard(50)
        self._send_json(200, {"ok": True, "scores": scores})

    def _get_bots_list(self, parsed):
        # Список активных ботов
        bot_manager = self.relay.get_bot_manager()
        bots = bot_manager.list_bots()
        self._send_json(200, {"ok": True, "bots": bots})

    def _get_server_stats(self, parsed):
        # (v2.0.2) Служебная сводка хоста: uptime, кеш событий, сессии,
        # файлы/каналы, голос/voxel. Только счётчики (см. фасад
        # get_server_stats) — самочувствие сервера глазами хоста, для
        # «а жив ли он вообще» из браузера/curl. За access_key, как все
        # обычные GET (проверка выше по _check_key).
        self._send_json(200, self.relay.get_server_stats())

    def _get_voice_info(self, parsed):
        # Информация о голосовом сервере (хост + порт для TCP-подключения)
        # Клиент спрашивает это перед тем как открыть VoiceChannelDialog
        self._send_json(200, self.relay.get_voice_info())

    def _get_voice_participants(self, parsed):
        # Список участников голосового канала
        participants = self.relay.get_voice_participants()
        self._send_json(200, {"ok": True, "participants": participants})

    # ── демонстрация экрана (v3.6.0, модуль screen/) ─────────────────────
    def _get_screen_info(self, parsed):
        # Кто сейчас показывает экран: [{from, title, started, viewers}].
        # Медиа через WebRTC P2P — сервер отдаёт только реестр показов.
        self._send_json(200, self.relay.get_screen_info())

    def _get_screen_poll(self, parsed):
        # Сигналинг WebRTC: забрать адресату свои письма (offer/answer/bye).
        # Маршрут в AUTH_REQUIRED_GET: почта персональная, подпись
        # X-Relay-Auth обязательна (как /dm/history) — иначе знающий ключ
        # мог бы читать чужой хендшейк по подставленному X-Relay-From.
        qs = parse_qs(parsed.query)
        try:
            since = int((qs.get("since") or ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        self._send_json(200, self.relay.screen_poll(self._sender_from_header(), since))

    def _get_voxel_info(self, parsed):
        # Информация о TCP-сервере сессий (порт = HTTP+2)
        self._send_json(200, self.relay.get_voxel_info())

    def _get_voxel_sessions(self, parsed):
        # Список активных сессий (rooms)
        sessions = self.relay.get_voxel_sessions()
        self._send_json(200, {"ok": True, "sessions": sessions})

    def _get_pinned(self, parsed):
        # Все закреплённые сообщения (не только из последних 50)
        pinned = self.relay.get_pinned_messages()
        self._send_json(200, {"ok": True, "pinned": pinned})

    def _get_search(self, parsed):
        # (v3.8.0) Поиск по ВСЕЙ истории: общий чат + каналы.
        # q - что ищем (минимум 2 символа, регистр не важен), author -
        # необязательный фильтр по автору, limit - сколько последних
        # совпадений вернуть (10-100, дефолт 50). Зашифрованные сообщения
        # не ищутся честно: сервер их не читает (E2E).
        qs = parse_qs(parsed.query)
        q = (qs.get("q", [""])[0] or "").strip()
        if len(q) < 2:
            self._send_json(400, {"ok": False,
                "error": "запрос короче 2 символов"})
            return
        author = (qs.get("author", [""])[0] or "").strip()[:64]
        limit = 50
        if "limit" in qs:
            try:
                limit = max(10, min(int(qs["limit"][0]), 100))
            except ValueError:
                limit = 50
        out = self.relay.search_history(q, limit=limit, author=author)
        self._send_json(200, {"ok": True, "results": out["results"],
                              "total": out["total"]})

    def _get_dm_history(self, parsed):
        # Получение истории личных сообщений - раньше этот роут был по
        # ошибке зарегистрирован внутри do_POST, а клиент всегда запрашивал
        # его через GET (это же просто чтение данных, не действие) - в итоге
        # история ЛС никогда не загружалась.
        sender = self._sender_from_header()
        qs = parse_qs(parsed.query)
        other_user = qs.get("user", [""])[0]
        limit = None
        if "limit" in qs:
            try:
                limit = int(qs["limit"][0])
            except ValueError:
                limit = None

        if not other_user:
            self._send_json(400, {"ok": False, "error": "не указан пользователь"})
            return
        if self._valid_dm_name(other_user) is None:
            self._send_json(400, {"ok": False, "error": "недопустимое имя пользователя"})
            return
        # Кламп: limit=0/отрицательный отдавал ВСЮ историю диалога (а она
        # не ограничена ничем) — теперь как у /events: разумный диапазон.
        if limit is not None:
            limit = max(1, min(limit, 500))

        history = self.relay.get_dm_history(sender, other_user, limit)
        self._send_json(200, {"ok": True, "messages": history})

    def _get_dm_conversations(self, parsed):
        # Получение списка диалогов - та же история, что и с /dm/history:
        # жил в do_POST, вызывался через GET.
        sender = self._sender_from_header()
        conversations = self.relay.get_dm_conversations(sender)
        self._send_json(200, {"ok": True, "conversations": conversations})

    def _get_channels(self, parsed):
        # Получение списка каналов
        channels = self.relay.get_channels()
        self._send_json(200, {"ok": True, "channels": channels})

    def _get_channel_messages(self, parsed):
        # Получение сообщений канала
        qs = parse_qs(parsed.query)
        channel_name = qs.get("channel", [""])[0]
        limit = 50
        if "limit" in qs:
            try:
                limit = int(qs["limit"][0])
            except ValueError:
                limit = 50
        # Кламп: limit=0/отрицательный раньше отдавал ВЕСЬ канал одним
        # ответом (messages[-0:] = все события) — кладь трафика и RAM.
        limit = max(1, min(limit, 500))

        if not channel_name:
            self._send_json(400, {"ok": False, "error": "не указан канал"})
            return

        messages = self.relay.get_channel_messages(channel_name, limit)
        self._send_json(200, {"ok": True, "messages": messages})

    def _get_files(self, parsed):
        # Список всех файлов (файловая витрина)
        files = self.relay.list_all_files()
        self._send_json(200, {"ok": True, "files": files})

    def _get_download(self, parsed):
        file_id = parsed.path[len("/download/"):]
        ev = self.relay.get_file_event(file_id)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "файл не найден"})
            return
        path = self.relay.file_bytes_path(file_id)
        if not path.exists():
            self._send_json(404, {"ok": False, "error": "файл пропал с диска хоста"})
            return
        # (v2.0.5) Телеметрия: факт скачивания (id файла не записываем).
        self.relay.telemetry.record("file.downloaded")
        self._send_file_bytes(path, ev["name"], etag=ev.get("sha256", ""))

    def _get_dm_download(self, parsed):
        file_id = parsed.path[len("/dm_download/"):]
        ev = self.relay.get_dm_file_event(file_id)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "файл не найден"})
            return
        requester = self._sender_from_header()
        if requester not in (ev.get("from"), ev.get("to")):
            # Приватный DM-файл: скачать может только тот, кто
            # его отправил, или тот, кому он адресован.
            self._send_json(403, {"ok": False, "error": "это личный файл не для тебя"})
            return
        path = self.relay.dm_file_bytes_path(file_id)
        if not path.exists():
            self._send_json(404, {"ok": False, "error": "файл пропал с диска хоста"})
            return
        # (v2.0.5) Телеметрия: факт скачивания ЛС-файла (без id/имён).
        self.relay.telemetry.record("dm.file.downloaded")
        self._send_file_bytes(path, ev["name"], etag=ev.get("sha256", ""))

    # ── POST: диспетчер ───────────────────────────────────────────────────
    def _do_POST_impl(self):
        if not self._check_key():
            self._send_json(403, {"ok": False, "error": "неверный ключ доступа"})
            return
        parsed = urlparse(self.path)
        handler = self.POST_ROUTES.get(parsed.path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "неизвестный путь"})
            return

        # ── Центральная проверка подлинности отправителя (v1.9.5) ──
        # Раньше authenticate() вызывали только /send_text, /bot_command и
        # /dispatch; остальные ~23 POST-маршрута верили X-Relay-From на слово:
        # любой, знающий access_key, мог РЕДАКТИРОВАТЬ и УДАЛЯТЬ чужие
        # сообщения, перезаписывать чужие аватарки и профили, слать DM и
        # файлы от чужого имени. Теперь подпись X-Relay-Auth проверяется
        # для ВСЕХ POST-маршрутов в одном месте.
        if parsed.path in STREAM_BODY_ROUTES:
            # Файловые/аватарные тела стримятся на диск — их хендлеры сами
            # кормят HMAC кусками (relay.begin_stream_auth) и сами
            # отвергают запрос до записи при невалидной подписи.
            self._auth_sender = ""
            self._body_raw = b""
            handler(self, parsed)
            return

        raw = self._read_body()
        sender = self.relay.authenticate(
            raw, self._sender_from_header(""), self.headers.get("X-Relay-Auth", "")
        )
        if sender is None:
            self._send_json(403, {
                "ok": False,
                "error": "неверная сессия или подмена имени",
            })
            return
        self._auth_sender = sender
        self._body_raw = raw
        handler(self, parsed)

    # ── POST: маршруты ────────────────────────────────────────────────────
    def _post_send_text(self, parsed):
        # Отправитель и тело уже проверены центральной аутентификацией
        # в _do_POST_impl (v1.9.5): здесь осталась только доменная валидация.
        sender = self._auth_sender
        raw = self._body_raw
        body = self._parse_json(raw)

        # Проверяем, зашифровано ли сообщение
        encrypted = body.get("encrypted", False)
        text = (body.get("text") or "").strip()
        iv = body.get("iv", "")

        if not text:
            self._send_json(400, {"ok": False, "error": "пустое сообщение"})
            return

        # (v2.0.4) Антифлуд — ПОСЛЕ валидации (пустые тексты не съедают
        # токены) и ДО записи в журнал: флудер не должен писать JSONL,
        # плодить события в кешах всех клиентов и будить ботов. 429 с
        # retry_after — честный ответ, а не 500/403: клиент покажет
        # «слишком много сообщений» и подождёт.
        if not self.relay.allow_message(sender):
            # (v2.0.5) Телеметрия: антифлуд сработал (факт 429, без имени).
            self.relay.telemetry.record("http.flood_blocked")
            self._send_json(429, {
                "ok": False,
                "error": "слишком много сообщений, притормози",
                "retry_after": round(self.relay.flood_retry_after(sender), 2),
            })
            return

        # Если сообщение зашифровано, сохраняем как есть
        # Если нет - сохраняем обычный текст
        message_text = text
        if encrypted and iv:
            # Сохраняем зашифрованное сообщение
            ev = self.relay.add_text(sender, message_text[:4000], encrypted=True, iv=iv)
        else:
            ev = self.relay.add_text(sender, message_text[:4000])

        self._send_json(200, {"ok": True, "seq": ev["seq"]})

    def _post_edit_text(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        target_seq = self._parse_seq(body)
        new_text = (body.get("text") or "").strip()
        if target_seq <= 0:
            self._send_json(400, {"ok": False, "error": "неверный seq"})
            return
        if not new_text:
            self._send_json(400, {"ok": False, "error": "пустой текст"})
            return
        # v1.9.5: флаг шифрования правки (см. event_store.edit_text)
        encrypted = bool(body.get("encrypted", False))
        iv = body.get("iv", "")
        ev = self.relay.edit_text(sender, target_seq, new_text[:4000],
                                  encrypted=encrypted, iv=iv)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "не найдено или нельзя редактировать"})
            return
        self._send_json(200, {"ok": True, "seq": ev["seq"], "target_seq": target_seq})

    def _post_add_reaction(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        target_seq = self._parse_seq(body)
        emoji = (body.get("emoji") or "").strip()
        if target_seq <= 0:
            self._send_json(400, {"ok": False, "error": "неверный seq"})
            return
        if not emoji:
            self._send_json(400, {"ok": False, "error": "пустой эмодзи"})
            return
        ev = self.relay.add_reaction(sender, target_seq, emoji)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "сообщение не найдено"})
            return
        self._send_json(
            200,
            {"ok": True, "seq": ev["seq"], "target_seq": target_seq,
             "emoji": emoji, "added": bool(ev.get("added", True))},
        )

    def _post_pin_message(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        target_seq = self._parse_seq(body)
        if target_seq <= 0:
            self._send_json(400, {"ok": False, "error": "неверный seq"})
            return
        ev = self.relay.pin_message(sender, target_seq)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "сообщение не найдено"})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "seq": ev["seq"],
                "target_seq": target_seq,
                "pinned": ev["pinned"],
            },
        )

    def _post_typing(self, parsed):
        # Эфемерный сигнал "я сейчас печатаю" - не пишется в историю, просто
        # отметка с TTL, отдаётся всем через поле "typing" в ответе GET /events.
        # Тело уже вычитано центральной аутентификацией (для подписи) —
        # keep-alive склейки байтов со следующим запросом больше нет.
        sender = self._auth_sender
        self.relay.set_typing(sender)
        # (v2.0.5) Телеметрия: «печатает…» — самый частый сигнал клиента,
        # счётчик показывает живую активность даже без отправки сообщений.
        self.relay.telemetry.record("chat.typing")
        self._send_json(200, {"ok": True})

    def _post_delete_event(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        target_seq = self._parse_seq(body)
        if target_seq <= 0:
            self._send_json(400, {"ok": False, "error": "неверный seq"})
            return
        ev = self.relay.delete_event(sender, target_seq)
        if ev is None:
            self._send_json(404, {"ok": False, "error": "не найдено или нельзя удалить"})
            return
        self._send_json(200, {"ok": True, "seq": ev["seq"], "target_seq": target_seq})

    def _post_avatar(self, parsed):
        # Стриминговый маршрут (тело = PNG-байты): аутентификация
        # инкрементальная, см. STREAM_BODY_ROUTES.
        user, mac, signature = self.relay.begin_stream_auth(
            self._sender_from_header(), self.headers.get("X-Relay-Auth", "")
        )
        if user is None:
            self._send_json(403, {
                "ok": False,
                "error": "неверная сессия или подмена имени",
            })
            return
        sender = user
        length = self._header_int(self.headers.get("Content-Length"), 0)
        if length is None:
            self._send_json(400, {"ok": False, "error": "неверный Content-Length"})
            return
        if length < 0 or length > MAX_AVATAR_UPLOAD_SIZE:
            self._send_json(400, {"ok": False, "error": "неверный размер аватарки"})
            return
        # length == 0 = удаление аватарки (v1.9.6): раньше отвергалось 400,
        # и «Убрать аватар» в клиентах всегда падал
        data = self.rfile.read(length) if length else b""
        if length and len(data) != length:
            self._send_json(400, {"ok": False, "error": "оборвалась загрузка"})
            return
        if mac is not None:
            mac.update(data)
            import hmac as _hmac
            if not _hmac.compare_digest(mac.hexdigest(), signature):
                self._send_json(403, {
                    "ok": False,
                    "error": "неверная сессия или подмена имени",
                })
                return
        if not self.relay.set_avatar(sender, data):
            self._send_json(400, {"ok": False, "error": "не удалось сохранить"})
            return
        self._send_json(200, {"ok": True})

    def _post_profile_update(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        # Только владелец может менять свой профиль (sender уже валидирован
        # через authenticate в маршрутах, где это критично).
        fields = {}
        if "bio" in body:
            fields["bio"] = body["bio"]
        if "status" in body:
            fields["status"] = body["status"]
        if "display_name" in body:
            fields["display_name"] = body["display_name"]
        if not fields:
            self._send_json(400, {"ok": False, "error": "нет полей для обновления"})
            return
        self.relay.save_profile(sender, **fields)
        self._send_json(200, {"ok": True, "profile": self.relay.get_profile(sender)})

    def _post_friends_add(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        friend_name = (body.get("name") or "").strip()
        if not friend_name:
            self._send_json(400, {"ok": False, "error": "не указано имя друга"})
            return
        if self.relay.add_friend(sender, friend_name):
            self._send_json(200, {"ok": True, "friends": self.relay.get_friends(sender)})
        else:
            self._send_json(400, {"ok": False, "error": "не удалось добавить (уже в списке или лимит)"})

    def _post_friends_remove(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        friend_name = (body.get("name") or "").strip()
        if not friend_name:
            self._send_json(400, {"ok": False, "error": "не указано имя друга"})
            return
        if self.relay.remove_friend(sender, friend_name):
            self._send_json(200, {"ok": True, "friends": self.relay.get_friends(sender)})
        else:
            self._send_json(400, {"ok": False, "error": "друг не найден в списке"})

    def _post_channel_create(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        channel_name = (body.get("name") or "").strip()
        if not channel_name:
            self._send_json(400, {"ok": False, "error": "пустое имя канала"})
            return
        result = self.relay.create_channel(channel_name, sender)
        if "error" in result:
            self._send_json(400, {"ok": False, "error": result["error"]})
            return
        self._send_json(200, {"ok": True, "channel": result})

    def _post_channel_send(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        channel_name = (body.get("channel") or "").strip()
        text = (body.get("text") or "").strip()
        if not channel_name:
            self._send_json(400, {"ok": False, "error": "не указан канал"})
            return
        if not text:
            self._send_json(400, {"ok": False, "error": "пустое сообщение"})
            return
        # E2E-шифрование (как в /send_text): если клиент прислал encrypted+iv,
        # сохраняем метку, чтобы получатель знал что текст надо расшифровывать.
        encrypted = bool(body.get("encrypted", False))
        iv = body.get("iv", "")
        result = self.relay.send_channel_message(
            channel_name, sender, text[:4000], encrypted=encrypted, iv=iv
        )
        if "error" in result:
            self._send_json(400, {"ok": False, "error": result["error"]})
            return
        self._send_json(200, {"ok": True, "seq": result["seq"]})

    def _post_channel_join(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        channel_name = (body.get("channel") or "").strip()
        if not channel_name:
            self._send_json(400, {"ok": False, "error": "не указан канал"})
            return
        result = self.relay.add_channel_member(channel_name, sender)
        if "error" in result:
            self._send_json(400, {"ok": False, "error": result["error"]})
            return
        self._send_json(200, {"ok": True, "result": result})

    def _post_channel_delete(self, parsed):
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        channel_name = (body.get("channel") or "").strip()
        if not channel_name:
            self._send_json(400, {"ok": False, "error": "не указан канал"})
            return
        result = self.relay.delete_channel(channel_name, sender)
        if "error" in result:
            # not_creator - отдельный код, чтобы UI показал внятное сообщение
            if result["error"] == "not_creator":
                self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": f"удалить может только создатель канала ({result.get('creator', '?')})",
                        "code": "not_creator",
                    },
                )
            else:
                self._send_json(400, {"ok": False, "error": result["error"]})
            return
        self._send_json(200, {"ok": True, "name": result["name"]})

    def _post_send_file(self, parsed):
        # Стриминговый маршрут: подпись проверяем инкрементально, по кускам
        # (файлы до 2 ГБ — в RAM для HMAC их не прочитать). Отвергаем ДО
        # записи на диск, если сессия невалидна.
        user, mac, signature = self.relay.begin_stream_auth(
            self._sender_from_header(), self.headers.get("X-Relay-Auth", "")
        )
        if user is None:
            self._send_json(403, {
                "ok": False,
                "error": "неверная сессия или подмена имени",
            })
            return
        sender = user
        filename = Path(
            header_decode(self.headers.get(HEADER_FILENAME, "файл") or "файл")
        ).name
        length = self._header_int(self.headers.get("Content-Length"), 0)
        if length is None:
            self._send_json(400, {"ok": False, "error": "неверный Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "пустой файл"})
            return
        if length > self.relay.max_file_size:
            self._send_json(413, {"ok": False, "error": "файл больше лимита хоста"})
            return
        # Стримим тело сразу на диск: RAM хоста больше не зависит от размера файла.
        streamed = self._stream_body_to_file(length, self.relay.files_dir, mac=mac)
        if streamed is None:
            self._send_json(400, {"ok": False, "error": "оборвалась загрузка - попробуй ещё раз"})
            return
        tmp, digest, _received = streamed
        if mac is not None:
            import hmac as _hmac

            if not _hmac.compare_digest(mac.hexdigest(), signature):
                tmp.unlink(missing_ok=True)
                self._send_json(403, {
                    "ok": False,
                    "error": "неверная сессия или подмена имени",
                })
                return
        # Клиент прислал свой хеш? Сверяем - расхождение означает битую передачу.
        claimed = (self.headers.get(HEADER_SHA256) or "").strip().lower()
        if claimed and claimed != digest:
            tmp.unlink(missing_ok=True)
            self._send_json(400, {"ok": False, "error": "хеш файла не совпал - передача была битой"})
            return
        ev = self.relay.add_file_from_path(sender, filename, tmp, sha256=digest)
        self._send_json(200, {"ok": True, "seq": ev["seq"], "file_id": ev["file_id"],
                              "sha256": ev.get("sha256", "")})

    def _post_send_dm_file(self, parsed):
        # Приватная отправка файла в ЛС - физически в отдельную папку
        # (dm_files_dir), в общую витрину /files не попадает (см.
        # EventStore.add_dm_file). Раньше этого роута не было вообще, и UI
        # молча слал файл через /send_file - в общую витрину, видную всем
        # на сервере, а не только собеседнику.
        user, mac, signature = self.relay.begin_stream_auth(
            self._sender_from_header(), self.headers.get("X-Relay-Auth", "")
        )
        if user is None:
            self._send_json(403, {
                "ok": False,
                "error": "неверная сессия или подмена имени",
            })
            return
        sender = user
        recipient = header_decode(self.headers.get(HEADER_TO, "") or "")
        if not recipient:
            self._send_json(400, {"ok": False, "error": "не указан получатель"})
            return
        if self._valid_dm_name(recipient) is None:
            self._send_json(400, {"ok": False, "error": "недопустимое имя получателя"})
            return
        filename = Path(
            header_decode(self.headers.get(HEADER_FILENAME, "файл") or "файл")
        ).name
        length = self._header_int(self.headers.get("Content-Length"), 0)
        if length is None:
            self._send_json(400, {"ok": False, "error": "неверный Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "пустой файл"})
            return
        if length > self.relay.max_file_size:
            self._send_json(413, {"ok": False, "error": "файл больше лимита хоста"})
            return
        streamed = self._stream_body_to_file(length, self.relay.dm_files_dir, mac=mac)
        if streamed is None:
            self._send_json(400, {"ok": False, "error": "оборвалась загрузка - попробуй ещё раз"})
            return
        tmp, digest, _received = streamed
        if mac is not None:
            import hmac as _hmac

            if not _hmac.compare_digest(mac.hexdigest(), signature):
                tmp.unlink(missing_ok=True)
                self._send_json(403, {
                    "ok": False,
                    "error": "неверная сессия или подмена имени",
                })
                return
        claimed = (self.headers.get(HEADER_SHA256) or "").strip().lower()
        if claimed and claimed != digest:
            tmp.unlink(missing_ok=True)
            self._send_json(400, {"ok": False, "error": "хеш файла не совпал - передача была битой"})
            return
        ev = self.relay.add_dm_file_from_path(sender, recipient, filename, tmp, sha256=digest)
        self._send_json(200, {"ok": True, "seq": ev["seq"], "file_id": ev["file_id"],
                              "sha256": ev.get("sha256", "")})

    def _post_file_have(self, parsed):
        # Реестр владельцев (multi-source v1): клиент сообщает "я скачал файл
        # и могу им раздавать". Сервер копит file_id -> [пользователи] -
        # фундамент будущей раздачи кусков между клиентами.
        body = self._parse_json(self._body_raw)
        file_id = str(body.get("file_id", "")).strip()
        user = self._auth_sender
        if not file_id or not user:
            self._send_json(400, {"ok": False, "error": "нужны file_id и имя"})
            return
        holders = self.relay.register_file_holder(file_id, user)
        if holders is None:
            self._send_json(404, {"ok": False, "error": "файл не найден"})
            return
        self._send_json(200, {"ok": True, "holders": holders})

    def _post_bot_command(self, parsed):
        # Endpoint для команд ботам
        # Отправитель и тело проверены центральной аутентификацией.
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        bot_id = body.get("bot_id", "")
        command = body.get("command", "")
        args = body.get("args", [])

        if not bot_id:
            self._send_json(400, {"ok": False, "error": "не указан bot_id"})
            return

        bot_manager = self.relay.get_bot_manager()
        response = bot_manager.process_command(bot_id, sender, command, args)

        if response is None:
            self._send_json(404, {"ok": False, "error": f"бот '{bot_id}' не найден"})
            return

        # (v2.0.5) Телеметрия: команда боту. bot_id приходит из запроса —
        # _safe_key в телеметрии ограничивает алфавит/длину, мусор не
        # разрастается в словарь ключей.
        self.relay.telemetry.record("bot.commands." + str(bot_id))

        # Извлекаем текст для чата + bot_action из ответа бота.
        # Поллинговые команды (state/getscores/matchlist/meta) могут
        # возвращать text, но помечены silent=True - тогда text НЕ
        # добавляется в чат (иначе будет спам каждые 800мс).
        text_in_chat, bot_action = self.relay.bot_response_to_chat(response)
        seq: int | None = None

        if text_in_chat:
            ev = self.relay.add_text("🤖 " + bot_id, text_in_chat)
            seq = ev["seq"]

        self._send_json(
            200,
            {
                "ok": True,
                "seq": seq,
                "bot_response": text_in_chat or "",
                "bot_action": bot_action,
            },
        )

    def _post_dispatch(self, parsed):
        # Endpoint для диспетчера !-команд из общего чата: клиент пишет
        # "!snake" в строке ввода, этот эндпоинт перебирает всех ботов и
        # находит того, кто матчит. Если никто не сматчил - клиент
        # отправляет как обычный текст.
        # Отправитель и тело проверены центральной аутентификацией.
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        text = (body.get("text") or "").strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "пустой текст"})
            return

        bot_manager = self.relay.get_bot_manager()
        result = bot_manager.dispatch_command(sender, text)

        if result is None:
            # Никто не сматчил - пусть клиент шлёт как обычное сообщение
            self._send_json(200, {"ok": True, "matched": False})
            return

        bot_id = result["bot_id"]
        response = result["response"]
        # (v2.0.5) Телеметрия: !-команда через dispatcher — тот же счётчик,
        # что и у /bot_command (важно «кто играл», а не каким путём).
        self.relay.telemetry.record("bot.commands." + str(bot_id))

        # Тот же helper что и в /bot_command - поллинговые команды
        # (silent=True) не спамят чат.
        text_in_chat, bot_action = self.relay.bot_response_to_chat(response)
        seq: int | None = None

        # Если есть текст для чата - добавляем как сообщение от бота
        if text_in_chat:
            ev = self.relay.add_text("🤖 " + bot_id, text_in_chat)
            seq = ev["seq"]

        self._send_json(
            200,
            {
                "ok": True,
                "matched": True,
                "bot_id": bot_id,
                "bot_name": result.get("bot_name", bot_id),
                "bot_response": text_in_chat or "",
                "bot_action": bot_action,
                "seq": seq,
            },
        )

    def _post_dm_send(self, parsed):
        # Отправка личного сообщения (отправитель уже проверен центрально)
        sender = self._auth_sender
        body = self._parse_json(self._body_raw)
        recipient = body.get("to", "")
        text = body.get("text", "")

        if not recipient or not text:
            self._send_json(400, {"ok": False, "error": "не указан получатель или текст"})
            return
        if self._valid_dm_name(recipient) is None:
            self._send_json(400, {"ok": False, "error": "недопустимое имя получателя"})
            return
        # Кап текста, как у /send_text и /channel/send: раньше сюда можно
        # было писать мегабайты в dm/*.jsonl безнаказанно.
        text = text[:4000]

        # (v2.0.4) Антифлуд — общее ведро с /send_text (флуд в ЛС так же
        # вреден, как флуд в общий чат; два ведра на человека не дали бы
        # ничего, кроме удвоенного числа настроек).
        if not self.relay.allow_message(sender):
            # (v2.0.5) Телеметрия: антифлуд сработал (ЛС — общее ведро с чатом).
            self.relay.telemetry.record("http.flood_blocked")
            self._send_json(429, {
                "ok": False,
                "error": "слишком много сообщений, притормози",
                "retry_after": round(self.relay.flood_retry_after(sender), 2),
            })
            return

        # E2E-шифрование (как в /send_text): метка + вектор для получателя
        encrypted = bool(body.get("encrypted", False))
        iv = body.get("iv", "")
        ev = self.relay.send_dm(sender, recipient, text, encrypted=encrypted, iv=iv)
        self._send_json(200, {"ok": True, "seq": ev["seq"]})

    def _post_voxel_create(self, parsed):
        # Создание новой сессии с настройками (mode, wall_hp, team sizes).
        # Числовые поля раньше парсились голым int(): мусорное значение
        # ("abc") улетало в 500 «internal error» — теперь честный 400.
        body = self._parse_json(self._body_raw)
        sid = (body.get("session_id") or "").strip()
        game_mode = (body.get("game_mode") or "pve").strip()
        wall_hp = self._int_arg(body.get("wall_hp"), 0)
        team_red_size = self._int_arg(body.get("team_red_size"), 5)
        team_blue_size = self._int_arg(body.get("team_blue_size"), 5)
        if not sid:
            self._send_json(400, {"ok": False, "error": "не указан session_id"})
            return
        if wall_hp is None or team_red_size is None or team_blue_size is None:
            self._send_json(400, {"ok": False,
                                  "error": "wall_hp/team_*_size должны быть числами"})
            return
        ok = self.relay.voxel_create_session(
            sid, game_mode=game_mode, wall_hp=wall_hp,
            team_red_size=team_red_size, team_blue_size=team_blue_size,
        )
        if ok is None:
            self._send_json(503, {"ok": False, "error": "voxel-сервер не запущен"})
            return
        if ok:
            # (v2.0.5) Телеметрия: факт (sid не записываем — это
            # пользовательская строка).
            self.relay.telemetry.record("voxel.created")
            self._send_json(200, {"ok": True, "session_id": sid})
        else:
            self._send_json(400, {"ok": False, "error": "сессия уже существует"})

    def _post_voxel_start(self, parsed):
        # Запустить игру (заполнить боты + playing)
        body = self._parse_json(self._body_raw)
        sid = (body.get("session_id") or "").strip()
        if not sid:
            self._send_json(400, {"ok": False, "error": "не указан session_id"})
            return
        ok = self.relay.voxel_start_game(sid)
        if ok is None:
            self._send_json(503, {"ok": False, "error": "voxel-сервер не запущен"})
            return
        if ok:
            self.relay.telemetry.record("voxel.started")
            self._send_json(200, {"ok": True})
        else:
            self._send_json(400, {"ok": False, "error": "сессия не найдена"})

    def _post_voxel_fillbots(self, parsed):
        # Заполнить пустые слоты ботами
        body = self._parse_json(self._body_raw)
        sid = (body.get("session_id") or "").strip()
        if not sid:
            self._send_json(400, {"ok": False, "error": "не указан session_id"})
            return
        added = self.relay.voxel_fill_bots(sid)
        if added is None:
            self._send_json(503, {"ok": False, "error": "voxel-сервер не запущен"})
            return
        if added:
            self.relay.telemetry.record("voxel.fillbots")
        self._send_json(200, {"ok": True, "added": added})

    def _post_voxel_addbot(self, parsed):
        # Добавить бота в сессию
        body = self._parse_json(self._body_raw)
        sid = (body.get("session_id") or "").strip()
        team = (body.get("team") or "none").strip()
        if not sid:
            self._send_json(400, {"ok": False, "error": "не указан session_id"})
            return
        ok = self.relay.voxel_add_bot(sid, team=team)
        if ok is None:
            self._send_json(503, {"ok": False, "error": "voxel-сервер не запущен"})
            return
        if ok:
            self.relay.telemetry.record("voxel.addbot")
            self._send_json(200, {"ok": True})
        else:
            self._send_json(400, {"ok": False, "error": "не удалось (сессия не найдена или заполнена)"})

    def _post_bots_add(self, parsed):
        # Добавление бота по ID/имени
        body = self._parse_json(self._body_raw)
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            self._send_json(400, {"ok": False, "error": "не указан bot_id"})
            return

        bot_manager = self.relay.get_bot_manager()
        ok = bot_manager.register_bot_by_id(bot_id)
        if ok:
            self._send_json(200, {"ok": True, "bot_id": bot_id})
        else:
            self._send_json(400, {"ok": False, "error": f"не удалось добавить бота '{bot_id}'"})

    def _post_bots_remove(self, parsed):
        # Удаление бота
        body = self._parse_json(self._body_raw)
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            self._send_json(400, {"ok": False, "error": "не указан bot_id"})
            return

        bot_manager = self.relay.get_bot_manager()
        ok = bot_manager.unregister_bot(bot_id)
        if ok:
            self._send_json(200, {"ok": True, "bot_id": bot_id})
        else:
            self._send_json(404, {"ok": False, "error": f"бот '{bot_id}' не найден"})

    # ── музыка (MusicBot, v3.3.0) ─────────────────────────────────────────
    def _get_music_sync(self, parsed):
        """GET /music/sync — состояние плеера для синхронизации клиентов.

        Отдаётся ВСЕГДА (не только когда играет): клиент по state_version
        видит смену трека/паузы мгновенно, position считается от серверных
        часов, server_time — для поправки на расхождение часов клиента."""
        state = self.relay.music_state()
        if state is None:
            self._send_json(200, {"ok": True, "playing": False, "track": None,
                                  "queue": [], "open_dj": True})
        else:
            self._send_json(200, {"ok": True, **state})

    def _get_music_library(self, parsed):
        """GET /music/library — библиотека доступных треков."""
        library = self.relay.get_music_library()
        self._send_json(200, {"ok": True, "tracks": library})

    def _get_music_stream(self, parsed):
        """GET /music/stream/<track_id> — стриминг аудио файла.

        v3.3.0: путь к файлу резолвится фасадом (music_stream_path) —
        публичная библиотека локальные пути больше не отдаёт."""
        # Извлекаем track_id из пути
        track_id = parsed.path[len("/music/stream/"):]
        if not track_id:
            self._send_json(400, {"ok": False, "error": "не указан track_id"})
            return

        track = self.relay.music_stream_path(track_id)
        if not track:
            self._send_json(404, {"ok": False, "error": f"трек '{track_id}' не найден"})
            return

        file_path = Path(track["path"])
        if not file_path.exists():
            self._send_json(404, {"ok": False, "error": "файл не найден на диске"})
            return

        # Отдаём файл с поддержкой Range запросов.
        # v3.3.0 ФИКС: раньше тут вызывался self._serve_file(...) — метода
        # с таким именем в хендлере НИКОГДА не существовало (правильно:
        # _send_file_bytes), любой запрос к /music/stream/<id> падал
        # с AttributeError 500. Поэтому стриминг «не получался».
        self._send_file_bytes(file_path, track["filename"])

    def _music_call(self, result):
        """Общая отправка ответа музыкальных POST-ов: None = бота нет."""
        if result is None:
            self._send_json(503, {"ok": False, "error": "музыкальный модуль не запущен"})
            return
        self._send_json(200 if result.get("ok") else 400, result)

    def _post_music_play(self, parsed):
        # POST /music/play {track_id?, position?} — играть/возобновить
        body = self._parse_json(self._body_raw)
        position = body.get("position")
        if position is not None:
            try:
                position = float(position)
            except (TypeError, ValueError):
                position = None
        self._music_call(self.relay.music_play(
            self._auth_sender, str(body.get("track_id") or ""),
            position))

    def _post_music_pause(self, parsed):
        self._music_call(self.relay.music_pause(self._auth_sender))

    def _post_music_skip(self, parsed):
        self._music_call(self.relay.music_skip(self._auth_sender))

    def _post_music_seek(self, parsed):
        body = self._parse_json(self._body_raw)
        try:
            position = float(body.get("position"))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "нужен position (секунды)"})
            return
        self._music_call(self.relay.music_seek(self._auth_sender, position))

    def _post_music_queue_add(self, parsed):
        body = self._parse_json(self._body_raw)
        track_id = str(body.get("track_id") or "").strip()
        if not track_id:
            self._send_json(400, {"ok": False, "error": "не указан track_id"})
            return
        self._music_call(self.relay.music_queue_add(self._auth_sender, track_id))

    def _post_music_queue_remove(self, parsed):
        body = self._parse_json(self._body_raw)
        try:
            index = int(body.get("index"))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "нужен index (с 1)"})
            return
        self._music_call(self.relay.music_queue_remove(self._auth_sender, index))

    def _post_music_ended(self, parsed):
        # Браузер сообщает: аудио-элемент дошёл до конца трека
        self._music_call(self.relay.music_ended(self._auth_sender))

    def _post_music_rescan(self, parsed):
        self._music_call(self.relay.music_rescan(self._auth_sender))

    def _post_music_settings(self, parsed):
        # POST /music/settings {music_dir?, open_dj?} — только хост
        body = self._parse_json(self._body_raw)
        music_dir = body.get("music_dir")
        open_dj = body.get("open_dj")
        if open_dj is not None:
            open_dj = bool(open_dj)
        self._music_call(self.relay.music_settings(
            self._auth_sender, music_dir=music_dir, open_dj=open_dj))

    def _post_music_api_search(self, parsed):
        # POST /music/api/search {query, provider?} — внешние библиотеки
        body = self._parse_json(self._body_raw)
        query = str(body.get("query") or "").strip()
        provider = str(body.get("provider") or "").strip()
        self._music_call(self.relay.music_search(
            self._auth_sender, query, provider))

    def _post_music_api_import(self, parsed):
        # POST /music/api/import {provider, track_id} — скачать в библиотеку
        body = self._parse_json(self._body_raw)
        provider = str(body.get("provider") or "").strip()
        track_id = str(body.get("track_id") or body.get("id") or "").strip()
        if not provider or not track_id:
            self._send_json(400, {"ok": False, "error": "нужны provider и track_id"})
            return
        self._music_call(self.relay.music_import(
            self._auth_sender, provider, track_id))

    # ── телеметрия (v2.0.5) ──────────────────────────────────────────────
    def _get_telemetry_summary(self, parsed):
        # GET /telemetry/summary — приборная панель хоста: счётчики,
        # латентности, гейджи, среда. Только агрегаты; контента тут нет
        # по построению (см. lib/telemetry.py — «три столпа»). Отдаётся
        # даже при выключенной телеметрии: хост должен видеть, что сбор
        # стоит, и что осталось в файлах до этого момента.
        snap = self.relay.get_telemetry_summary()
        status = 200 if snap.get("ok") else 503
        self._send_json(status, snap)

    def _post_telemetry_enable(self, parsed):
        # POST /telemetry/enable — включить сбор (тело не нужно).
        enabled = self.relay.set_telemetry_enabled(True)
        self._send_json(200, {"ok": True, "enabled": enabled})

    def _post_telemetry_disable(self, parsed):
        # POST /telemetry/disable — выключить сбор (тело не нужно).
        # Выбор сохраняется в telemetry/config.json и переживает рестарт.
        enabled = self.relay.set_telemetry_enabled(False)
        self._send_json(200, {"ok": True, "enabled": enabled,
                              "note": "сбор остановлен; уже записанное "
                                      "осталось в telemetry/ — удалите "
                                      "папку, если хотите стереть историю"})

    # ── демонстрация экрана (v3.6.0, модуль screen/) ─────────────────────
    def _post_screen_publish(self, parsed):
        # POST /screen/publish {on, title} — ведущий объявил/продлил/кончил.
        # on=true повторно = heartbeat живости (показ гаснет сам через
        # STREAM_LIVE_S тишины, если вкладка умерла без «стоп»).
        body = self._parse_json(self._body_raw)
        on = bool(body.get("on"))
        title = str(body.get("title") or "")[:80]
        self._send_json(200, self.relay.screen_publish(self._auth_sender, on, title))

    def _post_screen_signal(self, parsed):
        # POST /screen/signal {to, data} — релей сигнала WebRTC адресату.
        # data — непрозрачный JSON для получателя (hello/offer/answer/bye);
        # сервер его не понимает и не должен: размер/лимиты режет хаб.
        body = self._parse_json(self._body_raw)
        to = str(body.get("to") or "").strip()
        data = body.get("data")
        if not to or not isinstance(data, dict):
            self._send_json(400, {"ok": False,
                                  "error": "нужны to и data (объект)"})
            return
        self._send_json(200, self.relay.screen_signal(self._auth_sender, to, data))

    def _post_screen_watch(self, parsed):
        # POST /screen/watch {to, on} — зритель сообщил о фактическом
        # подключении к P2P-потоку (или уходе): счётчик «смотрят: N».
        body = self._parse_json(self._body_raw)
        to = str(body.get("to") or "").strip()
        on = bool(body.get("on", True))
        if not to:
            self._send_json(400, {"ok": False, "error": "нужен to"})
            return
        self._send_json(200, self.relay.screen_watch(self._auth_sender, to, on))

    # ── таблицы маршрутов ────────────────────────────────────────────────
    GET_ROUTES = {
        "/": _get_web_app,
        "/web": _get_web_app,
        "/web/": _get_web_app,
        "/events": _get_events,
        "/friends": _get_friends,
        "/leaderboard": _get_leaderboard,
        "/bots/list": _get_bots_list,
        "/server/stats": _get_server_stats,
        "/voice/info": _get_voice_info,
        "/voice/participants": _get_voice_participants,
        "/screen/info": _get_screen_info,    # (v3.6.0) экран: кто показывает
        "/screen/poll": _get_screen_poll,    # (v3.6.0) экран: сигналинг-почта
        "/voxel/info": _get_voxel_info,
        "/voxel/sessions": _get_voxel_sessions,
        "/pinned": _get_pinned,
        "/search": _get_search,              # (v3.8.0) поиск по всей истории
        "/dm/history": _get_dm_history,
        "/dm/conversations": _get_dm_conversations,
        "/channels": _get_channels,
        "/channel/messages": _get_channel_messages,
        "/files": _get_files,
        "/crypto/info": _get_crypto_info,
        "/games": _get_games,          # (v2.0.5) лаунчер игр
        "/telemetry/summary": _get_telemetry_summary,  # (v2.0.5)
        "/music/sync": _get_music_sync,  # музыка: синхронизация состояния
        "/music/library": _get_music_library,  # музыка: библиотека треков
    }
    # Префиксные маршруты проверяются в этом порядке (после точных)
    GET_PREFIX_ROUTES = (
        ("/avatar/", _get_avatar),
        ("/profile/", _get_profile),
        ("/download/", _get_download),
        ("/dm_download/", _get_dm_download),
        ("/static/", _get_static),
        ("/games/", _get_game),       # (v2.0.5) HTML самих игр
        ("/music/stream/", _get_music_stream),  # музыка: стриминг треков
    )
    POST_ROUTES = {
        "/send_text": _post_send_text,
        "/edit_text": _post_edit_text,
        "/add_reaction": _post_add_reaction,
        "/pin_message": _post_pin_message,
        "/typing": _post_typing,
        "/delete_event": _post_delete_event,
        "/avatar": _post_avatar,
        "/profile/update": _post_profile_update,
        "/friends/add": _post_friends_add,
        "/friends/remove": _post_friends_remove,
        "/channel/create": _post_channel_create,
        "/channel/send": _post_channel_send,
        "/channel/join": _post_channel_join,
        "/channel/delete": _post_channel_delete,
        "/send_file": _post_send_file,
        "/send_dm_file": _post_send_dm_file,
        "/file/have": _post_file_have,
        "/bot_command": _post_bot_command,
        "/dispatch": _post_dispatch,
        "/dm/send": _post_dm_send,
        "/voxel/create": _post_voxel_create,
        "/voxel/start": _post_voxel_start,
        "/voxel/fillbots": _post_voxel_fillbots,
        "/voxel/addbot": _post_voxel_addbot,
        "/bots/add": _post_bots_add,
        "/bots/remove": _post_bots_remove,
        "/telemetry/enable": _post_telemetry_enable,    # (v2.0.5) opt-in
        "/telemetry/disable": _post_telemetry_disable,  # (v2.0.5) opt-out
        # музыка v3.3.0: управление плеером из веб-клиента
        "/music/play": _post_music_play,
        "/music/pause": _post_music_pause,
        "/music/skip": _post_music_skip,
        "/music/seek": _post_music_seek,
        "/music/queue/add": _post_music_queue_add,
        "/music/queue/remove": _post_music_queue_remove,
        "/music/ended": _post_music_ended,
        "/music/rescan": _post_music_rescan,
        "/music/settings": _post_music_settings,
        "/music/api/search": _post_music_api_search,
        "/music/api/import": _post_music_api_import,
        # экран v3.6.0: публикация/heartbeat, сигналинг WebRTC, счётчик зрителей
        "/screen/publish": _post_screen_publish,
        "/screen/signal": _post_screen_signal,
        "/screen/watch": _post_screen_watch,
    }


def make_handler_class(relay: RelayServer) -> type[RelayHTTPHandler]:
    """Создаёт класс хендлера, привязанный к конкретному серверу.

    Подкласс на каждый экземпляр RelayServer - так два сервера в одном
    процессе (например, в тестах) не будут делить relay между собой, как
    это было бы с одним общим класс-атрибутом."""
    class BoundRelayHandler(RelayHTTPHandler):
        pass

    BoundRelayHandler.relay = relay
    return BoundRelayHandler


class QueuedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer без собственного слушающего сокета: готовые
    клиентские подключения (plain или уже TLS-обёрнутые) берёт из очереди
    TlsPlainMux. Так http и https работают на одном порту.

    ВАЖНО про serve_forever(): родительская реализация (socketserver.
    BaseServer.serve_forever) ждёт готовности `self.socket` через
    selectors.select() и только потом вызывает get_request(). Но у нас
    server_bind() ничего не делает (слушатель — у TlsPlainMux), поэтому
    self.socket — это «мёртвый», никогда не подключённый и не слушающий
    сокет. Его готовность для select() не специфицирована и зависит от
    платформы: на Linux такой сокет иногда считается «готовым» почти
    сразу (эмпирически совпадает с ошибочным/неподключённым состоянием),
    из-за чего в тестах всё работало — но на Windows (selectors там тоже
    поверх WinSock select()) тот же сокет может никогда не отмечаться
    готовым к чтению. Тогда _handle_request_noblock() не вызывается
    вообще, соединения из очереди не забираются, и раздача «висит»:
    друг видит «Не удалось подключиться: Time Out», хотя порт занят и
    TlsPlainMux исправно принимает TCP-соединения.
    Поэтому serve_forever() здесь переопределён полностью: никакого
    select() над фиктивным сокетом, просто дёргаем get_request() в
    цикле — он сам блокируется на очереди максимум 0.5с и этого
    достаточно, чтобы shutdown() мог прервать поток быстро."""

    def __init__(self, handler_class, in_queue):
        self._in_queue = in_queue
        self._stop_event = threading.Event()
        super().__init__(None, handler_class, bind_and_activate=False)

    def server_bind(self):  # слушатель принадлежит мультиплексору
        pass

    def server_activate(self):
        pass

    def get_request(self):
        # socketserver ждёт КОРТЕЖ (socket, client_address); таймаут обязателен,
        # иначе serve_forever не сможет быстро заметить сигнал остановки.
        try:
            conn = self._in_queue.get(timeout=0.5)
        except queue.Empty:
            raise TimeoutError("нет готовых подключений")  # OSError → цикл жив
        return conn, ("tls-mux", 0)

    def serve_forever(self, poll_interval=0.5):
        # См. пояснение в докстринге класса: не полагаемся на
        # selectors.select() над несвязанным self.socket — сразу зовём
        # get_request() в цикле, он сам ограничивает время ожидания.
        self._stop_event.clear()
        while not self._stop_event.is_set():
            self._handle_request_noblock()

    def shutdown(self):
        self._stop_event.set()

    def server_close(self):
        pass  # не закрываем чужой слушающий сокет

    def handle_error(self, request, client_address):
        # Обрывы клиентов (в т.ч. WS-туннелей голоса) — штатное дело, их не
        # логируем. Всё остальное (баг в хендлере, битый диск) — в журнал:
        # раньше падение хендлера исчезало без следа в stderr хоста.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
            return
        log.warning("Ошибка обработки запроса от %s: %s", client_address, exc,
                    exc_info=True)
