from __future__ import annotations

import io
from pathlib import Path

try:
    from PIL import Image, ImageDraw

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

NO_PIL_MESSAGE = (
    "Для своего фото нужна библиотека Pillow (её не будет по умолчанию, "
    "чтобы прога оставалась 'просто запусти и работает' без неё).\n\n"
    "Поставить: закрой это окно, открой командную строку и введи\n"
    "    pip install Pillow\n"
    "и запусти программу заново.\n\n"
    "Без Pillow всё остальное работает как обычно - просто вместо фото "
    "будет цветной кружок с первой буквой имени."
)


def make_circular_avatar_png(source_path: Path, size: int) -> bytes:
    """Обрезает картинку по центру в квадрат, ужимает до size x size и
    вырезает по кругу (альфа-маска) - на выходе готовый PNG, который любой
    клиент (даже без Pillow) сможет просто показать через tk.PhotoImage,
    круглая форма уже "запечена" в прозрачность."""
    if not HAS_PIL:
        raise RuntimeError(NO_PIL_MESSAGE)
    img = Image.open(source_path).convert("RGBA")
    w, h = img.size
    m = min(w, h)
    left, top = (w - m) // 2, (h - m) // 2
    img = img.crop((left, top, left + m, top + m)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    img.putalpha(mask)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
