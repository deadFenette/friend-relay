"""
Утилиты для обработки файлов в чате.

Включает:
- Скачивание файлов
- Показ превью изображений
- Обработку кликов по файлам
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lib import client
from qt_app.async_bridge import AsyncBridge
from qt_app.theme import PALETTE


class FileHandler(QObject):
    """Обработчик файлов для чата."""

    download_completed = Signal(str)  # file_path
    download_failed = Signal(str)  # error_message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()

    def download_file(
        self, base_url: str, file_id: str, filename: str, parent_widget: QWidget,
        access_key: str = "",
    ) -> None:
        """Скачивает файл и предлагает сохранить.

        Транспорт - lib/client.download_file (умная раздача: Range-куски,
        retry, сверка sha256). Раньше здесь был сырой urllib без
        X-Relay-Key - на хосте с ключом скачивание падало 403."""
        try:
            save_path, _ = QFileDialog.getSaveFileName(
                parent_widget, "Сохранить файл", str(Path.home() / filename), "Все файлы (*)"
            )
            if not save_path:
                return

            def do_download():
                client.download_file(
                    base_url, file_id, Path(save_path), access_key
                )
                return save_path

            def on_success(path: str):
                self.download_completed.emit(str(Path(path).name))

            def on_error(error: Exception):
                self.download_failed.emit(str(error))

            self._bridge.run(do_download, on_success=on_success, on_error=on_error)

        except Exception as e:
            self.download_failed.emit(str(e))

    def show_image_preview(
        self, base_url: str, file_id: str, filename: str, parent_widget: QWidget,
        access_key: str = "",
    ) -> None:
        """Показывает диалог с полноразмерным изображением."""
        dialog = QDialog(parent_widget)
        dialog.setWindowTitle(filename)
        dialog.setMinimumSize(600, 400)
        dialog.setStyleSheet(f"background: {PALETTE.surface_1};")

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Прокручиваемая область для изображения
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")

        image_label = QLabel()
        image_label.setAlignment(__import__("PySide6.QtCore").Qt.AlignmentFlag.AlignCenter)
        image_label.setStyleSheet(f"background: {PALETTE.surface_0};")
        image_label.setText("Загрузка...")

        scroll.setWidget(image_label)
        layout.addWidget(scroll, 1)

        # Кнопки внизу
        button_row = QWidget()
        button_row.setStyleSheet(f"background: {PALETTE.surface_2};")
        button_layout = QVBoxLayout(button_row)
        button_layout.setContentsMargins(12, 8, 12, 8)
        button_layout.setSpacing(8)

        download_btn = QPushButton("💾 Скачать")
        download_btn.setCursor(__import__("PySide6.QtCore").Qt.CursorShape.PointingHandCursor)
        download_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.accent};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {PALETTE.accent_hover};
            }}
        """)
        download_btn.clicked.connect(
            lambda: self.download_file(base_url, file_id, filename, parent_widget, access_key)
        )
        button_layout.addWidget(download_btn)

        button_layout.addStretch(1)

        close_btn = QPushButton("✕ Закрыть")
        close_btn.setCursor(__import__("PySide6.QtCore").Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.surface_3};
                color: {PALETTE.text_primary};
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background: {PALETTE.surface_2};
            }}
        """)
        close_btn.clicked.connect(dialog.close)
        button_layout.addWidget(close_btn)

        layout.addWidget(button_row)

        dialog.show()

        # Загружаем изображение асинхронно (через client - с ключом доступа)
        def fetch_image() -> bytes | None:
            try:
                return client.fetch_file_bytes(base_url, file_id, access_key)
            except Exception:
                return None

        def on_image_loaded(data: bytes | None) -> None:
            if not data:
                image_label.setText("Ошибка загрузки")
                return

            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                image_label.setText("Ошибка загрузки изображения")
                return

            # Масштабируем если слишком большое
            screen = parent_widget.screen()
            if screen:
                screen_size = screen.availableSize()
                max_width = screen_size.width() - 100
                max_height = screen_size.height() - 200

                if pixmap.width() > max_width or pixmap.height() > max_height:
                    pixmap = pixmap.scaled(
                        max_width,
                        max_height,
                        __import__("PySide6.QtCore").Qt.AspectRatioMode.KeepAspectRatio,
                        __import__("PySide6.QtCore").Qt.TransformationMode.SmoothTransformation,
                    )

            image_label.setPixmap(pixmap)
            image_label.setText("")

        self._bridge.run(
            fetch_image,
            on_success=on_image_loaded,
            on_error=lambda e: image_label.setText("Ошибка загрузки"),
        )

    def is_image_file(self, filename: str) -> bool:
        """Проверяет, является ли файл изображением."""
        image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
        return any(filename.lower().endswith(ext) for ext in image_extensions)

    def format_file_size(self, size: int) -> str:
        """Форматирует размер файла в читаемый вид."""
        if size < 1024:
            return f"{size} Б"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} КБ"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} МБ"
        else:
            return f"{size / (1024 * 1024 * 1024):.1f} ГБ"
