#!/usr/bin/env python3
"""Стресс-прогон test_web_voice.py под GIL-нагрузкой: имитирует занятую
машину (Windows-хост с антivirusом, UI, и т.п.), чтобы поймать гонку
WS-handshake, которая не воспроизводится на idle Linux."""
import runpy
import sys
import threading

stop = False


def hog():
    while not stop:
        pass  # крутит GIL без пауз


def main() -> int:
    n_hogs = max(1, (__import__("os").cpu_count() or 2))
    threads = [threading.Thread(target=hog, daemon=True) for _ in range(n_hogs)]
    for t in threads:
        t.start()
    code = 0
    try:
        runpy.run_path("tests/test_web_voice.py", run_name="__main__")
    except SystemExit as e:
        code = int(e.code or 0)
    finally:
        global stop
        stop = True
    return code


if __name__ == "__main__":
    sys.exit(main())
