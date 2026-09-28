"""Тест алиасов OrbitalBot (bugfix: !orbit не запускал игру — бот ловил
только полный триггер !orbital).

Запуск: python tests/test_orbit_alias.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.bots.orbital import OrbitalBot

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def main() -> int:
    print("== Алиасы OrbitalBot: !orbit / !orb ==\n")
    with tempfile.TemporaryDirectory() as td:
        bot = OrbitalBot(Path(td))

        # Матчинг
        check("A1. !orbit матчится", bot.matches_command("!orbit"))
        check("A2. !orb матчится", bot.matches_command("!orb"))
        check("A3. !orbital матчится (как раньше)", bot.matches_command("!orbital"))
        check("A4. !orbit top матчится", bot.matches_command("!orbit top"))
        check("A5. !Orbit top матчится (регистр)", bot.matches_command("!Orbit TOP"))
        check("A6. !orbitx НЕ матчится", not bot.matches_command("!orbitx"))
        check("A7. !orbitalx НЕ матчится", not bot.matches_command("!orbitalx"))
        check("A8. !snake НЕ матчится", not bot.matches_command("!snake"))
        check("A9. !orbit trap матчится (валидация подкоманды — позже, как у !orbital trap)", bot.matches_command("!orbit trap"))
        check("A10. !help orbit матчится", bot.matches_command("!help orbit"))

        # Парсинг: аргументы сохраняют регистр
        p = bot.parse_command("!orbit")
        check("B1. !orbit → пустая команда", p == ("", []))
        p = bot.parse_command("!orbit Top")
        check("B2. !orbit Top → ('Top', []) — регистр аргумента сохранён", p == ("Top", []))
        r = bot.process_command("Вася", "Top", [])
        check("B2a. process_command приводит 'Top' к top (открывает рейтинг)", isinstance(r, dict) and r.get("tab") == "leaderboard")
        p = bot.parse_command("!Orbit trigger Xy")
        check("B3. !Orbit trigger Xy → ('trigger', ['Xy'])", p == ("trigger", ["Xy"]))
        p = bot.parse_command("!orb top")
        check("B4. !orb top → ('top', [])", p == ("top", []))
        p = bot.parse_command("!orbital")
        check("B5. !orbital → пустая команда", p == ("", []))
        p = bot.parse_command("!help orbit")
        check("B6. !help orbit → help", p == ("help", []))
        check("B7. !orbitx → None", bot.parse_command("!orbitx") is None)

        # process_command открывает игру
        r = bot.process_command("Вася", "", [])
        check("C1. !orbit открывает игру", isinstance(r, dict) and r.get("action") == "open_game")
        r = bot.process_command("Вася", "top", [])
        check("C2. !orbit top → вкладка рейтинга", isinstance(r, dict) and r.get("tab") == "leaderboard")

        # Смена триггера: алиасы продолжают работать
        bot.process_command("Вася", "trigger", ["space"])
        check("D1. после смены триггера !space матчится", bot.matches_command("!space"))
        check("D2. алиас !orbit всё ещё матчится", bot.matches_command("!orbit"))
        check("D3. старый !orbital больше не матчится", not bot.matches_command("!orbital"))

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
