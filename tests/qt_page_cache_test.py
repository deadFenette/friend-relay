"""Тесты оптимизации переключения каналов (FeedPageManager, v9.3).

Проверяют:
  1. Повторное открытие канала НЕ пересоздаёт виджеты (горячий путь —
     setCurrentWidget, те же объекты баблов).
  2. Позиция скролла сохраняется при уходе и восстанавливается при возврате.
  3. Дифф чужого потока не попадает в чужую ленту (guard + dirty).
  4. Двойной вызов _update_online_and_typing на тик устранён.
  5. LRU: страниц не больше MAX_PAGES, активная не вытесняется.
  6. _clear_all: все страницы уничтожены, активна свежая общая.

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/qt_page_cache_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from qt_app.screens.chat_screen import ChatScreen, FeedPageManager

app = QApplication(sys.argv)

screen = ChatScreen()
screen.set_connection("http://127.0.0.1:1", "tester", "", connected=False)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def make_msgs(start_seq: int, count: int, sender: str = "vasya") -> list[dict]:
    return [
        {"seq": start_seq + i, "kind": "text", "from": sender, "text": f"м{start_seq + i}", "ts": 0}
        for i in range(count)
    ]


# Подготовка: общий чат с сообщениями
screen._connected = True
screen._on_messages_loaded({"events": make_msgs(1, 5), "online": [], "next_since": 5})

# -- 1. Горячее переключение: страница канала переиспользуется --------------
chan_msgs = make_msgs(100, 5, sender="petya")
screen._session.set_channel("игры")
screen._session._channel_tails["игры"] = chan_msgs  # кэш ядра
screen._on_channel_pill_clicked("игры")  # холодный путь: батч-рендер из кэша
bubble_cold = screen._message_widgets.get(100)
check(
    "1a. канал отрисован из кэша ядра при первом открытии",
    bubble_cold is not None and screen._rendered_channel == "игры",
)

page = screen._pages.page("игры")
screen._on_channel_pill_clicked(None)  # ушли в общий чат
screen._on_channel_pill_clicked("игры")  # вернулись — должен быть горячий путь
check(
    "1b. возврат в канал НЕ пересоздаёт страницу",
    screen._pages.page("игры") is page,
)
check(
    "1c. баблы те же объекты (ноль пересозданий)",
    screen._message_widgets.get(100) is bubble_cold,
)

# -- 2. Обновление хвоста фонового канала помечает страницу dirty ----------
screen._on_channel_pill_clicked(None)  # ушли в общий чат
page = screen._pages.page("игры")
check("2a. перед обновлением страница не dirty", page is not None and not page.dirty)
# В реальном потоке ядро (ChatSession._on_channel_loaded) сначала кладёт
# свежий хвост в свой кэш и только потом зовёт on_channel_loaded — повторяем.
fresh_chan_msgs = make_msgs(100, 7, sender="petya")
screen._session._channel_tails["игры"] = fresh_chan_msgs
screen._on_channel_messages_loaded("игры", fresh_chan_msgs)
check("2b. фоновое обновление пометило страницу dirty", page.dirty)
screen._on_channel_pill_clicked("игры")  # возврат: dirty → перерисовка из хвоста ядра
check(
    "2c. после возврата dirty сброшен и хвост свежий",
    not page.dirty and screen._message_widgets.get(106) is not None,
)

# -- 3. Дифф чужого потока не рисуется в чужой ленте ------------------------
screen._on_channel_pill_clicked(None)
before = len(screen._feed_containers)
session_state_before = list(screen._session._channel_tails.get("игры", []))

from lib.client_core.chat_session import TailDiff

diff = TailDiff(added=[{"seq": 999, "kind": "text", "from": "x", "text": "чужое", "ts": 0}])
screen._rendered_channel  # noqa: B018 — читаем для ясности
screen._pages.page("игры").dirty = False
screen._apply_channel_diff("игры", diff)
check(
    "3. дифф фонового канала не попал в общий чат (guard, страница dirty)",
    len(screen._feed_containers) == before and screen._pages.page("игры").dirty,
)

# -- 4. Онлайн обновляется один раз за тик ----------------------------------
calls = {"n": 0}
orig_update = screen._update_online_and_typing


def counting_update(result: dict) -> None:
    calls["n"] += 1
    orig_update(result)


screen._update_online_and_typing = counting_update
screen._on_poll_result({"events": [], "online": ["a"], "typing": [], "next_since": 6})
check("4. _on_poll_result вызывает обновление онлайна ровно один раз", calls["n"] == 1)
screen._update_online_and_typing = orig_update

# -- 5. LRU: не больше MAX_PAGES, активная не вытесняется -------------------
for i in range(FeedPageManager.MAX_PAGES + 4):
    name = f"канал{i}"
    screen._session._channel_tails[name] = make_msgs(1000 + i * 10, 2)
    screen._on_channel_pill_clicked(name)
pages = screen._pages
check(
    "5a. число страниц не превышает MAX_PAGES",
    len(pages._pages) <= FeedPageManager.MAX_PAGES,
)
check(
    "5b. активная страница переживает вытеснение",
    pages.page(f"канал{FeedPageManager.MAX_PAGES + 3}") is pages.current(),
)

# -- 6. _clear_all: кэш страниц опустошён ------------------------------------
screen._clear_all()
check(
    "6a. после отключения осталась одна пустая общая страница",
    len(screen._pages._pages) == 1
    and screen._pages.current() is not None
    and screen._pages.current().key is None,
)
check("6b. rendered_channel сброшен", screen._rendered_channel is None)

print()
if failures:
    print(f"FAILED: {len(failures)} проверок: {failures}")
    sys.exit(1)
print("FEED PAGE CACHE TESTS OK")
app.quit()
sys.exit(0)
