#!/usr/bin/env python3
"""Тексты прогресса передачи (скорость скачивания/заливки) — v1.5.4.

Проверяют lib/formatters.transfer_progress_text и его обёртки, которыми
пользуются ОБА клиента: qt_app/screens/files_screen.py и legacy/tkinter.

Запуск: python tests/test_download_progress.py  (без зависимостей)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.formatters import (
    download_progress_text,
    transfer_progress_text,
    upload_progress_text,
)

OK = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


print("── transfer_progress_text ──")

t = download_progress_text(3_145_728, 7_000_000, 2.25)
check("проценты внутри", "44%" in t, t)
check("скорость внутри", "/с" in t, t)
check("дробь получено/всего", "3.0МБ/6.7МБ" in t, t)
check("стрелка вниз", t.startswith("↓"), t)

u = upload_progress_text(1_048_576, 4_000_000, 1.0)
check("стрелка вверх", u.startswith("↑"), u)
check("проценты заливки", "26%" in u, u)

check("ноль секунд не делит на ноль",
      "…" in download_progress_text(100, 1000, 0.0))
check("крошечный elapsed не делит на ноль",
      "…" in download_progress_text(100, 1000, 0.01))
check("нулевые байты без скорости",
      "…" in download_progress_text(0, 1000, 5.0))

t_no_total = download_progress_text(655_360, None, 1.0)
check("без total — нет процентов", "%" not in t_no_total, t_no_total)
check("без total — есть объём", "640.0КБ" in t_no_total, t_no_total)

check("100% не переваливает",
      "100%" in download_progress_text(5_000_000, 4_000_000, 3.0))
check("unknown total None ок", isinstance(
    download_progress_text(10, None, 0.2), str))

# обёртки не путают направление
check("обёртки расходятся стрелками",
      download_progress_text(1, 10, 1.0).startswith("↓")
      and upload_progress_text(1, 10, 1.0).startswith("↑"))

# отрицательный pct невозможен
check("клэмп процентов", "0%" in download_progress_text(0, 1000, 1.0))

print(f"\nИтог: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
