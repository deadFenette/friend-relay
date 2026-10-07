"""
Rail — вертикальная навигационная панель (левая зона макета).

64px, всегда виден, только иконки. При наведении расширяется до 240px
с подписями (200ms slide). Никакого дерева каналов — плоский список
человекочитаемых "Spaces".

Теперь использует SVG иконки вместо эмодзи.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from qt_app.icons import SvgIconWidget
from qt_app.theme import PALETTE, RADIUS

COLLAPSED_W = 64
EXPANDED_W = 240





class RailItem(QPushButton):
    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self._icon_name = icon_name
        self._label_text = label
        self._badge_count = 0

        # Создаём layout для контента
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 12, 0)
        layout.setSpacing(8)

        # SVG иконка
        self._icon_widget = SvgIconWidget(icon_name, size=24)
        layout.addWidget(self._icon_widget)

        # Текст
        self._text_label = QLabel(label)
        self._text_label.setStyleSheet(f"""
            QLabel {{
                color: {PALETTE.text_secondary};
                font-size: 14px;
            }}
        """)
        layout.addWidget(self._text_label)

        # Тултип
        self.setToolTip(f"{label}")

        # Бейдж (скрыт по умолчанию)
        self._badge = QLabel()
        self._badge.setVisible(False)
        self._badge.setStyleSheet(f"""
            QLabel {{
                background: {PALETTE.danger};
                color: white;
                border-radius: 10px;
                padding: 2px 6px;
                font-size: 12px;
                font-weight: 600;
            }}
        """)
        self._badge.setFixedSize(20, 20)
        layout.addWidget(self._badge)

        self.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                border: none;
                border-radius: {RADIUS}px;
                background: transparent;
            }}
            QPushButton:hover {{
                background: {PALETTE.surface_2};
            }}
            QPushButton:hover QLabel {{
                color: {PALETTE.text_primary};
            }}
            QPushButton:checked {{
                background: {PALETTE.surface_2};
            }}
            QPushButton:checked QLabel {{
                color: {PALETTE.text_primary};
                font-weight: 600;
            }}
        """)

    def set_expanded(self, expanded: bool) -> None:
        self._text_label.setVisible(expanded)
        # Бейдж виден только когда есть непрочитанное (count > 0).
        # Раньше тут было `False and ...` — dead code, бейдж никогда не
        # показывался в сжатом состоянии. Теперь показываем всегда когда
        # count > 0, независимо от expanded.
        self._badge.setVisible(self._badge_count > 0)

    def set_horizontal(self, horizontal: bool) -> None:
        """Режим нижней панели (окно <800px): иконка по центру кнопки,
        без боковых полей — иначе при ширине кнопки ~36px иконка обрезается."""
        layout = self.layout()
        if layout is not None:
            margins = (0, 0, 0, 0) if horizontal else (20, 0, 12, 0)
            layout.setContentsMargins(*margins)
        self._text_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter if horizontal else Qt.AlignmentFlag.AlignLeft
        )
        self.setMinimumWidth(56 if horizontal else 0)

    def set_badge(self, count: int) -> None:
        """Устанавливает счётчик для бейджа."""
        self._badge_count = count
        if count > 0:
            if count > 9:
                self._badge.setText("9+")
            else:
                self._badge.setText(str(count))
            self._badge.setVisible(True)
        else:
            self._badge.setVisible(False)


class Rail(QWidget):
    """Emits item_selected(key) when a space is clicked.

    Два режима (responsive):
      - вертикальный (по умолчанию): 64px слева, при наведении 240px;
      - горизонтальный «bottom tab bar» для окон < 800px: фиксированная
        высота, только иконки, без раскрытия.
    """

    item_selected = Signal(str)

    BOTTOM_BAR_H = 56

    def __init__(self, items: list[tuple[str, str, str]], parent=None):
        """items: list of (key, icon, label)."""
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setStyleSheet(f"#Rail {{ background: {PALETTE.surface_1}; }}")
        self.setFixedWidth(COLLAPSED_W)

        self._items: dict[str, RailItem] = {}
        self._expanded = False
        self._horizontal = False

        # QBoxLayout (а не QVBoxLayout): направление переключается на лету
        # через setDirection — без пересоздания layout и переустановки
        # родителей элементов (перенос layout на temp-виджет в PySide6
        # удалял C++-объекты кнопок при deleteLater).
        layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        layout.setContentsMargins(8, 16, 8, 16)
        layout.setSpacing(4)

        for key, icon, label in items:
            btn = RailItem(icon, label, self)
            btn.clicked.connect(lambda checked, k=key: self._on_clicked(k))
            layout.addWidget(btn)
            self._items[key] = btn
        layout.addStretch(1)

        if items:
            self._items[items[0][0]].setChecked(True)

        self._width_anim = QPropertyAnimation(self, b"minimumWidth", self)
        self._width_anim.setDuration(200)
        self._width_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._max_anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._max_anim.setDuration(200)
        self._max_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -- режимы: <800px рейл становится нижней панелью --

    def set_orientation(self, horizontal: bool) -> None:
        """Перекладывает элементы между вертикальным и горизонтальным
        режимом (смена направления QBoxLayout — элементы сохраняются).
        Состояние (checked/бейджи) сохраняется."""
        if horizontal == self._horizontal:
            return
        self._horizontal = horizontal

        layout = self.layout()
        if layout is not None:
            layout.setDirection(
                QBoxLayout.Direction.LeftToRight
                if horizontal
                else QBoxLayout.Direction.TopToBottom
            )
            margins = (12, 6, 12, 6) if horizontal else (8, 16, 8, 16)
            layout.setContentsMargins(*margins)
            for btn in self._items.values():
                btn.set_expanded(False)
                btn.set_horizontal(horizontal)

        if horizontal:
            self._width_anim.stop()
            self._max_anim.stop()
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setFixedHeight(self.BOTTOM_BAR_H)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.setFixedWidth(COLLAPSED_W)

    def is_horizontal(self) -> bool:
        return self._horizontal

    def _on_clicked(self, key: str) -> None:
        for k, btn in self._items.items():
            btn.setChecked(k == key)
        self.item_selected.emit(key)

    def enterEvent(self, event) -> None:
        if not self._horizontal:
            self._animate_width(EXPANDED_W, expanded=True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._horizontal:
            self._animate_width(COLLAPSED_W, expanded=False)
        super().leaveEvent(event)

    def _animate_width(self, target: int, expanded: bool) -> None:
        self._expanded = expanded
        for btn in self._items.values():
            btn.set_expanded(expanded)
        for anim in (self._width_anim, self._max_anim):
            anim.stop()
            anim.setStartValue(self.width())
            anim.setEndValue(target)
            anim.start()

    def set_badge(self, key: str, count: int) -> None:
        """Устанавливает бейдж для элемента по ключу."""
        if key in self._items:
            self._items[key].set_badge(count)

    def clear_badge(self, key: str) -> None:
        """Очищает бейдж для элемента по ключу."""
        self.set_badge(key, 0)
