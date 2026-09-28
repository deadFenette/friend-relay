"""
Экран профиля пользователя.

Структура:
  - Профиль-карточка (большая аватарка + имя + статус + online-индикатор)
  - О себе (bio + статус-сообщение)
  - Друзья (список с онлайн-индикаторами + добавление/удаление)
  - Статистика игр (ELO, победы/поражения/ничьи, место в рейтинге)
  - Внешний вид (превью текста + выбор шрифта и размера)
  - Информация об аккаунте (имя, URL сервера)

Особенности:
  - Bio и Status сохраняются на сервере (relay profiles/{name}.json)
  - Список друзей тянется с сервера, онлайн-статус в реальном времени
  - Статистика игр берётся из ChessBot ELO leaderboard
  - QComboBox кастомизирован (тёмный popup, белый-на-белом побеждён)
  - Превью шрифта обновляется в реальном времени при выборе
  - Карточки с subtle border + glass background
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.client_core.social import (
    FRIENDS_POLL_INTERVAL_MS,
    STATS_POLL_INTERVAL_MS,
    avatar_size_error,
    counter_state,
    member_since_text,
    my_stats_from_leaderboard,
    status_bubble,
    validate_display_name,
)
from lib.constants import (
    APP_TITLE,
    MAX_BIO_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_STATUS_LENGTH,
)
from lib.storage import load_settings, save_settings
from qt_app.async_bridge import AsyncBridge
from qt_app.theme import PALETTE, RADIUS, RADIUS_LG
from qt_app.widgets.avatar_widget import AvatarWidget
from qt_app.widgets.glass_button import GlassButton
from qt_app.widgets.level_meter import LevelMeter
from voice import list_audio_devices
from voice.devices import HAS_AUDIO as _HAS_AUDIO

# ── Доступные шрифты для выпадашки ──────────────────────────────────

_FONT_OPTIONS = [
    "Segoe UI",
    "Arial",
    "Tahoma",
    "Verdana",
    "Times New Roman",
    "Georgia",
    "Courier New",
    "Consolas",
    "Roboto",
    "Ubuntu",
]


def _field_qss() -> str:
    """Стили для инпутов — включает тёмный popup для QComboBox,
    который на Qt6/Linux по умолчанию белый на белом.
    """
    return f"""
        QLineEdit, QSpinBox, QComboBox, QTextEdit {{
            background: {PALETTE.surface_3};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 8px 12px;
            color: {PALETTE.text_primary};
            min-height: 22px;
        }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
            border: 1px solid {PALETTE.accent};
            background: {PALETTE.surface_4};
        }}

        /* Стрелочка QComboBox — кастомная, т.к. дефолтная на тёмном фоне почти невидима */
        QComboBox::drop-down {{
            border: none;
            width: 24px;
        }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {PALETTE.text_secondary};
            margin-right: 8px;
        }}
        QComboBox::down-arrow:on {{
            border-top: 5px solid {PALETTE.accent};
        }}

        /* Popup список — вот тут фиксился баг «белый на белом». */
        QComboBox QAbstractItemView {{
            background: {PALETTE.surface_2};
            color: {PALETTE.text_primary};
            border: 1px solid {PALETTE.border};
            border-radius: {RADIUS}px;
            padding: 4px;
            outline: 0;
            selection-background-color: {PALETTE.accent};
            selection-color: {PALETTE.text_on_accent};
        }}
        QComboBox QAbstractItemView::item {{
            background: transparent;
            color: {PALETTE.text_primary};
            padding: 6px 12px;
            min-height: 24px;
            border-radius: {RADIUS - 2}px;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background: {PALETTE.surface_3};
        }}
    """


def _section_card() -> str:
    """QSS для карточки-секции: subtle border + лёгкий фон."""
    return f"""
        QFrame#SectionCard {{
            background: {PALETTE.surface_1};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS_LG}px;
        }}
    """


def _slider_qss() -> str:
    """Горизонтальный QSlider в стиле приложения (как в диалоге голоса)."""
    return f"""
        QSlider::groove:horizontal {{
            height: 4px; background: {PALETTE.surface_3};
            border-radius: 2px;
        }}
        QSlider::sub-page:horizontal {{
            background: {PALETTE.accent}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {PALETTE.text_secondary}; width: 12px;
            height: 12px; margin: -5px 0; border-radius: 6px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {PALETTE.text_primary};
        }}
    """


def _checkbox_qss() -> str:
    """QCheckBox с читаемым индикатором."""
    return f"""
        QCheckBox {{
            color: {PALETTE.text_secondary}; font-size: 12px;
            spacing: 8px; background: transparent;
        }}
        QCheckBox:hover {{ color: {PALETTE.text_primary}; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {PALETTE.border};
            border-radius: 4px; background: {PALETTE.surface_3};
        }}
        QCheckBox::indicator:checked {{
            background: {PALETTE.accent}; border-color: {PALETTE.accent};
        }}
        QCheckBox::indicator:hover {{
            border-color: {PALETTE.border_strong};
        }}
    """


class ProfileSettingsScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()
        self.settings = load_settings()
        self._base_url = ""
        self._my_name = ""
        self._access_key = ""
        self._connected = False
        # Кэш текущего профиля с сервера (чтобы не перезатереть при save)
        self._current_profile: dict | None = None

        # Главный scroll (контента стало много — bio, друзья, статистика)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        scroll.setWidget(inner)

        root = QVBoxLayout(inner)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(18)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── Заголовок ──────────────────────────────────────────────
        title = QLabel("Профиль")
        title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 22px; font-weight: 700;"
        )
        root.addWidget(title)

        subtitle = QLabel(
            "Аватар, о себе, друзья, статистика, звук и уведомления"
        )
        subtitle.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 13px;"
        )
        root.addWidget(subtitle)

        # ── Карточка профиля (большая) ─────────────────────────────
        root.addWidget(self._build_profile_card())

        # ── О себе (bio + status) ──────────────────────────────────
        root.addWidget(self._build_about_card())

        # ── Друзья ─────────────────────────────────────────────────
        root.addWidget(self._build_friends_card())

        # ── Статистика игр ─────────────────────────────────────────
        root.addWidget(self._build_stats_card())

        # ── Карточка внешнего вида (шрифты) ────────────────────────
        root.addWidget(self._build_appearance_card())

        # ── Звук и голос: устройства записи/воспроизведения (v1.9.6) ─
        root.addWidget(self._build_voice_card())

        # ── Уведомления (v1.9.6) ──────────────────────────────────
        root.addWidget(self._build_notifications_card())

        # ── Карточка подключения ───────────────────────────────────
        root.addWidget(self._build_connection_card())

        # ── Кнопка сохранения ──────────────────────────────────────
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_btn = GlassButton("💾  Сохранить")
        save_btn.setFixedWidth(180)
        save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(save_btn)
        root.addLayout(save_row)

        # Оборачиваем scroll в главный layout
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # Таймер периодического обновления друзей (online-статус)
        self._friends_poll_timer = QTimer(self)
        self._friends_poll_timer.timeout.connect(self._refresh_friends)
        self._friends_poll_interval = FRIENDS_POLL_INTERVAL_MS  # 10 секунд

        # Таймер обновления статистики (ELO может меняться после партий)
        self._stats_poll_timer = QTimer(self)
        self._stats_poll_timer.timeout.connect(self._refresh_stats)
        self._stats_poll_interval = STATS_POLL_INTERVAL_MS  # 30 секунд

        # Загружаем текущую аватарку (если есть base_url)
        self._load_current_avatar()

    # ── Карточки ──────────────────────────────────────────────────────

    def _build_profile_card(self) -> QFrame:
        """Большая карточка с аватаром 96px + именем + статусом + online."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QHBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        # Аватар
        self._avatar_widget = AvatarWidget(self.settings.get("name", "User"), size=96)
        layout.addWidget(self._avatar_widget)

        # Инфо
        info = QVBoxLayout()
        info.setSpacing(4)

        # Имя (display_name) + кнопка смены. Логин показывается мелкой
        # припиской, когда отображаемое имя отличается от имени входа.
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._name_label = QLabel(self.settings.get("name", "User") or "—")
        self._name_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 18px; font-weight: 700;"
        )
        name_row.addWidget(self._name_label)
        self._rename_btn = GlassButton("✏")
        self._rename_btn.setFixedWidth(32)
        self._rename_btn.setToolTip("Изменить имя (как тебя видят другие)")
        self._rename_btn.clicked.connect(self._rename_display_name)
        name_row.addWidget(self._rename_btn)
        name_row.addStretch(1)
        info.addLayout(name_row)

        self._login_hint_label = QLabel("")
        self._login_hint_label.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        info.addWidget(self._login_hint_label)

        # Статус-сообщение (берётся с сервера, не путать с connection status)
        self._status_message_label = QLabel("")
        self._status_message_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; font-style: italic;"
        )
        self._status_message_label.setWordWrap(True)
        info.addWidget(self._status_message_label)

        self._conn_label = QLabel("●  Не подключено")
        self._conn_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        info.addWidget(self._conn_label)

        info.addSpacing(8)

        # Кнопки загрузки/удаления аватара
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)

        upload_btn = GlassButton("📤  Загрузить")
        upload_btn.clicked.connect(self._upload_avatar)
        buttons_row.addWidget(upload_btn)

        remove_btn = GlassButton("🗑  Удалить")
        remove_btn.clicked.connect(self._remove_avatar)
        buttons_row.addWidget(remove_btn)

        buttons_row.addStretch(1)
        info.addLayout(buttons_row)

        layout.addLayout(info, 1)
        return card

    def _build_about_card(self) -> QFrame:
        """Карточка 'О себе': bio (многострочное) + status (однострочное)."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        header = QLabel("📝  О себе")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        layout.addWidget(header)

        # Status (короткое, однострочное)
        status_label = QLabel(f"Статус (до {MAX_STATUS_LENGTH} симв.)")
        status_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        layout.addWidget(status_label)

        self._status_input = QLineEdit()
        self._status_input.setPlaceholderText("Напр. «В игре» или «На связи»")
        self._status_input.setMaxLength(MAX_STATUS_LENGTH)
        self._status_input.setStyleSheet(_field_qss())
        self._status_input.textChanged.connect(self._on_status_changed)
        layout.addWidget(self._status_input)

        # Счётчик символов status
        self._status_counter = QLabel(f"0 / {MAX_STATUS_LENGTH}")
        self._status_counter.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        self._status_counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._status_counter)

        # Bio (многострочное)
        bio_label = QLabel(f"О себе (до {MAX_BIO_LENGTH} симв.)")
        bio_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        layout.addWidget(bio_label)

        self._bio_input = QTextEdit()
        self._bio_input.setPlaceholderText("Расскажите о себе: любимые игры, хобби, что угодно…")
        self._bio_input.setMaximumHeight(90)
        self._bio_input.setStyleSheet(_field_qss())
        self._bio_input.textChanged.connect(self._on_bio_changed)
        layout.addWidget(self._bio_input)

        # Счётчик символов bio
        self._bio_counter = QLabel(f"0 / {MAX_BIO_LENGTH}")
        self._bio_counter.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        self._bio_counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._bio_counter)

        return card

    def _build_friends_card(self) -> QFrame:
        """Карточка друзей: список + кнопка добавления."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Заголовок + кнопка добавить
        header_row = QHBoxLayout()
        header = QLabel("👥  Друзья")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        header_row.addWidget(header)
        header_row.addStretch(1)

        add_btn = GlassButton("＋  Добавить")
        add_btn.clicked.connect(self._add_friend_dialog)
        header_row.addWidget(add_btn)
        layout.addLayout(header_row)

        # Контейнер для списка друзей (вертикальный layout, динамически
        # перезаполняется при обновлении).
        self._friends_container = QVBoxLayout()
        self._friends_container.setSpacing(4)
        layout.addLayout(self._friends_container)

        # Плейсхолдер «пока нет друзей»
        self._friends_empty = QLabel("Пока нет друзей. Нажмите «＋ Добавить».")
        self._friends_empty.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 12px; font-style: italic; padding: 8px;"
        )
        layout.addWidget(self._friends_empty)

        return card

    def _build_stats_card(self) -> QFrame:
        """Карточка статистики игр: ELO, победы/поражения/ничьи, место."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("♟  Статистика шахмат")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        layout.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        # ELO
        grid.addWidget(self._info_label("ELO рейтинг"), 0, 0)
        self._elo_label = QLabel("—")
        self._elo_label.setStyleSheet(
            f"color: {PALETTE.accent}; font-family: 'Consolas, monospace'; font-size: 18px; font-weight: 700;"
        )
        grid.addWidget(self._elo_label, 0, 1)

        # Место
        grid.addWidget(self._info_label("Место в рейтинге"), 1, 0)
        self._rank_label = QLabel("—")
        self._rank_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-family: 'Consolas, monospace'; font-size: 13px;"
        )
        grid.addWidget(self._rank_label, 1, 1)

        # Победы / Поражения / Ничьи
        grid.addWidget(self._info_label("Победы"), 2, 0)
        self._wins_label = QLabel("0")
        self._wins_label.setStyleSheet(
            f"color: {PALETTE.success}; font-family: 'Consolas, monospace'; font-size: 13px; font-weight: 600;"
        )
        grid.addWidget(self._wins_label, 2, 1)

        grid.addWidget(self._info_label("Поражения"), 3, 0)
        self._losses_label = QLabel("0")
        self._losses_label.setStyleSheet(
            f"color: {PALETTE.danger}; font-family: 'Consolas, monospace'; font-size: 13px; font-weight: 600;"
        )
        grid.addWidget(self._losses_label, 3, 1)

        grid.addWidget(self._info_label("Ничьи"), 4, 0)
        self._draws_label = QLabel("0")
        self._draws_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-family: 'Consolas, monospace'; font-size: 13px; font-weight: 600;"
        )
        grid.addWidget(self._draws_label, 4, 1)

        layout.addLayout(grid)

        # Кнопка «обновить статистику»
        refresh_row = QHBoxLayout()
        refresh_row.addStretch(1)
        refresh_btn = GlassButton("↻  Обновить")
        refresh_btn.clicked.connect(self._refresh_stats)
        refresh_row.addWidget(refresh_btn)
        layout.addLayout(refresh_row)

        return card

    def _build_appearance_card(self) -> QFrame:
        """Карточка с превью шрифта и выбором размера/семейства."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        header = QLabel("🎨  Внешний вид")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        layout.addWidget(header)

        # Превью текста — обновляется в реальном времени
        preview_label = QLabel("Aa Бб 123  Sample text — превью шрифта")
        self._font_preview_label = preview_label
        preview_label.setStyleSheet(
            f"""
            background: {PALETTE.surface_2};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 14px 18px;
            color: {PALETTE.text_primary};
            font-size: {self.settings.get('font_size', 13)}px;
            font-family: '{self.settings.get('font_family', 'Segoe UI')}';
            """
        )
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(preview_label)

        # Выбор шрифта + размера
        form_row = QHBoxLayout()
        form_row.setSpacing(12)

        # Семейство
        family_col = QVBoxLayout()
        family_col.setSpacing(6)
        family_label = QLabel("Шрифт")
        family_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        family_col.addWidget(family_label)

        self._font_family_input = QComboBox()
        self._font_family_input.addItems(_FONT_OPTIONS)
        self._font_family_input.setStyleSheet(_field_qss())
        current_font = self.settings.get("font_family", "Segoe UI")
        idx = self._font_family_input.findText(current_font)
        if idx >= 0:
            self._font_family_input.setCurrentIndex(idx)
        self._font_family_input.currentTextChanged.connect(self._update_font_preview)
        family_col.addWidget(self._font_family_input)
        form_row.addLayout(family_col, 2)

        # Размер
        size_col = QVBoxLayout()
        size_col.setSpacing(6)
        size_label = QLabel("Размер, px")
        size_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        size_col.addWidget(size_label)

        self._font_size_input = QSpinBox()
        self._font_size_input.setRange(8, 24)
        self._font_size_input.setValue(int(self.settings.get("font_size", 13)))
        self._font_size_input.setStyleSheet(_field_qss())
        self._font_size_input.valueChanged.connect(self._update_font_preview)
        size_col.addWidget(self._font_size_input)
        form_row.addLayout(size_col, 1)

        layout.addLayout(form_row)
        return card

    def _build_connection_card(self) -> QFrame:
        """Карточка с информацией о подключении (read-only)."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("🔌  Подключение")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        layout.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        # Имя пользователя
        grid.addWidget(self._info_label("Имя"), 0, 0)
        self._account_name_label = QLabel("—")
        self._account_name_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-family: 'Consolas, monospace'; font-size: 13px;"
        )
        grid.addWidget(self._account_name_label, 0, 1)

        # URL сервера
        grid.addWidget(self._info_label("Сервер"), 1, 0)
        self._server_url_label = QLabel("—")
        self._server_url_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-family: 'Consolas, monospace'; font-size: 13px;"
        )
        self._server_url_label.setWordWrap(True)
        grid.addWidget(self._server_url_label, 1, 1)

        # Статус подключения
        grid.addWidget(self._info_label("Статус"), 2, 0)
        self._conn_status_label = QLabel("●  Не подключено")
        self._conn_status_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 13px;"
        )
        grid.addWidget(self._conn_status_label, 2, 1)

        # Дата регистрации (с сервера profile)
        grid.addWidget(self._info_label("На сервере с"), 3, 0)
        self._member_since_label = QLabel("—")
        self._member_since_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-family: 'Consolas, monospace'; font-size: 13px;"
        )
        grid.addWidget(self._member_since_label, 3, 1)

        layout.addLayout(grid)
        return card

    def _info_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; font-weight: 600;"
        )
        return lbl

    # ── Звук и голос (v1.9.6) ───────────────────────────────────

    def _build_voice_card(self) -> QFrame:
        """Карточка настроек голоса: микрофон, динамик, громкость,
        шумоподавление + проверка микрофона с живым метром уровня.

        Те же ключи настроек, что и в диалоге голосового канала — меняются
        в любом из двух мест, применяются сразу (сохранение на каждый чих,
        как в Discord)."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("🎧  Звук и голос")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        header_row.addWidget(header)
        header_row.addStretch(1)

        refresh_btn = GlassButton("↻")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("Пересканировать аудиоустройства (воткнул наушники?)")
        refresh_btn.clicked.connect(lambda: self._fill_voice_combos())
        header_row.addWidget(refresh_btn)
        layout.addLayout(header_row)

        if not _HAS_AUDIO:
            hint = QLabel(
                "⚠  Библиотека звука не установлена (pip install sounddevice).\n"
                "Выбор устройств появится после установки."
            )
            hint.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 11px; padding: 8px;"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            return card

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        # Микрофон (устройство записи)
        grid.addWidget(self._info_label("🎤 Микрофон (запись)"), 0, 0)
        self._mic_combo = QComboBox()
        self._mic_combo.setToolTip("С какого устройства пишется твой голос")
        self._mic_combo.setStyleSheet(_field_qss())
        self._mic_combo.activated.connect(self._on_voice_device_changed)
        grid.addWidget(self._mic_combo, 0, 1)

        # Динамик (устройство воспроизведения)
        grid.addWidget(self._info_label("🔊 Динамик (воспроизведение)"), 1, 0)
        self._spk_combo = QComboBox()
        self._spk_combo.setToolTip("Куда идёт звук собеседников")
        self._spk_combo.setStyleSheet(_field_qss())
        self._spk_combo.activated.connect(self._on_voice_device_changed)
        grid.addWidget(self._spk_combo, 1, 1)

        layout.addLayout(grid)

        # Громкость вывода
        vol_row = QHBoxLayout()
        vol_lbl = QLabel("Громкость собеседников")
        vol_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; font-weight: 600;"
        )
        vol_row.addWidget(vol_lbl)
        self._vol_value_label = QLabel("100%")
        self._vol_value_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
            f"font-family: 'Consolas, monospace';"
        )
        vol_row.addStretch(1)
        vol_row.addWidget(self._vol_value_label)
        layout.addLayout(vol_row)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 200)
        self._vol_slider.setValue(
            int(float(self.settings.get("voice_output_volume", 1.0)) * 100)
        )
        self._vol_slider.setStyleSheet(_slider_qss())
        self._vol_slider.setToolTip(
            "Громкость голоса в наушниках/динамиках (не трогает системную)"
        )
        self._vol_slider.valueChanged.connect(self._on_voice_volume_changed)
        layout.addWidget(self._vol_slider)

        # Шумоподавление
        self._ns_check = QCheckBox("Шумоподавление микрофона (гул/шум/гейт)")
        self._ns_check.setChecked(
            bool(self.settings.get("voice_noise_suppression", True))
        )
        self._ns_check.setToolTip(
            "Фильтр гула 50 Гц и гейт тишины в паузах.\n"
            "Выключи для микрофонов со своим DSP — двойная обработка портит голос."
        )
        self._ns_check.setStyleSheet(_checkbox_qss())
        self._ns_check.toggled.connect(self._on_ns_toggled)
        layout.addWidget(self._ns_check)

        # Проверка микрофона: 3с записи с живым метром — без сервера
        test_row = QHBoxLayout()
        self._mic_test_btn = GlassButton("🎙  Проверить микрофон")
        self._mic_test_btn.setToolTip(
            "3 секунды записи с выбранного микрофона — сразу видно, слышит ли он тебя"
        )
        self._mic_test_btn.clicked.connect(self._run_mic_test)
        test_row.addWidget(self._mic_test_btn)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        meter_row = QHBoxLayout()
        meter_row.addWidget(self._info_label("Уровень"))
        self._mic_meter = LevelMeter()
        meter_row.addWidget(self._mic_meter, 1)
        layout.addLayout(meter_row)

        self._mic_test_result = QLabel("")
        self._mic_test_result.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px;"
        )
        self._mic_test_result.setWordWrap(True)
        layout.addWidget(self._mic_test_result)

        self._fill_voice_combos()
        return card

    def _fill_voice_combos(self) -> None:
        """Заполняет комбобоксы устройств (и «По умолчанию» первым)."""
        devices = list_audio_devices()
        cur_in = self.settings.get("voice_input_device", "")
        cur_out = self.settings.get("voice_output_device", "")

        for combo, items, cur in (
            (self._mic_combo, devices["inputs"], cur_in),
            (self._spk_combo, devices["outputs"], cur_out),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("По умолчанию (системное)", "")
            for d in items:
                label = d["name"] if len(d["name"]) <= 36 else d["name"][:35] + "…"
                combo.addItem(label, d["name"])
            idx = combo.findData(cur)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)

    def _on_voice_device_changed(self) -> None:
        """Смена устройства записи/воспроизведения — сразу в настройки."""
        self.settings["voice_input_device"] = self._mic_combo.currentData() or ""
        self.settings["voice_output_device"] = self._spk_combo.currentData() or ""
        save_settings(self.settings)

    def _on_voice_volume_changed(self, value: int) -> None:
        vol = value / 100.0
        self._vol_value_label.setText(f"{value}%")
        self.settings["voice_output_volume"] = vol
        save_settings(self.settings)

    def _on_ns_toggled(self, checked: bool) -> None:
        self.settings["voice_noise_suppression"] = checked
        save_settings(self.settings)

    # ── Проверка микрофона ──────────────────────────────────────

    def _run_mic_test(self) -> None:
        """3 секунды записи с выбранного микрофона, живой метр уровня.

        Работает БЕЗ сервера: сразу видно «уровень есть/тишина/не открылся».
        Если открыт голосовой канал — не трогаем устройства (заняты)."""
        import threading

        if self._mic_test_running:
            return
        dev_name = self._mic_combo.currentData() or ""
        self._mic_test_running = True
        self._mic_test_btn.setEnabled(False)
        self._mic_test_result.setText("Слушаю 3 секунды… скажи что-нибудь 🎙")
        self._mic_test_result.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        self._mic_peak = 0.0
        self._mic_level = 0.0

        result: dict = {}

        def _worker():
            try:
                import sounddevice as sd

                device = None
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if (d.get("name") or "").strip() == dev_name and int(
                        d.get("max_input_channels") or 0
                    ) > 0:
                        device = i
                        break

                def _cb(indata, frames, t, status):
                    import numpy as _np

                    arr = _np.frombuffer(
                        indata.tobytes(), dtype="int16"
                    ).astype(_np.float32)
                    rms = float(_np.sqrt(_np.mean(arr ** 2))) if arr.size else 0.0
                    self._mic_level = min(1.0, rms / 1800.0)
                    if rms > self._mic_peak:
                        self._mic_peak = rms

                kwargs = {"device": device} if device is not None else {}
                import time

                with sd.InputStream(
                    samplerate=16000, channels=1, dtype="int16",
                    blocksize=160, callback=_cb, **kwargs,
                ):
                    time.sleep(3.0)
                result["ok"] = True
            except Exception as e:
                result["ok"] = False
                result["err"] = str(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        def _tick():
            self._mic_meter.set_level(self._mic_level)
            if t.is_alive():
                QTimer.singleShot(80, _tick)
            else:
                self._finish_mic_test(result)

        _tick()

    _mic_test_running = False
    _mic_peak = 0.0
    _mic_level = 0.0

    def _finish_mic_test(self, result: dict) -> None:
        self._mic_test_running = False
        self._mic_test_btn.setEnabled(True)
        self._mic_meter.set_level(0.0)
        if not result.get("ok"):
            self._mic_test_result.setText(
                f"⚠ Не удалось открыть микрофон: {result.get('err', '?')}"
            )
            self._mic_test_result.setStyleSheet(
                f"color: {PALETTE.danger}; font-size: 11px;"
            )
            return
        peak = self._mic_peak
        if peak < 60:
            self._mic_test_result.setText(
                "🔇 Тишина: микрофон открыт, но ничего не слышно.\n"
                "Проверь, что выбрано верное устройство и оно не замьючено в системе."
            )
            self._mic_test_result.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 11px;"
            )
        elif peak > 12000:
            self._mic_test_result.setText(
                f"✓ Микрофон работает (уровень высокий — {int(peak)},\n"
                "говори тише или отодвинь микрофон)."
            )
            self._mic_test_result.setStyleSheet(
                f"color: {PALETTE.success}; font-size: 11px;"
            )
        else:
            self._mic_test_result.setText(
                f"✓ Микрофон работает — уровень нормальный ({int(peak)})."
            )
            self._mic_test_result.setStyleSheet(
                f"color: {PALETTE.success}; font-size: 11px;"
            )

    # ── Уведомления (v1.9.6) ────────────────────────────────────

    def _build_notifications_card(self) -> QFrame:
        """Звук «динь» на новые сообщения (когда окно не в фокусе)."""
        card = QFrame()
        card.setObjectName("SectionCard")
        card.setStyleSheet(_section_card())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("🔔  Уведомления")
        header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 15px; font-weight: 700;"
        )
        layout.addWidget(header)

        self._sound_check = QCheckBox("Звук при новом сообщении (окно не в фокусе)")
        self._sound_check.setChecked(bool(self.settings.get("sound_on_message", True)))
        self._sound_check.setToolTip(
            "Мягкий «динь», когда кто-то пишет, а окно свёрнуто/неактивно.\n"
            "В фокусе звук не играет — не бесит."
        )
        self._sound_check.setStyleSheet(_checkbox_qss())
        self._sound_check.toggled.connect(self._on_sound_settings_changed)
        layout.addWidget(self._sound_check)

        vol_row = QHBoxLayout()
        vol_lbl = QLabel("Громкость уведомления")
        vol_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; font-weight: 600;"
        )
        vol_row.addWidget(vol_lbl)
        self._notif_vol_value = QLabel("50%")
        self._notif_vol_value.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
            f"font-family: 'Consolas, monospace';"
        )
        vol_row.addStretch(1)
        vol_row.addWidget(self._notif_vol_value)
        layout.addLayout(vol_row)

        self._notif_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._notif_vol_slider.setRange(0, 100)
        self._notif_vol_slider.setValue(
            int(float(self.settings.get("sound_volume", 0.5)) * 100)
        )
        self._notif_vol_slider.setStyleSheet(_slider_qss())
        self._notif_vol_slider.setToolTip("Громкость «динь» уведомления")
        self._notif_vol_slider.valueChanged.connect(self._on_sound_settings_changed)
        layout.addWidget(self._notif_vol_slider)

        test_row = QHBoxLayout()
        test_btn = GlassButton("♪  Проверить звук")
        test_btn.setToolTip("Проиграть уведомление с текущей громкостью")
        test_btn.clicked.connect(self._test_notification_sound)
        test_row.addWidget(test_btn)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        return card

    def _on_sound_settings_changed(self) -> None:
        vol = self._notif_vol_slider.value() / 100.0
        self._notif_vol_value.setText(f"{self._notif_vol_slider.value()}%")
        self.settings["sound_on_message"] = self._sound_check.isChecked()
        self.settings["sound_volume"] = vol
        save_settings(self.settings)

    def _test_notification_sound(self) -> None:
        from qt_app.sound import play_message_sound

        self._on_sound_settings_changed()
        play_message_sound(float(self.settings.get("sound_volume", 0.5)))

    # ── Логика ────────────────────────────────────────────────────────

    def set_connection(self, base_url: str, my_name: str, access_key: str, connected: bool) -> None:
        """Устанавливает параметры подключения и обновляет UI."""
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key
        self._connected = connected

        # Обновляем карточки (имя при подключении — логин; после загрузки
        # профиля _refresh_profile заменит на display_name, если он задан)
        if my_name:
            self._name_label.setText(my_name)
            self._login_hint_label.setText("")
            self._account_name_label.setText(my_name)
        if base_url:
            self._server_url_label.setText(base_url)
        else:
            self._server_url_label.setText("—")

        if connected:
            self._conn_label.setText("●  В сети")
            self._conn_label.setStyleSheet(
                f"color: {PALETTE.success}; font-size: 12px; font-weight: 600;"
            )
            self._conn_status_label.setText("●  В сети")
            self._conn_status_label.setStyleSheet(
                f"color: {PALETTE.success}; font-size: 13px; font-weight: 600;"
            )
            self._load_current_avatar()
            # Загружаем профиль с сервера (bio, status, friends, stats)
            self._refresh_profile()
            self._refresh_friends()
            self._refresh_stats()
            # Запускаем таймеры периодического обновления
            if not self._friends_poll_timer.isActive():
                self._friends_poll_timer.start(self._friends_poll_interval)
            if not self._stats_poll_timer.isActive():
                self._stats_poll_timer.start(self._stats_poll_interval)
        else:
            self._conn_label.setText("●  Не подключено")
            self._conn_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px;"
            )
            self._conn_status_label.setText("●  Не подключено")
            self._conn_status_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 13px;"
            )
            # Останавливаем таймеры когда не подключены
            self._friends_poll_timer.stop()
            self._stats_poll_timer.stop()

    # ── Загрузка профиля с сервера ────────────────────────────────────

    def _refresh_profile(self) -> None:
        """Тянет профиль с сервера и заполняет поля bio/status/created_at."""
        if not self._base_url or not self._my_name:
            return

        def _do():
            return client.get_profile(self._base_url, self._my_name, self._access_key)

        def _on_ok(profile):
            if not profile:
                return
            self._current_profile = profile

            # Имя в карточке: display_name (то, что видят другие в друзьях
            # и профиле); если не задан или совпадает с логином — просто логин.
            dn = (profile.get("display_name") or "").strip()
            if dn and dn != self._my_name:
                self._name_label.setText(dn)
                self._login_hint_label.setText(f"логин: {self._my_name}")
            else:
                self._name_label.setText(self._my_name)
                self._login_hint_label.setText("")

            # Заполняем поля БЕЗ триггера textChanged (чтобы не дёргать сервер)
            self._status_input.blockSignals(True)
            self._status_input.setText(profile.get("status", ""))
            self._status_input.blockSignals(False)
            self._on_status_changed()  # обновим счётчик

            self._bio_input.blockSignals(True)
            self._bio_input.setText(profile.get("bio", ""))
            self._bio_input.blockSignals(False)
            self._on_bio_changed()

            # Статус-сообщение в карточке профиля
            self._status_message_label.setText(status_bubble(profile.get("status", "")))

            # Дата регистрации
            since = member_since_text(profile.get("created_at", 0))
            if since:
                self._member_since_label.setText(since)

        self._bridge.run(_do, on_success=_on_ok, on_error=lambda e: None)

    def _rename_display_name(self) -> None:
        """Диалог смены отображаемого имени (display_name).

        Имя хранится на сервере (profiles/{login}.json), поэтому смена
        доступна только при подключении. Логин (имя входа) меняется в
        Настройках — поле «Имя» до нажатия «Подключиться».
        """
        if not self._connected or not self._base_url or not self._my_name:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Смена имени работает при подключении.\n\n"
                "Имя входа (логин) меняется в Настройках — поле «Имя» — "
                "и применяется при следующем подключении.",
            )
            return

        current = (self._current_profile or {}).get("display_name") or self._my_name
        text, ok = QInputDialog.getText(
            self,
            "Изменить имя",
            f"Новое имя (до {MAX_DISPLAY_NAME_LENGTH} символов):",
            text=current,
        )
        if not ok:
            return
        normalized = validate_display_name(text)
        if normalized is None:
            QMessageBox.warning(self, APP_TITLE, "Имя не может быть пустым")
            return

        new_name = normalized  # замыкание для воркера

        def _do():
            return client.update_profile(
                self._base_url,
                self._my_name,
                display_name=new_name,
                access_key=self._access_key,
            )

        def _on_ok(profile):
            if profile:
                self._current_profile = profile
            # Карточку обновляем из того, что реально сохранил сервер
            saved = (profile or {}).get("display_name") or new_name
            if saved != self._my_name:
                self._name_label.setText(saved)
                self._login_hint_label.setText(f"логин: {self._my_name}")
            else:
                self._name_label.setText(self._my_name)
                self._login_hint_label.setText("")
            # Друзья тянут display_name из профилей — обновим их карточки
            self._refresh_friends()
            QMessageBox.information(
                self, "Готово", f"Имя обновлено: {saved}"
            )

        def _on_err(err):
            QMessageBox.warning(
                self, "Не получилось", f"Не удалось сменить имя: {err}"
            )

        self._bridge.run(_do, on_success=_on_ok, on_error=_on_err)

    def _refresh_friends(self) -> None:
        """Тянет список друзей с сервера и обновляет карточку."""
        if not self._base_url or not self._my_name:
            return

        def _do():
            return client.get_friends(self._base_url, self._my_name, self._access_key)

        def _on_ok(friends):
            self._render_friends(friends)

        self._bridge.run(_do, on_success=_on_ok, on_error=lambda e: None)

    def _render_friends(self, friends: list[dict]) -> None:
        """Перерисовывает список друзей в карточке."""
        # Очищаем старые виджеты
        while self._friends_container.count():
            item = self._friends_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not friends:
            self._friends_empty.setVisible(True)
            return

        self._friends_empty.setVisible(False)
        for f in friends:
            row = self._build_friend_row(f)
            self._friends_container.addWidget(row)

    def _build_friend_row(self, friend: dict) -> QWidget:
        """Создаёт виджет-строку для одного друга."""
        w = QFrame()
        w.setStyleSheet(f"""
            QFrame {{
                background: {PALETTE.surface_2};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
            }}
            QFrame:hover {{
                border: 1px solid {PALETTE.border};
                background: {PALETTE.surface_3};
            }}
        """)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # Мини-аватар (24px) — используем AvatarWidget
        name = friend.get("name", "?")
        avatar = AvatarWidget(name, size=24)
        if self._base_url:
            # v1.9.5: сначала кеш (список друзей пересобирается каждые 10с —
            # раньше каждый пересбор дёргал N HTTP-запросов аватарок и
            # плейсхолдеры мерцали). Негативный кеш внутри AvatarCache
            # останавливает вечные запросы для без-аватарных юзеров.
            from qt_app.widgets.avatar_widget import AvatarCache

            cached = AvatarCache().get(name)
            if cached:
                avatar.set_avatar_data(cached)
            else:
                avatar.load_avatar(self._base_url, self._access_key)
        layout.addWidget(avatar)

        # Имя + статус
        info = QVBoxLayout()
        info.setSpacing(0)
        name_label = QLabel(friend.get("display_name", name))
        name_label.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 13px; font-weight: 600;"
        )
        info.addWidget(name_label)

        status_text = friend.get("status", "")
        if status_text:
            status_label = QLabel(status_text)
            status_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 11px; font-style: italic;"
            )
            info.addWidget(status_label)

        layout.addLayout(info, 1)

        # Онлайн-индикатор
        online = friend.get("online", False)
        dot = QLabel("●" if online else "○")
        dot.setStyleSheet(
            f"color: {PALETTE.success if online else PALETTE.text_dim}; font-size: 14px;"
        )
        layout.addWidget(dot)

        # Кнопка удаления
        remove_btn = GlassButton("✕")
        remove_btn.setFixedWidth(32)
        remove_btn.setStyleSheet(f"""
            GlassButton {{
                background: transparent;
                color: {PALETTE.text_secondary};
                border: none;
                padding: 4px;
            }}
            GlassButton:hover {{
                color: {PALETTE.danger};
                background: {PALETTE.surface_3};
                border-radius: {RADIUS}px;
            }}
        """)
        remove_btn.clicked.connect(lambda _=False, _n=name: self._remove_friend(_n))
        layout.addWidget(remove_btn)

        return w

    def _refresh_stats(self) -> None:
        """Тянет leaderboard с сервера и обновляет карточку статистики."""
        if not self._base_url or not self._my_name:
            return

        def _do():
            return client.get_leaderboard(self._base_url, self._access_key)

        def _on_ok(scores):
            self._render_stats(scores)

        self._bridge.run(_do, on_success=_on_ok, on_error=lambda e: None)

    def _render_stats(self, scores: list[dict]) -> None:
        """Заполняет ELO/победы/поражения/ничьи/место из leaderboard."""
        stats = my_stats_from_leaderboard(scores, self._my_name)
        self._elo_label.setText(stats.elo)
        self._rank_label.setText(stats.rank)
        self._wins_label.setText(stats.wins)
        self._losses_label.setText(stats.losses)
        self._draws_label.setText(stats.draws)

    # ── Обработчики изменений полей ───────────────────────────────────

    def _on_status_changed(self) -> None:
        text = self._status_input.text()
        label, warn = counter_state(text, MAX_STATUS_LENGTH)
        self._status_counter.setText(label)
        self._status_counter.setStyleSheet(
            f"color: {PALETTE.warning if warn else PALETTE.text_dim}; font-size: 10px;"
        )

    def _on_bio_changed(self) -> None:
        text = self._bio_input.toPlainText()
        label, warn = counter_state(text, MAX_BIO_LENGTH)
        self._bio_counter.setText(label)
        self._bio_counter.setStyleSheet(
            f"color: {PALETTE.warning if warn else PALETTE.text_dim}; font-size: 10px;"
        )

    # ── Друзья: добавление/удаление ───────────────────────────────────

    def _add_friend_dialog(self) -> None:
        """Открывает диалог ввода имени друга и добавляет его."""
        if not self._base_url or not self._my_name:
            QMessageBox.warning(self, "Ошибка", "Сначала подключитесь к серверу")
            return

        name, ok = QInputDialog.getText(
            self, "Добавить друга", "Введите имя пользователя:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        def _do():
            return client.add_friend(self._base_url, self._my_name, name, self._access_key)

        def _on_ok(_friends):
            self._refresh_friends()
            QMessageBox.information(self, "Готово", f"Пользователь {name} добавлен в друзья")

        def _on_err(err):
            QMessageBox.warning(self, "Не получилось", f"Не удалось добавить: {err}")

        self._bridge.run(_do, on_success=_on_ok, on_error=_on_err)

    def _remove_friend(self, name: str) -> None:
        """Удаляет друга по имени (без подтверждения — кнопка маленькая)."""
        if not self._base_url or not self._my_name:
            return

        def _do():
            return client.remove_friend(self._base_url, self._my_name, name, self._access_key)

        def _on_ok(_friends):
            self._refresh_friends()

        def _on_err(err):
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить: {err}")

        self._bridge.run(_do, on_success=_on_ok, on_error=_on_err)

    # ── Сохранение ────────────────────────────────────────────────────

    def _save_settings(self) -> None:
        """Сохраняет локальные настройки (шрифт) + отправляет bio/status
        на сервер."""
        # 1. Локальные настройки шрифта
        self.settings.update(
            {
                "font_size": self._font_size_input.value(),
                "font_family": self._font_family_input.currentText(),
            }
        )
        save_settings(self.settings)

        # 2. Bio + status на сервер (если подключены)
        if self._base_url and self._my_name:
            bio = self._bio_input.toPlainText().strip()
            status = self._status_input.text().strip()

            def _do():
                return client.update_profile(
                    self._base_url, self._my_name,
                    bio=bio, status=status,
                    access_key=self._access_key,
                )

            def _on_ok(profile):
                if profile:
                    self._current_profile = profile
                    # Обновляем статус-сообщение в карточке профиля.
                    status_msg = profile.get("status", "")
                    if status_msg:
                        self._status_message_label.setText(f"💬  {status_msg}")
                    else:
                        self._status_message_label.setText("")
                QMessageBox.information(self, "Сохранено", "Профиль обновлён")

            def _on_err(err):
                QMessageBox.warning(self, "Ошибка сети", f"Не удалось сохранить профиль: {err}")

            self._bridge.run(_do, on_success=_on_ok, on_error=_on_err)
        else:
            QMessageBox.information(self, "Сохранено", "Локальные настройки сохранены")

    # ── Шрифт ─────────────────────────────────────────────────────────

    def _update_font_preview(self) -> None:
        """Обновляет превью шрифта при изменении QComboBox/QSpinBox."""
        font_family = self._font_family_input.currentText()
        font_size = self._font_size_input.value()
        self._font_preview_label.setStyleSheet(f"""
            background: {PALETTE.surface_2};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 14px 18px;
            color: {PALETTE.text_primary};
            font-size: {font_size}px;
            font-family: '{font_family}';
        """)

    # ── Аватар ────────────────────────────────────────────────────────

    def _load_current_avatar(self) -> None:
        """Загружает текущую аватарку пользователя."""
        if self._base_url and self._my_name:
            self._avatar_widget.set_username(self._my_name)
            self._avatar_widget.load_avatar(self._base_url, self._access_key)

    def _upload_avatar(self) -> None:
        """Загружает новую аватарку."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите изображение для аватарки",
            str(Path.home()),
            "Изображения (*.png *.jpg *.jpeg *.gif *.bmp)",
        )
        if not file_path:
            return

        file_size = Path(file_path).stat().st_size
        size_error = avatar_size_error(file_size)
        if size_error:
            QMessageBox.warning(self, "Ошибка", size_error)
            return

        base_url = self._base_url
        access_key = self._access_key
        username = self._my_name

        if not base_url:
            QMessageBox.warning(self, "Ошибка", "Сначала подключитесь к серверу")
            return

        def upload_avatar():
            with open(file_path, "rb") as f:
                data = f.read()
            return client.upload_avatar(base_url, username, data, access_key)

        def on_success(result):
            self._avatar_widget.load_avatar(base_url, access_key)
            QMessageBox.information(self, "Успех", "Аватарка успешно обновлена")

        def on_error(error):
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить аватарку: {error}")

        self._bridge.run(upload_avatar, on_success=on_success, on_error=on_error)

    def _remove_avatar(self) -> None:
        """Удаляет текущую аватарку."""
        reply = QMessageBox.question(
            self,
            "Удаление аватарки",
            "Вы уверены, что хотите удалить аватарку?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        base_url = self._base_url
        access_key = self._access_key
        username = self._my_name

        if not base_url:
            QMessageBox.warning(self, "Ошибка", "Сначала подключитесь к серверу")
            return

        def remove_avatar():
            return client.upload_avatar(base_url, username, b"", access_key)

        def on_success(result):
            self._avatar_widget.clear_avatar()
            QMessageBox.information(self, "Успех", "Аватарка успешно удалена")

        def on_error(error):
            QMessageBox.critical(self, "Ошибка", f"Не удалось удалить аватарку: {error}")

        self._bridge.run(remove_avatar, on_success=on_success, on_error=on_error)
