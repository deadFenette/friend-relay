"""
MessageBubble — бабл сообщения (редизайн взаимодействий v9.5).

Без жёстких границ, только разница в elevation (surface_2 на surface_1).
Появление нового сообщения: slide up 8px + fade, 150ms.

Контекстное меню по правому клику — ВСЕ действия с сообщением
(ответить/копировать/реакции/закрепить/редактировать/удалить).
При наведении — одна круглая glass-кнопка у угла бабла: открывает
ReactionPopover (стеклянная пилюля с крупными эмодзи). Топорная
hover-панель с серыми кнопками из v9.4 удалена.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QContextMenuEvent, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib.constants import EVENT_KIND_FILE, FILE_PREVIEW_MAX_BYTES, FILE_PREVIEW_MAX_WIDTH
from lib.formatters import fmt_size
from qt_app.async_bridge import AsyncBridge
from qt_app.theme import DUR_FAST, PALETTE, RADIUS, RADIUS_LG

_PREVIEW_IMAGE_MAX_HEIGHT = 160
# Максимальная ширина текстовых баблов и вложенных карточек (читаемость:
# при 420px короткие фразы переносились на 2-3 строки даже на широких окнах)
_BUBBLE_MAX_WIDTH = 560
_PREVIEW_IMAGE_TIMEOUT = 6

# Высота чипа реакции. Радиус пилюли считается как _CHIP_H // 2 — QSS-радиус
# больше height/2 Qt рисует КВАДРАТНЫМИ углами (не клампит, в отличие от QPainter).
_CHIP_H = 28


def _decode_scaled_qimage(data: bytes | None, max_w: int, max_h: int) -> QImage | None:
    """Декодирует и масштабирует картинку В ФОНОВОМ потоке (оптимизация v9.3).

    QImage (в отличие от QPixmap) полностью реентерабелен: декод JPEG/PNG
    и smooth-масштаб можно безопасно делать вне GUI-потока. Раньше сюда
    приезжали сырые байты до 8 МБ, а QPixmap.loadFromData + ДВА smooth-масштаба
    выполнялись в GUI-потоке — каждый превью-контент замораживал интерфейс
    на десятки-сотни мс, а лента с несколькими картинками фризила на секунды.
    Теперь в GUI-поток летит уже маленький (<= max_w x max_h) QImage, и там
    остаётся только дешёвый QPixmap.fromImage."""
    if not data:
        return None
    img = QImage()
    if not img.loadFromData(data):
        return None
    if img.width() > max_w:
        img = img.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
    if img.height() > max_h:
        img = img.scaledToHeight(max_h, Qt.TransformationMode.SmoothTransformation)
    return img


def _fetch_preview_image_bytes(url: str) -> bytes | None:
    """Качает картинку og:image превью. Идёт по отдельному потоку через
    AsyncBridge (см. _load_preview_image ниже) - не блокирует UI."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_PREVIEW_IMAGE_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


