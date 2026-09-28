"""Страницы ленты чата: FeedPage + FeedPageManager (v9.3, вынесено из
chat_screen.py в v1.9.9 — экран стал тоньше, классы живут отдельно).

Каждому потоку (общий чат / канал) соответствует СВОЯ страница ленты
(FeedPage), живущая в QStackedWidget под управлением FeedPageManager.
Повторное открытие канала = мгновенный setCurrentWidget: ноль
пересоздания виджетов, ноль пересборки QSS, позиция скролла сохранена.
Раньше каждое переключение делало полный teardown + пересоздание ленты
(до MAX_RENDERED_ROWS строк по ~10-15 виджетов каждая) в GUI-потоке.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QScrollArea, QStackedWidget, QVBoxLayout, QWidget


class FeedPage:
    """Страница ленты одного потока: общий чат (key=None) или канал (key=имя).

    Владеет СВОИМ корневым виджетом, layout'ом и индексами отрисованного
    (message_widgets/containers/last_sender — прежние поля экрана, теперь
    на странице). Страница переживает переключения: пока она жива в кэше
    FeedPageManager, возврат в поток не пересоздаёт ни одного виджета.

    dirty=True — отрисовка устарела относительно хвоста в ядре (например,
    в фоновой канал пришли изменения, пока страница скрыта); при активации
    такая страница перерисовывается из свежего хвоста.
    """

    __slots__ = (
        "containers",
        "dirty",
        "key",
        "last_sender",
        "layout",
        "message_widgets",
        "root",
        "scroll_pos",
    )

    def __init__(self, key: str | None) -> None:
        self.key = key
        self.root = QWidget()
        self.root.setStyleSheet("background: transparent;")
        self.layout = QVBoxLayout(self.root)
        self.layout.setContentsMargins(24, 12, 24, 16)
        self.layout.setSpacing(6)
        self.layout.addStretch(1)
        # seq -> MessageBubble (для «показать» из панели пинов, реакций, поиска)
        self.message_widgets: dict[int, QWidget] = {}
        # Порядок строк ленты (container, seq) — для обрезки хвоста
        self.containers: list[tuple[QWidget, int | None]] = []
        # Последний отправитель — для группировки сообщений подряд
        self.last_sender: str | None = None
        # Сохранённая позиция скролла (восстанавливается при возврате)
        self.scroll_pos: int = 0
        # Отрисовка устарела — при активации перерисовать из хвоста ядра
        self.dirty: bool = False

    def clear(self) -> None:
        """Полностью очищает страницу: удаляет ВСЕ элементы layout'а (и виджеты,
        и накопленные растяжки) и оставляет ровно один хвостовой stretch.

        Раньше каждая перезагрузка ленты вызывала addStretch() поверх старого:
        растяжки накапливались, первая из них оказывалась ПЕРЕД сообщениями и
        лента переставала прижиматься к низу (сообщения зависали по центру),
        а после десятка переподключений layout зарастал мусором."""
        while self.layout.count():
            item = self.layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.layout.addStretch(1)
        self.message_widgets.clear()
        self.containers.clear()
        self.last_sender = None
        self.dirty = False


class FeedPageManager:
    """LRU-кэш страниц ленты поверх QStackedWidget.

    Ключ страницы: None = общий чат, str = имя канала.

    Зачем: переключение каналов раньше пересобирало ленту с нуля — сотни
    виджетов с QSS за один клик. Теперь активная смена потока — это
    setCurrentWidget (константная операция) + восстановление скролла;
    пересборка происходит ТОЛЬКО при первом открытии потока или когда его
    страница помечена dirty. Страницы сверх MAX_PAGES вытесняются по LRU
    (кроме активной), чтобы память не росла с числом каналов.
    """

    MAX_PAGES = 6  # общий чат + до 5 каналов одновременно в виджетах

    def __init__(self, scroll: QScrollArea) -> None:
        self._scroll = scroll
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        scroll.setWidget(self._stack)
        self._pages: dict[str | None, FeedPage] = {}
        # LRU-порядок ключей: последний элемент — самый свежий
        self._order: list[str | None] = []
        self._current: FeedPage | None = None

    # -- доступ ---------------------------------------------------------

    def current(self) -> FeedPage | None:
        return self._current

    def page(self, key: str | None) -> FeedPage | None:
        return self._pages.get(key)

    # -- жизненный цикл --------------------------------------------------

    def activate(self, key: str | None) -> FeedPage:
        """Делает страницу потока активной (создав при необходимости),
        сохраняя позицию скролла уходящей страницы и восстанавливая позицию
        открываемой (отложенно — после пересчёта layout'а)."""
        page = self._pages.get(key)
        if page is None:
            page = self._create_page(key)
        self._touch(key)

        if self._current is page:
            return page

        if self._current is not None:
            self._current.scroll_pos = self._scroll.verticalScrollBar().value()

        self._current = page
        self._stack.setCurrentWidget(page.root)

        saved = page.scroll_pos
        if saved:
            bar = self._scroll.verticalScrollBar()
            QTimer.singleShot(0, lambda: bar.setValue(saved))
        return page

    def evict(self, key: str | None) -> None:
        """Уничтожает страницу (если она не активная) и освобождает её виджеты."""
        page = self._pages.get(key)
        if page is None or page is self._current:
            return
        self._pages.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        self._stack.removeWidget(page.root)
        page.root.deleteLater()

    def evict_all(self) -> None:
        """Уничтожает ВСЕ страницы (отключение от сервера) и активирует
        свежую пустую общую — чтобы делегирующие свойства экрана всегда
        имели валидную текущую страницу."""
        for page in self._pages.values():
            self._stack.removeWidget(page.root)
            page.root.deleteLater()
        self._pages.clear()
        self._order.clear()
        self._current = None
        self.activate(None)

    # -- внутреннее -------------------------------------------------------

    def _create_page(self, key: str | None) -> FeedPage:
        page = FeedPage(key)
        self._pages[key] = page
        self._order.append(key)
        self._stack.addWidget(page.root)
        self._evict_overflow()
        return page

    def _touch(self, key: str | None) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def _evict_overflow(self) -> None:
        # Сентинел обязателен: ключ общего чата — это сам None, и дефолт
        # next(..., None) конфликтовал бы с ним («жертва не найдена» vs
        # «жертва — общий чат»), из-за чего вытеснение молча не работало.
        sentinel = object()
        while len(self._pages) > self.MAX_PAGES:
            victim_key = next(
                (k for k in self._order if self._pages.get(k) is not self._current),
                sentinel,
            )
            if victim_key is sentinel:
                break
            self.evict(victim_key)
