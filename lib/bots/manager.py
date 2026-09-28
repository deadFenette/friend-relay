from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from lib.bots.base import BaseBot

# (v3.5.0) Пауза перед перезапуском упавшего бота: 1-е падение — через 5с,
# 2-е — 30с, далее — каждые 120с (чтобы вечнобагнутый бот не крутил
# бесконечный цикл падений). Счётчик падений живёт на самом боте.
RESTART_BACKOFF_S = (5, 30, 120)

log = logging.getLogger("friendrelay.bots")


class BotManager:
    """Менеджер ботов - добавление, удаление, маршрутизация команд."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.bots_dir = data_dir / "bots"
        self.bots_dir.mkdir(parents=True, exist_ok=True)

        self._bots: dict[str, BaseBot] = {}
        self._lock = threading.Lock()
        # (v3.5.0) остановка менеджера: после shutdown() ботов перезапускать нельзя
        self._stopping = False
        # (v3.5.0) активные таймеры перезапуска — отменяем в shutdown()
        self._restart_timers: set[threading.Timer] = set()
        self._config_path = self.bots_dir / "config.json"
        self._load_config()

    def _load_config(self) -> None:
        """Загружает конфигурацию ботов из файла."""
        if not self._config_path.exists():
            return
        try:
            json.loads(self._config_path.read_text(encoding="utf-8"))
            # Боты будут загружаться по запросу через register_bot()
        except (json.JSONDecodeError, OSError):
            pass

    def _save_config(self) -> None:
        """Сохраняет конфигурацию ботов в файл."""
        config = {"bots": list(self._bots.keys())}
        try:
            self._config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def register_bot(self, bot: BaseBot) -> bool:
        """Регистрирует бота в системе.

        Args:
            bot: экземпляр бота

        Returns:
            True если бот успешно зарегистрирован, False если уже существует
        """
        with self._lock:
            if bot.bot_id in self._bots:
                return False
            # (v3.5.0) колбэк краша ставим ДО старта: падение в первые
            # секунды тоже должно привести к перезапуску.
            bot._on_crash = self._schedule_restart
            self._bots[bot.bot_id] = bot
            bot.start()
            self._save_config()
            return True

    def register_bot_by_id(self, bot_id: str) -> bool:
        """Регистрирует бота по его ID/имени с динамической загрузкой модуля.

        Пытается найти класс бота по имени bot_id по следующей схеме:
        1. Ищет модуль lib.{bot_id}_bot.py с классом {BotId}Bot
        2. Если не найдено, ищет в модуле lib.bots.{bot_id}.py
        3. Возвращает False если бот не найден или не удалось загрузить

        Конструктор найденного класса бота должен принимать (data_dir: Path)
        и вызывать super().__init__(bot_id, name, data_dir) внутри.

        Args:
            bot_id: ID/имя бота (например, "night_shift", "echo", "weather")

        Returns:
            True если бот успешно загружен и зарегистрирован
        """
        # v1.9.5: проверка+регистрация атомарны. Раньше проверка bot_id
        # в self._bots шла вне лока, а кладка в dict — под локом ПОСЛЕ
        # старта: два параллельных /bots/add запускали ДВА экземпляра бота,
        # первый терялся в dict'е, а его поток (сокеты/таймеры) утекал.
        with self._lock:
            if bot_id in self._bots or bot_id in getattr(self, "_starting", set()):
                return False
            if not hasattr(self, "_starting"):
                self._starting = set()
            self._starting.add(bot_id)

        # Преобразуем bot_id в имя класса (snake_case -> CamelCase)
        class_name = "".join(part.capitalize() for part in bot_id.split("_")) + "Bot"

        # Список кандидатов для импорта (модуль -> класс)
        candidates = [
            (f"lib.{bot_id}_bot", class_name),
            (f"lib.bots.{bot_id}", class_name),
            (f"lib.{bot_id}", class_name),
        ]

        for module_name, cls_name in candidates:
            try:
                import importlib

                mod = importlib.import_module(module_name)
                bot_cls = getattr(mod, cls_name, None)
                if bot_cls is None:
                    continue
                if not issubclass(bot_cls, BaseBot):
                    continue
                # Конструктор бота: BotClass(data_dir)
                bot = bot_cls(self.data_dir)
                with self._lock:
                    self._bots[bot.bot_id] = bot
                # (v3.5.0) колбэк краша — до старта; и ЧИСТИМ _starting на
                # успехе: раньше бот оставался в _starting навсегда, и после
                # удаления повторное добавление того же id молча проваливалось.
                bot._on_crash = self._schedule_restart
                bot.start()
                with self._lock:
                    self._starting.discard(bot_id)
                    self._save_config()
                return True
            except (ImportError, AttributeError, TypeError, Exception):
                continue

        # Ни один кандидат не подошёл
        with self._lock:
            self._starting.discard(bot_id)
        return False

    def unregister_bot(self, bot_id: str) -> bool:
        """Удаляет бота из системы.

        Args:
            bot_id: ID бота для удаления

        Returns:
            True если бот успешно удалён, False если не найден
        """
        with self._lock:
            bot = self._bots.pop(bot_id, None)
            if bot is None:
                return False
            bot.stop()
            self._save_config()
            return True

    def get_bot(self, bot_id: str) -> BaseBot | None:
        """Получает бота по ID."""
        with self._lock:
            return self._bots.get(bot_id)

    def list_bots(self) -> list[str]:
        """Возвращает список ID всех зарегистрированных ботов."""
        with self._lock:
            return list(self._bots.keys())

    def process_command(
        self, bot_id: str, sender: str, command: str, args: list[str]
    ) -> str | dict | None:
        """Маршрутизирует команду в нужный бот.

        Args:
            bot_id: ID бота-получателя
            sender: имя отправителя команды
            command: текст команды
            args: аргументы команды

        Returns:
            Ответ бота (str / dict) или None если бот не найден
        """
        bot = self.get_bot(bot_id)
        if bot is None:
            return None
        return bot.process_command(sender, command, args)

    def dispatch_command(self, sender: str, text: str) -> dict | None:
        """Перебирает всех ботов, ищет того, кто матчит текст команды, и
        выполняет команду у него.

        Используется чатом для перехвата !-команд «в общем потоке»: клиент
        пишет "!snake" в строке ввода, чат шлёт это на /dispatch, сервер
        спрашивает каждого бота matches_command(text), первый совпавший
        выполняет process_command с распарсенными (command, args).

        Returns:
            None  — никто не сматчил (нужно отправить как обычное сообщение)
            {"bot_id": str, "bot_name": str, "response": str | dict} — если сматчили
        """
        with self._lock:
            bots = list(self._bots.items())

        for bot_id, bot in bots:
            try:
                if not bot.matches_command(text):
                    continue
                parsed = bot.parse_command(text)
                if parsed is None:
                    continue
                command, args = parsed
                response = bot.process_command(sender, command, args)
                return {
                    "bot_id": bot_id,
                    "bot_name": getattr(bot, "name", bot_id),
                    "response": response,
                }
            except Exception:
                # Бот упал на разборе/исполнении — не роняем dispatcher,
                # просто переходим к следующему кандидату.
                continue
        return None

    def shutdown(self) -> None:
        """Останавливает всех ботов.

        (v3.5.0) Сначала помечаем остановку и отменяем ожидающие
        перезапуски — иначе упавший перед выходом бот поднялся бы заново
        на закрывающемся сервере."""
        with self._lock:
            self._stopping = True
            timers = list(self._restart_timers)
            self._restart_timers.clear()
        for t in timers:
            t.cancel()
        with self._lock:
            for bot in list(self._bots.values()):
                bot.stop()
            self._bots.clear()

    # ──────────────────────── (v3.5.0) автоперезапуск ────────────────────────

    def _schedule_restart(self, bot: BaseBot) -> None:
        """Колбэк BaseBot при краше фонового потока: планирует перезапуск
        с бэкоффом 5с → 30с → 120с. Вызывается из упавшего потока бота."""
        with self._lock:
            if self._stopping or bot.bot_id not in self._bots:
                return  # менеджер остановлен или бота удалили вручную
            backoff = RESTART_BACKOFF_S[min(bot.crash_count, len(RESTART_BACKOFF_S)) - 1]
            timer = threading.Timer(backoff, self._restart_bot, args=(bot.bot_id,))
            timer.daemon = True
            self._restart_timers.add(timer)
        log.warning(
            "BotManager: бот «%s» упал (№%d), перезапуск через %dс",
            bot.bot_id,
            bot.crash_count,
            backoff,
        )
        timer.start()

    def _restart_bot(self, bot_id: str) -> None:
        # Сработавший таймер вычёркиваем из набора (он сам и есть этот поток)
        fired = threading.current_thread()
        with self._lock:
            if isinstance(fired, threading.Timer):
                self._restart_timers.discard(fired)
            if self._stopping:
                return
            bot = self._bots.get(bot_id)
        if bot is None or bot.is_running():
            return  # бота удалили, или уже кто-то поднял
        log.info("BotManager: перезапуск бота «%s»", bot_id)
        bot.start()
