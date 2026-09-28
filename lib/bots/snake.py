from __future__ import annotations

import re
import time
from pathlib import Path

from lib.bots.base import BaseBot

_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")


class SnakeBot(BaseBot):
    """Бот 'Змейка' — открывает полноценную аркадную игру в окне.

    В чате:
      !snake            → открыть окно игры (поле + турнирная таблица справа)
      !snake top        → открыть игру сразу на вкладке с топом
      !snake trigger X  → сменить триггер на !X (админская команда)
      !snake help       → справка
      !help snake       → справка (алиас)
      !help <trigger>   → справка (алиас по текущему триггеру)

    Игра — клиентская (QPainter canvas, QTimer 130 мс, WASD/стрелки).
    На сервере хранятся только:
      - текущий trigger (по умолчанию "snake")
      - таблица очков: scores = [{name, score, length, ts}]
    """

    def __init__(self, data_dir: Path):
        super().__init__("snake", "Змейка", data_dir)

        # Настройки игры (используются клиентом, но продублированы тут для /meta)
        self._grid_size = 25
        self._tick_ms = 130

        # Инициализируем глобальные данные если нет
        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "snake")
            self.set_data("scores", [])

    # ------------------------------------------------------------------
    # Dispatcher hooks
    # ------------------------------------------------------------------

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "snake")

    def matches_command(self, text: str) -> bool:
        if not text:
            return False
        text = text.strip()
        if not text.startswith("!"):
            return False
        trigger = self.trigger
        # !<trigger> / !<trigger> args
        if text == f"!{trigger}":
            return True
        if text.startswith(f"!{trigger} "):
            return True
        # !help snake / !help <trigger>
        if text.lower() == f"!help {trigger}":
            return True
        if text.lower() == "!help snake":
            return True
        return False

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        trigger = self.trigger
        low = text.lower()
        # !help <trigger> / !help snake → синтезируем подкоманду "help"
        if low == f"!help {trigger}" or low == "!help snake":
            return ("help", [])
        # !<trigger> [args...]
        rest = text[len(f"!{trigger}") :].strip()
        if not rest:
            return ("", [])
        parts = rest.split()
        return (parts[0], parts[1:])

    def get_meta(self) -> dict:
        return {
            "trigger": self.trigger,
            "name": self.name,
            "grid_size": self._grid_size,
            "tick_ms": self._tick_ms,
        }

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    def process_command(self, sender: str, command: str, args: list[str]) -> str | dict:
        cmd = (command or "").lower()

        if cmd == "":
            # !<trigger> — открыть игру
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
            return {
                "action": "scores",
                "scores": self._top_scores(50),
                "silent": True,
            }

        if cmd == "meta":
            # silent=True — поллинговая команда
            return {"action": "meta", **self.get_meta(), "silent": True}

        return f"❌ Неизвестная подкоманда. Напиши !{self.trigger} help"

    # ------------------------------------------------------------------
    # Подкоманды
    # ------------------------------------------------------------------

    def _cmd_trigger(self, args: list[str]) -> str:
        if not args:
            return (
                f"Текущий триггер: !{self.trigger}\nЧтобы сменить: !{self.trigger} trigger <новый>"
            )

        new_trigger = args[0].strip().lower()
        if not _TRIGGER_RE.match(new_trigger):
            return (
                f"❌ Триггер '{new_trigger}' недопустим. "
                "Буквы/цифры/_, 2-31 символ, начинается с буквы."
            )

        old = self.trigger
        self.set_data("trigger", new_trigger)
        return (
            f"✅ Триггер сменён: !{old} → !{new_trigger}\n"
            f"Теперь игра открывается по команде !{new_trigger}"
        )

    def _cmd_save_score(self, sender: str, args: list[str]) -> dict:
        if not args:
            return {"action": "error", "text": "❌ Не указан счёт"}

        try:
            score = int(args[0])
        except ValueError:
            return {"action": "error", "text": f"❌ Неверный счёт: {args[0]}"}

        length = 3
        if len(args) > 1:
            try:
                length = int(args[1])
            except ValueError:
                length = 3

        score = max(score, 0)

        scores: list[dict] = self.get_data("scores", [])
        entry = {
            "name": sender,
            "score": score,
            "length": length,
            "ts": int(time.time()),
        }
        scores.append(entry)
        # Сортируем по убыванию очков, оставляем топ-200
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
        best = self._best_for(sender)

        return {
            "action": "score_saved",
            "rank": rank,
            "total": len(scores),
            "best": best,
            "text": (
                f"🏆 {sender} набрал {score} очков (длина {length}). "
                f"Место в топе: {rank}. Лучший результат: {best}"
            ),
        }

    # ------------------------------------------------------------------
    # Хелперы для таблицы
    # ------------------------------------------------------------------

    def _top_scores(self, limit: int = 10) -> list[dict]:
        scores: list[dict] = self.get_data("scores", [])
        return scores[:limit]

    def _best_for(self, name: str) -> int:
        scores: list[dict] = self.get_data("scores", [])
        player_scores = [s["score"] for s in scores if s["name"] == name]
        return max(player_scores) if player_scores else 0

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"🐍 Змейка — аркадная игра в окне\n"
            f"─────────────────────────────────────\n"
            f"!{t}         — открыть игру (поле + таблица рекордов справа)\n"
            f"!{t} top     — открыть на вкладке таблицы\n"
            f"!{t} trigger <новый>  — сменить триггер (например: !{t} trigger worm → !worm)\n"
            f"!{t} help    — эта справка\n"
            f"!help {t}   — алиас справки\n\n"
            f"Управление в игре:\n"
            f"  ←↑↓→ или W/A/S/D — поворот змейки\n"
            f"  Пробел — пауза\n"
            f"  R — рестарт\n"
            f"  Esc — закрыть\n\n"
            f"Счёт сохраняется только при game over (в стену или в себя). "
            f"Турнирная таблица — топ-10 справа от поля, как в старых автоматах."
        )
