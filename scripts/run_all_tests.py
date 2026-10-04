#!/usr/bin/env python3
"""Авто-раннер всех тестов friend-relay (v3.8.1).

Кроссплатформенная замена scripts/run_regression.sh: тот же принцип
(каждый tests/test_*.py - отдельный процесс), но добавлено то, чего
sh-версии не хватало для CI и отладки:

  - ПЕР-ТЕСТОВЫЙ ТАЙМАУТ (по умолчанию 300с): зависший тест убивается
    и помечается FAIL(timeout) - раньше один hang замораживал весь
    прогон, и приходилось искать виновного руками.
  - --ci: компактный вывод + GitHub Actions аннотации ::error с файлом
    упавшего теста (видны прямо в PR).
  - --only / --skip: фильтры по подстроке имени файла.
  - --list: показать план прогона и выйти.
  - Нормализация сводок PASS=N/FAIL=N (наследие run_regression.sh),
    подсчёт итогов, код возврата 1 при любом FAIL.

По умолчанию пропускаются тесты, требующие дисплей/аудио/Windows
(qt_*, test_v195_qt, test_legacy_live*) - как в run_regression.sh.

Тесты из TEST_NEEDS требуют зависимости СВЕРХ безголового набора CI
(например playwright с браузером): если модуля нет в этом интерпретаторе,
тест помечается SKIP (не FAIL, не считается упавшим) - CI остаётся быстрым
и детерминированным, а тяжёлые браузерные E2E гоняются локально/на релизе.

Использование:
  python scripts/run_all_tests.py              # весь headless-регресс
  python scripts/run_all_tests.py --ci         # в GitHub Actions
  python scripts/run_all_tests.py --only voice # только голосовое
  python scripts/run_all_tests.py --skip stability --timeout 120
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# Тесты, требующие дисплей/Qt-рендер/Windows: в headless-песочнице и CI
# их не гоняем (та же политика, что в scripts/run_regression.sh).
DEFAULT_SKIP = ("test_qt_", "test_v195_qt.py", "test_legacy_live")

# (CI-fix) Зависимости сверх безголового набора CI: имя файла -> модули.
# Нет модуля в этом интерпретаторе -> SKIP (прямой запуск такого теста
# тоже должен сам завершаться 0 - см. test_web_images_v368.py).
TEST_NEEDS = {
    "test_web_images_v368.py": ("playwright",),
}

SUMMARY_RE = re.compile(
    r"(?:PASS=(?P<pa>\d+)|(?P<oa>\d+)\s*OK)\D*?"
    r"(?:PASS=(?P<pf>\d+)|(?P<of>\d+)\s*FAIL)?",
    re.IGNORECASE,
)


def parse_summary(out: str) -> tuple[int, int]:
    """Достаёт итоги теста из его вывода: 'N OK / M FAIL', 'PASS=N FAIL=M',
    'Итого: N OK, M FAIL'. Возвращает (ok, fail); при нечитаемой сводке - (0, 0)."""
    norm = out.replace("PASS=", "OK=")
    norm = re.sub(r"OK=(\d+)", r"\1 OK", norm)
    norm = re.sub(r"FAIL=(\d+)", r"\1 FAIL", norm)
    pairs = re.findall(r"(\d+)\s*OK\D{0,20}?(\d+)\s*FAIL", norm)
    if pairs:
        ok, fail = pairs[-1]
        return int(ok), int(fail)
    ok = re.findall(r"(\d+)\s*OK", norm)
    fail = re.findall(r"(\d+)\s*FAIL", norm)
    if ok or fail:
        return int(ok[-1]) if ok else 0, int(fail[-1]) if fail else 0
    return 0, 0


def _module_ok(py: str, module: str) -> bool:
    """Есть ли модуль в этом интерпретаторе (дешёвая проба без импорта)."""
    probe = ("import importlib.util,sys; "
             f"sys.exit(0 if importlib.util.find_spec({module!r}) else 1)")
    try:
        return subprocess.run([py, "-c", probe],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def discover(skip: tuple[str, ...], only: tuple[str, ...]) -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    res = []
    for f in files:
        name = f.name
        if any(s in name for s in skip):
            continue
        if only and not any(o in name for o in only):
            continue
        res.append(f)
    return res


def run_one(py: str, path: Path, timeout: float) -> tuple[int, str, float, bool]:
    """Возвращает (rc, вывод, секунды, timed_out)."""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [py, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), time.monotonic() - t0, False
    except subprocess.TimeoutExpired as e:
        out = ""
        if e.stdout:
            out += e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else e.stdout
        if e.stderr:
            out += e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else e.stderr
        out += f"\n[runner] убит по таймауту {timeout:.0f}с"
        return 124, out, time.monotonic() - t0, True
    except OSError as e:
        return 127, f"[runner] не удалось запустить: {e}", time.monotonic() - t0, False


def main() -> int:
    ap = argparse.ArgumentParser(description="Авто-раннер тестов friend-relay")
    ap.add_argument("--only", nargs="*", default=[], help="подстроки имён: гонять только их")
    ap.add_argument("--skip", nargs="*", default=[], help="подстроки имён: исключить")
    ap.add_argument("--timeout", type=float, default=300.0, help="таймаут одного теста, сек")
    ap.add_argument("--py", default=sys.executable, help="интерпретатор для тестов")
    ap.add_argument("--ci", action="store_true", help="компактный вывод + аннотации GitHub Actions")
    ap.add_argument("--list", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    skip = DEFAULT_SKIP + tuple(args.skip)
    files = discover(skip, tuple(args.only))
    if args.list:
        for f in files:
            print(f.name)
        print(f"=== {len(files)} тестов (skip: {', '.join(skip)}) ===")
        return 0

    if not files:
        print("[runner] тесты не найдены - проверь --only/--skip")
        return 2

    print(f"[runner] {len(files)} тестов, py={args.py}, timeout={args.timeout:.0f}s"
          + (", режим CI" if args.ci else ""))
    total_ok = total_fail = 0
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    t_start = time.monotonic()

    for i, path in enumerate(files, 1):
        # (CI-fix) SKIP по отсутствию зависимости (TEST_NEEDS): не запускаем
        # и не считаем упавшим - печатаем строку со статусом SKIP.
        needs = TEST_NEEDS.get(path.name, ())
        missing = [m for m in needs if not _module_ok(args.py, m)]
        if missing:
            line = (f"[{i:02d}/{len(files)}] {path.name:42s} {'SKIP':7s} "
                    f"{'':6s}  (нет: {', '.join(missing)})")
            print(line, flush=True)
            skipped.append((path.name, ", ".join(missing)))
            continue
        rc, out, secs, timed_out = run_one(args.py, path, args.timeout)
        ok, fail = parse_summary(out)
        if rc == 0 and fail == 0 and (ok > 0 or not out.strip()):
            # rc=0 без сводки считаем пройденным (1 проверка на сам запуск)
            ok = max(ok, 1) if not out.strip() or ok == 0 else ok
        status = "OK" if (rc == 0 and not timed_out) else ("TIMEOUT" if timed_out else "FAIL")
        line = f"[{i:02d}/{len(files)}] {path.name:42s} {status:7s} {secs:6.1f}s  ({ok} OK / {fail} FAIL)"
        if args.ci and status == "OK":
            print(line, flush=True)
        else:
            print(line, flush=True)
            if status != "OK" or fail > 0:
                tail = "\n".join(out.strip().splitlines()[-25:])
                print(tail + "\n", flush=True)
                if args.ci and status != "OK":
                    print(f"::error file=tests/{path.name}::тест {status} (rc={rc})", flush=True)
        if status != "OK" or fail > 0:
            failed.append((path.name, status if status != "OK" else f"{fail} FAIL"))
            total_fail += fail or 1
        else:
            total_ok += ok

    wall = time.monotonic() - t_start
    print("=" * 70)
    print(f"ИТОГО: {total_ok} OK / {total_fail} FAIL за {wall:.0f}с; "
          f"упавших файлов: {len(failed)}"
          + (f"; пропущено (нет зависимостей): {len(skipped)}" if skipped else ""))
    for name, why in failed:
        print(f"  - {name}: {why}")
    for name, why in skipped:
        print(f"  [SKIP] {name}: нет {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
