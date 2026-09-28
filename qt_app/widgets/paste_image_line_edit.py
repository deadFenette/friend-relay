"""
PasteImageLineEdit — обычный QLineEdit, который вдобавок умеет Ctrl+V
изображением из буфера обмена (например, после PrintScreen).

Обычный QLineEdit при вставке умеет только текст — картинку в буфере он
просто молча игнорирует, ничего не происходит. Чтобы вставить скриншот,
нужно явно перехватывать Ctrl+V, проверять буфер на наличие изображения и
обрабатывать его отдельно, до того как событие дойдёт до штатной текстовой
вставки.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit


class PasteImageLineEdit(QLineEdit):
    """QLineEdit + image_pasted(QImage), если в буфере обмена на Ctrl+V
    оказалась картинка вместо текста."""

    image_pasted = Signal(QImage)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime is not None and mime.hasImage():
                image = clipboard.image()
                if not image.isNull():
                    self.image_pasted.emit(image)
                    return  # не даём событию дойти до обычной вставки текста
        super().keyPressEvent(event)


def save_clipboard_image_to_temp(image: QImage) -> Path | None:
    """Сохраняет вставленное изображение в PNG во временную папку с именем
    по времени - дальше отправляется тем же путём, что и обычный файл
    (см. _on_image_pasted в chat_screen.py/dm_screen.py)."""
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        return None
    # v1.9.5: уникальный суффикс — две вставки за одну секунду раньше
    # писали в ОДИН файл, и первая могла уйти как содержимое второй.
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    dest = Path(tempfile.gettempdir()) / f"pasted_{ts}_{time.monotonic_ns() % 100000}.png"
    if not pixmap.save(str(dest), "PNG"):
        return None
    return dest
