#!/usr/bin/env python3
"""v3.8.2 — параметр сервера --max-file-size (лимит файла, человекочитаемо).

Раньше лимит задавался только старым --max-mb (голые МБ, дефолт жёстко 2048,
в шпаргалке не показывался, значение GUI-хоста из settings.json игнорировалось).
Теперь: --max-file-size 2G|1.5GB|500M|2ГБ (голое число = МБ), приоритет
--max-file-size > --max-mb > settings.json > 2 ГБ, лимит виден в шпаргалке,
ошибки — внятным текстом до старта сервера.

A. Парсер        — латиница/кириллица, запятая, пробелы, T; мусор/минус/ноль
                   и < 1 МБ — ValueError с человеческим текстом.
B. Приоритет     — --max-file-size > --max-mb > settings.json
                   (max_file_size_mb, туда пишет GUI-спиннер) > 2 ГБ;
                   мусор в settings тихо уходит в дефолт; --max-mb < 1 — ошибка.
C. parse_args    — новые флаги реально доходят до argparse (дефолты None).
D. RelayServer   — max_file_size доезжает до фасада без искажений.
E. Шпаргалка     — print_hint печатает строку «Лимит файла : N МБ (X ГБ)».
F. main() e2e    — мусорное значение: код выхода 2 и ОШИБКА в stderr ДО
                   старта сервера и записи settings.
G. Структурные   — README краток и документирует параметр, README_full
                   хранит историю, version.json = 3.8.2.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server_main
from lib.relay_server import RelayServer

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def expect_value_error(fn, *args) -> tuple[bool, str]:
    try:
        fn(*args)
        return False, ""
    except ValueError as e:
        return True, str(e)


def test_a_parser() -> None:
    print("A. Парсер размеров")
    cases = [
        ("2048", 2048),   # голое число = МБ (совместимость с --max-mb)
        ("2G", 2048), ("2g", 2048), ("2GB", 2048), ("2gb", 2048),
        ("1.5G", 1536), ("1,5G", 1536),   # запятая как десятичный разделитель
        ("500M", 500), ("500MB", 500), ("500мб", 500), ("500М", 500),
        ("2ГБ", 2048), ("2 ГБ", 2048), ("  2  G  ", 2048),
        ("1T", 1024 * 1024),
    ]
    for text, want in cases:
        try:
            got = server_main.parse_size_mb(text)
            check(f"parse_size_mb({text!r}) == {want}", got == want, f"got {got}")
        except ValueError as e:
            check(f"parse_size_mb({text!r}) == {want}", False, str(e))

    bad = ["abc", "", "2X", "0", "0.5", "512k", "-5", "G"]
    for text in bad:
        ok, msg = expect_value_error(server_main.parse_size_mb, text)
        check(f"parse_size_mb({text!r}) -> ValueError", ok, msg[:60])
    # сообщения должны быть человеческими
    ok, msg = expect_value_error(server_main.parse_size_mb, "abc")
    check("сообщение про примеры формата", ok and "2G" in msg)
    ok, msg = expect_value_error(server_main.parse_size_mb, "0")
    check("сообщение про минимум 1 МБ", ok and "1 МБ" in msg)


def test_b_priority() -> None:
    print("B. Приоритет --max-file-size > --max-mb > settings.json > 2ГБ")
    check("CLI --max-file-size сильнее всего",
          server_main.resolve_max_mb("1G", 512, {"max_file_size_mb": 7}) == 1024)
    check("старый --max-mb сильнее settings",
          server_main.resolve_max_mb(None, 512, {"max_file_size_mb": 7}) == 512)
    check("settings.json (GUI-спиннер) учитывается",
          server_main.resolve_max_mb(None, None, {"max_file_size_mb": 1024}) == 1024)
    check("строка в settings тоже читается",
          server_main.resolve_max_mb(None, None, {"max_file_size_mb": "4096"}) == 4096)
    check("пустой settings -> дефолт 2 ГБ",
          server_main.resolve_max_mb(None, None, {}) ==
          2 * 1024 * 1024 * 1024 // (1024 * 1024))
    ok, _ = expect_value_error(server_main.resolve_max_mb, None, 0, {})
    check("--max-mb=0 -> ValueError", ok)
    check("мусор в settings тихо -> дефолт",
          server_main.resolve_max_mb(None, None, {"max_file_size_mb": "abc"}) ==
          2 * 1024 * 1024 * 1024 // (1024 * 1024))
    check("ноль в settings тихо -> дефолт",
          server_main.resolve_max_mb(None, None, {"max_file_size_mb": 0}) ==
          2 * 1024 * 1024 * 1024 // (1024 * 1024))


def test_c_parse_args() -> None:
    print("C. parse_args: флаги доходят до argparse")
    old = sys.argv
    try:
        sys.argv = ["server_main.py", "--max-file-size", "2G"]
        ns = server_main.parse_args()
        check("--max-file-size принят строкой", ns.max_file_size == "2G")
        check("--max-mb по умолчанию None", ns.max_mb is None)
        sys.argv = ["server_main.py", "--max-mb", "512"]
        ns = server_main.parse_args()
        check("--max-mb принят", ns.max_mb == 512)
        check("--max-file-size по умолчанию None", ns.max_file_size is None)
        sys.argv = ["server_main.py"]
        ns = server_main.parse_args()
        check("без флагов оба None (решит resolve_max_mb)",
              ns.max_file_size is None and ns.max_mb is None)
    finally:
        sys.argv = old


def test_d_relay_wiring() -> None:
    print("D. RelayServer принимает max_file_size как раньше")
    with tempfile.TemporaryDirectory() as td:
        relay = RelayServer(Path(td), host_name="t",
                            max_file_size=5 * 1024 * 1024)
        check("relay.max_file_size == 5 МБ",
              relay.max_file_size == 5 * 1024 * 1024)
        try:
            relay.close()
        except Exception:
            pass


def test_e_hint() -> None:
    print("E. Шпаргалка печатает лимит файла")
    import argparse

    ns = argparse.Namespace(name="Емеля", port=8420, key="", bind="0.0.0.0",
                            max_mb=2048, data_dir="")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            server_main.print_hint(relay=object(), args=ns,
                                   data_dir=Path(tempfile.gettempdir()),
                                   admin_key="k")
        out = buf.getvalue()
    except Exception as e:  # get_interfaces и т.п. — среда без сети тоже валидна
        check("print_hint не упал", False, repr(e))
        return
    check("строка «Лимит файла : 2048 МБ (2 ГБ)»",
          "Лимит файла : 2048 МБ (2 ГБ)" in out)
    check("подсказка про --max-file-size в шпаргалке",
          "--max-file-size" in out)


def test_f_main_error_path() -> None:
    print("F. main(): мусорный размер — выход 2 ДО старта сервера")
    root = Path(__file__).resolve().parent.parent
    for argv, needle in (
        (["--max-file-size", "мусор"], "ОШИБКА"),
        (["--max-file-size", "0"], "1 МБ"),
        (["--max-mb", "0"], "ОШИБКА"),
    ):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(root / "server_main.py"),
                 "--data-dir", td, "--quiet", *argv],
                capture_output=True, text=True, timeout=120, cwd=str(root))
            out = (r.stdout or "") + (r.stderr or "")
            check(f"exit 2 для {' '.join(argv)!s}", r.returncode == 2,
                  f"rc={r.returncode}")
            check(f"внятная ошибка для {' '.join(argv)!s}", needle in out,
                  out[-120:].replace("\n", " "))
            # settings.json не должен был записаться (resolve раньше сохранения)
            check("settings не записан при ошибке",
                  not (Path(td) / "settings.json").exists())


def test_g_structural() -> None:
    print("G. Структурные проверки")
    root = Path(__file__).resolve().parent.parent
    src = (root / "server_main.py").read_text(encoding="utf-8")
    check("server_main: --max-file-size", "--max-file-size" in src)
    check("server_main: resolve_max_mb", "def resolve_max_mb" in src)
    check("server_main: строка шпаргалки", "Лимит файла" in src)

    readme = (root / "README.md").read_text(encoding="utf-8")
    check("README: параметр задокументирован", "--max-file-size" in readme)
    check("README: краток (нет простыни)", "СТРУКТУРА ПРОЕКТА" not in readme
          and len(readme) < 8000, f"{len(readme)} байт")
    check("README: ссылка на полную историю", "docs/README_full.md" in readme)

    full = (root / "docs" / "README_full.md").read_text(encoding="utf-8")
    check("README_full: блок v3.8.2 на месте", ">>> v3.8.2" in full)
    check("README_full: старые блоки живы (v3.6.3, v3.5.11)",
          ">>> v3.6.3" in full and ">>> v3.5.11" in full)

    ver = json.loads((root / "version.json").read_text(encoding="utf-8"))
    # (v3.8.3) «не ниже», а не «==»: тесты переживают бампы (принцип
    # test_fix_v363), иначе каждый релиз красил чужой тест
    vt = tuple(int(x) for x in str(ver.get("version", "0")).split(".")[:3])
    check("version.json не ниже 3.8.2", vt >= (3, 8, 2))


def main() -> int:
    print("=== tests/test_maxsize_v382.py — --max-file-size (v3.8.2) ===")
    test_a_parser()
    test_b_priority()
    test_c_parse_args()
    test_d_relay_wiring()
    test_e_hint()
    test_f_main_error_path()
    test_g_structural()
    print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
