"""Игровой движок Orbital Defense.

Чистая логика: обновление позиций, столкновения, спавн, волны, комбо,
power-ups. Ноль Qt-зависимости: клавиши — строковые константы KEY_*
из entities (canvas конвертирует Qt.Key в них), цвета — QColor из
entities (с заглушкой, если PySide6 не установлен).
"""
from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from lib.games.orbital.entities import (
    KEY_DOWN,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_SHIFT,
    KEY_UP,
    Asteroid,
    Laser,
    Particle,
    PowerUp,
    QColor,
)

# ── Константы игрового мира ───────────────────────────────────────────

DEFAULT_W = 800
DEFAULT_H = 600
PLANET_RADIUS = 35
PLANET_MAX_HP = 100

ORBIT_MIN = 70
ORBIT_MAX = 220
ORBIT_DEFAULT_RADIUS = 120
ORBIT_ACCEL = 240.0  # градусов/с²
ORBIT_FRICTION = 0.94

SPAWN_BASE_INTERVAL = 1.5  # секунд
SPAWN_MIN_INTERVAL = 0.3
WAVE_DURATION = 20.0  # секунд на волну
SHOOT_COOLDOWN = 0.25
RAPID_COOLDOWN = 0.12

GRAVITY = 60.0
SHAKE_DECAY = 0.9

COMBO_DURATION = 2.5

POWERUP_DROP_CHANCE = 0.12


