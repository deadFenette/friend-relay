"""OrbitalGameWidget — QWidget с QPainter, отрисовывает engine и принимает ввод.

View-слой (qt_app/widgets/games): тонкая обёртка над OrbitalEngine из
lib/games/orbital/engine.py. Цикл QTimer 16мс, вызывает engine.tick(dt),
перерисовывает. Стрелки/Space/Shift конвертируются из Qt.Key в строковые
KEY_* движка, пауза/рестарт обрабатывает сам виджет (он же знает про
Qt.Key_P/R).
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from lib.games.orbital.engine import OrbitalEngine
from lib.games.orbital.entities import (
    ASTEROID_TYPES,
    KEY_DOWN,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_SHIFT,
    KEY_UP,
)

# Конвертация Qt.Key → строковые идентификаторы движка (engine без Qt).
_QT_KEY_TO_ENGINE = {
    Qt.Key.Key_Left: KEY_LEFT,
    Qt.Key.Key_Right: KEY_RIGHT,
    Qt.Key.Key_Up: KEY_UP,
    Qt.Key.Key_Down: KEY_DOWN,
    Qt.Key.Key_Shift: KEY_SHIFT,
}


class OrbitalGameWidget(QWidget):
    """Игровое поле: canvas + ввод. Логика в OrbitalEngine."""

    # Эмитится когда игра закончилась. Canvas сам не лезет в сеть —
    # родительский диалог ловит сигнал и сохраняет счёт через /bot_command.
    score_submitted = Signal(int, int)  # score, wave

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.engine = OrbitalEngine()
        self.engine.on_game_over = self._on_engine_game_over

        self._dt = 0.016
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        # Подписан на on_game_over — нужен флаг чтобы не слать сигнал дважды
        self._score_sent = False

    # ── Жизненный цикл ───────────────────────────────────────────────

    def start_game(self) -> None:
        self.engine.reset()
        self._score_sent = False
        self.timer.start(16)

    def stop_game(self) -> None:
        self.timer.stop()

    def pause_toggle(self) -> None:
        if self.engine.game_over:
            return
        self.engine.paused = not self.engine.paused

    def restart(self) -> None:
        self.start_game()

    def is_game_over(self) -> bool:
        return self.engine.game_over

    def score(self) -> int:
        return self.engine.score

    def wave(self) -> int:
        return self.engine.wave

    # ── Ввод ─────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        self.engine.resize(self.width(), self.height())

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k == Qt.Key.Key_P:
            self.pause_toggle()
            return
        if k == Qt.Key.Key_R:
            self.restart()
            return
        if k == Qt.Key.Key_Escape:
            return  # пусть родитель закрывает окно
        if k in (Qt.Key.Key_Space, Qt.Key.Key_Shift):
            self.engine.try_shoot()
        ek = _QT_KEY_TO_ENGINE.get(k)
        if ek is not None:
            self.engine.key_down(ek)

    def keyReleaseEvent(self, event) -> None:
        ek = _QT_KEY_TO_ENGINE.get(event.key())
        if ek is not None:
            self.engine.key_up(ek)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.engine.try_shoot()

    # ── Главный цикл ────────────────────────────────────────────────

    def _tick(self) -> None:
        self.engine.tick(self._dt)
        self.engine.update_particles(self._dt)
        self.update()

    def _on_engine_game_over(self, score: int, wave: int) -> None:
        if self._score_sent:
            return
        self._score_sent = True
        self.score_submitted.emit(score, wave)

    # ── Отрисовка ────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Shake
        ox, oy = self.engine.shake_offset
        p.translate(ox, oy)

        self._draw_background(p)
        self._draw_stars(p)
        self._draw_planet(p)
        self._draw_orbit(p)
        self._draw_satellite(p)
        self._draw_lasers(p)
        self._draw_asteroids(p)
        self._draw_powerups(p)
        self._draw_particles(p)

        # UI без shake
        p.resetTransform()
        self._draw_ui(p)

        if self.engine._flash > 0:
            p.fillRect(self.rect(), QColor(255, 255, 255, int(30 * self.engine._flash * 3)))

        if self.engine.game_over:
            self._draw_game_over(p)

        p.end()

    def _draw_background(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor(8, 8, 18))

    def _draw_stars(self, p: QPainter) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        now = time.time()
        for sx, sy, sz in self.engine.stars:
            alpha = int(80 + math.sin(now * 2 + sx) * 60)
            p.setBrush(QColor(200, 220, 255, alpha))
            p.drawEllipse(QPointF(sx, sy), sz * 0.5, sz * 0.5)

    def _draw_planet(self, p: QPainter) -> None:
        cx, cy = self.engine.cx, self.engine.cy
        r = self.engine.planet_radius

        # Glow
        grad = QRadialGradient(cx, cy, r * 2)
        grad.setColorAt(0, QColor(60, 120, 220))
        grad.setColorAt(0.5, QColor(30, 60, 150))
        grad.setColorAt(1, QColor(10, 20, 60, 0))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), r * 1.5, r * 1.5)

        # Тело планеты
        p.setPen(QPen(QColor(100, 180, 255, 120), 2))
        p.setBrush(QColor(40, 90, 200))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # HP-индикатор (заполнение по радиусу)
        hp_pct = self.engine.planet_hp_pct
        hp_color = (QColor(80, 255, 120) if hp_pct > 0.5
                    else QColor(255, 200, 60) if hp_pct > 0.25
                    else QColor(255, 60, 60))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hp_color)
        p.drawEllipse(QPointF(cx, cy), r * hp_pct, r * hp_pct)

    def _draw_orbit(self, p: QPainter) -> None:
        p.setPen(QPen(QColor(100, 200, 255, 40), 1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(self.engine.cx, self.engine.cy),
                       self.engine.orbit_radius, self.engine.orbit_radius)

    def _draw_satellite(self, p: QPainter) -> None:
        sx, sy = self.engine.satellite_pos
        rad = math.radians(self.engine.orbit_angle)

        # Щит
        if self.engine.shield > 0:
            shield_alpha = int(80 + math.sin(time.time() * 8) * 40)
            p.setPen(QPen(QColor(80, 150, 255, shield_alpha), 2))
            p.setBrush(QColor(80, 150, 255, 30))
            p.drawEllipse(QPointF(sx, sy), 18, 18)

        # Тело спутника
        p.setPen(QPen(QColor(200, 255, 255), 2))
        p.setBrush(QColor(100, 220, 255))
        p.drawEllipse(QPointF(sx, sy), self.engine.satellite_size, self.engine.satellite_size)

        # Пушка
        gun_len = 14
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawLine(QPointF(sx, sy),
                   QPointF(sx + math.cos(rad) * gun_len, sy + math.sin(rad) * gun_len))

    def _draw_lasers(self, p: QPainter) -> None:
        for l in self.engine.lasers:
            p.setPen(QPen(QColor(100, 255, 200, int(200 * l.life)), 2 + l.power))
            ex = l.x + math.cos(l.angle) * l.length
            ey = l.y + math.sin(l.angle) * l.length
            p.drawLine(QPointF(l.x, l.y), QPointF(ex, ey))

    def _draw_asteroids(self, p: QPainter) -> None:
        for a in self.engine.asteroids:
            p.setPen(QPen(a.color.lighter(150), 2))
            p.setBrush(QColor(a.color.red(), a.color.green(), a.color.blue(), 120))
            p.drawEllipse(QPointF(a.x, a.y), a.radius, a.radius)
            # HP-индикатор (дуга)
            if a.hp > 1:
                p.setPen(QPen(QColor(255, 255, 255), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                arc_size = (a.radius + 2) * 2
                p.drawArc(
                    QRectF(a.x - a.radius - 2, a.y - a.radius - 2, arc_size, arc_size),
                    0, int(5760 * a.hp / ASTEROID_TYPES[a.kind].hp),
                )

    def _draw_powerups(self, p: QPainter) -> None:
        for pu in self.engine.powerups:
            pulse = 1.0 + math.sin(pu.pulse) * 0.2
            color = pu.color
            p.setPen(QPen(color, 2))
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 80))
            p.drawEllipse(QPointF(pu.x, pu.y), pu.radius * pulse, pu.radius * pulse)
            p.setPen(QPen(QColor(255, 255, 255), 1))
            p.setFont(QFont("Segoe UI Emoji", 10))
            p.drawText(QRectF(pu.x - 10, pu.y - 10, 20, 20),
                       Qt.AlignmentFlag.AlignCenter, pu.char)

    def _draw_particles(self, p: QPainter) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        for pt in self.engine.particles:
            alpha = int(255 * (pt.life / pt.max_life))
            p.setBrush(QColor(pt.color.red(), pt.color.green(), pt.color.blue(), alpha))
            sz = pt.size * (pt.life / pt.max_life)
            p.drawEllipse(QPointF(pt.x, pt.y), sz, sz)

    def _draw_ui(self, p: QPainter) -> None:
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.setFont(QFont("Consolas", 14, QFont.Weight.Bold))

        p.drawText(15, 30, f"SCORE: {self.engine.score}")
        p.drawText(15, 55, f"WAVE: {self.engine.wave}")
        if self.engine.combo > 1:
            p.setPen(QPen(QColor(255, 200, 80), 1))
            p.drawText(15, 80, f"COMBO x{self.engine.combo:.1f}")

        # HP-бар планеты снизу
        hp_pct = self.engine.planet_hp_pct
        hp_color = (QColor(80, 255, 120) if hp_pct > 0.5
                    else QColor(255, 200, 60) if hp_pct > 0.25
                    else QColor(255, 60, 60))
        bar_w, bar_h = 200, 10
        bx = self.engine.cx - bar_w / 2
        by = self.engine.h - 30
        p.setPen(QPen(QColor(255, 255, 255, 100), 1))
        p.setBrush(QColor(40, 40, 60))
        p.drawRect(int(bx), int(by), bar_w, bar_h)
        p.setBrush(hp_color)
        p.drawRect(int(bx), int(by), int(bar_w * hp_pct), bar_h)
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.setFont(QFont("Consolas", 9))
        p.drawText(int(bx), int(by) - 5,
                   f"PLANET HP {int(self.engine.planet_hp)}/{self.engine.planet_max_hp}")

        if self.engine.time_slow > 0:
            p.setPen(QPen(QColor(100, 200, 255), 2))
            p.setFont(QFont("Consolas", 12))
            p.drawText(self.engine.w - 150, 30,
                       f"TIME DILATION: {self.engine.time_slow:.1f}s")

    def _draw_game_over(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor(0, 0, 0, 180))
        p.setPen(QPen(QColor(255, 80, 80), 2))
        p.setFont(QFont("Consolas", 36, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "GAME OVER")
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.setFont(QFont("Consolas", 16))
        p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignmentFlag.AlignCenter,
                   f"Score: {self.engine.score}  |  Wave: {self.engine.wave}\nPress R to restart")
