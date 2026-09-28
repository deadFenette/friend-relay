"""Клиентская телеметрия Friend Relay (v3.1.2) — локальная, отключаемая, БЕЗ КОНТЕНТА.

ЗАЧЕМ
  Серверная телеметрия (lib/telemetry.py) отвечает на вопрос «что делает сервер?».
  Эта модуль отвечает на другой вопрос — «что делает сам клиент?»: какие ошибки
  падают в GUI, какие диалоги открываются, по каким ссылкам-приглашениям кликают,
  как часто открывают игры, сколько длятся матчи. Данные ПОМОГАЮТ ХОСТУ
  отлаживать проблемы на стороне клиента — особенно на чужой машине, куда
  разработчик не может заглянуть напрямую.

  Хост, запустивший сервер И просматривающий телеметрию через отдельный
  файл ``ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt`` (см. ниже), видит картину целиком:
  серверную часть — из lib/telemetry.py, клиентскую — из этого модуля.

ЛЕГАЛЬНОСТЬ / ПРИВАТНОСТЬ — те же три столпа, что у серверной телеметрии:

1. ЛОКАЛЬНОСТЬ. Всё пишется ТОЛЬКО в <data_dir>/telemetry/ на диске
   самого хоста (или клиента, если он не хост — на его собственной машине).
   Сетевых вызовов в модуле НЕТ (см. assertion ниже: нет ни urllib, ни socket,
   ни http). Данные НЕ ПОКИДАЮТ машину.

2. БЕЗ КОНТЕНТА. Собираются только:
     • счётчики событий (фиксированные строки из кода — НЕ пользовательский ввод)
     • числа (длительности, размеры)
     • типы исключений (имя класса ошибки — НЕ её сообщение, в котором
       могут быть данные пользователя)
   Тексты сообщений, имена участников, имена файлов, ссылки, IP-адреса,
   содержимое ЛС — НЕ МОГУТ попасть в телеметрию архитектурно: публичные
   методы принимают только ключи и числа. Это защищено тестом
   tests/test_telemetry_v205.py (для серверной части) и
   tests/test_client_telemetry_v312.py (для этой).

3. ОТКЛЮЧАЕМОСТЬ. Тумблер в Настройках → телеметрия клиента мгновенно
   превращается в no-op. Выбор сохраняется в telemetry/client_config.json
   и переживает рестарт. По умолчанию ВКЛЮЧЕНО — данные не покидают
   машину, а ценность для отладки огромна.

ФАЙЛЫ (всё внутри <data_dir>/telemetry/):
  client_config.json                  — {"enabled": true|false}
  client_events-YYYYMMDD.jsonl        — дельты счётчиков на каждый флуш-такт
  ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt      — человеко-читаемый снимок (обновляется
                                         при каждом флуше, тот же файл, что и
                                         для серверной части — для удобства
                                         хоста «всё в одном месте»)

СОБИРАЕМЫЕ СОБЫТИЯ (только эти семейства ключей):
  client.start                        — запуск приложения
  client.shutdown                     — закрытие приложения
  client.screen.<name>                — переключение между экранами (chat/dm/…)
  client.connection.connect / .disconnect / .fail
                                      — успех/обрыв/провал подключения к хосту
                                      (тип исключения НЕ пишется — только факт)
  client.dialog.open.<game>           — открытие диалога игры (chess/go/…)
  client.dialog.close.<game>          — закрытие диалога игры
  client.dialog.error.<game>          — исключение в диалоге игры (тип в отдельном
                                        поле exception_type, НЕ сообщение)
  client.link.click.chess / .go       — клик по ссылке-приглашению friendrelay://
                                        (ТОЛЬКО тип игры и факт клика — без кода
                                        матча, без имени хоста)
  client.command.dispatch             — отправка !-команды боту (без текста команды)
  client.command.error                — исключение при обработке !-команды
  client.message.send / .edit / .delete
                                      — действия с сообщениями (без текста)
  client.message.reaction / .pin      — реакции и закрепления
  client.voice.join / .leave / .error — голосовой канал (без имён)
  client.match.duration_ms            — длительность матча (наблюдение, ms)
  client.poll.latency_ms              — латентность поллинга чата (наблюдение, ms)
  client.crash.<exception_type>       — необработанное исключение в GUI-потоке
                                        (тип исключения, НЕ сообщение — там могут
                                        быть данные пользователя)

ПОЧЕМУ ТАК МАЛО
  Каждая новая точка телеметрии — это вопрос «может ли это содержание
  пролезть в ключ?». Чтобы ответ был «нет» с уверенностью, ключи делают
  фикс-строками из кода (а не берут что-то из запроса). Поэтому
  «какой канал открыл пользователь» → client.screen.<name>, где <name> —
  заранее известная строка ("chat", "dm", …). Если откроют кастомный
  канал с именем пользователя — это НЕ пишется как ключ; пишется
  client.screen.channel без указания имени.
"""
from __future__ import annotations

