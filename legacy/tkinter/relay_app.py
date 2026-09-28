#!/usr/bin/env python3
"""Точка входа. Просто запускает окно - никаких аргументов командной строки,
всё настраивается прямо в интерфейсе (та же философия, что у mod_client.py)."""
import sys
from pathlib import Path

# Корень проекта (legacy/tkinter/relay_app.py -> .. -> ..), чтобы работали
# импорты "lib.*" и "legacy.*" при прямом запуске файла.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from legacy.tkinter.app import run

if __name__ == "__main__":
    run()
