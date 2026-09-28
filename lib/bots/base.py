from __future__ import annotations

import abc
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# (v3.4.2 fix) Пауза дебаунса записи на диск. Музыкальный бот дёргает
# set_data на КАЖДОЕ действие (play/pause/seek/queue/volume/state_version),
# и раньше каждый вызов = полная перезапись music_data.json СИНХРОННО, под
# общим локом бота. На Windows (Defender сканирует каждый файл, HDD) запись
# занимает 20-100 мс — все /music/sync и кнопки GUI вставали в очередь,
# а GUI-поток Qt, звавший api_* напрямую, замораживал окно («Не отвечает»).
# Теперь запись на диск отложена на SAVE_DEBOUNCE_S и сливается в одну.
SAVE_DEBOUNCE_S = 1.0

# (v3.5.0) Логгер ботов: краш фонового потока бота больше не молчащий.
log = logging.getLogger("friendrelay.bots")


class BaseBot(abc.ABC):
    """Базовый класс для всех ботов.

    Каждый бот должен наследоваться от этого класса и реализовать методы:
    - process_command(): обработка команд от пользователей
    - get_help(): описание команд бота
    """

    def __init__(self, bot_id: str, name: str, data_dir: Path):
        self.bot_id = bot_id
        self.name = name
        self.data_dir = data_dir
        self.storage_path = data_dir / f"{bot_id}_data.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load_data()
        self._running = False
        self._thread: threading.Thread | None = None
        # (v3.5.0) Краш-обвязка: фоновый поток бота под защитой — падение
        # пишется в лог, счётчик растёт, BotManager через _on_crash
        # перезапускает бота с бэкоффом. Раньше поток умирал молча:
        # бот исчезал из чата, а manager ничего не замечал.
        self._crash_count = 0
        self._on_crash: Callable[[BaseBot], None] | None = None
        # (v3.4.2 fix) Отложенная запись: _save_timer либо None, либо живой
        # Timer. Все обращения — под self._lock (set_data/_save_data/flush).
        self._save_timer: threading.Timer | None = None

    def _load_data(self) -> dict[str, Any]:
        """Загружает данные бота из JSON файла."""
        if not self.storage_path.exists():
            return {}
        try:
            return json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_data(self) -> None:
        """Сохраняет данные бота в JSON файл (атомарно, v1.9.5: обрыв записи
        раньше оставлял битый JSON → _load_data возвращал {} → очки/матчи/
        статистика ВСЕХ ботов обнулялись молча).

        (v3.4.2 fix) Теперь это ДЕБАУНС-запись: планировщик вызывает её из
        фонового Timer-потока через SAVE_DEBOUNCE_S после последнего
        set_data, а не синхронно в потоке вызвавшем set_data. Файл на диске
        может отставать от памяти максимум на SAVE_DEBOUNCE_S — для
        очереди/громкости/state_version это не страшно; flush_data()
        принудительно пишет немедленно (стоп сервера, критичные моменты)."""
        import os

        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = self.storage_path.with_suffix(".json.tmp")
            try:
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(tmp, self.storage_path)
            except OSError:
                tmp.unlink(missing_ok=True)

    def _schedule_save(self) -> None:
        """(v3.4.2 fix) Планирует одну слитую запись на диск. Вызывать ПОД
        локом. Повторные вызовы до срабатывания таймера не плодят записи —
        все изменения сливаются в один файл за SAVE_DEBOUNCE_S."""
        if self._save_timer is not None:
            return
        timer = threading.Timer(SAVE_DEBOUNCE_S, self._debounced_flush)
        timer.daemon = True
        self._save_timer = timer
        timer.start()

    def _debounced_flush(self) -> None:
        """Тело Timer: сбрасываем таймер и пишем. Берём _lock — на случай
        параллельного flush_data()/stop() (кто первый — тот и пишет)."""
        with self._lock:
            self._save_timer = None
        self._save_data()

    def flush_data(self) -> None:
        """(v3.4.2 fix) Немедленная запись на диск (стоп сервера, тесты).
        Отменяет запланированный дебаунс, чтобы не было двойной записи."""
        with self._lock:
            timer = self._save_timer
            self._save_timer = None
        if timer is not None:
            timer.cancel()
        self._save_data()

    def get_data(self, key: str, default: Any = None) -> Any:
        """Получает значение из хранилища бота."""
        with self._lock:
            return self._data.get(key, default)

    def set_data(self, key: str, value: Any) -> None:
        """Устанавливает значение в хранилище бота.

        (v3.4.2 fix) ПИШЕТ ТОЛЬКО В ПАМЯТЬ под локом; диск — отложенно
        (одной слитой записью через SAVE_DEBOUNCE_S). Раньше каждый вызов
        перезаписывал файл синхронно: музыкальный бот звал set_data по
        несколько раз на каждое действие ПОД общим локом — все /music/sync
        и GUI-кнопки ждали завершения дискового I/O."""
        with self._lock:
            self._data[key] = value
            self._schedule_save()

    def get_player_data(self, player_name: str, default: dict | None = None) -> dict:
        """Получает данные конкретного игрока."""
        if default is None:
            default = {}
        players = self.get_data("players", {})
        return players.get(player_name, default)

    def set_player_data(self, player_name: str, data: dict) -> None:
        """Устанавливает данные конкретного игрока."""
        players = self.get_data("players", {})
        players[player_name] = data
        self.set_data("players", players)

    @abc.abstractmethod
    def process_command(self, sender: str, command: str, args: list[str]) -> str | dict:
        """Обрабатывает команду от пользователя.

        Args:
            sender: имя отправителя
            command: текст команды (например, "!hunt" или "up" для подкоманды)
            args: аргументы команды

        Returns:
            Ответ бота для отправки в чат. Может быть:
            - str: текст, который будет отправлен в чат как сообщение от бота
            - dict: структурированный ответ с action, например:
              {"action": "open_game", "tab": "leaderboard"}
              {"action": "score_saved", "rank": 3, "text": "..."}
              {"action": "meta", "trigger": "snake", "name": "Змейка"}
              В dict может быть поле "text" — если есть, оно добавится в чат.
        """

    @abc.abstractmethod
    def get_help(self) -> str:
        """Возвращает справку по командам бота."""

    def matches_command(self, text: str) -> bool:
        """Проверяет, является ли текст командой этого бота.

        Переопределяется в наследниках. По умолчанию — False (бот не реагирует
        на прямой ввод текста в чате, только на явно направленные команды через
        /bot_command).
        """
        return False

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        """Разбирает сырой текст чата на (command, args), если это команда
        этого бота. Возвращает None если текст не является командой.

        По умолчанию — None. Наследники с поддержкой dispatcher переопределяют.
        """
        return None

    def get_meta(self) -> dict:
        """Возвращает метаданные бота для dispatcher'а: trigger, name и т.п.
        По умолчанию — пустой dict."""
        return {}

    def start(self) -> None:
        """Запускает бота (для фоновых задач).

        (v3.5.0) Поток идёт через _guarded_loop: необработанное исключение
        в цикле бота больше не убивает его молча — пишется лог, инкремент
        crash_count и колбэк в BotManager (перезапуск с бэкоффом)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._guarded_loop, daemon=True, name=f"bot-{self.bot_id}"
        )
        self._thread.start()

    def is_running(self) -> bool:
        """Жив ли фоновый поток бота (v3.5.0, для BotManager)."""
        return self._running

    @property
    def crash_count(self) -> int:
        """Сколько раз фоновый цикл падал с исключением (v3.5.0)."""
        return self._crash_count

    def stop(self) -> None:
        """Останавливает бота.

        (v3.4.2 fix) Перед остановкой принудительно пишем данные на диск:
        после дебаунса set_data файл мог отставать от памяти до 1 c —
        без flush последний play/pause/queue терялся бы при выходе."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.flush_data()
        except Exception:
            pass

    def _guarded_loop(self) -> None:
        """(v3.5.0) Обёртка фонового потока: краш = лог + колбэк менеджеру.
        Само исключение наружу не выпускаем — threading с ним бы просто
        напечатал трейс и забыл, а нам нужен управляемый перезапуск."""
        try:
            self._background_loop()
        except Exception:
            self._running = False
            self._crash_count += 1
            log.exception(
                "Бот «%s» упал в фоновом цикле (падение №%d)",
                self.bot_id,
                self._crash_count,
            )
            callback = self._on_crash
            if callback is not None:
                try:
                    callback(self)
                except Exception:
                    log.exception("BotManager: обработка краша %s упала", self.bot_id)

    def _background_loop(self) -> None:
        """Фоновый цикл бота (переопределяется в наследниках)."""
        while self._running:
            time.sleep(1)

    @staticmethod
    def roll_d20() -> int:
        """Бросок d20 (1-20)."""
        return random.randint(1, 20)

    @staticmethod
    def roll_d6() -> int:
        """Бросок d6 (1-6)."""
        return random.randint(1, 6)
