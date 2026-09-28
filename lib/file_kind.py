from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from lib.constants import FILE_PREVIEW_MAX_WIDTH

if TYPE_CHECKING:
    import tkinter as tk

try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import cairosvg  # type: ignore

    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False

FileKind = Literal["image", "document", "archive", "mod", "file"]

_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".svg"}
_ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz", ".zst"}
_DOCUMENT = {
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".rtf",
    ".odt",
    ".xls",
    ".xlsx",
    ".csv",
    ".ppt",
    ".pptx",
    ".md",
    ".json",
    ".xml",
    ".html",
    ".htm",
}
_MOD = {
    ".jar",
    ".pak",
    ".pk3",
    ".wad",
    ".esp",
    ".esm",
    ".esl",
    ".fomod",
    ".unitypackage",
    ".u mod",
    ".rmod",
    ".dzip",
}

_ICON = {
    "image": "🖼",
    "document": "📄",
    "archive": "📦",
    "mod": "🎮",
    "file": "📎",
}


def file_kind(filename: str) -> FileKind:
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE:
        return "image"
    if ext in _MOD:
        return "mod"
    if ext in _ARCHIVE:
        return "archive"
    if ext in _DOCUMENT:
        return "document"
    return "file"


def file_icon(kind: FileKind) -> str:
    return _ICON[kind]


def is_previewable_image(filename: str, size_bytes: int, max_bytes: int) -> bool:
    return file_kind(filename) == "image" and 0 < size_bytes <= max_bytes


def _svg_to_png_bytes(svg_bytes: bytes, max_width: int) -> bytes | None:
    if not HAS_CAIROSVG:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg_bytes, output_width=max_width)
    except Exception:
        return None


def make_preview_image(
    data: bytes,
    master: tk.Misc,
    max_width: int = FILE_PREVIEW_MAX_WIDTH,
    filename: str | None = None,
) -> tk.PhotoImage | None:
    """PNG/GIF через Tk; JPEG/WebP/SVG — через Pillow/cairosvg если установлены.

    tkinter импортируется тут же, локально (а не в шапке файла) — эта функция
    единственное место в модуле, которому он реально нужен. Остальные функции
    (file_kind/file_icon/is_previewable_image) чистые и должны быть
    импортируемы даже там, где tkinter не установлен (например, из Qt-версии
    UI в qt_app/, которая эту функцию не использует вовсе — у неё своя,
    qt_app/preview.py, через QPixmap)."""
    import tkinter as tk

    ext = Path(filename or "").suffix.lower()

    if ext == ".svg":
        png_bytes = _svg_to_png_bytes(data, max_width)
        if png_bytes is not None:
            data = png_bytes

    try:
        img = tk.PhotoImage(data=data, master=master)
    except tk.TclError:
        if not HAS_PIL:
            return None
        try:
            pil = Image.open(io.BytesIO(data))
            if pil.mode not in ("RGB", "RGBA"):
                pil = pil.convert("RGBA")
            w, h = pil.size
            if w > max_width:
                nh = max(1, int(h * max_width / w))
                pil = pil.resize((max_width, nh), Image.LANCZOS)
            img = ImageTk.PhotoImage(pil, master=master)
        except Exception:
            return None
    if img.width() > max_width:
        factor = max(1, (img.width() + max_width - 1) // max_width)
        img = img.subsample(factor, factor)
    return img
