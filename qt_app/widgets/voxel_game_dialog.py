"""VoxelGameDialog — окно мультиплеерной игры Voxel Shooter.

Два экрана:
  1. Lobby — список сессий, создание новой, выбор команды, кнопки
     «Войти», «Добавить бота» (после входа)
  2. Game — VoxelCanvas с snapshot'ом + панель игроков справа

Переключение между экранами по состоянию VoxelSessionClient.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.games.voxel.entities import TEAM_BLUE, TEAM_NONE, TEAM_RED
from qt_app.async_bridge import AsyncBridge
from qt_app.theme import PALETTE, RADIUS, RADIUS_LG
from qt_app.voxel_client import VoxelSessionClient
from qt_app.widgets.games.voxel_canvas import VoxelCanvas

_TEAM_LABELS = {
    TEAM_NONE: "Без команды",
    TEAM_RED: "🔴 Красная",
    TEAM_BLUE: "🔵 Синяя",
}


def _glass_btn_qss(primary: bool = False, danger: bool = False) -> str:
    if danger:
        bg, hover = PALETTE.danger, "#e0475d"
    elif primary:
        bg, hover = PALETTE.accent, PALETTE.accent_hover
    else:
        bg, hover = PALETTE.glass_bg, PALETTE.glass_bg_hover
    return f"""
        QPushButton {{
            background: {bg};
            color: {PALETTE.text_on_accent if (primary or danger) else PALETTE.text_primary};
            border: 1px solid {PALETTE.border if not (primary or danger) else bg};
            border-radius: {RADIUS}px;
            padding: 8px 14px;
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {hover};
            border-color: {PALETTE.border_strong if not (primary or danger) else hover};
        }}
    """


class VoxelGameDialog(QDialog):
    """Диалог игры: лобби → выбор сессии → окно игры."""

    closed = Signal()

    def __init__(self, parent: QWidget | None, base_url: str, player_name: str,
                 access_key: str = "", bot_id: str = "voxel_shooter",
                 bot_meta: dict | None = None, initial_tab: str = "lobby"):
        super().__init__(parent)
        self.setWindowTitle("🔫 Воксельный Шутер")
        self.setModal(False)
        self.setMinimumSize(1200, 760)
        self.setStyleSheet(f"background: {PALETTE.surface_0};")

        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._bot_meta = bot_meta or {}

        self._bridge = AsyncBridge()
        self._client = VoxelSessionClient(parent=self)
        # Qt-сигналы — автоматически доставляются в главный поток
        # (исправляет "QBasicTimer: Timers cannot be started from another thread")
        self._client.snapshot_received.connect(self._on_snapshot)
        self._client.joined.connect(self._on_joined)
        self._client.error.connect(self._on_session_error)
        self._client.disconnected.connect(self._on_session_disconnected)

        self._voice_host = ""
        self._voice_port = 0  # TCP-порт voxel-сервера (HTTP+2)

        self._current_screen: str = "lobby"  # lobby | game

        self._setup_ui()
        self._refresh_sessions()
        self._refresh_voice_info()

        # Опрос участников/сессий каждые 2 сек (через HTTP)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh_sessions)
        self._poll_timer.start(3000)

    # ── UI ─────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Root layout — переключается между lobby и game через QStackedWidget
        from PySide6.QtWidgets import QStackedWidget

        self._stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._stack)

        # Lobby screen
        self._lobby_widget = self._build_lobby()
        self._stack.addWidget(self._lobby_widget)

        # Game screen
        self._game_widget = self._build_game_screen()
        self._stack.addWidget(self._game_widget)

        self._stack.setCurrentWidget(self._lobby_widget)

    def _build_lobby(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("🔫  Воксельный Шутер")
        title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 22px; font-weight: 700;"
        )
        layout.addWidget(title)

        subtitle = QLabel("Мультиплеерный top-down шутер с разрушаемым миром")
        subtitle.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 13px;")
        layout.addWidget(subtitle)

        # Sessions list card
        card = QFrame()
        card.setObjectName("Card")
        card.setStyleSheet(f"""
            #Card {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_LG}px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(10)

        sessions_header = QLabel("🎮  Активные сессии")
        sessions_header.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )
        card_layout.addWidget(sessions_header)

        self._sessions_list = QListWidget()
        self._sessions_list.setStyleSheet(f"""
            QListWidget {{
                background: {PALETTE.surface_2};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 4px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 12px;
                border-radius: {RADIUS}px;
                color: {PALETTE.text_primary};
            }}
            QListWidget::item:hover {{
                background: {PALETTE.surface_3};
            }}
            QListWidget::item:selected {{
                background: {PALETTE.surface_3};
                color: {PALETTE.text_primary};
            }}
        """)
        self._sessions_list.setMinimumHeight(200)
        card_layout.addWidget(self._sessions_list)

        # Refresh + Create buttons
        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("🔄 Обновить")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(_glass_btn_qss())
        refresh_btn.clicked.connect(self._refresh_sessions)
        btn_row.addWidget(refresh_btn)

        create_btn = QPushButton("➕ Создать новую сессию")
        create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_btn.setStyleSheet(_glass_btn_qss(primary=True))
        create_btn.clicked.connect(self._on_create_session)
        btn_row.addWidget(create_btn)

        btn_row.addStretch(1)
        card_layout.addLayout(btn_row)

        # ── Режим игры ──
        from PySide6.QtWidgets import QButtonGroup, QRadioButton

        mode_lbl = QLabel("Режим:")
        mode_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; padding-top: 8px;"
        )
        card_layout.addWidget(mode_lbl)

        mode_row = QHBoxLayout()
        self._mode_buttons: dict[str, QRadioButton] = {}
        self._mode_group = QButtonGroup(self)
        for mode_id, mode_label in [
            ("pve", "🌊 PvE (волны врагов)"),
            ("pvp", "⚔ PvP (только игроки)"),
            ("mixed", "🎯 Mixed (и то, и то)"),
        ]:
            rb = QRadioButton(mode_label)
            rb.setStyleSheet(f"color: {PALETTE.text_primary};")
            rb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._mode_buttons[mode_id] = rb
            self._mode_group.addButton(rb)
            mode_row.addWidget(rb)
            if mode_id == "pve":
                rb.setChecked(True)
        mode_row.addStretch(1)
        card_layout.addLayout(mode_row)

        # HP стен
        hp_lbl = QLabel("HP стен (0 = по типу):")
        hp_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; padding-top: 8px;"
        )
        card_layout.addWidget(hp_lbl)
        self._wall_hp_input = QSpinBox()
        self._wall_hp_input.setRange(0, 9999)
        self._wall_hp_input.setValue(0)
        self._wall_hp_input.setSpecialValueText("По типу блока")
        self._wall_hp_input.setStyleSheet(f"""
            QSpinBox {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 6px 10px;
                color: {PALETTE.text_primary};
            }}
        """)
        card_layout.addWidget(self._wall_hp_input)

        # Типы блоков — подсказка
        blocks_hint = QLabel(
            "🪵 Дерево (30 HP, пробиваемое)  ·  "
            "🧱 Кирпич (60 HP, пробиваемое)  ·  "
            "🏗 Бетон (200 HP, пуленепробиваемый — только гранаты!)"
        )
        blocks_hint.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px; padding: 4px;"
        )
        blocks_hint.setWordWrap(True)
        card_layout.addWidget(blocks_hint)

        # Размеры команд — настраиваемые (1v1, 3v3, 5v5, и т.п.)
        team_size_row = QHBoxLayout()
        team_size_lbl = QLabel("Размеры команд:")
        team_size_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; padding-top: 8px;"
        )
        team_size_row.addWidget(team_size_lbl)

        red_size_lbl = QLabel("🔴")
        red_size_lbl.setStyleSheet(f"color: {PALETTE.danger};")
        team_size_row.addWidget(red_size_lbl)
        self._team_red_size_input = QSpinBox()
        self._team_red_size_input.setRange(1, 10)
        self._team_red_size_input.setValue(5)
        self._team_red_size_input.setFixedWidth(50)
        self._team_red_size_input.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._team_red_size_input.setStyleSheet(f"""
            QSpinBox {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 4px 8px;
                color: {PALETTE.text_primary};
            }}
        """)
        team_size_row.addWidget(self._team_red_size_input)

        vs_lbl = QLabel("vs")
        vs_lbl.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
        team_size_row.addWidget(vs_lbl)

        blue_size_lbl = QLabel("🔵")
        blue_size_lbl.setStyleSheet("color: #60a0ff;")
        team_size_row.addWidget(blue_size_lbl)
        self._team_blue_size_input = QSpinBox()
        self._team_blue_size_input.setRange(1, 10)
        self._team_blue_size_input.setValue(5)
        self._team_blue_size_input.setFixedWidth(50)
        self._team_blue_size_input.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._team_blue_size_input.setStyleSheet(f"""
            QSpinBox {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 4px 8px;
                color: {PALETTE.text_primary};
            }}
        """)
        team_size_row.addWidget(self._team_blue_size_input)
        team_size_row.addStretch(1)
        card_layout.addLayout(team_size_row)

        # Team selector
        team_lbl = QLabel("Команда:")
        team_lbl.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; padding-top: 8px;"
        )
        card_layout.addWidget(team_lbl)
        self._team_buttons: dict[str, QRadioButton] = {}
        self._team_group = QButtonGroup(self)
        team_row = QHBoxLayout()
        for team_id, label in _TEAM_LABELS.items():
            rb = QRadioButton(label)
            rb.setStyleSheet(f"color: {PALETTE.text_primary};")
            rb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._team_buttons[team_id] = rb
            self._team_group.addButton(rb)
            team_row.addWidget(rb)
            if team_id == TEAM_NONE:
                rb.setChecked(True)
        team_row.addStretch(1)
        card_layout.addLayout(team_row)

        # Join button
        join_btn = QPushButton("▶  Войти в сессию")
        join_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        join_btn.setFixedHeight(44)
        join_btn.setStyleSheet(_glass_btn_qss(primary=True))
        join_btn.clicked.connect(self._on_join_session)
        card_layout.addWidget(join_btn)

        self._lobby_status = QLabel("")
        self._lobby_status.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        card_layout.addWidget(self._lobby_status)

        layout.addWidget(card)
        layout.addStretch(1)

        # Hint
        hint = QLabel(
            "💡 Создай сессию → позови друзей через !voxel → они войдут в ту же сессию по имени.\n"
            "🤖 В игре будет кнопка «Добавить бота» — NPC-противник в твою сессию.\n"
            "👥 Если ≥2 разных команд — включается PvP между игроками."
        )
        hint.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px; padding: 12px;"
            f"background: {PALETTE.surface_1}; border-radius: {RADIUS}px;"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        return w

    def _build_game_screen(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Left: canvas
        left = QVBoxLayout()
        left.setSpacing(8)

        title = QLabel("🔫  Воксельный Шутер")
        title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 16px; font-weight: 700;"
        )
        left.addWidget(title)

        self._canvas = VoxelCanvas()
        self._canvas.input_changed.connect(self._on_canvas_input)
        left.addWidget(self._canvas)

        # Bottom controls
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_bot_btn = QPushButton("🤖 Добавить бота")
        add_bot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_bot_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        add_bot_btn.setStyleSheet(_glass_btn_qss(primary=True))
        add_bot_btn.clicked.connect(self._on_add_bot)
        btn_row.addWidget(add_bot_btn)

        fill_bots_btn = QPushButton("🤖🤖 Заполнить ботами")
        fill_bots_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fill_bots_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        fill_bots_btn.setStyleSheet(_glass_btn_qss())
        fill_bots_btn.clicked.connect(self._on_fill_bots)
        btn_row.addWidget(fill_bots_btn)

        start_btn = QPushButton("▶ Старт игры")
        start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        start_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        start_btn.setStyleSheet(_glass_btn_qss(primary=True))
        start_btn.clicked.connect(self._on_start_game)
        btn_row.addWidget(start_btn)

        leave_btn = QPushButton("← Выйти в лобби")
        leave_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        leave_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        leave_btn.setStyleSheet(_glass_btn_qss(danger=True))
        leave_btn.clicked.connect(self._on_leave_session)
        btn_row.addWidget(leave_btn)

        close_btn = QPushButton("✕ Закрыть")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(_glass_btn_qss())
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        btn_row.addStretch(1)
        left.addLayout(btn_row)
        layout.addLayout(left, 1)

        # Right: players panel
        right = QVBoxLayout()
        right.setSpacing(10)

        players_title = QLabel("👥  Игроки")
        players_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )
        right.addWidget(players_title)

        self._players_list = QListWidget()
        self._players_list.setStyleSheet(f"""
            QListWidget {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 4px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 8px;
                color: {PALETTE.text_primary};
            }}
        """)
        self._players_list.setMinimumWidth(220)
        self._players_list.setMaximumWidth(280)
        right.addWidget(self._players_list, 1)

        layout.addLayout(right)

        return w

    # ── HTTP API: /voxel/sessions, /voxel/addbot ──────────────────

    def _refresh_voice_info(self) -> None:
        """Узнаёт порт voxel-сервера (HTTP+2) через /voxel/info.

        Транспорт — в lib.client: вьюха не собирает заголовки сама."""
        if not self._base_url:
            return

        def _do():
            return client.get_voxel_info(self._base_url, self._player_name, self._access_key)

        self._bridge.run(_do, on_success=self._on_voice_info, on_error=lambda _e: None)

    def _on_voice_info(self, info: dict) -> None:
        if not info.get("enabled"):
            self._lobby_status.setText("⚠ Голосовой сервер не запущен")
            return
        port = info.get("port", 0)
        if port == 0:
            return
        # Хост берём из base_url
        from urllib.parse import urlsplit
        parts = urlsplit(self._base_url)
        self._voice_host = parts.hostname or "127.0.0.1"
        self._voice_port = port

    def _refresh_sessions(self) -> None:
        """GET /voxel/sessions → обновляем список в лобби."""
        if not self._base_url or self._current_screen != "lobby":
            return

        def _do():
            return client.get_voxel_sessions(self._base_url, self._player_name, self._access_key)

        self._bridge.run(_do, on_success=self._on_sessions_loaded, on_error=lambda _e: None)

    def _on_sessions_loaded(self, sessions: list) -> None:
        self._sessions_list.clear()
        if not sessions:
            item = QListWidgetItem("Нет активных сессий — создай новую!")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._sessions_list.addItem(item)
            return
        for s in sessions:
            sid = s["session_id"]
            mode = s.get("game_mode", "pve")
            mode_label = {"pve": "🌊 PvE", "pvp": "⚔ PvP", "mixed": "🎯 Mixed"}.get(mode, mode)
            text = (
                f"🎮 {sid}  ·  {mode_label}\n"
                f"   👥 {s['players']}/{s['max_players']}  ·  🤖 {s['bots']} ботов  ·  "
                f"🌊 волна {s['wave']}"
                + ("  ·  ⚔ PvP активен" if s.get("pvp") else "")
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            self._sessions_list.addItem(item)

    def _on_create_session(self) -> None:
        """Создаёт новую сессию через POST /voxel/create (транспорт — lib.client).
        Передаёт mode + wall_hp из UI настроек.
        """
        from PySide6.QtWidgets import QInputDialog

        sid, ok = QInputDialog.getText(self, "Создать сессию", "Имя сессии:")
        if not ok or not sid.strip():
            return
        sid = sid.strip()

        # Получаем выбранный режим
        game_mode = "pve"
        for mode_id, rb in self._mode_buttons.items():
            if rb.isChecked():
                game_mode = mode_id
                break

        wall_hp = self._wall_hp_input.value()
        team_red_size = self._team_red_size_input.value()
        team_blue_size = self._team_blue_size_input.value()

        def _do():
            return client.voxel_create_session(
                self._base_url, self._player_name, sid,
                game_mode=game_mode, wall_hp=wall_hp,
                team_red_size=team_red_size, team_blue_size=team_blue_size,
                access_key=self._access_key,
            )

        self._bridge.run(_do, on_success=lambda _r: self._refresh_sessions(),
                         on_error=lambda e: self._lobby_status.setText(f"⚠ {e}"))

    def _on_join_session(self) -> None:
        """Подключается к выбранной сессии через TCP."""
        if not self._voice_host or self._voice_port == 0:
            self._lobby_status.setText("⚠ Voxel-сервер недоступен — подождите…")
            self._refresh_voice_info()
            return

        # Получаем session_id из выбранного элемента
        item = self._sessions_list.currentItem()
        if item is None:
            self._lobby_status.setText("⚠ Выбери сессию или создай новую")
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(sid, str):
            self._lobby_status.setText("⚠ Создай новую сессию")
            return

        # Выбранная команда
        team = TEAM_NONE
        for tid, rb in self._team_buttons.items():
            if rb.isChecked():
                team = tid
                break

        # Подключаемся к TCP-серверу
        self._lobby_status.setText("Подключаюсь…")
        ok = self._client.connect(self._voice_host, self._voice_port,
                                    self._player_name, self._access_key)
        if not ok:
            self._lobby_status.setText("⚠ Не удалось подключиться")
            return
        self._client.join_session(sid, team=team)

    def _on_add_bot(self) -> None:
        """POST /voxel/addbot — добавить бота в текущую сессию.
        Сначала спрашивает команду через QInputDialog.
        """
        if not self._client.session_id:
            return

        from PySide6.QtWidgets import QInputDialog

        sid = self._client.session_id

        # Выбор команды для бота
        items = ["🔴 Красная", "🔵 Синяя", "⚪ Без команды"]
        team_map = {"🔴 Красная": "red", "🔵 Синяя": "blue", "⚪ Без команды": "none"}
        choice, ok = QInputDialog.getItem(
            self, "Добавить бота", "Команда бота:", items, 2, editable=False
        )
        if not ok:
            return
        team = team_map.get(choice, "none")

        def _do():
            return client.voxel_add_bot(
                self._base_url, self._player_name, sid, team, self._access_key
            )

        self._bridge.run(_do, on_success=lambda _r: None, on_error=lambda _e: None)

    def _on_fill_bots(self) -> None:
        """Заполняет все пустые слоты ботами через POST /voxel/fillbots."""
        if not self._client.session_id:
            return
        sid = self._client.session_id

        def _do():
            return client.voxel_fill_bots(
                self._base_url, self._player_name, sid, self._access_key
            )

        self._bridge.run(_do, on_success=lambda _r: None, on_error=lambda _e: None)

    def _on_start_game(self) -> None:
        """Запускает игру: заполняет пустые слоты + переводит в playing."""
        if not self._client.session_id:
            return
        sid = self._client.session_id

        def _do():
            return client.voxel_start_game(
                self._base_url, self._player_name, sid, self._access_key
            )

        self._bridge.run(_do, on_success=lambda _r: None, on_error=lambda _e: None)

    # ── Session callbacks ──────────────────────────────────────────

    def _on_joined(self, session_id: str, player_id: str) -> None:
        """Сервер подтвердил вход — переключаемся на экран игры."""
        self._current_screen = "game"
        self._canvas.set_player_id(player_id)
        self._stack.setCurrentWidget(self._game_widget)
        # ВАЖНО: canvas должен получить фокус, иначе keyPressEvent не сработает.
        # setFocus() после setCurrentWidget — даём Qt время переключить страницу.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, lambda: self._canvas.setFocus())
        # Останавливаем poll лобби
        self._poll_timer.stop()

    def _on_snapshot(self, snapshot: dict) -> None:
        """Получили snapshot — отдаём в canvas + обновляем список игроков."""
        self._canvas.set_snapshot(snapshot)
        self._update_players_list(snapshot.get("players", []))

    def _on_session_error(self, msg: str) -> None:
        self._lobby_status.setText(f"⚠ {msg}")

    def _on_session_disconnected(self) -> None:
        self._current_screen = "lobby"
        self._stack.setCurrentWidget(self._lobby_widget)
        self._lobby_status.setText("Соединение разорвано")
        self._poll_timer.start(3000)

    def _on_canvas_input(self, input_data: dict) -> None:
        """Canvas накопил input — отправляем на сервер."""
        if self._client.is_connected():
            self._client.send_input(input_data)

    def _update_players_list(self, players: list[dict]) -> None:
        self._players_list.clear()
        for p in players:
            team_label = _TEAM_LABELS.get(p.get("team", TEAM_NONE), "?")
            bot_marker = " 🤖" if p.get("is_bot") else ""
            alive_marker = "" if p.get("alive", True) else " 💀"
            text = (f"{p.get('name', '?')}{bot_marker}{alive_marker}\n"
                    f"  {team_label}  ·  K: {p.get('kills', 0)}  D: {p.get('deaths', 0)}  "
                    f"💰 {p.get('money', 0)}")
            self._players_list.addItem(text)

    def _on_leave_session(self) -> None:
        """Выход из сессии — возвращаемся в лобби."""
        self._client.leave()
        self._current_screen = "lobby"
        self._stack.setCurrentWidget(self._lobby_widget)
        self._poll_timer.start(3000)

    # ── Жизненный цикл ─────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._poll_timer.stop()
        self._client.leave()
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._current_screen == "game":
                self._on_leave_session()
                return
            self.close()
            return
        if self._current_screen == "game":
            self._canvas.keyPressEvent(event)
