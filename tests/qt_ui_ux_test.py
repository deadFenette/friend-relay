"""Тесты Spatial Glass UI/UX (style.txt) — компоненты v9.4/v9.5.

Покрывают:
  1. TypingIndicator: показ/скрытие, анимация волны (phase крутится).
  2. SkeletonFeed: shimmer-блоки созданы и анимируются.
  3. CommandPalette: регистрация, фильтрация, открытие/закрытие,
     клавиатура (Enter выполняет команду, Esc закрывает).
  4. Toast: один экземпляр на родителя, переиспользование.
  5. SlidingStackedWidget: переход "push" (300ms OutBack) завершается.
  6. Rail: переключение вертикальный <-> горизонтальный (bottom bar).
  7. MessageBubble (редизайн v9.5): hover react-кнопка лениво создаётся,
     ReactionPopover открывается, эмитит emoji_picked; панель v9.4 удалена.
  8. MainWindow: responsive — при ширине <800px рейл становится нижней
     панелью, при возврате ширины — снова вертикальный.

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/qt_ui_ux_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from qt_app.main_window import MainWindow
from qt_app.theme import build_qss
from qt_app.widgets.command_palette import CommandPalette
from qt_app.widgets.message_bubble import MessageBubble
from qt_app.widgets.rail import Rail
from qt_app.widgets.skeleton import SkeletonBlock, SkeletonFeed
from qt_app.widgets.sliding_stack import SlidingStackedWidget
from qt_app.widgets.toast import Toast
from qt_app.widgets.typing_indicator import TypingIndicator

app = QApplication(sys.argv)
app.setStyleSheet(build_qss())

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# -- 1. TypingIndicator -----------------------------------------------------
typing = TypingIndicator()
check("1a. по умолчанию скрыт", not typing.isVisible())
typing.set_typers(["vasya"])
check("1b. set_typers(['vasya']) показывает виджет", typing.isVisible())
typing.set_typers(["a", "b", "c"])
check("1c. три печатающих — текст «и ещё 2»", "и ещё 2" in typing._label_text())
typing._set_phase(0.25)
check("1d. свойство phase анимируется (0..1)", 0.24 <= typing.phase <= 0.26)
typing.set_typers([])
check("1e. пустой список прячет виджет", not typing.isVisible())

# -- 2. SkeletonFeed --------------------------------------------------------
skeleton = SkeletonFeed()
blocks = skeleton.findChildren(SkeletonBlock)
check("2a. скелетон ленты = 5 shimmer-блоков", len(blocks) == 5)
skeleton.start()
running = all(
    b._anim.state() == b._anim.state().Running for b in blocks
) if hasattr(blocks[0], "_anim") else False
check("2b. shimmer-анимации запущены", running)

# -- 3. CommandPalette ------------------------------------------------------
host = QWidget()
host.resize(900, 600)
host.show()
palette = CommandPalette(host)
clicked = {"n": 0}
palette.register("Открыть Чат", lambda: clicked.__setitem__("n", clicked["n"] + 1), icon="💬", category="Разделы")
palette.register("Создать канал", lambda: None, icon="➕", category="Действия")
palette.open_palette()
check("3a. открытие — видимый оверлей", palette.isVisible() and palette.is_open())
check("3b. все команды в списке", palette._list.count() == 2)
palette._input.setText("чат")
check("3c. фильтр «чат» оставляет одну команду", palette._list.count() == 1)
palette._input.setText("")
check("3d. пустой запрос — снова обе", palette._list.count() == 2)
# Enter на выбранной строке выполняет команду и закрывает
QTest.keyClick(palette._input, Qt.Key.Key_Return)
QTest.qWait(30)
check("3e. Enter выполнил команду и закрыл", clicked["n"] == 1 and not palette.isVisible())
palette.open_palette()
QTest.keyClick(palette._input, Qt.Key.Key_Escape)
QTest.qWait(30)
check("3f. Esc закрывает палитру", not palette.isVisible())

# -- 4. Toast ---------------------------------------------------------------
toast_parent = QWidget()
toast_parent.resize(600, 400)
toast_parent.show()
Toast.show_toast(toast_parent, "Тест")
toasts = toast_parent.findChildren(Toast)
check("4a. тост создан", len(toasts) == 1 and toasts[0].isVisible())
Toast.show_toast(toast_parent, "Второй")
check("4b. повторный вызов переиспользует тот же тост", len(toast_parent.findChildren(Toast)) == 1)
check("4c. текст обновлён", "Второй" in toasts[0].text())

# -- 5. SlidingStackedWidget: push ------------------------------------------
stack = SlidingStackedWidget()
page_a, page_b = QWidget(), QWidget()
stack.addWidget(page_a)
stack.addWidget(page_b)
stack.resize(800, 500)
stack.show()
stack.go_to(page_b, "push")
QTest.qWait(stack.PUSH_MS + 200)
check("5a. push-переход завершён на целевой странице", stack.currentWidget() is page_b)
check("5b. после перехода анимация сброшена", not stack._animating)
check("5c. геометрия целевой страницы восстановлена", page_b.pos().x() == 0 and page_b.pos().y() == 0)

# -- 6. Rail: вертикальный <-> горизонтальный -------------------------------
rail = Rail([("a", "💬", "Чат"), ("b", "📁", "Файлы")])
rail.resize(64, 400)
rail.show()
rail.set_orientation(True)
check("6a. горизонтальный режим включён", rail.is_horizontal() and rail.height() == rail.BOTTOM_BAR_H)
rail.set_orientation(False)
check("6b. обратный переход вернул вертикаль 64px", not rail.is_horizontal() and rail.width() == 64)
check("6c. элементы пережили перекомпоновку", len(rail._items) == 2 and all(rail._items[k].parent() is rail for k in rail._items))

# -- 7. MessageBubble (v9.5): react-кнопка + ReactionPopover -----------------
bubble = MessageBubble("привет", is_me=False, message_data={"seq": 1}, my_name="tester")
bubble.resize(400, 60)
bubble.show()
check("7a. react-кнопка ленивая: до hover не создана", bubble._react_button is None)
bubble._show_react_button()
QTest.qWait(60)
check("7b. после hover кнопка создана и видима", bubble._react_button is not None and bubble._react_button.isVisible())
check("7c. кнопка круглая (30x30, рядом с баблом)", bubble._react_button.width() == 30 and bubble._react_button.x() > bubble._label.geometry().right())
bubble._hide_react_button()
check("7d. уход курсора прячет кнопку", not bubble._react_button.isVisible())

# Пикер реакций: открытие у бабла, 6 эмодзи, сигнал долетает до ленты
from qt_app.widgets import reaction_popover as _rp

got: list = []
bubble.reaction_requested.connect(lambda seq, e: got.append((seq, e)))
bubble._open_reaction_popover()
QTest.qWait(80)
pop = _rp.ReactionPopover._active


def _alive(w) -> bool:
    try:
        return bool(w and w.isVisible())
    except RuntimeError:
        return False  # WA_DeleteOnClose уже удалил C++-объект


check("7e. попап открыт и единственный (_active)", pop is not None and _rp.ReactionPopover._active is pop and _alive(pop))
check("7f. в попапе 6 эмодзи-кнопок", pop is not None and pop.layout().count() == 6)
if pop is not None and _alive(pop):
    pop.layout().itemAt(0).widget().click()
QTest.qWait(60)
check("7g. клик эмитит reaction_requested(seq, emoji) и закрывает попап", got == [(1, "👍")] and not _alive(pop))

# Моя реакция подчёркнута кольцом
bubble2 = MessageBubble("сам себе", is_me=True, message_data={"seq": 2, "reactions": {"🔥": ["tester"]}}, my_name="tester")
bubble2.resize(400, 60)
bubble2.show()
check("7i. _my_reaction находит мою 🔥", bubble2._my_reaction() == "🔥")

# -- 8. MainWindow: responsive rail -----------------------------------------
window = MainWindow()
window.show()
QTest.qWait(50)
window.resize(1100, 700)
QTest.qWait(50)
check("8a. широкое окно — вертикальный рейл", not window.rail.is_horizontal())
window.resize(700, 500)
QTest.qWait(50)
check("8b. окно <800px — рейл стал нижней панелью", window.rail.is_horizontal())
check("8c. высота нижней панели = 56", window.rail.height() == window.rail.BOTTOM_BAR_H)
window.resize(1000, 600)
QTest.qWait(50)
check("8d. окно снова широкое — рейл вернулся слева", not window.rail.is_horizontal())
# Палитра доступна и команды зарегистрированы
check("8e. палитра создана, статические команды зарегистрированы",
      window.palette is not None and len(window.palette._commands) >= 3)
# Канальные команды регистрируются на лету
window._screens["chat"]._channels = [{"name": "игры"}, {"name": "музыка"}]
window._register_channel_commands()
cats = [c.category for c in window.palette._commands]
check("8f. каналы попали в палитру", cats.count("Каналы") == 3)  # общий + 2

print()
if failures:
    print(f"FAILED: {len(failures)} проверок: {failures}")
    sys.exit(1)
print("UI/UX TESTS OK")
app.quit()
sys.exit(0)
