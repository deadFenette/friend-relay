#!/usr/bin/env python3
"""Бенчмарк горячей точки /events: bisect против линейного скана (v2.0.2).

Показывает выигрыш events_since после замены list-comprehension на bisect
(см. lib/domain/event_store.py и docs/PERFORMANCE.md). Мерило честное:
одинаковое множество событий, одинаковые точки since, один и тот же
EventStore на временном каталоге.

Запуск: python scripts/bench_events_since.py [размер кеша]
"""
from __future__ import annotations

import bisect
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.domain.event_store import EventStore


def linear_scan(events: list[dict], since: int) -> list[dict]:
    """Старая реализация events_since (до v2.0.2) — эталон для сравнения."""
    return [e for e in events if e["seq"] > since]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    with tempfile.TemporaryDirectory(prefix="frelay_bench_") as td:
        store = EventStore(Path(td), cache_limit=n)
        for i in range(n):
            store.add_text("Вася", f"сообщение {i}")
        with store._lock:
            events = list(store._events)
        probes = [0, 1, n // 4, n // 2, n - 10, n + 5]
        rounds = 2000

        t0 = time.perf_counter()
        for _ in range(rounds):
            for since in probes:
                linear_scan(events, since)
        t_linear = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(rounds):
            for since in probes:
                store.events_since(since)
        t_bisect = time.perf_counter() - t0

        # корректность: оба пути обязаны вернуть одно и то же
        for since in probes:
            assert linear_scan(events, since) == store.events_since(since), since

        print(f"событий в кеше: {n}, точек since: {len(probes)}, раундов: {rounds}")
        print(f"линейный скан (старый путь): {t_linear * 1000:8.1f} мс")
        print(f"bisect (новый путь):         {t_bisect * 1000:8.1f} мс")
        print(f"ускорение: {t_linear / max(t_bisect, 1e-9):.1f}x")
        store.close()


if __name__ == "__main__":
    main()