class OrbitalEngine:
    """Игровое состояние + методы обновления.

    Canvas создаёт engine, дёргает tick(dt) каждый кадр, читает поля для
    отрисовки, и вызывает handle_key_down / handle_key_up / handle_shoot
    на ввод пользователя.

    Клавиши — строковые константы KEY_* из entities (не Qt.Key_*), чтобы
    движок не зависел от Qt.

    Signals (вместо Qt signal'ов — простые callbacks, чтобы не тащить Qt
    в чистую логику):
        on_game_over(score, wave)  — вызывается когда planet_hp <= 0
        on_wave_changed(wave)      — когда начинается новая волна
    """

    def __init__(self, w: int = DEFAULT_W, h: int = DEFAULT_H):
        self.w = w
        self.h = h
        self.on_game_over: Callable[[int, int], None] | None = None
        self.on_wave_changed: Callable[[int], None] | None = None
        self._reset_game()

    # ── Жизненный цикл ───────────────────────────────────────────────

    def reset(self) -> None:
        """Полный сброс к началу новой игры."""
        self._reset_game()

    def _reset_game(self) -> None:
        self.cx = self.w / 2
        self.cy = self.h / 2
        self.planet_radius = PLANET_RADIUS
        self.planet_hp = PLANET_MAX_HP
        self.planet_max_hp = PLANET_MAX_HP

        self.orbit_angle = -90.0
        self.orbit_radius = ORBIT_DEFAULT_RADIUS
        self.orbit_speed = 0.0

        self.satellite_size = 8

        self.lasers: list[Laser] = []
        self.asteroids: list[Asteroid] = []
        self.particles: list[Particle] = []
        self.powerups: list[PowerUp] = []
        self.stars: list[tuple[float, float, float]] = [
            (random.uniform(0, self.w), random.uniform(0, self.h), random.uniform(0.5, 2.5))
            for _ in range(120)
        ]

        self.score = 0
        self.combo = 0
        self.combo_timer = 0.0
        self.wave = 1
        self.wave_timer = 0.0
        self.spawn_timer = 0.0
        self.game_over = False
        self.paused = False
        self.time_slow = 0.0

        self.shield = 0.0
        self.rapid = 0.0
        self.bomb_ready = False

        self.keys: set[str] = set()
        self._last_shot = 0.0

        self._shake = 0.0
        self._flash = 0.0

    def resize(self, w: int, h: int) -> None:
        self.w = w
        self.h = h
        self.cx = w / 2
        self.cy = h / 2

    # ── Публичные геттеры для canvas ────────────────────────────────

    @property
    def satellite_pos(self) -> tuple[float, float]:
        """Текущая позиция спутника в пикселях."""
        rad = math.radians(self.orbit_angle)
        return (self.cx + math.cos(rad) * self.orbit_radius,
                self.cy + math.sin(rad) * self.orbit_radius)

    @property
    def planet_hp_pct(self) -> float:
        return self.planet_hp / self.planet_max_hp if self.planet_max_hp else 0.0

    @property
    def shake_offset(self) -> tuple[float, float]:
        if self._shake < 0.1:
            return (0.0, 0.0)
        return (random.uniform(-self._shake, self._shake),
                random.uniform(-self._shake, self._shake))

    # ── Ввод ────────────────────────────────────────────────────────

    def key_down(self, key: str) -> None:
        """Обрабатывает нажатие клавиши (строковый KEY_* из entities).
        Возвращает True если клавиша съедена игрой, False если можно
        передать выше (например Esc → закрыть окно).
        """
        self.keys.add(key)
        # пауза/рестарт/выстрел обрабатывает canvas (ему нужны Qt.Key_P и т.д.)

    def key_up(self, key: str) -> None:
        self.keys.discard(key)

    def try_shoot(self) -> None:
        if self.game_over or self.paused:
            return
        now = time.time()
        cooldown = RAPID_COOLDOWN if self.rapid > 0 else SHOOT_COOLDOWN
        if now - self._last_shot < cooldown:
            return
        self._last_shot = now

        rad = math.radians(self.orbit_angle)
        sx = self.cx + math.cos(rad) * self.orbit_radius
        sy = self.cy + math.sin(rad) * self.orbit_radius
        power = 1.0 + (self.orbit_radius - ORBIT_MIN) / (ORBIT_MAX - ORBIT_MIN) * 2.0
        self.lasers.append(Laser.spawn(sx, sy, self.orbit_angle, power))
        self._shake = max(self._shake, 2.0)

    # ── Главный цикл ────────────────────────────────────────────────

    def tick(self, dt: float) -> None:
        if self.game_over:
            return
        if self.paused:
            return

        slow_factor = 0.3 if self.time_slow > 0 else 1.0
        if self.time_slow > 0:
            self.time_slow -= dt
        dt_eff = dt * slow_factor

        self._update_orbit(dt_eff)
        self._update_spawning(dt_eff)
        self._update_waves(dt_eff)
        self._update_asteroids(dt_eff)
        self._update_lasers(dt_eff)
        self._update_laser_asteroid_collisions()
        self._update_powerups(dt_eff)
        self._update_combo(dt_eff)
        self._update_powerup_timers(dt_eff)

        self._shake *= SHAKE_DECAY
        self._flash = max(0.0, self._flash - dt_eff)

        if self.planet_hp <= 0 and not self.game_over:
            self.game_over = True
            if self.on_game_over:
                self.on_game_over(self.score, self.wave)

    # ── Внутренние апдейты ─────────────────────────────────────────

    def _update_orbit(self, dt: float) -> None:
        if KEY_LEFT in self.keys:
            self.orbit_speed -= ORBIT_ACCEL * dt
        if KEY_RIGHT in self.keys:
            self.orbit_speed += ORBIT_ACCEL * dt
        if KEY_UP in self.keys:
            self.orbit_radius = max(ORBIT_MIN, self.orbit_radius - 80 * dt)
        if KEY_DOWN in self.keys:
            self.orbit_radius = min(ORBIT_MAX, self.orbit_radius + 80 * dt)
        if KEY_SHIFT in self.keys and self.time_slow <= 0:
            self.time_slow = 2.0

        self.orbit_speed *= ORBIT_FRICTION
        self.orbit_angle += self.orbit_speed * dt

    def _update_spawning(self, dt: float) -> None:
        self.spawn_timer -= dt
        if self.spawn_timer <= 0:
            self._spawn_asteroid()
            self.spawn_timer = max(SPAWN_MIN_INTERVAL,
                                   SPAWN_BASE_INTERVAL - self.wave * 0.08)

    def _update_waves(self, dt: float) -> None:
        self.wave_timer += dt
        if self.wave_timer > WAVE_DURATION:
            self.wave += 1
            self.wave_timer = 0
            self.planet_max_hp = min(200, self.planet_max_hp + 10)
            self.planet_hp = min(self.planet_hp + 20, self.planet_max_hp)
            self._flash = 0.3
            if self.on_wave_changed:
                self.on_wave_changed(self.wave)

    def _update_asteroids(self, dt: float) -> None:
        for a in self.asteroids:
            dx = self.cx - a.x
            dy = self.cy - a.y
            dist = math.hypot(dx, dy) + 0.1
            a.vx += (dx / dist) * GRAVITY * dt * 0.05 * a.speed
            a.vy += (dy / dist) * GRAVITY * dt * 0.05 * a.speed
            a.x += a.vx * dt * 60
            a.y += a.vy * dt * 60
            a.angle += a.rot_speed

            if dist < self.planet_radius + a.radius:
                if self.shield > 0:
                    self.shield -= 1
                    self._explode(a.x, a.y, a.color, 15)
                    a.hp = 0
                else:
                    self.planet_hp -= a.hp * 8
                    self._explode(a.x, a.y, QColor(255, 80, 80), 25)
                    self._shake = 6.0
                    a.hp = 0
                    self.combo = 0
        self.asteroids = [a for a in self.asteroids if a.hp > 0]

    def _update_lasers(self, dt: float) -> None:
        for l in self.lasers:
            l.x += math.cos(l.angle) * l.speed * 60 * dt
            l.y += math.sin(l.angle) * l.speed * 60 * dt
            l.life -= dt
        self.lasers = [l for l in self.lasers if l.life > 0]

    def _update_laser_asteroid_collisions(self) -> None:
        for l in self.lasers[:]:
            for a in self.asteroids[:]:
                if math.hypot(l.x - a.x, l.y - a.y) < a.radius + 3:
                    a.hp -= l.power
                    l.life = 0
                    self._explode(l.x, l.y, QColor(200, 255, 255), 5)
                    if a.hp <= 0:
                        self._destroy_asteroid(a)
                    break

    def _update_powerups(self, dt: float) -> None:
        sat_x, sat_y = self.satellite_pos
        for p in self.powerups[:]:
            p.pulse += dt * 4
            p.life -= dt
            dist = math.hypot(p.x - sat_x, p.y - sat_y)
            if dist < 25:
                self._apply_powerup(p.kind)
                self.powerups.remove(p)
            elif p.life <= 0:
                self.powerups.remove(p)

    def _update_combo(self, dt: float) -> None:
        if self.combo_timer > 0:
            self.combo_timer -= dt
            if self.combo_timer <= 0:
                self.combo = 0

    def _update_powerup_timers(self, dt: float) -> None:
        if self.shield > 0:
            self.shield -= dt
        if self.rapid > 0:
            self.rapid -= dt

    # ── Спавн и эффекты ────────────────────────────────────────────

    def _spawn_asteroid(self) -> None:
        side = random.choice(["top", "bottom", "left", "right"])
        if side == "top":
            x, y = random.uniform(0, self.w), -30
        elif side == "bottom":
            x, y = random.uniform(0, self.w), self.h + 30
        elif side == "left":
            x, y = -30, random.uniform(0, self.h)
        else:
            x, y = self.w + 30, random.uniform(0, self.h)

        dx = self.cx - x
        dy = self.cy - y
        dist = math.hypot(dx, dy)
        base_speed = 1.0 + self.wave * 0.15
        vx = (dx / dist) * base_speed * random.uniform(0.7, 1.3)
        vy = (dy / dist) * base_speed * random.uniform(0.7, 1.3)

        roll = random.random()
        if self.wave < 3:
            kind = "small" if roll < 0.7 else "medium"
        elif self.wave < 6:
            kind = "small" if roll < 0.4 else "medium" if roll < 0.8 else "fast"
        else:
            kind = ("medium" if roll < 0.3 else "fast"
                    if roll < 0.5 else "large" if roll < 0.8 else "splitter")

        self.asteroids.append(Asteroid.spawn(x, y, vx, vy, kind))

    def _destroy_asteroid(self, a: Asteroid) -> None:
        self._explode(a.x, a.y, a.color, int(a.radius))
        mult = 1 + self.combo * 0.1
        self.score += int(a.score * mult)
        self.combo += 1
        self.combo_timer = COMBO_DURATION

        if a.kind == "splitter":
            for _ in range(2):
                angle = random.uniform(0, 360)
                sp = 1.5
                self.asteroids.append(Asteroid.spawn(
                    a.x, a.y,
                    math.cos(math.radians(angle)) * sp,
                    math.sin(math.radians(angle)) * sp,
                    "small",
                ))

        if random.random() < POWERUP_DROP_CHANCE:
            self.powerups.append(PowerUp(a.x, a.y, PowerUp.random_kind()))

    def _explode(self, x: float, y: float, color: QColor, count: int) -> None:
        for _ in range(count):
            self.particles.append(Particle.spawn(x, y, color))

    def _apply_powerup(self, kind: str) -> None:
        if kind == "shield":
            self.shield = 10.0
        elif kind == "rapid":
            self.rapid = 8.0
        elif kind == "bomb":
            for a in self.asteroids:
                a.hp = 0
                self._explode(a.x, a.y, a.color, 10)
            self.asteroids.clear()
            self._shake = 10.0
        elif kind == "repair":
            self.planet_hp = min(self.planet_hp + 30, self.planet_max_hp)

    def update_particles(self, dt: float) -> None:
        """Двигает частицы и чистит мёртвые. Вызывается из canvas после tick."""
        for pt in self.particles:
            pt.x += pt.vx * dt
            pt.y += pt.vy * dt
            pt.life -= dt
        self.particles = [pt for pt in self.particles if pt.life > 0]
