"""VoxelShooterBot — мультиплеерный top-down шутер.

Отличается от snake/orbital тем, что игра синхронная и мультиплеерная:
  - !voxel              → открыть лобби (список сессий + создать/войти)
  - !voxel help         → справка
  - !voxel trigger X    → сменить триггер

Команды savescore/getscores не нужны — игра realtime, очки не сохраняются
отдельно (показываются в HUD). При выходе из сессии статистика теряется.

Серверная часть:
  - lib/voxel_server.py:VoxelSessionServer — TCP-сервер (порт = HTTP+2)
  - lib/games/voxel/engine.py:VoxelEngine — симуляция
  - qt_app/widgets/games/voxel_canvas.py:VoxelCanvas — клиентский рендер
"""
from __future__ import annotations

import re
from pathlib import Path

from lib.bots.base import BaseBot

_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")


class VoxelShooterBot(BaseBot):
    """Бот «Воксельный Шутер» — мультиплеерный top-down шутер."""

    def __init__(self, data_dir: Path):
        super().__init__("voxel_shooter", "Воксельный Шутер", data_dir)
        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "voxel")
            # У мультиплеерной игры нет таблицы очков — статистика живёт в сессии

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "voxel")

    def matches_command(self, text: str) -> bool:
        if not text:
            return False
        text = text.strip()
        if not text.startswith("!"):
            return False
        t = self.trigger
        return (
            text == f"!{t}"
            or text.startswith(f"!{t} ")
            or text.lower() in (f"!help {t}", "!help voxel_shooter")
        )

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        t = self.trigger
        low = text.lower()
        if low in (f"!help {t}", "!help voxel_shooter"):
            return ("help", [])
        rest = text[len(f"!{t}"):].strip()
        if not rest:
            return ("", [])
        parts = rest.split()
        return (parts[0], parts[1:])

    def get_meta(self) -> dict:
        return {
            "trigger": self.trigger,
            "name": self.name,
            "tick_ms": 16,  # 60 FPS
            "game": "voxel_shooter",
            "multiplayer": True,  # ключевой флаг для UI
        }

    def process_command(self, sender: str, command: str, args: list[str]) -> str | dict:
        cmd = (command or "").lower()
        if cmd == "":
            return {"action": "open_game", "tab": "lobby", "meta": self.get_meta()}
        if cmd == "help":
            return self.get_help()
        if cmd == "trigger":
            return self._cmd_trigger(args)
        if cmd == "meta":
            return {"action": "meta", **self.get_meta()}
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

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"🔫 Воксельный Шутер — мультиплеерный top-down шутер\n"
            f"─────────────────────────────────────────────────────\n"
            f"!{t}         — открыть лобби (создать/войти в сессию)\n"
            f"!{t} trigger <name> — сменить триггер\n"
            f"!{t} help    — эта справка\n\n"
            f"Управление в игре:\n"
            f"  W/A/S/D — бегать\n"
            f"  ЛКМ     — стрелять (автострельба если купил автомат)\n"
            f"  G       — кинуть гранату\n"
            f"  1/2     — пистолет / автомат\n"
            f"  B       — магазин (деньги за убийства)\n\n"
            f"🤖 Боты: в лобби кнопка «Добавить бота» — NPC в твою сессию\n"
            f"👥 Команды: при входе в сессию выбери Red/Blue/None\n"
            f"   Если в сессии ≥2 разных команд — PvP включается\n"
            f"🏗 Стены разрушаются выстрелами и взрывами!"
        )
