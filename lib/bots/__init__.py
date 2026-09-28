"""lib.bots — ВСЕ боты Friend Relay в одном пакете.

Слои проекта:
    lib/games/      — чистые игровые движки (без сети и Qt): chess_engine,
                      orbital/, voxel/
    lib/bots/       — ИИ-обёртки над движками + менеджер: подписки на
                      команды чата, хранение счётов, сетевые ответы
    qt_app/         — только UI

Публичный API:
    from lib.bots import BaseBot, BotManager

Состав:
    base.py         — BaseBot: каркас бота (команды, данные, потоки)
    manager.py      — BotManager: реестр ботов, /dispatch, списки
    chess.py        — ChessBot (ELO, матчи)      над games/chess_engine
    orbital.py      — OrbitalBot                 над games/orbital
    voxel.py        — VoxelShooterBot            над games/voxel
    snake.py        — SnakeBot (встраивался в voxel-канву)
    night_shift.py  — NightShiftBot (ночной режим)
"""
from lib.bots.base import BaseBot
from lib.bots.manager import BotManager

__all__ = ["BaseBot", "BotManager"]
