"""Сущности игры Orbital Defense.

Чистые данные без логики: хранение + базовые константы (типы астероидов,
power-up'ов). Никакой отрисовки, никакого ввода — это зона engine.py / canvas.py.

Разнесено в отдельный модуль чтобы:
- engine.py работал с типами, а не с QPainter-зависимым кодом
- можно было переиспользовать сущности в тестах без поднятия Qt
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# QColor нужен только для отрисовки (canvas.py), но entities используется и в
# engine.py (без Qt). Делаем QColor опциональным — если PySide6 нет,
# используем простую заглушку вместо QColor. Canvas конвертирует.
try:
    from PySide6.QtGui import QColor
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

    class QColor:  # type: ignore[no-redef]
        """Заглушка QColor если PySide6 не установлен. Engine работает без неё.

        Поддерживает основной API, который использует engine: конструктор
        (включая копирующий QColor(other), как в реальном Qt), red/green/blue,
        alpha, lighter.
        """

        def __init__(self, r=0, g: int = 0, b: int = 0, a: int = 255):
            if hasattr(r, "red"):  # копирующий конструктор QColor(other)
                self._r = r.red()
                self._g = r.green()
                self._b = r.blue()
                self._a = r.alpha() if hasattr(r, "alpha") else 255
            else:
                self._r, self._g, self._b, self._a = int(r), g, b, a

        def red(self) -> int:
            return self._r

        def green(self) -> int:
            return self._g

        def blue(self) -> int:
            return self._b

        def alpha(self) -> int:
            return self._a

        def lighter(self, factor: int = 150) -> QColor:
            f = factor / 100.0
            return QColor(min(255, int(self._r * f)),
                          min(255, int(self._g * f)),
                          min(255, int(self._b * f)), self._a)

# ── Идентификаторы клавиш ────────────────────────────────────────────
# Строковые идентификаторы клавиш — engine не зависит от Qt.
# Canvas (qt_app/widgets/games/orbital_canvas.py) конвертирует Qt.Key
# в эти строки перед вызовом engine.key_down / key_up.
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_UP = "up"
KEY_DOWN = "down"
KEY_SHIFT = "shift"

# ── Астероиды ──────────────────────────────────────────────────────────

@dataclass
class AsteroidType:
    hp: int
    radius: float
    speed: float
    score: int
    color: QColor


ASTEROID_TYPES: dict[str, AsteroidType] = {
    "small":    AsteroidType(hp=1, radius=8,  speed=2.0, score=10, color=QColor(255, 180,  80)),
    "medium":   AsteroidType(hp=2, radius=14, speed=1.4, score=25, color=QColor(255, 100,  60)),
    "large":    AsteroidType(hp=4, radius=22, speed=0.9, score=60, color=QColor(200,  60,  60)),
    "fast":     AsteroidType(hp=1, radius=6,  speed=3.5, score=40, color=QColor(255, 255, 100)),
    "splitter": AsteroidType(hp=3, radius=18, speed=1.1, score=80, color=QColor(200,  80, 200)),
}


@dataclass
class Asteroid:
    """Астероид, летящий к планете под действием гравитации."""
    x: float
    y: float
    vx: float
    vy: float
    kind: str
    hp: int = 1
    radius: float = 8.0
    speed: float = 1.0
    score: int = 0
    color: QColor = field(default_factory=lambda: QColor(255, 255, 255))
    angle: float = 0.0
    rot_speed: float = 0.0

    @classmethod
    def spawn(cls, x: float, y: float, vx: float, vy: float, kind: str) -> Asteroid:
        info = ASTEROID_TYPES[kind]
        return cls(
            x=x, y=y, vx=vx, vy=vy, kind=kind,
            hp=info.hp, radius=info.radius, speed=info.speed,
            score=info.score, color=QColor(info.color),
            angle=random.uniform(0, 360),
            rot_speed=random.uniform(-3, 3),
        )

    @property
    def max_hp(self) -> int:
        return ASTEROID_TYPES[self.kind].hp


# ── Частицы (взрывы) ───────────────────────────────────────────────────

@dataclass
class Particle:
    """Частица взрыва — летит по инерции, затухает по life."""
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    color: QColor
    size: float = field(default_factory=lambda: random.uniform(1.5, 4.0))

    @classmethod
    def spawn(cls, x: float, y: float, color: QColor) -> Particle:
        angle = random.uniform(0, 360)
        speed = random.uniform(20, 120)
        life = random.uniform(0.3, 1.2)
        return cls(
            x=x, y=y,
            vx=math.cos(math.radians(angle)) * speed,
            vy=math.sin(math.radians(angle)) * speed,
            life=life, max_life=life, color=color,
        )


# ── Лазеры ─────────────────────────────────────────────────────────────

@dataclass
class Laser:
    """Луч лазера от спутника. Угол в градусах, как у спутника."""
    x: float
    y: float
    angle: float  # радианы
    speed: float = 8.0
    power: float = 1.0
    life: float = 1.5
    length: float = 25.0

    @classmethod
    def spawn(cls, x: float, y: float, angle_deg: float, power: float) -> Laser:
        return cls(
            x=x, y=y,
            angle=math.radians(angle_deg),
            power=power,
            length=20 + power * 5,
        )


# ── Power-ups ─────────────────────────────────────────────────────────

@dataclass
class PowerUpType:
    color: QColor
    char: str


POWERUP_TYPES: dict[str, PowerUpType] = {
    "shield": PowerUpType(color=QColor( 80, 150, 255), char="🛡"),
    "rapid":  PowerUpType(color=QColor( 80, 255, 100), char="⚡"),
    "bomb":   PowerUpType(color=QColor(255,  60,  60), char="💣"),
    "repair": PowerUpType(color=QColor(255, 200,  80), char="+"),
}


@dataclass
class PowerUp:
    """Бонус, выпадающий из астероидов. Надо подобрать спутником."""
    x: float
    y: float
    kind: str
    radius: float = 10.0
    life: float = 8.0
    pulse: float = 0.0

    @property
    def color(self) -> QColor:
        return POWERUP_TYPES[self.kind].color

    @property
    def char(self) -> str:
        return POWERUP_TYPES[self.kind].char

    @classmethod
    def random_kind(cls) -> str:
        return random.choice(list(POWERUP_TYPES.keys()))
