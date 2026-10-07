"""Skeleton-экраны — скелетоны вместо крутилок (см. docs/DESIGN_STYLE.md).

Пока данные грузятся, на экране — скруглённые блоки, по которым плавно
пробегает блик, а не вращающееся колесо.

SkeletonBlock — скруглённый блок surface_2, по которому плавно пробегает
блик (градиентная полоса white@low-alpha). Движение — QPropertyAnimation по
собственному свойству ``phase`` (0..1, loop), без QTimer-хаков.

SkeletonFeed — колонка «скелетонов сообщений»: разные ширины, попеременно
слева/справа, один «файловый» блок. Подставляется в ленту чата на время
«Загрузка…», чтобы экран не пустовал и не показывал текстовый спиннер.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qt_app.theme import PALETTE, RADIUS


class SkeletonBlock(QWidget):
    """Один shimmer-блок. Фиксированная высота, ширина по layout."""

    def __init__(self, height: int = 14, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self.setFixedHeight(height)

        self._anim = QPropertyAnimation(self, b"phase", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1400)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = value
        self.update()

    phase = Property(float, _get_phase, _set_phase)

    def start(self) -> None:
        if self._anim.state() != QPropertyAnimation.State.Running:
            self._anim.start()

    def stop(self) -> None:
        self._anim.stop()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS, RADIUS)

        # База
        p.fillPath(path, QColor(PALETTE.surface_2))

        # Пробегающий блик: окно ~35% ширины, центр едет от -0.4 до 1.4
        center = -0.4 + 1.8 * self._phase
        grad = QLinearGradient(center * w - 0.18 * w, 0, center * w + 0.18 * w, 0)
        c = QColor(255, 255, 255, 0)
        c.setAlphaF(0.0)
        grad.setColorAt(0.0, c)
        mid = QColor(255, 255, 255)
        mid.setAlphaF(0.07)
        grad.setColorAt(0.5, mid)
        grad.setColorAt(1.0, c)
        p.fillPath(path, grad)
        p.end()


class SkeletonFeed(QWidget):
    """«Скелет» ленты сообщений: 5 блоков, попеременно слева/справа.

    Используется вместо текстового плейсхолдера «Загрузка…». Живёт на
    странице канала до прихода данных — очищается
    обычным _clear_feed (это обычный виджет в layout ленты).
    """

    ROWS: tuple[int, ...] = (200, 150, 230, 120, 180)  # ширины блоков

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        for i, width in enumerate(self.ROWS):
            row = QHBoxLayout()
            block = SkeletonBlock(height=38 if i % 2 == 0 else 32)
            block.setFixedWidth(width)
            if i % 2 == 0:
                row.addWidget(block)
                row.addStretch(1)
            else:
                row.addStretch(1)
                row.addWidget(block)
            layout.addLayout(row)

        layout.addStretch(1)
        self.start()

    def start(self) -> None:
        for block in self.findChildren(SkeletonBlock):
            block.start()

    def stop(self) -> None:
        for block in self.findChildren(SkeletonBlock):
            block.stop()
