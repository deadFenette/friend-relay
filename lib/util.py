from __future__ import annotations

import re
import threading
import time
from collections import deque
from pathlib import Path

"""HTTP-заголовки по спеке - latin-1, а у нас кириллические имена и имена
файлов. Стандартный трюк: с одной стороны кодируем UTF-8-байты как строку
latin-1 (тогда urllib/http.client спокойно всё передаёт байт-в-байт), с
другой - декодируем обратно. Без этого 'Вася' долетит до собеседника
кракозябрами."""

_SAFE_NAME_RE = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9_\-.# ]+")
# '#' разрешён (v3.3.2): дискриминаторы имён «Жуж#2» как в Discord.
# Символ безопасен для файловых имён (нет path-семантики), а без него
# safe_name() молча превращал «Жуж#2» в «Жуж2» — профили/аватары/DM
# разъезжались бы с настоящим именем.


def header_encode(value: str) -> str:
    return value.encode("utf-8", "replace").decode("latin-1", "replace")


def header_decode(value: str) -> str:
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def safe_name(name: str, limit: int = 64) -> str:
    """Имя человека -> безопасное имя файла (никаких '..' и path-разделителей,
    даже если имя пришло из недоверенного заголовка).

    Раньше функция называлась _safe_name и жила в relay_server.py, из-за чего
    channel_manager/profile_manager были вынуждены импортировать приватный
    символ из фасада (сервис зависел от сборщика зависимостей - перевёрнутая
    иерархия). Теперь это обычная утилита слоя домена."""
    cleaned = _SAFE_NAME_RE.sub("", name).strip()
    return cleaned[:limit]


# ── общие мелкие утилиты (v2.0.2) ─────────────────────────────────────────
# Живут здесь, а не в http_api/transport, потому что нужны ДВУМ слоям:
# транспорт парсит заголовки и JSON-поля, домен клампит лимиты. Правило
# слоёв (ARCHITECTURE.md): утилита общего назначения не может жить в
# transport, иначе домен зависел бы от транспорта.


