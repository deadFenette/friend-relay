"""
ChannelPill — адаптирован под Web Studio дизайн.

Заменяет Discord-style дерево каналов на pill-виджет:
  - Активный канал: полный фон accent_dim + bold текст
  - Unread: левая акцентная полоска 3px
  - Hover: сдвиг на +4px (gentle nudge) + glass_bg_hover фон

Стиль из веб-клиента: радиус 10px, 1px границы.
Использует старые имена для обратной совместимости.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QMenu, QWidget

from qt_app.theme import PALETTE, RADIUS


class ChannelPill(QWidget):
    """Один канал в боковой панели.

    Стилизован под pill: радиус 10px (из веб-стиля), 1px границы.
    """

    clicked = Signal(object)  # data: user_role payload (имя канала или None)
    delete_requested = Signal(object)  # data: имя канала

    def __init__(self, icon: str, label: str, data, parent=None, deletable: bool = False):
        super().__init__(parent)
        self._icon = icon
        self._label = label
        self._data = data
        self._deletable = deletable
        self._active = False
        self._unread = False
        self._hover_progress = 0.0  # 0..1, для нуджа +4px
        self._hovered = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
        self.setMinimumWidth(160)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Анимация смещения вправо при hover
        self._nudge_anim = QPropertyAnimation(self, b"nudge", self)
        self._nudge_anim.setDuration(180)
        self._nudge_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ---------- публичный API ----------

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def set_unread(self, unread: bool) -> None:
        self._unread = unread
        self.update()

    def data(self):
        return self._data

    # ---------- Qt-свойство для анимации ----------

    def _get_nudge(self) -> float:
        return self._hover_progress

    def _set_nudge(self, v: float) -> None:
        self._hover_progress = v
        self.update()

    nudge = Property(float, _get_nudge, _set_nudge)

    # ---------- события ----------

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._nudge_anim.stop()
        self._nudge_anim.setStartValue(self._hover_progress)
        self._nudge_anim.setEndValue(1.0 if not self._active else 0.0)
        self._nudge_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._nudge_anim.stop()
        self._nudge_anim.setStartValue(self._hover_progress)
        self._nudge_anim.setEndValue(0.0)
        self._nudge_anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._data)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if not self._deletable:
            return
        menu = QMenu(self)
        act_delete = menu.addAction("🗑  Удалить канал")
        action = menu.exec(event.globalPos())
        if action == act_delete:
            self.delete_requested.emit(self._data)

    # ---------- отрисовка ----------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Нудж вправо: максимум 4px
        offset_x = int(4 * self._hover_progress)

        rect = self.rect().adjusted(offset_x, 0, 0, 0)
        radius = RADIUS  # 10px из веб-стиля

        # Фон — используем старые имена для совместимости
        if self._active:
            # Активный: accent_dim
            bg = QColor(PALETTE.accent)
            bg.setAlphaF(0.18)  # accent_dim
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(rect, radius, radius)
            # Граница
            pen = QPen(QColor(PALETTE.accent), 1)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0, 0, -1, -1), radius, radius)
        elif self._hovered:
            # Hover: glass_bg_hover
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(0.12 * 255)))  # glass_bg_hover
            p.drawRoundedRect(rect, radius, radius)

        # Unread-полоска слева
        if self._unread and not self._active:
            bar_rect = QRect(rect.left() + 4, rect.top() + 8, 3, rect.height() - 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.fillRect(bar_rect, QColor(PALETTE.accent))

        # Иконка — используем старые имена
        text_color = (
            QColor(PALETTE.text_primary) if self._active else QColor(PALETTE.text_secondary)
        )
        if self._hovered and not self._active:
            text_color = QColor(PALETTE.text_primary)

        p.setPen(text_color)
        # иконка слева (8px от края)
        icon_x = rect.left() + 14
        icon_y = rect.center().y() + 5  # ~ baseline
        p.drawText(icon_x, icon_y, self._icon)

        # Текст
        font = p.font()
        if self._active:
            font.setBold(True)
        p.setFont(font)
        text_x = icon_x + 24
        p.drawText(text_x, icon_y, self._label)
