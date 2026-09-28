"""E2E тест: не дёргаем set_connection() напрямую, а по-настоящему кликаем
кнопку "Подключиться" в UI в режиме хоста, ждём реального ответа сервера
через сигнал connected_changed, и только потом проверяем что Files/Bots
экраны сами подхватили данные."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lib import constants

# подменяем DATA_DIR на временную папку, чтобы не мусорить в ~/.friend_relay
TMP_HOME = Path(tempfile.mkdtemp(prefix="frelay_e2e_"))
constants.DATA_DIR = TMP_HOME / ".friend_relay"
constants.SETTINGS_FILE = constants.DATA_DIR / "settings.json"

OUT_DIR = Path(tempfile.mkdtemp(prefix="frelay_e2e_shots_"))

from qt_app.main_window import MainWindow
from qt_app.theme import build_qss

app = QApplication(sys.argv)
app.setStyleSheet(build_qss())
window = MainWindow()
window.show()

settings = window._screens["settings"]
settings._name_input.setText("Хозяйка")
settings._host_radio.setChecked(True)
settings._port_input.setValue(8766)

result_ok = {"connected": False}
settings.connected_changed.connect(lambda ok: result_ok.update(connected=ok))


def click_connect():
    print("кликаю 'Подключиться' (режим хоста)...")
    settings._connect_btn.click()
    QTimer.singleShot(1500, check_connected)


def check_connected():
    print("connected_changed сигнал получен со значением:", result_ok["connected"])
    assert result_ok["connected"], "подключение не прошло за отведённое время"
    assert window._screens["files"].connected, "FilesScreen не узнал о подключении"
    assert window._screens["bots"]._bot_manager is not None, "BotsScreen не получил bot_manager"
    print("relay поднят:", settings.relay is not None, "| base_url:", settings.base_url)

    window.rail._on_clicked("files")
    QTimer.singleShot(500, shot_files)


def shot_files():
    window.grab().save(str(OUT_DIR / "shot_e2e_files.png"))
    window.rail._on_clicked("bots")
    QTimer.singleShot(200, shot_bots)


def shot_bots():
    window.grab().save(str(OUT_DIR / "shot_e2e_bots.png"))
    click_disconnect()


def click_disconnect():
    print("кликаю 'Отключиться'...")
    window.rail._on_clicked("settings")
    settings._connect_btn.click()
    QTimer.singleShot(300, check_disconnected)


def check_disconnected():
    assert not result_ok["connected"], "отключение не прошло"
    assert settings.relay is None, "сервер не остановился"
    print("отключение ок, пробую переподключиться...")
    QTimer.singleShot(200, click_reconnect)


def click_reconnect():
    settings._connect_btn.click()
    QTimer.singleShot(1500, check_reconnected)


def check_reconnected():
    assert result_ok["connected"], "повторное подключение не прошло"
    window.rail._on_clicked("files")
    QTimer.singleShot(400, finish)


def finish():
    print("E2E TEST OK — connect/disconnect/reconnect через реальные клики работают; скриншоты в", OUT_DIR)
    shutil.rmtree(TMP_HOME, ignore_errors=True)
    app.quit()


QTimer.singleShot(200, click_connect)
sys.exit(app.exec())
