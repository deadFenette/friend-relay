"""(v3.4.0) Единый журнал сервера Friend Relay.

ФИЛОСОФИЯ (по запросу хоста):
  КОНСОЛЬ — немая. Если сервер запущен из консоли (server_main.py), там
  видны ТОЛЬКО ошибки и краши. Никаких статус-строк, никаких дашбордов.
  ВСЁ ОСТАЛЬНОЕ — в файл: <data_dir>/logs/server.log (ротация по дням,
  хранятся последние 7 дней).

  GUI (server_gui.py) читает тот же журнал и показывает его в окне.

Использование:
    from lib.server_log import setup_server_logging, get_logger
    setup_server_logging(data_dir)          # один раз при старте процесса
    log = get_logger()
    log.info("сервер запущен на порту %s", port)
    log.error("не удалось ...", exc_info=True)

Потокобезопасно (logging сам держит локи). Повторный вызов setup_server_logging
не плодит дубликаты хендлеров — просто перенастраивает уровни.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER_NAME = "relay.server"
_FMT_FILE = "%(asctime)s %(levelname)-7s %(message)s"
_FMT_CONSOLE = "%(levelname)s: %(message)s"

# Флаг «setup_server_logging уже звали в этом процессе». Библиотека
# (RelayServer) не переопределяет настройки лаунчера (server_main/GUI),
# а настраивает журнал сама только если процесс этого не сделал.
_configured = False


def is_configured() -> bool:
    """True, если setup_server_logging уже вызывался в этом процессе."""
    return _configured

# Чтобы разные процессы/модули получали один и тот же логгер.
_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Логгер сервера. До setup_server_logging пишет только warnings+ в stderr."""
    global _logger
    if _logger is None:
        _logger = logging.getLogger(LOGGER_NAME)
        if not _logger.handlers:
            _h = logging.StreamHandler(sys.stderr)
            _h.setLevel(logging.WARNING)
            _h.setFormatter(logging.Formatter(_FMT_CONSOLE))
            _logger.addHandler(_h)
            _logger.setLevel(logging.INFO)
    return _logger


def setup_server_logging(
    data_dir: Path,
    console_errors: bool = True,
    gui_callback=None,
) -> logging.Logger:
    """Настраивает журнал сервера. Вызывать ОДИН раз в начале процесса.

    data_dir         — папка данных хоста (~/.friend_relay/relay_data);
                       журнал ляжет в data_dir/logs/server.log (+ .log.1…7)
    console_errors   — True: в консоль (stderr) идут только WARNING/ERROR
                       (философия «немая консоль»); False — консоль совсем
                       молчит (так делает GUI, у него свой живой журнал)
    gui_callback     — если задан (callable(str)), каждая запись журнала
                       уходит и в него (GUI показывает лог в окне).
                       Вызывается из чужих потоков — GUI сам должен
                       обеспечить потокобезопасность (Qt signal это делает).
    """
    global _logger
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False

    # Защита от повторной настройки в одном процессе (тесты запускают
    # сервер много раз): старые хендлеры снимаем, ставим заново.
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    fmt_file = logging.Formatter(_FMT_FILE, datefmt="%Y-%m-%d %H:%M:%S")

    # 1) ФАЙЛ — всё (INFO+), ротация по дням, живёт 7 дней.
    try:
        from logging.handlers import TimedRotatingFileHandler

        logs_dir = Path(data_dir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            logs_dir / "server.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt_file)
        log.addHandler(fh)
    except OSError:
        # Папка данных недоступна — журнал только в консоль. Сервер
        # всё равно должен работать (философия проекта: деградировать,
        # а не падать).
        pass

    # 2) КОНСОЛЬ — только ошибки (или ничего в GUI-режиме).
    if console_errors:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(logging.Formatter(_FMT_CONSOLE))
        log.addHandler(ch)

    # 3) GUI-колбэк — всё (INFO+).
    if gui_callback is not None:
        gh = _CallbackHandler(gui_callback)
        gh.setLevel(logging.INFO)
        gh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(gh)

    _logger = log
    return log


class _CallbackHandler(logging.Handler):
    """Перенаправляет записи журнала в callable (для GUI)."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._callback(msg)
        except Exception:
            # Журнал не должен ронять сервер ни при каких условиях.
            pass


def log_exception(prefix: str = "ошибка") -> None:
    """Записывает текущее исключение в журнал (в обработчике except)."""
    get_logger().error("%s", prefix, exc_info=True)
