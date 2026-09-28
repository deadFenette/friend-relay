"""
Мост к сети — замена tkinter-очереди (`_ui_queue` + `after()`-поллинг каждые
N мс из lib/app.py). В Qt для этого есть штатный механизм: сигнал, испущенный
из фонового потока, Qt сам безопасно доставляет в поток виджета-получателя
(автоматическое queued-соединение, если эмиттер и получатель в разных
потоках) — не нужно вручную гонять очередь и таймер.

Использование:
    bridge = AsyncBridge()
    bridge.run(
        lambda: client.list_files(base_url, name, key),
        on_success=self._apply_files_list,
        on_error=lambda e: messagebox_analog(str(e)),
    )

Оптимизация v9.3: раньше каждый вызов run() порождал НОВЫЙ daemon-thread
(`threading.Thread(...)`). При живом поллинге раз в секунду, загрузках
каналов, аватарок, превью и файлов это давало постоянный churn потоков:
создание/уничтожение потока стоит памяти под стек, а частые короткие
потоки усиливают конкуренцию за GIL — GUI-поток недополучал кванты и
интерфейс подёргивался. Теперь все задачи идут через общий bounded
ThreadPoolExecutor (переиспользуемые потоки, ограничение параллелизма),
а API run() не изменился ни для одного вызывателя.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Signal

# Ограниченный общий пул для ВСЕХ фоновых задач приложения. 8 воркеров
# с запасом покрывают поллинг + загрузки каналов + аватарки + превью:
# типичный клиент редко держит больше 3-4 параллельных запросов, а потолок
# не даёт всплеску (например, рендер ленты с десятком превью) плодить
# десятки потоков. Дефолтный executor у ThreadPoolExecutor вообще
# unbounded по потокам (min(32, cpu+4)) — для чата это избыточно.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="relay-async")

# Защита submit'а при интерпретации в процессе закрытия приложения.
_EXECUTOR_LOCK = threading.Lock()


def shutdown_executor(wait: bool = False) -> None:
    """Глушит общий пул (вызывать при выходе из приложения; необязательно —
    потоки daemon'овые и умрут сами)."""
    with _EXECUTOR_LOCK:
        _EXECUTOR.shutdown(wait=wait)


class _WorkerSignals(QObject):
    success = Signal(object)
    error = Signal(object)


class AsyncBridge(QObject):
    """Держи один экземпляр на экран/окно (не пересоздавай на каждый вызов) -
    так проще отследить, какие запросы ещё летят, если понадобится отмена.

    Все задачи выполняются в общем bounded-пуле (см. _EXECUTOR); результат
    доставляется в GUI-поток через queued-сигналы. Отменять отдельные задачи
    намеренно не предоставляем: поллинг держит инвариант in-flight в ядре
    (ChatSession._poll_in_flight) и его отмена заклинила бы синхронизацию —
    устаревшие ответы и так отсекаются guard'ами ядра по каналам/курсорам.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def run(
        self,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        signals = _WorkerSignals(self)  # parent=self держит от сборки мусора
        if on_success is not None:
            signals.success.connect(on_success)
        if on_error is not None:
            signals.error.connect(on_error)

        def worker() -> None:
            try:
                result = fn()
            except (
                Exception
            ) as e:  # сетевые ошибки уже типизированы как RelayError на вызывающей стороне
                signals.error.emit(e)
            else:
                signals.success.emit(result)

        with _EXECUTOR_LOCK:
            try:
                _EXECUTOR.submit(worker)
            except RuntimeError:
                pass  # пул уже заглушен (приложение закрывается) — тихо игнорируем
