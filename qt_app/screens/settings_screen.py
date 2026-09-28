"""
Экран «Настройки» — аналог блока подключения из lib/app.py (_on_connect_click
и всё, что вокруг). Логика подключения та же, что в tkinter-версии:

- Хост: поднимаем lib.relay_server.RelayServer на 0.0.0.0:port, base_url
  становится http://127.0.0.1:port.
- Клиент: base_url — это то, что ввели в поле "Адрес сервера".
- В обоих случаях после этого делаем ping() + первый poll_events() в фоне
  (через AsyncBridge/сигналы, а не ручную очередь), и только по успеху
  считаем себя подключенными.

Настройки читаются/пишутся через lib/storage.py (load_settings/save_settings) -
её не трогала, формат тот же, что у tkinter-версии, так что настройки
переезжают между версиями бесшовно.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.constants import (
    APP_TITLE,
    APP_VERSION,
    CHAT_TAIL_ON_CONNECT,
    DATA_DIR,
    HOST_DATA_DIR_NAME,
)
from lib.net_utils import get_local_ips
from lib.relay_server import RelayServer
from lib.storage import load_settings, save_settings
from lib.updater import check_for_updates, download_update, schedule_replace_and_restart
from qt_app.async_bridge import AsyncBridge
from qt_app.icons import SvgIconWidget
from qt_app.theme import PALETTE, RADIUS, RADIUS_LG, build_qss
from qt_app.widgets.glass_button import GlassButton


class SpinBoxWithoutWheel(QSpinBox):
    """SpinBox который не реагирует на колесико мыши."""

    def wheelEvent(self, event):
        """Игнорируем события колёсика мыши."""
        event.ignore()


class ComboBoxWithoutWheel(QComboBox):
    """ComboBox который не реагирует на колесико мыши."""

    def wheelEvent(self, event):
        """Игнорируем события колёсика мыши."""
        event.ignore()


def _field_qss() -> str:
    return f"""
        QLineEdit, QSpinBox, QComboBox {{
            background: {PALETTE.surface_3};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS}px;
            padding: 6px 10px;
            color: {PALETTE.text_primary};
        }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {PALETTE.accent}; }}
    """


def _section_card_qss() -> str:
    return f"""
        QFrame#SectionCard {{
            background: {PALETTE.surface_1};
            border: 1px solid {PALETTE.border_soft};
            border-radius: {RADIUS_LG}px;
        }}
    """


def _icon_header(icon_name: str, text: str) -> QWidget:
    """Создаёт заголовок с SVG иконкой."""
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    icon = SvgIconWidget(icon_name, size=20)
    layout.addWidget(icon)

    label = QLabel(text)
    label.setStyleSheet(
        f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
    )
    layout.addWidget(label)

    return widget


class SettingsScreen(QWidget):
    # испускается после успешного подключения/старта хоста, чтобы MainWindow
    # прокинул base_url/ключ в FilesScreen и BotManager в BotsScreen
    connected_changed = Signal(bool)
    # испускается при изменении настроек шифрования
    encryption_settings_changed = Signal(bool, str)  # enabled, secret_key
    # внутренний сигнал для прогресс-бара (эмитится из фонового потока, Qt
    # автоматически доставляет в главный поток)
    _download_progress = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()
        self.relay: RelayServer | None = None
        self.base_url: str = ""
        self.access_key: str = ""
        self.my_name: str = ""
        self.connected: bool = False
        self._pending_update = None  # info о последней найденной версии

        self.settings = load_settings()

        # Подключаем внутренний сигнал прогресса к обновлению прогресс-бара
        self._download_progress.connect(self._on_download_progress)

        # Главный layout с прокруткой
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Область прокрутки
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {PALETTE.surface_0};
                border: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {PALETTE.border};
                border-radius: 4px;
                min-height: 28px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255,255,255,0.18);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::horizontal {{
                height: 0px;
            }}
        """)

        # Контейнер для контента
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(18)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Заголовок с иконкой
        title_widget = QWidget()
        title_layout = QHBoxLayout(title_widget)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(12)

        title_icon = SvgIconWidget("theme", size=24)
        title_layout.addWidget(title_icon)

        title = QLabel("Настройки")
        title.setStyleSheet(f"color: {PALETTE.text_primary}; font-size: 20px; font-weight: 700;")
        title_layout.addWidget(title)

        title_layout.addStretch(1)
        root.addWidget(title_widget)

        version_label = QLabel(f"Версия {APP_VERSION} · Friend Relay")
        version_label.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 13px;")
        root.addWidget(version_label)

        # ── Карточка «Обновления» ──────────────────────────────────
        update_card = QFrame()
        update_card.setObjectName("SectionCard")
        update_card.setStyleSheet(_section_card_qss())
        update_card_layout = QVBoxLayout(update_card)
        update_card_layout.setContentsMargins(16, 16, 16, 16)
        update_card_layout.setSpacing(10)

        update_title = _icon_header("refresh", "Обновления")
        update_card_layout.addWidget(update_title)

        self._update_status = QLabel("Не проверялось")
        self._update_status.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        update_card_layout.addWidget(self._update_status)

        # Поле URL манифеста (перенесено сюда из формы ниже)
        self._manifest_url_input = QLineEdit(self.settings.get("update_manifest_url", ""))
        self._manifest_url_input.setStyleSheet(_field_qss())
        self._manifest_url_input.setPlaceholderText(
            "URL манифеста (например: https://yourname.github.io/friend-relay/version.json)"
        )
        update_card_layout.addWidget(self._manifest_url_input)

        # Прогресс-бар (скрыт по умолчанию)
        self._update_progress = QProgressBar()
        self._update_progress.setFixedHeight(6)
        self._update_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {PALETTE.surface_3};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: {PALETTE.accent};
                border-radius: 3px;
            }}
        """)
        self._update_progress.setVisible(False)
        update_card_layout.addWidget(self._update_progress)

        # Кнопки
        update_btn_row = QHBoxLayout()
        update_btn_row.setSpacing(8)

        self._update_btn = GlassButton("Проверить")
        self._update_btn.clicked.connect(self._check_for_updates)
        update_btn_row.addWidget(self._update_btn)

        self._download_btn = GlassButton("Скачать и установить")
        self._download_btn.clicked.connect(self._download_last_known_update)
        self._download_btn.setVisible(False)
        update_btn_row.addWidget(self._download_btn)

        update_btn_row.addStretch(1)
        update_card_layout.addLayout(update_btn_row)
        root.addWidget(update_card)

        # ── Карточка «Подключение» ──────────────────────────────────
        # Переписано с QFormLayout на вертикальный layout — раньше QForm
        # сжимал QHBoxLayout с radio-кнопками, и они налезали на соседнее
        # поле. Теперь radio-кнопки — вертикально, поля под ними.
        conn_card = QFrame()
        conn_card.setObjectName("SectionCard")
        conn_card.setStyleSheet(_section_card_qss())
        conn_layout = QVBoxLayout(conn_card)
        conn_layout.setContentsMargins(16, 16, 16, 16)
        conn_layout.setSpacing(12)

        conn_title = _icon_header("search", "Подключение")
        conn_layout.addWidget(conn_title)

        # Имя
        name_col = QVBoxLayout()
        name_col.setSpacing(4)
        name_lbl = QLabel("Имя")
        name_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        name_col.addWidget(name_lbl)
        self._name_input = QLineEdit(self.settings.get("name", ""))
        self._name_input.setStyleSheet(_field_qss())
        self._name_input.setPlaceholderText("Как тебя видят другие")
        self._name_input.setToolTip(
            "Имя входа (логин) — применяется при подключении.\n"
            "Отображаемое имя можно сменить позже в Профиле (кнопка ✏)."
        )
        name_col.addWidget(self._name_input)
        conn_layout.addLayout(name_col)

        # Режим: 2 вертикальные радио-кнопки с подписями
        mode_lbl = QLabel("Режим")
        mode_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; padding-top: 4px;"
        )
        conn_layout.addWidget(mode_lbl)

        self._client_radio = QRadioButton("Подключиться к другу")
        self._client_radio.setStyleSheet(f"color: {PALETTE.text_primary};")
        self._client_radio.setChecked(True)  # по умолчанию клиент
        conn_layout.addWidget(self._client_radio)

        # Поля клиента (адрес хоста)
        self._client_fields = QWidget()
        cf_layout = QVBoxLayout(self._client_fields)
        cf_layout.setContentsMargins(28, 0, 0, 0)
        cf_layout.setSpacing(4)

        cf_hint = QLabel(
            "Адрес хоста. Для ZeroTier: http://100.xxx.xxx.xxx:8420\n"
            "Для локалки: http://192.168.1.10:8420"
        )
        cf_hint.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        cf_layout.addWidget(cf_hint)

        self._host_url_input = QLineEdit(self.settings.get("host_url", "http://"))
        self._host_url_input.setStyleSheet(_field_qss())
        self._host_url_input.setPlaceholderText("http://100.123.45.67:8420")
        cf_layout.addWidget(self._host_url_input)
        conn_layout.addWidget(self._client_fields)

        self._host_radio = QRadioButton("Раздавать самой (стать хостом)")
        self._host_radio.setStyleSheet(f"color: {PALETTE.text_primary};")
        conn_layout.addWidget(self._host_radio)

        # Поля хоста (порт, макс. размер файла, IP-подсказки)
        self._host_fields = QWidget()
        hf_layout = QVBoxLayout(self._host_fields)
        hf_layout.setContentsMargins(28, 0, 0, 0)
        hf_layout.setSpacing(6)

        hf_hint = QLabel(
            "Хост слушает 0.0.0.0 — доступен по всем интерфейсам.\n"
            "Друзья подключаются к твоему ZeroTier-IP (100.x.x.x)\n"
            "или IP в локалке (192.168.x.x).\n"
            "💡 Если включен VPN (NordVPN, ExpressVPN и др.), выберите 'Все интерфейсы' или физический IP."
        )
        hf_hint.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        hf_layout.addWidget(hf_hint)

        # Список твоих IP-адресов (обновляется при показе)
        self._host_ips_label = QLabel("Твои IP: проверяю…")
        self._host_ips_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
            f"background: {PALETTE.surface_2};"
            f"border-radius: 4px; padding: 6px 8px;"
            f"font-family: 'Consolas, monospace';"
        )
        self._host_ips_label.setWordWrap(True)
        hf_layout.addWidget(self._host_ips_label)

        # Порт
        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        port_lbl = QLabel("Порт")
        port_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        port_lbl.setFixedWidth(80)
        port_row.addWidget(port_lbl)
        self._port_input = SpinBoxWithoutWheel()
        self._port_input.setRange(1, 65535)
        self._port_input.setValue(int(self.settings.get("port", 8420)))
        self._port_input.setStyleSheet(_field_qss())
        port_row.addWidget(self._port_input, 1)
        hf_layout.addLayout(port_row)

        # Интерфейс прослушивания
        host_row = QHBoxLayout()
        host_row.setSpacing(8)
        host_lbl = QLabel("Интерфейс")
        host_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        host_lbl.setFixedWidth(80)
        host_row.addWidget(host_lbl)
        self._host_combo = QComboBox()
        self._host_combo.setStyleSheet(_field_qss())
        self._host_combo.addItem("Все интерфейсы (0.0.0.0)", "0.0.0.0")
        self._host_combo.addItem("Только локальный (127.0.0.1)", "127.0.0.1")
        host_row.addWidget(self._host_combo, 1)
        hf_layout.addLayout(host_row)

        # Макс. размер файла
        size_row = QHBoxLayout()
        size_row.setSpacing(8)
        size_lbl = QLabel("Макс. файл")
        size_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        size_lbl.setFixedWidth(80)
        size_row.addWidget(size_lbl)
        self._max_size_input = SpinBoxWithoutWheel()
        self._max_size_input.setRange(1, 100000)
        self._max_size_input.setSuffix(" МБ")
        self._max_size_input.setValue(int(self.settings.get("max_file_size_mb", 2048)))
        self._max_size_input.setStyleSheet(_field_qss())
        size_row.addWidget(self._max_size_input, 1)
        hf_layout.addLayout(size_row)

        conn_layout.addWidget(self._host_fields)

        # Ключ доступа (общий для обоих режимов)
        key_col = QVBoxLayout()
        key_col.setSpacing(4)
        key_col.setContentsMargins(0, 4, 0, 0)
        key_lbl = QLabel("Ключ доступа (необязательно)")
        key_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        key_col.addWidget(key_lbl)
        self._key_input = QLineEdit(self.settings.get("access_key", ""))
        self._key_input.setStyleSheet(_field_qss())
        self._key_input.setPlaceholderText("если хост его требует")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_col.addWidget(self._key_input)
        conn_layout.addLayout(key_col)

        # Восстанавливаем режим из настроек
        is_host = bool(self.settings.get("is_host", False))
        self._host_radio.setChecked(is_host)
        self._client_radio.setChecked(not is_host)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._client_radio)
        self._mode_group.addButton(self._host_radio)
        self._client_radio.toggled.connect(self._on_mode_toggle)
        self._on_mode_toggle()  # сразу показываем правильные поля

        root.addWidget(conn_card)

        # ── Шифрование (карточка) ────────────────────────────────────
        enc_card = QFrame()
        enc_card.setObjectName("SectionCard")
        enc_card.setStyleSheet(_section_card_qss())
        enc_layout = QVBoxLayout(enc_card)
        enc_layout.setContentsMargins(16, 16, 16, 16)
        enc_layout.setSpacing(10)

        enc_title = QLabel("🔐  Шифрование")
        enc_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )
        enc_layout.addWidget(enc_title)

        enc_check_row = QHBoxLayout()
        self._encryption_enabled = QCheckBox()
        self._encryption_enabled.setChecked(bool(self.settings.get("encryption_enabled", False)))
        self._encryption_enabled.setStyleSheet(f"color: {PALETTE.text_primary};")
        self._encryption_enabled.toggled.connect(self._on_encryption_settings_changed)
        enc_check_row.addWidget(self._encryption_enabled)
        enc_check_lbl = QLabel("Включить AES-256-GCM")
        enc_check_lbl.setStyleSheet(f"color: {PALETTE.text_primary};")
        enc_check_row.addWidget(enc_check_lbl)
        enc_check_row.addStretch(1)
        enc_layout.addLayout(enc_check_row)

        self._secret_key_input = QLineEdit(self.settings.get("secret_key", ""))
        self._secret_key_input.setStyleSheet(_field_qss())
        self._secret_key_input.setPlaceholderText("Секретный ключ (пароль) — должен совпадать с хостом")
        self._secret_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_key_input.textChanged.connect(self._on_encryption_settings_changed)
        enc_layout.addWidget(self._secret_key_input)

        # Индикатор силы пароля
        self._password_strength_label = QLabel("")
        self._password_strength_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; padding-left: 4px;"
        )
        self._secret_key_input.textChanged.connect(self._update_password_strength)
        enc_layout.addWidget(self._password_strength_label)
        self._update_password_strength()

        root.addWidget(enc_card)

        # ── Карточка «Интерфейс» (масштабирование) ─────────────────────────
        ui_card = QFrame()
        ui_card.setObjectName("SectionCard")
        ui_card.setStyleSheet(_section_card_qss())
        ui_layout = QVBoxLayout(ui_card)
        ui_layout.setContentsMargins(16, 16, 16, 16)
        ui_layout.setSpacing(12)

        ui_title = _icon_header("ux", "Интерфейс")
        ui_layout.addWidget(ui_title)

        # Масштаб интерфейса
        scale_col = QVBoxLayout()
        scale_col.setSpacing(4)
        scale_lbl = QLabel("Масштаб интерфейса")
        scale_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        scale_col.addWidget(scale_lbl)

        scale_row = QHBoxLayout()
        scale_row.setSpacing(8)

        self._scale_spin = SpinBoxWithoutWheel()
        self._scale_spin.setRange(80, 150)
        self._scale_spin.setSuffix("%")
        self._scale_spin.setValue(self.settings.get("ui_scale", 100))
        self._scale_spin.setStyleSheet(_field_qss())
        self._scale_spin.setFixedWidth(100)
        self._scale_spin.valueChanged.connect(self._on_scale_changed)
        scale_row.addWidget(self._scale_spin)

        scale_hint = QLabel("Рекомендуется 100-110%")
        scale_hint.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px;"
        )
        scale_row.addWidget(scale_hint)
        scale_row.addStretch(1)

        scale_col.addLayout(scale_row)
        ui_layout.addLayout(scale_col)

        # Тема
        theme_col = QVBoxLayout()
        theme_col.setSpacing(4)
        theme_lbl = QLabel("Цветовая тема")
        theme_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px; padding-top: 8px;"
        )
        theme_col.addWidget(theme_lbl)

        from qt_app.theme import ThemeName

        self._theme_combo = ComboBoxWithoutWheel()
        self._theme_combo.setStyleSheet(_field_qss())
        self._theme_combo.addItem("Aurora (тёмный)", ThemeName.AURORA)
        self._theme_combo.addItem("Lukewarm Ocean (синий)", ThemeName.LUKEWARM_OCEAN)
        self._theme_combo.addItem("Snow (светлый)", ThemeName.SNOW)
        self._theme_combo.addItem("Cherry Grove (вишнёвый)", ThemeName.CHERRY_GROVE)
        self._theme_combo.addItem("Sakura (розовый)", ThemeName.SAKURA)
        self._theme_combo.addItem("Crème Brûlée (песочный)", ThemeName.CREME_BRULEE)

        current_theme = self.settings.get("theme_name", ThemeName.AURORA)
        for i in range(self._theme_combo.count()):
            if self._theme_combo.itemData(i) == current_theme:
                self._theme_combo.setCurrentIndex(i)
                break

        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_col.addWidget(self._theme_combo)
        ui_layout.addLayout(theme_col)

        root.addWidget(ui_card)

        # ── Карточка «О программе» ─────────────────────────────────────
        about_card = QFrame()
        about_card.setObjectName("SectionCard")
        about_card.setStyleSheet(_section_card_qss())
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(16, 16, 16, 16)
        about_layout.setSpacing(10)

        about_title = _icon_header("info", "О программе")
        about_layout.addWidget(about_title)

        about_text = QLabel(
            "Friend Relay — децентрализованный мессенджер для друзей.\n\n"
            "Обменивайтесь сообщениями, файлами и играйте в игры вместе\n"
            "без облачных серверов. Все данные хранятся локально.\n\n"
            "Требования:\n"
            "• Python 3.11+\n"
            "• Для удалённого доступа: ZeroTier или LAN"
        )
        about_text.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 13px; line-height: 1.6;"
        )
        about_text.setWordWrap(True)
        about_layout.addWidget(about_text)

        root.addWidget(about_card)

        # ── Статус + кнопка подключения ───────────────────────────────
        self._status_label = QLabel("Не подключено")
        self._status_label.setStyleSheet(f"color: {PALETTE.text_secondary};")
        root.addWidget(self._status_label)

        self._connect_btn = GlassButton("Подключиться")
        self._connect_btn.setFixedWidth(200)
        self._connect_btn.clicked.connect(self._on_connect_click)
        root.addWidget(self._connect_btn)

        # Добавляем stretch чтобы контент не прилипал к низу
        root.addStretch(1)

        # Устанавливаем контент в scroll area
        scroll.setWidget(content)
        main_layout.addWidget(scroll)

    def _on_mode_toggle(self) -> None:
        """Показывает/прячет поля в зависимости от режима.

        Вместо того чтобы скрывать отдельные QFormLayout-ряды (что даёт
        пустые промежутки), скрываем целые QWidget-контейнеры — UI не дёргается.
        """
        is_host = self._host_radio.isChecked()
        # Клиентские поля — адрес хоста
        self._client_fields.setVisible(not is_host)
        # Хостовые поля — порт, макс. размер, IP-подсказки
        self._host_fields.setVisible(is_host)
        # Если выбрали режим хоста — обновляем список IP-адресов и интерфейсов
        if is_host:
            self._refresh_host_ips()
            self._refresh_host_interfaces()

    def _refresh_host_ips(self) -> None:
        """Перечисляет IP-адреса этой машины — чтобы юзер сразу увидел
        свой ZeroTier-адрес (100.x.x.x) и мог дать его друзьям.

        ZeroTier-адреса начинаются с 100., приватные с 10./172.16-31./192.168.
        Детект адресов — в lib/net_utils.get_local_ips(); здесь только
        форматирование для показа.
        """
        try:
            ips = get_local_ips()

            if not ips:
                self._host_ips_label.setText("Твои IP: 127.0.0.1 (только локально)")
                return

            # Помечаем ZeroTier (100.x.x.x) и приватные диапазоны
            lines = []
            for ip in ips[:8]:  # максимум 8, чтобы не раздуло метку
                if ip.startswith("100."):
                    lines.append(f"  {ip}  ← ZeroTier")
                elif ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                    lines.append(f"  {ip}  ← локалка")
                else:
                    lines.append(f"  {ip}")
            self._host_ips_label.setText("Твои IP:\n" + "\n".join(lines))
        except Exception:
            self._host_ips_label.setText("Твои IP: не удалось определить")

    def _refresh_host_interfaces(self) -> None:
        """Обновляет выпадающий список интерфейсов."""
        try:
            # Сохраняем текущий выбор
            current_data = self._host_combo.currentData()

            # Очищаем кроме базовых
            while self._host_combo.count() > 2:
                self._host_combo.removeItem(2)

            # Добавляем найденные IP
            ips = get_local_ips()
            for ip in ips:
                if ip not in ["0.0.0.0", "127.0.0.1"]:
                    self._host_combo.addItem(f"{ip} (сетевой)", ip)

            # Восстанавливаем выбор если возможно
            saved_host = self.settings.get("server_host", "0.0.0.0")
            for i in range(self._host_combo.count()):
                if self._host_combo.itemData(i) == saved_host or self._host_combo.itemData(i) == current_data:
                    self._host_combo.setCurrentIndex(i)
                    break
        except Exception:
            pass

    def _on_encryption_settings_changed(self) -> None:
        """Обрабатывает изменение настроек шифрования."""
        enabled = self._encryption_enabled.isChecked()
        secret_key = self._secret_key_input.text().strip()

        # Сохраняем настройки
        self.settings["encryption_enabled"] = enabled
        self.settings["secret_key"] = secret_key
        save_settings(self.settings)

        self.encryption_settings_changed.emit(enabled, secret_key)

    def _update_password_strength(self) -> None:
        """Обновляет индикатор силы пароля в реальном времени."""
        from lib.crypto import password_strength
        pwd = self._secret_key_input.text()
        score, label, color = password_strength(pwd)
        # 4-сегментная полоска: ●●●●
        filled = "●" * score
        empty = "○" * (4 - score)
        bar = f"{filled}{empty}"
        self._password_strength_label.setText(f"{bar}  {label}")
        self._password_strength_label.setStyleSheet(
            f"color: {color}; font-size: 11px; padding-left: 4px; font-weight: 600;"
        )

    # -- подключение / отключение --
    def _on_connect_click(self) -> None:
        if self.connected:
            self._disconnect()
            return

        name = self._name_input.text().strip()
        if not name:
            QMessageBox.warning(self, APP_TITLE, "Сначала введи своё имя")
            return
        self.my_name = name
        self.access_key = self._key_input.text().strip()

        if self._host_radio.isChecked():
            port = self._port_input.value()
            max_size_bytes = self._max_size_input.value() * 1024 * 1024
            host = self._host_combo.currentData()
            data_dir = DATA_DIR / HOST_DATA_DIR_NAME

            # (v3.4.2 fix) Старт сервера уводим из GUI-потока. Раньше
            # RelayServer(...) + relay.start() выполнялись ПРЯМО здесь:
            #   * генерация TLS-сертификата — ЗАМЕРЕНО 84.6 с на слабом CPU
            #     (см. docs/HANG_DIAGNOSIS_v3.4.1.md);
            #   * get_interfaces(): netsh + getaddrinfo + connect(8.8.8.8)
            #     без таймаутов — при VPN может висеть вечно;
            #   * MusicBot._scan_library() — открывает каждый файл библиотеки.
            # Всё это замораживало окно Qt намертво («Не отвечает»).
            self._pending_host = (host, port, data_dir, max_size_bytes)
            self._status_label.setText("Поднимаю раздачу… (первый старт может занять минуту)")
            self._connect_btn.setEnabled(False)
            self._bridge.run(
                self._start_relay_job,
                on_success=self._on_relay_started,
                on_error=self._on_relay_start_failed,
            )
            return
        else:
            host_url = self._host_url_input.text().strip().rstrip("/")
            if not host_url.startswith("http://") and not host_url.startswith("https://"):
                QMessageBox.warning(self, APP_TITLE, "Адрес хоста должен начинаться с http://")
                return
            self.base_url = host_url
            self._status_label.setText("Подключаюсь…")

        self._save_current_settings()
        self._connect_btn.setEnabled(False)

        self._bridge.run(
            self._try_ping_then_poll,
            on_success=self._on_connect_ok,
            on_error=self._on_connect_failed,
        )

    def _start_relay_job(self):
        """(v3.4.2 fix) Создание+старт RelayServer — выполняется в пуле
        AsyncBridge (фоновый поток), а не в GUI-потоке."""
        host, port, data_dir, max_size_bytes = self._pending_host
        relay = RelayServer(
            data_dir, host_name=self.my_name, access_key=self.access_key,
            max_file_size=max_size_bytes,
        )
        ok = relay.start(host, port)
        if not ok:
            raise RuntimeError(f"Порт {port} занят (запущен другой сервер?)")
        self.relay = relay
        return port

    def _on_relay_started(self, port) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self._status_label.setText(f"Раздача поднята на порту {port}. Подключаюсь…")
        self._save_current_settings()
        self._bridge.run(
            self._try_ping_then_poll,
            on_success=self._on_connect_ok,
            on_error=self._on_connect_failed,
        )

    def _on_relay_start_failed(self, err) -> None:
        self.relay = None
        self._connect_btn.setEnabled(True)
        QMessageBox.critical(
            self,
            APP_TITLE,
            f"Не удалось запустить раздачу: {err}",
        )

    def _try_ping_then_poll(self):
        # ping с name= регистрирует сессию на сервере и возвращает session_token,
        # который потом используется для подписи всех POST-запросов
        # (защита от подмены sender — см. lib/auth.py).
        info = client.ping(self.base_url, name=self.my_name)
        t0 = time.monotonic()
        result = client.poll_events(
            self.base_url, 0, self.access_key, self.my_name, tail=CHAT_TAIL_ON_CONNECT
        )
        rtt_ms = int((time.monotonic() - t0) * 1000)
        return info, result, rtt_ms

    def _on_connect_ok(self, payload) -> None:
        info, result, rtt_ms = payload
        # (v3.3.2) Дискриминаторы как в Discord: сервер мог выдать
        # «Жуж#2» вместо «Жуж» (имя хоста зарезервировано). Принимаем
        # выданное имя — все подписи запросов идут от него.
        assigned = ""
        if isinstance(info, dict):
            assigned = str(info.get("name_assigned", "") or "")
        if assigned and assigned != self.my_name:
            self.my_name = assigned
            self._name_input.setText(assigned)
        self.connected = True
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Отключиться")
        self._status_label.setText(f"Подключено · пинг {rtt_ms}мс")
        # Имя входа зафиксировано на время сессии: поле только для чтения,
        # чтобы не было иллюзии «поменял, но ничего не произошло».
        # Отображаемое имя (как видят другие) меняется в Профиле.
        self._name_input.setReadOnly(True)
        self._name_input.setToolTip(
            "Имя входа зафиксировано на время сессии.\n"
            "Отключись, чтобы сменить логин; имя для друзей\n"
            "меняется в Профиле."
        )
        self.connected_changed.emit(True)

    def _on_connect_failed(self, error: Exception) -> None:
        self._connect_btn.setEnabled(True)
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        self._status_label.setText(f"Ошибка: {error}")
        QMessageBox.critical(self, APP_TITLE, f"Не удалось подключиться:\n{error}")

    def _disconnect(self) -> None:
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        # Сбрасываем клиентскую сессию — при следующем подключении получим новый token
        from lib import client
        client.clear_session()
        self.connected = False
        self._name_input.setReadOnly(False)
        self._name_input.setToolTip("")
        self._connect_btn.setText("Подключиться")
        self._status_label.setText("Не подключено")
        self.connected_changed.emit(False)

    def _save_current_settings(self) -> None:
        self.settings.update(
            {
                "name": self.my_name,
                "is_host": self._host_radio.isChecked(),
                "host_url": self._host_url_input.text().strip(),
                "update_manifest_url": self._manifest_url_input.text().strip(),
                "encryption_enabled": self._encryption_enabled.isChecked(),
                "secret_key": self._secret_key_input.text().strip(),
                "ui_scale": self._scale_spin.value(),
                "theme_name": self._theme_combo.currentData(),
                "port": self._port_input.value(),
                "server_host": self._host_combo.currentData(),
                "access_key": self._key_input.text().strip(),
            }
        )
        save_settings(self.settings)

    def _on_scale_changed(self, value: int) -> None:
        """Обрабатывает изменение масштаба интерфейса."""
        self.settings["ui_scale"] = value
        save_settings(self.settings)

        # Масштабирование требует перезапуска для полного применения
        self._scale_spin.setToolTip("Перезапустите приложение для применения масштаба")

    def _on_theme_changed(self, index: int) -> None:
        """Обрабатывает изменение цветовой темы."""
        theme_name = self._theme_combo.currentData()
        self.settings["theme_name"] = theme_name
        save_settings(self.settings)

        # Применяем тему (требует перерисовки UI)
        from qt_app.theme import set_theme
        set_theme(theme_name)
        self.setStyleSheet(build_qss())  # Применяем новые стили

        # Обновляем иконки с новым цветом
        self.update()

    def _check_for_updates(self) -> None:
        """Проверяет наличие обновлений."""
        self._update_status.setText("Проверяю обновления…")
        self._update_status.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        self._update_btn.setEnabled(False)
        self._download_btn.setVisible(False)
        self._update_progress.setVisible(False)

        manifest_url = self._manifest_url_input.text().strip() or self.settings.get(
            "update_manifest_url", ""
        )
        if not manifest_url:
            self._update_status.setText(
                "Не задан URL манифеста — впиши его в поле выше"
            )
            self._update_status.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 12px;"
            )
            self._update_btn.setEnabled(True)
            return
        # Сохраняем сразу, а не только при подключении к серверу
        self.settings["update_manifest_url"] = manifest_url
        save_settings(self.settings)

        def do_check():
            return check_for_updates(manifest_url)

        def on_ok(info):
            self._update_btn.setEnabled(True)
            if info is None:
                self._update_status.setText("Установлена последняя версия")
                self._update_status.setStyleSheet(
                    f"color: {PALETTE.success}; font-size: 12px;"
                )
            else:
                # Запоминаем info, показываем кнопку «Скачать»
                self._pending_update = info
                ver = info.version
                self._update_status.setText(f"Доступна версия {ver}")
                self._update_status.setStyleSheet(
                    f"color: {PALETTE.accent}; font-size: 12px; font-weight: 600;"
                )
                self._download_btn.setText(f"Скачать v{ver}")
                self._download_btn.setVisible(True)
                # Заодно показываем что нового (notes) если есть
                if info.notes:
                    # tooltip с краткой инфой
                    self._download_btn.setToolTip(info.notes[:500])

        def on_err(e):
            self._update_status.setText(f"Ошибка: {e}")
            self._update_status.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 12px;"
            )
            self._update_btn.setEnabled(True)

        self._bridge.run(do_check, on_success=on_ok, on_error=on_err)

    def _download_last_known_update(self) -> None:
        """Качает последний info об обновлении, который нашли в _check_for_updates."""
        info = getattr(self, "_pending_update", None)
        if info is None:
            return
        self._show_update_dialog(info)

    def _on_download_progress(self, pct: int) -> None:
        """Обновляет прогресс-бар (вызывается из главного потока через signal)."""
        self._update_progress.setValue(pct)
        self._update_status.setText(f"Скачиваю: {pct}%")

    def _show_update_dialog(self, info) -> None:
        """Показывает диалог с информацией об обновлении."""
        msg = f"Доступна новая версия {info.version}!\n\n"
        if info.notes:
            msg += f"Что нового:\n{info.notes}\n\n"
        msg += "Скачать и установить?"

        reply = QMessageBox.question(
            self,
            "Доступно обновление",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._download_and_install(info)

    def _download_and_install(self, info) -> None:
        """Скачивает и устанавливает обновление."""
        self._update_status.setText("Скачиваю обновление…")
        self._update_status.setStyleSheet(
            f"color: {PALETTE.accent}; font-size: 12px;"
        )
        self._update_progress.setValue(0)
        self._update_progress.setVisible(True)
        self._download_btn.setEnabled(False)
        self._update_btn.setEnabled(False)

        import tempfile
        from pathlib import Path

        temp_dir = Path(tempfile.gettempdir())
        # Расширение зависит от того, что качаем — .exe или .zip
        # (манифест может указывать что угодно)
        # Смотрим на primary URL
        url = info.primary_url or ""
        if url.lower().endswith(".zip"):
            new_file = temp_dir / f"FriendRelay_{info.version}.zip"
        else:
            new_file = temp_dir / f"FriendRelay_{info.version}.exe"

        def progress_cb(sent, total):
            if total > 0:
                pct = int((sent / total) * 100)
                # Эмитим сигнал — Qt сам доставит в главный поток
                self._download_progress.emit(pct)

        def do_download():
            return download_update(
                info,
                new_file,
                progress_cb=progress_cb,
                preferred_base_relay_url=self.base_url if self.connected else "",
                preferred_file_id=getattr(info, "relay_file_id", ""),
            )

        def on_ok(result):
            success, source = result
            if success:
                self._update_progress.setValue(100)
                self._update_status.setText(f"✓ Скачано с: {source}")
                self._update_status.setStyleSheet(
                    f"color: {PALETTE.success}; font-size: 12px;"
                )
                # Задаём вопрос: это .exe (одномоментная замена) или .zip
                # (пользователь распакует сам)?
                if str(new_file).lower().endswith(".exe"):
                    self._install_update(new_file)
                else:
                    # ZIP — открываем папку, пусть юзер сам распакует
                    import os
                    import subprocess
                    if os.name == "nt":
                        subprocess.Popen(["explorer", "/select,", str(new_file)])
                    else:
                        subprocess.Popen(["xdg-open", str(temp_dir)])
                    self._update_status.setText(
                        f"✓ Скачано в: {new_file}\nРаспакуй и запусти вручную"
                    )
            else:
                self._update_status.setText(f"⚠ Ошибка скачивания: {source}")
                self._update_status.setStyleSheet(
                    f"color: {PALETTE.warning}; font-size: 12px;"
                )
                self._update_progress.setVisible(False)
                self._download_btn.setEnabled(True)
                self._update_btn.setEnabled(True)

        def on_err(e):
            self._update_status.setText(f"⚠ Ошибка: {e}")
            self._update_status.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 12px;"
            )
            self._update_progress.setVisible(False)
            self._download_btn.setEnabled(True)
            self._update_btn.setEnabled(True)

        self._bridge.run(do_download, on_success=on_ok, on_error=on_err)

    def _install_update(self, new_exe) -> None:
        """Устанавливает обновление и перезапускает приложение.

        ВАЖНО: schedule_replace_and_restart пишет .bat-хелпер, который ждёт,
        пока процесс с текущим PID исчезнет из tasklist, и только тогда
        подменяет exe и перезапускает его. Раньше здесь вызывался
        self.close() - но SettingsScreen встроен в QStackedWidget внутри
        MainWindow, а не является отдельным top-level окном, поэтому close()
        на нём НЕ завершает процесс приложения (проверено: после close()
        event loop продолжает жить). В итоге .bat-хелпер ждал бы завершения
        процесса вечно, а обновление так и не применялось бы, пока
        приложение не закроют вручную. Правильно - явно завершать саму
        QApplication."""
        self._update_status.setText("Устанавливаю обновление…")

        if schedule_replace_and_restart(new_exe):
            self._update_status.setText("Приложение закроется для обновления…")
            # Даём время на запуск helper-скрипта
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication

            QTimer.singleShot(2000, QApplication.instance().quit)
        else:
            self._update_status.setText("Ошибка установки обновления")
            self._update_btn.setEnabled(True)

    def get_bot_manager(self):
        return self.relay.get_bot_manager() if self.relay is not None else None

    def get_encryption_settings(self) -> tuple[bool, str]:
        """Возвращает настройки шифрования (включено, секретный ключ)."""
        return self._encryption_enabled.isChecked(), self._secret_key_input.text().strip()
