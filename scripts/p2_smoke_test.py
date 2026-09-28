"""P2 smoke test: Qt изолирован в qt_app, движки игр работают без PySide6.

1. Блокируем PySide6 → импортируем lib.games.{orbital,voxel}.{entities,engine},
   гоняем короткую симуляцию orbital на строковых клавишах.
2. С реальным PySide6 (если установлен) → импортируем канвасы и диалоги.
3. Проверяем, что ни один модуль в lib/ больше не импортирует PySide6
   (кроме файлов с try/except-заглушкой в entities).
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJ = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJ)

failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  OK  {name}")
    except Exception:
        failures.append(name)
        print(f" FAIL {name}")
        traceback.print_exc()


# ── 1. Движки без PySide6 ────────────────────────────────────────────
print("== engines without PySide6 ==")


def block_qt() -> None:
    import importlib

    for mod in list(sys.modules):
        if mod.startswith("PySide6"):
            del sys.modules[mod]

    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("PySide6"):
            raise ImportError(f"PySide6 заблокирован в тесте: {name}")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = guarded  # type: ignore[assignment]
    try:
        import lib.games.orbital.engine as og
        import lib.games.orbital.entities as oe
        import lib.games.voxel.engine as vg
        import lib.games.voxel.entities as ve

        assert not og.Qt if hasattr(og, "Qt") else True
        assert oe._HAS_QT is False, "заглушка QColor должна активироваться"
        assert ve._HAS_QT is False

        # Короткая симуляция orbital: клавиши — строки, QColor — заглушка
        eng = og.OrbitalEngine(400, 300)
        eng.key_down(oe.KEY_LEFT)
        eng.key_down(oe.KEY_SHIFT)
        eng.try_shoot()
        assert eng.lasers, "выстрел должен создать лазер"
        assert isinstance(eng.lasers[0].power, float)
        assert isinstance(eng.asteroids[0].color.red(), int) if eng.asteroids else True
        for _ in range(120):
            eng.tick(0.016)
            eng.update_particles(0.016)
        eng.key_up(oe.KEY_LEFT)
        print(f"    orbital sim: asteroids={len(eng.asteroids)}, score={eng.score}, "
              f"particles={len(eng.particles)}")

        # Voxel engine создаёт сессию без Qt
        veng = vg.VoxelEngine("smoke-session")
        veng.add_player("tester", "Tester")
        veng.tick()
        snap = veng.to_snapshot()
        assert isinstance(snap, dict)
        print(f"    voxel snapshot keys: {sorted(snap)[:5]}")
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]


check("lib.games.* imports + run without PySide6", block_qt)

# ── 2. Канвасы и диалоги с PySide6 ───────────────────────────────────
print("== qt_app canvases/dialogs with PySide6 ==")

try:
    import PySide6

    HAS_QT = True
except ImportError:
    HAS_QT = False
    print("  (PySide6 не установлен в этом окружении — пропускаем GUI-импорты)")


def import_canvases() -> None:
    from qt_app.widgets.games.orbital_canvas import OrbitalGameWidget
    from qt_app.widgets.games.voxel_canvas import VoxelCanvas

    assert hasattr(OrbitalGameWidget, "score_submitted")
    assert hasattr(VoxelCanvas, "input_changed")


def import_dialogs() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from qt_app.widgets.orbital_game_dialog import OrbitalGameDialog
    from qt_app.widgets.voxel_game_dialog import VoxelGameDialog

    w1 = OrbitalGameDialog(None, "http://127.0.0.1:1", "tester", "")
    w2 = VoxelGameDialog(None, "http://127.0.0.1:1", "tester", "")
    assert w1._board is not None
    assert w2._canvas is not None
    app.quit()


if HAS_QT:
    check("qt_app.widgets.games canvases import", import_canvases)
    check("game dialogs construct (offscreen)", import_dialogs)

# ── 3. lib/ не тянет PySide6 (кроме entities с заглушкой) ────────────
print("== lib/ Qt-free audit ==")


def audit_lib_qt() -> None:
    import ast
    from pathlib import Path

    import lib.bots.orbital
    import lib.bots.voxel
    import lib.games.chess_engine  # шахматы должны оставаться чистыми
    import lib.voxel_server

    bad = []
    for p in Path(PROJ, "lib").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        # Импорт PySide6 допустим ТОЛЬКО внутри try (try/except-заглушка)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                hits = [n for n in names if n.startswith("PySide6")]
                if not hits:
                    continue
                if node.col_offset > 0:  # вложен (import внутри try/def)
                    continue
                # проверяем: находится ли узел внутри ast.Try
                def inside_try(stack, target) -> bool:
                    while stack:
                        cur = stack.pop()
                        for child in ast.iter_child_nodes(cur):
                            if child is target:
                                return isinstance(cur, ast.Try)
                            stack.append(child)
                    return False

                if not inside_try([tree], node):
                    bad.append(f"{p.relative_to(PROJ)}:{node.lineno}")
    assert not bad, "PySide6 вне try/except в lib/: " + ", ".join(bad)


check("lib/ contains no hard PySide6 imports", audit_lib_qt)

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL P2 SMOKE CHECKS PASSED")
