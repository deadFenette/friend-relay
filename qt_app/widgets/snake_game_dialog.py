"""
SnakeGameDialog — полноценная аркадная змейка поверх чата.

Открывается по команде !snake (или текущему триггеру) из чата.
Особенности:
  - Реалтайм-loop через QTimer (130 мс по умолчанию).
  - Управление стрелками или WASD.
  - Пробел — пауза, R — рестарт, Esc — закрыть.
  - Турнирная таблица ТОЛЬКО ВНУТРИ окна игры (как в старых аркадных автоматах):
    правая панель с топ-10, обновляется при открытии и после game over.
  - Ретро-CRT вайб: тёмный фон, неоновая змейка (#39FF14), scanlines overlay,
    моноширинный шрифт для очков и таблицы.

Счёт сохраняется на сервере только при game over.
"""

from __future__ import annotations

import random
import time
from collections import deque

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib import client
from qt_app.async_bridge import AsyncBridge

# ----- визуальные константы (ретро-CRT) -----------------------------------

_GRID_SIZE = 25  # клеток по горизонтали и вертикали
_CELL_PX = 22  # пикселей на клетку
_BOARD_PX = _GRID_SIZE * _CELL_PX  # 550

_BG_COLOR = QColor("#0a0a0f")
_GRID_COLOR = QColor(255, 255, 255, 10)  # тонкая сетка
_GRID_BORDER = QColor("#2a2a3a")

_SNAKE_HEAD = QColor("#39FF14")  # neon green
_SNAKE_BODY_BASE = QColor("#2BD614")  # зеленый хвост
_FOOD_COLOR = QColor("#FF1744")  # neon pink
_FOOD_GLOW = QColor(255, 23, 68, 90)

_TEXT_PRIMARY = QColor("#F0F0F5")
_TEXT_DIM = QColor("#8E8E99")
_TEXT_ACCENT = QColor("#39FF14")
_TEXT_DANGER = QColor("#FB7185")
_PANEL_BG = QColor("#121218")
_PANEL_BORDER = QColor("#272730")
_SCANLINE_ALPHA = 28  # 0-255; ~11% — лёгкий CRT-шейдер


# ----- направления --------------------------------------------------------

_UP = (0, -1)
_DOWN = (0, 1)
_LEFT = (-1, 0)
_RIGHT = (1, 0)


