"""ChessGameDialog — «Красивые Шахматы»: полноценная игра против ИИ поверх чата.

Открывается по команде !chess (или текущему триггеру) из чата.

МОДУЛЬНАЯ СТРУКТУРА (ранее всё жило в этом файле — 2389 строк):
  - lib/chess_engine.py          — движок и ИИ (чистая логика, без Qt);
                                   используется и сервером (lib/chess_bot.py)
  - lib/client_core/chess_match.py — ядро матча (Фаза 2): политика сохранения
                                   результата, сетевой матч (!chess host/join/…),
                                   рейтинг ELO — чистый Python, тестируется без PySide6
  - qt_app/widgets/chess_board_widget.py    — доска ChessBoardWidget
  - qt_app/widgets/chess_piece_renderer.py  — рендер фигур (3 скина)
  - этот файл                    — View: композит ChessGame (доска + инфо-панель),
                                   ChessGameDialog (доска + рейтинг) и таймеры

Импорты сохранены для обратной совместимости: как и раньше,
`from qt_app.widgets.chess_game_dialog import ChessEngine, ChessAI,
ChessBoardWidget, ...` продолжает работать.

ВАЖНО: это НЕ парный сетевой матч между двумя людьми — в режимах
AI/PVP оба игрока играют на одной стороне (как в змейке/orbital).
Для игры по сети используйте режим «Сеть» (MODE_NET) — матч живёт на
relay-сервере через ChessBot.process_command и попадает в общий
рейтинг ELO, видимый всем через !chess top.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib.client_core.chess_match import (
    GAME_OVER_WATCH_MS,
    NET_POLL_INTERVAL_MS,
    ChessMatch,
    should_leave_on_close,
)
from lib.games.chess_engine import ChessAI, ChessEngine
from qt_app.async_bridge import AsyncBridge
from qt_app.widgets.chess_board_widget import ChessBoardWidget
from qt_app.widgets.chess_piece_renderer import (
    PIECE_SKIN_DEFAULT,
    PIECE_SKIN_LABELS,
    PIECE_SKINS,
    PIECE_UNICODE,
)

__all__ = [
    "PIECE_SKINS",
    "PIECE_SKIN_DEFAULT",
    "PIECE_SKIN_LABELS",
    "PIECE_UNICODE",
    "ChessAI",
    "ChessBoardWidget",
    "ChessEngine",
    "ChessGame",
    "ChessGameDialog",
]


# ═══════════════════════════════════════════════════════════════════
# КОМПОЗИТ: ДОСКА СЛЕВА + ИНФО-ПАНЕЛЬ СПРАВА
# ═══════════════════════════════════════════════════════════════════

class ChessGame(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(900, 700)
        self.setFocusPolicy(Qt.StrongFocus)

        self.board = ChessBoardWidget(self)
        self.board.setGeometry(20, 40, 640, 640)
        # ВАЖНО: keyPressEvent (F/R/P/1/2/3) определён в ChessBoardWidget,
        # но без явной focus policy он не получит фокус — клавиши не сработают.
        # StrongFocus позволяет получать фокус и по клику, и по Tab.
        self.board.setFocusPolicy(Qt.StrongFocus)
        # Передаём фокус доске сразу при создании.
        self.board.setFocus()

        # Боковая панель
        self.panel_x = 680

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(50)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(35, 35, 40))

        # Заголовок
        p.setPen(QColor(220, 200, 160))
        p.setFont(QFont("Consolas", 18, QFont.Bold))
        p.drawText(20, 28, "♔ Красивые Шахматы")

        # Панель справа
        x = self.panel_x
        y = 40

        # Бейдж режима
        is_pvp = self.board.mode == self.board.MODE_PVP
        is_net = self.board.mode == self.board.MODE_NET
        if is_net:
            mode_label = "🌐 PVP по сети"
            mode_color = QColor(140, 220, 180)
        elif is_pvp:
            mode_label = "👥 PVP (hot-seat)"
            mode_color = QColor(220, 180, 120)
        else:
            mode_label = "🤖 vs ИИ"
            mode_color = QColor(160, 200, 240)
        p.setPen(mode_color)
        p.setFont(QFont("Consolas", 11, QFont.Bold))
        p.drawText(x, y, mode_label)
        y += 22

        # Статус
        p.setPen(QColor(220, 220, 220))
        p.setFont(QFont("Consolas", 11))
        # Статус может быть длинным в PVP/NET (с именем игрока) — обрежем.
        status = self.board.status_text
        if len(status) > 30:
            status = status[:28] + "…"
        p.drawText(x, y, status)

        # Информация
        y += 32
        p.setFont(QFont("Consolas", 10))
        # Текущий скин фигур — отображается во всех режимах
        skin_label = PIECE_SKIN_LABELS.get(self.board.piece_skin, "?")
        if is_net:
            my_color = "белые" if self.board.net_color else "чёрные"
            opp = self.board.net_opponent_name or "?"
            mid = self.board.net_match_id or "—"
            info = [
                f"Ты: {my_color}",
                f"Соперник: {opp}",
                f"Код матча: {mid}",
                f"Партия №{self.board.engine.fullmove}",
                f"Скин фигур: {skin_label}",
                "",
                "Управление:",
                "ЛКМ — ходить (твой ход)",
                "F — перевернуть доску",
                "P — сменить режим",
                "",
                "ELO учитывается у обоих",
            ]
        elif is_pvp:
            turn = self.board.white_player_name if self.board.engine.white_to_move else self.board.black_player_name
            info = [
                f"Ход: {turn}",
                f"Партия №{self.board.engine.fullmove}",
                f"Скин фигур: {skin_label}",
                f"Авто-переворот: {'вкл' if self.board.auto_flip_pvp else 'выкл'}",
                "",
                "Управление:",
                "ЛКМ — ходить (оба игрока)",
                "F — перевернуть доску",
                "R — новая партия",
                "P — сменить режим",
                "",
                "ELO в PVP не сохраняется",
            ]
        else:
            info = [
                f"Ход: {'Белые (вы)' if self.board.engine.white_to_move else 'Чёрные (ИИ)'}",
                f"Партия №{self.board.engine.fullmove}",
                f"Скин фигур: {skin_label}",
                "",
                "Управление:",
                "ЛКМ — ходить",
                "F — перевернуть доску",
                "R — новая партия",
                "P — сменить режим",
                "1/2/3 — сложность ИИ",
                "",
                f"ИИ глубина: {self.board.ai.depth}",
            ]
        p.setPen(QColor(190, 190, 195))
        for line in info:
            p.drawText(x, y, line)
            y += 18

        # История ходов (последние 12)
        y += 8
        p.setPen(QColor(180, 160, 120))
        p.setFont(QFont("Consolas", 10, QFont.Bold))
        p.drawText(x, y, "История:")
        y += 18
        p.setFont(QFont("Consolas", 9))
        p.setPen(QColor(160, 160, 160))

        hist = self.board.engine.history
        moves_shown = []
        for i in range(0, len(hist), 2):
            w = self._move_notation(hist[i])
            b = self._move_notation(hist[i + 1]) if i + 1 < len(hist) else ""
            moves_shown.append(f"{i // 2 + 1}. {w} {b}")
        for line in moves_shown[-16:]:
            p.drawText(x, y, line)
            y += 16

    def _move_notation(self, state) -> str:
        fr, to, captured, _, _, _, promo = state
        files = "abcdefgh"
        ranks = "87654321"
        f1, r1 = files[fr % 8], ranks[fr // 8]
        f2, r2 = files[to % 8], ranks[to // 8]
        pchar = ""
        if promo:
            pchar = "=" + promo.upper()
        capture = "x" if captured != '.' else ""
        return f"{f1}{r1}{capture}{f2}{r2}{pchar}"


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГ: ОБЁРТКА ДЛЯ ЧАТА (доска слева + рейтинг ELO справа)
# ═══════════════════════════════════════════════════════════════════

_BG = "#232328"
_PANEL_BG = "#1a1a1f"
_PANEL_BORDER = "#3a3a44"
_TEXT_PRIMARY = "#f0f0f5"
_TEXT_DIM = "#8e8e99"
_ACCENT = "#dcc8a0"


class ChessGameDialog(QDialog):
    """Диалог-обёртка: шахматная доска слева, рейтинг ELO справа.

    Поддерживает три режима:
      • vs ИИ  — игрок против ChessAI. Результат партии (мат/пат)
                 отправляется на сервер через бот-команду "result"
                 и обновляет таблицу рейтинга.
      • PVP    — два игрока за одним компьютером (hot-seat). ELO не
                 сохраняется (дружеская партия), но рейтинг других
                 игроков по-прежнему виден.
      • Сеть   — онлайн-матч через relay-сервер (!chess host/join),
                 ELO обновляется у обоих игроков сервером.
    """

    def __init__(
        self,
        parent: QWidget | None,
        base_url: str,
        player_name: str,
        access_key: str,
        bot_id: str = "chess",
        bot_meta: dict | None = None,
        initial_tab: str = "play",
    ):
        super().__init__(parent)
        self.setWindowTitle("♔ Красивые Шахматы")
        self.setModal(False)
        self.setStyleSheet(f"background: {_BG};")

        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._bot_meta = bot_meta or {}
        self._initial_tab = initial_tab

        self._bridge = AsyncBridge()
        # Ядро матча (Фаза 2): политика сохранения результата, сетевой матч,
        # рейтинг ELO — чистый Python, без Qt. Диалог только рисует и крутит
        # таймеры.
        self._match = ChessMatch(
            base_url, player_name, access_key, bot_id=bot_id, runner=self._bridge.run
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # -- слева: игровой виджет (доска + встроенная инфо-панель) --
        self._game = ChessGame(self)
        root.addWidget(self._game)

        # Колбэк, чтобы доска сообщила диалогу о завершении партии.
        self._game.board.on_game_over = self._on_board_game_over

        # -- справа: рейтинг + кнопки --
        right = QVBoxLayout()
        right.setSpacing(10)

        title = QLabel("🏆 Рейтинг ELO")
        title.setStyleSheet(
            f"color: {_ACCENT}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 16px; font-weight: bold;"
        )
        right.addWidget(title)

        self._leaderboard_label = QLabel("Загрузка…")
        self._leaderboard_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 12px; background: {_PANEL_BG}; border: 1px solid {_PANEL_BORDER};"
            f"border-radius: 8px; padding: 10px;"
        )
        self._leaderboard_label.setFixedWidth(240)
        self._leaderboard_label.setWordWrap(True)
        self._leaderboard_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.addWidget(self._leaderboard_label, 1)

        self._my_stats_label = QLabel("")
        self._my_stats_label.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        self._my_stats_label.setWordWrap(True)
        right.addWidget(self._my_stats_label)

        # ─── Переключатель режима ИИ / PVP / Сеть ──────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self._btn_mode_ai = QPushButton("🤖 ИИ")
        self._btn_mode_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mode_ai.setCheckable(True)
        self._btn_mode_ai.clicked.connect(lambda _checked=False: self._set_mode(ChessBoardWidget.MODE_AI))
        mode_row.addWidget(self._btn_mode_ai)

        self._btn_mode_pvp = QPushButton("👥 PVP")
        self._btn_mode_pvp.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mode_pvp.setCheckable(True)
        self._btn_mode_pvp.clicked.connect(lambda _checked=False: self._set_mode(ChessBoardWidget.MODE_PVP))
        mode_row.addWidget(self._btn_mode_pvp)

        self._btn_mode_net = QPushButton("🌐 Сеть")
        self._btn_mode_net.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mode_net.setCheckable(True)
        self._btn_mode_net.clicked.connect(lambda _checked=False: self._on_net_mode_clicked())
        mode_row.addWidget(self._btn_mode_net)
        right.addLayout(mode_row)

        # Чекбокс авто-переворота в PVP
        self._chk_autoflip = QCheckBox("Авто-переворот доски в PVP")
        self._chk_autoflip.setChecked(True)
        self._chk_autoflip.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        self._chk_autoflip.toggled.connect(self._on_autoflip_toggled)
        right.addWidget(self._chk_autoflip)

        # ─── Сетевые действия (видны только в MODE_NET) ───────────────
        self._net_widget = QWidget()
        net_layout = QVBoxLayout(self._net_widget)
        net_layout.setContentsMargins(0, 0, 0, 0)
        net_layout.setSpacing(4)

        self._btn_net_host = QPushButton("＋ Создать матч")
        self._btn_net_host.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_net_host.clicked.connect(lambda _checked=False: self._on_net_host())
        net_layout.addWidget(self._btn_net_host)

        self._btn_net_join = QPushButton("↪ Присоединиться")
        self._btn_net_join.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_net_join.clicked.connect(lambda _checked=False: self._on_net_join())
        net_layout.addWidget(self._btn_net_join)

        self._btn_net_resign = QPushButton("🏳 Сдаться")
        self._btn_net_resign.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_net_resign.clicked.connect(lambda _checked=False: self._on_net_resign())
        net_layout.addWidget(self._btn_net_resign)

        self._btn_net_leave = QPushButton("✕ Выйти из матча")
        self._btn_net_leave.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_net_leave.clicked.connect(lambda _checked=False: self._on_net_leave())
        net_layout.addWidget(self._btn_net_leave)

        self._net_status_label = QLabel("")
        self._net_status_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 10px; background: {_PANEL_BG}; border: 1px solid {_PANEL_BORDER};"
            f"border-radius: 6px; padding: 6px;"
        )
        self._net_status_label.setWordWrap(True)
        net_layout.addWidget(self._net_status_label)
        right.addWidget(self._net_widget)
        self._net_widget.setVisible(False)

        # ─── Селектор скина фигур ──────────────────────────────────────
        skin_title = QLabel("♟ Скин фигур")
        skin_title.setStyleSheet(
            f"color: {_ACCENT}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 12px; font-weight: bold; margin-top: 6px;"
        )
        right.addWidget(skin_title)

        skin_row = QHBoxLayout()
        skin_row.setSpacing(4)
        self._btn_skin = {}
        for skin_id in PIECE_SKINS:
            label = PIECE_SKIN_LABELS.get(skin_id, skin_id)
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, _s=skin_id: self._set_skin(_s))
            skin_row.addWidget(btn)
            self._btn_skin[skin_id] = btn
        right.addLayout(skin_row)

        # Стандартные кнопки
        btn_row = QHBoxLayout()
        btn_restart = QPushButton("↻ Новая партия")
        btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_restart.clicked.connect(lambda _checked=False: self._on_restart())
        btn_row.addWidget(btn_restart)

        btn_refresh = QPushButton("⟳ Рейтинг")
        btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh.clicked.connect(lambda _checked=False: self._refresh_leaderboard())
        btn_row.addWidget(btn_refresh)

        btn_close = QPushButton("✕ Закрыть")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.clicked.connect(lambda _checked=False: self.close())
        btn_row.addWidget(btn_close)
        right.addLayout(btn_row)

        plain_buttons = [
            btn_restart, btn_refresh, btn_close,
            self._btn_mode_ai, self._btn_mode_pvp, self._btn_mode_net,
            self._btn_net_host, self._btn_net_join,
            self._btn_net_resign, self._btn_net_leave,
            *self._btn_skin.values(),
        ]
        for b in plain_buttons:
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {_PANEL_BORDER}; color: {_TEXT_PRIMARY};
                    border: none; border-radius: 6px; padding: 8px 12px;
                    font-family: 'Consolas, Menlo, monospace'; font-weight: bold;
                    min-height: 18px;
                }}
                QPushButton:hover {{ background: #4a4a56; }}
                QPushButton:pressed {{ background: #2a2a32; }}
                QPushButton:checked {{
                    background: {_ACCENT}; color: #1a1a1f;
                }}
                QPushButton:checked:hover {{
                    background: #e8d4ac;
                }}
                QPushButton:disabled {{
                    background: #2a2a30; color: #5a5a60;
                }}
            """)

        root.addLayout(right)

        # -- подписка на ядро: ядро решает, диалог рисует --
        self._match.on_status = self._on_net_status
        self._match.on_my_stats = self._on_my_stats
        self._match.on_leaderboard = self._on_leaderboard
        self._match.on_leaderboard_error = self._on_leaderboard_error
        self._match.on_no_connection = self._on_leaderboard_no_connection
        self._match.on_match_ready = self._on_match_ready
        self._match.on_match_state = self._on_match_state
        self._match.on_match_reset = self._on_match_reset
        self._match.on_move_rejected = self._on_move_rejected
        self._match.on_net_error = self._on_net_error

        # -- фоновый опрос состояния игры на предмет game over --
        # (нужен только для режима ИИ, в PVP колбэк on_game_over вызывается
        # напрямую из доски — но таймер оставляем как страховку.)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_game_over)
        self._poll_timer.start(GAME_OVER_WATCH_MS)

        # -- отдельный таймер для опроса сетевого матча (MODE_NET) --
        # Раз в 800 мс запрашивает !chess state и применяет новые ходы.
        self._net_poll_timer = QTimer(self)
        self._net_poll_timer.timeout.connect(self._net_poll)
        self._net_poll_interval = NET_POLL_INTERVAL_MS
        # Колбэк доски для отправки ходов на сервер.
        self._game.board.on_net_move = self._net_send_move

        # -- синхронизируем начальный режим с UI --
        self._sync_mode_buttons()

        self._refresh_leaderboard()
        if initial_tab == "leaderboard":
            self._refresh_leaderboard()

    # ------------------------------------------------------------------
    # Переключение режима ИИ ↔ PVP
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        """Переключает режим игры. В PVP сбрасываем доску к новой партии."""
        board = self._game.board
        if board.mode == mode:
            self._sync_mode_buttons()
            return
        board.set_mode(mode)
        self._match.reset_result_flag()
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        is_pvp = self._game.board.mode == ChessBoardWidget.MODE_PVP
        is_net = self._game.board.mode == ChessBoardWidget.MODE_NET
        is_ai = not (is_pvp or is_net)
        self._btn_mode_ai.setChecked(is_ai)
        self._btn_mode_pvp.setChecked(is_pvp)
        self._btn_mode_net.setChecked(is_net)
        self._chk_autoflip.setVisible(is_pvp)
        self._net_widget.setVisible(is_net)
        # Управляем таймером опроса сети
        if is_net:
            if not self._net_poll_timer.isActive():
                self._net_poll_timer.start(self._net_poll_interval)
        else:
            if self._net_poll_timer.isActive():
                self._net_poll_timer.stop()
        # Подсказка под таблицей рейтинга
        if is_pvp:
            self._my_stats_label.setText(
                "PVP-режим: результаты не идут в ELO.\n"
                "Два игрока ходят по очереди мышью."
            )
        elif is_net:
            self._my_stats_label.setText(
                "🌐 Сетевой PVP: игра с другом через relay.\n"
                "Создай матч и поделись кодом, или присоединись."
            )
        else:
            self._my_stats_label.setText("")
        # Также синхронизируем кнопки скинов — на случай, если skin
        # был загружен из QSettings при старте.
        self._sync_skin_buttons()

    # ------------------------------------------------------------------
    # Онлайн-PVP: сетевые операции
    # ------------------------------------------------------------------

    def _on_net_mode_clicked(self) -> None:
        """Обработчик клика по кнопке «🌐 Сеть». Переключает в NET-режим."""
        self._set_mode(ChessBoardWidget.MODE_NET)

    # ------------------------------------------------------------------
    # Колбэки ядра (lib/client_core/chess_match.py) — здесь только рисование
    # ------------------------------------------------------------------

    def _on_net_status(self, text: str) -> None:
        self._net_status_label.setText(text)

    def _on_my_stats(self, text: str) -> None:
        self._my_stats_label.setText(text)

    def _on_net_error(self, err: str) -> None:
        self._net_status_label.setText(f"⚠ Ошибка сети: {err}")

    def _on_match_ready(self, color: bool, match_id: str, opponent_name: str) -> None:
        """Матч создан/присоединён: инициализация доски.
        Хост играет белыми, присоединившийся — чёрными.

        v3.1.1: кроме доски синхронизируем UI диалога — кнопки режима
        (ИИ/PVP/Сеть), видимость панели сети и net_poll_timer. Раньше
        это делалось только в ``_set_mode``, который не всегда вызывается
        перед ``_on_match_ready`` (например, при клике по ссылке-приглашению
        до фикса в chat_screen._open_game_with_code). Теперь UI гарантированно
        в NET-режиме, и opponent виден в статусе сразу."""
        self._game.board.set_mode(ChessBoardWidget.MODE_NET)
        self._game.board.start_net_match(
            color=color, match_id=match_id, opponent_name=opponent_name
        )
        # v3.1.1: синхронизируем кнопки режима, видимость панели сети
        # и net_poll_timer — на случай, если _set_mode(MODE_NET) ещё не
        # был вызван (путь через клик по ссылке-приглашению).
        if self._game.board.mode == ChessBoardWidget.MODE_NET:
            self._sync_mode_buttons()

    def _on_match_state(self, match: dict) -> None:
        """Состояние матча с сервера (ход соперника, финиш) → на доску."""
        self._game.board.apply_net_state(match)
        # v3.1: матч завершён — сбрасываем net_match_id, чтобы игрок мог
        # сразу создать новую игру (см. ChessBot._cmd_host: finished матчи
        # больше не блокируют host). Доска остаётся с финальной позицией.
        if isinstance(match, dict) and match.get("status") == "finished":
            self._game.board.net_match_id = None

    def _on_match_reset(self) -> None:
        """Выход из матча: сброс сетевого состояния доски (бывшее тело
        _on_net_left)."""
        board = self._game.board
        board.net_match_id = None
        board.net_opponent_name = ""
        board.net_applied_moves = 0
        board.engine.reset()
        board.game_over = False
        board.status_text = "🌐 Нет активного матча"
        board.update()

    def _on_move_rejected(self, text: str) -> None:
        self._net_status_label.setText(f"⚠ Ход отклонён: {text}")
        self._net_poll()  # немедленный опрос для ресинхронизации

    # ------------------------------------------------------------------
    # Сетевые действия — тонкие вызовы ядра
    # ------------------------------------------------------------------

    def _on_net_host(self) -> None:
        """Кнопка «Создать матч» — core.host() (!chess host)."""
        self._match.host()

    def _on_net_join(self) -> None:
        """Кнопка «Присоединиться» — спрашивает код матча и зовёт core.join().
        Нормализация кода (trim + верхний регистр) — в ядре."""
        from PySide6.QtWidgets import QInputDialog
        mid, ok = QInputDialog.getText(
            self, "Присоединиться к матчу",
            "Введите 4-значный код матча:",
        )
        if not ok or not mid.strip():
            return
        self._match.join(mid)

    def _on_net_resign(self) -> None:
        """Кнопка «Сдаться» — core.resign() (!chess resign)."""
        if not self._game.board.net_match_id:
            self._net_status_label.setText("Нет активного матча.")
            return
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "Сдаться",
            "Сдаться в текущем матче? Победа будет присуждена сопернику.",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._match.resign()

    def _on_net_leave(self) -> None:
        """Кнопка «Выйти из матча» — core.leave() (!chess leave)."""
        if not self._game.board.net_match_id:
            self._net_status_label.setText("Нет активного матча.")
            return
        self._match.leave()

    def _net_send_move(self, uci: str) -> None:
        """Колбэк от доски: игрок сделал ход — отправка через ядро.
        Сервер подтвердит или отклонит; фактическое обновление доски при
        расхождении делает _net_poll через apply_net_state()."""
        self._match.send_move(uci)

    def _net_poll(self) -> None:
        """Тик таймера: ядро опрашивает !chess state (политика — в ядре),
        состояние доски передаётся параметрами."""
        board = self._game.board
        self._match.poll_state(
            is_net_mode=board.mode == ChessBoardWidget.MODE_NET,
            game_over=board.game_over,
            net_color=board.net_color,
        )

    def _set_skin(self, skin: str) -> None:
        """Переключает скин фигур и обновляет UI кнопок."""
        self._game.board.set_skin(skin)
        self._sync_skin_buttons()

    def _sync_skin_buttons(self) -> None:
        current = self._game.board.piece_skin
        for skin_id, btn in self._btn_skin.items():
            btn.setChecked(skin_id == current)

    def _on_autoflip_toggled(self, checked: bool) -> None:
        self._game.board.auto_flip_pvp = checked
        self._game.board.update()

    # ------------------------------------------------------------------
    # Game over → политика сохранения результата — в ядре
    # ------------------------------------------------------------------

    def _on_board_game_over(self, outcome: str, winner_color: str | None) -> None:
        """Колбэк из доски: что делать с результатом (ELO в режиме ИИ,
        ничего в PVP/NET) решает ядро."""
        board = self._game.board
        self._match.on_board_game_over(
            outcome, winner_color, mode=board.mode, player_side=board.player_side
        )

    def _check_game_over(self) -> None:
        """Страховка (QTimer): если колбэк доски не сработал, исход ловим
        по состоянию движка. Факты передаются параметрами, политика — в ядре."""
        board = self._game.board
        self._match.check_game_over_fallback(
            game_over=board.game_over,
            mode=board.mode,
            player_side=board.player_side,
            is_checkmate=board.engine.is_checkmate(),
            white_to_move=board.engine.white_to_move,
        )

    def _on_restart(self) -> None:
        board = self._game.board
        board._reset_game()
        self._match.reset_result_flag()
        self._sync_mode_buttons()

    # ------------------------------------------------------------------
    # Таблица рейтинга
    # ------------------------------------------------------------------

    def _refresh_leaderboard(self) -> None:
        self._match.refresh_leaderboard()

    def showEvent(self, event):
        """v3: при первом показе диалога — если у нас уже есть состояние
        матча (из !chess host/join в чате), применяем его сразу, без
        повторного сетевого запроса. Иначе — обычное поведение (рейтинг
        подтягивается, матч начнётся по кнопке внутри диалога)."""
        super().showEvent(event)
        # Применяем initial_match только один раз (флаг).
        if getattr(self, "_initial_match_applied", False):
            return
        self._initial_match_applied = True
        # bot_meta может содержать match-данные, если диалог открыт через
        # interpret_dispatch → _open_game_dialog (после !chess host/join).
        initial_match = None
        if isinstance(self._bot_meta, dict):
            # interpret_dispatch кладёт сам match-словарь в meta (см.
            # chat_session.py:match_created/match_joined).
            if self._bot_meta.get("match_id") or self._bot_meta.get("white"):
                initial_match = self._bot_meta
        if initial_match and hasattr(self._match, "apply_initial_match"):
            # Переключаемся в NET-режим (онлайн-матч) и применяем состояние.
            try:
                self._set_mode("net")
            except Exception:
                pass
            self._match.apply_initial_match(initial_match)

    def _on_leaderboard(self, lines: list[str]) -> None:
        if not lines:
            self._leaderboard_label.setText("<i>Пока нет партий.<br>Сыграй первую!</i>")
            return
        self._leaderboard_label.setText("<br>".join(lines))

    def _on_leaderboard_no_connection(self) -> None:
        self._leaderboard_label.setText("<i>Нет подключения к серверу</i>")

    def _on_leaderboard_error(self) -> None:
        self._leaderboard_label.setText("<i>Не удалось загрузить рейтинг</i>")

    def closeEvent(self, event) -> None:
        self._poll_timer.stop()
        if self._net_poll_timer.isActive():
            self._net_poll_timer.stop()
        # Если в активном сетевом матче — корректно выходим (соперник получит
        # победу). Вызов синхронный, как и раньше: окно всё равно закрывается.
        board = self._game.board
        if should_leave_on_close(board.mode, board.net_match_id, board.game_over):
            self._match.leave_on_close()
        super().closeEvent(event)
