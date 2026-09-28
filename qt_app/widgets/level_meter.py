"""LevelMeter — горизонтальный метр уровня 0..1 (v1.9.6).

Используется в диалоге голосового канала и в карточке «Звук и голос»
профиля: живой отклик «микрофон слышит тебя» без подключения к серверу.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from qt_app.theme import PALETTE


class LevelMeter(QWidget):
    """Сегментный метр: зелёный, верхняя треть — жёлтая (видно перегруз).

    Красится в paintEvent — дёшево; set_level игнорирует микродельты,
    чтобы таймер UI не будрил repaint понапрасну."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self.setFixedHeight(8)
        self.setMinimumWidth(60)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, float(level)))
        if abs(level - self._level) < 0.01:
            return
        self._level = level
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 1, -1, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(rect, 4, 4)
        if self._level > 0.02:
            w = int(rect.width() * self._level)
            bar = rect.adjusted(0, 0, -(rect.width() - w), 0)
            color = (PALETTE.warning if self._level > 0.75
                     else PALETTE.success)
            p.setBrush(QColor(color))
            p.drawRoundedRect(bar, 4, 4)
        p.end()
