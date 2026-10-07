"""TypingIndicator — волна из точек «кто-то печатает».

Три точки не мигают по-простому, а плавно перетекают волной
(масштаб + прозрачность со сдвигом фазы).

Реализация: QPainter-виджет, фаза волны крутится QPropertyAnimation по
собственному Qt-свойству ``phase`` (без QTimer-хаков).
Три точки рисуются со сдвигом фазы: у каждой своя амплитуда масштаба и
прозрачности, за счёт чего точки «перетекают» друг в друга волной.

Слева от точек — имя(а) печатающего, справа точки. Высота компактная,
чтобы индикатор не прыгал при появлении/исчезновении.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from qt_app.theme import PALETTE


class TypingIndicator(QWidget):
    """Волна из трёх точек + текст «N печатает…».

    API:
        set_typers(["vasya"])        — показать «vasya печатает…»
        set_typers(["a", "b"])       — «a и b печатают…»
        set_typers(["a","b","c"])    — «a, b и ещё 2 печатают…»
        set_typers([])               — скрыть (виджет схлопывается)
    """

    PHASE_MS = 900  # полный цикл волны — быстрый, но не нервный

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._typers: list[str] = []

        self.setFixedHeight(24)
        self.setVisible(False)

        self._anim = QPropertyAnimation(self, b"phase", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(self.PHASE_MS)
        self._anim.setLoopCount(-1)  # бесконечный цикл
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)

    # -- Qt-свойства для QPropertyAnimation --

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = value
        self.update()

    phase = Property(float, _get_phase, _set_phase)

    # -- публичный API --

    def set_typers(self, names: list[str]) -> None:
        """Обновляет список печатающих. Пустой список — скрыть виджет."""
        self._typers = list(names)
        visible = bool(self._typers)
        self.setVisible(visible)
        if visible:
            if self._anim.state() != QPropertyAnimation.State.Running:
                self._anim.start()
            self.updateGeometry()
        else:
            self._anim.stop()

    # -- геометрия --

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(24)
        return hint

    # -- отрисовка --

    def _dots_origin_x(self) -> int:
        """Точки рисуются у правого края — текст идёт от левого."""
        return self.width() - 52

    def _label_text(self) -> str:
        names = self._typers
        if not names:
            return ""
        if len(names) == 1:
            return f"{names[0]} печатает…"
        if len(names) == 2:
            return f"{names[0]} и {names[1]} печатают…"
        return f"{names[0]} и ещё {len(names) - 1} печатают…"

    def paintEvent(self, _event) -> None:
        if not self._typers:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Текст «… печатает»
        p.setPen(QColor(PALETTE.text_secondary))
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(
            self.rect().adjusted(24, 0, -60, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._label_text(),
        )

        # Три точки волной: фаза каждой сдвинута на 2π/3,
        # масштаб 0.75..1.15, прозрачность 0.35..1.0 (scale + opacity offset)
        base = QColor(PALETTE.accent)
        cx = self._dots_origin_x()
        cy = self.height() // 2
        for i in range(3):
            t = (self._phase * 2 * math.pi) + (i * 2 * math.pi / 3)
            wave = (math.sin(t) + 1) / 2  # 0..1
            scale = 0.75 + 0.4 * wave
            alpha = 0.35 + 0.65 * wave
            radius = 3.0 * scale

            color = QColor(base)
            color.setAlphaF(alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            center_y = cy - (1.5 * wave)  # точка чуть приподнимается на пике
            p.drawEllipse(
                int(cx + i * 12 - radius), int(center_y - radius),
                int(radius * 2), int(radius * 2),
            )
        p.end()
