"""
OnlineUserChip — компактный pill-виджет для отображения онлайн-пользователя.

Показывает цветной кружок (первые буквы имени) + имя. Лёгкий glass-фон,
hover подсвечивает. Клик - заглушка (будущее: открыть профиль / ЛС).

Используется в топ-стрипе чата вместо текста "Онлайн: alice, bob".
"""
from __future__ import annotations

import hashlib

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from qt_app.theme import PALETTE, RADIUS_PILL

# Палитра цветов аватаров — на случай если у пользователя нет картинки.
# Определяется по хешу имени, чтобы один юзер всегда получал один цвет.
_AVATAR_COLORS = [
    QColor("#F87171"),  # red-400
    QColor("#FB923C"),  # orange-400
    QColor("#FBBF24"),  # amber-400
    QColor("#A3E635"),  # lime-400
    QColor("#34D399"),  # emerald-400
    QColor("#22D3EE"),  # cyan-400
    QColor("#60A5FA"),  # blue-400
    QColor("#818CF8"),  # indigo-400
    QColor("#A78BFA"),  # violet-400
    QColor("#E879F9"),  # fuchsia-400
    QColor("#F472B6"),  # pink-400
]


def color_for_name(name: str) -> QColor:
    """Детерминированный цвет аватара по имени (хеш)."""
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(_AVATAR_COLORS)
    return _AVATAR_COLORS[idx]


def initials(name: str) -> str:
    """Первые буквы имени (до 2 символов, в верхнем регистре)."""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()


class OnlineUserChip(QWidget):
    """Компактный pill-виджет онлайн-пользователя.

    Рисуется через QPainter: цветной кружок 18px с инициалами + имя.
    Не интерактивный (на клик пока не реагирует) — но с hover-эффектом.
    """

    def __init__(self, name: str, parent=None, is_me: bool = False):
        super().__init__(parent)
        self._name = name
        self._is_me = is_me
        self._hovered = False
        self._color = color_for_name(name)
        self._initials = initials(name)
        self._online_dot_color = QColor(PALETTE.success)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)
        self.setMinimumWidth(80)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def name(self) -> str:
        return self._name

    # ── Hover ────────────────────────────────────────────────────────

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    # ── Отрисовка ────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        from PySide6.QtCore import QPointF

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(0, 0, -1, -1)
        radius = RADIUS_PILL

        # Фон pill'а: glass + subtle border, на hover — чуть ярче
        if self._hovered:
            bg = QColor(255, 255, 255, 30)
            border = QColor(255, 255, 255, 60)
        else:
            bg = QColor(255, 255, 255, 15)
            border = QColor(255, 255, 255, 25)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QColor(border))
        p.drawRoundedRect(rect, radius, radius)

        # Цветной кружок с инициалами
        circle_radius = 9.0
        circle_x = 14.0
        circle_y = rect.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(circle_x, circle_y), circle_radius, circle_radius)

        # Зелёная точка online (внизу справа от кружка)
        dot_x = circle_x + circle_radius - 2
        dot_y = circle_y + circle_radius - 2
        # белая обводка чтобы точка «оторвалась» от кружка
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(dot_x, dot_y), 4, 4)
        p.setBrush(self._online_dot_color)
        p.drawEllipse(QPointF(dot_x, dot_y), 3, 3)

        # Инициалы поверх кружка
        p.setPen(QColor(255, 255, 255))
        font = p.font()
        font.setPixelSize(9)
        font.setBold(True)
        p.setFont(font)
        p.drawText(
            int(circle_x - circle_radius),
            int(circle_y - circle_radius),
            int(circle_radius * 2),
            int(circle_radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            self._initials,
        )

        # Имя пользователя
        text_x = int(circle_x + circle_radius + 8)
        text_color = QColor(PALETTE.text_primary if self._is_me else PALETTE.text_secondary)
        if self._hovered:
            text_color = QColor(PALETTE.text_primary)
        p.setPen(text_color)
        font2 = p.font()
        font2.setPixelSize(12)
        font2.setBold(self._is_me)
        p.setFont(font2)
        display_name = self._name if len(self._name) <= 16 else self._name[:15] + "…"
        p.drawText(
            text_x, 0,
            self.width() - text_x - 6, self.height(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            display_name,
        )

        p.end()