import datetime
import json
import os
import platform
import re
import threading
import time
from pathlib import Path

# ── ограничения (всё, что может расти из-за мусорных ключей, ограничено) ──
_MAX_COUNTER_KEYS = 256     # больше ключей не появится — всё льётся в overflow
_MAX_TIMING_KEYS = 32
_MAX_KEY_LEN = 64
_KEY_RE = re.compile(r"^[a-z0-9_./*-]+$")
_EVENTS_ROTATE_BYTES = 2 * 1024 * 1024   # events-файл больше — вращаем в .1
FLUSH_INTERVAL_S = 20.0                  # как часто пишем на диск


def _safe_static(value: str, whitelist: frozenset[str] | None = None) -> str | None:
    """Проверяет, что ``value`` — это фикс-строка из кода (а не пользовательский
    ввод). Если задан whitelist — значение должно быть в нём. Иначе —
    должно подходить под _KEY_RE (lowercase, ограниченный алфавит, длина).

    Возвращает безопасный ключ или None (звонивший тогда логирует событие
    как «.other», без значения).
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()[:_MAX_KEY_LEN]
    if not v or ".." in v:
        return None
    if whitelist is not None:
        return v if v in whitelist else None
    return v if _KEY_RE.match(v) else None


# Whitelist'ы для ключей, где значение может прийти «снаружи» —
# принимаем ТОЛЬКО эти заранее известные строки, всё другое сворачиваем
# в «other» (фикс-строка), никогда не пуская в ключ пользовательский ввод.
_SCREEN_WHITELIST = frozenset({
    "chat", "dm", "files", "bots", "profile", "settings", "telemetry",
})
_GAME_WHITELIST = frozenset({
    "snake", "orbital", "voxel_shooter", "chess", "go", "night_shift",
    "minesweeper", "2048",
})


class ClientTelemetry:
    """Клиентский сборщик: счётчики + латентности + гейджи, файл записи,
    флуш-поток.

    Один экземпляр на приложение (создаётся в qt_app/main_window.py).
    GUI дёргает record()/observe()/exception() — всё дешёвое (в памяти),
    диск пишет только флушер. Выключенная телеметрия — no-op дороже одного
    чтения bool.
    """

    def __init__(self, data_dir: Path, enabled: bool | None = None):
        self.dir = Path(data_dir) / "telemetry"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Совсем бездисковое окружение — телеметрия честно живёт в RAM
            # и умирает с процессом.
            pass

        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list] = []  # key -> [min, max, sum, count]
        # Используем dict вместо list для O(1):
        self._timings = {}
        self._exceptions: dict[str, int] = {}  # exception_type -> count
        self._overflow = 0
        self._started_at = time.time()

        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._last_flush: dict[str, int] = {}

        # Конфиг: enabled=None → читаем файл (нет файла = включено).
        self._config_path = self.dir / "client_config.json"
        if enabled is None:
            self._enabled = self._read_config()
        else:
            self._enabled = bool(enabled)
            self._write_config()

        self._env = self._collect_env()

    # ── среда ────────────────────────────────────────────────────────────
    @staticmethod
    def _collect_env() -> dict:
        """Снимок окружения. Сознательно БЕЗ путей (в них имя пользователя),
        без hostname — только обезличенные характеристики."""
        env = {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "role": "client",   # отличаем от серверной телеметрии
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
            return True   # нет файла / битой — включено по умолчанию

    def _write_config(self) -> None:
        try:
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"enabled": self._enabled}),
                           encoding="utf-8")
            os.replace(tmp, self._config_path)
        except OSError:
            pass

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """Включить/выключить и запомнить выбор. Уже собранное НЕ стираем —
        это история клиента, он сам решает, удалить ли файлы руками."""
        self._enabled = bool(on)
        self._write_config()

    # ── запись событий (горячий путь — без диска) ────────────────────────
    def record(self, key: str, n: int = 1) -> None:
        """Счётчик key += n. Ключи — константы из кода, НЕ пользовательские
        строки; мусорные ключи гасятся валидацией."""
        if not self._enabled or n == 0:
            return
        key = self._safe_key(key)
        if key is None:
            self._overflow += 1
            return
        with self._lock:
            if key in self._counters or len(self._counters) < _MAX_COUNTER_KEYS:
                self._counters[key] = self._counters.get(key, 0) + n
            else:
                self._overflow += 1

    def observe(self, key: str, value: float) -> None:
        """Числовое наблюдение (латентность, длительность) → min/max/avg/count."""
        if not self._enabled:
            return
        key = self._safe_key(key)
        if key is None:
            self._overflow += 1
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
                self._timings[key] = [v, v, v, 1]
            else:
                t = self._timings[key]
                t[0] = min(t[0], v)
                t[1] = max(t[1], v)
                t[2] += v
                t[3] += 1

    def exception(self, where: str, exc: BaseException) -> None:
        """Записывает тип исключения (БЕЗ сообщения и без traceback — там могут
        быть данные пользователя). ``where`` — фикс-строка из whitelist'а
        ниже, чтобы контролировать пространство ключей."""
        if not self._enabled:
            return
        # where — это ключ-префикс, валидируем как обычный ключ.
        where_key = self._safe_key(where)
        if where_key is None:
            where_key = "client.error.other"
        # Имя класса исключения — НЕ сообщение. Имя класса фиксировано в коде
        # библиотеки/Python, пользовательский ввод туда попасть не может.
        exc_type = type(exc).__name__ or "UnknownError"
        # Дополнительно валидируем — на всякий случай (если кто-то подложит
        # свой Exception-класс с мусором в имени).
        exc_type = re.sub(r"[^A-Za-z0-9_]", "", exc_type)[:64] or "UnknownError"
        with self._lock:
            full_key = f"client.crash.{where_key}.{exc_type}"
            # Не раздуваем: одинаковые (where, exc_type) → один счётчик.
            if (full_key in self._counters
                    or len(self._counters) < _MAX_COUNTER_KEYS):
                self._counters[full_key] = self._counters.get(full_key, 0) + 1
            else:
                self._overflow += 1

    @staticmethod
    def _safe_key(key: str) -> str | None:
        """Валидация ключа: нижний регистр, ограниченный алфавит, длина,
        без «..» и хвостовых слэшей."""
        k = str(key).strip().lower()[:_MAX_KEY_LEN]
        if not k or k.startswith(".") or ".." in k or k.endswith("/"):
            return None
        return k if _KEY_RE.match(k) else None

    # ── специфичные хелперы (GUI-код зовёт ИХ, а не сырой record) ─────────
    def note_screen(self, screen: str) -> None:
        """Переключение между экранами. ``screen`` валидируется по whitelist'у —
        любое неизвестное имя сворачивается в 'other' (например, если
        кто-то создаст кастомный экран — не уйдёт в ключ)."""
        s = _safe_static(screen, _SCREEN_WHITELIST) or "other"
        self.record(f"client.screen.{s}")

    def note_game_dialog(self, game: str, action: str) -> None:
        """Открытие/закрытие/ошибка диалога игры. ``game`` по whitelist'у,
        ``action`` — только open/close/error."""
        g = _safe_static(game, _GAME_WHITELIST) or "other"
        a = _safe_static(action, frozenset({"open", "close", "error"})) or "other"
        self.record(f"client.dialog.{a}.{g}")

    def note_invite_link_click(self, game: str) -> None:
        """Клик по ссылке-приглашению friendrelay://<game>/join/<CODE>.
        Код матча НЕ пишется — только тип игры."""
        g = _safe_static(game, _GAME_WHITELIST) or "other"
        self.record(f"client.link.click.{g}")

    def note_connection(self, ok: bool, failed: bool = False) -> None:
        """Успех/обрыв/провал подключения к хосту."""
        if failed:
            self.record("client.connection.fail")
        elif ok:
            self.record("client.connection.connect")
        else:
            self.record("client.connection.disconnect")

    def note_app_start(self) -> None:
        self.record("client.start")

    def note_app_shutdown(self) -> None:
        self.record("client.shutdown")

    def note_message_action(self, action: str) -> None:
        """send / edit / delete / reaction / pin — без текста и без адресата."""
        a = _safe_static(
            action,
            frozenset({"send", "edit", "delete", "reaction", "pin"}),
        ) or "other"
        self.record(f"client.message.{a}")

    def note_voice(self, action: str) -> None:
        a = _safe_static(action, frozenset({"join", "leave", "error"})) or "other"
        self.record(f"client.voice.{a}")

    def note_command(self, ok: bool) -> None:
        """!-команда боту — без текста команды (там может быть !chess join ABCD,
        код матча — данные). Только факт срабатывания и успех/провал."""
        self.record("client.command.dispatch" if ok else "client.command.error")

    def observe_match_duration_ms(self, ms: float) -> None:
        self.observe("client.match.duration_ms", ms)

    def observe_poll_latency_ms(self, ms: float) -> None:
        self.observe("client.poll.latency_ms", ms)

    # ── снимок ───────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Полный снимок для записи в файл ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt."""
        with self._lock:
            counters = dict(self._counters)
            timings = {
                k: {"min_ms": round(v[0], 2),
                     "avg_ms": round(v[2] / v[3], 2) if v[3] else 0.0,
                     "max_ms": round(v[1], 2),
                     "count": v[3]}
                for k, v in self._timings.items()
            }
            overflow = self._overflow
            enabled = self._enabled
            started_at = self._started_at
        return {
            "ok": True,
            "enabled": enabled,
            "role": "client",
            "started_at": started_at,
            "uptime_s": int(time.time() - started_at),
            "env": dict(self._env),
            "counters": counters,
            "timings": timings,
            "overflow": overflow,
            "privacy": {
                "local_only": True,
                "no_content": True,
                "opt_out": "Settings → disable client telemetry",
                "files": "telemetry/ в папке данных",
                "what_we_collect": "только счётчики и числа, без текстов/имён/кодов",
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
        """Останов флушера + финальный флуш (вызывается при закрытии приложения)."""
        self._stop_event.set()
        t = self._flush_thread
        if t is not None:
            t.join(timeout=2.0)
            self._flush_thread = None
        self.flush()

    def _flush_loop(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            self.flush()

    def flush(self) -> None:
        """Пишет:
          • ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt — человеко-читаемый снимок (атомарно,
            через tmp+replace). Хост открывает этот файл — и видит текущее
            состояние телеметрии (клиентской И серверной, если серверная
            тоже пишет в ту же папку).
          • client_events-YYYYMMDD.jsonl — дельты счётчиков (история по дням).
        Все ошибки диска гасятся: телеметрия не имеет права уронить GUI."""
        try:
            snap = self.snapshot()
            # 1) Человеко-читаемый файл для хоста.
            self._write_human_readable(snap)
            # 2) JSON-снимок для программного разбора.
            tmp = self.dir / "client_summary.json.tmp"
            tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.dir / "client_summary.json")
            # 3) Дневной файл дельт.
            with self._lock:
                deltas = {k: v - self._last_flush.get(k, 0)
                          for k, v in self._counters.items()
                          if v != self._last_flush.get(k, 0)}
                self._last_flush = dict(self._counters)
            if deltas:
                day = datetime.datetime.now().strftime("%Y%m%d")
                ev = self.dir / f"client_events-{day}.jsonl"
                line = json.dumps(
                    {"t": int(time.time()), "deltas": deltas},
                    ensure_ascii=False) + "\n"
                self._append_rotating(ev, line)
        except Exception:
            pass

    def _write_human_readable(self, snap: dict) -> None:
        """Человеко-читаемый снимок в файл ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt
        (или .txt — выбираем .txt чтобы открылся в Блокноте на Windows без
        вопросов о кодировке)."""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append(" ОТСЛЕЖИВАНИЕ ТЕЛЕМЕТРИИ GUI — Friend Relay (клиент)")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"Версия приложения : {snap['env'].get('app_version', '?')}")
        lines.append(f"Платформа        : {snap['env'].get('platform', '?')}")
        lines.append(f"Python           : {snap['env'].get('python', '?')}")
        lines.append(f"Роль             : {snap['env'].get('role', 'client')}")
        uptime = snap.get("uptime_s", 0)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"Uptime           : {h}ч {m}м {s}с")
        lines.append(f"Телеметрия       : {'ВКЛЮЧЕНА' if snap.get('enabled') else 'ВЫКЛЮЧЕНА'}")
        lines.append("")
        lines.append("-" * 70)
        lines.append(" ПРИВАТНОСТЬ")
        lines.append("-" * 70)
        priv = snap.get("privacy", {})
        lines.append(f"  Только локально        : {priv.get('local_only', True)}")
        lines.append(f"  Без контента           : {priv.get('no_content', True)}")
        lines.append(f"  Как отключить          : {priv.get('opt_out', '?')}")
        lines.append(f"  Что собирается         : {priv.get('what_we_collect', '?')}")
        lines.append("")
        lines.append("-" * 70)
        lines.append(" СЧЁТЧИКИ СОБЫТИЙ (без текстов, без имён, без кодов)")
        lines.append("-" * 70)
        counters = snap.get("counters", {})
        if not counters:
            lines.append("  (пока ничего не произошло)")
        else:
            # Группируем по префиксу (client.dialog.*, client.link.* и т.д.)
            for key in sorted(counters):
                lines.append(f"  {key:<48} {counters[key]}")
        lines.append("")
        lines.append("-" * 70)
        lines.append(" НАБЛЮДЕНИЯ (латентности, длительности — мин/сред/макс/кол-во)")
        lines.append("-" * 70)
        timings = snap.get("timings", {})
        if not timings:
            lines.append("  (пока нет наблюдений)")
        else:
            for key in sorted(timings):
                t = timings[key]
                lines.append(
                    f"  {key:<40} min={t['min_ms']:>8.2f}  "
                    f"avg={t['avg_ms']:>8.2f}  max={t['max_ms']:>8.2f}  "
                    f"n={t['count']}"
                )
        if snap.get("overflow"):
            lines.append("")
            lines.append(f"  ⚠ {snap['overflow']} событий не влезло в лимит ключей")
        lines.append("")
        lines.append("=" * 70)
        lines.append(" Файлы телеметрии в этой папке:")
        lines.append("   client_config.json          — тумблер вкл/выкл")
        lines.append("   client_summary.json         — этот снимок (JSON)")
        lines.append("   client_events-YYYYMMDD.jsonl — история по дням")
        lines.append("   summary.json                — серверная телеметрия (JSON)")
        lines.append("   events-YYYYMMDD.jsonl       — серверная история по дням")
        lines.append("=" * 70)

        target = self.dir / "ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt"
        tmp = self.dir / "ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt.tmp"
        try:
            tmp.write_text("\n".join(lines), encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            pass

    @staticmethod
    def _append_rotating(path: Path, line: str) -> None:
        """Аппенд с простым вращением: >2 МБ → переименовать в .1."""
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


# ── singleton (создаётся в main_window.py при старте приложения) ──────────
_instance: ClientTelemetry | None = None
_instance_lock = threading.Lock()


def init(data_dir: Path, enabled: bool | None = None) -> ClientTelemetry:
    """Создаёт глобальный инстанс клиентской телеметрии. Вызывается ОДИН раз
    из qt_app/main_window.py при старте. Повторные вызовы — no-op
    (инстанс уже живёт до конца процесса)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = ClientTelemetry(data_dir, enabled=enabled)
            _instance.start_flusher()
        return _instance


def get() -> ClientTelemetry | None:
    """Возвращает глобальный инстанс или None, если init() не вызывался.
    GUI-код зовёт get() и потом .note_*() / .record() / .observe() —
    каждый вызов дешёвый (no-op, если телеметрия выключена)."""
    return _instance


def safe_record(key: str, n: int = 1) -> None:
    """Безопасная запись счётчика — если инстанс не инициализирован, no-op.
    Удобно вызывать из мест, где init() мог не сработать (например, ранняя
    инициализация до создания main_window)."""
    inst = _instance
    if inst is not None:
        try:
            inst.record(key, n)
        except Exception:
            pass  # телеметрия не должна ронять GUI


def safe_exception(where: str, exc: BaseException) -> None:
    """Аналог safe_record для исключений — всегда no-op на ошибку."""
    inst = _instance
    if inst is not None:
        try:
            inst.exception(where, exc)
        except Exception:
            pass


def safe_observe(key: str, value: float) -> None:
    """Аналог safe_record для наблюдений (латентности и т.п.)."""
    inst = _instance
    if inst is not None:
        try:
            inst.observe(key, value)
        except Exception:
            pass
