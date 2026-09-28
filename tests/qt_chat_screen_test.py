"""Регрессионные тесты общего чата (ChatScreen) — без сети и сервера.

Проверяют конкретные баги, найденные при рефакторинге v7.5:
  1. Накопление stretch-элементов в _feed_layout при перезагрузках
     (раньше каждая перезагрузка добавляла ещё один addStretch() —
     лента переставала прижиматься к низу).
  2. Оптимистичные реакции: раньше тогглились по self._messages,
     который никогда не заполнялся — мгновенный отклик не работал.
  3. Обрезка ленты MAX_RENDERED_ROWS: раньше виджеты накапливались
     бесконечно.
  4. Поиск по расшифрованному (отображаемому) тексту, а не по сырому
     JSON шифрованных сообщений.

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/qt_chat_screen_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from qt_app.screens.chat_screen import ChatScreen

app = QApplication(sys.argv)

screen = ChatScreen()
screen.set_connection("http://127.0.0.1:1", "tester", "", connected=False)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def feed_stretch_count(s: ChatScreen) -> int:
    return sum(
        1 for i in range(s._feed_layout.count()) if s._feed_layout.itemAt(i).widget() is None
    )


# -- 1. Перезагрузка ленты не накапливает растяжки ------------------------
for _ in range(5):
    screen._on_messages_loaded({"events": [], "online": [], "next_since": 0})
check(
    "1. _clear_feed: ровно одна растяжка после 5 перезагрузок",
    feed_stretch_count(screen) == 1,
)
check(
    "1b. _clear_feed сбрасывает группировку (_last_sender is None)",
    screen._last_sender is None,
)

# -- 2. Оптимистичные реакции работают -----------------------------------
ev = {"seq": 1, "kind": "text", "from": "vasya", "text": "привет", "ts": 0, "reactions": {}}
screen._connected = True
screen._on_messages_loaded({"events": [ev], "online": [], "next_since": 1})
bubble = screen._message_widgets.get(1)
check("2a. сообщение отрисовано и зарегистрировано по seq", bubble is not None)
if bubble is not None:
    # мнимый запрос реакции: должен обновить ДАННЫЕ БАБЛА сразу
    screen._bridge.run = lambda *a, **kw: None  # глушим сеть
    screen._on_reaction_requested(1, "🔥")
    check(
        "2b. реакция мгновенно в данных бабла",
        bubble.message_data.get("reactions", {}).get("🔥") == ["tester"],
    )
    screen._on_reaction_requested(1, "🔥")
    check("2c. повторный клик снимает реакцию", "🔥" not in bubble.message_data.get("reactions", {}))

# -- 3. Лента обрезается до MAX_RENDERED_ROWS -----------------------------
from lib.constants import MAX_RENDERED_ROWS

for i in range(2, MAX_RENDERED_ROWS + 60):
    screen._render_message(
        {"seq": i, "kind": "text", "from": "vasya", "text": f"м{i}", "ts": 0}
    )
check(
    "3. лента не превышает MAX_RENDERED_ROWS",
    len(screen._feed_containers) <= MAX_RENDERED_ROWS,
)
check(
    "3b. старые seq вычищены из _message_widgets",
    1 not in screen._message_widgets and screen._message_widgets.get(MAX_RENDERED_ROWS + 59),
)

# -- 4. Поиск по отображаемому (расшифрованному) тексту -------------------
screen._apply_search_filter("м100")
check("4. поиск находит сообщение по отображаемому тексту", "Найдено: 1" in screen._search_result_label.text())
screen._apply_search_filter("")

# -- 5. Каналы рендерятся через общую фабрику (сигналы подключены) --------
screen._connected = True
chan_msg = {"seq": 500, "kind": "text", "from": "petya", "text": "канал", "ts": 0}
screen._current_channel = "тест"
screen._render_message(chan_msg)
chan_bubble = screen._message_widgets.get(500)
check("5a. сообщение канала зарегистрировано по seq", chan_bubble is not None)
if chan_bubble is not None:
    # Фактическая проверка подключения: эмитим сигнал бабла и смотрим,
    # что обработчик ChatScreen получил вызов (раньше в каналах сигналы
    # не подключались вовсе — меню ПКМ молчало).
    got = {}
    orig = screen._on_reply_requested
    screen._on_reply_requested = lambda ev: got.setdefault("reply", ev)
    chan_bubble.reply_requested.emit(chan_msg)
    screen._on_reply_requested = orig
    check("5b. reply_requested бабла канала доходит до ChatScreen", bool(got.get("reply")))

# -- 6. Инкрементальные события не рисуются в ленте канала ---------------
before = len(screen._feed_containers)
screen._on_incremental_events(
    {"events": [{"seq": 501, "kind": "text", "from": "olga", "text": "общее", "ts": 0}], "online": [], "next_since": 501}
)
check(
    "6. события общего чата не прилетают в открытый канал",
    len(screen._feed_containers) == before,
)

print()
if failures:
    print(f"FAILED: {len(failures)} проверок: {failures}")
    sys.exit(1)
print("CHAT SCREEN REGRESSION TESTS OK")
app.quit()
sys.exit(0)
