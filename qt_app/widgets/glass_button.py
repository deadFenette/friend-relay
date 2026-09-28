"""
GlassButton — адаптирован под Web Studio дизайн.

Стиль из веб-клиента: 1px границы, без теней по умолчанию,
hover -> glass_bg_hover, press -> glass_bg_active. Радиус 10px.

Всё через QPainter + QPropertyAnimation на своих Qt-свойствах (scale,
bg_alpha) — так анимация идёт через Qt event loop, а не хаками на QTimer.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QPushButton

from qt_app.theme import PALETTE, RADIUS


class GlassButton(QPushButton):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(36)

        self._scale = 1.0
        self._bg_alpha = 0.06  # glass_bg alpha

        self._scale_anim = QPropertyAnimation(self, b"scale", self)
        self._scale_anim.setDuration(120)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._alpha_anim = QPropertyAnimation(self, b"bgAlpha", self)
        self._alpha_anim.setDuration(150)
        self._alpha_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -- Qt properties (нужны как QProperty, чтобы QPropertyAnimation мог их крутить) --
    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.update()

    scale = Property(float, _get_scale, _set_scale)

    def _get_bg_alpha(self) -> float:
        return self._bg_alpha

    def _set_bg_alpha(self, value: float) -> None:
        self._bg_alpha = value
        self.update()

    bgAlpha = Property(float, _get_bg_alpha, _set_bg_alpha)

    # -- события --
    def enterEvent(self, event) -> None:
        self._animate_to(alpha=0.12)  # glass_bg_hover
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._animate_to(alpha=0.06)  # glass_bg
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._scale_anim.stop()
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(0.97)
        self._scale_anim.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._scale_anim.stop()
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(1.0)
        self._scale_anim.start()
        super().mouseReleaseEvent(event)

    def _animate_to(self, alpha: float) -> None:
        self._alpha_anim.stop()
        self._alpha_anim.setStartValue(self._bg_alpha)
        self._alpha_anim.setEndValue(alpha)
        self._alpha_anim.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect())
        cx, cy = rect.center().x(), rect.center().y()
        painter.translate(cx, cy)
        painter.scale(self._scale, self._scale)
        painter.translate(-cx, -cy)

        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        # Фон — glass_bg/glass_bg_hover (используем старые имена для совместимости)
        bg_color = QColor(255, 255, 255, int(self._bg_alpha * 255))
        painter.fillPath(path, bg_color)

        # Граница — 1px border
        border_color = QColor(PALETTE.border)
        painter.setPen(border_color)
        painter.drawPath(path)

        # Текст (используем старое имя для совместимости)
        painter.setPen(QColor(PALETTE.text_primary))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())
        painter.end()
