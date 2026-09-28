"""Сущности Voxel Shooter — чистые dataclasses без логики.

Могут быть сериализованы в dict/JSON для передачи по сети между
сервером (VoxelEngine) и клиентом (VoxelCanvas).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# QColor нужен только для отрисовки (canvas.py), но entities используется и в
# engine.py (без Qt). Делаем QColor опциональным — если PySide6 нет,
# используем простой tuple (r, g, b) вместо QColor. Canvas конвертирует.
try:
    from PySide6.QtGui import QColor
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

    class QColor:  # type: ignore[no-redef]
        """Заглушка QColor если PySide6 не установлен. Engine работает без неё."""
        def __init__(self, r: int = 0, g: int = 0, b: int = 0, a: int = 255):
            self._r, self._g, self._b, self._a = r, g, b, a

        def red(self) -> int:
            return self._r

        def green(self) -> int:
            return self._g

        def blue(self) -> int:
            return self._b

# Размеры поля — фиксированы для всех сессий
TILE_SIZE = 40
FIELD_W = 900
FIELD_H = 700
COLS = FIELD_W // TILE_SIZE  # 22
ROWS = FIELD_H // TILE_SIZE  # 17

# Строковые идентификаторы клавиш/кнопок — engine не зависит от Qt.
# Canvas (клиент) конвертирует Qt.Key в эти строки перед отправкой на сервер.
KEY_W = "w"
KEY_A = "a"
KEY_S = "s"
KEY_D = "d"
MOUSE_LEFT = "left"

# Команды
TEAM_RED = "red"
TEAM_BLUE = "blue"
TEAM_NONE = "none"

TEAM_COLORS = {
    TEAM_RED: QColor(220, 60, 60),
    TEAM_BLUE: QColor(60, 120, 220),
    TEAM_NONE: QColor(60, 220, 80),  # для ботов без команды
}


@dataclass
class Player:
    """Игрок в сессии — человек или бот.

    id — уникальный идентификатор (для людей = имя, для ботов = "bot_1" и т.п.)
    name — отображаемое имя
    team — red/blue/none
    is_bot — True если NPC
    """
    id: str
    name: str
    team: str = TEAM_NONE
    is_bot: bool = False
    x: float = 60.0
    y: float = 60.0
    size: int = 30
    hp: int = 100
    max_hp: int = 100
    speed: float = 4.5
    money: int = 0
    kills: int = 0
    deaths: int = 0
    weapon: str = "pistol"  # pistol | rifle
    has_rifle: bool = False
    grenades: int = 2
    shoot_cooldown: int = 0
    grenade_cooldown: int = 0
    # AI стратегия для ботов
    ai_strategy: str = "aggressive"  # aggressive | sniper | flanker | defensive
    ai_strafe_dir: int = 1  # направление стрейфа (1 или -1)
    ai_strafe_timer: int = 0  # когда менять направление стрейфа
    # Input от клиента: строковые идентификаторы ("w","a","s","d" и "left")
    keys: set = field(default_factory=set)  # set[str]
    mouse_btn: set = field(default_factory=set)  # set[str] — "left" etc.
    mouse_x: int = 0
    mouse_y: int = 0
    shop_open: bool = False
    alive: bool = True

    @property
    def color(self) -> QColor:
        return TEAM_COLORS.get(self.team, TEAM_COLORS[TEAM_NONE])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "team": self.team,
            "is_bot": self.is_bot,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "size": self.size,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "money": self.money,
            "kills": self.kills,
            "deaths": self.deaths,
            "weapon": self.weapon,
            "grenades": self.grenades,
            "alive": self.alive,
            "shop_open": self.shop_open,
        }


# ── Типы блоков ────────────────────────────────────────────────────
# Каждый тип: (hp, color, bulletproof)
# bulletproof = пули не наносят урон, только гранаты
BLOCK_TYPES = {
    "wood":     {"hp": 30,  "color": (140, 100, 50),  "bulletproof": False, "label": "🪵 Дерево"},
    "brick":    {"hp": 60,  "color": (150, 80, 60),    "bulletproof": False, "label": "🧱 Кирпич"},
    "concrete": {"hp": 200, "color": (120, 120, 130),  "bulletproof": True,  "label": "🏗 Бетон"},
}


@dataclass
class Wall:
    """Воксельная стена — разрушаемая. Тип определяет HP и пуленепробиваемость."""
    gx: int
    gy: int
    x: float = 0.0
    y: float = 0.0
    tile_size: int = TILE_SIZE
    hp: int = 60
    max_hp: int = 60
    alive: bool = True
    block_type: str = "brick"        # wood | brick | concrete
    bulletproof: bool = False        # пули не наносят урон (только гранаты)

    def __post_init__(self):
        self.x = self.gx * self.tile_size
        self.y = self.gy * self.tile_size
        # Если тип задан, применяем его характеристики
        bt = BLOCK_TYPES.get(self.block_type)
        if bt and self.hp == 60 and self.max_hp == 60:
            self.hp = bt["hp"]
            self.max_hp = bt["hp"]
            self.bulletproof = bt["bulletproof"]

    def to_dict(self) -> dict:
        return {
            "gx": self.gx, "gy": self.gy,
            "x": self.x, "y": self.y,
            "tile_size": self.tile_size,
            "hp": self.hp, "max_hp": self.max_hp,
            "alive": self.alive,
            "block_type": self.block_type,
        }


@dataclass
class Bullet:
    """Пуля — летит по прямой, наносит урон при попадании."""
    x: float
    y: float
    vx: float
    vy: float
    damage: int
    is_player: bool = True  # True = от игрока, False = от врага
    owner_id: str = ""  # id игрока который выпустил (для score)
    alive: bool = True

    @classmethod
    def from_angle(cls, x: float, y: float, angle: float, speed: float,
                   damage: int, is_player: bool = True, owner_id: str = "") -> Bullet:
        return cls(
            x=x, y=y,
            vx=math.cos(angle) * speed,
            vy=math.sin(angle) * speed,
            damage=damage,
            is_player=is_player,
            owner_id=owner_id,
        )

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 1), "y": round(self.y, 1),
            "is_player": self.is_player,
        }


@dataclass
class Grenade:
    """Граната — летит, отскакивает от стен, взрывается по таймеру."""
    x: float
    y: float
    vx: float
    vy: float
    timer: int = 90  # кадров до взрыва (~1.5 сек)
    radius: int = 6
    alive: bool = True
    owner_id: str = ""

    @classmethod
    def from_target(cls, x: float, y: float, tx: float, ty: float,
                    owner_id: str = "") -> Grenade:
        angle = math.atan2(ty - y, tx - x)
        return cls(x=x, y=y, vx=math.cos(angle) * 5, vy=math.sin(angle) * 5,
                   owner_id=owner_id)

    def to_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1)}


@dataclass
class Explosion:
    """Взрыв — анимация (растёт круг)."""
    x: float
    y: float
    r: int = 5
    max_r: int = 100
    alive: bool = True

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "r": int(self.r), "max_r": self.max_r}


@dataclass
class Enemy:
    """ИИ-враг (PvE) — преследует ближайшего игрока, стреляет."""
    x: float
    y: float
    wave: int = 1
    size: int = 30
    hp: int = 80
    max_hp: int = 80
    speed: float = 1.2
    alive: bool = True
    shoot_cooldown: int = 0
    target_id: str = ""  # кого преследует

    def __post_init__(self):
        self.hp = 80 + self.wave * 15
        self.max_hp = self.hp
        self.speed = 1.2 + min(self.wave * 0.12, 2.5)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 1), "y": round(self.y, 1),
            "size": self.size, "hp": self.hp, "max_hp": self.max_hp,
            "alive": self.alive,
        }


@dataclass
class Particle:
    """Частица взрыва/крови — для визуала."""
    x: float
    y: float
    vx: float
    vy: float
    life: int
    max_life: int
    color: QColor = field(default_factory=lambda: QColor(255, 255, 255))
    size: int = 4

    @classmethod
    def spawn(cls, x: float, y: float, color: QColor, count: int = 8) -> list[Particle]:
        return [
            cls(
                x=x, y=y,
                vx=math.cos(random.uniform(0, 2 * math.pi)) * random.uniform(1, 5),
                vy=math.sin(random.uniform(0, 2 * math.pi)) * random.uniform(1, 5),
                life=random.randint(10, 35),
                max_life=35,
                color=color,
            )
            for _ in range(count)
        ]

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 1), "y": round(self.y, 1),
            "life": self.life, "max_life": self.max_life,
            "color": (self.color.red(), self.color.green(), self.color.blue()),
        }
