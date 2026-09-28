"""
Виджет аватарки для PySide6.

Показывает круглое изображение аватарки или цветной кружок с буквой,
если аватарка не загружена. Поддерживает асинхронную загрузку и кеширование.

Оптимизация v9.3: раньше кешировались только СЫРЫЕ байты аватарки
(AvatarCache), а QPixmap.loadFromData + smooth-масштаб + кругление
выполнялись заново для КАЖДОГО сообщения при каждом рендере ленты
(переключение канала = до сотни декодов одного и того же 96px PNG
в GUI-потоке). Теперь два уровня кеша:
  1. AvatarCache      — name -> bytes     (сеть: скачали один раз)
  2. AvatarPixmapCache — (name, size) -> готовый круглый QPixmap
     (декод + масштаб + маска: один раз на пару (юзер, размер))
Пиксмап-кеш отсекает самый дорогой кусок перерисовки ленты.
"""

from __future__ import annotations

import hashlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QLabel

from qt_app.async_bridge import AsyncBridge


def _color_for_name(name: str) -> QColor:
    """Генерирует стабильный цвет на основе имени."""
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    hue = h % 360
    return QColor.fromHsl(hue, 150, 180)


class AvatarPixmapCache:
    """Кеш ГОТОВЫХ круглых пиксмапов аватарок: (name, size) -> QPixmap.

    Только GUI-поток (QPixmap вне GUI-потока нельзя). Вытеснения нет:
    записей порядка десятков (число знакомых юзеров × пара размеров) —
    память мизерная, а hit-rate нужен максимальный.
    """

    _cache: dict[tuple[str, int], QPixmap] = {}

    @classmethod
    def get(cls, name: str, size: int) -> QPixmap | None:
        return cls._cache.get((name, size))

    @classmethod
    def put(cls, name: str, size: int, pixmap: QPixmap) -> None:
        cls._cache[(name, size)] = pixmap

    @classmethod
    def invalidate(cls, name: str) -> None:
        """Пользователь сменил аватарку — выбрасываем все его размеры."""
        for key in [k for k in cls._cache if k[0] == name]:
            cls._cache.pop(key, None)

    @classmethod
    def clear(cls) -> None:
        cls._cache.clear()


