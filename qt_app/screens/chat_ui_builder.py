"""Построение UI экрана чата (вынесено из chat_screen.py в v1.9.9).

ChatScreen остаётся тонким координатором: вся раскладка (панель каналов,
топ-стрип онлайн/поиска/пинов, лента, инпут-строка) собирается здесь —
функцией build_chat_ui(screen), которая проставляет виджеты как атрибуты
экрана и подключает сигналы к его методам. Разметка не смешивается с
логикой: экрану остаётся только обработка событий и поллинг.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qt_app.screens.feed_pages import FeedPageManager
from qt_app.theme import FONT_SIZE_BODY, PALETTE, RADIUS, RADIUS_SM
from qt_app.widgets.drop_scroll_area import DropScrollArea
from qt_app.widgets.paste_image_line_edit import PasteImageLineEdit
from qt_app.widgets.typing_indicator import TypingIndicator


def glass_btn(
    text: str,
    tooltip: str = "",
    width: int | None = None,
    height: int = 32,
    radius: int = RADIUS,
    font_size: int = 14,
    padding: str = "0 12px",
) -> QPushButton:
    """Фабрика glass-кнопок топ-стрипа и инпут-строки.

    Стиль из веб-клиента: glass_bg фон, glass_bg_hover при наведении.
    Раньше один и тот же QSS копипастился пять раз (поиск, войс, пины,
    отмена ответа, отмена правки) — теперь стиль в одном месте."""
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip(tooltip)
    if width is not None:
        btn.setFixedWidth(width)
    btn.setFixedHeight(height)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {PALETTE.glass_bg};
            color: {PALETTE.text_secondary};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {radius}px;
            padding: {padding};
            font-size: {font_size}px;
        }}
        QPushButton:hover {{
            background: {PALETTE.glass_bg_hover};
            color: {PALETTE.text_primary};
            border-color: {PALETTE.border};
        }}
    """)
    return btn


