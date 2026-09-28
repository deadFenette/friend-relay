"""
Qt-версия `lib.file_kind.make_preview_image` — та же идея (байты -> уменьшенная
картинка для превью), но возвращает QPixmap вместо tk.PhotoImage.

lib/file_kind.py трогать не стала: file_kind()/file_icon()/is_previewable_image()
там чистые (без tkinter) и одинаково нужны и старому, и новому UI. А вот
make_preview_image привязан к tk.Misc/tk.PhotoImage, так что для Qt — своя
версия рядом, в qt_app.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from lib.constants import FILE_PREVIEW_MAX_WIDTH

try:
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgRenderer

    HAS_QTSVG = True
except ImportError:
    HAS_QTSVG = False


def make_preview_pixmap(
    data: bytes, max_width: int = FILE_PREVIEW_MAX_WIDTH, filename: str | None = None
) -> QPixmap | None:
    """PNG/JPEG/GIF/BMP — нативно через QPixmap (Qt сам их декодирует).
    SVG — через QSvgRenderer, если модуль QtSvg доступен."""
    ext = Path(filename or "").suffix.lower()

    if ext == ".svg":
        return _svg_to_pixmap(data, max_width)

    pixmap = QPixmap()
    if not pixmap.loadFromData(data):
        return None
    if pixmap.width() > max_width:
        pixmap = pixmap.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    return pixmap


def _svg_to_pixmap(data: bytes, max_width: int) -> QPixmap | None:
    if not HAS_QTSVG:
        return None
    try:
        renderer = QSvgRenderer(data)
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        if size.isEmpty():
            return None
        if size.width() > max_width:
            h = max(1, int(size.height() * max_width / size.width()))
            size.setWidth(max_width)
            size.setHeight(h)
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return pixmap
    except Exception:
        return None
