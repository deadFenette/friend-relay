"""Toast — ненавязчивый фидбек вместо модалок (ТЗ docs/DESIGN_STYLE.md, раздел 2
«Respectful: no modal spam» и раздел 8 «Every action has a reaction»).

Копирование, скачивание файла и подобные мелкие успешные действия
показывают pill-тост внизу по центру родителя: появление slide-up + fade
120ms, держится ~1.5s, плавно уезжает. Одновременно существует не более
одного тоста на родителя — повторный вызов просто заменяет текст.

Использование:
    Toast.show_toast(parent, "Скопировано в буфер")
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

from qt_app.theme import DUR_FAST, EASE_OUT_CUBIC, PALETTE

_HOLD_MS = 1500  # ТЗ: «morphs into a checkmark for 1.5s»

# Высота тоста фиксируется, радиус = height/2: QSS-радиус больше height/2
# Qt рисует КВАДРАТНЫМИ углами (не клампит, в отличие от QPainter).
_TOAST_H = 36


class Toast(QLabel):
    """Pill-тост. Показывается как плавающий ребёнок родителя."""

    _active: dict[int, Toast] = {}  # id(parent) -> Toast

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(_TOAST_H)
        self.setStyleSheet(
            f"""
            QLabel {{
                background: {PALETTE.surface_3};
                color: {PALETTE.text_primary};
                border: 1px solid {PALETTE.border};
                border-radius: {_TOAST_H // 2}px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 600;
            }}
        """
        )
        self.setVisible(False)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    # ------------------------------------------------------------------
    @staticmethod
    def show_toast(parent: QWidget, text: str, icon: str = "✓") -> None:
        """Показать (или переиспользовать) тост на родителе."""
        existing = Toast._active.get(id(parent))
        if existing is not None:
            try:
                existing._display(f"{icon}  {text}")
                return
            except RuntimeError:
                # старый тост уже удалён C++-сторой — создаём заново
                Toast._active.pop(id(parent), None)
        toast = Toast(parent)
        Toast._active[id(parent)] = toast
        toast._display(f"{icon}  {text}")

    # ------------------------------------------------------------------
    def _display(self, text: str) -> None:
        self.setText(text)
        self.adjustSize()

        parent = self.parentWidget()
        if parent is not None:
            x = (parent.width() - self.width()) // 2
            y = parent.height() - self.height() - 28
            self.move(max(8, x), max(8, y))

        self.setVisible(True)
        self.raise_()

        # v1.9.5: старая fade-out группа могла ещё играть — стопаем
        # (два QPropertyAnimation одной opacity = дёргание)
        old_grp = getattr(self, "_fade_group", None)
        if old_grp is not None:
            old_grp.stop()
        self._fade_group = QParallelAnimationGroup(self)

        fade = QPropertyAnimation(self._effect, b"opacity", self)
        fade.setDuration(DUR_FAST)
        fade.setEasingCurve(EASE_OUT_CUBIC)
        fade.setStartValue(self._effect.opacity())
        fade.setEndValue(1.0)
        self._fade_group.addAnimation(fade)

        rise = QPropertyAnimation(self, b"pos", self)
        rise.setDuration(DUR_FAST)
        rise.setEasingCurve(EASE_OUT_CUBIC)
        rise.setStartValue(self.pos() + QPoint(0, 8))
        rise.setEndValue(self.pos())
        self._fade_group.addAnimation(rise)

        # DeleteWhenStopped: сотни показов тостов больше не копят мёртвые
        # QObject-группы в детях (v1.9.5)
        self._fade_group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._hide_timer.start(_HOLD_MS)

    def _fade_out(self) -> None:
        fade = QPropertyAnimation(self._effect, b"opacity", self)
        fade.setDuration(DUR_FAST + 60)
        fade.setEasingCurve(EASE_OUT_CUBIC)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.finished.connect(lambda: self.setVisible(False))
        # v1.9.5: конфликт fade-in/fade-out на одном свойстве — сначала стоп
        prev = getattr(self, "_fade_out_anim", None)
        if prev is not None:
            prev.stop()
        self._fade_out_anim = fade  # держим ссылку
        fade.start()
