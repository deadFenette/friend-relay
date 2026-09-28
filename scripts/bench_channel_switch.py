"""Бенчмарк переключения каналов ChatScreen (offscreen).

Сравнивает:
  COLD  — первое открытие канала (батч-рендер из кэша ядра):
          это СТАРАЯ стоимость КАЖДОГО переключения (v9.2).
  HOT   — повторное открытие через кэш страниц (FeedPageManager):
          новая стоимость типичного переключения (v9.3).
  FORCE — принудительная полная пересборка (dirty-страница): это путь
          старой версии, оставлен только для честного сравнения.

Запуск:
    QT_QPA_PLATFORM=offscreen python3 scripts/bench_channel_switch.py
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from lib.constants import CHAT_TAIL_ON_CONNECT
from qt_app.screens.chat_screen import ChatScreen

app = QApplication(sys.argv)

screen = ChatScreen()
screen.set_connection("http://127.0.0.1:1", "tester", "", connected=False)
screen._connected = True

N = CHAT_TAIL_ON_CONNECT  # 60 сообщений — типичный хвост канала
ROUNDS = 7


def make_msgs(start: int, count: int) -> list[dict]:
    return [
        {"seq": start + i, "kind": "text", "from": "vasya", "text": f"сообщение {start + i}", "ts": 0}
        for i in range(count)
    ]


def timed(fn) -> float:
    times = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def switch_to(channel: str | None, msgs: list[dict]) -> None:
    """Имитация клика по pill'у: ядро уже отдало хвост из кэша."""
    screen._session.set_channel(channel)
    if channel is None:
        screen._session._general_tail = msgs
    else:
        screen._session._channel_tails[channel] = msgs
    screen._on_channel_pill_clicked(channel)


print(f"Сообщений в ленте: {N}, замеров на путь: {ROUNDS} (медиана)\n")

# COLD: каждый раз НОВЫЙ канал — первый батч-рендер (то, что v9.2 делала
# на КАЖДОЕ переключение)
cold_ms = timed(
    lambda: switch_to(f"cold{time.perf_counter_ns()}", make_msgs(1000, N))
)

# HOT: туда-обратно между двумя уже отрисованными каналами
switch_to("A", make_msgs(2000, N))
switch_to("B", make_msgs(3000, N))
hot_ms = timed(lambda: switch_to("A" if screen._rendered_channel == "B" else "B", []))

# FORCE: старый путь — полная пересборка ленты при каждом переключении
def force_rebuild():
    target = "A" if screen._rendered_channel == "B" else "B"
    page = screen._pages.page(target)
    if page is not None:
        page.dirty = True
    switch_to(target, make_msgs(4000, N))


force_ms = timed(force_rebuild)

print(f"COLD  (первый рендер канала, v9.2 = каждый раз): {cold_ms:8.1f} мс")
print(f"FORCE (полная пересборка, путь старой версии):   {force_ms:8.1f} мс")
print(f"HOT   (кэш страниц, v9.3):                       {hot_ms:8.3f} мс")
if hot_ms > 0:
    print(f"\nВыигрыш повторного переключения: ~{force_ms / max(hot_ms, 1e-9):.0f}x")
app.quit()
