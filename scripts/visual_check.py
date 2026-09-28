"""Визуальная проверка Spatial Glass UI: скриншоты ключевых состояний."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QPainter, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from qt_app.main_window import MainWindow
from qt_app.theme import build_qss
from qt_app.widgets.skeleton import SkeletonFeed
from qt_app.widgets.typing_indicator import TypingIndicator

OUT = Path("/tmp/visual")
OUT.mkdir(exist_ok=True)

app = QApplication(sys.argv)
app.setStyleSheet(build_qss())
window = MainWindow()
window.resize(1100, 720)
window.show()

chat = window._screens["chat"]


def shot(name: str) -> None:
    window.grab().save(str(OUT / f"{name}.png"))
    print("shot:", name)


def shot_with_toplevel(name: str, top) -> None:
    """Кадр окна + ДОрисованное top-level окно (пикер — отдельный popup,
    window.grab() его не захватывает). Рисуем только детей — фон пикера
    полупрозрачный (DrawWindowBackground залил бы чёрным)."""
    base = window.grab()
    p = QPainter(base)
    offset = top.mapToGlobal(QPoint(0, 0)) - window.mapToGlobal(QPoint(0, 0))
    top.render(p, offset, QRegion(), QWidget.RenderFlag.DrawChildren)
    p.end()
    base.save(str(OUT / f"{name}.png"))
    print("shot:", name)


def add_fake_msgs() -> None:
    chat._connected = True
    msgs = [
        {"seq": i, "kind": "text", "from": "vasya" if i % 3 else "tester",
         "text": f"Сообщение {i}: проверка Spatial Glass 🎨", "ts": 0}
        for i in range(1, 9)
    ]
    # Последнему сообщению — реакции, чтобы показать чипы (v9.5: кольцо «моё»)
    msgs[-1]["reactions"] = {"🔥": ["tester", "vasya"], "👍": ["petya"]}
    chat._on_messages_loaded({"events": msgs, "online": ["vasya", "petya"], "next_since": 9})


def step1() -> None:
    add_fake_msgs()
    QTimer.singleShot(300, step2)


def step2() -> None:
    shot("1_chat_feed")
    # React-кнопка при наведении (v9.5: одна круглая у угла бабла)
    feed_widgets = chat._message_widgets
    if feed_widgets:
        target = list(feed_widgets.values())[-2]
        target._show_react_button()
    QTimer.singleShot(250, step3)


def step3() -> None:
    shot("2_react_button_hover")
    # Пикер реакций (стеклянная пилюля с эмодзи)
    if chat._message_widgets:
        target = list(chat._message_widgets.values())[-2]
        target._open_reaction_popover()
    QTimer.singleShot(400, step4)


def step4() -> None:
    from qt_app.widgets.reaction_popover import ReactionPopover
    pop = ReactionPopover._active
    if pop is not None:
        shot_with_toplevel("3_reaction_popover", pop)
        pop.close()
    else:
        shot("3_reaction_popover")
    # Палитра команд
    window._toggle_palette()
    QTimer.singleShot(400, step5)


def step5() -> None:
    shot("4_command_palette")
    window.palette.close_palette()
    # Скелетон загрузки канала
    chat._show_feed_placeholder("Загрузка канала…", skeleton=True)
    QTimer.singleShot(200, step6)


def step6() -> None:
    shot("5_skeleton_loading")
    # Волна typing
    chat._typing_indicator.set_typers(["vasya", "petya"])
    QTimer.singleShot(300, step7)


def step7() -> None:
    shot("6_typing_wave")
    # Узкое окно — рейл вниз
    chat._typing_indicator.set_typers([])
    window.resize(720, 560)
    QTimer.singleShot(300, step8)


def step8() -> None:
    shot("7_bottom_rail_narrow")
    window.resize(1100, 720)
    QTimer.singleShot(250, step9)


def step9() -> None:
    shot("8_back_wide")
    print("VISUAL OK — скриншоты в", OUT)
    app.quit()


QTimer.singleShot(200, step1)
sys.exit(app.exec())
