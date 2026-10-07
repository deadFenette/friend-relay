"""
SlidingStackedWidget — контейнер экранов с анимированными переходами.

Один QMainWindow, все экраны — страницы этого стека. Реализованы переходы:
  - "slide"     — горизонтальный drill-down (Chat List -> Chat Room). 250ms,
                  InOutQuart; исходящий экран уезжает влево на 0.9 ширины
                  И гаснет (fade + сдвиг).
  - "crossfade" — переход между равноправными разделами (табы настроек).
                  180ms, OutCubic.
  - "push"      — вертикальный push для модалок/оверлеев (User Profile).
                  300ms, OutBack (лёгкий «перелёт» для playfulness).

Всё через QPropertyAnimation на geometry/opacity, без QTimer-хаков и без
пересчёта layout во время анимации (виджеты уже размещены до старта).
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget, QWidget

from qt_app.theme import DUR_CROSSFADE, DUR_PUSH, DUR_SLIDE


class SlidingStackedWidget(QStackedWidget):
    SLIDE_MS = DUR_SLIDE
    FADE_MS = DUR_CROSSFADE
    PUSH_MS = DUR_PUSH

    def __init__(self, parent=None):
        super().__init__(parent)
        self._animating = False
        self._pending: tuple[QWidget, str] | None = None
        self._group: QParallelAnimationGroup | None = None

    def go_to(self, widget: QWidget, transition: str = "slide") -> None:
        """transition: 'slide' | 'crossfade' | 'push'."""
        if widget is self.currentWidget():
            return
        if self._animating:
            # не рвём анимацию на середине — запомним последний запрошенный переход
            self._pending = (widget, transition)
            return
        if transition == "crossfade":
            self._crossfade_to(widget)
        elif transition == "push":
            self._push_to(widget)
        else:
            self._slide_to(widget)

    # -- slide (drill-down): исходящий уезжает влево и гаснет --
    def _slide_to(self, new_widget: QWidget) -> None:
        old_widget = self.currentWidget()
        w = self.width()

        old_effect = QGraphicsOpacityEffect(old_widget)
        old_widget.setGraphicsEffect(old_effect)

        self.setCurrentWidget(new_widget)
        new_widget.setGeometry(w, 0, w, self.height())

        self._animating = True
        group = QParallelAnimationGroup(self)

        fade_out = QPropertyAnimation(old_effect, b"opacity", self)
        fade_out.setDuration(self.SLIDE_MS)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutQuart)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        group.addAnimation(fade_out)

        anim_out = QPropertyAnimation(old_widget, b"pos", self)
        anim_out.setDuration(self.SLIDE_MS)
        anim_out.setEasingCurve(QEasingCurve.Type.InOutQuart)
        anim_out.setStartValue(QPoint(0, 0))
        anim_out.setEndValue(QPoint(-int(w * 0.9), 0))
        group.addAnimation(anim_out)

        anim_in = QPropertyAnimation(new_widget, b"pos", self)
        anim_in.setDuration(self.SLIDE_MS)
        anim_in.setEasingCurve(QEasingCurve.Type.InOutQuart)
        anim_in.setStartValue(QPoint(w, 0))
        anim_in.setEndValue(QPoint(0, 0))
        group.addAnimation(anim_in)

        group.finished.connect(lambda: self._finish_transition(old_widget, new_widget))
        # v1.9.5: DeleteWhenStopped — сотни переключений экранов больше не
        # копят мёртвые анимационные QObject (self._group перезаписывался,
        # старые группы оставались детьми навсегда).
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    # -- crossfade (равноправная навигация) --
    def _crossfade_to(self, new_widget: QWidget) -> None:
        old_widget = self.currentWidget()

        old_effect = QGraphicsOpacityEffect(old_widget)
        old_widget.setGraphicsEffect(old_effect)
        new_effect = QGraphicsOpacityEffect(new_widget)
        new_widget.setGraphicsEffect(new_effect)
        new_effect.setOpacity(0.0)

        self.setCurrentWidget(new_widget)

        self._animating = True
        group = QParallelAnimationGroup(self)

        anim_out = QPropertyAnimation(old_effect, b"opacity", self)
        anim_out.setDuration(self.FADE_MS)
        anim_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim_out.setStartValue(1.0)
        anim_out.setEndValue(0.0)

        anim_in = QPropertyAnimation(new_effect, b"opacity", self)
        anim_in.setDuration(self.FADE_MS)
        anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim_in.setStartValue(0.0)
        anim_in.setEndValue(1.0)

        group.addAnimation(anim_out)
        group.addAnimation(anim_in)
        group.finished.connect(lambda: self._finish_transition(old_widget, new_widget))
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    # -- push (модалки/оверлеи: OutBack, 300ms) --
    def _push_to(self, new_widget: QWidget) -> None:
        old_widget = self.currentWidget()
        h = self.height()

        old_effect = QGraphicsOpacityEffect(old_widget)
        old_widget.setGraphicsEffect(old_effect)
        old_effect.setOpacity(1.0)

        self.setCurrentWidget(new_widget)
        new_widget.setGeometry(0, h, self.width(), h)

        self._animating = True
        group = QParallelAnimationGroup(self)

        fade_out = QPropertyAnimation(old_effect, b"opacity", self)
        fade_out.setDuration(int(self.PUSH_MS * 0.6))
        fade_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.25)
        group.addAnimation(fade_out)

        # OutBack даёт лёгкий overshoot — анимация не выглядит мёртвой
        anim_in = QPropertyAnimation(new_widget, b"pos", self)
        anim_in.setDuration(self.PUSH_MS)
        anim_in.setEasingCurve(QEasingCurve.Type.OutBack)
        anim_in.setStartValue(QPoint(0, h))
        anim_in.setEndValue(QPoint(0, 0))
        group.addAnimation(anim_in)

        group.finished.connect(lambda: self._finish_transition(old_widget, new_widget))
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _finish_transition(self, old_widget: QWidget, new_widget: QWidget) -> None:
        old_widget.setGraphicsEffect(None)
        new_widget.setGraphicsEffect(None)
        old_widget.move(0, 0)
        new_widget.move(0, 0)
        self._animating = False
        if self._pending is not None:
            widget, transition = self._pending
            self._pending = None
            self.go_to(widget, transition)
