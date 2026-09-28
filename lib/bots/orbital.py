"""OrbitalBot — бот «Orbital Defense»: защити планету с орбиты.

Структура полностью аналогична snake_bot.py:
  - хранит trigger (по умолчанию "orbital") и scores в JSON
  - matches_command ловит !orbital, алиасы !orbit/!orb, !orbital top, !orbital trigger X, !orbital help, !help orbital
  - process_command возвращает dict с action="open_game" для открытия окна игры

UI-часть (диалог) живёт в qt_app/widgets/orbital_game_dialog.py.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from lib.bots.base import BaseBot

_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")


class OrbitalBot(BaseBot):
    """Бот «Orbital Defense» — защити планету с орбиты.

    Команды в чате:
      !orbital              → открыть игру
      !orbital top          → открыть таблицу рекордов
      !orbital trigger X    → сменить триггер
      !orbital help         → справка

    Короткие алиасы !orbit / !orb — синонимы полного триггера (их можно
    и менять регистром: !Orbit тоже сработает).
    """

    # Жёсткие алиасы: пользователь по привычке пишет !orbit — должны открыть игру.
    # Коллизий с другими ботами нет (snake/voxel/chess начинаются иначе).
    _ALIASES = ("orbit", "orb")

    def __init__(self, data_dir: Path):
        super().__init__("orbital", "Orbital Defense", data_dir)
        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "orbital")
            self.set_data("scores", [])

    # ── Dispatcher hooks ────────────────────────────────────────────

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "orbital")

    def matches_command(self, text: str) -> bool:
        if not text:
            return False
        text = text.strip()
        if not text.startswith("!"):
            return False
        low = text.lower()
        # Триггер или один из алиасов, регистр не важен: !orbital / !orbit /
        # !orb, ровно или с подкомандой через пробел. "!orbitx" не матчится —
        # префикс обязан кончаться концом строки или пробелом.
        for cand in (self.trigger, *self._ALIASES):
            if low == f"!{cand}" or low.startswith(f"!{cand} "):
                return True
        return low in (f"!help {self.trigger}", "!help orbital", "!help orbit")

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        low = text.lower()
        t = self.trigger
        if low in (f"!help {t}", "!help orbital", "!help orbit"):
            return ("help", [])
        # Срезаем совпавший префикс с ИСХОДНОГО текста (аргументы сохраняют
        # регистр), префикс узнаём по lower-копии.
        for cand in (t, *self._ALIASES):
            prefix = f"!{cand}"
            if low == prefix:
                return ("", [])
            if low.startswith(prefix + " "):
                rest = text[len(prefix):].strip()
                if not rest:
                    return ("", [])
                parts = rest.split()
                return (parts[0], parts[1:])
        return None

    def get_meta(self) -> dict:
        return {
            "trigger": self.trigger,
            "name": self.name,
            "tick_ms": 16,  # 60 FPS для аркады
            "game": "orbital",
        }

    # ── Команды ──────────────────────────────────────────────────────

    def process_command(self, sender: str, command: str, args: list[str]) -> str | dict:
        cmd = (command or "").lower()
        if cmd == "":
            return {"action": "open_game", "tab": "play", "meta": self.get_meta()}
        if cmd == "top":
            return {"action": "open_game", "tab": "leaderboard", "meta": self.get_meta()}
        if cmd == "help":
            return self.get_help()
        if cmd == "trigger":
            return self._cmd_trigger(args)
        if cmd == "savescore":
            return self._cmd_save_score(sender, args)
        if cmd == "getscores":
            # silent=True — поллинговая команда, не спамит чат
            return {"action": "scores", "scores": self._top_scores(50), "silent": True}
        if cmd == "meta":
            # silent=True — поллинговая команда
            return {"action": "meta", **self.get_meta(), "silent": True}
        return f"❌ Неизвестная подкоманда. Напиши !{self.trigger} help"

    def _cmd_trigger(self, args: list[str]) -> str:
        if not args:
            return f"Текущий триггер: !{self.trigger}"
        new = args[0].strip().lower()
        if not _TRIGGER_RE.match(new):
            return "❌ Недопустимый триггер."
        old = self.trigger
        self.set_data("trigger", new)
        return f"✅ Триггер: !{old} → !{new}"

    def _cmd_save_score(self, sender: str, args: list[str]) -> dict:
        if not args:
            return {"action": "error", "text": "❌ Нет счёта"}
        try:
            score = int(args[0])
        except ValueError:
            return {"action": "error", "text": "❌ Неверный счёт"}
        wave = int(args[1]) if len(args) > 1 else 1
        scores: list[dict] = self.get_data("scores", [])
        entry = {"name": sender, "score": score, "wave": wave, "ts": int(time.time())}
        scores.append(entry)
        scores.sort(key=lambda s: (-s["score"], s["ts"]))
        scores = scores[:200]
        self.set_data("scores", scores)
        rank = next(
            (
                i + 1
                for i, s in enumerate(scores)
                if s["name"] == sender and s["score"] == score and s["ts"] == entry["ts"]
            ),
            None,
        )
        best = max((s["score"] for s in scores if s["name"] == sender), default=0)
        return {
            "action": "score_saved",
            "rank": rank,
            "best": best,
            "text": f"🛰️ {sender}: {score} очков (волна {wave}). Место: {rank}",
        }

    def _top_scores(self, limit: int = 10) -> list[dict]:
        return self.get_data("scores", [])[:limit]

    # ── Справка ──────────────────────────────────────────────────────

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"🛰️ Orbital Defense — защити планету с орбиты!\n"
            f"─────────────────────────────────────────────\n"
            f"!{t}         — начать игру\n"
            f"!{t} top     — таблица рекордов\n"
            f"!{t} trigger <name> — сменить триггер\n"
            f"!{t} help    — справка\n\n"
            f"Управление:\n"
            f"  ← →  — скорость вращения по орбите\n"
            f"  ↑ ↓  — радиус орбиты (ближе/дальше от планеты)\n"
            f"  Пробел / ЛКМ — выстрел лазером\n"
            f"  Shift — замедление времени (энергия)\n"
            f"  P — пауза\n"
            f"  R — рестарт\n\n"
            f"Механика:\n"
            f"  • Астероиды летят к планете под гравитацией\n"
            f"  • Чем дальше орбита — тем сильнее лазер, но медленнее поворот\n"
            f"  • Комбо за быстрое уничтожение (+множитель)\n"
            f"  • Power-ups: щит 🛡, скорострельность ⚡, бомба 💣, ремонт +"
        )
