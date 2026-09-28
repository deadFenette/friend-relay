"""Телеметрия Friend Relay (v2.0.5) — локальная, отключаемая, без контента.

ЧТО ЭТО
Самонаблюдение хоста: счётчики событий (сообщения, файлы, входы в голос),
латентности HTTP, пики онлайна. Приборная панель ДЛЯ ХОСТА — смотреть через
GET /telemetry/summary или просто открыть telemetry/summary.json в папке
данных. Слово «телеметрия» здесь означает «сервер меряет сам себя», а НЕ
«отправляет данные кому-то»: сетевых вызовов в модуле нет вообще.

ЛЕГАЛЬНОСТЬ / ПРИВАТНОСТЬ — три столпа (подробно в README, раздел
«Телеметрия и приватность»):

1. ЛОКАЛЬНОСТЬ. Всё пишется ТОЛЬКО в <data_dir>/telemetry/ на диске
   самого хоста. В модуле нет ни одного обращения к сети (это сознательное
   ограничение архитектуры: нет ни urllib, ни socket — только файлы).
   Данные обрабатываются хозяином у себя дома для своих же нужд —
   никакого третьего лица в цепочке нет.

2. БЕЗ КОНТЕНТА. Собираются только числа: счётчики, размеры, длительности.
   Тексты сообщений, имена участников, имена файлов, ссылки, содержимое
   ЛС попасть в телеметрию НЕ МОГУТ архитектурно: публичные методы
   принимают только ключи событий (фиксированные строки из кода) и числа.
   Имена никогда не используются в ключах — маршруты маскируются
   (route_key: /avatar/Вася → /avatar/*). Это защищено тестом
   (tests/test_telemetry_v205.py): уникальная строка, отправленная в чат,
   не встречается ни в одном файле телеметрии.

3. ОТКЛЮЧАЕМОСТЬ. POST /telemetry/disable — и запись замирает мгновенно
   (счётчики в памяти больше не пополняются, фlush пишет «enabled: false»).
   Выбор сохраняется в telemetry/config.json и переживает рестарт.
   По умолчанию включено: данные не покидают машину хоста, а ценность
   («что происходит с моим сервером») сразу видна; но выключение — один
   запрос, без правок кода.

ФАЙЛЫ (всё внутри <data_dir>/telemetry/):
  config.json                      — {"enabled": true|false}
  summary.json                     — последний снимок snapshot() (атомарно)
  events-YYYYMMDD.jsonl            — дельты счётчиков на каждый флуш-такт
                                     (история по дням; >5 МБ → вращение в .1)

СОБИРАЕМЫЕ СОБЫТИЯ (конвенции ключей — только эти семейства):
  http.route.<маршрут>             — запросы по маршрутам (id/имена маскируются)
  http.status.<код>                — ответы по кодам (200/403/429/500…)
  http.latency_ms                  — наблюдения длительности (min/avg/max/count)
  http.errors.<ТипИсключения>      — необработанные падения хендлеров
  http.flood_blocked               — срабатывания антифлуда (429)
  session.created                  — выдачи session_token (/ping)
  session.kind.<browser|python>    — те же входы по типу клиента (по User-Agent)
  chat.text / chat.text.encrypted  — сообщения общего чата
  chat.edited / chat.deleted       — правки и удаления
  chat.reactions / chat.pins       — реакции и закрепления
  chat.typing / channel.messages   — «печатает…» и сообщения каналов
  channel.created / channel.deleted
  dm.sent / dm.sent.encrypted / dm.files — ЛС (только факт, не адресат)
  file.uploaded / file.uploaded_bytes / file.downloaded / file.have
  avatar.set
  voice.join / voice.leave         — входы/выходы голосового канала (без имён)
  voxel.created / voxel.started / voxel.fillbots / voxel.addbot
  bot.commands.<bot_id>            — команды ботам (шахматы, змейка, …)
  games.list / games.opened.<id>   — открытие веб-игр
  server.online_now / server.online_peak — гейджи онлайна (сейчас/пик)

Потокобезопасность: все записи под одним Lock — O(1) без диска;
диск трогает только флуш-поток раз в FLUSH_INTERVAL_S (и stop()).
"""
from __future__ import annotations

