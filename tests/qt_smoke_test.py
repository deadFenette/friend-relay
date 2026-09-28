"""
Быстрая проверка "ничего не падает и выглядит адекватно" без реального экрана.

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/qt_smoke_test.py

Сохраняет скриншоты в /tmp/qt_smoke_*.png на каждом шаге. Полезно дёргать
после правок в qt_app/, прежде чем открывать реальное окно.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from qt_app.main_window import MainWindow
from qt_app.theme import build_qss

OUT_DIR = Path("/tmp")

app = QApplication(sys.argv)
app.setStyleSheet(build_qss())
window = MainWindow()
window.show()


def shot(name: str) -> None:
    window.grab().save(str(OUT_DIR / f"qt_smoke_{name}.png"))


def step_settings():
    shot("1_chat")
    window.rail._on_clicked("settings")
    QTimer.singleShot(250, step_send_message)


def step_send_message():
    shot("2_settings")
    window.rail._on_clicked("chat")
    window._screens["chat"]._input.setText("Тестовое сообщение")
    window._screens["chat"]._on_send()
    QTimer.singleShot(200, step_finish)


def step_finish():
    shot("3_new_message")
    print("SMOKE TEST OK — скриншоты в /tmp/qt_smoke_*.png")
    app.quit()


QTimer.singleShot(100, step_settings)
sys.exit(app.exec())
