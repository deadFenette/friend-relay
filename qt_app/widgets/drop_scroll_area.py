"""
DropScrollArea — QScrollArea с рабочим drag-and-drop файлов и красивой
подсвеченной зоной сброса.

Раньше в проекте было три копии этого класса (chat_screen.py, dm_screen.py)
с одинаковыми dragEnterEvent/dropEvent, но БЕЗ setAcceptDrops(True) — из-за
этого Qt никогда не доставлял события перетаскивания этим виджетам, и вся
фича была мертва по всему приложению. Здесь: один класс, используется везде,
с явным setAcceptDrops и видимым оверлеем поверх ленты во время перетаскивания.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from qt_app.theme import PALETTE, RADIUS


class _DropOverlay(QWidget):
    """Полупрозрачная подсвеченная зона с пунктирной рамкой - появляется
    поверх ленты, когда пользователь тащит файл над окном."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # QWidget (в отличие от QLabel/QPushButton) не рисует background/
        # border из QSS без этого атрибута - без него оверлей был бы
        # прозрачным прямоугольником без видимой рамки и подложки.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Без селектора-обёртки (просто голый набор правил) - именно так
        # стилизован весь остальной проект (см. message_bubble.py и т.п.).
        # Именованный селектор "_DropOverlay { ... }" тут не срабатывал:
        # глобальный QWidget { background: transparent } из theme.py
        # перебивал его в каскаде, и рамка с фоном просто не были видны.
        self.setStyleSheet(f"""
            background: rgba(18, 18, 24, 0.88);
            border: 2px dashed {PALETTE.accent};
            border-radius: {RADIUS * 2}px;
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)

        icon = QLabel("📥")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 40px; background: transparent; border: none;")
        layout.addWidget(icon)

        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(f"""
            color: {PALETTE.text_primary};
            font-size: 14px;
            font-weight: 600;
            background: transparent;
            border: none;
        """)
        layout.addWidget(label)

        self.hide()

    def show_over(self, target: QWidget) -> None:
        self.setGeometry(0, 0, target.width(), target.height())
        self.raise_()
        self.show()


class DropScrollArea(QScrollArea):
    """QScrollArea с рабочим drag-and-drop файлов (см. docstring модуля)."""

    file_dropped = Signal(Path)

    def __init__(
        self,
        parent: QWidget | None = None,
        drop_text: str = "Отпустите файл здесь, чтобы отправить",
    ) -> None:
        super().__init__(parent)
        # Вот он, недостающий кусок: без этого Qt не доставляет
        # dragEnterEvent/dropEvent этому виджету вообще, независимо от того,
        # что эти методы переопределены ниже.
        self.setAcceptDrops(True)
        self._overlay = _DropOverlay(drop_text, self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._overlay.isVisible():
            self._overlay.show_over(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
            self._overlay.show_over(self)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._overlay.hide()
        urls = event.mimeData().urls()
        any_file = False
        for url in urls:
            if url.isLocalFile():
                file_path = Path(url.toLocalFile())
                if file_path.is_file():
                    self.file_dropped.emit(file_path)
                    any_file = True
        if any_file:
            event.acceptProposedAction()
        else:
            event.ignore()