class _ClickableLabel(QLabel):
    """QLabel, который умеет клик мышью (для превью-ссылки: открыть в браузере)."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MessageBubble(QWidget):
    # Сигналы для действий с сообщением
    reply_requested = Signal(dict)  # ev: dict с данными сообщения
    edit_requested = Signal(dict)  # ev: dict с данными сообщения
    delete_requested = Signal(int)  # seq: int
    copy_requested = Signal(str)  # text: str
    reaction_requested = Signal(int, str)  # seq: int, emoji: str
    pin_requested = Signal(int)  # seq: int
    download_requested = Signal(str, str)  # file_id: str, filename: str
    # v3: клик по in-app ссылке (friendrelay://...) — например, приглашение
    # в шахматы. URL отдаётся наверх, ChatScreen решает, что с ним делать.
    link_activated = Signal(str)
    # Лента выросла после вставки позднего контента (превью картинки/ссылки
    # догрузились и растолкали всё ниже) — экран подтягивает скролл к низу,
    # если пользователь был приклеен к низу (иначе он «остаётся» на месте,
    # а сообщения уезжают вверх за экран).
    content_grew = Signal()

    def __init__(
        self,
        text: str,
        is_me: bool,
        message_data: dict | None = None,
        base_url: str = "",
        access_key: str = "",
        my_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._text = text
        self._is_me = is_me
        self._message_data = message_data or {}
        self._base_url = base_url
        self._access_key = access_key
        self._my_name = my_name
        self._bridge = AsyncBridge()
        self._preview_image_label: QLabel | None = None
        self._file_preview_label: QLabel | None = None
        # Контейнер реакций — храним ссылку, чтобы обновлять без пересоздания bubble
        self._reactions_container: QWidget | None = None
        self._reactions_layout: QHBoxLayout | None = None
        self._reactions_row: QHBoxLayout | None = None

        bg = PALETTE.accent if is_me else PALETTE.surface_2
        fg = PALETTE.text_on_accent if is_me else PALETTE.text_primary

        self._top_offset = 0.0  # доп. отступ сверху, анимируется 8px -> 0

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 2, 0, 2)
        main_layout.setSpacing(0)
        self._main_layout = main_layout
        # React-кнопка (v9.5): одна круглая glass-кнопка у угла бабла,
        # создаётся ЛЕНИВО при первом наведении (у сотен баблов не должны
        # висеть сотни лишних кнопок — как и старый бар, только красивее)
        self._react_button: QPushButton | None = None
        self._react_fade: QPropertyAnimation | None = None

        # Контейнер для текста
        text_container = QWidget()
        text_layout = QHBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(self._render_text(text))
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        # v3: НЕ открываем ссылки в системном браузере автоматически —
        # friendrelay:// обрабатываем сами (signal link_activated →
        # ChatScreen открывает шахматы и джойнит). Внешние http(s) ссылки
        # по-прежнему идут в системный браузер через _on_link_clicked.
        self._label.setOpenExternalLinks(False)
        self._label.linkActivated.connect(self._on_link_clicked)
        self._label.setStyleSheet(f"""
            background: {bg};
            color: {fg};
            border-radius: {RADIUS_LG}px;
            padding: 9px 16px;
        """)
        # v3: ВАЖНО — TextSelectableByMouse alone гасит linkActivated
        # (клики идут на выделение текста, не на ссылки). Добавляем
        # LinksAccessibleByMouse чтобы клики по <a> снова работали.
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        # Отключаем стандартное контекстное меню QLabel
        self._label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Ширина «по содержимому»: QLabel с wordWrap сжимается до минимума,
        # из-за чего даже короткие фразы переносились на 2-3 строки. Задаём
        # минимум по фактической ширине текста — короткие баблы обнимают
        # текст, длинные переносятся на _BUBBLE_MAX_WIDTH.
        fm = self._label.fontMetrics()
        natural = max(
            (fm.horizontalAdvance(line) for line in self._text.split("\n")), default=0
        ) + 52  # padding 2*14 + запас на emoji/разметку
        if natural < _BUBBLE_MAX_WIDTH:
            # v1.9.5: кап снизу 240px. Раньше длинная однострочная фраза
            # ставила minWidth ~430+ — в узком окне лейбл не мог сжаться,
            # текст обрезался справа (горизонтальный скролл скрыт), вплоть
            # до нечитаемости. Короткие фразы по-прежнему «обнимают» текст.
            self._label.setMinimumWidth(min(natural, 240))

        if is_me:
            text_layout.addStretch(1)
            text_layout.addWidget(self._label)
        else:
            text_layout.addWidget(self._label)
            text_layout.addStretch(1)

        main_layout.addWidget(text_container)

        # Предпросмотр ссылки
        link_preview = self._message_data.get("link_preview")
        if link_preview:
            preview_container = QWidget()
            preview_layout = QVBoxLayout(preview_container)
            preview_layout.setContentsMargins(0, 4, 0, 0)
            preview_layout.setSpacing(4)

            title = link_preview.get("title")
            description = link_preview.get("description")
            url = link_preview.get("url")
            image_url = link_preview.get("image")

            if image_url:
                self._preview_image_label = QLabel()
                self._preview_image_label.setStyleSheet(f"""
                    background: {PALETTE.surface_3};
                    border-top-left-radius: {RADIUS}px;
                    border-top-right-radius: {RADIUS}px;
                """)
                self._preview_image_label.setMaximumWidth(FILE_PREVIEW_MAX_WIDTH)
                self._preview_image_label.setVisible(False)  # пока не загрузилось
                preview_layout.addWidget(self._preview_image_label)
                self._load_preview_image(image_url)

            if title:
                title_label = QLabel(title)
                title_label.setStyleSheet(f"""
                    background: {PALETTE.surface_3};
                    color: {PALETTE.text_primary};
                    border-radius: {RADIUS}px;
                    padding: 6px 10px;
                    font-weight: 600;
                """)
                title_label.setWordWrap(True)
                title_label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
                preview_layout.addWidget(title_label)

            if description:
                desc_label = QLabel(description)
                desc_label.setStyleSheet(f"""
                    background: {PALETTE.surface_3};
                    color: {PALETTE.text_secondary};
                    border-radius: {RADIUS}px;
                    padding: 4px 10px;
                    font-size: 12px;
                """)
                desc_label.setWordWrap(True)
                desc_label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
                preview_layout.addWidget(desc_label)

            if url:
                url_label = _ClickableLabel(url)
                url_label.setStyleSheet(f"""
                    background: {PALETTE.surface_3};
                    color: {PALETTE.accent};
                    border-radius: {RADIUS}px;
                    padding: 4px 10px;
                    font-size: 12px;
                """)
                url_label.setWordWrap(True)
                url_label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
                url_label.setCursor(Qt.CursorShape.PointingHandCursor)
                url_label.setToolTip("Открыть ссылку в браузере")
                url_label.clicked.connect(lambda u=url: QDesktopServices.openUrl(QUrl(u)))
                preview_layout.addWidget(url_label)

            if is_me:
                preview_container_layout = QHBoxLayout()
                preview_container_layout.addStretch(1)
                preview_container_layout.addWidget(preview_container)
                main_layout.addLayout(preview_container_layout)
            else:
                main_layout.addWidget(preview_container)

        # Вложение файла
        if self._message_data.get("kind") == EVENT_KIND_FILE:
            file_container = QWidget()
            file_layout = QVBoxLayout(file_container)
            file_layout.setContentsMargins(0, 4, 0, 0)
            file_layout.setSpacing(4)

            file_id = self._message_data.get("file_id")
            filename = self._message_data.get("name", "файл")
            file_size = self._message_data.get("size", 0)

            # Проверяем, является ли файл изображением
            image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
            is_image = any(filename.lower().endswith(ext) for ext in image_extensions)

            if is_image:
                # Для изображений показываем превью
                self._file_preview_label = _ClickableLabel()
                self._file_preview_label.setStyleSheet(f"""
                    background: {PALETTE.surface_3};
                    border-top-left-radius: {RADIUS}px;
                    border-top-right-radius: {RADIUS}px;
                """)
                self._file_preview_label.setMaximumWidth(FILE_PREVIEW_MAX_WIDTH)
                self._file_preview_label.setVisible(False)
                self._file_preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
                self._file_preview_label.setToolTip("Нажмите, чтобы открыть в полном размере")
                self._file_preview_label.clicked.connect(
                    lambda: self.download_requested.emit(file_id, filename)
                )
                file_layout.addWidget(self._file_preview_label)
                self._load_file_preview(file_id)

            # Имя файла и размер (кликабельная строка — скачивание)
            file_label_text = f"📎 {filename}"
            if file_size > 0:
                file_label_text = f"📎 {filename} ({fmt_size(file_size)})"

            clickable_file_label = _ClickableLabel(file_label_text)
            clickable_file_label.setStyleSheet(f"""
                background: {PALETTE.surface_3};
                color: {PALETTE.text_primary};
                border-radius: {RADIUS}px;
                padding: 6px 10px;
                font-weight: 600;
            """)
            clickable_file_label.setWordWrap(True)
            clickable_file_label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
            clickable_file_label.setCursor(Qt.CursorShape.PointingHandCursor)
            clickable_file_label.setToolTip("Нажмите, чтобы скачать файл")
            clickable_file_label.clicked.connect(
                lambda: self.download_requested.emit(file_id, filename)
            )
            file_layout.addWidget(clickable_file_label)

            if is_me:
                file_container_layout = QHBoxLayout()
                file_container_layout.addStretch(1)
                file_container_layout.addWidget(file_container)
                main_layout.addLayout(file_container_layout)
            else:
                main_layout.addWidget(file_container)

        # Реакции — отдельный метод, чтобы можно было обновлять без пересоздания bubble
        self._render_reactions(main_layout, is_me)

        # Индикатор закрепления
        if self._message_data.get("pinned", False):
            pin_label = QLabel("📌 Закреплено")
            pin_label.setStyleSheet(f"""
                background: {PALETTE.accent};
                color: {PALETTE.text_on_accent};
                border-radius: {RADIUS}px;
                padding: 4px 10px;
                font-size: 12px;
                font-weight: 600;
            """)
            pin_row = QHBoxLayout()
            pin_row.setContentsMargins(0, 4, 0, 0)
            if is_me:
                pin_row.addStretch(1)
                pin_row.addWidget(pin_label)
            else:
                pin_row.addWidget(pin_label)
                pin_row.addStretch(1)
            main_layout.addLayout(pin_row)

        # Контекстное меню (только правый клик)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    # ── Публичные акцессоры (ChatScreen читает их для поиска/реакций) ──

    @property
    def message_data(self) -> dict:
        """Данные сообщения (seq, reactions, pinned…). Изменяемые."""
        return self._message_data

    def display_text(self) -> str:
        """Отображаемый (уже расшифрованный) текст — по нему работает поиск."""
        return self._text

    # ── Реакции ──────────────────────────────────────────────────────

    def _render_reactions(self, main_layout: QVBoxLayout, is_me: bool) -> None:
        """Рисует блок реакций. Идемпотентно: при повторном вызове
        заменяет содержимое контейнера, не пересоздавая сам bubble.

        Реакции, где есть моё имя — подсвечены accent'ом (видно, что я уже
        отреагировал). При hover'e такой реакции — accent_hover, иначе
        обычная surface.
        """
        reactions = self._message_data.get("reactions", {})

        # Если контейнер уже существует — чистим его перед перерисовкой
        if self._reactions_layout is not None:
            while self._reactions_layout.count():
                item = self._reactions_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            # Если реакций больше нет — прячем контейнер
            if not reactions:
                if self._reactions_container is not None:
                    self._reactions_container.setVisible(False)
                return
            if self._reactions_container is not None:
                self._reactions_container.setVisible(True)
        elif reactions:
            # Первый раз — создаём контейнер
            self._reactions_container = QWidget()
            self._reactions_container.setObjectName("ReactionsContainer")
            self._reactions_layout = QHBoxLayout(self._reactions_container)
            self._reactions_layout.setContentsMargins(0, 4, 0, 0)
            self._reactions_layout.setSpacing(4)

            self._reactions_row = QHBoxLayout()
            if is_me:
                self._reactions_row.addStretch(1)
                self._reactions_row.addWidget(self._reactions_container)
            else:
                self._reactions_row.addWidget(self._reactions_container)
                self._reactions_row.addStretch(1)
            main_layout.addLayout(self._reactions_row)
        else:
            return  # нет реакций и не было

        # Рисуем pill'ы реакций (v9.5.1: ЧЕСТНЫЕ пилюли — фикс квадратных
        # углов из-за QSS radius 999; моя реакция — accent-кольцо, чужая — glass)
        for emoji, users in reactions.items():
            count = len(users)
            is_mine = self._my_name in users if self._my_name else False

            pill = QPushButton(f"{emoji}  {count}")
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setToolTip(", ".join(users))
            # ВАЖНО (баг v9.5): QSS border-radius больше height/2 Qt НЕ клампит —
            # рисует квадратные углы. Поэтому фиксируем высоту чипа и задаём
            # радиус ровно height/2 — гарантированная пилюля.
            pill.setFixedHeight(_CHIP_H)
            # Подсветка: моя реакция — accent-кольцо + лёгкий tint,
            # чужая — glass (как в Telegram: кольцо, а не заливка)
            if is_mine:
                bg = PALETTE.accent_dim
                fg = PALETTE.text_primary
                border = PALETTE.accent
                hover_bg = PALETTE.accent_dim
                hover_border = PALETTE.accent_hover
            else:
                bg = "rgba(255,255,255,0.06)"
                fg = PALETTE.text_secondary
                border = "rgba(255,255,255,0.10)"
                hover_bg = PALETTE.glass_bg_hover
                hover_border = "rgba(255,255,255,0.20)"

            pill.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    color: {fg};
                    border: 1px solid {border};
                    border-radius: {_CHIP_H // 2}px;
                    padding: 0 11px 0 9px;
                    font-size: 14px;
                }}
                QPushButton:hover {{
                    background: {hover_bg};
                    color: {PALETTE.text_primary};
                    border: 1px solid {hover_border};
                }}
                QPushButton:pressed {{
                    background: {PALETTE.surface_3};
                }}
            """)
            pill.clicked.connect(
                lambda checked=False, e=emoji: self.reaction_requested.emit(
                    self._message_data.get("seq"), e
                )
            )
            self._reactions_layout.addWidget(pill)

    def update_reactions(self, reactions: dict) -> None:
        """Локально обновляет реакции без перезагрузки ленты.

        Вызывается из chat_screen когда приходит delta reaction-события
        (или после успешной отправки своей реакции).
        """
        self._message_data["reactions"] = reactions
        # Ищем главный layout (первый QVBoxLayout в self.layout())
        layout = self.layout()
        if layout is None:
            return
        # _render_reactions хранит ссылку на свой контейнер — пересоздаём внутри
        self._render_reactions(layout, self._is_me)

    def _render_text(self, text: str) -> str:
        """Рендерит текст сообщения — markdown → HTML если есть разметка,
        иначе возвращает как есть (plain text QLabel быстрее).

        Использует lib/markdown_renderer.py. Безопасно от XSS: все HTML-символы
        экранируются до применения markdown.
        """
        if not text:
            return ""
        try:
            from lib.markdown_renderer import has_markdown, render_markdown
            if has_markdown(text):
                return render_markdown(text)
        except ImportError:
            pass
        return text

    def _on_link_clicked(self, url: str) -> None:
        """v3: обработчик клика по ссылке в тексте сообщения.

        Внутренние ссылки (friendrelay://) отдаём наверх через сигнал
        link_activated — ChatScreen решает, что делать (например, открыть
        диалог шахмат и присоединиться к матчу по коду).

        Внешние ссылки (http/https) открываем в системном браузере
        (как раньше, когда работал setOpenExternalLinks(True)).
        """
        if url.startswith("friendrelay://"):
            self.link_activated.emit(url)
            return
        # http(s) — в системный браузер
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    def _load_preview_image(self, image_url: str) -> None:
        """Тянет og:image превью-ссылки в фоне и подставляет, когда придёт -
        не блокирует отрисовку остальной ленты. Декод и масштаб — тоже в
        фоне (см. _decode_scaled_qimage): GUI-поток только вставляет готовый
        маленький пиксмап."""
        def fetch_and_decode() -> QImage | None:
            return _decode_scaled_qimage(
                _fetch_preview_image_bytes(image_url),
                FILE_PREVIEW_MAX_WIDTH,
                _PREVIEW_IMAGE_MAX_HEIGHT,
            )

        self._bridge.run(
            fetch_and_decode,
            on_success=lambda img: self._apply_preview_image(
                self._preview_image_label, img
            ),
            on_error=lambda e: None,
        )

    def _load_file_preview(self, file_id: str) -> None:
        """Загружает превью файла (изображения) с сервера.

        Транспорт - lib/client.fetch_file_bytes: идёт с X-Relay-Key/From
        (раньше тут был сырой urllib без ключа - на хосте с access_key
        превью файлов падало 403, а og:image из внешнего интернета - нет,
        поэтому для внешних ссылок urllib остаётся)."""
        if not self._base_url or not self._file_preview_label:
            return

        def fetch() -> bytes | None:
            from lib import client

            try:
                return client.fetch_file_bytes(
                    self._base_url, file_id, self._access_key,
                    max_bytes=FILE_PREVIEW_MAX_BYTES,
                )
            except Exception:
                return None

        self._bridge.run(
            lambda: _decode_scaled_qimage(
                fetch(), FILE_PREVIEW_MAX_WIDTH, _PREVIEW_IMAGE_MAX_HEIGHT
            ),
            on_success=lambda img: self._apply_preview_image(
                self._file_preview_label, img
            ),
            on_error=lambda e: None,
        )

    def _apply_preview_image(self, label: QLabel | None, image: QImage | None) -> None:
        """Вставляет ГОТОВОЕ (уже декодированное и уменьшенное в фоне)
        превью — общая точка для og:image и файловых превью.

        Бабл мог быть удалён (diff заменил его, лента перетриммирована,
        канал переключён) к моменту прихода ответа сети — вызов метода
        на удалённом C++ объекте в PySide6 бросает RuntimeError; гасим,
        чтобы поздние колбэки не роняли приложение."""
        if image is None or image.isNull():
            return
        try:
            if label is None:
                return
            label.setPixmap(QPixmap.fromImage(image))
            label.setVisible(True)
            # Высота бабла изменилась — пусть экран решает, надо ли
            # переподтянуть скролл (сам бабл не знает про «низ ленты»).
            self.content_grew.emit()
        except RuntimeError:
            return  # виджеты бабла уже уничтожены — превью никому не нужно

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """Перехватываем контекстное меню для всего виджета."""
        global_pos = event.globalPos()
        self._show_context_menu(global_pos)
        event.accept()

    def _show_context_menu(self, global_pos):
        """Показывает контекстное меню по ПКМ."""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border};
                border-radius: {RADIUS}px;
                padding: 4px;
            }}
            QMenu::item {{
                background: transparent;
                color: {PALETTE.text_primary};
                padding: 6px 12px;
                border-radius: 4px;
            }}
            QMenu::item:hover {{
                background: {PALETTE.surface_2};
            }}
            QMenu::item:disabled {{
                color: {PALETTE.text_secondary};
            }}
        """)

        copy_action = QAction("📋  Копировать", self)
        copy_action.triggered.connect(lambda: self.copy_requested.emit(self._text))
        menu.addAction(copy_action)

        reply_action = QAction("↩  Ответить", self)
        reply_action.triggered.connect(lambda: self.reply_requested.emit(self._message_data))
        menu.addAction(reply_action)

        # Подменю для реакций
        reaction_menu = QMenu("Реакции", self)
        reaction_menu.setStyleSheet(menu.styleSheet())

        common_emojis = ["👍", "❤️", "😂", "😮", "😢", "🔥"]
        for emoji in common_emojis:
            emoji_action = QAction(emoji, self)
            emoji_action.triggered.connect(
                lambda checked=False, e=emoji: self.reaction_requested.emit(
                    self._message_data.get("seq"), e
                )
            )
            reaction_menu.addAction(emoji_action)

        menu.addMenu(reaction_menu)

        pin_action = QAction("📌  Закрепить/Открепить", self)
        pin_action.triggered.connect(lambda: self.pin_requested.emit(self._message_data.get("seq")))
        menu.addAction(pin_action)

        if self._is_me:
            menu.addSeparator()

            edit_action = QAction("✏️  Редактировать", self)
            edit_action.triggered.connect(lambda: self.edit_requested.emit(self._message_data))
            menu.addAction(edit_action)

            delete_action = QAction("🗑  Удалить", self)
            delete_action.triggered.connect(
                lambda: self.delete_requested.emit(self._message_data.get("seq"))
            )
            menu.addAction(delete_action)

        # v1.9.5: меню удаляется после закрытия — раньше каждое ПКМ по
        # сообщению оставляло живой QMenu + ~8 QAction детьми бабла.
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        menu.exec(global_pos)

    # ── React-кнопка + ReactionPopover (редизайн v9.5) ─────────────
    # Вместо панели с серыми кнопками: одна круглая glass-кнопка у угла
    # бабла (со стороны свободного места), по клику — стеклянная пилюля
    # с крупными эмодзи. Все прочие действия — в меню по ПКМ.
    # Кнопка создаётся ЛЕНИВО при первом наведении (у сотен баблов не
    # должны висеть сотни лишних кнопок).

    _REACT_GLYPH = "😊"
    _REACT_SIZE = 30
    _REACT_GAP = 8

    def _my_reaction(self) -> str:
        """Эмодзи, которым я уже отреагировал (первое совпадение), либо ""."""
        if not self._my_name:
            return ""
        for emoji, users in (self._message_data.get("reactions", {}) or {}).items():
            if self._my_name in users:
                return emoji
        return ""

    def _ensure_react_button(self) -> None:
        if self._react_button is not None:
            return
        btn = QPushButton(self._REACT_GLYPH, self)
        btn.setObjectName("ReactButton")
        btn.setFixedSize(self._REACT_SIZE, self._REACT_SIZE)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("Реакция · ПКМ — все действия")
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.surface_2};
                color: {PALETTE.text_primary};
                border: 1px solid {PALETTE.border_strong};
                border-radius: {self._REACT_SIZE // 2}px;
                font-size: 14px;
                padding: 0;
            }}
            QPushButton:hover {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.accent};
            }}
            QPushButton:pressed {{
                background: {PALETTE.surface_4};
            }}
        """)
        btn.clicked.connect(self._open_reaction_popover)
        self._react_button = btn
        self._place_react_button()
        btn.hide()

    def _place_react_button(self) -> None:
        """Кнопка живёт РЯДОМ с баблом (на свободной стороне строки):
        у исходящих — слева от бабла, у входящих — справа. По вертикали —
        по центру бабла. Геометрию держим вручную: layout о ней не знает,
        поэтому перестановка не переукладывает ленту."""
        btn = self._react_button
        if btn is None:
            return
        try:
            r = self._label.geometry()
        except RuntimeError:
            return  # бабл уже уничтожен (перестройка ленты)
        if self._is_me:
            # v1.9.5: кнопка у СВОИХ сообщений раньше НАЕЗЖАЛА на текст
            # (r.left() у wordWrap-лейбла шире виджета → x уходил < 0 →
            # кнопка поверх первых символов), у ЧУЖИХ — улетала за край
            # виджета (x > width) и становилась недоступной. Клампим.
            x = r.left() - self._REACT_SIZE - self._REACT_GAP
            if x < self._REACT_GAP:
                # нет места слева — прячем кнопку у своих (кликабельность
                # важнее декоративности: реакцию можно поставить из ПКМ)
                btn.hide()
                return
        else:
            x = min(r.right() + self._REACT_GAP,
                    self.width() - self._REACT_SIZE - self._REACT_GAP)
        y = r.top() + max(0, (r.height() - self._REACT_SIZE) // 2)
        btn.move(max(0, x), y)

    def _show_react_button(self) -> None:
        try:
            self._ensure_react_button()
            btn = self._react_button
            if btn is None or btn.isVisible():
                return
            self._place_react_button()
            # Фейд появления — только если на самом бабле сейчас НЕТ другого
            # QGraphicsEffect (play_entrance вешает эффект на весь бабл на
            # первые 150мс; вложенные эффекты в Qt запрещены — известная
            # ловушка «one painter» из v9.4).
            if self.graphicsEffect() is None:
                eff = QGraphicsOpacityEffect(btn)
                btn.setGraphicsEffect(eff)
                anim = QPropertyAnimation(eff, b"opacity", btn)
                anim.setDuration(DUR_FAST)
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                anim.setStartValue(0.0)
                anim.setEndValue(1.0)
                self._react_fade = anim
                anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
                self._react_fade = None
            else:
                btn.setGraphicsEffect(None)  # без фейда — просто показать
            btn.show()
            btn.raise_()
        except RuntimeError:
            pass  # бабл уже уничтожен

    def _hide_react_button(self) -> None:
        btn = self._react_button
        if btn is None or not btn.isVisible():
            return
        try:
            btn.setGraphicsEffect(None)  # снять ДО hidden: вложенность эффектов
            btn.hide()
        except RuntimeError:
            pass

    def _open_reaction_popover(self) -> None:
        from qt_app.widgets.reaction_popover import ReactionPopover

        try:
            pop = ReactionPopover.open_for(self, my_reaction=self._my_reaction())
            pop.emoji_picked.connect(
                lambda emoji: self.reaction_requested.emit(
                    self._message_data.get("seq"), emoji
                )
            )
        except RuntimeError:
            pass  # бабл уже уничтожен

    def resizeEvent(self, event) -> None:
        # Бабл переразмерен (перенос текста, превью догрузилось) — держим
        # кнопку у актуального угла бабла.
        try:
            self._place_react_button()
        except RuntimeError:
            pass
        super().resizeEvent(event)

    def enterEvent(self, event) -> None:
        try:
            self._show_react_button()
        except RuntimeError:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # Курсор мог просто уйти на дочернюю кнопку — прячем только при
        # полном уходе с бабла.
        if not self.underMouse():
            try:
                self._hide_react_button()
            except RuntimeError:
                pass
        super().leaveEvent(event)

    # -- "slide up 8px" реализован через анимацию собственного top-margin,
    # а НЕ через pos()/move(): этот виджет живёт внутри QVBoxLayout родителя,
    # и лэйаут-менеджер каждый кадр сам расставляет детей по позициям -
    # прямая анимация pos() с ним конкурирует и уводит виджет не туда
    # (что и вскрылось на скриншоте теста). Анимировать contentsMargins
    # безопасно: это меняет sizeHint самого виджета, а не спорит за его
    # координаты с родительским layout.
    def _get_top_offset(self) -> float:
        return self._top_offset

    def _set_top_offset(self, value: float) -> None:
        self._top_offset = value
        m = self.layout().contentsMargins()
        self.layout().setContentsMargins(m.left(), 2 + int(value), m.right(), 2)

    topOffset = Property(float, _get_top_offset, _set_top_offset)

    def play_entrance(self) -> None:
        """slide up 8px + fade, 150ms — вызывать сразу после добавления в layout."""
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)

        self._fade = QPropertyAnimation(effect, b"opacity", self)
        self._fade.setDuration(150)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._offset_anim = QPropertyAnimation(self, b"topOffset", self)
        self._offset_anim.setDuration(150)
        self._offset_anim.setStartValue(8.0)
        self._offset_anim.setEndValue(0.0)
        self._offset_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade.finished.connect(lambda: self.setGraphicsEffect(None))
        self._fade.start()
        self._offset_anim.start()