class _BoardCanvas(QWidget):
    """Игровое поле: QPainter canvas с реалтайм-обновлением."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(_BOARD_PX, _BOARD_PX)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._snake: deque[tuple[int, int]] = deque()
        self._direction: tuple[int, int] = _RIGHT
        self._next_direction: tuple[int, int] = _RIGHT  # принимается на следующем тике
        self._food: tuple[int, int] = (0, 0)
        self._score = 0
        self._status = "idle"  # idle | playing | paused | gameover
        self._tick_ms = 130
        self._last_eat_ts = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------- управление состоянием ---------------------------

    def start_game(self) -> None:
        self._snake = deque([(12, 12), (11, 12), (10, 12)])
        self._direction = _RIGHT
        self._next_direction = _RIGHT
        self._score = 0
        self._status = "playing"
        self._spawn_food()
        self._timer.start(self._tick_ms)
        self.update()

    def pause(self) -> None:
        if self._status == "playing":
            self._status = "paused"
            self._timer.stop()
            self.update()
        elif self._status == "paused":
            self._status = "playing"
            self._timer.start(self._tick_ms)
            self.update()

    def restart(self) -> None:
        self._timer.stop()
        self.start_game()

    def is_game_over(self) -> bool:
        return self._status == "gameover"

    def is_idle(self) -> bool:
        return self._status == "idle"

    def score(self) -> int:
        return self._score

    def snake_length(self) -> int:
        return len(self._snake)

    def status(self) -> str:
        return self._status

    # ------------------- ввод -------------------------------------------

    def key_pressed(self, key: int) -> bool:
        """Возвращает True если клавиша обработана."""
        d = None
        if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
            d = _UP
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
            d = _DOWN
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            d = _LEFT
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            d = _RIGHT
        elif key == Qt.Key.Key_Space:
            self.pause()
            return True
        elif key == Qt.Key.Key_R:
            self.restart()
            return True

        if d is None:
            return False

        # Нельзя развернуться на 180°
        if d[0] == -self._direction[0] and d[1] == -self._direction[1]:
            return True  # съели клавишу, но проигнорировали
        self._next_direction = d
        return True

    # ------------------- игровой цикл ----------------------------------

    def _spawn_food(self) -> None:
        occupied = set(self._snake)
        if len(occupied) >= _GRID_SIZE * _GRID_SIZE:
            return
        while True:
            x = random.randint(0, _GRID_SIZE - 1)
            y = random.randint(0, _GRID_SIZE - 1)
            if (x, y) not in occupied:
                self._food = (x, y)
                return

    def _tick(self) -> None:
        if self._status != "playing":
            return
        self._direction = self._next_direction
        head_x, head_y = self._snake[0]
        dx, dy = self._direction
        new_head = (head_x + dx, head_y + dy)

        # столкновение со стеной
        if not (0 <= new_head[0] < _GRID_SIZE and 0 <= new_head[1] < _GRID_SIZE):
            self._game_over()
            return

        # столкновение с собой (хвост сдвинется, поэтому проверяем без последнего сегмента,
        # только если не едим еду)
        body = list(self._snake)
        will_eat = new_head == self._food
        if not will_eat and body:
            body = body[:-1]
        if new_head in body:
            self._game_over()
            return

        self._snake.appendleft(new_head)
        if will_eat:
            self._score += 10
            self._last_eat_ts = time.time()
            self._spawn_food()
        else:
            self._snake.pop()

        self.update()

    def _game_over(self) -> None:
        self._status = "gameover"
        self._timer.stop()
        self.update()

    # ------------------- отрисовка --------------------------------------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # фон
        p.fillRect(self.rect(), _BG_COLOR)

        # сетка
        p.setPen(QPen(_GRID_COLOR, 1))
        for i in range(_GRID_SIZE + 1):
            x = i * _CELL_PX
            p.drawLine(x, 0, x, _BOARD_PX)
            p.drawLine(0, x, _BOARD_PX, x)

        # рамка
        p.setPen(QPen(_GRID_BORDER, 2))
        p.drawRect(0, 0, _BOARD_PX - 1, _BOARD_PX - 1)

        # еда с glow
        fx, fy = self._food
        food_rect = QRectF(fx * _CELL_PX + 2, fy * _CELL_PX + 2, _CELL_PX - 4, _CELL_PX - 4)
        p.setBrush(QBrush(_FOOD_GLOW))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(food_rect.adjusted(-3, -3, 3, 3))
        p.setBrush(QBrush(_FOOD_COLOR))
        p.drawEllipse(food_rect)

        # змейка: голова ярче, хвост темнее
        snake_list = list(self._snake)
        n = max(1, len(snake_list))
        for i, (sx, sy) in enumerate(snake_list):
            t = i / max(1, n - 1)  # 0 = голова, 1 = хвост
            # лерп от head к body
            r = int(_SNAKE_HEAD.red() * (1 - t) + _SNAKE_BODY_BASE.red() * t)
            g = int(_SNAKE_HEAD.green() * (1 - t) + _SNAKE_BODY_BASE.green() * t)
            b = int(_SNAKE_HEAD.blue() * (1 - t) + _SNAKE_BODY_BASE.blue() * t)
            color = QColor(r, g, b)
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(
                sx * _CELL_PX + 1,
                sy * _CELL_PX + 1,
                _CELL_PX - 2,
                _CELL_PX - 2,
                3,
                3,
            )

        # CRT scanlines overlay
        p.setPen(QPen(QColor(0, 0, 0, _SCANLINE_ALPHA), 1))
        for y in range(0, _BOARD_PX, 2):
            p.drawLine(0, y, _BOARD_PX, y)

        # статус-оверлей
        if self._status in ("idle", "paused", "gameover"):
            self._draw_status_overlay(p)

    def _draw_status_overlay(self, p: QPainter) -> None:
        # полупрозрачная заливка
        p.fillRect(self.rect(), QColor(0, 0, 0, 160))

        font = QFont("Consolas, Menlo, Monaco, monospace")
        font.setPixelSize(28)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(_TEXT_ACCENT))

        if self._status == "idle":
            text = "PRESS  R  TO START"
        elif self._status == "paused":
            text = "PAUSED"
            p.setPen(QPen(_TEXT_PRIMARY))
        elif self._status == "gameover":
            text = "GAME OVER"
            p.setPen(QPen(_TEXT_DANGER))
        else:
            text = ""

        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)

        if self._status == "gameover":
            font2 = QFont("Consolas, Menlo, Monaco, monospace")
            font2.setPixelSize(16)
            p.setFont(font2)
            p.setPen(QPen(_TEXT_PRIMARY))
            sub = f"Score: {self._score}    Length: {len(self._snake)}"
            r2 = QRectF(0, self.rect().center().y() + 20, self.width(), 30)
            p.drawText(r2, Qt.AlignmentFlag.AlignCenter, sub)

            font3 = QFont("Consolas, Menlo, Monaco, monospace")
            font3.setPixelSize(13)
            p.setFont(font3)
            p.setPen(QPen(_TEXT_DIM))
            r3 = QRectF(0, self.rect().center().y() + 50, self.width(), 24)
            p.drawText(r3, Qt.AlignmentFlag.AlignCenter, "R — restart   ·   Esc — close")


# ----------------------------------------------------------------------


class SnakeGameDialog(QDialog):
    """Диалог-обёртка: игровое поле слева, турнирная таблица справа."""

    def __init__(
        self,
        parent: QWidget | None,
        base_url: str,
        player_name: str,
        access_key: str,
        bot_id: str = "snake",
        bot_meta: dict | None = None,
        initial_tab: str = "play",
    ):
        super().__init__(parent)
        self.setWindowTitle("🐍 Змейка — аркадный автомат")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {_BG_COLOR.name()};")

        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._bot_meta = bot_meta or {}
        self._initial_tab = initial_tab

        self._bridge = AsyncBridge()
        self._top_scores: list[dict] = []
        self._score_saved = False  # чтобы не сохранить дважды при одном game over

        # ----- layout -----
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # -- левая колонка: поле + инфо --
        left = QVBoxLayout()
        left.setSpacing(10)

        title = QLabel("🐍 SNAKE")
        title.setStyleSheet(
            f"color: {_TEXT_ACCENT.name()}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 22px; font-weight: bold; letter-spacing: 4px;"
        )
        left.addWidget(title)

        self._board = _BoardCanvas()
        self._board._tick_ms = self._bot_meta.get("tick_ms", 130)
        left.addWidget(self._board)

        # строка счёта
        self._score_label = QLabel("SCORE: 0    LENGTH: 3")
        self._score_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY.name()}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 14px; font-weight: bold; letter-spacing: 2px;"
        )
        left.addWidget(self._score_label)

        # кнопки
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        def _mk_btn(text: str, primary: bool = False) -> QPushButton:
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(32)
            bg = _SNAKE_HEAD.name() if primary else _PANEL_BORDER.name()
            fg = "#0a0a0f" if primary else _TEXT_PRIMARY.name()
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {bg}; color: {fg};
                    border: none; border-radius: 6px;
                    padding: 0 16px;
                    font-family: 'Consolas, Menlo, monospace';
                    font-weight: bold; letter-spacing: 1px;
                }}
                QPushButton:hover {{ background: {_SNAKE_BODY_BASE.name() if primary else "#2f2f3a"}; }}
            """)
            return b

        self._btn_restart = _mk_btn("↻ RESTART", primary=True)
        self._btn_restart.clicked.connect(self._on_restart)
        btn_row.addWidget(self._btn_restart)

        self._btn_pause = _mk_btn("⏸ PAUSE")
        self._btn_pause.clicked.connect(lambda: self._board.pause())
        btn_row.addWidget(self._btn_pause)

        self._btn_close = _mk_btn("✕ CLOSE")
        self._btn_close.clicked.connect(self.close)
        btn_row.addWidget(self._btn_close)

        btn_row.addStretch(1)
        left.addLayout(btn_row)

        # подсказка управления
        hint = QLabel("←↑↓→ / WASD — поворот  ·  SPACE — пауза  ·  R — рестарт  ·  ESC — выход")
        hint.setStyleSheet(
            f"color: {_TEXT_DIM.name()}; font-family: 'Consolas, Menlo, monospace';font-size: 11px;"
        )
        left.addWidget(hint)

        left.addStretch(1)
        root.addLayout(left)

        # -- правая колонка: турнирная таблица --
        right = QVBoxLayout()
        right.setSpacing(10)

        lb_title = QLabel("🏆 HALL OF FAME")
        lb_title.setStyleSheet(
            f"color: {_TEXT_ACCENT.name()}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 16px; font-weight: bold; letter-spacing: 3px;"
        )
        right.addWidget(lb_title)

        self._leaderboard_label = QLabel("Загрузка…")
        self._leaderboard_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._leaderboard_label.setTextFormat(Qt.TextFormat.RichText)
        self._leaderboard_label.setStyleSheet(f"""
            QLabel {{
                background: {_PANEL_BG.name()};
                border: 1px solid {_PANEL_BORDER.name()};
                border-radius: 8px;
                padding: 12px;
                color: {_TEXT_PRIMARY.name()};
                font-family: 'Consolas, Menlo, monospace';
                font-size: 13px;
            }}
        """)
        self._leaderboard_label.setMinimumWidth(260)
        self._leaderboard_label.setMaximumWidth(300)
        right.addWidget(self._leaderboard_label)

        self._my_best_label = QLabel("Твой лучший: —")
        self._my_best_label.setStyleSheet(
            f"color: {_TEXT_DIM.name()}; font-family: 'Consolas, Menlo, monospace'; font-size: 12px;"
        )
        right.addWidget(self._my_best_label)

        refresh_btn = QPushButton("🔄 Обновить")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_PANEL_BORDER.name()}; color: {_TEXT_PRIMARY.name()};
                border: none; border-radius: 6px; padding: 6px 12px;
                font-family: 'Consolas, Menlo, monospace'; font-size: 12px;
            }}
            QPushButton:hover {{ background: #2f2f3a; }}
        """)
        refresh_btn.clicked.connect(self._refresh_leaderboard)
        right.addWidget(refresh_btn)

        right.addStretch(1)

        right_host = QWidget()
        right_host.setLayout(right)
        right_host.setFixedWidth(320)
        root.addWidget(right_host)

        # таймер обновления UI (счёт/длина)
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._update_score_label)
        self._ui_timer.start(100)

        # при game over нужно сохранить счёт — опрашиваем состояние каждые 200 мс
        self._check_game_over_timer = QTimer(self)
        self._check_game_over_timer.timeout.connect(self._check_game_over)
        self._check_game_over_timer.start(200)

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._board.setFocus()
        self._refresh_leaderboard()
        if self._initial_tab == "play":
            # сразу стартуем
            QTimer.singleShot(150, self._board.start_game)
        else:
            # пользователь пришёл по !snake top — открываем на паузе,
            # чтобы он сначала глянул таблицу
            self._board.start_game()
            QTimer.singleShot(50, self._board.pause)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        if self._board.key_pressed(key):
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        # Если была активная игра (не game over) — она считается «forfeit»,
        # счёт не сохраняется. Если уже game over — сохранён ранее.
        try:
            self._board._timer.stop()
            self._ui_timer.stop()
            self._check_game_over_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Логика
    # ------------------------------------------------------------------

    def _on_restart(self) -> None:
        self._score_saved = False
        self._board.restart()

    def _update_score_label(self) -> None:
        s = self._board.score()
        l = self._board.snake_length()
        self._score_label.setText(f"SCORE: {s}    LENGTH: {l}")

    def _check_game_over(self) -> None:
        if not self._board.is_game_over():
            return
        if self._score_saved:
            return
        # Сохраняем только если был игровой процесс и счёт > 0
        if self._board.snake_length() <= 3 and self._board.score() == 0:
            # died very fast — не сохраняем нулевой
            self._score_saved = True
            return
        self._score_saved = True
        self._save_score(self._board.score(), self._board.snake_length())

    def _save_score(self, score: int, length: int) -> None:
        """Отправляет счёт на сервер через /bot_command с подкомандой savescore."""
        if not self._base_url or not self._player_name:
            return

        def _do() -> dict:
            return client.send_bot_command_full(
                self._base_url,
                self._bot_id,
                command="savescore",
                args=[str(score), str(length)],
                name=self._player_name,
                access_key=self._access_key,
            )

        self._bridge.run(_do, on_success=self._on_score_saved, on_error=lambda e: None)

    def _on_score_saved(self, response: dict) -> None:
        # response: {"ok": True, "bot_response": str, "bot_action": dict | None, "seq": int | None}
        action = response.get("bot_action") or {}
        if action.get("action") == "score_saved":
            rank = action.get("rank")
            best = action.get("best")
            if rank is not None:
                self._my_best_label.setText(
                    f"Твой лучший: {best if best is not None else '—'}   ·   место: #{rank}"
                )
        # В любом случае обновим таблицу
        self._refresh_leaderboard()

    # ------------------------------------------------------------------
    # Турнирная таблица
    # ------------------------------------------------------------------

    def _refresh_leaderboard(self) -> None:
        if not self._base_url or not self._player_name:
            self._leaderboard_label.setText("<i>Нет подключения к серверу</i>")
            return

        def _do() -> dict:
            return client.send_bot_command_full(
                self._base_url,
                self._bot_id,
                command="getscores",
                args=[],
                name=self._player_name,
                access_key=self._access_key,
            )

        self._bridge.run(_do, on_success=self._on_scores_loaded, on_error=self._on_scores_error)

    def _on_scores_loaded(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        scores = action.get("scores") if isinstance(action, dict) else None
        if not scores:
            # может быть в bot_response текстом
            self._leaderboard_label.setText("<i>Пока нет рекордов.<br>Стань первым!</i>")
            self._update_my_best([])
            return

        self._top_scores = scores
        self._render_leaderboard(scores)
        self._update_my_best(scores)

    def _on_scores_error(self, error: Exception) -> None:
        self._leaderboard_label.setText(f"<i style='color:#FB7185'>Ошибка: {error}</i>")

    def _render_leaderboard(self, scores: list[dict]) -> None:
        if not scores:
            self._leaderboard_label.setText("<i>Пока нет рекордов.<br>Стань первым!</i>")
            return

        lines = []
        lines.append(
            "<table cellspacing='0' cellpadding='3' style='font-family: Consolas, Menlo, monospace;'>"
        )
        for i, s in enumerate(scores[:10]):
            name = (s.get("name") or "???")[:16]
            sc = s.get("score", 0)
            ln = s.get("length", 0)
            medal = ""
            color = _TEXT_PRIMARY.name()
            if i == 0:
                medal, color = "🥇", "#FFD700"
            elif i == 1:
                medal, color = "🥈", "#C0C0C0"
            elif i == 2:
                medal, color = "🥉", "#CD7F32"
            else:
                medal = f"<span style='color:{_TEXT_DIM.name()}'>#{i + 1}</span>"
            is_me = name == self._player_name
            row_color = "rgba(57,255,20,0.08)" if is_me else "transparent"
            bold = "font-weight: bold;" if is_me else ""
            lines.append(
                f"<tr style='background:{row_color};'>"
                f"<td style='color:{color};{bold} padding-right:8px;'>{medal}</td>"
                f"<td style='color:{color};{bold} padding-right:10px;'>{name}</td>"
                f"<td style='color:{color};{bold} text-align:right;'>{sc}</td>"
                f"<td style='color:{_TEXT_DIM.name()}; font-size:11px; padding-left:8px;'>L{ln}</td>"
                f"</tr>"
            )
        lines.append("</table>")
        self._leaderboard_label.setText("".join(lines))

    def _update_my_best(self, scores: list[dict]) -> None:
        my_scores = [s["score"] for s in scores if s.get("name") == self._player_name]
        best = max(my_scores) if my_scores else 0
        self._my_best_label.setText(f"Твой лучший: {best if best else '—'}")
