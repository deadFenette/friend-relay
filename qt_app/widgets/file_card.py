"""
FileCard — карточка файла в сетке вкладки «Файлы». Аналог связки
`_file_card()` + `_fetch_file_preview()` из lib/tabs_ui.py, но превью
грузится через AsyncBridge (сигналы) вместо ручной UI-очереди.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from lib.file_kind import file_icon, file_kind, is_previewable_image
from qt_app.theme import PALETTE, RADIUS

THUMB_W, THUMB_H = 160, 120


def fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


class FileCard(QWidget):
    download_clicked = Signal(str)  # file_id
    delete_clicked = Signal(int)  # seq

    def __init__(self, file_event: dict, parent=None, can_delete: bool = False):
        super().__init__(parent)
        self.file_id = file_event.get("file_id", "")
        self.name = file_event.get("name", "файл")
        self.size_bytes = file_event.get("size", 0)
        self.sender = file_event.get("from", "?")
        self.seq = file_event.get("seq")
        self.kind = file_kind(self.name)

        self.setObjectName("FileCard")
        self.setFixedWidth(THUMB_W + 20)
        self.setStyleSheet(f"""
            #FileCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._thumb = QLabel()
        self._thumb.setFixedSize(THUMB_W, THUMB_H)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            f"background: {PALETTE.surface_3}; "
            f"border-top-left-radius: {RADIUS}px; border-top-right-radius: {RADIUS}px;"
        )
        self._show_icon_placeholder()
        layout.addWidget(self._thumb)

        meta = QVBoxLayout()
        meta.setContentsMargins(10, 8, 10, 10)
        meta.setSpacing(2)

        name_lbl = QLabel(self._elide(self.name))
        name_lbl.setToolTip(self.name)
        name_lbl.setStyleSheet(f"color: {PALETTE.text_primary}; font-weight: 600;")
        meta.addWidget(name_lbl)

        info_lbl = QLabel(f"{fmt_size(self.size_bytes)} · {self.sender}")
        info_lbl.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
        meta.addWidget(info_lbl)

        dl_btn = QPushButton("⬇ Скачать")
        dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dl_btn.setStyleSheet(f"""
            QPushButton {{
                margin-top: 6px;
                background: {PALETTE.accent};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: {RADIUS}px;
                padding: 6px 0;
            }}
            QPushButton:hover {{ background: {PALETTE.accent_hover}; }}
            QPushButton:pressed {{ background: {PALETTE.accent_press}; }}
        """)
        dl_btn.clicked.connect(lambda: self.download_clicked.emit(self.file_id))
        meta.addWidget(dl_btn)

        if can_delete and self.seq is not None:
            del_btn = QPushButton("🗑 Удалить")
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(f"""
                QPushButton {{
                    margin-top: 4px;
                    background: transparent;
                    color: {PALETTE.danger};
                    border: 1px solid {PALETTE.danger};
                    border-radius: {RADIUS}px;
                    padding: 5px 0;
                }}
                QPushButton:hover {{ background: {PALETTE.danger}; color: {PALETTE.text_on_accent}; }}
            """)
            del_btn.clicked.connect(lambda: self.delete_clicked.emit(self.seq))
            meta.addWidget(del_btn)

        layout.addLayout(meta)

    def _elide(self, text: str, limit: int = 28) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _show_icon_placeholder(self) -> None:
        self._thumb.setText(file_icon(self.kind))
        self._thumb.setStyleSheet(
            self._thumb.styleSheet() + f"font-size: 40px; color: {PALETTE.text_secondary};"
        )

    def is_previewable(self, max_bytes: int) -> bool:
        return is_previewable_image(self.name, self.size_bytes, max_bytes)

    def set_preview_pixmap(self, pixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        scaled = pixmap.scaled(
            THUMB_W,
            THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb.setText("")
        self._thumb.setPixmap(scaled)
