"""CommandPalette — spotlight-оверлей с поиском по всему приложению.

Вызывается по Ctrl+K: затемнённая подложка, карточка поиска поверх всего
окна. Ищет по разделам, ЛС, настройкам и действиям — основной инструмент
опытного пользователя.

Spotlight-оверлей поверх всего окна:
  - затемнённый фон rgba(11,11,15,0.72) (настоящий blur-behind на QWidget
    без QML/WebEngine не даётся, поэтому используем translucent-подложку);
  - карточка по центру: surface_1, радиус XL, 1px бордер, ОДИН
    QGraphicsDropShadowEffect на вид (blur=20, 15%);
  - появление: карточка падает сверху с OutBack 200ms + фейд фона;
  - навигация ↑↓, Enter — выполнить, Esc — закрыть (Esc всегда закрывает
    оверлеи);
  - клик мимо карточки тоже закрывает.

Регистрация команд: ``palette.register(title, callback, icon=…, hint=…,
category=…, keywords=…)``. Поиск — по подстроке в title/hint/category/
keywords, без учёта регистра.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qt_app.theme import (
    DUR_FAST,
    DUR_PUSH,
    EASE_OUT_CUBIC,
    PALETTE,
    RADIUS,
    RADIUS_XL,
)


@dataclass
class Command:
    title: str
    callback: Callable[[], None]
    icon: str = ""
    hint: str = ""
    category: str = ""
    keywords: str = ""
    shortcut: str = ""

    def searchable(self) -> str:
        return " ".join((self.title, self.hint, self.category, self.keywords)).lower()

    def display(self) -> str:
        icon = f"{self.icon}  " if self.icon else ""
        return f"{icon}{self.title}"

    def suffix(self) -> str:
        parts = [p for p in (self.shortcut, self.hint) if p]
        return "   ".join(parts)


class CommandPalette(QWidget):
    """Оверлей поверх родителя. Родитель обязан вызывать resizeEvent →
    ``palette.setGeometry(self.rect())`` (см. MainWindow)."""

    opened = Signal()
    closed = Signal()

    MAX_RESULTS = 40

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("PaletteRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#PaletteRoot { background: transparent; }"
        )
        self.setVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._commands: list[Command] = []
        self._card: QWidget | None = None

        # Фон-подложка — ОТДЕЛЬНЫЙ виджет-сиблинг карточки: вложенные
        # QGraphicsEffect (opacity на корне + shadow на карточке) Qt
        # не поддерживает («paint device can only be painted once»). Тёмный
        # полупрозрачный слой уходит в подложку с opacity-эффектом, тень —
        # остаётся на карточке.
        self._backdrop = QWidget(self)
        self._backdrop.setObjectName("PaletteBackdrop")
        self._backdrop.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._backdrop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._backdrop.setStyleSheet(
            "#PaletteBackdrop { background: rgba(11, 11, 15, 0.72); }"
        )
        self._backdrop_effect = QGraphicsOpacityEffect(self._backdrop)
        self._backdrop_effect.setOpacity(0.0)
        self._backdrop.setGraphicsEffect(self._backdrop_effect)

        self._build_card()

    # ------------------------------------------------------------------
    # Карточка
    # ------------------------------------------------------------------
    def _build_card(self) -> None:
        card = QWidget(self)
        card.setObjectName("PaletteCard")
        card.setStyleSheet(
            f"""
            #PaletteCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border};
                border-radius: {RADIUS_XL}px;
            }}
        """
        )
        # Единственная тень на этот view (blur=20, 15%)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 8)
        color = QColor(0, 0, 0)
        color.setAlphaF(0.15)
        shadow.setColor(color)
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(10)

        self._input = QLineEdit(card)
        self._input.setPlaceholderText("Куда идём?  ·  команды, разделы, каналы…")
        self._input.setStyleSheet(
            f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {PALETTE.text_primary};
                font-size: 17px;
                padding: 2px 6px;
            }}
        """
        )
        self._input.textChanged.connect(self._on_query_changed)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        self._list = QListWidget(card)
        self._list.setObjectName("PaletteList")
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setStyleSheet(
            f"""
            QListWidget {{
                background: transparent;
                border: none;
                font-size: 14px;
            }}
            QListWidget::item {{
                padding: 9px 10px;
                border-radius: {RADIUS}px;
                margin: 1px 0;
            }}
            QListWidget::item:selected {{
                background: {PALETTE.accent_dim};
                color: {PALETTE.text_primary};
            }}
            QListWidget::item:hover {{ background: {PALETTE.surface_2}; }}
        """
        )
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.itemActivated.connect(self._activate_item)
        self._list.itemClicked.connect(self._activate_item)
        layout.addWidget(self._list, 1)

        footer = QLabel("↑↓ — навигация   ·   Enter — открыть   ·   Esc — закрыть")
        footer.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 12px; padding: 2px 6px 4px;"
        )
        layout.addWidget(footer)

        self._card = card

    # ------------------------------------------------------------------
    # Регистрация команд
    # ------------------------------------------------------------------
    def register(
        self,
        title: str,
        callback: Callable[[], None],
        icon: str = "",
        hint: str = "",
        category: str = "",
        keywords: str = "",
        shortcut: str = "",
    ) -> None:
        """Добавляет команду. Повторный вызов с тем же title — заменяет."""
        self.unregister(title)
        self._commands.append(
            Command(
                title=title,
                callback=callback,
                icon=icon,
                hint=hint,
                category=category,
                keywords=keywords,
                shortcut=shortcut,
            )
        )

    def unregister(self, title: str) -> None:
        self._commands = [c for c in self._commands if c.title != title]

    # ------------------------------------------------------------------
    # Открытие/закрытие + анимации (Vertical Push)
    # ------------------------------------------------------------------
    def is_open(self) -> bool:
        return self.isVisible()

    def open_palette(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self._backdrop.setGeometry(self.rect())
        self._input.clear()
        self._fill_results("")
        self.setVisible(True)
        self.raise_()
        self._input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._animate_open()
        self.opened.emit()

    def close_palette(self) -> None:
        self.setVisible(False)
        self.closed.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.close_palette()
        else:
            self.open_palette()

    def _animate_open(self) -> None:
        if self._card is None:
            return
        card = self._card
        w = self.width()
        card_h = min(420, max(260, self.height() // 2))
        card_w = min(560, w - 64)
        card.setGeometry((w - card_w) // 2, 96, card_w, card_h)
        card.show()
        card.raise_()

        group = QParallelAnimationGroup(self)

        fade = QPropertyAnimation(self._backdrop_effect, b"opacity", self)
        fade.setDuration(DUR_FAST)
        fade.setEasingCurve(EASE_OUT_CUBIC)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        group.addAnimation(fade)

        drop = QPropertyAnimation(card, b"pos", self)
        drop.setDuration(DUR_PUSH)
        drop.setEasingCurve(QEasingCurve.Type.OutBack)
        drop.setStartValue(card.pos() + QPoint(0, -14))
        drop.setEndValue(card.pos())
        group.addAnimation(drop)

        # v1.9.5: DeleteWhenStopped — не копим мёртвые группы
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    # ------------------------------------------------------------------
    # Фильтрация и результаты
    # ------------------------------------------------------------------
    def _on_query_changed(self, query: str) -> None:
        self._fill_results(query.strip().lower())

    def _fill_results(self, query: str) -> None:
        self._list.clear()
        matched: list[Command] = []
        for cmd in self._commands:
            if not query or query in cmd.searchable():
                matched.append(cmd)
            if len(matched) >= self.MAX_RESULTS:
                break

        for cmd in matched:
            item = QListWidgetItem(cmd.display())
            if cmd.suffix():
                item.setText(f"{cmd.display()}      {cmd.suffix()}")
            item.setToolTip(cmd.category or cmd.title)
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            self._list.addItem(item)

        if not matched:
            empty = QListWidgetItem("Ничего не найдено")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)
        elif self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _activate_item(self, item: QListWidgetItem) -> None:
        cmd: Command | None = item.data(Qt.ItemDataRole.UserRole)
        if cmd is None:
            return
        self.close_palette()
        try:
            cmd.callback()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Клавиатура и мышь
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        """↑↓/Enter/Esc прямо в поле ввода управляют списком."""
        if obj is self._input and event.type() == QKeyEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self._list.currentItem()
                if item is not None:
                    self._activate_item(item)
                return True
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
        return super().eventFilter(obj, event)

    def _move_selection(self, delta: int) -> None:
        row = self._list.currentRow() + delta
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_palette()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        # Клик по фону (мимо карточки) закрывает
        card = self._card
        if card is None or not card.geometry().contains(event.position().toPoint()):
            self.close_palette()
            return
        super().mousePressEvent(event)
