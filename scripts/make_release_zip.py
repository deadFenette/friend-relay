#!/usr/bin/env python3
"""Сборка релизного ZIP Friend Relay.

Формат как в прошлых релизах: всё внутри папки friend_relay/,
без мусора (__pycache__, pyc, tool-results, upload, download, .git,
worklog разработчика, .env с локальными секретами).

Версия читается из version.json (одна точка правды - раньше номер
версии был захардкожен в этом скрипте, и после бампа version.json
собирался zip со СТАРЫМ именем).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def release_version() -> str:
    """Версия релиза из version.json (фолбэк - чтобы сборка не падала,
    если файл повредили: пусть лучше zip соберётся с именем «unknown»)."""
    try:
        return json.loads(
            (ROOT / "version.json").read_text(encoding="utf-8")
        ).get("version") or "unknown"
    except Exception:
        return "unknown"


INCLUDE_DIRS = ("lib", "voice", "qt_app", "web_client", "tests", "scripts",
                "docs", "legacy")
INCLUDE_FILES = (
    "README.md", "ARCHITECTURE.md",
    "RUN.bat", "RUN_LEGACY.bat", "RUN_TELEMETRY.bat",
    "build_exe.bat", "build_legacy_exe.bat",
    "main_qt.py", "telemetry_viewer.py",
    "pyproject.toml", "requirements.txt",
    "version.json", ".gitignore",
)
EXCLUDE_PARTS = {"__pycache__", ".git", "tool-results", "download", "upload",
                 ".venv", "build", "dist", "node_modules", "skills"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log")


def wanted(path: Path) -> bool:
    return not (set(path.parts) & EXCLUDE_PARTS
                or path.suffix in EXCLUDE_SUFFIX)


def main() -> None:
    out = ROOT / "download" / f"friend_relay_v{release_version()}.zip"
    files: list[Path] = []
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and wanted(p):
                files.append(p)
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.is_file():
            files.append(p)

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(set(files)):
            z.write(p, "friend_relay/" + p.relative_to(ROOT).as_posix())
    print(f"OK: {out} ({len(set(files))} файлов, "
          f"{out.stat().st_size // 1024} КиБ)")


if __name__ == "__main__":
    main()
