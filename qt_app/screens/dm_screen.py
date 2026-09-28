"""
Экран личных сообщений (DM) — аналог dm-таба из lib/tabs_ui.py.

Архитектура:
- Левая колонка: список диалогов с аватарками и последними сообщениями
- Правая колонка: чат с выбранным пользователем
- Асинхронная загрузка через AsyncBridge
- Аватарки через AvatarWidget с кешированием
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.client_core.chat_session import decrypt_text_of
from lib.client_core.social import (
    DM_POLL_INTERVAL_MS,
    can_send_dm,
    dm_preview_text,
    group_dm_flags,
)
from qt_app.async_bridge import AsyncBridge
from qt_app.theme import PALETTE, RADIUS
from qt_app.widgets.avatar_widget import AvatarCache, AvatarWidget
from qt_app.widgets.drop_scroll_area import DropScrollArea
from qt_app.widgets.file_card import fmt_size as _fmt_file_size
from qt_app.widgets.glass_button import GlassButton
from qt_app.widgets.message_bubble import MessageBubble
from qt_app.widgets.paste_image_line_edit import (
    PasteImageLineEdit,
    save_clipboard_image_to_temp,
)


def _fmt_time(ts: float) -> str:
    """Форматирует timestamp в строку времени."""
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except Exception:
        return ""


class NewDMDialog(QDialog):
    """Диалог для создания нового личного сообщения."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новое ЛС")
        self.setFixedSize(360, 200)
        self.setStyleSheet(f"background: {PALETTE.surface_2};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        label = QLabel("Имя пользователя:")
        label.setStyleSheet(f"color: {PALETTE.text_primary}; font-size: 13px;")
        layout.addWidget(label)

        self._input = QLineEdit()
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: 8px;
                padding: 10px 14px;
                color: {PALETTE.text_primary};
            }}
            QLineEdit:focus {{
                border: 1px solid {PALETTE.accent};
            }}
        """)
        self._input.setPlaceholderText("Введите имя пользователя")
        layout.addWidget(self._input)

        buttons = QHBoxLayout()
        buttons.addStretch()

        ok_btn = GlassButton("Начать чат")
        ok_btn.setFixedWidth(120)
        ok_btn.clicked.connect(self.accept)
        buttons.addWidget(ok_btn)

        cancel_btn = GlassButton("Отмена")
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        layout.addLayout(buttons)
        self._input.setFocus()

    def get_username(self) -> str:
        return self._input.text().strip()


class ConversationItem(QWidget):
    """Элемент списка диалогов."""

    clicked = Signal(str)  # имя пользователя

    def __init__(self, conv_data: dict, is_active: bool = False, parent=None):
        super().__init__(parent)
        self._conv_data = conv_data
        self._user = conv_data.get("user", "?")
        self._last_message = conv_data.get("last_message", "")
        self._last_time = conv_data.get("last_time", 0)
        self._is_active = is_active

        self._setup_ui()
        self._update_style()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        # Аватарка
        self._avatar = AvatarWidget(self._user, size=40)
        layout.addWidget(self._avatar)

        # Информация о диалоге
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)

        # Имя и время
        top_row = QHBoxLayout()
        name_label = QLabel(self._user)
        name_label.setStyleSheet(f"color: {PALETTE.text_primary}; font-weight: 600;")
        top_row.addWidget(name_label)

        if self._last_time:
            time_str = _fmt_time(self._last_time)
            time_label = QLabel(time_str)
            time_label.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
            top_row.addStretch()
            top_row.addWidget(time_label)

        info_layout.addLayout(top_row)

        # Последнее сообщение
        preview = dm_preview_text(self._last_message)
        preview_label = QLabel(preview or "  ")
        preview_label.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
        preview_label.setWordWrap(True)
        info_layout.addWidget(preview_label)

        layout.addLayout(info_layout, 1)

    def _update_style(self) -> None:
        if self._is_active:
            self.setStyleSheet(f"""
                ConversationItem {{
                    background: {PALETTE.surface_2};
                    border-left: 3px solid {PALETTE.accent};
                }}
            """)
        else:
            self.setStyleSheet(f"""
                ConversationItem {{
                    background: {PALETTE.surface_1};
                    border-left: 3px solid transparent;
                }}
                ConversationItem:hover {{
                    background: {PALETTE.surface_2};
                }}
            """)

    def set_active(self, active: bool) -> None:
        self._is_active = active
        self._update_style()

    def update_data(self, conv_data: dict) -> None:
        self._conv_data = conv_data
        self._last_message = conv_data.get("last_message", "")
        self._last_time = conv_data.get("last_time", 0)
        # Перерисовать можно было бы эффективнее, но для простоты пересоздаём
        # В реальном проекте лучше обновлять только изменившиеся поля

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._user)
        super().mousePressEvent(event)

    def get_avatar_widget(self) -> AvatarWidget:
        return self._avatar


class DMScreen(QWidget):
    """Экран личных сообщений."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()
        self._avatar_cache = AvatarCache()

        self._base_url = ""
        self._my_name = ""
        self._access_key = ""
        self._connected = False

        # Шифрование: ключ/соль живут в client (application-слой), вьюха
        # держит только флаг "шифровать ли исходящие сообщения".
        self._encryption_enabled = False

        self._active_user: str | None = None
        self._conversations: list[dict] = []
        self._conversation_items: dict[str, ConversationItem] = {}
        self._conv_empty_label: QLabel | None = (
            None  # "Нет диалогов" - см. _on_conversations_loaded
        )
        self._messages: list[dict] = []
        # v1.9.5: seq -> контейнер сообщения (инкрементальный дифф ленты)
        self._dm_widgets: dict[int, QWidget] = {}

        # Таймер для поллинга DM (каждые 2 секунды)
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._is_polling = False

        self._setup_ui()

    def _setup_ui(self) -> None:
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Левая колонка: список диалогов
        left_panel = QWidget()
        left_panel.setFixedWidth(280)
        left_panel.setStyleSheet(f"background: {PALETTE.surface_1};")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Заголовок левой панели
        header = QWidget()
        header.setStyleSheet(f"background: {PALETTE.surface_2};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("Диалоги")
        title.setStyleSheet(f"color: {PALETTE.text_primary}; font-weight: 700; font-size: 16px;")
        header_layout.addWidget(title)

        new_dm_btn = GlassButton("+")
        new_dm_btn.setFixedSize(32, 32)
        new_dm_btn.clicked.connect(self._on_new_dm)
        header_layout.addStretch()
        header_layout.addWidget(new_dm_btn)

        left_layout.addWidget(header)

        # Список диалогов
        self._conv_scroll = QScrollArea()
        self._conv_scroll.setWidgetResizable(True)
        self._conv_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._conv_scroll.setStyleSheet(f"background: {PALETTE.surface_1}; border: none;")

        self._conv_list = QWidget()
        self._conv_list_layout = QVBoxLayout(self._conv_list)
        self._conv_list_layout.setContentsMargins(0, 0, 0, 0)
        self._conv_list_layout.setSpacing(0)
        self._conv_list_layout.addStretch()

        self._conv_scroll.setWidget(self._conv_list)
        left_layout.addWidget(self._conv_scroll, 1)

        main_layout.addWidget(left_panel)

        # Правая колонка: чат
        right_panel = QWidget()
        right_panel.setStyleSheet(f"background: {PALETTE.surface_0};")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Заголовок чата
        self._chat_header = QWidget()
        self._chat_header.setFixedHeight(56)
        self._chat_header.setStyleSheet(f"background: {PALETTE.surface_2};")
        header_layout = QHBoxLayout(self._chat_header)
        header_layout.setContentsMargins(16, 0, 16, 0)

        self._chat_title = QLabel("Выбери диалог или начни новый")
        self._chat_title.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 14px;")
        header_layout.addWidget(self._chat_title)

        right_layout.addWidget(self._chat_header)

        # Область сообщений
        self._chat_scroll = DropScrollArea(drop_text="Отпустите файл здесь, чтобы отправить лично")
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setStyleSheet(f"background: {PALETTE.surface_0}; border: none;")
        self._chat_scroll.file_dropped.connect(self._send_file)

        self._chat_content = QWidget()
        self._chat_layout = QVBoxLayout(self._chat_content)
        self._chat_layout.setContentsMargins(16, 16, 16, 16)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch()

        self._chat_empty = QLabel("Диалог не выбран\nВыбери пользователя слева")
        self._chat_empty.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 14px;")
        self._chat_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_layout.addWidget(self._chat_empty)

        self._chat_scroll.setWidget(self._chat_content)
        right_layout.addWidget(self._chat_scroll, 1)

        # Строка ввода
        input_panel = QWidget()
        input_panel.setStyleSheet(f"background: {PALETTE.surface_2};")
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(16, 12, 16, 12)

        row = QHBoxLayout()

        self._input = PasteImageLineEdit()
        self._input.image_pasted.connect(self._on_image_pasted)
        self._input.setPlaceholderText("Написать сообщение…")
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: 8px;
                padding: 10px 14px;
                color: {PALETTE.text_primary};
            }}
            QLineEdit:focus {{
                border: 1px solid {PALETTE.accent};
            }}
        """)
        self._input.returnPressed.connect(self._on_send)
        row.addWidget(self._input, 1)

        send_btn = GlassButton("Отправить")
        send_btn.setFixedWidth(100)
        send_btn.clicked.connect(self._on_send)
        row.addWidget(send_btn)

        input_layout.addLayout(row)
        right_layout.addWidget(input_panel)

        main_layout.addWidget(right_panel, 1)

    def set_connection(self, base_url: str, my_name: str, access_key: str, connected: bool) -> None:
        """Устанавливает параметры подключения."""
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key
        self._connected = connected

        if connected:
            self._refresh_conversations()
            self._start_polling()
        else:
            self._stop_polling()
            self._clear_all()

    def set_encryption_settings(self, enabled: bool, secret_key: str,
                                 salt: str = "") -> None:
        """Принимает настройки шифрования + per-installation соль.

        Владелец CryptoManager — lib.client (application-слой): экран только
        транслирует настройки туда и держит флаг шифрования."""
        self._encryption_enabled = enabled
        if salt:
            client.set_crypto_salt(salt)
        client.set_crypto_secret_key(secret_key if enabled and secret_key else "")

    def _decrypt_message_text(self, msg: dict) -> str:
        """Расшифровка текста сообщения — общая функция ядра чата
        (decrypt_text_of), экран не дублирует плейсхолдер."""
        return decrypt_text_of(msg)

    def _clear_all(self) -> None:
        """Очищает всё при отключении."""
        self._active_user = None
        self._conversations = []
        self._messages = []
        # v1.9.5: seq -> контейнер сообщения (для точечного дописывания диффа)
        self._dm_widgets: dict[int, QWidget] = {}

        # Очищаем список диалогов
        for item in self._conversation_items.values():
            item.deleteLater()
        self._conversation_items.clear()
        if self._conv_empty_label is not None:
            self._conv_empty_label.deleteLater()
            self._conv_empty_label = None

        # Очищаем чат
        for i in reversed(range(self._chat_layout.count())):
            widget = self._chat_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

        self._chat_layout.addStretch()
        self._chat_empty = QLabel("Не подключено")
        self._chat_empty.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 14px;")
        self._chat_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_layout.addWidget(self._chat_empty)

        self._chat_title.setText("Выбери диалог или начни новый")
        self._chat_title.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 14px;")

    def _refresh_conversations(self) -> None:
        """Загружает список диалогов."""
        if not self._connected:
            return
        # v1.9.5: при переподключении отпечаток устарел (список мог
        # смениться на сервере) — принудительная пересборка один раз.

        self._bridge.run(
            lambda: client.get_dm_conversations(self._base_url, self._my_name, self._access_key),
            on_success=self._on_conversations_loaded,
            on_error=self._on_conversations_error,
        )

    def _on_conversations_loaded(self, convs: list[dict]) -> None:
        """Обрабатывает загруженные диалоги.

        v1.9.5: пересобираем список ТОЛЬКО при реальных изменениях (раньше
        каждые 2с сносились ВСЕ ConversationItem и строились заново: терялся
        hover, дёргался скролл, а для юзеров без аватарки кеширования не
        было — по HTTP-запросу на каждого каждые 2с, бесконечно)."""
        import json as _json

        fingerprint = _json.dumps(
            [(c.get("user"), c.get("last_message"), c.get("last_time"))
             for c in convs], ensure_ascii=False
        )
        if fingerprint == getattr(self, "_conv_fingerprint", None):
            # ничего не изменилось — но активность могла смениться
            for u, item in self._conversation_items.items():
                item.set_active(u == self._active_user)
            return
        self._conv_fingerprint = fingerprint
        self._conversations = convs

        # Удаляем старые элементы
        for item in self._conversation_items.values():
            item.deleteLater()
        self._conversation_items.clear()

        # Удаляем прошлый "Нет диалогов", если был - раньше эта метка не
        # отслеживалась и не удалялась, а поллинг вызывает эту функцию
        # каждые 2 секунды (_on_poll_tick), так что при пустом списке
        # диалогов новый QLabel создавался и добавлялся в layout на каждый
        # тик, бесконечно накапливаясь друг под другом.
        if self._conv_empty_label is not None:
            self._conv_empty_label.deleteLater()
            self._conv_empty_label = None

        if not convs:
            empty_label = QLabel("Нет диалогов\nНажми + чтобы начать")
            empty_label.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 13px;")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Вставляем перед stretch
            self._conv_list_layout.insertWidget(self._conv_list_layout.count() - 1, empty_label)
            self._conv_empty_label = empty_label
            return

        # Создаём новые элементы
        for conv in convs:
            user = conv.get("user", "?")
            is_active = user == self._active_user
            item = ConversationItem(conv, is_active)
            item.clicked.connect(self._on_conversation_clicked)

            # Загружаем аватарку (v1.9.5: негативный кеш — b"" для «нет
            # аватарки», без него каждый тик = новый HTTP-запрос)
            cached = self._avatar_cache.get(user)
            if cached:
                item.get_avatar_widget().set_avatar_data(cached)
            elif not self._avatar_cache.has_record(user):
                item.get_avatar_widget().load_avatar(self._base_url, self._access_key)

                # Кешируем после загрузки (None → негативная запись)
                def cache_avatar(data, username=user):
                    self._avatar_cache.set(username, data or b"")

                item.get_avatar_widget().avatar_loaded.connect(cache_avatar)

            # Вставляем перед stretch
            self._conv_list_layout.insertWidget(self._conv_list_layout.count() - 1, item)
            self._conversation_items[user] = item

    def _on_conversations_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки диалогов."""
        # Показываем ошибку в заголовке чата — пользователь хотя бы видит,
        # что что-то не так (раньше ошибки молча глотались).
        self._chat_title.setText(f"⚠ Не удалось загрузить диалоги: {error}")
        self._chat_title.setStyleSheet(f"color: {PALETTE.danger}; font-size: 13px;")

    def _start_polling(self) -> None:
        """Запускает поллинг DM."""
        if not self._is_polling:
            self._is_polling = True
            self._poll_timer.start(DM_POLL_INTERVAL_MS)

    def _stop_polling(self) -> None:
        """Останавливает поллинг DM."""
        if self._is_polling:
            self._is_polling = False
            self._poll_timer.stop()

    def _on_poll_tick(self) -> None:
        """Тик таймера поллинга - обновляет диалоги и чат."""
        if not self._connected:
            self._stop_polling()
            return

        # Обновляем список диалогов
        self._refresh_conversations()

        # Если открыт активный чат, обновляем его
        if self._active_user:
            self._refresh_chat()

    def _on_conversation_clicked(self, user: str) -> None:
        """Обрабатывает клик по диалогу."""
        self._open_chat_with(user)

    def _open_chat_with(self, user: str) -> None:
        """Открывает чат с пользователем."""
        if not user:
            return

        self._active_user = user

        # Обновляем UI
        self._chat_title.setText(f"💬 {user}")
        self._chat_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-weight: 600; font-size: 16px;"
        )

        # Обновляем активный элемент в списке
        for u, item in self._conversation_items.items():
            item.set_active(u == user)

        # Загружаем сообщения
        self._refresh_chat()

    def _refresh_chat(self) -> None:
        """Загружает сообщения чата."""
        if not self._connected or not self._active_user:
            return

        user = self._active_user
        self._bridge.run(
            lambda: client.get_dm_history(
                self._base_url, self._my_name, user, self._access_key, limit=100
            ),
            # v1.9.5: юзер захвачен замыканием — быстрые клики «A → B»
            # раньше давали два запроса в полёте, и поздний ответ A рисовал
            # переписку A под шапкой B (ядро чата от такого защищено, DM — нет).
            on_success=lambda msgs, u=user: self._on_chat_loaded(msgs, u),
            on_error=self._on_chat_error,
        )

    def _on_chat_loaded(self, msgs: list[dict], user: str | None = None) -> None:
        """Обрабатывает загруженные сообщения.

        v1.9.5 — три фикса разом:
        1) guard юзера: поздний ответ чужого диалога не рисуется (см.
           _refresh_chat);
        2) ИНКРЕМЕНТАЛЬНЫЙ дифф: раньше лента пересобиралась ЦЕЛИКОМ каждые
           2с (поллинг) — удалялись все баблы и создавались заново, позиция
           чтения истории сбрасывалась в низ, читать переписку было
           невозможно, а на длинных диалогах — тысячи конструкций виджетов
           каждые 2с (web-клиент это уже починили диффом, QT — нет);
        3) скролл: принудительно вниз только если читали низ; чтение
           истории больше не прыгает.
        """
        if self._active_user is None:
            return
        if user is not None and user != self._active_user:
            return  # ответ устаревшего запроса (каналили другой диалог)

        from lib.client_core.chat_session import diff_tail

        diff = diff_tail(self._messages or [], msgs)
        append_only = (
            bool(self._messages)
            and not diff.removed
            and not diff.changed
            and diff.added
            and diff.added[0].get("seq", 0) > (self._messages[-1].get("seq", 0))
        )
        was_at_bottom = self._is_chat_scrolled_to_bottom()

        if append_only:
            # Быстрый путь (99% тиков): дописываем новые сообщения вниз,
            # не трогая существующие виджеты (скролл/selection живут).
            self._messages = msgs
            self._chat_empty.hide()
            prev_sender = self._messages[-1 - len(diff.added)].get("from") if len(self._messages) > len(diff.added) else None
            for msg in diff.added:
                self._append_dm_message(msg, grouped=(msg.get("from") == prev_sender))
                prev_sender = msg.get("from")
            if was_at_bottom:
                self._scroll_chat_to_bottom()
            return

        if not diff.is_empty() or not self._dm_widgets:
            # Полная пересборка (первая загрузка / правки-удаления):
            self._messages = msgs
            self._render_full_chat(msgs, was_at_bottom)
        # дифф пуст и виджеты уже есть — ничего не делаем (тихий тик)

    def _is_chat_scrolled_to_bottom(self) -> bool:
        bar = self._chat_scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 32

    def _scroll_chat_to_bottom(self) -> None:
        bar = self._chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _render_full_chat(self, msgs: list[dict], was_at_bottom: bool | None = None) -> None:
        """Полная отрисовка переписки (первая загрузка/пересборка).
        was_at_bottom=None → как раньше (всегда вниз); иначе уважаем
        позицию чтения (v1.9.5)."""
        if was_at_bottom is None:
            was_at_bottom = True

        # Очищаем старые сообщения
        for i in reversed(range(self._chat_layout.count())):
            widget = self._chat_layout.itemAt(i).widget()
            if widget and widget != self._chat_empty:
                widget.deleteLater()
        self._dm_widgets.clear()

        if not msgs:
            self._chat_empty.setText(
                f"Начни общение с {self._active_user}\nНапиши первое сообщение!"
            )
            self._chat_layout.addWidget(self._chat_empty)
            return

        # Скрываем empty label
        self._chat_empty.hide()

        # Добавляем сообщения с группировкой (общий хелпер с _append_dm_message)
        grouped_flags = group_dm_flags(msgs)
        for msg, grouped in zip(msgs, grouped_flags, strict=True):
            self._append_dm_message(msg, grouped)

        # v1.9.5: вниз — только если читали низ (см. _on_chat_loaded)
        if was_at_bottom:
            self._scroll_chat_to_bottom()

    def _append_dm_message(self, msg: dict, grouped: bool) -> None:
        """Рисует ОДНО сообщение переписки в конец ленты (v1.9.5: extracted
        из полной пересборки — им же пользуется инкрементальный дифф)."""
        is_me = msg.get("from") == self._my_name
        sender = msg.get("from", "?")

        # Контейнер для сообщения
        msg_container = QWidget()
        msg_layout = QHBoxLayout(msg_container)
        msg_layout.setContentsMargins(0, 4, 0, 4)
        msg_layout.setSpacing(8)

        if not is_me:
            # Левая колонка для входящих
            if not grouped:
                # Показываем аватарку и заголовок только для первого сообщения в группе
                header = QWidget()
                header_layout = QVBoxLayout(header)
                header_layout.setContentsMargins(0, 0, 0, 0)
                header_layout.setSpacing(2)

                # Аватарка
                avatar = AvatarWidget(sender, size=24)
                cached = self._avatar_cache.get(sender)
                if cached:
                    avatar.set_avatar_data(cached)
                elif not self._avatar_cache.has_record(sender):
                    avatar.load_avatar(self._base_url, self._access_key)

                    def cache_avatar(data, username=sender):
                        self._avatar_cache.set(username, data or b"")

                    avatar.avatar_loaded.connect(cache_avatar)

                header_layout.addWidget(avatar)

                # Имя и время
                meta = QWidget()
                meta_layout = QVBoxLayout(meta)
                meta_layout.setContentsMargins(0, 0, 0, 0)
                meta_layout.setSpacing(0)

                name_label = QLabel(sender)
                name_label.setStyleSheet(
                    f"color: {PALETTE.text_primary}; font-weight: 600; font-size: 12px;"
                )
                meta_layout.addWidget(name_label)

                if msg.get("ts"):
                    time_str = _fmt_time(msg.get("ts", 0))
                    time_label = QLabel(time_str)
                    time_label.setStyleSheet(
                        f"color: {PALETTE.text_secondary}; font-size: 12px;"
                    )
                    meta_layout.addWidget(time_label)

                header_layout.addWidget(meta)
                msg_layout.addWidget(header)
            else:
                # Отступ для сгруппированных сообщений
                spacer = QWidget()
                spacer.setFixedSize(28, 1)
                msg_layout.addWidget(spacer)

            # Сообщение
            bubble = self._build_message_widget(msg, is_me)
            msg_layout.addWidget(bubble, 1)
            msg_layout.addStretch()
        else:
            # Правая колонка для исходящих
            msg_layout.addStretch()

            if not grouped:
                # Время над сообщением
                if msg.get("ts"):
                    time_str = _fmt_time(msg.get("ts", 0))
                    time_label = QLabel(time_str)
                    time_label.setStyleSheet(
                        f"color: {PALETTE.text_secondary}; font-size: 12px;"
                    )
                    time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                    msg_layout.addWidget(time_label)

            # Сообщение
            bubble = self._build_message_widget(msg, is_me)
            msg_layout.addWidget(bubble)

        self._chat_layout.insertWidget(self._chat_layout.count() - 1, msg_container)
        seq = msg.get("seq")
        if isinstance(seq, int):
            self._dm_widgets[seq] = msg_container

    def _build_message_widget(self, msg: dict, is_me: bool) -> QWidget:
        """Раньше тут всегда создавался текстовый MessageBubble, даже для
        msg.get("kind") == "dm_file" - в итоге приватно отправленный файл
        просто не был виден в окне ЛС (пустой пузырь без текста). Теперь
        файловые сообщения рисуются отдельной компактной карточкой со
        скачиванием."""
        if msg.get("kind") == "dm_file":
            return self._build_dm_file_widget(msg)
        decrypted_text = self._decrypt_message_text(msg)
        return MessageBubble(decrypted_text, is_me)

    def _build_dm_file_widget(self, msg: dict) -> QWidget:
        name = msg.get("name", "файл")
        size = msg.get("size", 0)
        file_id = msg.get("file_id", "")

        card = QWidget()
        card.setStyleSheet(f"""
            background: {PALETTE.surface_2};
            border-radius: {RADIUS}px;
        """)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        icon = QLabel("📎")
        icon.setStyleSheet("font-size: 20px; background: transparent;")
        layout.addWidget(icon)

        meta = QVBoxLayout()
        meta.setSpacing(0)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-weight: 600; font-size: 12px; background: transparent;"
        )
        name_lbl.setToolTip(name)
        meta.addWidget(name_lbl)
        size_lbl = QLabel(_fmt_file_size(size))
        size_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; background: transparent;"
        )
        meta.addWidget(size_lbl)
        layout.addLayout(meta, 1)

        dl_btn = QPushButton("⬇")
        dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dl_btn.setFixedSize(28, 28)
        dl_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.accent};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: 8px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background: {PALETTE.accent_hover}; }}
        """)
        dl_btn.clicked.connect(lambda: self._on_dm_file_download(file_id, name))
        layout.addWidget(dl_btn)

        return card

    def _on_dm_file_download(self, file_id: str, suggested_name: str) -> None:
        if not self._connected or not file_id:
            return
        dest_str, _ = QFileDialog.getSaveFileName(
            self, "Сохранить файл", str(Path.home() / "Downloads" / suggested_name)
        )
        if not dest_str:
            return
        dest = Path(dest_str)

        def do_download():
            client.download_dm_file(self._base_url, self._my_name, file_id, dest, self._access_key)
            return str(dest)

        self._bridge.run(do_download, on_success=lambda _p: None,
                         on_error=self._on_download_error)

    def _on_download_error(self, error: Exception) -> None:
        """Обрабатывает ошибку скачивания DM-файла."""
        QMessageBox.warning(self, "Файл", f"Не удалось скачать файл: {error}")

    def _on_chat_error(self, error: Exception) -> None:
        """Обрабатывает ошибку загрузки сообщений."""
        self._chat_title.setText(f"⚠ Не удалось загрузить сообщения: {error}")
        self._chat_title.setStyleSheet(f"color: {PALETTE.danger}; font-size: 13px;")

    def _on_send(self) -> None:
        """Отправляет сообщение."""
        text = self._input.text().strip()
        if not can_send_dm(self._connected, self._active_user, text):
            return

        self._input.clear()

        # Шифрование (если включено) выполняет client-слой: вьюха только
        # передаёт флаг. Сервер пометит событие encrypted+iv, получатель
        # расшифрует через client.decrypt_message.
        self._bridge.run(
            lambda: client.send_dm(
                self._base_url, self._my_name, self._active_user, text,
                self._access_key, encrypt=self._encryption_enabled,
            ),
            on_success=self._on_message_sent,
            on_error=self._on_message_error,
        )

    def _on_message_sent(self, seq: int) -> None:
        """Обрабатывает успешную отправку сообщения."""
        # Перезагружаем чат
        self._refresh_chat()
        # Обновляем список диалогов
        self._refresh_conversations()

    def _on_message_error(self, error: Exception) -> None:
        """Обрабатывает ошибку отправки сообщения."""
        self._chat_title.setText(f"⚠ Не отправлено: {error}")
        self._chat_title.setStyleSheet(f"color: {PALETTE.danger}; font-size: 13px;")

    def _on_new_dm(self) -> None:
        """Создаёт новый диалог."""
        if not self._connected:
            return

        dialog = NewDMDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            username = dialog.get_username()
            if username:
                self._open_chat_with(username)

    # -- Drag-and-drop файлов ----------------------------------------------

    def _send_file(self, file_path: Path) -> None:
        """Отправляет файл в ЛС - приватно (см. client.send_dm_file):
        физически лежит на хосте отдельно от общей витрины, скачать может
        только собеседник. Раньше тут по ошибке вызывался общий
        client.send_file, который кладёт файл в ПУБЛИЧНУЮ витрину, видную
        всем на сервере - при этом сам файл в окне ЛС даже не отображался."""
        if not self._connected or not self._active_user:
            return

        def progress_callback(sent: int, total: int, elapsed: float) -> None:
            # Можно добавить индикатор прогресса в будущем
            pass

        self._bridge.run(
            lambda: client.send_dm_file(
                self._base_url,
                self._my_name,
                self._active_user,
                file_path,
                self._access_key,
                progress_callback,
            ),
            on_success=self._on_file_sent,
            on_error=self._on_file_error,
        )

    def _on_file_sent(self, result: tuple[int, str]) -> None:
        """Обрабатывает успешную отправку файла."""
        # Перезагружаем чат и диалоги
        self._refresh_chat()
        self._refresh_conversations()

    def _on_file_error(self, error: Exception) -> None:
        """Обрабатывает ошибку отправки файла."""
        QMessageBox.warning(self, "Файл", f"Не удалось отправить файл: {error}")

    def _on_image_pasted(self, image) -> None:
        """Ctrl+V с картинкой в буфере (например, после PrintScreen) - шлём
        приватно собеседнику, тем же путём, что и drag-and-drop (см.
        _send_file)."""
        if not self._connected or not self._active_user:
            return
        dest = save_clipboard_image_to_temp(image)
        if dest is None:
            return
        self._send_file(dest)