def parse_int(value, default: int = 0) -> int | None:
    """Безопасный int из недоверенного входа (заголовки, JSON-поля).

    Возвращает:
      default - значение пустое/не указано ("" или None): «не задано»
                легитимно, значит берём умолчание;
      None    - значение ЕСТЬ, но это не число (мусор) - вызывающий
                отвечает 400, а не молча подставляет умолчание;
      int     - всё хорошо.

    Раньше эта семантика дублировалась методами _header_int и _int_arg в
    http_api.py (и до v1.9.9 - вообще голым int() с 500 на мусор). Теперь
    одна точка правды: транспорт делегирует сюда, тесты гоняют сюда."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clamp(value: int, lo: int, hi: int) -> int:
    """Зажимает value в [lo, hi]. Классика для лимитов запросов
    (limit=?count=... - раньше каждый маршрут клампил своими max/min)."""
    return max(lo, min(value, hi))


class MtimeFileCache:
    """Потокобезопасный кеш «файл -> байты» с проверкой по (mtime, size).

    Зачем: веб-клиент просит / и /static/*.css|js при каждой загрузке
    страницы у КАЖДОГО друга, а транспорт до v2.0.2 читал файл с диска на
    каждый запрос. Правка разработчиком подхватывается честно: ключ
    инвалидации - (st_mtime_ns, st_size), изменился файл = перечитали,
    не изменился = отдаём из RAM. То же поведение «без рестарта», что и
    при чтении каждый раз, только без дискового I/O на горячем пути.

    Только мелкая статика (JS/CSS/HTML); для файлов витрины он не нужен -
    там у клиента свои прогресс-бары и Range-запросы мимо кеша."""

    def __init__(self, max_entries: int = 64):
        # 64 записи с запасом покрывают весь web_client/ (десяток модулей);
        # вытеснение - простейший FIFO через dict (Python 3.7+ помнит порядок).
        self._max = max_entries
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple[int, int], bytes]] = {}

    def get(self, path: Path) -> bytes | None:
        """Байты файла или None, если файла нет/не читается (OSError).
        Вызывающий переводит None в 404, как раньше при not exists()."""
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        try:
            data = path.read_bytes()
        except OSError:
            return None
        with self._lock:
            if len(self._cache) >= self._max and path not in self._cache:
                # FIFO-вытеснение: удаляем самую старую запись
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[path] = (stamp, data)
        return data

    def invalidate(self, path: Path | None = None) -> None:
        """Ручная инвалидация (одного файла или всего кеша) - на случай,
        если кто-то меняет файл с сохранением mtime/size (не бывает, но
        пусть будет инструмент)."""
        with self._lock:
            if path is None:
                self._cache.clear()
            else:
                self._cache.pop(path, None)


# ── служебные структуры ядра (v2.0.4) ─────────────────────────────────────
# Ничего внешнего, только stdlib, и ничего доменного: это «кирпичи» для
# горячих точек сервера (кеш превью, антифлуд, метрики /server/stats).
# Живут в util.py по тому же правилу слоёв, что и parse_int/MtimeFileCache:
# нужны ДВУМ слоям сразу (link_preview в application, транспорт в server/).
# Таймеры - только time.monotonic(): перевод часов не должен «омолаживать»
# записи кеша и ведра антифлуда.


class TTLCache:
    """Потокобезопасный кеш «ключ -> значение» с временем жизни записи.

    Зачем: повторяющиеся дорогие операции (скачивание страницы для превью
    ссылки) нельзя кешировать навечно, но и повторять на каждый чих тоже.
    TTL решает обе стороны: свежий результат отдаётся из RAM, устаревший
    честно пересчитывается. Вытеснение при переполнении - LRU через dict
    (Python 3.7+ помнит порядок вставки, перезапись обновляет свежесть) -
    тот же приём, что в MtimeFileCache и RateLimiter, чтобы не плодить
    разные политики в одном проекте.

    Кеш хранит только УСПЕШНЫЕ результаты (решение вызывающего: None
    просто не кладётся) - неудачная попытка не «запоминается на 10 минут».
    """

    def __init__(self, max_entries: int = 128, ttl: float = 600.0):
        self._max = max(1, max_entries)
        self._ttl = max(0.0, float(ttl))
        self._lock = threading.Lock()
        # key -> (expires_at, value); expires_at по monotonic - перевод
        # системных часов не должен «омолаживать» или убивать записи.
        self._data: dict = {}

    def get(self, key):
        """Значение или None, если ключа нет / запись протухла. Протухшая
        запись удаляется лениво (при чтении) - отдельный поток-уборщик
        не нужен, запись всё равно никому не мешает."""
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires_at, value = hit
            if now >= expires_at:
                del self._data[key]
                return None
            return value

    def put(self, key, value) -> None:
        now = time.monotonic()
        with self._lock:
            if key in self._data:
                # Перезапись = свежесть: обновлённый ключ идёт в конец
                # очереди вытеснения (LRU), а не умирает первым как
                # «самый старый» — его значение только что было полезно.
                del self._data[key]
            elif len(self._data) >= self._max:
                # Вытеснение: удаляем самую старую запись (первую в
                # порядке вставки — dict помнит порядок).
                self._data.pop(next(iter(self._data)), None)
            self._data[key] = (now + self._ttl, value)

    def __len__(self) -> int:
        # Включает протухшие, но ещё не вытолкнутые записи - это счётчик
        # памяти, а не «сколько живых значений»; для диагностики хватит.
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class TokenBucket:
    """Классическое токен-ведро (rate limiting): вёрдро на burst токенов,
    восполняется со скоростью rate токенов/сек.

    Зачем: флуд - это не «слишком много сообщений вообще», а «слишком
    быстро подряд». Ведро пропускает нормальный всплеск человека (встал
    из-за стола, ответил на пять реплик разом) и режет именно поток
    (скрипт, шлющий 100 сообщений в секунду, жжёт CPU хоста, диск JSONL
    и сеть всех поллящих клиентов). monotonic - как в TTLCache.
    """

    def __init__(self, rate: float, burst: float):
        self._rate = max(0.001, float(rate))  # защита от деления на ноль
        self._burst = max(1.0, float(burst))
        self._tokens = self._burst  # стартуем с полным ведром
        self._ts = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._burst,
                           self._tokens + (now - self._ts) * self._rate)
        self._ts = now

    def take(self, tokens: float = 1.0) -> bool:
        """Снимает tokens из ведра; False - не хватает (запрос режем)."""
        with _BUCKET_LOCK:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def retry_after(self, tokens: float = 1.0) -> float:
        """Сколько секунд ждать до следующего успешного take(tokens)."""
        with _BUCKET_LOCK:
            self._refill()
            missing = tokens - self._tokens
            return max(0.0, missing / self._rate)


# Глобальный лок для ВСЕХ вёдер: вёдер мало (по одному на отправителя),
# contention ничтожный, а один лок вместо лока на экземпляр - меньше
# аллокаций и ровно та же корректность.
_BUCKET_LOCK = threading.Lock()


class RateLimiter:
    """Набор токен-вёдер по ключу (имя отправителя) с LRU-вытеснением.

    Зачем отдельный класс: транспорт не должен сам вести словарь вёдер -
    это политика фасада (RelayServer.allow_message), а тут - механизм.
    LRU-вытеснение (dict помнит порядок, свежий ключ переставляется в
    конец) не даёт словарю расти вечно даже при спаме случайными именами
    (случай /ping случайными именами уже закрывался - см. issue_session).
    """

    def __init__(self, rate: float, burst: float, max_keys: int = 512):
        self._rate = rate
        self._burst = burst
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket(self, key: str) -> TokenBucket:
        # Вызывается ТОЛЬКО под self._lock (см. методы ниже).
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._rate, self._burst)
            self._buckets[key] = bucket
        # LRU: свежий ключ переставляем в конец (del+set дешевле, чем
        # кажется - это два словарных присваивания без реаллокации).
        self._buckets.pop(key, None)
        self._buckets[key] = bucket
        if len(self._buckets) > self._max_keys:
            # Выталкиваем самый старый (первый в порядке вставки)
            self._buckets.pop(next(iter(self._buckets)), None)
        return bucket

    def allow(self, key: str) -> bool:
        """Разрешён ли этому ключу ещё один «сообщение-токен»."""
        with self._lock:
            return self._bucket(key).take()

    def retry_after(self, key: str) -> float:
        """Сколько секунд этому ключу ждать (для поля retry_after в 429)."""
        with self._lock:
            return self._bucket(key).retry_after()

    def active(self) -> int:
        """Сколько ключей живёт сейчас (для /server/stats)."""
        with self._lock:
            return len(self._buckets)


class LatencyMeter:
    """Счётчик запросов + кольцевой буфер последних замеров латентности.

    Зачем: /server/stats до сих пор отвечал «жив ли сервер», но не «как
    ему дышится» - среднее и p95 последних запросов показывают деградацию
    (диск, сеть, друг-флудер) раньше, чем она станет заметной в чате.
    Окно фиксированное (deque с maxlen) - память константная, а сортировка
    окна на snapshot() стоит копейки, потому что stats() зовут редко
    (человек открыл страницу), в отличие от самих замеров (каждый запрос).
    """

    def __init__(self, window: int = 256):
        self._lock = threading.Lock()
        self._samples: deque[float] = deque(maxlen=max(1, window))
        self._total = 0  # все запросы с старта, не только окно

    def note(self, elapsed_ms: float) -> None:
        """Отметить один завершённый запрос (ms). Отрицательное - мусор,
        молча игнорируем (статистика не должна падать/врать)."""
        if elapsed_ms < 0:
            return
        with self._lock:
            self._total += 1
            self._samples.append(float(elapsed_ms))

    def snapshot(self) -> dict:
        """Словарь для /server/stats: requests_total, avg_ms, p95_ms.
        p95 = (ceil(0.95*n)-1)-й элемент отсортированного окна - без
        интерполяции, зато стабильно и без numpy."""
        with self._lock:
            samples = list(self._samples)
            total = self._total
        if not samples:
            return {"requests_total": total, "avg_ms": 0.0, "p95_ms": 0.0}
        samples.sort()
        p95 = samples[max(0, int(len(samples) * 0.95 + 0.999) - 1)]
        return {
            "requests_total": total,
            "avg_ms": round(sum(samples) / len(samples), 2),
            "p95_ms": round(p95, 2),
        }


def fmt_uptime(seconds: int) -> str:
    """Секунды -> короткая человекочитаемая строка: «3д 2ч 5м», «2ч 5м»,
    «5м 20с», «42с». Для /server/stats (uptime_human) - хост смотрит в
    браузере и не должен в уме переводить 185492 секунды. Отрицательное
    и мусорное трактуем как 0 (статистика не должна ронять страницу)."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs or not parts:  # «0с» лучше пустой строки
        parts.append(f"{secs}с")
    return " ".join(parts)
