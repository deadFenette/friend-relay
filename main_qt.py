"""Каноническая точка входа Friend Relay на PySide6.

Запуск: ``python main_qt.py`` или ``RUN.bat``.
Старая Tkinter-ветка находится в ``legacy/tkinter/`` и не участвует
в обычном запуске или сборке.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from qt_app.main_window import MainWindow
from qt_app.theme import build_qss
from qt_app.widgets.toast import Toast

ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"


class _ErrorBridge(QObject):
    """Мост «любой поток → GUI-поток» для показа toast об ошибках.

    excepthook/watchdog срабатывают в произвольных потоках, а трогать
    QWidget оттуда нельзя (thread affinity). Сигнал, подключённый к
    слоту окна, доставляется queued-соединением в GUI-поток — это
    единственный разрешённый Qt способ."""

    got = Signal(str)


def main() -> int:
    # (v3.5.0) Стабильность: краш/сегфолт/зависание = файл в logs/, а не
    # «намертво и без причины». Подробности — lib/stability.py.
    from lib.stability import HangWatchdog, enable_faulthandler, install_excepthooks

    enable_faulthandler(LOGS_DIR)

    app = QApplication(sys.argv)
    app.setStyleSheet(build_qss())
    window = MainWindow()

    bridge = _ErrorBridge()
    bridge.got.connect(lambda msg: Toast.show_toast(window, msg, icon="⚠"))

    install_excepthooks(LOGS_DIR, on_error=bridge.got.emit)

    watchdog = HangWatchdog(LOGS_DIR, stuck_after_s=15.0, check_every_s=2.0)

    def _on_resume(seconds: float, dump_path: Path | None) -> None:
        msg = f"Окно не отвечало {seconds:.1f}с"
        if dump_path is not None:
            msg += f" — стеки: {Path(dump_path).name}"
        bridge.got.emit(msg)

    watchdog.start(on_resume=_on_resume)

    # Сердцебиение ИЗ GUI-потока: пока event loop крутится — тикает.
    # Встал цикл (диск/сеть/лок) — сердцебиение пропало, дамп стеков.
    beat = QTimer(window)
    beat.setInterval(2000)
    beat.timeout.connect(watchdog.heartbeat)
    beat.start()

    window.show()
    code = app.exec()
    watchdog.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
