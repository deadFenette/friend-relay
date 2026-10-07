"""ReactionPopover — стеклянная «пилюля» быстрых эмодзи (v9.5, редизайн 6.3).

Вместо панельки с серыми кнопками под баблом (v9.4 — выглядела топорно)
сделано как в современных мессенджерах: маленькая круглая кнопка у угла
бабла открывает плавающий стеклянный пикер с КРУПНЫМИ цветными эмодзи.

Дизайн:
- frameless Qt.Popup: закрывается кликом мимо и по Esc (стандарт попапов);
- появление: windowOpacity fade + подлёт на 10px с OutBack-перелётом,
  чтобы пикер «пружинил». Используется именно
  windowOpacity, а не QGraphicsOpacityEffect: на попапе рисуется своя
  стеклянная подложка (paintEvent), и вкладывать эффекты нельзя;
- эмодзи крупные (22px) и цветные — реакция читается мгновенно;
- эмодзи, которым я уже реагировал, обведено accent-кольцом: видно, что
  клик по нему снимет реакцию;
- ховер эмодзи — круглый glass-фон через QSS (без QGraphicsEffect на
  кнопках: вложенные эффекты в Qt запрещены).

OOP: класс отвечает ТОЛЬКО за пикер. Позиционирование под бабл и запрет
второго открытого пикера — методы класса (open_for). Владелец сигнала —
MessageBubble: попап не знает ни про сервер, ни про ленту.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from qt_app.theme import DUR_POP, PALETTE, out_back_overshoot

QUICK_EMOJIS: tuple[str, ...] = ("👍", "❤️", "😂", "🔥", "😮", "😢")

_BTN = 42          # клетка одного эмодзи
_PAD_H, _PAD_V = 10, 7


class ReactionPopover(QWidget):
    """Плавающий пикер реакций. Открывать через ReactionPopover.open_for()."""

    emoji_picked = Signal(str)

    def __init__(self, my_reaction: str = "", parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._my_reaction = my_reaction
        self._base_y = 0
        self._float_y = 10.0  # стартуем на 10px ниже — анимация поднимет

        row = QHBoxLayout(self)
        row.setContentsMargins(_PAD_H, _PAD_V, _PAD_H, _PAD_V)
        row.setSpacing(2)
        for emoji in QUICK_EMOJIS:
            btn = QPushButton(emoji, self)
            btn.setFixedSize(_BTN, _BTN)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("Реагировать " + emoji)
            mine = emoji == my_reaction
            ring = (
                f"border: 2px solid {PALETTE.accent};"
                if mine
                else "border: 2px solid transparent;"
            )
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent;
                    {ring}
                    border-radius: {_BTN // 2}px;
                    font-size: 22px;
                }}
                QPushButton:hover {{
                    background: rgba(255,255,255,0.10);
                }}
                QPushButton:pressed {{
                    background: rgba(255,255,255,0.18);
                }}
                """
            )
            btn.clicked.connect(lambda checked=False, e=emoji: self._pick(e))
            row.addWidget(btn)

        self.setFixedSize(self.sizeHint())

    def _pick(self, emoji: str) -> None:
        self.emoji_picked.emit(emoji)
        self.close()

    # -- стеклянная подложка (свой paintEvent: полупрозрачная пилюля) -------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = r.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        # плотная тёмная подложка — текст/эмодзи читаются на любом фоне ленты
        p.fillPath(path, QColor(18, 18, 24, 244))
        # тонкий стеклянный кант + блик сверху (Spatial Glass)
        p.setPen(QColor(255, 255, 255, 30))
        p.drawPath(path)
        p.setPen(QColor(255, 255, 255, 12))
        highlight = QPainterPath()
        highlight.addRoundedRect(
            QRectF(r.left() + 2, r.top() + 1.5, r.width() - 4, r.height() / 2),
            radius, radius,
        )
        p.drawPath(highlight)

    # -- анимация появления: fade + подлёт с OutBack-перелётом --------------

    def _get_float_y(self) -> float:
        return self._float_y

    def _set_float_y(self, value: float) -> None:
        self._float_y = value
        self.move(self.x(), self._base_y + round(value))

    floatY = Property(float, _get_float_y, _set_float_y)

    def _play_intro(self) -> None:
        self._base_y = self.y()
        self.move(self.x(), self._base_y + int(self._float_y))

        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(DUR_POP)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        rise = QPropertyAnimation(self, b"floatY", self)
        rise.setDuration(DUR_POP + 60)
        rise.setStartValue(10.0)
        rise.setEndValue(0.0)
        rise.setEasingCurve(out_back_overshoot(2.2))

        self.setWindowOpacity(0.0)
        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        rise.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # -- открытие у конкретного бабла (единственный активный попап) ---------

    _active: ReactionPopover | None = None

    @classmethod
    def open_for(cls, bubble, my_reaction: str = "") -> ReactionPopover:
        """Закрывает предыдущий пикер (если был) и открывает новый у бабла.

        Позиционирование — НАД баблом, выравнивание по его стороне:
        исходящие (is_me) — правый край бабла, входящие — левый.
        Если сверху места нет — падает под бабл.
        """
        prev = cls._active
        if prev is not None:
            try:
                prev.close()
            except RuntimeError:
                pass
            cls._active = None

        pop = cls(my_reaction=my_reaction)
        # Проводку к баблу делает ВЫЗЫВАЮЩИЙ (MessageBubble._open_reaction_popover
        # подписывается на emoji_picked) — попап остаётся чистым пикером и не
        # знает про сервер. Дублировать соединение здесь нельзя: реакция
        # улетела бы дважды.
        pop._position_near(bubble)
        cls._active = pop
        # v1.9.5: прозрачность выставляем ДО show() — раньше первый кадр
        # рисовался полностью непрозрачным (вспышка) и только потом
        # _play_intro делал setWindowOpacity(0.0).
        pop.setWindowOpacity(0.0)
        pop.show()
        pop._play_intro()
        return pop

    def _position_near(self, bubble) -> None:
        label = bubble._label
        origin = label.mapToGlobal(label.rect().topLeft())
        w, h = self.width(), self.height()
        x = origin.x() + label.width() - w if bubble._is_me else origin.x()
        y = origin.y() - h - 10
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            x = max(screen.left() + 8, min(x, screen.right() - w - 8))
            if y < screen.top() + 8:  # сверху не влезает — показываем под баблом
                y = origin.y() + min(label.height(), 48) + 10
            y = max(screen.top() + 8, y)
        self.move(x, y)

    def closeEvent(self, event) -> None:
        if ReactionPopover._active is self:
            ReactionPopover._active = None
        super().closeEvent(event)