def build_chat_ui(screen) -> None:
    """Собирает разметку экрана чата и подключает сигналы к его методам.

    Экран (ChatScreen) передаётся целиком: функция проставляет виджеты
    как его атрибуты (_channels_panel, _scroll, _input, ...) и вешает
    сигналы на его же обработчики (_on_send, _toggle_search_bar, ...).
    Тела методов экрана не меняются — меняется только место сборки.
    """
    root = QHBoxLayout(screen)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    # ============ ЛЕВАЯ ПАНЕЛЬ: КАНАЛЫ (Web Studio стиль) ============
    screen._channels_panel = QWidget()
    screen._channels_panel.setObjectName("ChannelsPanel")
    screen._channels_panel.setFixedWidth(220)
    screen._channels_panel.setStyleSheet(f"""
        #ChannelsPanel {{
            background: {PALETTE.surface_1};
            border-right: 1px solid {PALETTE.border_soft};
        }}
    """)
    channels_layout = QVBoxLayout(screen._channels_panel)
    channels_layout.setContentsMargins(12, 18, 12, 12)
    channels_layout.setSpacing(10)

    # Заголовок с тонким разделителем
    channels_header = QLabel("КАНАЛЫ")
    channels_header.setStyleSheet(f"""
        color: {PALETTE.text_secondary};
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 2px;
        padding: 0 4px 4px 4px;
    """)
    channels_layout.addWidget(channels_header)

    # Scrollable список pills
    screen._channels_scroll = QScrollArea()
    screen._channels_scroll.setWidgetResizable(True)
    screen._channels_scroll.setFrameShape(QFrame.Shape.NoFrame)
    screen._channels_scroll.setStyleSheet(
        "QScrollArea { background: transparent; border: none; }"
    )
    screen._channels_scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    channels_list_host = QWidget()
    channels_list_host.setStyleSheet("background: transparent;")
    screen._channels_list_layout = QVBoxLayout(channels_list_host)
    screen._channels_list_layout.setContentsMargins(0, 0, 0, 0)
    screen._channels_list_layout.setSpacing(4)
    screen._channels_list_layout.addStretch(1)
    screen._channels_scroll.setWidget(channels_list_host)
    channels_layout.addWidget(screen._channels_scroll, 1)

    # Кнопка создания канала — с glass-стилем и иконкой
    screen._create_channel_btn = QPushButton("+  Создать канал")
    screen._create_channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    screen._create_channel_btn.setFixedHeight(36)
    screen._create_channel_btn.setStyleSheet(f"""
        QPushButton {{
            background: {PALETTE.glass_bg};
            color: {PALETTE.text_primary};
            border: 1px solid {PALETTE.border};
            border-radius: {RADIUS}px;
            padding: 0 14px;
            font-size: 12px;
            font-weight: 600;
            text-align: left;
            padding-left: 16px;
        }}
        QPushButton:hover {{
            background: {PALETTE.glass_bg_hover};
            border-color: {PALETTE.border_strong};
            color: {PALETTE.text_primary};
        }}
        QPushButton:pressed {{
            background: {PALETTE.glass_bg_active};
        }}
    """)
    screen._create_channel_btn.clicked.connect(screen._on_create_channel)
    channels_layout.addWidget(screen._create_channel_btn)

    root.addWidget(screen._channels_panel)

    # ============ ЦЕНТР: ЧАТ ============
    chat_container = QWidget()
    chat_container.setStyleSheet(f"background: {PALETTE.surface_0};")
    chat_layout = QVBoxLayout(chat_container)
    chat_layout.setContentsMargins(0, 0, 0, 0)
    chat_layout.setSpacing(0)

    # ---- Топ-стрип: компактная стеклянная панель с онлайн/поиск/пины ----
    screen._online_strip = QWidget()
    screen._online_strip.setObjectName("OnlineStrip")
    screen._online_strip.setFixedHeight(52)
    screen._online_strip.setStyleSheet(f"""
        #OnlineStrip {{
            background: {PALETTE.surface_1};
            border-bottom: 1px solid {PALETTE.border_soft};
        }}
    """)
    strip_layout = QHBoxLayout(screen._online_strip)
    strip_layout.setContentsMargins(20, 0, 20, 0)
    strip_layout.setSpacing(8)

    # Иконка «онлайн» + счётчик слева (компактно)
    screen._online_count_label = QLabel("●")
    screen._online_count_label.setStyleSheet(
        f"color: {PALETTE.text_secondary}; font-size: 12px; font-weight: 600;"
    )
    strip_layout.addWidget(screen._online_count_label)

    # Контейнер с чипами онлайн-пользователей (scrollable, если их много)
    screen._online_chips_scroll = QScrollArea()
    screen._online_chips_scroll.setWidgetResizable(True)
    screen._online_chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
    screen._online_chips_scroll.setStyleSheet(
        "QScrollArea { background: transparent; border: none; }"
    )
    screen._online_chips_scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    screen._online_chips_scroll.setVerticalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    screen._online_chips_host = QWidget()
    screen._online_chips_host.setStyleSheet("background: transparent;")
    screen._online_chips_layout = QHBoxLayout(screen._online_chips_host)
    screen._online_chips_layout.setContentsMargins(0, 0, 0, 0)
    screen._online_chips_layout.setSpacing(6)
    screen._online_chips_layout.addStretch(1)
    screen._online_chips_scroll.setWidget(screen._online_chips_host)
    strip_layout.addWidget(screen._online_chips_scroll, 1)

    # Статус-метка для ошибок (синхронная, без чипов)
    screen._online_label = QLabel("")
    screen._online_label.setStyleSheet(f"""
        color: {PALETTE.text_secondary};
        font-size: 12px;
        padding: 4px 0;
    """)
    screen._online_label.setVisible(False)  # показываем только при ошибках
    strip_layout.addWidget(screen._online_label)

    # Поиск в ленте — Ctrl+Shift+F (глобальный Ctrl+K занят палитрой
    # команд, ТЗ раздел 9: «Ctrl+K opens a spotlight-style overlay»)
    screen._search_btn = screen._glass_btn(
        "🔍", tooltip="Поиск по ленте (Ctrl+Shift+F)", width=32)
    screen._search_btn.clicked.connect(screen._toggle_search_bar)
    strip_layout.addWidget(screen._search_btn)

    # Кнопка голосового канала
    screen._voice_btn = screen._glass_btn("🎙", tooltip="Голосовой канал", width=32)
    screen._voice_btn.clicked.connect(screen._toggle_voice_dialog)
    strip_layout.addWidget(screen._voice_btn)

    # Кнопка закреплённых
    screen._pinned_btn = screen._glass_btn("📌 0", font_size=12)
    screen._pinned_btn.setVisible(False)
    screen._pinned_btn.clicked.connect(screen._show_pinned_dialog)
    strip_layout.addWidget(screen._pinned_btn)

    chat_layout.addWidget(screen._online_strip)

    # ---- Строка поиска (скрыта, открывается по 🔍) ----
    screen._search_bar = QWidget()
    screen._search_bar.setObjectName("SearchBar")
    screen._search_bar.setStyleSheet(f"""
        #SearchBar {{
            background: {PALETTE.surface_1};
            border-bottom: 1px solid {PALETTE.border_soft};
        }}
    """)
    search_layout = QHBoxLayout(screen._search_bar)
    search_layout.setContentsMargins(20, 8, 20, 8)
    search_layout.setSpacing(8)

    screen._search_input = QLineEdit()
    screen._search_input.setPlaceholderText("Поиск по загруженным сообщениям…")
    screen._search_input.setStyleSheet(f"""
        QLineEdit {{
            background: {PALETTE.surface_3};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 6px 12px;
            color: {PALETTE.text_primary};
            font-size: 12px;
        }}
        QLineEdit:focus {{
            border: 1px solid {PALETTE.accent};
            background: {PALETTE.surface_4};
        }}
    """)
    screen._search_input.textChanged.connect(screen._on_search_text_changed)
    search_layout.addWidget(screen._search_input, 1)

    screen._search_result_label = QLabel("")
    screen._search_result_label.setStyleSheet(
        f"color: {PALETTE.text_secondary}; font-size: 12px;"
    )
    search_layout.addWidget(screen._search_result_label)

    search_close_btn = QPushButton("✕")
    search_close_btn.setFixedSize(24, 24)
    search_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    search_close_btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent; border: none;
            color: {PALETTE.text_secondary}; font-size: 13px;
        }}
        QPushButton:hover {{ color: {PALETTE.text_primary}; }}
    """)
    search_close_btn.clicked.connect(screen._toggle_search_bar)
    search_layout.addWidget(search_close_btn)

    screen._search_bar.setVisible(False)
    chat_layout.addWidget(screen._search_bar)

    # ---- Лента сообщений ----
    screen._scroll = DropScrollArea(
        drop_text="Отпустите файл здесь, чтобы отправить в чат")
    screen._scroll.setWidgetResizable(True)
    screen._scroll.setStyleSheet(f"background: {PALETTE.surface_0}; border: none;")
    screen._scroll.file_dropped.connect(screen._send_file)
    screen._scroll.verticalScrollBar().valueChanged.connect(screen._on_scroll_changed)

    # Кэш страниц ленты (общий чат + каналы): мгновенное переключение
    # без пересоздания виджетов (см. FeedPageManager).
    screen._pages = FeedPageManager(screen._scroll)
    chat_layout.addWidget(screen._scroll, 1)

    # ---- Плавающая кнопка "вниз" со счётчиком непрочитанных ----
    screen._scroll_down_btn = QPushButton("↓", screen._scroll)
    screen._scroll_down_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    screen._scroll_down_btn.setFixedSize(40, 40)
    screen._scroll_down_btn.setStyleSheet(f"""
        QPushButton {{
            background: {PALETTE.accent};
            color: {PALETTE.text_on_accent};
            border: none;
            border-radius: 20px;
            font-size: 16px;
            font-weight: 700;
        }}
        QPushButton:hover {{
            background: {PALETTE.accent_hover};
        }}
    """)
    screen._scroll_down_btn.clicked.connect(screen._on_scroll_down_clicked)
    screen._scroll_down_btn.setVisible(False)
    screen._scroll_down_btn.raise_()

    # ---- Индикатор набора текста (волна из точек, ТЗ раздел 8) ----
    screen._typing_indicator = TypingIndicator()
    screen._typing_indicator.setVisible(False)
    chat_layout.addWidget(screen._typing_indicator)

    # Таймер для typing-индикатора
    screen._typing_timer = QTimer()
    screen._typing_timer.timeout.connect(screen._update_typing_indicator)
    screen._typing_timer.start(1000)

    # ---- Строка ввода: glass-стиль ----
    input_row = QWidget()
    input_row.setObjectName("InputRow")
    input_row.setStyleSheet(f"""
        #InputRow {{
            background: {PALETTE.surface_1};
            border-top: 1px solid {PALETTE.border_soft};
        }}
    """)
    row_layout = QVBoxLayout(input_row)
    row_layout.setContentsMargins(16, 10, 16, 12)
    row_layout.setSpacing(8)

    # Reply bar (скрыт по умолчанию)
    screen._reply_bar = QWidget()
    screen._reply_bar.setObjectName("ReplyBar")
    screen._reply_bar.setStyleSheet(f"""
        #ReplyBar {{
            background: {PALETTE.surface_2};
            border: 1px solid {PALETTE.border_soft};
            border-left: 3px solid {PALETTE.accent};
            border-radius: {RADIUS}px;
        }}
    """)
    screen._reply_bar.setVisible(False)
    reply_layout = QHBoxLayout(screen._reply_bar)
    reply_layout.setContentsMargins(12, 8, 8, 8)
    reply_layout.setSpacing(8)

    screen._reply_label = QLabel("")
    screen._reply_label.setStyleSheet(
        f"color: {PALETTE.text_secondary}; font-size: 12px;")
    screen._reply_label.setWordWrap(True)
    reply_layout.addWidget(screen._reply_label, 1)

    cancel_reply_btn = screen._glass_btn(
        "✕", width=28, height=28, radius=RADIUS_SM, font_size=13, padding="4px"
    )
    cancel_reply_btn.clicked.connect(screen._cancel_reply)
    reply_layout.addWidget(cancel_reply_btn)

    row_layout.addWidget(screen._reply_bar)

    # Поле ввода + кнопка отправки
    inner = QHBoxLayout()
    inner.setSpacing(8)

    screen._input = PasteImageLineEdit()
    screen._input.image_pasted.connect(screen._on_image_pasted)
    screen._input.setPlaceholderText("Написать сообщение…  ·  !snake — открыть игру")
    screen._input.setFixedHeight(40)
    screen._input.setStyleSheet(f"""
        QLineEdit {{
            background: {PALETTE.surface_3};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 0 14px;
            color: {PALETTE.text_primary};
            font-size: {FONT_SIZE_BODY}px;
        }}
        QLineEdit:focus {{
            border: 1px solid {PALETTE.accent};
            background: {PALETTE.surface_4};
        }}
    """)
    screen._input.returnPressed.connect(screen._on_send)
    # Escape для отмены редактирования/ответа
    screen._escape_shortcut = QShortcut(Qt.Key.Key_Escape, screen)
    screen._escape_shortcut.activated.connect(screen._cancel_modes)
    screen._search_shortcut = QShortcut("Ctrl+Shift+F", screen)
    screen._search_shortcut.activated.connect(screen._focus_search)

    inner.addWidget(screen._input, 1)

    # Кнопка отправки — accent с hover-glow
    screen._send_btn = QPushButton("➤  Отправить")
    screen._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    screen._send_btn.setFixedHeight(40)
    screen._send_btn.setFixedWidth(120)
    screen._send_btn.setStyleSheet(f"""
        QPushButton {{
            background: {PALETTE.accent};
            color: {PALETTE.text_on_accent};
            border: none;
            border-radius: {RADIUS}px;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {PALETTE.accent_hover};
        }}
        QPushButton:pressed {{
            background: {PALETTE.accent_press};
        }}
    """)
    screen._send_btn.clicked.connect(screen._on_send)
    inner.addWidget(screen._send_btn)

    # Кнопка отмены редактирования (скрыта)
    screen._cancel_edit_btn = screen._glass_btn(
        "✕", width=40, height=40, radius=RADIUS_SM, font_size=13, padding="4px"
    )
    screen._cancel_edit_btn.setVisible(False)
    screen._cancel_edit_btn.clicked.connect(screen._cancel_edit)
    inner.addWidget(screen._cancel_edit_btn)

    row_layout.addLayout(inner)
    chat_layout.addWidget(input_row)

    root.addWidget(chat_container, 1)

    screen._input.textEdited.connect(screen._on_input_text_edited)

    # Активируем пустую страницу общего чата — делегирующие свойства
    # (_feed_layout/_message_widgets/...) всегда должны иметь текущую.
    screen._pages.activate(None)

    screen._show_empty_state()
