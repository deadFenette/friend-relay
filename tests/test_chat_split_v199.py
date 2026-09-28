"""Разборка chat_screen.py (v1.9.9) — проверка связки без запуска Qt.

chat_ui_builder.build_chat_ui(screen) и widgets/pinned_dialog.py работают
с «чужим» экраном через атрибуты/методы (screen._input, screen._on_send,
...). Раньше это был один класс — опечатка была бы поймана при запуске
GUI; после разборки ловим её статически:

  1. каждый атрибут, который builder ПИШЕТ (screen._x = ...), должен
     использоваться/читаться в chat_screen.py (self._x);
  2. каждый МЕТОД экрана, который builder/диалог вызывают или подключают
     к сигналам, должен быть объявлен в ChatScreen;
  3. обратная совместимость: FeedPage/FeedPageManager реэкспортируются
     из chat_screen (старые импорты не должны сломаться).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJ = Path(__file__).resolve().parent.parent
BUILDER = PROJ / "qt_app" / "screens" / "chat_ui_builder.py"
SCREEN = PROJ / "qt_app" / "screens" / "chat_screen.py"
PINNED = PROJ / "qt_app" / "widgets" / "pinned_dialog.py"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def screen_members(src: str) -> tuple[set[str], set[str], set[str]]:
    """(методы, присваиваемые поля, ЧИТАЕМЫЕ поля self._x) класса ChatScreen."""
    tree = ast.parse(src)
    methods: set[str] = set()
    attrs: set[str] = set()
    reads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ChatScreen":
            for item in ast.walk(node):
                if isinstance(item, ast.FunctionDef):
                    methods.add(item.name)
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"):
                            attrs.add(t.attr)
                if isinstance(item, ast.Attribute):
                    if (isinstance(item.value, ast.Name)
                            and item.value.id == "self"
                            and isinstance(item.ctx, ast.Load)):
                        reads.add(item.attr)
    return methods, attrs, reads


def builder_writes_and_calls(src: str) -> tuple[set[str], set[str]]:
    """(записываемые атрибуты screen._x, вызываемые/читаемые имена)."""
    tree = ast.parse(src)
    writes: set[str] = set()
    uses: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "screen"):
                    writes.add(t.attr)
        if isinstance(node, ast.Attribute):
            if (isinstance(node.value, ast.Name) and node.value.id == "screen"):
                uses.add(node.attr)
    return writes, uses


def main() -> int:
    builder_src = BUILDER.read_text(encoding="utf-8")
    screen_src = SCREEN.read_text(encoding="utf-8")
    pinned_src = PINNED.read_text(encoding="utf-8")

    methods, attrs, reads = screen_members(screen_src)
    writes, uses = builder_writes_and_calls(builder_src)
    _, pinned_uses = builder_writes_and_calls(pinned_src)

    print("1. экран: всё, что он ЧИТАЕТ у self, кто-то создаёт")
    # читаемые экраном self._x должны быть: полем __init__, свойством/методом
    # (property), созданы builder'ом или диалогом пинов — иначе AttributeError
    provided = attrs | methods | writes | pinned_uses
    # публичные имена без подчёркивания — методы базового QWidget
    # (setUpdatesEnabled, window, ...), их не создаём
    missing_reads = sorted(r for r in reads if r not in provided and r.startswith("_"))
    check(f"члены экрана ({len(reads)}) обеспечены", not missing_reads,
          f"никто не создаёт: {missing_reads}")

    print("2. builder: все вызываемые методы экрана существуют")
    # uses включает и атрибуты-виджеты; методы = те, что не в attrs и не
    # создаются builder'ом
    created = writes
    missing_methods = sorted(
        u for u in uses
        if u not in methods and u not in created and u not in attrs
    )
    check(f"методы/свойства ({len(uses) - len(created)}) на месте",
          not missing_methods, f"не найдены: {missing_methods}")

    print("3. pinned_dialog: всё, что он читает у экрана, существует")
    missing_pinned = sorted(
        u for u in pinned_uses
        if u not in methods and f"self.{u}" not in screen_src
    )
    # _pinned_messages/_bridge/_scroll и т.п. создаются builder'ом или __init__
    missing_pinned = [u for u in missing_pinned
                      if u not in attrs and f"screen._{u}" not in builder_src]
    check(f"члены экрана для диалога пинов ({len(pinned_uses)}) на месте",
          not missing_pinned, f"не найдены: {missing_pinned}")

    print("4. совместимость: chat_screen реэкспортирует страницы ленты")
    check("FeedPage импортируется из chat_screen",
          "feed_pages import" in screen_src and "FeedPage," in screen_src)
    check("FeedPageManager импортируется из chat_screen",
          "FeedPageManager" in screen_src)
    check("feed_pages.py содержит FeedPageManager",
          "class FeedPageManager" in (PROJ / "qt_app/screens/feed_pages.py")
          .read_text(encoding="utf-8"))

    print()
    print(f"ИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
