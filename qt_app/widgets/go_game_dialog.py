"""GoGameDialog — «Го» для PySide6-клиента: доска, ИИ, hot-seat и сеть.

Открывается по команде !go (или текущему триггеру GoBot) из чата —
маршрутизацию делает ChatScreen._open_game_dialog (qt_app/screens/
chat_screen.py), регистрацию бота — lib/relay_server.py.

МОДУЛЬНАЯ СТРУКТУРА (зеркало шахмат):
  - lib/games/go_engine.py          — движок правил + эвристический ИИ
                                       (чистый Python, без Qt);
  - lib/client_core/go_match.py     — ядро сети/результата (тестируется
                                       без PySide6);
  - этот файл                       — View: GoBoardWidget (рисование доски)
                                       + GoGameDialog (композит, таймеры).

Три режима:
  • 🤖 ИИ  — игрок (чёрные) против эвристического GoAI (белые). Результат
             партии отправляется боту (result win/loss/draw) и идёт в ELO.
  • 👥 PVP — два игрока за одним компьютером (hot-seat). ELO не пишется.
  • 🌐 Сеть — онлайн-матч через relay (!go host/join/move). Хост — чёрные,
             код матча 4 символа; сервер-рефери валидирует каждый ход
             (взятия/ко/суицид/очерёдность), ELO обновляет сервер.

Рисование: классическая деревянная доска (можно и в тёмной теме — дерево
Го-гиобan традиционно светлое), камни — радиальные градиенты, метка
последнего хода — красная точка, ко-запрет показывается в инфо-строке.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib.client_core.go_match import (
    MODE_AI,
    MODE_NET,
    MODE_PVP,
    NET_POLL_INTERVAL_MS,
    GoMatch,
    result_for_player,
    should_leave_on_close,
)
from lib.games.go_engine import (
    BLACK,
    EMPTY,
    WHITE,
    GoAI,
    GoEngine,
    format_vertex,
    parse_vertex,
)
from qt_app.async_bridge import AsyncBridge

# Палитра диалога — те же тона, что в шахматном (единый стиль окон игр)
_BG = "#232328"
_PANEL_BG = "#1a1a1f"
_PANEL_BORDER = "#3a3a44"
_TEXT_PRIMARY = "#f0f0f5"
_TEXT_DIM = "#8e8e99"
_ACCENT = "#dcc8a0"

# Цвета доски: тёплое дерево + тёмно-коричневая сетка (традиция Го)
_WOOD = QColor("#d9b06a")
_WOOD_DARK = QColor("#c89b55")
_GRID = QColor("#5a4426")
_COORD = QColor("#6b5231")

__all__ = ["MODE_AI", "MODE_NET", "MODE_PVP", "GoBoardWidget", "GoGameDialog"]


class GoBoardWidget(QWidget):
    """Виджет доски: рисует позицию GoEngine и ловит клики/наведение.

    Виджет ВЛАДЕЕТ движком (как шахматная доска — ChessEngine). Диалог
    только командует: new_game/try_play/do_pass/apply_match — и подписан
    на колбэки on_stone_played (клик игрока по пустому пункту) и
    on_local_game_over (два паса в локальном режиме).
    """

    # колбэки (как в ChessBoardWidget — сигналы не нужны, диалог один)
    on_stone_played = None      # callable(x: int, y: int)
    on_local_game_over = None   # callable(score: dict)
    on_info_changed = None      # callable() — обновить инфо-строку

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = GoEngine(9)
        self._mode = MODE_AI
        self._my_black = True          # в сети: мой цвет (True = чёрные)
        self._hover: tuple[int, int] | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(420, 420)

    # ── служебное ────────────────────────────────────────────────────────
    @property
    def engine(self) -> GoEngine:
        return self._engine

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def my_black(self) -> bool:
        """Мой цвет в сетевом матче (True = чёрные)."""
        return self._my_black

    def is_my_turn(self) -> bool:
        """Можно ли ТУТ кликать: в AI/PVP — всегда (клик = ход стороны),
        в сети — только когда ход моего цвета и партия жива."""
        if self._engine.game_over:
            return False
        if self._mode == MODE_NET:
            return (self._engine.to_move() == BLACK) == self._my_black
        return True

    # ── управление состоянием ────────────────────────────────────────────
    def new_game(self, size: int, mode: str, my_black: bool = True) -> None:
        self._engine = GoEngine(size)
        self._mode = mode
        self._my_black = my_black
        self._hover = None
        self.update()
        self._emit_info()

    def apply_match(self, match: dict) -> None:
        """Серверный матч → доска: перегоняем moves[] через движок
        (сервер авторитетен, локальная позиция пересобирается целиком —
        расхождения исключены по построению)."""
        size = int(match.get("size") or 9)
        engine = GoEngine(size)
        moves = match.get("moves") or []

        for i, v in enumerate(moves):
            color = BLACK if i % 2 == 0 else WHITE
            try:
                if v == "pass":
                    engine.pass_turn(color)
                else:
                    vertex = parse_vertex(v, size)
                    if vertex:
                        engine.play(vertex[0], vertex[1], color)
            except Exception:
                break
        self._engine = engine
        self.update()
        self._emit_info()

    def set_my_black(self, my_black: bool) -> None:
        self._my_black = my_black

    def try_play(self, x: int, y: int) -> bool:
        """Локальный ход (AI/PVP): валидируем и применяем. False — нельзя."""
        color = self._engine.to_move()
        if not self._engine.is_legal(x, y, color):
            return False
        self._engine.play(x, y, color)
        self.update()
        self._emit_info()
        if self._engine.game_over and self.on_local_game_over:
            self.on_local_game_over(self._engine.area_score())
        return True

    def do_pass(self) -> bool:
        """Пас текущей стороны (AI/PVP). Два паса → конец."""
        if self._engine.game_over:
            return False
        self._engine.pass_turn(self._engine.to_move())
        self.update()
        self._emit_info()
        if self._engine.game_over and self.on_local_game_over:
            self.on_local_game_over(self._engine.area_score())
        return True

    # ── отрисовка ────────────────────────────────────────────────────────
    def _geometry(self) -> tuple[float, float, float]:
        """(cell, ox, oy): размер пункта и смещение левой верхней точки
        сетки. Поля — под координаты (буквы/цифры)."""
        w, h = self.width(), self.height()
        cell = min(w, h) / (self._engine.size + 1)
        return cell, (w - cell * (self._engine.size - 1)) / 2, \
            (h - cell * (self._engine.size - 1)) / 2

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        size = self._engine.size
        cell, ox, oy = self._geometry()

        # дерево: тёплые тона панели + деревянная доска с рамкой
        p.fillRect(self.rect(), QColor(_PANEL_BG))
        wood = QRectF(ox - cell * 0.8, oy - cell * 0.8,
                      cell * (size - 1) + cell * 1.6,
                      cell * (size - 1) + cell * 1.6)
        p.setBrush(QBrush(_WOOD))
        p.setPen(QPen(QColor(_WOOD_DARK), 2))
        p.drawRoundedRect(wood, 10, 10)

        # сетка
        pen = QPen(_GRID, 1.4 if size <= 13 else 1.0)
        p.setPen(pen)
        for i in range(size):
            p.drawLine(QPointF(ox + i * cell, oy),
                       QPointF(ox + i * cell, oy + (size - 1) * cell))
            p.drawLine(QPointF(ox, oy + i * cell),
                       QPointF(ox + (size - 1) * cell, oy + i * cell))
        # внешняя рамка чуть толще
        p.setPen(QPen(_GRID, 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(ox, oy, cell * (size - 1), cell * (size - 1)))

        # звёздные точки (хоси) — канонические позиции для трёх размеров
        mid = size // 2
        edge = 2 if size == 9 else 3
        stars = {(edge, edge), (size - 1 - edge, edge),
                 (edge, size - 1 - edge), (size - 1 - edge, size - 1 - edge)}
        if size >= 13:
            stars |= {(edge, mid), (mid, edge), (mid, size - 1 - edge),
                      (size - 1 - edge, mid)}
        stars.add((mid, mid))
        p.setBrush(QBrush(_GRID))
        p.setPen(Qt.PenStyle.NoPen)
        for sx, sy in stars:
            p.drawEllipse(QPointF(ox + sx * cell, oy + sy * cell),
                          cell * 0.09, cell * 0.09)

        # координаты (буквы без I — как в нотации Го; цифры снизу вверх)
        p.setPen(QPen(_COORD))
        p.setFont(QFont("Segoe UI", max(7, int(cell * 0.22))))
        letters = "ABCDEFGHJKLMNOPQRST"
        for i in range(size):
            p.drawText(QRectF(ox + i * cell - cell / 2, oy - cell * 0.8,
                              cell, cell * 0.6), Qt.AlignmentFlag.AlignCenter,
                       letters[i])
            p.drawText(QRectF(ox + i * cell - cell / 2,
                              oy + (size - 1) * cell + cell * 0.25,
                              cell, cell * 0.6),
                       Qt.AlignmentFlag.AlignCenter, letters[i])
            num_top = str(size - i)
            p.drawText(QRectF(ox - cell * 0.9, oy + i * cell - cell * 0.3,
                              cell * 0.7, cell * 0.6),
                       Qt.AlignmentFlag.AlignCenter, num_top)
            p.drawText(QRectF(ox + (size - 1) * cell + cell * 0.2,
                              oy + i * cell - cell * 0.3, cell * 0.7,
                              cell * 0.6),
                       Qt.AlignmentFlag.AlignCenter, num_top)

        # камни
        for y in range(size):
            for x in range(size):
                v = self._engine.board[y * size + x]
                if v == EMPTY:
                    continue
                self._draw_stone(p, ox + x * cell, oy + y * cell,
                                 cell * 0.47, black=(v == BLACK))

        # метка последнего хода (красная точка) — сразу видно ответ соперника
        if self._engine.last_move:
            lx, ly = self._engine.last_move
            p.setPen(QPen(QColor("#e2574c"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(ox + lx * cell, oy + ly * cell),
                          cell * 0.18, cell * 0.18)

        # призрак камня под курсором (только когда ход возможен)
        if self._hover and self.is_my_turn() \
                and self._engine.board[self._engine._idx(*self._hover)] == EMPTY \
                and self._engine.is_legal(self._hover[0], self._hover[1],
                                          self._engine.to_move()):
            hx, hy = self._hover
            ghost = QColor(0, 0, 0, 70) if self._engine.to_move() == BLACK \
                else QColor(255, 255, 255, 110)
            p.setBrush(QBrush(ghost))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(ox + hx * cell, oy + hy * cell),
                          cell * 0.47, cell * 0.47)
        p.end()

    def _draw_stone(self, p: QPainter, cx: float, cy: float,
                    r: float, black: bool) -> None:
        """Камень с радиальным градиентом: объём + мягкая тень-ободок."""
        g = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.6)
        if black:
            g.setColorAt(0.0, QColor("#5c5c66"))
            g.setColorAt(0.35, QColor("#2c2c33"))
            g.setColorAt(1.0, QColor("#0c0c10"))
        else:
            g.setColorAt(0.0, QColor("#ffffff"))
            g.setColorAt(0.5, QColor("#f1f1ea"))
            g.setColorAt(1.0, QColor("#c9c9bd"))
        p.setBrush(QBrush(g))
        p.setPen(QPen(QColor("#00000055") if black else QColor("#00000022"), 1))
        p.drawEllipse(QPointF(cx, cy), r, r)

    # ── мышь ─────────────────────────────────────────────────────────────
    def _vertex_at(self, pos) -> tuple[int, int] | None:
        cell, ox, oy = self._geometry()
        x = round((pos.x() - ox) / cell)
        y = round((pos.y() - oy) / cell)
        if not (0 <= x < self._engine.size and 0 <= y < self._engine.size):
            return None
        # клик должен быть близко к пересечению (не посреди клетки)
        dx = abs(pos.x() - (ox + x * cell)) / cell
        dy = abs(pos.y() - (oy + y * cell)) / cell
        if max(dx, dy) > 0.5:
            return None
        return x, y

    def mouseMoveEvent(self, event) -> None:
        self._hover = self._vertex_at(event.position())
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def mousePressEvent(self, event) -> None:
        v = self._vertex_at(event.position())
        if v is None:
            return
        if self.on_stone_played:
            self.on_stone_played(*v)

    def _emit_info(self) -> None:
        if self.on_info_changed:
            self.on_info_changed()


class GoGameDialog(QDialog):
    """Диалог: доска слева, рейтинг и управление справа (как в шахматах)."""

    def __init__(
        self,
        parent: QWidget | None,
        base_url: str,
        player_name: str,
        access_key: str,
        bot_id: str = "go",
        bot_meta: dict | None = None,
        initial_tab: str = "play",
    ):
        super().__init__(parent)
        self.setWindowTitle("⚫ Го — игра в окружение")
        self.setModal(False)
        self.setStyleSheet(f"background: {_BG};")

        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._bot_meta = bot_meta or {}
        self._initial_tab = initial_tab
        self._net_match_id: str | None = None

        self._bridge = AsyncBridge()
        self._match = GoMatch(
            base_url, player_name, access_key, bot_id=bot_id,
            runner=self._bridge.run,
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # ── слева: доска + инфо-строка ───────────────────────────────────
        left = QVBoxLayout()
        self._board = GoBoardWidget(self)
        self._board.on_stone_played = self._on_stone_clicked
        self._board.on_local_game_over = self._on_local_game_over
        self._board.on_info_changed = self._refresh_info
        left.addWidget(self._board, 1)

        self._info_label = QLabel("")
        self._info_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 12px; background: {_PANEL_BG}; border: 1px solid "
            f"{_PANEL_BORDER}; border-radius: 8px; padding: 8px;")
        self._info_label.setWordWrap(True)
        left.addWidget(self._info_label)
        root.addLayout(left, 1)

        # ── справа: рейтинг + режимы + управление ────────────────────────
        right = QVBoxLayout()
        right.setSpacing(10)

        title = QLabel("🏆 Рейтинг ELO")
        title.setStyleSheet(
            f"color: {_ACCENT}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 16px; font-weight: bold;")
        right.addWidget(title)

        self._leaderboard_label = QLabel("Загрузка…")
        self._leaderboard_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 12px; background: {_PANEL_BG}; border: 1px solid "
            f"{_PANEL_BORDER}; border-radius: 8px; padding: 10px;")
        self._leaderboard_label.setFixedWidth(240)
        self._leaderboard_label.setWordWrap(True)
        self._leaderboard_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.addWidget(self._leaderboard_label, 1)

        self._my_stats_label = QLabel("")
        self._my_stats_label.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px;")
        self._my_stats_label.setWordWrap(True)
        right.addWidget(self._my_stats_label)

        # ─── переключатель режима ────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self._btn_mode_ai = QPushButton("🤖 ИИ")
        self._btn_mode_ai.setCheckable(True)
        self._btn_mode_ai.clicked.connect(
            lambda _c=False: self._set_mode(MODE_AI))
        mode_row.addWidget(self._btn_mode_ai)

        self._btn_mode_pvp = QPushButton("👥 PVP")
        self._btn_mode_pvp.setCheckable(True)
        self._btn_mode_pvp.clicked.connect(
            lambda _c=False: self._set_mode(MODE_PVP))
        mode_row.addWidget(self._btn_mode_pvp)

        self._btn_mode_net = QPushButton("🌐 Сеть")
        self._btn_mode_net.setCheckable(True)
        self._btn_mode_net.clicked.connect(self._on_net_mode_clicked)
        mode_row.addWidget(self._btn_mode_net)
        right.addLayout(mode_row)

        # размер доски (AI/PVP; сетевой матч задаёт хост)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Доска:"))
        self._size_combo = QComboBox()
        for s in (9, 13, 19):
            self._size_combo.addItem(f"{s}×{s}", s)
        self._size_combo.setCurrentIndex(0)
        size_row.addWidget(self._size_combo)
        size_row.addStretch(1)
        right.addLayout(size_row)

        # ─── сетевые действия ────────────────────────────────────────────
        self._net_widget = QWidget()
        net_layout = QVBoxLayout(self._net_widget)
        net_layout.setContentsMargins(0, 0, 0, 0)
        net_layout.setSpacing(4)

        self._btn_net_host = QPushButton("＋ Создать матч")
        self._btn_net_host.clicked.connect(lambda: self._on_net_host())
        net_layout.addWidget(self._btn_net_host)

        self._btn_net_join = QPushButton("↪ Присоединиться")
        self._btn_net_join.clicked.connect(lambda: self._on_net_join())
        net_layout.addWidget(self._btn_net_join)

        self._btn_net_resign = QPushButton("🏳 Сдаться")
        self._btn_net_resign.clicked.connect(lambda: self._on_net_resign())
        net_layout.addWidget(self._btn_net_resign)

        self._btn_net_leave = QPushButton("✕ Выйти из матча")
        self._btn_net_leave.clicked.connect(lambda: self._on_net_leave())
        net_layout.addWidget(self._btn_net_leave)

        self._net_status_label = QLabel("")
        self._net_status_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 10px; background: {_PANEL_BG}; border: 1px solid "
            f"{_PANEL_BORDER}; border-radius: 6px; padding: 6px;")
        self._net_status_label.setWordWrap(True)
        net_layout.addWidget(self._net_status_label)
        right.addWidget(self._net_widget)
        self._net_widget.setVisible(False)

        # ─── кнопки партии ───────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_pass = QPushButton("⏭ Пас")
        btn_pass.clicked.connect(lambda: self._on_pass())
        btn_row.addWidget(btn_pass)

        btn_resign = QPushButton("🏳 Сдаться")
        btn_resign.clicked.connect(lambda: self._on_local_resign())
        btn_row.addWidget(btn_resign)
        right.addLayout(btn_row)

        btn_row2 = QHBoxLayout()
        btn_restart = QPushButton("↻ Новая партия")
        btn_restart.clicked.connect(lambda: self._on_restart())
        btn_row2.addWidget(btn_restart)

        btn_refresh = QPushButton("⟳ Рейтинг")
        btn_refresh.clicked.connect(lambda: self._match.refresh_leaderboard())
        btn_row2.addWidget(btn_refresh)

        btn_close = QPushButton("✕ Закрыть")
        btn_close.clicked.connect(lambda: self.close())
        btn_row2.addWidget(btn_close)
        right.addLayout(btn_row2)

        root.addLayout(right)

        # единый стиль кнопок (как в шахматном диалоге)
        for b in (self._btn_mode_ai, self._btn_mode_pvp, self._btn_mode_net,
                  self._btn_net_host, self._btn_net_join, self._btn_net_resign,
                  self._btn_net_leave, btn_pass, btn_resign, btn_restart,
                  btn_refresh, btn_close):
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {_PANEL_BORDER}; color: {_TEXT_PRIMARY};
                    border: none; border-radius: 6px; padding: 8px 12px;
                    font-family: 'Consolas, Menlo, monospace'; font-weight: bold;
                    min-height: 18px;
                }}
                QPushButton:hover {{ background: #4a4a56; }}
                QPushButton:pressed {{ background: #2a2a32; }}
                QPushButton:checked {{ background: {_ACCENT}; color: #1a1a1f; }}
                QPushButton:disabled {{ background: #2a2a30; color: #5a5a60; }}
            """)

        # ── таймеры ──────────────────────────────────────────────────────
        # опрос сетевого матча
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(NET_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._net_poll)
        # отложенный ход ИИ (чтобы интерфейс перерисовался после игрока)
        self._ai_timer = QTimer(self)
        self._ai_timer.setSingleShot(True)
        self._ai_timer.setInterval(420)
        self._ai_timer.timeout.connect(self._ai_move)

        # подписки ядра сети
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

        # старт: режим ИИ, доска по селектору
        self._set_mode(MODE_AI)
        self._match.refresh_leaderboard()

    def showEvent(self, event):
        """v3: при первом показе — если у нас уже есть состояние матча
        (из !go host/join в чате), применяем его сразу, без повторного
        сетевого запроса. Иначе — обычное поведение."""
        super().showEvent(event)
        if getattr(self, "_initial_match_applied", False):
            return
        self._initial_match_applied = True
        initial_match = None
        if isinstance(self._bot_meta, dict):
            # interpret_dispatch кладёт сам match-словарь в meta (см.
            # chat_session.py:match_created/match_joined).
            if self._bot_meta.get("match_id") or self._bot_meta.get("black"):
                initial_match = self._bot_meta
        if initial_match and hasattr(self._match, "apply_initial_match"):
            try:
                self._set_mode(MODE_NET)
            except Exception:
                pass
            self._match.apply_initial_match(initial_match)

    # ── режимы ───────────────────────────────────────────────────────────
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._net_match_id = None
        self._board.new_game(int(self._size_combo.currentData() or 9), mode)
        self._sync_mode_buttons()
        self._net_widget.setVisible(mode == MODE_NET)
        self._poll_timer.stop()
        if mode == MODE_NET:
            self._poll_timer.start()
        self._match.reset_result_flag()
        self._refresh_info()

    def _sync_mode_buttons(self) -> None:
        self._btn_mode_ai.setChecked(self._mode == MODE_AI)
        self._btn_mode_pvp.setChecked(self._mode == MODE_PVP)
        self._btn_mode_net.setChecked(self._mode == MODE_NET)

    def _on_net_mode_clicked(self) -> None:
        self._set_mode(MODE_NET)

    # ── клик по доске ────────────────────────────────────────────────────
    def _on_stone_clicked(self, x: int, y: int) -> None:
        if self._mode == MODE_NET:
            if not self._board.is_my_turn():
                self._set_status_board("⏳ Сейчас ход соперника.")
                return
            vertex = format_vertex(x, y, self._board.engine.size)
            # локальная проверка ДО отправки — мгновенный фидбек, сервер
            # всё равно пере-валидует (он арбитр)
            if not self._board.engine.is_legal(
                    x, y, self._board.engine.to_move()):
                self._set_status_board(f"❌ {vertex}: ход нарушает правила.")
                return
            self._match.send_move(vertex)
            return
        # AI / PVP: применяем локально
        if not self._board.try_play(x, y):
            self._set_status_board("❌ Здесь нельзя: занято/ко/самоубийство.")
            return
        if self._mode == MODE_AI and not self._board.engine.game_over:
            self._ai_timer.start()   # ИИ ответит с небольшой задержкой

    # ── ИИ ───────────────────────────────────────────────────────────────
    def _ai_move(self) -> None:
        engine = self._board.engine
        if engine.game_over or self._mode != MODE_AI:
            return
        if engine.to_move() != WHITE:
            return
        mv = GoAI.choose_move(engine, WHITE)
        if mv is None:
            engine.pass_turn(WHITE)
            self._set_status_board(f"🤖 {GoAI.NAME}: пас.")
        else:
            engine.play(mv[0], mv[1], WHITE)
            self._set_status_board(
                f"🤖 {GoAI.NAME}: {format_vertex(mv[0], mv[1], engine.size)}.")
        self._board.update()
        self._refresh_info()
        if engine.game_over:
            self._on_local_game_over(engine.area_score())

    # ── пас / сдача / новая партия (локальные режимы) ────────────────────
    def _on_pass(self) -> None:
        if self._mode == MODE_NET:
            if self._board.is_my_turn():
                self._match.send_move("pass")
            else:
                self._set_status_board("⏳ Сейчас не твой ход.")
            return
        self._board.do_pass()
        if self._mode == MODE_AI and not self._board.engine.game_over:
            self._ai_timer.start()

    def _on_local_resign(self) -> None:
        if self._board.engine.game_over:
            return
        if self._mode == MODE_NET:
            self._on_net_resign()
            return
        answer = QMessageBox.question(
            self, "Сдаться?", "Сдать текущую партию?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._mode == MODE_AI:
            # сдача = поражение (честно идёт в ELO)
            self._match.save_ai_result("loss")
        self._set_status_board("🏳 Партия сдана.")

    def _on_restart(self) -> None:
        self._set_mode(self._mode)

    # ── локальный финал (два паса в AI/PVP) ──────────────────────────────
    def _on_local_game_over(self, score: dict) -> None:
        if self._mode == MODE_AI:
            outcome = result_for_player(score, player_is_black=True)
            self._match.save_ai_result(outcome)
        elif self._mode == MODE_PVP:
            pass   # дружеская партия — ELO не трогаем
        self._refresh_info()

    # ── сеть: подписки ядра ──────────────────────────────────────────────
    def _on_match_ready(self, my_black: bool, match_id: str, opponent: str) -> None:
        """Матч создан/присоединён: инициализация доски.

        v3.1.1: кроме доски синхронизируем UI диалога — кнопки режима
        (ИИ/PVP/Сеть), видимость панели сети и poll_timer. Раньше это
        делалось только в ``_set_mode``, который не всегда вызывается
        перед ``_on_match_ready`` (например, при клике по ссылке-приглашению
        до фикса в chat_screen._open_game_with_code). Теперь UI гарантированно
        в NET-режиме, и opponent виден в статусе сразу.
        """
        self._net_match_id = match_id
        self._board.new_game(9, MODE_NET, my_black=my_black)
        self._board.set_my_black(my_black)
        # v3.1.1: гарантированно синхронизируем UI диалога под NET-режим.
        if self._mode != MODE_NET:
            try:
                self._set_mode(MODE_NET)
            except Exception:
                pass
        # Запускаем poll_timer, если ещё не запущен — нужен для приёма
        # ходов соперника.
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def _on_match_state(self, match: dict) -> None:
        if not match:
            return
        my_black = (match.get("black") == self._player_name)
        self._net_match_id = match.get("match_id") or self._net_match_id
        self._board.set_my_black(my_black)
        self._board.apply_match(match)
        if match.get("status") == "finished":
            self._poll_timer.stop()
            # v3.1: матч завершён — сбрасываем _net_match_id, чтобы игрок
            # мог сразу создать новую игру (см. GoBot._cmd_host: finished
            # матчи больше не блокируют host). Доска остаётся с финальной
            # позицией до тех пор, пока игрок не нажмёт «Создать матч»
            # или не закроет окно.
            self._net_match_id = None

    def _on_match_reset(self) -> None:
        self._net_match_id = None
        # v3.1: НЕ перезапускаем poll_timer автоматически — он запустится
        # только при создании/присоединении к новому матчу (см. _on_match_ready).
        self._poll_timer.stop()

    def _on_move_rejected(self, text: str) -> None:
        self._set_status_board(f"❌ {text}")
        # немедленная синхронизация с сервером — локальная позиция могла
        # разойтись (оптимистичная отправка)
        self._net_poll()

    def _on_net_status(self, text: str) -> None:
        self._net_status_label.setText(text)

    def _on_net_error(self, err: str) -> None:
        self._net_status_label.setText(f"⚠ Сеть: {err}")

    def _on_my_stats(self, text: str) -> None:
        self._my_stats_label.setText(text)

    def _on_leaderboard(self, lines: list[str]) -> None:
        self._leaderboard_label.setText(
            "\n".join(lines) if lines else "Рейтинг пуст — сыграй первую партию!")

    def _on_leaderboard_error(self) -> None:
        self._leaderboard_label.setText("Рейтинг недоступен (ошибка сети).")

    def _on_leaderboard_no_connection(self) -> None:
        self._leaderboard_label.setText("Рейтинг: нет подключения к серверу.")

    def _net_poll(self) -> None:
        self._match.poll_state(
            is_net_mode=(self._mode == MODE_NET),
            game_over=self._board.engine.game_over,
            net_color_black=self._board.my_black,
        )

    def _on_net_host(self) -> None:
        self._match.host(int(self._size_combo.currentData() or 9))

    def _on_net_join(self) -> None:
        code, ok = QInputDialog.getText(
            self, "Присоединиться к матчу",
            "Код матча (4 символа):")
        if ok:
            self._match.join(code)

    def _on_net_resign(self) -> None:
        if not self._net_match_id:
            self._net_status_label.setText("Нет активного матча.")
            return
        answer = QMessageBox.question(
            self, "Сдаться?", "Сдать сетевую партию? Соперник получит победу.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._match.resign()

    def _on_net_leave(self) -> None:
        self._match.leave()

    # ── инфо-строка ──────────────────────────────────────────────────────
    def _set_status_board(self, text: str) -> None:
        self._info_suffix = text
        self._refresh_info()

    def _refresh_info(self) -> None:
        e = self._board.engine
        side = "чёрные ⚫" if e.to_move() == BLACK else "белые ⚪"
        if e.game_over:
            sc = e.area_score()
            base = (f"🏁 Партия завершена. Счёт: чёрные {sc['black']} : "
                    f"белые {sc['white']} (+коми {sc['komi']:g}) — "
                    f"победа {sc['winner']}.")
        else:
            ko = f" | ко-запрет: {format_vertex(*e.ko_point, e.size)}" \
                if e.ko_point else ""
            base = (f"Ход: {side} | взято чёрными: {e.captured[BLACK]}, "
                    f"белыми: {e.captured[WHITE]}{ko}")
        suffix = getattr(self, "_info_suffix", "")
        if suffix and not e.game_over:
            base += f"\n{suffix}"
        elif e.game_over:
            self._info_suffix = ""
        self._info_label.setText(base)

    # ── закрытие ─────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        self._ai_timer.stop()
        self._poll_timer.stop()
        if should_leave_on_close(self._mode, self._net_match_id,
                                 self._board.engine.game_over):
            self._match.leave_on_close()
        super().closeEvent(event)