class AvatarWidget(QLabel):
    """Круглый аватар с поддержкой асинхронной загрузки."""

    avatar_loaded = Signal()  # испускается после успешной загрузки

    def __init__(self, name: str, size: int = 40, parent=None):
        super().__init__(parent)
        self._name = name
        self._size = size
        self._avatar_data: bytes | None = None
        self._bridge = AsyncBridge()
        self._load_in_progress = False

        self.setFixedSize(size, size)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        """Показывает цветной кружок с первой буквой имени."""
        pixmap = QPixmap(self._size, self._size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Рисуем круг
        color = _color_for_name(self._name)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, self._size, self._size)

        # Рисуем букву
        letter = self._name[0].upper() if self._name else "?"
        painter.setPen(QColor(255, 255, 255))
        font = painter.font()
        font.setPixelSize(int(self._size * 0.5))
        font.setBold(True)
        painter.setFont(font)

        rect = pixmap.rect()
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, letter)

        painter.end()
        self.setPixmap(pixmap)

    def set_username(self, name: str) -> None:
        """Устанавливает новое имя пользователя."""
        self._name = name
        self._avatar_data = None
        self._load_in_progress = False
        self._show_placeholder()

    def clear_avatar(self) -> None:
        """Очищает аватарку и показывает placeholder."""
        self._avatar_data = None
        self._load_in_progress = False
        self._show_placeholder()

    def load_avatar(self, base_url: str, access_key: str = "") -> None:
        """Загружает аватарку с сервера."""
        if self._load_in_progress or self._avatar_data is not None:
            return
        # Негативный кеш (v1.9.5): аватарки точно нет — не дёргаем сервер
        # на каждой пересборке списка (диалоги пересобираются каждые 2с).
        if AvatarCache().has_record(self._name):
            return

        from lib import client

        name_at_start = self._name
        self._load_in_progress = True
        self._bridge.run(
            lambda: client.fetch_avatar(base_url, name_at_start, access_key),
            on_success=lambda data: self._on_avatar_loaded(data, name_at_start),
            on_error=self._on_avatar_error,
        )

    def _on_avatar_loaded(self, data: bytes | None, name: str | None = None) -> None:
        """Обрабатывает успешную загрузку аватарки."""
        self._load_in_progress = False

        # v1.9.5: пока запрос летел, set_username мог сменить владельца
        # виджета — байты СТАРОГО юзера больше нельзя кешировать под НОВЫМ
        # именем (иначе чужая аватарка закрепится в глобальном кеше).
        if name is not None and name != self._name:
            if data is not None:
                AvatarCache().set(name, data)
            return

        if data is None:
            AvatarCache().set_missing(self._name)  # негативная запись
            return  # Нет аватарки на сервере

        self.set_avatar_data(data)

    def _on_avatar_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки аватарки."""
        self._load_in_progress = False
        # Оставляем placeholder, ничего не делаем

    def _round_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Создаёт круглую версию pixmap."""
        size = self._size
        rounded = QPixmap(size, size)
        rounded.fill(Qt.GlobalColor.transparent)

        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Маска - круг
        path = painter.clipPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)

        # Рисуем уменьшенное изображение по центру
        scaled = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

        # Центрируем
        x = (size - scaled.width()) // 2
        y = (size - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

        painter.end()
        return rounded

    def set_avatar_data(self, data: bytes) -> None:
        """Устанавливает аватарку напрямую (из кеша).

        Сначала смотрим в пиксмап-кеш: готовый круглый QPixmap для этой пары
        (name, size) переиспользуется всеми сообщениями этого юзера, декод
        и кругление выполняются один раз за жизнь приложения."""
        self._avatar_data = data

        cached = AvatarPixmapCache.get(self._name, self._size)
        if cached is not None:
            self.setPixmap(cached)
            self.avatar_loaded.emit()
            return

        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            rounded = self._round_pixmap(pixmap)
            AvatarPixmapCache.put(self._name, self._size, rounded)
            self.setPixmap(rounded)
            self.avatar_loaded.emit()
        else:
            self._show_placeholder()


class AvatarCache:
    """Глобальный кеш сырых аватарок (bytes)."""

    _instance = None
    _cache: dict[str, bytes] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get(self, name: str) -> bytes | None:
        # b"" — негативная запись («у юзера нет аватарки», v1.9.5):
        # раньше отсутствие кешировалось как «нет записи», и каждый
        # пересбор списка диалогов (раз в 2с!) стартовал НОВЫЙ HTTP-запрос
        # аватарки для каждого безаватарного юзера — бесконечный спам.
        data = self._cache.get(name)
        return data if data else None

    def has_record(self, name: str) -> bool:
        """Есть ли запись (в т.ч. негативная b"") — звать перед load_avatar."""
        return name in self._cache

    def set(self, name: str, data: bytes) -> None:
        changed = self._cache.get(name) != data
        self._cache[name] = data
        if changed and data:
            # Байты изменились (юзер перезалил аватар) — сбрасываем и
            # готовые пиксмапы, иначе все виджеты навсегда покажут старое.
            AvatarPixmapCache.invalidate(name)
        self._trim()

    def set_missing(self, name: str) -> None:
        """Негативная запись: аватарки нет (v1.9.5, см. get)."""
        self.set(name, b"")

    _CAP = 256  # мягкий LRU-кап: не растём вечно на сотнях знакомых

    def _trim(self) -> None:
        if len(self._cache) <= self._CAP:
            return
        # простейший LRU: пере-вставляем всё в свежем порядке (dict
        # сохраняет порядок вставки) и обрезаем хвост
        items = list(self._cache.items())[-self._CAP:]
        self._cache.clear()
        self._cache.update(items)

    def clear(self) -> None:
        self._cache.clear()
