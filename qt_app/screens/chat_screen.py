"""
Экран чата — аналог ChatUIMixin из lib/chat_ui.py.

Реализует общий чат с:
- Отображением сообщений с группировкой
- Отправкой текстовых сообщений
- Асинхронной загрузкой через AsyncBridge
- Индикатором онлайн пользователей

Оптимизация v9.3 (переключение каналов без лагов):
- Каждому потоку (общий чат / канал) соответствует СВОЯ страница ленты
  (FeedPage), живущая в QStackedWidget под управлением FeedPageManager.
  Повторное открытие канала = мгновенный setCurrentWidget: ноль
  пересоздания виджетов, ноль пересборки QSS, позиция скролла сохранена.
  Раньше каждое переключение делало полный teardown + пересоздание ленты
  (до MAX_RENDERED_ROWS строк по ~10-15 виджетов каждая) в GUI-потоке.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.client_core.chat_session import (
    ChatSession,
    diff_tail,
    interpret_dispatch,
    merge_delta_into,
)
from lib.constants import EVENT_KIND_FILE, MAX_RENDERED_ROWS
from qt_app.async_bridge import AsyncBridge
from qt_app.screens.chat_ui_builder import build_chat_ui, glass_btn
from qt_app.screens.feed_pages import (  # noqa: F401 - реэкспорт для совместимости
    FeedPage,
    FeedPageManager,
)
from qt_app.theme import PALETTE, RADIUS
from qt_app.utils.file_handler import FileHandler
from qt_app.widgets.avatar_widget import AvatarCache
from qt_app.widgets.channel_pill import ChannelPill
from qt_app.widgets.online_user_chip import OnlineUserChip
from qt_app.widgets.paste_image_line_edit import save_clipboard_image_to_temp
from qt_app.widgets.skeleton import SkeletonFeed
from qt_app.widgets.toast import Toast
from qt_app.widgets.voice_channel_dialog import VoiceChannelDialog

# Экран чата — тонкий координатор: построение баблов вынесено в фабрику
# qt_app/screens/chat_message_renderer.py (единственная точка рендеринга для
# общего чата И каналов), ПОЛИТИКА (поллинг, каналы, типинг, дешифровка,
# разбор dispatch) — в ядро lib/client_core/chat_session.py без Qt, РАЗМЕТКА
# — в qt_app/screens/chat_ui_builder.py (build_chat_ui), страницы ленты —
# в qt_app/screens/feed_pages.py, диалог пинов — в widgets/pinned_dialog.py.


class ChatScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()
        self._avatar_cache = AvatarCache()
        self._file_handler = FileHandler(self)
        self._file_handler.download_completed.connect(self._on_file_download_completed)
        self._file_handler.download_failed.connect(self._on_file_download_failed)

        self._base_url = ""
        self._my_name = ""
        self._access_key = ""
        self._connected = False

        # Шифрование: ключ/соль живут в client (application-слой), вьюха
        # держит только флаг "шифровать ли исходящие сообщения".
        self._encryption_enabled = False

        # Последний отрисованный список онлайна — чтобы не пересоздавать чипы
        # каждый тик поллинга без изменений (мерцание + лишний GC).
        self._rendered_online: list[str] = []
        self._pinned_messages: list[dict] = []

        # Каналы: список для pill'ов — вьюха; открытый канал и хвосты — ядро.
        self._channels: list[dict] = []

        # Для редактирования сообщений
        self._editing_seq: int | None = None
        self._editing_original_text = ""

        # Для ответов
        self._reply_to: dict | None = None

        # Живой поллинг: таймер — у вьюхи, политика (in-flight, kick,
        # last_seq) — в ядре (см. ниже).
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._on_poll_tick)

        # ЯДРО экрана (lib/client_core, БЕЗ Qt): live-поллинг, открытый канал
        # и хвосты, «печатает», дешифровка, разбор ответов ботов. AsyncBridge
        # внедрён как runner — колбэки ядра приезжают уже в GUI-потоке,
        # экран только рисует.
        self._session = ChatSession(runner=self._bridge.run)
        self._session.on_poll_result = self._on_poll_result
        self._session.on_initial_result = self._on_messages_loaded
        self._session.on_initial_error = self._on_messages_error
        self._session.on_channel_loaded = self._on_channel_messages_loaded
        self._session.on_channel_load_error = self._on_channel_messages_error
        self._session.on_channel_diff = self._apply_channel_diff
        self._session.on_polling_stopped = self._poll_timer.stop
        # v1.9.5: детектор обрыва связи из ядра (5 ошибок поллинга подряд)
        self._session.on_poll_failed = self._on_poll_failed
        self._session.on_poll_recovered = self._on_poll_recovered

        # Счётчик непрочитанных, пока лента проскроллена вверх
        self._unread_count = 0
        # «Приклеен к низу»: юзер у нижнего края — новые сообщения
        # подтягивают скролл за ним. Сбрасывается при прокрутке вверх.
        self._stick_bottom = True
        # Какой поток сейчас отрисован на экране (None = общий чат,
        # строка = канал). Дифф-обновления применяются только когда
        # приходящий снапшот принадлежит тому же потоку.
        self._rendered_channel: str | None = None
        # Состояние счётчика онлайна для guard'а перерисовки (см.
        # _update_online_and_typing): не дёргаем setText/setStyleSheet
        # каждый тик поллинга при неизменных данных (лишний ре-полиш QSS).
        self._online_state: tuple[int, bool] | None = None  # (count, is_green)

        self._setup_ui()

    # -- делегирование к текущей странице ленты (FeedPage) ----------------
    # Раньше это были обычные атрибуты экрана: единственная лента.
    # Теперь каждый поток (общий чат/канал) держит свои индексы в своей
    # странице (см. FeedPageManager), а экран проецирует их на текущую.

    def _cur_page(self) -> FeedPage:
        """Текущая страница ленты (с автопочинкой, если кэш пуст)."""
        return self._pages.current() or self._pages.activate(None)

    @property
    def _message_widgets(self) -> dict[int, QWidget]:
        return self._cur_page().message_widgets

    @property
    def _feed_containers(self) -> list[tuple[QWidget, int | None]]:
        return self._cur_page().containers

    @property
    def _feed_layout(self) -> QVBoxLayout:
        return self._cur_page().layout

    @property
    def _last_sender(self) -> str | None:
        return self._cur_page().last_sender

    @_last_sender.setter
    def _last_sender(self, value: str | None) -> None:
        self._cur_page().last_sender = value

    def _setup_ui(self) -> None:
        """Вся разметка экрана собирается в qt_app/screens/chat_ui_builder.py
        (build_chat_ui): панель каналов, топ-стрип, лента, инпут-строка.
        Экран остаётся тонким координатором событий."""
        build_chat_ui(self)

    def _glass_btn(
        self,
        text: str,
        tooltip: str = "",
        width: int | None = None,
        height: int = 32,
        radius: int = RADIUS,
        font_size: int = 14,
        padding: str = "0 12px",
    ) -> QPushButton:
        """Фабрика glass-кнопок — реализация в chat_ui_builder.glass_btn."""
        return glass_btn(
            text, tooltip=tooltip, width=width, height=height,
            radius=radius, font_size=font_size, padding=padding)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_scroll_down_btn()

    def _position_scroll_down_btn(self) -> None:
        margin = 16
        x = self._scroll.width() - self._scroll_down_btn.width() - margin
        y = self._scroll.height() - self._scroll_down_btn.height() - margin
        self._scroll_down_btn.move(max(0, x), max(0, y))
        self._scroll_down_btn.raise_()

    def _clear_feed(self) -> None:
        """Полностью очищает ТЕКУЩУЮ страницу ленты и сбрасывает
        экранное состояние (непрочитанные, приклейку к низу).
        Содержимое страницы чистит FeedPage.clear — там же инвариант
        «ровно один хвостовой stretch» (см. комментарий там)."""
        self._cur_page().clear()
        self._unread_count = 0
        # Пустая лента = «у низа» (иначе первый батч не притянется вниз,
        # если сигнал scrollbar'а не прилетит)
        self._stick_bottom = True
        self._scroll_down_btn.setVisible(False)

    def _show_empty_state(self) -> None:
        self._set_status_text("Не подключено")
        empty = QLabel("Подключись к серверу в настройках")
        empty.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 13px;")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._feed_layout.insertWidget(self._feed_layout.count() - 1, empty)

    def _show_feed_placeholder(self, text: str, skeleton: bool = False) -> None:
        """Плейсхолдер в ленте (ошибка / «Нет сообщений») — чтобы экран
        не пустовал. Для состояний «Загрузка…» вместо текста показываем
        shimmer-скелетон (ТЗ раздел 8: «Skeleton screens, not spinners»)."""
        if skeleton:
            self._feed_layout.insertWidget(self._feed_layout.count() - 1, SkeletonFeed())
            return
        empty = QLabel(text)
        empty.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 13px;")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._feed_layout.insertWidget(self._feed_layout.count() - 1, empty)

    def _render_feed_batch(self, messages: list[dict], channel: str | None) -> None:
        """Полная (пере)отрисовка ленты ОДНИМ батчем — при первом открытии
        потока или когда его страница устарела (dirty). Живые обновления
        идут диффом, это НЕ путь сюда; повторное открытие живой страницы
        тоже не попадает сюда (мгновенный setCurrentWidget).

        Два антифриза:
          - анимации появления отключены: десятки QGraphicsOpacityEffect
            параллельно (по одному на бабл) давали фриз на слабых машинах;
          - перерисовка приостановлена на время вставки: N insertWidget
            без неё = N промежуточных релайаутов + пейнтов.
        """
        self._rendered_channel = channel
        self._pages.activate(channel)
        self._clear_feed()

        if not messages:
            self._show_feed_placeholder(
                "Нет сообщений" if channel is None else f"Нет сообщений в канале '{channel}'"
            )
            return

        self.setUpdatesEnabled(False)
        try:
            for ev in messages:
                self._render_message(ev, animate=False)
        finally:
            self.setUpdatesEnabled(True)

        # Автопрокрутка к низу при открытии потока — после пересчёта layout'а
        self._stick_bottom = True
        self._scroll_after_layout()

    def _apply_full_snapshot_diff(self, new_msgs: list[dict]) -> bool:
        """Снапшот этого же потока уже отрисован — применяем разницу точечно
        вместо пересборки (нет мигания и скачка скролла).

        «Старое» — данные уже отрисованных баблов (в порядке ленты), сравнение
        по seq/содержимому делает diff_tail из ядра. False — если диффить
        нечего (пустая отрисовка), вызывающий сделает полный батч."""
        if not self._feed_containers:
            return False
        old = [
            self._message_widgets[seq].message_data
            for (_c, seq) in self._feed_containers
            if seq is not None and seq in self._message_widgets
        ]
        diff = diff_tail(old, new_msgs)
        if diff.is_empty():
            return True
        self._apply_channel_diff(self._rendered_channel, diff)
        return True

    def _set_status_text(self, text: str, color: str | None = None) -> None:
        """Показывает статус/ошибку в топ-стрипе вместо чипов.

        Используется когда нет подключения, при ошибках каналов/файлов и т.п.
        Скрывает контейнер чипов, показывает _online_label с текстом.
        При следующем успешном _update_online_and_typing чипы вернутся,
        а label снова спрячется.
        """
        self._online_chips_scroll.setVisible(False)
        self._online_label.setVisible(True)
        self._online_label.setText(text)
        if color:
            self._online_label.setStyleSheet(
                f"color: {color}; font-size: 12px; padding: 4px 0;"
            )
        else:
            self._online_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px; padding: 4px 0;"
            )

    # -- каналы -----------------------------------------------------------

    def _load_channels(self) -> None:
        """Загружает список каналов."""
        if not self._connected:
            return

        self._bridge.run(
            lambda: client.get_channels(self._base_url, self._my_name, self._access_key),
            on_success=self._on_channels_loaded,
            on_error=self._on_channels_error,
        )

    def _on_channels_loaded(self, channels: list[dict]) -> None:
        """Обрабатывает загруженный список каналов — рендерит pill'ы."""
        self._channels = channels

        # Очищаем старые pills
        while self._channels_list_layout.count():
            item = self._channels_list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # "Общий чат"
        general_pill = ChannelPill(
            icon="💬",
            label="Общий чат",
            data=None,
            parent=self,
            deletable=False,
        )
        general_pill.clicked.connect(self._on_channel_pill_clicked)
        general_pill.set_active(self._session.current_channel is None)
        self._channels_list_layout.addWidget(general_pill)

        # Каналы — каждый pill удаляем, если пользователь создатель
        for channel in channels:
            name = channel.get("name", "Без названия")
            creator = channel.get("creator", "")
            is_creator = (creator == self._my_name) if self._my_name else False
            pill = ChannelPill(
                icon="📢",
                label=name,
                data=name,
                parent=self,
                deletable=is_creator,
            )
            pill.clicked.connect(self._on_channel_pill_clicked)
            pill.delete_requested.connect(self._on_channel_delete_requested)
            pill.set_active(self._session.current_channel == name)
            self._channels_list_layout.addWidget(pill)

        self._channels_list_layout.addStretch(1)

    def _on_channel_pill_clicked(self, data) -> None:
        """Обрабатывает клик по pill'у канала.

        Переключение должно ощущаться мгновенно. Пути (по убыванию скорости):
          1. Живая страница этого потока в кэше — setCurrentWidget + сетевая
             догрузка диффом (ноль пересозданий виджетов);
          2. Кэш хвоста в ядре — батч-рендер из памяти (сеть догонит диффом);
          3. Ничего нет — плейсхолдер «Загрузка…» вместо пустого экрана.
        Раньше каждый клик пересобирал ленту с нуля (сотни виджетов с QSS) —
        на каналах с историей это ощущалось как «тупит при переключении»,
        а до ответа сети вообще висела лента ПРЕДЫДУЩЕГО канала.
        """
        channel_name = data  # None = общий чат
        self._session.set_channel(channel_name)
        self._update_pill_activity(channel_name)
        self._show_feed_for(channel_name)
        # Свежий хвост с сервера: ядро само поймёт (канал/общий), ответ
        # ляжет диффом на активную страницу или пометит скрытую dirty.
        self._load_messages()

    # -- Публичный API для Command Palette (Ctrl+K) ------------------------

    def channel_names(self) -> list[str]:
        """Имена каналов для быстрого перехода из палитры команд."""
        return [c.get("name", "") for c in self._channels if c.get("name")]

    def jump_to_channel(self, channel: str | None) -> None:
        """Программный переход в канал (None — общий чат)."""
        self._on_channel_pill_clicked(channel)

    def _update_pill_activity(self, channel_name: str | None) -> None:
        """Обновляет подсветку активного pill'а в панели каналов."""
        for i in range(self._channels_list_layout.count()):
            item = self._channels_list_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, ChannelPill):
                w.set_active(w.data() == channel_name)

    def _show_feed_for(self, channel_name: str | None) -> None:
        """Мгновенно показывает ленту потока (см. _on_channel_pill_clicked)."""
        page = self._pages.page(channel_name)
        if page is not None and page.containers and not page.dirty:
            # ГОРЯЧИЙ путь: страница уже отрисована и актуальна
            self._rendered_channel = channel_name
            self._pages.activate(channel_name)  # save/restore scroll внутри
            return

        # Страницы нет или она устарела — перерисовываем из кэша ядра
        if channel_name is None:
            tail = self._session.general_tail
            renderable = [ev for ev in tail if ev.get("kind") in ("text", EVENT_KIND_FILE)]
            if tail:
                self._render_feed_batch(renderable, None)
            else:
                self._rendered_channel = None
                self._pages.activate(None)
                self._clear_feed()
                self._show_feed_placeholder("Загрузка…", skeleton=True)
        else:
            tail = self._session.channel_tail(channel_name)
            if tail:
                self._render_feed_batch(tail, channel_name)
            else:
                self._rendered_channel = channel_name
                self._pages.activate(channel_name)
                self._clear_feed()
                self._show_feed_placeholder("Загрузка канала…", skeleton=True)

    def _on_channels_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки каналов."""
        self._set_status_text(f"Ошибка загрузки каналов: {error}", color=PALETTE.danger)

    def _on_create_channel(self) -> None:
        """Обрабатывает создание нового канала."""
        if not self._connected:
            return

        channel_name, ok = QInputDialog.getText(
            self, "Создать канал", "Название канала:", QLineEdit.EchoMode.Normal
        )

        if ok and channel_name.strip():
            self._bridge.run(
                lambda: client.create_channel(
                    self._base_url, self._my_name, channel_name.strip(), self._access_key
                ),
                on_success=self._on_channel_created,
                on_error=self._on_channel_create_error,
            )

    def _on_channel_created(self, result: dict) -> None:
        """Обрабатывает успешное создание канала."""
        self._load_channels()
        self._set_status_text(f"✓ Канал '{result.get('name')}' создан", color=PALETTE.success)

    def _on_channel_create_error(self, error: Exception) -> None:
        """Обрабатывает ошибку создания канала."""
        self._set_status_text(f"Ошибка: {error}", color=PALETTE.danger)

    def _on_channel_delete_requested(self, channel_name) -> None:
        """Обрабатывает запрос на удаление канала (через правый клик на pill'е)."""
        if not self._connected or not channel_name:
            return

        # Подтверждение с предупреждением об удалении всех сообщений
        reply = QMessageBox.question(
            self,
            "Удалить канал",
            f"Удалить канал '{channel_name}'?\n\n"
            f"Все сообщения канала будут потеряны. Это действие нельзя отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._bridge.run(
            lambda: client.delete_channel(
                self._base_url, self._my_name, channel_name, self._access_key
            ),
            on_success=lambda r: self._on_channel_deleted(channel_name, r),
            on_error=self._on_channel_delete_error,
        )

    def _on_channel_deleted(self, channel_name: str, result: dict) -> None:
        """Обрабатывает успешное удаление канала."""
        # Если удалили текущий активный канал — переключаемся на общий
        if self._session.current_channel == channel_name:
            self._session.set_channel(None)
            self._load_messages()
        self._load_channels()
        self._set_status_text(f"🗑 Канал '{channel_name}' удалён", color=PALETTE.success)

    def _on_channel_delete_error(self, error: Exception) -> None:
        """Обрабатывает ошибку удаления канала."""
        self._set_status_text(f"Ошибка удаления: {error}", color=PALETTE.danger)

    def _load_channel_messages(self, channel_name: str) -> None:
        """Загружает сообщения канала (сеть, хвост и устаревшие ответы — в ядре)."""
        self._session.load_channel(channel_name)

    def _on_channel_messages_loaded(self, channel_name: str, messages: list[dict]) -> None:
        """Хвост канала загружен (кэш уже сохранён в ядре).

        Если на экране уже отрисован ЭТОТ канал (мгновенный кэш-рендер при
        клике) — применяем разницу диффом: нет мигания, нет скачка скролла.
        Пока запрос летел, поток могли переключить: тогда ленту чужого
        потока НЕ трогаем, а страница канала (если живёт в кэше) помечается
        dirty — при возврате она перерисуется из свежего хвоста.
        """
        if self._rendered_channel != channel_name:
            stale = self._pages.page(channel_name)
            if stale is not None:
                stale.dirty = True
            return
        if self._feed_containers:
            if self._apply_full_snapshot_diff(messages):
                return
        self._render_feed_batch(messages, channel_name)

    def _on_channel_messages_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки сообщений канала.

        Если лента так и не появилась — заменяем «Загрузка…» на ошибку
        прямо в ленте (пустое место + статус наверху юзер не связывает)."""
        self._set_status_text(f"Ошибка загрузки сообщений: {error}", color=PALETTE.danger)
        if not self._feed_containers:
            self._clear_feed()
            self._show_feed_placeholder("Не удалось загрузить — проверь подключение")

    def set_connection(self, base_url: str, my_name: str, access_key: str, connected: bool) -> None:
        """Устанавливает параметры подключения."""
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key
        self._connected = connected
        # Ядро живёт своими копиями параметров — все сетевые запросы
        # выполняются через него.
        self._session.configure(base_url, my_name, access_key)
        self._session.set_connected(connected)

        if connected:
            self._load_channels()
            self._load_messages()
            self._start_live_polling()
        else:
            self._stop_live_polling()
            self._clear_all()

    def set_encryption_settings(self, enabled: bool, secret_key: str,
                                 salt: str = "") -> None:
        """Принимает настройки шифрования + per-installation соль.

        Владелец CryptoManager — lib.client (application-слой): экран только
        транслирует настройки туда и держит флаг шифрования. Salt нужна для
        вывода AES-ключа через PBKDF2. Без соли все установки с одним
        паролем получали бы одинаковый ключ — теперь у каждой свой.
        """
        self._encryption_enabled = enabled
        if salt:
            client.set_crypto_salt(salt)
        client.set_crypto_secret_key(secret_key if enabled and secret_key else "")

    def _decrypt_message_text(self, msg: dict) -> str:
        """Расшифровка — в ядре (оно же умеет плейсхолдер при неудаче)."""
        return self._session.decrypt_text(msg)

    def _clear_all(self) -> None:
        """Очищает всё при отключении."""
        self._last_sender = None
        self._pinned_messages = []
        self._pinned_btn.setVisible(False)
        self._rendered_channel = None
        # Ядро: last_seq, типинг, открытый канал, хвосты каналов
        self._session.reset()
        self._update_typing_indicator()
        self._rendered_online = []
        if self._search_bar.isVisible():
            self._toggle_search_bar()

        # Канальные pill'ы — вьюшечное состояние
        self._channels = []
        # Очищаем список pills
        while self._channels_list_layout.count():
            item = self._channels_list_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        self._channels_list_layout.addStretch(1)

        # Все страницы ленты уничтожаются (виджеты и индексы), активируется
        # свежая пустая общая — иначе застарелые сообщения оставались бы
        # в виджетах до следующего рендера.
        self._pages.evict_all()
        self._clear_feed()
        self._show_empty_state()

    def _load_messages(self) -> None:
        """Полная перезагрузка ТЕКУЩЕЙ ленты (первое подключение, смена канала,
        возврат в общий чат). Сеть и состояние — в ядре: в режиме канала оно
        само перезагрузит канал, иначе общий tail. Живые обновления после неё
        идут инкрементально через poll-тики — действия пользователя
        (send/delete/…) НИКОГДА не должны вызывать полную пересборку, для
        этого есть session.after_message_action()."""
        if not self._connected:
            return

        if self._session.current_channel is not None:
            # В режиме канала «перезагрузить ленту» = перезагрузить канал.
            # Раньше общий tail нечаянно заменял ленту канала общими сообщениями.
            self._session.load_initial()
            return

        self._session.load_initial()
        self._refresh_pinned()

    def _on_messages_loaded(self, result: dict) -> None:
        """Полный снапшот общей ленты (первое подключение / возврат из канала).

        Если общий чат уже отрисован (кэш из ядра) — разница применяется
        диффом, без полной пересборки и мигания."""
        events = result.get("events", [])

        self._update_online_and_typing(result)

        if not events:
            self._rendered_channel = None
            self._clear_feed()
            self._show_feed_placeholder("Нет сообщений")
            return

        renderable = [
            ev for ev in events if ev.get("kind") in ("text", EVENT_KIND_FILE)
        ]
        if self._rendered_channel is None and self._feed_containers:
            if self._apply_full_snapshot_diff(renderable):
                return
        self._render_feed_batch(renderable, None)

    def _on_messages_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки сообщений."""
        self._set_status_text(f"Ошибка: {error}", color=PALETTE.danger)

    def _update_online_and_typing(self, result: dict) -> None:
        online = result.get("online", [])

        # Возвращаем видимость чипам (если до этого показывали статус/ошибку)
        self._online_label.setVisible(False)
        self._online_chips_scroll.setVisible(True)

        # Счётчик слева обновляем ТОЛЬКО при изменении: setText одинакового
        # текста дёшев, а setStyleSheet каждый тик вызывал полный ре-полиш
        # стиля (пересборка QSS) раз в секунду без видимой причины.
        new_state = (len(online), bool(online))
        if new_state != self._online_state:
            self._online_state = new_state
            count = new_state[0]
            self._online_count_label.setText(f"● {count}")
            if online:
                self._online_count_label.setStyleSheet(
                    f"color: {PALETTE.success}; font-size: 12px; font-weight: 600;"
                )
            else:
                self._online_count_label.setStyleSheet(
                    f"color: {PALETTE.text_secondary}; font-size: 12px;"
                )

        # Перерисовываем чипы ТОЛЬКО если состав онлайна изменился: раньше
        # каждые 2 секунды все чипы уничтожались и создавались заново —
        # лишняя нагрузка, мерцание и сброс hover'а под курсором.
        # Сортировка (свои первыми, далее по алфавиту) — в ядре.
        sorted_online = self._session.sort_online(online)
        if sorted_online != self._rendered_online:
            self._rendered_online = sorted_online
            # Сначала чистим старые (оставляем stretch в конце)
            while self._online_chips_layout.count() > 1:
                item = self._online_chips_layout.takeAt(0)
                w = item.widget() if item else None
                if w is not None:
                    w.deleteLater()
            for name in sorted_online:
                chip = OnlineUserChip(name, parent=self, is_me=(name == self._my_name))
                self._online_chips_layout.insertWidget(
                    self._online_chips_layout.count() - 1,
                    chip,  # вставляем перед stretch
                )

        for name in result.get("typing", []):
            self.handle_typing_event(name)

    # -- Live-обновления (поллинг новых событий без полной перезагрузки) --

    def _start_live_polling(self) -> None:
        if not self._session.is_polling:
            self._session.start_polling()
            # 1000 мс: сообщения «долетают» за полсекунды в среднем (раньше
            # было 2000 мс — и это чувствовалось как «долго летят»).
            self._poll_timer.start(1000)

    def _stop_live_polling(self) -> None:
        if self._session.is_polling:
            self._session.stop_polling()
            self._poll_timer.stop()

    def _on_poll_tick(self) -> None:
        # Защита от наложения запросов, last_seq и добор отложенного kick —
        # в ядре; при обрыве связи оно само глушит наш таймер.
        self._session.tick()

    def _on_poll_failed(self, error: Exception) -> None:
        """Хост не отвечает несколько тиков подряд (детектор в ядре,
        v1.9.5): раньше обрыв НЕ детектировался вообще — статус висел
        «подключено», онлайн-чипы и «печатает» замирали, лечилось только
        ручным переподключением."""
        self._set_status_text("⚠ Нет связи с хостом — восстановление…",
                              color=PALETTE.warning)

    def _on_poll_recovered(self) -> None:
        """Связь вернулась после обрыва — убираем предупреждение."""
        self._set_status_text("✓ Связь восстановлена", color=PALETTE.success)

    def _on_poll_result(self, result: dict) -> None:
        """Дельта-ответ ядра: обновляем онлайн/типинг и дорисовываем
        только затронутые баблы — лента не мигает целиком.

        Онлайн обновляется ровно ОДИН раз: раньше _update_online_and_typing
        вызывался и здесь, и внутри _on_incremental_events — двойная работа
        (сортировка, сравнение, guard'ы) на каждый тик поллинга."""
        self._on_incremental_events(result)

    def _after_message_action(self) -> None:
        """Обновление ленты после успешного действия (send/edit/delete/pin/файл).
        Решение — в ядре: в общем чате — мгновенный дельта-poll, в канале —
        тихое дифф-обновление хвоста. Раньше здесь была _load_messages() —
        ПОЛНАЯ пересборка ленты после каждого действия: страница мигала,
        скролл прыгал."""
        self._session.after_message_action()

    def _apply_channel_diff(self, channel_name: str, diff) -> None:
        """Применяет дифф хвоста канала от ядра: новые дописываются,
        изменённые перерисовываются на месте, удалённые убираются.
        Полная пересборка — только при переключении канала; устаревшие
        ответы (канал уже переключили) отсекаются в ядре, а на случай
        рассинхрона с отрисовкой здесь страховочный guard: дифф чужого
        потока просто пометит его страницу dirty, но не попадёт в чужую
        ленту."""
        if channel_name != self._rendered_channel:
            stale = self._pages.page(channel_name)
            if stale is not None:
                stale.dirty = True
            return
        was_near_bottom = self._is_scrolled_to_bottom()
        for msg in (*diff.added, *diff.changed):
            # _render_message сам разберётся: новое допишет, старое
            # пересоберёт на месте через _replace_bubble
            self._render_message(msg)
        # Удалённые — убираем точечно (срез на месте: список принадлежит
        # странице, переприсваивать нельзя — см. делегирующее свойство)
        for seq in diff.removed:
            bubble = self._message_widgets.pop(seq, None)
            if bubble is not None:
                container = bubble.parentWidget()
                if container is not None:
                    self._feed_layout.removeWidget(container)
                    container.deleteLater()
                    self._feed_containers[:] = [
                        (c, s) for (c, s) in self._feed_containers if s != seq
                    ]
        if was_near_bottom and not diff.is_empty():
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_incremental_events(self, result: dict) -> None:
        """Обрабатывает НОВЫЕ события с прошлого тика (курсор — в ядре) -
        дописывает новые сообщения и обновляет уже отрисованные (реакция/пин/
        правка/превью) на месте, без пересборки всей ленты (см. _render_message
        и merge_delta_into из ядра)."""
        self._update_online_and_typing(result)

        events = result.get("events", [])

        if not events or self._session.current_channel is not None:
            # Открыт канал: события ОБЩЕГО чата не должны прилетать в чужую
            # ленту (раньше новые общие сообщения дорисовывались внизу канала).
            # seq уже продвинут — при возврате в общий чат _load_messages()
            # заберёт всё заново.
            return

        was_near_bottom = self._is_scrolled_to_bottom()
        new_from_others = 0

        for ev in events:
            kind = ev.get("kind")
            seq = ev.get("seq")

            if kind in ("text", EVENT_KIND_FILE):
                is_new = seq not in self._message_widgets
                self._render_message(ev)
                if is_new:
                    if ev.get("from") == self._my_name:
                        # Своё сообщение — всегда летим вниз (стандарт мессенджеров):
                        # даже если юзер читал историю, отправка из инпута = «хочу
                        # видеть своё сообщение и ответ на него».
                        self._stick_bottom = True
                    else:
                        new_from_others += 1

            elif kind in ("reaction", "pin", "edit", "link_preview"):
                target_seq = ev.get("target_seq")
                bubble = self._message_widgets.get(target_seq)
                if bubble is not None:
                    merged = dict(bubble.message_data)
                    merge_delta_into(merged, ev)
                    # Реакции обновляем на месте без пересоздания bubble —
                    # иначе скролл прыгает и hover-эффекты скидываются.
                    if kind == "reaction" and hasattr(bubble, "update_reactions"):
                        bubble.update_reactions(merged.get("reactions", {}))
                    else:
                        # pin/edit/link_preview требуют пересоздания bubble
                        # (текст/пин-метка меняются структурно)
                        self._render_message(merged)

            elif kind == "delete":
                target_seq = ev.get("target_seq")
                bubble = self._message_widgets.pop(target_seq, None)
                if bubble is not None:
                    container = bubble.parentWidget()
                    if container is not None:
                        self._feed_layout.removeWidget(container)
                        container.deleteLater()
                        # Раньше запись оставалась в _feed_containers навсегда
                        # (устаревший виджет в индексе) — убираем на месте.
                        self._feed_containers[:] = [
                            (c, s) for (c, s) in self._feed_containers if s != target_seq
                        ]

        if was_near_bottom:
            QTimer.singleShot(0, self._scroll_to_bottom)
        else:
            if new_from_others:
                self._unread_count += new_from_others
                self._update_scroll_down_btn()

        # v1.9.6: звук уведомления о чужих сообщениях — только когда окно
        # неактивно/свёрнуто (в фокусе звук бесит). Настройки — Профиль →
        # Уведомления (sound_on_message / sound_volume).
        if new_from_others:
            self._maybe_play_message_sound()

        # Пин мог поставиться/сняться или запиненное сообщение удалено —
        # обновим бейдж.
        if any(e.get("kind") in ("pin", "delete") for e in events):
            self._refresh_pinned()

    def _maybe_play_message_sound(self) -> None:
        """«Динь» при новых чужих сообщениях вне фокуса окна."""
        from lib.storage import load_settings
        from qt_app.sound import play_message_sound

        settings = load_settings()
        if not settings.get("sound_on_message", True):
            return
        window = self.window()
        if window is not None and window.isActiveWindow() and not window.isMinimized():
            return
        play_message_sound(float(settings.get("sound_volume", 0.5)))

    def _is_scrolled_to_bottom(self) -> bool:
        bar = self._scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 32

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _scroll_after_layout(self) -> None:
        """Скролл вниз после того, как layout пересчитает высоты.

        insertWidget НЕ обновляет scrollBar.maximum() мгновенно — значения
        известны только после прохода событий; прямой setValue сразу после
        вставки улетал в никуда (лента оставалась наверху — «нет
        автопрокрутки»), особенно с превью, догружающимися позже."""
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_bubble_content_grew(self) -> None:
        """У отрисованного бабла догрузился поздний контент (превью
        картинки/ссылки) и он вырос — если юзер был приклеен к низу,
        подтягиваем ленту за ним."""
        if self._stick_bottom:
            self._scroll_after_layout()

    def _on_link_activated(self, url: str) -> None:
        """v3: клик по in-app ссылке friendrelay:// в сообщении.

        Сейчас поддерживается:
          friendrelay://chess/join/<CODE>  — открыть диалог шахмат и
                                            присоединиться к матчу.
          friendrelay://go/join/<CODE>     — то же для Го.
        Unknown URL scheme — показываем toast и игнорируем.
        """
        if url.startswith("friendrelay://chess/join/"):
            code = url[len("friendrelay://chess/join/"):].strip().upper()
            code = code.split("/")[0].split("?")[0].split("#")[0]
            if not code:
                return
            self._open_game_with_code("chess", code)
            return
        if url.startswith("friendrelay://go/join/"):
            code = url[len("friendrelay://go/join/"):].strip().upper()
            code = code.split("/")[0].split("?")[0].split("#")[0]
            if not code:
                return
            self._open_game_with_code("go", code)
            return
        # неизвестная схема — не делаем вид, что всё ок
        # v3.5.7 ФИКС: раньше здесь читался self._toast, которого НИКОГДА
        # не существует (hasattr всегда False) — предупреждение молча
        # терялось. Используем канонический Toast.show_toast, как весь
        # остальной экран: тост создаётся на месте, без состояния.
        Toast.show_toast(self, f"Неизвестная ссылка: {url}", icon="?")

    def _open_game_with_code(self, game: str, code: str) -> None:
        """v3.1.1: открыть диалог игры (chess/go) и присоединиться к матчу
        по коду из кликабельной ссылки-приглашения в чате.

        Использует ВНУТРЕННИЙ match-объект диалога (dlg._match), а не
        отдельный — иначе join проходит на сервере, но диалог не знает
        об этом (бесконечная загрузка). Теперь: открываем диалог →
        dlg._match.join(code) → on_match_joined срабатывает внутри диалога
        → доска готова.

        v3.1.1 (bugfix):
          • Раньше диалог конструировался как ``DialogCls(self, …, parent=self)``
            — ``parent`` передавался И позиционно (первый аргумент), И как
            keyword. Python бросал ``TypeError: __init__() got multiple values
            for argument 'parent'`` → клик по ссылке friendrelay://chess/join/
            тихо падал, диалог не открывался. Теперь ``parent`` передаётся
            только как keyword.
          • ``dlg._match.refresh()`` заменён на ``refresh_leaderboard()`` —
            метода ``refresh()`` у ChessMatch/GoMatch нет, вызов тихо падал
            в try/except, рейтинг не подтягивался.
          • Диалог переводится в NET-режим ДО ``show()`` — иначе UI (кнопки
            режима, панель сети, poll_timer) оставался в AI-режиме, хотя
            доска переключалась на NET только после прихода on_match_ready.
          • Добавлена проверка валидности C++ объекта диалога через
            ``Shiboken.isValid`` (WA_DeleteOnClose мог уже удалить объект).
          • Установлен ``WA_DeleteOnClose`` — иначе закрытый, но живой
            диалог копился в памяти после каждого клика по ссылке.
        """
        try:
            if game == "chess":
                from qt_app.widgets.chess_game_dialog import ChessGameDialog
                DialogCls = ChessGameDialog
            elif game == "go":
                from qt_app.widgets.go_game_dialog import GoGameDialog
                DialogCls = GoGameDialog
            else:
                return
        except ImportError:
            return
        attr = f"_{game}_dialog"
        # если диалог уже открыт — переиспользуем, джойним через его match
        dlg = getattr(self, attr, None)
        if dlg is not None:
            # WA_DeleteOnClose мог уже удалить C++ объект — проверяем.
            try:
                from shiboken6 import Shiboken
                if not Shiboken.isValid(dlg):
                    dlg = None
                    setattr(self, attr, None)
            except Exception:
                try:
                    _ = dlg.isVisible()  # бросит RuntimeError, если удалён
                except RuntimeError:
                    dlg = None
                    setattr(self, attr, None)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            # v3.1: используем ВНУТРЕННИЙ match диалога
            if hasattr(dlg, "_match") and dlg._match:
                dlg._match.join(code)
            return
        # создаём диалог — он сам создаст свой внутренний _match.
        # ВАЖНО: parent передаётся ТОЛЬКО как keyword — первый позиционный
        # параметр конструктора тоже ``parent``, двойная передача бросала
        # TypeError и клик по ссылке молча падал (v3.1.1 bugfix).
        dlg = DialogCls(
            parent=self,
            base_url=self._base_url,
            player_name=self._my_name,
            access_key=self._access_key,
            bot_id=game,
        )
        setattr(self, attr, dlg)
        # v3.1.1: WA_DeleteOnClose — закрытый диалог не копится в памяти.
        from PySide6.QtCore import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # v3.1.1: переводим в NET-режим ДО show(), чтобы кнопки режима,
        # панель сети и poll_timer были готовы сразу — не ждём прихода
        # on_match_ready (на котором раньше переключалась только доска,
        # а окружающий UI оставался в AI-режиме).
        try:
            if game == "chess":
                from qt_app.widgets.chess_board_widget import ChessBoardWidget
                dlg._set_mode(ChessBoardWidget.MODE_NET)
            elif game == "go":
                from qt_app.widgets.go_game_dialog import MODE_NET
                dlg._set_mode(MODE_NET)
        except Exception:
            pass
        dlg.show()
        # v3.1: используем ВНУТРЕННИЙ match диалога (dlg._match), а не
        # отдельный объект — иначе on_match_joined не дойдёт до доски.
        if hasattr(dlg, "_match") and dlg._match:
            # подтянуть рейтинг (метод называется refresh_leaderboard —
            # refresh() не существует и тихо падал в try/except, v3.1.1 fix)
            try:
                dlg._match.refresh_leaderboard()
            except Exception:
                pass
            dlg._match.join(code)

    def _on_scroll_changed(self, _value: int) -> None:
        self._stick_bottom = self._is_scrolled_to_bottom()
        if self._stick_bottom:
            if self._unread_count:
                self._unread_count = 0
            self._scroll_down_btn.setVisible(False)
        else:
            self._scroll_down_btn.setVisible(True)
            self._position_scroll_down_btn()

    def _update_scroll_down_btn(self) -> None:
        if self._unread_count > 0:
            label = str(self._unread_count) if self._unread_count <= 99 else "99+"
            self._scroll_down_btn.setText(label)
        else:
            self._scroll_down_btn.setText("↓")
        self._scroll_down_btn.setVisible(True)
        self._position_scroll_down_btn()
        self._pulse_scroll_down_btn()

    # ТЗ раздел 8, «New Message»: «If off-screen, the scroll thumb subtly
    # pulses» — кнопка «вниз» с непрочитанными мягко пульсирует (opacity
    # 1.0 -> 0.65 -> 1.0), пока её не нажмут. Анимация крутится только
    # пока есть непрочитанные.
    def _pulse_scroll_down_btn(self) -> None:
        if self._unread_count <= 0:
            self._stop_scroll_pulse()
            return
        if getattr(self, "_scroll_pulse", None) is not None \
                and self._scroll_pulse.state() == QPropertyAnimation.State.Running:
            return
        if not hasattr(self, "_scroll_pulse_effect"):
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            self._scroll_pulse_effect = QGraphicsOpacityEffect(self._scroll_down_btn)
            self._scroll_down_btn.setGraphicsEffect(self._scroll_pulse_effect)
        effect = self._scroll_pulse_effect
        anim = QPropertyAnimation(effect, b"opacity", self._scroll_down_btn)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.5, 0.65)
        anim.setEndValue(1.0)
        anim.setDuration(1100)
        anim.setLoopCount(-1)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._scroll_pulse = anim
        anim.start()

    def _stop_scroll_pulse(self) -> None:
        anim = getattr(self, "_scroll_pulse", None)
        if anim is not None:
            anim.stop()
            self._scroll_pulse = None
        effect = getattr(self, "_scroll_pulse_effect", None)
        if effect is not None:
            effect.setOpacity(1.0)

    def _on_scroll_down_clicked(self) -> None:
        self._unread_count = 0
        self._stop_scroll_pulse()
        self._scroll_to_bottom()
        self._scroll_down_btn.setVisible(False)

    # -- Поиск по загруженным сообщениям -----------------------------------

    def _focus_search(self) -> None:
        if not self._search_bar.isVisible():
            self._toggle_search_bar()
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _toggle_search_bar(self) -> None:
        showing = not self._search_bar.isVisible()
        self._search_bar.setVisible(showing)
        if showing:
            self._search_input.setFocus()
        else:
            self._search_input.clear()
            self._apply_search_filter("")

    def _on_search_text_changed(self, text: str) -> None:
        self._apply_search_filter(text.strip())

    def _apply_search_filter(self, query: str) -> None:
        """Ищет только среди уже загруженных в ленту сообщений (последние ~50
        плюс всё, что подгружено скроллом вверх) - полнотекстового поиска по
        всей истории на хосте пока нет."""
        query_lc = query.lower()
        matches = 0
        for bubble in self._message_widgets.values():
            container = bubble.parentWidget()
            if container is None:
                continue
            # Ищем по ОТОБРАЖАЕМОМУ тексту (расшифрованному), а не по сырому
            # text из данных — для шифрованных сообщений там JSON.
            text = bubble.display_text()
            is_match = (not query_lc) or (query_lc in text.lower())
            container.setVisible(is_match)
            if query_lc and is_match:
                matches += 1

        if not query_lc:
            self._search_result_label.setText("")
        elif matches == 0:
            self._search_result_label.setText("Ничего не найдено")
        else:
            self._search_result_label.setText(f"Найдено: {matches}")

    def _on_input_text_edited(self, _text: str) -> None:
        """Шлёт эфемерный сигнал "я печатаю" — троттлинг раз в ~2 сек ведёт
        ядро (чтобы не долбить сервер на каждую нажатую клавишу)."""
        if not self._connected:
            return
        if not self._session.should_send_typing():
            return
        self._bridge.run(
            lambda: client.send_typing(self._base_url, self._my_name, self._access_key),
            on_success=lambda _r: None,
            on_error=lambda _e: None,  # эфемерный сигнал, сетевой сбой не критичен
        )

    def _render_message(self, ev: dict, animate: bool = True) -> None:
        """Рендерит одно сообщение — если seq уже отрисован (пришло
        обновление по поллингу: реакция/пин/правка/превью или новый снапшот
        того же сообщения), пересобирает бабл на месте, не трогая остальную
        ленту и не сбрасывая скролл.

        animate=False — для БАТЧ-рендера (открытие канала/первая загрузка):
        десятки параллельных QGraphicsOpacityEffect давали фриз. Одиночные
        живые сообщения анимируются как раньше.

        Сама логика построения контейнера (аватарка + имя + время + bubble)
        вынесена в qt_app/screens/chat_message_renderer.py — это снимает
        ~120 LOC с god-object'а ChatScreen.
        """
        seq = ev.get("seq")
        existing_bubble = self._message_widgets.get(seq) if isinstance(seq, int) else None
        if existing_bubble is not None:
            self._replace_bubble(existing_bubble, ev)
            return

        # Делегируем построение контейнера фабрике
        from qt_app.screens.chat_message_renderer import build_message_container

        is_me = ev.get("from") == self._my_name
        sender = ev.get("from", "?")
        grouped = sender == self._last_sender
        self._last_sender = sender

        msg_container, bubble = build_message_container(
            chat_screen=self,
            ev=ev,
            my_name=self._my_name,
            base_url=self._base_url,
            access_key=self._access_key,
            avatar_cache=self._avatar_cache,
            is_me=is_me,
            grouped=grouped,
        )

        if isinstance(seq, int):
            self._message_widgets[seq] = bubble

        self._feed_layout.insertWidget(self._feed_layout.count() - 1, msg_container)
        self._feed_containers.append((msg_container, seq if isinstance(seq, int) else None))
        self._trim_feed()

        # v1.9.5: при живом поиске новое сообщение тоже проходит фильтр —
        # раньше прятались только старые баблы, а приезжающие по poll
        # несоответствия всё равно появлялись в ленте.
        if (self._search_bar is not None and self._search_bar.isVisible()
                and self._search_input is not None
                and self._search_input.text().strip()):
            query_lc = self._search_input.text().strip().lower()
            msg_container.setVisible(
                query_lc in bubble.display_text().lower()
            )

        # Анимация появления — только для одиночных живых сообщений
        if animate:
            bubble.play_entrance()

    def _trim_feed(self) -> None:
        """Обрезает хвост ленты до MAX_RENDERED_ROWS строк: старые сообщения
        убираются из отрисовки (не с хоста), чтобы виджеты не накапливались
        бесконечно за недели аптайма — иначе лента незаметно съедает память
        и замедляет прокрутку."""
        while len(self._feed_containers) > MAX_RENDERED_ROWS:
            container, seq = self._feed_containers.pop(0)
            if seq is not None:
                self._message_widgets.pop(seq, None)
            self._feed_layout.removeWidget(container)
            container.deleteLater()

    def _replace_bubble(self, old_bubble: QWidget, ev: dict) -> None:
        """Пересобирает уже отрисованный бабл на месте (пришла реакция/пин/
        правка/превью по поллингу) - не трогаем шапку с аватаркой/именем
        отправителя, просто меняем сам бабл внутри его же строки, чтобы не
        сбивать группировку и позицию скролла.

        Bubble создаётся через _create_bubble в chat_message_renderer.py —
        чтобы подключение сигналов было в одном месте, а не дублировалось.
        """
        container = old_bubble.parentWidget()
        if container is None:
            return
        msg_layout = container.layout()
        if msg_layout is None:
            return
        idx = msg_layout.indexOf(old_bubble)
        if idx == -1:
            return

        is_me = ev.get("from") == self._my_name
        msg_layout.removeWidget(old_bubble)
        old_bubble.setParent(None)
        old_bubble.deleteLater()

        from qt_app.screens.chat_message_renderer import _create_bubble

        new_bubble = _create_bubble(
            chat_screen=self,
            ev=ev,
            is_me=is_me,
            my_name=self._my_name,
            base_url=self._base_url,
            access_key=self._access_key,
        )
        if is_me:
            msg_layout.insertWidget(idx, new_bubble)
        else:
            msg_layout.insertWidget(idx, new_bubble, 1)

        seq = ev.get("seq")
        if isinstance(seq, int):
            self._message_widgets[seq] = new_bubble

    def _on_send(self) -> None:
        """Отправляет сообщение или сохраняет редактирование."""
        if not self._connected:
            return

        text = self._input.text().strip()
        if not text:
            return

        # Если в режиме редактирования
        if self._editing_seq is not None:
            # v1.9.5: правка ЗАШИФРОВАННОГО сообщения перешифровывается.
            # Раньше правка уходила plaintext: сервер записывал её в
            # событие с encrypted=True (флаг не снимался) → у ВСЕХ
            # получателей сообщение навсегда превращалось в «не удалось
            # расшифровать», плюс plaintext открыто лежал на сервере.
            was_encrypted = bool(getattr(self, "_editing_was_encrypted", False))
            self._bridge.run(
                lambda: client.edit_text(
                    self._base_url, self._my_name, self._editing_seq, text,
                    self._access_key,
                    encrypt=was_encrypted and self._encryption_enabled,
                ),
                on_success=self._on_edit_success,
                on_error=lambda e: self._on_message_error(e, text=text),
            )
            self._cancel_edit()
            return

        # Если в режиме ответа
        if self._reply_to is not None:
            # Добавляем префикс ответа к тексту
            sender = self._reply_to["sender"]
            reply_prefix = f"@{sender}: "
            text = reply_prefix + text
            self._cancel_reply()

        self._input.clear()

        # !-команды к ботам идут через dispatcher БЕЗ шифрования — это не
        # чат-сообщения, а команды (action=open_game, savescore и т.п.).
        # Шифровать их не нужно, сервер обрабатывает dispatch напрямую.
        if text.startswith("!"):
            self._bridge.run(
                lambda: client.dispatch_command(
                    self._base_url, self._my_name, text, self._access_key
                ),
                on_success=lambda r: self._on_dispatch_result(r, text),
                on_error=lambda e: self._send_text_to_server(text),
            )
            return

        # Обычное сообщение (с шифрованием если включено)
        self._send_text_to_server(text)

    def _on_dispatch_result(self, result: dict, original_text: str) -> None:
        """Обрабатывает ответ /dispatch: вердикт выносит ядро
        (interpret_dispatch), экран только исполняет — текст не сматчился:
        отправить как сообщение; open_game: открыть игру; иначе ленту
        дожимаем дельтой, если бот что-то написал."""
        verdict = interpret_dispatch(result, original_text)

        if verdict.kind == "text":
            self._send_text_to_server(verdict.text)
            return

        if verdict.kind == "open_game":
            # Если был сопутствующий текст — он уже добавлен сервером в чат:
            # подтягиваем дельтой (verdict.refresh), без пересборки всей ленты.
            self._open_game_dialog(verdict.bot_id, verdict.meta, verdict.tab)

        if verdict.refresh:
            self._after_message_action()

    def _open_game_dialog(self, bot_id: str, bot_meta: dict, tab: str) -> None:
        """Открывает окно игры поверх чата. Поддерживает несколько ботов:
        выбирает диалог по bot_id или meta.game.

        Чтобы добавить нового бота-игру:
          1. Создай qt_app/widgets/<name>_game_dialog.py с классом <Name>GameDialog
          2. Зарегистрируй соответствие ниже (bot_id → DialogClass)
          3. Создай lib/<name>_bot.py с ботом, возвращающим action="open_game"
          4. Зарегистрируй бота в lib/relay_server.py:_register_default_bots()
        """
        if not self._base_url or not self._my_name:
            return

        # Реестр диалогов: bot_id → Dialog-класс
        # Импорты ленивые — чтобы при отсутствии PySide6-зависимостей (системные
        # библиотеки на без-GUI серверах) chat_screen всё равно импортировался.
        game = bot_meta.get("game") or bot_id
        dialog_cls = None
        try:
            if game == "snake" or bot_id == "snake":
                from qt_app.widgets.snake_game_dialog import SnakeGameDialog
                dialog_cls = SnakeGameDialog
            elif game == "orbital" or bot_id == "orbital":
                from qt_app.widgets.orbital_game_dialog import OrbitalGameDialog
                dialog_cls = OrbitalGameDialog
            elif game == "voxel_shooter" or bot_id == "voxel_shooter":
                from qt_app.widgets.voxel_game_dialog import VoxelGameDialog
                dialog_cls = VoxelGameDialog
            elif game == "chess" or bot_id == "chess":
                from qt_app.widgets.chess_game_dialog import ChessGameDialog
                dialog_cls = ChessGameDialog
            elif game == "go" or bot_id == "go":
                # (v2.0.6) Го: доска + ИИ + онлайн-матчи через GoBot
                from qt_app.widgets.go_game_dialog import GoGameDialog
                dialog_cls = GoGameDialog
        except ImportError:
            return

        if dialog_cls is None:
            # Неизвестный бот-игра — игнорируем
            return

        dialog = dialog_cls(
            parent=self,
            base_url=self._base_url,
            player_name=self._my_name,
            access_key=self._access_key,
            bot_id=bot_id,
            bot_meta=bot_meta,
            initial_tab=tab,
        )
        # v1.9.5: диалог игры уничтожается при закрытии. Раньше каждый
        # «сыграл → закрыл» оставлял живой скрытый объект (движок, таймеры,
        # canvas) ребёнком ChatScreen до конца сессии — накопление на
        # десятки мегабайт при долгом вечере.
        from PySide6.QtCore import Qt as _Qt

        dialog.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # ── Голосовой канал ──────────────────────────────────────────────

    def _live_voice_dialog(self):
        """Живой диалог голоса или None.

        Диалог создаётся с WA_DeleteOnClose: при закрытии Qt удаляет C++
        объект, а Python-обёртка в self._voice_dialog остаётся. Обращение
        к ней (isVisible()) падало с
        «RuntimeError: libshiboken: Internal C++ object (VoiceChannelDialog)
        already deleted» — краш при каждом нажатии кнопки «Голос» после
        того, как окно закрыли его собственным крестиком. Проверяем
        валидность обёртки через shiboken и подстраховываемся try/except
        (на случай, если обёртка уже наполовину мертва).
        """
        existing = getattr(self, "_voice_dialog", None)
        if existing is None:
            return None
        try:
            from shiboken6 import Shiboken

            if not Shiboken.isValid(existing):
                # C++ объект уже удалён (WA_DeleteOnClose) — сбрасываем
                self._voice_dialog = None
                return None
            return existing
        except ImportError:  # shiboken нет — живём по-старому
            return existing
        except RuntimeError:  # обёртка мертва до проверки
            self._voice_dialog = None
            return None

    def _toggle_voice_dialog(self) -> None:
        """Открывает/закрывает окно голосового канала.

        Сначала спрашивает /voice/info на сервере, чтобы узнать хост и порт
        TCP-сервера (он на +1 от HTTP-порта). Потом открывает диалог.
        """
        if not self._base_url or not self._my_name:
            return

        # Если окно уже открыто — закрываем (с проверкой валидности
        # обёртки: WA_DeleteOnClose удаляет C++ объект сам, см. выше)
        existing = self._live_voice_dialog()
        if existing is not None and existing.isVisible():
            existing.close()
            self._voice_dialog = None
            return

        # Спрашиваем сервер
        self._bridge.run(
            lambda: self._fetch_voice_info(),
            on_success=self._on_voice_info_loaded,
            on_error=lambda e: self._set_status_text(
                f"Голосовой сервер недоступен: {e}", color=PALETTE.danger
            ),
        )

    def _fetch_voice_info(self) -> dict:
        """Синхронный запрос /voice/info — выполняется в фоновом потоке.

        Протокол (заголовки, разбор ответа) — в lib.client, вьюха только
        вызывает и разбирает результат."""
        return client.get_voice_info(self._base_url, self._my_name, self._access_key)

    def _on_voice_info_loaded(self, info: dict) -> None:
        if not info.get("enabled"):
            self._set_status_text(
                "Голосовой сервер не запущен на хосте",
                color=PALETTE.warning,
            )
            return

        voice_port = info.get("port", 0)
        if voice_port == 0:
            self._set_status_text(
                "Не удалось получить порт голосового сервера",
                color=PALETTE.warning,
            )
            return

        # Хост: берём из base_url (только домен/ip, без порта)
        from urllib.parse import urlsplit
        parts = urlsplit(self._base_url)
        voice_host = parts.hostname or "127.0.0.1"

        dialog = VoiceChannelDialog(
            parent=self,
            base_url=self._base_url,
            voice_host=voice_host,
            voice_port=voice_port,
            player_name=self._my_name,
            access_key=self._access_key,
        )
        self._voice_dialog = dialog
        # v1.9.5: как с игровыми диалогами — не копим скрытые VoiceClient'ы
        from PySide6.QtCore import Qt as _Qt

        dialog.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # v1.9.8: когда Qt удалит C++ объект (закрытие крестиком диалога),
        # ссылка self._voice_dialog должна обнулиться сама — иначе
        # _toggle_voice_dialog ловил «Internal C++ object already deleted».
        dialog.destroyed.connect(self._on_voice_dialog_destroyed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_voice_dialog_destroyed(self, *args) -> None:
        """C++ объект диалога удалён (WA_DeleteOnClose) — сбрасываем ссылку."""
        self._voice_dialog = None

    def _send_text_to_server(self, text: str) -> None:
        """Фактическая отправка текста в чат.

        Шифрование (если включено) выполняет client-слой: вьюха только
        передаёт флаг. Сервер получит структурированный конверт
        {text, encrypted, iv} и пометит событие — получатели расшифруют
        через client.decrypt_message."""
        # Отправляем в канал или общий чат (какой канал открыт — знает ядро)
        current_channel = self._session.current_channel
        if current_channel:
            self._bridge.run(
                lambda: client.send_channel_message(
                    self._base_url, self._my_name, current_channel,
                    text, self._access_key, encrypt=self._encryption_enabled,
                ),
                on_success=self._on_message_sent,
                on_error=lambda e: self._on_message_error(e, text=text),
            )
        else:
            self._bridge.run(
                lambda: client.send_text(
                    self._base_url, self._my_name, text, self._access_key,
                    encrypt=self._encryption_enabled,
                ),
                on_success=self._on_message_sent,
                on_error=lambda e: self._on_message_error(e, text=text),
            )

    def _on_message_sent(self, result: dict) -> None:
        """Обрабатывает успешную отправку сообщения: дельта-poll дорисует
        сообщение точечно (раньше здесь была полная пересборка ленты)."""
        # Своё сообщение — экран летит вниз (сообщение дорисуется тиком poll
        # и нас уже будет прижато к низу — см. _stick_bottom в инкременталке)
        self._stick_bottom = True
        self._after_message_action()

    def _on_edit_success(self, result: int) -> None:
        """Обрабатывает успешное редактирование сообщения (дельта-poll)."""
        self._after_message_action()

    def _on_message_error(self, error: Exception, text: str | None = None) -> None:
        """Обрабатывает ошибку отправки сообщения.

        v1.9.5: раньше — молчаливый pass. Инпут уже очищен ДО отправки,
        поэтому при сетевом сбое набранное сообщение терялось БЕЗВОЗВРАТНО
        (юзер уверен, что отправил). Теперь: текст возвращается в поле,
        причина — в статус-строку."""
        self._set_status_text(f"⚠ Не отправлено: {error}", color=PALETTE.danger)
        if text:
            self._input.setText(text)
            self._input.setFocus()

    # -- Обработчики действий с сообщениями -----------------------------------

    def _on_reply_requested(self, ev: dict) -> None:
        """Обрабатывает запрос на ответ на сообщение."""
        sender = ev.get("from", "?")
        # Превью — по расшифрованному тексту (для шифрованных сообщений
        # сырой "text" — конверт)
        text = self._decrypt_message_text(ev)
        preview = text[:50] + "…" if len(text) > 50 else text

        self._reply_to = {"sender": sender, "preview": preview, "original_ev": ev}
        self._reply_label.setText(f"↩ Ответ на {sender}: {preview}")
        self._reply_bar.setVisible(True)
        self._input.setFocus()

    def _cancel_reply(self) -> None:
        """Отменяет режим ответа."""
        self._reply_to = None
        self._reply_bar.setVisible(False)

    def _on_edit_requested(self, ev: dict) -> None:
        """Обрабатывает запрос на редактирование сообщения.

        В поле подставляем расшифрованный текст (для шифрованных сообщений
        "text" события — шифротекст/конверт, редактировать надо plaintext)."""
        self._editing_seq = ev.get("seq")
        self._editing_original_text = self._decrypt_message_text(ev)
        # v1.9.5: расшифрованный текст вернётся в чат ПЕРЕШИФРОВАННЫМ
        # (см. _on_send) — раньше правка шифрованного сообщения ломала
        # его для всех получателей.
        self._editing_was_encrypted = bool(ev.get("encrypted"))

        self._input.setText(self._editing_original_text)
        self._input.setFocus()
        self._input.selectAll()

        # Меняем текст кнопки отправки и показываем кнопку отмены
        self._send_btn.setText("Сохранить")
        self._cancel_edit_btn.setVisible(True)

    def _on_delete_requested(self, seq: int) -> None:
        """Обрабатывает запрос на удаление сообщения."""
        if not self._connected:
            return

        # Удаляем сразу без диалогового окна; лента обновляется дельтой
        self._bridge.run(
            lambda: client.delete_event(self._base_url, self._my_name, seq, self._access_key),
            on_success=lambda result: self._after_message_action(),
            on_error=self._on_message_error,
        )

    def _on_copy_requested(self, text: str) -> None:
        """Копирует текст сообщения в буфер обмена + тост-фидбек
        (ТЗ раздел 8: «Every action has a reaction» — morph в «✓»)."""
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        Toast.show_toast(self, "Скопировано в буфер")

    def _on_reaction_requested(self, seq: int, emoji: str) -> None:
        """Обрабатывает запрос на реакцию — локально тогглим без перезагрузки ленты.

        Сервер всё равно пришлёт delta-reaction на следующем poll-тике, и
        _on_incremental_events его подтвердит через _merge_delta_into.
        Но чтобы пользователь видел мгновенный отклик (без ожидания poll'а),
        мы сразу обновляем pill'ы реакций в существующем MessageBubble.
        """
        if not self._connected:
            return

        # Оптимистичное локальное обновление: тогглим мою реакцию в данных
        # бабла и сразу перерисовываем pill'ы. Раньше данные искались в
        # списке _messages, который никогда не заполнялся — мгновенный отклик
        # не работал, реакция была видна только после ответа сервера.
        bubble = self._message_widgets.get(seq)
        if bubble is not None and hasattr(bubble, "update_reactions"):
            data = bubble.message_data
            reactions = {e: list(u) for e, u in data.get("reactions", {}).items()}
            users = list(reactions.get(emoji, []))
            if self._my_name in users:
                users.remove(self._my_name)
                if users:
                    reactions[emoji] = users
                else:
                    reactions.pop(emoji, None)
            else:
                users.append(self._my_name)
                reactions[emoji] = users
            data["reactions"] = reactions
            bubble.update_reactions(reactions)

        # Отправляем на сервер в фоне — он пришлёт delta, который подтвердит
        # наше оптимистичное обновление (или откатит если сеть упала).
        self._bridge.run(
            lambda: client.add_reaction(
                self._base_url, self._my_name, seq, emoji, self._access_key
            ),
            on_success=lambda result: None,  # delta придёт через poll
            on_error=self._on_message_error,
        )

    def _on_pin_requested(self, seq: int) -> None:
        """Обрабатывает запрос на закрепление сообщения."""
        if not self._connected:
            return

        self._bridge.run(
            lambda: client.pin_message(self._base_url, self._my_name, seq, self._access_key),
            on_success=lambda result: self._after_message_action(),
            on_error=self._on_message_error,
        )

    def _on_download_requested(self, file_id: str, filename: str) -> None:
        """Обрабатывает запрос на скачивание файла или открытие изображения."""
        if not self._connected:
            return

        if self._file_handler.is_image_file(filename):
            # Для изображений показываем превью в диалоге
            self._file_handler.show_image_preview(
                self._base_url, file_id, filename, self, self._access_key
            )
        else:
            # Для остальных файлов предлагаем скачать
            self._file_handler.download_file(
                self._base_url, file_id, filename, self, self._access_key
            )

    def _on_file_download_completed(self, filename: str) -> None:
        """Обрабатывает успешное скачивание файла."""
        self._set_status_text(f"Файл сохранён: {filename}", color=PALETTE.success)
        Toast.show_toast(self, f"Файл сохранён: {filename}", icon="↓")

    def _on_file_download_failed(self, error: str) -> None:
        """Обрабатывает ошибку скачивания файла."""
        self._set_status_text(f"Ошибка скачивания: {error}", color=PALETTE.danger)

    # -- Закреплённые сообщения ------------------------------------------

    def _refresh_pinned(self) -> None:
        """Подтягивает список закреплённых сообщений (сканирует весь лог на
        хосте, не только последние загруженные в ленту - поэтому это
        отдельный запрос, а не фильтр по загруженной ленте)."""
        if not self._connected:
            return
        self._bridge.run(
            lambda: client.get_pinned_messages(self._base_url, self._my_name, self._access_key),
            on_success=self._on_pinned_loaded,
            on_error=lambda e: None,  # неважно если не удалось - кнопка просто не обновится
        )

    def _on_pinned_loaded(self, pinned: list[dict]) -> None:
        self._pinned_messages = pinned
        self._update_pinned_button()

    def _update_pinned_button(self) -> None:
        count = len(self._pinned_messages)
        self._pinned_btn.setText(f"📌 {count}")
        self._pinned_btn.setVisible(count > 0)

    def _show_pinned_dialog(self) -> None:
        """Диалог закрепов: сборка в qt_app/widgets/pinned_dialog.py
        (переход к баблу/открепление тоже живут там)."""
        from qt_app.widgets.pinned_dialog import show_pinned_dialog

        show_pinned_dialog(self)

    def _cancel_edit(self) -> None:
        """Отменяет режим редактирования."""
        self._editing_seq = None
        self._editing_original_text = ""
        self._input.clear()
        self._send_btn.setText("Отправить")
        self._cancel_edit_btn.setVisible(False)

    def _cancel_modes(self) -> None:
        """Отменяет все активные режимы (редактирование, ответ)."""
        if self._editing_seq is not None:
            self._cancel_edit()
        elif self._reply_to is not None:
            self._cancel_reply()

    def _update_typing_indicator(self) -> None:
        """Обновляет индикатор набора текста; протухание (5с) и список
        активных ведёт ядро. Сам индикатор — волна из точек (ТЗ 8)."""
        self._typing_indicator.set_typers(self._session.active_typers())

    def handle_typing_event(self, sender: str) -> None:
        """Обрабатывает событие набора текста от другого пользователя."""
        self._session.note_typing(sender)
        if sender != self._my_name and not self._typing_indicator.isVisible():
            self._update_typing_indicator()

    # -- Drag-and-drop файлов ----------------------------------------------

    def _send_file(self, file_path: Path) -> None:
        """Отправляет файл на сервер."""
        if not self._connected:
            return

        self._bridge.run(
            lambda: client.send_file(
                self._base_url, self._my_name, file_path, self._access_key
            ),
            on_success=self._on_file_sent,
            on_error=self._on_file_error,
        )

    def _on_file_sent(self, result: tuple[int, str]) -> None:
        """Обрабатывает успешную отправку файла (дельта-poll / дифф канала)."""
        # Как с текстом: свой файл — экран летит вниз
        self._stick_bottom = True
        self._after_message_action()

    def _on_file_error(self, error: Exception) -> None:
        """Обрабатывает ошибку отправки файла (v1.9.5: раньше молчаливый
        pass — файл «исчезал» без объяснений; теперь причина в статусе)."""
        self._set_status_text(f"⚠ Файл не отправлен: {error}", color=PALETTE.danger)

    def _on_image_pasted(self, image) -> None:
        """Ctrl+V с картинкой в буфере (например, после PrintScreen) - шлём
        как обычный файл, тем же путём, что и drag-and-drop (см.
        _send_file). Обычная вставка текста этот путь не трогает - см.
        PasteImageLineEdit.keyPressEvent."""
        if not self._connected:
            return
        dest = save_clipboard_image_to_temp(image)
        if dest is None:
            return
        self._send_file(dest)