import datetime
import json
import platform
import re
import threading
import time
from pathlib import Path

# ── ограничения (всё, что может расти из-за мусорных ключей, ограничено) ──
_MAX_COUNTER_KEYS = 512     # больше ключей не появится — всё льётся в overflow
_MAX_TIMING_KEYS = 64
_MAX_GAUGE_KEYS = 64
_MAX_KEY_LEN = 64
_KEY_RE = re.compile(r"^[a-z0-9_./*-]+$")
_EVENTS_ROTATE_BYTES = 5 * 1024 * 1024   # events-файл больше — вращаем в .1
FLUSH_INTERVAL_S = 15.0                  # как часто пишем на диск

# Точные маршруты без пользовательского хвоста — они проходят в
# телеметрию как есть (это имена обработчиков, не данные). Всё,
# где хвост = id/имя, описано в _PREFIX_ROUTES ниже; любой ДРУГОЙ
# многосегментный путь сворачивается в "/<первый сегмент>/*" —
# пространство ключей остаётся ограниченным даже под спамом.
_EXACT_ROUTES = frozenset({
    "/", "/web", "/web/", "/ping", "/events", "/friends", "/leaderboard",
    "/bots/list", "/server/stats", "/voice/info", "/voice/participants",
    "/voxel/info", "/voxel/sessions", "/pinned", "/dm/history",
    "/dm/conversations", "/channels", "/channel/messages", "/files",
    "/crypto/info", "/games", "/telemetry/summary",
    "/send_text", "/edit_text", "/add_reaction", "/pin_message",
    "/typing", "/delete_event", "/avatar", "/profile/update",
    "/friends/add", "/friends/remove", "/channel/create", "/channel/send",
    "/channel/join", "/channel/delete", "/send_file", "/send_dm_file",
    "/file/have", "/bot_command", "/dispatch", "/dm/send", "/voxel/create",
    "/voxel/start", "/voxel/fillbots", "/voxel/addbot", "/bots/add",
    "/bots/remove", "/telemetry/enable", "/telemetry/disable",
})

# Маршруты-шаблоны, у которых хвост — данные (id/имя), в телеметрию он
# не допускается: ключ обрезается до префикса со «*» (без самих данных).
_PREFIX_ROUTES = (
    "/avatar/", "/profile/", "/download/", "/dm_download/",
    "/static/", "/games/",
)


