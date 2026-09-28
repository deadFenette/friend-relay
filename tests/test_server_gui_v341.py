#!/usr/bin/env python3
"""(v3.4.1) Проверки server_gui.py:
1) честный отчёт о PySide6 (никакого вранья «не установлен», когда он есть);
2) ни одного захардкоженного «Жуж» и текстовых путей в пользовательских
   строках GUI — всё берётся из реальных настроек/папок.
"""
from __future__ import annotations

import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1


def main() -> int:
    gui = ROOT / "server_gui.py"
    smain = ROOT / "server_main.py"

    print("── компиляция")
    try:
        py_compile.compile(str(gui), doraise=True)
        py_compile.compile(str(smain), doraise=True)
        check("server_gui.py и server_main.py компилируются", True)
    except Exception as e:
        check(f"компиляция: {e}", False)

    print("── никаких захардкоженных имён/путей в UI")
    src = gui.read_text(encoding="utf-8")
    sm = smain.read_text(encoding="utf-8")
    check("в server_gui.py нет «Жуж» вообще", "Жуж" not in src)
    check("в server_main.py нет «Жуж» вообще", "Жуж" not in sm)
    check("музыка: реальная папка (self._music_dir)", "self._music_dir" in src)
    check("кнопка «Открыть папку музыки» (_on_open_music_dir)",
          "_on_open_music_dir" in src)
    check("журнал: показан реальный файл logs/server.log",
          "'logs' / 'server.log'" in src)
    check("нет старого текста «файлы — в relay_data/music»",
          "файлы — в relay_data/music" not in src)
    check("тултип ключа админа строится из имени хоста",
          "self.relay.host_name" in src and "«{host}#2»" in src)

    print("── server_gui.py --diag (в песочнице PySide6 нет)")
    r = subprocess.run([sys.executable, str(gui), "--diag"],
                       capture_output=True, text=True, timeout=60,
                       cwd=str(ROOT))
    out = r.stdout + r.stderr
    check("--diag: код 1 без PySide6", r.returncode == 1)
    check("--diag: показывает путь текущего python", sys.executable in out)
    # (v3.4.2) "pip install" без хвоста: если PySide6 стоит, но сломан
    # (нет libEGL в headless-песочнице), --diag советует
    # "pip install --force-reinstall PySide6" — подсказка та же по смыслу.
    check("--diag: подсказка pip install PySide6",
          "pip install" in out and "PySide6" in out)
    check("--diag: НЕ пишет старую ложь «PySide6 не установлен»",
          "PySide6 не установлен" not in out)

    print("── server_gui.py без PySide6 (фатальный путь)")
    err_log = ROOT / "server_gui_error.log"
    if err_log.exists():
        err_log.unlink()
    r2 = subprocess.run([sys.executable, str(gui)], capture_output=True,
                        text=True, timeout=60, cwd=str(ROOT))
    out2 = r2.stdout + r2.stderr
    check("код выхода 1", r2.returncode == 1)
    check("в сообщении есть PySide6", "PySide6" in out2)
    check("есть подсказка про server_main.py", "server_main.py" in out2)
    check("отчёт записан в server_gui_error.log", err_log.exists())
    if err_log.exists():
        err_log.unlink()

    print(f"\nИТОГО: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
