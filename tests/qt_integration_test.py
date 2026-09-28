"""Интеграционный тест: настоящий lib.relay_server.RelayServer + настоящий
lib.client.py + qt_app.screens.files_screen/bots_screen против них. Никаких
моков — если тут всё ок, значит и сетевая часть каркаса реально работает,
а не просто красиво нарисована."""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from lib import client
from lib.relay_server import RelayServer
from qt_app.main_window import MainWindow
from qt_app.theme import build_qss

DATA_DIR = Path(tempfile.mkdtemp(prefix="frelay_qt_test_"))
PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"
NAME = "тестер"

server = RelayServer(DATA_DIR, host_name=NAME)
assert server.start("127.0.0.1", PORT), "не удалось поднять RelayServer"
time.sleep(0.3)

OUT_DIR = Path(tempfile.mkdtemp(prefix="frelay_qt_shots_"))

app = QApplication(sys.argv)
app.setStyleSheet(build_qss())

# -- заливаем тестовый файл через НАСТОЯЩИЙ client.send_file --
# (картинку генерируем на месте — тест не должен зависеть от путей на машине)
img_path = OUT_DIR / "test_image.png"
_qpm = QPixmap(64, 64)
_qpm.fill(QColor("#7C6BFF"))
assert _qpm.save(str(img_path)), "не удалось создать тестовую картинку"
seq = client.send_file(BASE_URL, NAME, img_path)
print("uploaded, seq =", seq)

files = client.list_files(BASE_URL, NAME)
print("list_files() ->", files)
assert files, "сервер не вернул залитый файл"

bots = client.list_bots(BASE_URL, NAME)
print("list_bots() ->", bots)  # night_shift должен зарегистрироваться сам при старте сервера

window = MainWindow()
window.set_connection(BASE_URL, NAME, "", connected=True)
window.set_bot_manager(server.get_bot_manager())
window.show()


def go_files():
    window.rail._on_clicked("files")
    QTimer.singleShot(600, shot_files)  # ждём асинхронную загрузку списка + превью


def shot_files():
    window.grab().save(str(OUT_DIR / "shot_files_real.png"))
    window.rail._on_clicked("bots")
    QTimer.singleShot(200, shot_bots)


def shot_bots():
    window.grab().save(str(OUT_DIR / "shot_bots_real.png"))
    finish()


def finish():
    print("INTEGRATION TEST OK — скриншоты в", OUT_DIR)
    server.stop()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    app.quit()


QTimer.singleShot(200, go_files)
sys.exit(app.exec())