class Telemetry:
    """Счётчики + латентности + гейджи, файл записи, флуш-поток.

    Создаётся фасадом (RelayServer) один раз на сервер; транспорт и
    домен дёргают record()/observe()/set_gauge() — всё дешёвое (в памяти),
    диск пишет только флушер. Выключенная телеметрия — это no-op дороже
    одного чтения bool.
    """

    def __init__(self, data_dir: Path, enabled: bool | None = None):
        self.dir = Path(data_dir) / "telemetry"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Совсем бездисковое окружение — телеметрия честно живёт в RAM
            # и умирает с процессом (флуш сам поймает OSError и затихнет).
            pass

        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list] = {}   # key -> [min, max, sum, count]
        self._gauges: dict[str, dict] = {}    # key -> {"v": last, "max": peak}
        self._overflow = 0                    # не поместившихся записей
        self._started_at = time.time()

        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._last_flush: dict[str, int] = {}  # счётчик на прошлый флуш (дельты)

        # Конфиг: enabled=None → читаем файл (нет файла = включено).
        self._config_path = self.dir / "config.json"
        if enabled is None:
            self._enabled = self._read_config()
        else:
            self._enabled = bool(enabled)
            self._write_config()

        # Статическая среда (один раз): что за машина/сборка. Без путей и
        # имён пользователя — только система/архитектура/версия Python.
        self._env = self._collect_env()

    # ── среда ────────────────────────────────────────────────────────────
    @staticmethod
    def _collect_env() -> dict:
        """Снимок окружения хоста. Сознательно БЕЗ путей (в них имя
        пользователя), без hostname — только обезличенные характеристики."""
        env = {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
        }
        try:
            vj = Path(__file__).resolve().parent.parent / "version.json"
            env["app_version"] = (json.loads(
                vj.read_text(encoding="utf-8")).get("version") or "?")
        except Exception:
            env["app_version"] = "?"
        return env

    # ── конфиг вкл/выкл ──────────────────────────────────────────────────
    def _read_config(self) -> bool:
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            return bool(data.get("enabled", True))
        except (OSError, ValueError):
            return True   # нет файла / битый — включено по умолчанию

    def _write_config(self) -> None:
        """Атомарная запись конфига (как _save_data у ботов: tmp + replace,
        обрыв питания не оставляет битый JSON)."""
        try:
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"enabled": self._enabled}),
                           encoding="utf-8")
            import os
            os.replace(tmp, self._config_path)
        except OSError:
            pass

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """Включить/выключить и запомнить выбор. Уже собранное НЕ стираем —
        это история хоста, он сам решает, удалить ли файлы руками."""
        self._enabled = bool(on)
        self._write_config()

    # ── запись событий (горячий путь — без диска) ────────────────────────
    def record(self, key: str, n: int = 1) -> None:
        """Счётчик key += n. Ключи — константы из кода (см. докстринг),
        НЕ пользовательские строки; мусорные ключи гасятся валидацией."""
        if not self._enabled or n == 0:
            return
        key = self._safe_key(key)
        if key is None:
            return
        with self._lock:
            if key in self._counters or len(self._counters) < _MAX_COUNTER_KEYS:
                self._counters[key] = self._counters.get(key, 0) + n
            else:
                self._overflow += 1

    def observe(self, key: str, value: float) -> None:
        """Числовое наблюдение (латентность, размер) → min/max/avg/count."""
        if not self._enabled:
            return
        key = self._safe_key(key)
        if key is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            if key not in self._timings:
                if len(self._timings) >= _MAX_TIMING_KEYS:
                    self._overflow += 1
                    return
                self._timings[key] = [v, v, v, 1]   # min, max, sum, count
            else:
                t = self._timings[key]
                t[0] = min(t[0], v)
                t[1] = max(t[1], v)
                t[2] += v
                t[3] += 1

    def set_gauge(self, key: str, value: float) -> None:
        """«Текущее значение» + автоматический пик (max) рядом: ключ и
        ключ.peak. Пик живёт до рестарта — этого достаточно для «сколько
        народа бывало»."""
        if not self._enabled:
            return
        key = self._safe_key(key)
        if key is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            if key not in self._gauges and len(self._gauges) >= _MAX_GAUGE_KEYS:
                self._overflow += 1
                return
            g = self._gauges.setdefault(key, {"v": v, "peak": v})
            g["v"] = v
            g["peak"] = max(g["peak"], v)

    @staticmethod
    def _safe_key(key: str) -> str | None:
        """Валидация ключа: нижний регистр, ограниченный алфавит, длина,
        без «..» и хвостовых слэшей (пути вида ../etc не должны пролезать
        даже в память). Ключи рождаются в нашем коде, но последняя линия
        обороны не помешает (например, bot.commands.<bot_id> приходит
        из запроса)."""
        k = str(key).strip().lower()[:_MAX_KEY_LEN]
        if not k or k.startswith(".") or ".." in k or k.endswith("/"):
            return None
        return k if _KEY_RE.match(k) else None

    # ── нормализация маршрутов (приватность) ─────────────────────────────
    @staticmethod
    def route_key(path: str) -> str:
        """URL-путь → ключ маршрута без пользовательских данных:
        /avatar/Вася → /avatar/*, /download/abc123 → /download/*,
        /games/minesweeper → /games/*, /ping → /ping,
        /telemetry/summary → /telemetry/summary (точный маршрут = имя
        обработчика, данных в нём нет). Итог валидируется _safe_key,
        мусор сворачивается в "/<первый сегмент>/*" или /other."""
        p = str(path or "/").split("?", 1)[0]
        if p in _EXACT_ROUTES:
            return p
        for pref in _PREFIX_ROUTES:
            if p.startswith(pref):
                return pref + "*"
        # неизвестный многосегментный путь: оставляем только первый
        # сегмент — спам случайными путями не раздувает словарь ключей
        key = "/" + (p.strip("/").split("/", 1)[0] or "") + "/*"
        return Telemetry._safe_key(key) or "/other"

    # ── снимок ───────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Полный снимок для /telemetry/summary и summary.json. Копии —
        чтобы снапшот нельзя было менять на лету из других потоков."""
        with self._lock:
            counters = dict(self._counters)
            timings = {
                k: {"min_ms": round(v[0], 2), "avg_ms": round(v[2] / v[3], 2),
                    "max_ms": round(v[1], 2), "count": v[3]}
                for k, v in self._timings.items()
            }
            gauges = {k: dict(v) for k, v in self._gauges.items()}
            overflow = self._overflow
            enabled = self._enabled
            started_at = self._started_at
        return {
            "ok": True,
            "enabled": enabled,
            "started_at": started_at,
            "uptime_s": int(time.time() - started_at),
            "env": dict(self._env),
            "counters": counters,
            "timings": timings,
            "gauges": gauges,
            "overflow": overflow,
            # честная подпись модуля: что ЭТО и что тут НИКОГДА не будет
            "privacy": {
                "local_only": True,
                "no_content": True,
                "opt_out": "POST /telemetry/disable",
                "files": "telemetry/ в папке данных хоста",
            },
        }

    # ── флуш на диск ─────────────────────────────────────────────────────
    def start_flusher(self, interval: float = FLUSH_INTERVAL_S) -> None:
        if self._flush_thread is not None:
            return
        self._stop_event.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, args=(interval,), daemon=True)
        self._flush_thread.start()

    def stop_flusher(self) -> None:
        """Останов флушера + финальный флуш (вызывается из relay.stop())."""
        self._stop_event.set()
        t = self._flush_thread
        if t is not None:
            t.join(timeout=2.0)
            self._flush_thread = None
        self.flush()   # финальные дельты не теряются при остановке хоста

    def _flush_loop(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            self.flush()

    def flush(self) -> None:
        """Пишет summary.json (полный снимок, атомарно) и добавляет дельты
        счётчиков в дневной events-файл. Все ошибки диска гасятся: телеметрия
        не имеет права уронить сервер."""
        try:
            snap = self.snapshot()
            # 1) summary.json — последний снимок целиком
            tmp = self.dir / "summary.json.tmp"
            tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            import os
            os.replace(tmp, self.dir / "summary.json")
            # 2) дневной файл — ТОЛЬКО дельты с прошлого флуша (файл растёт
            #    пропорционально активности, а не времени; выключенные
            #    счётчики дельт не дают)
            with self._lock:
                deltas = {k: v - self._last_flush.get(k, 0)
                          for k, v in self._counters.items()
                          if v != self._last_flush.get(k, 0)}
                self._last_flush = dict(self._counters)
            if deltas:
                day = datetime.datetime.now().strftime("%Y%m%d")
                ev = self.dir / f"events-{day}.jsonl"
                line = json.dumps(
                    {"t": int(time.time()), "deltas": deltas},
                    ensure_ascii=False) + "\n"
                self._append_rotating(ev, line)
        except Exception:
            pass   # диск мог исчезнуть — сервер важнее телеметрии

    @staticmethod
    def _append_rotating(path: Path, line: str) -> None:
        """Аппенд с простым вращением: >5 МБ → переименовать в .1 (старый
        .1 затирается). Достаточно для дневного файла: сутки никогда не
        дадут 5 МБ счётчиков (тысячи сообщений = килобайты дельт)."""
        try:
            if path.exists() and path.stat().st_size > _EVENTS_ROTATE_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
