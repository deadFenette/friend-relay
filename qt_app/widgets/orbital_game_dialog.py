"""OrbitalGameDialog — окно игры «Orbital Defense» в стиле проекта.

Открывается из чата командой !orbital (или текущим триггером).
Структура как у SnakeGameDialog:
  - слева: игровое поле (OrbitalGameWidget из qt_app/widgets/games/orbital_canvas.py)
  - справа: турнирная таблица + управление
  - стиль — Spatial Glass (тема проекта), не чёрный raw-фон

Счёт сохраняется на сервере по сигналу score_submitted из canvas.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
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
from qt_app.theme import PALETTE, RADIUS, RADIUS_SM
from qt_app.widgets.games.orbital_canvas import OrbitalGameWidget

# ── Цвета в стиле Spatial Glass (тёмный космос, акцент periwinkle) ──

_BG = QColor("#08081a")             # очень тёмный синий для аркады
_PANEL_BG = QColor(PALETTE.surface_1)
_PANEL_BORDER = QColor(PALETTE.border_soft)
_ACCENT = QColor(PALETTE.accent)
_TEXT_PRIMARY = QColor(PALETTE.text_primary)
_TEXT_DIM = QColor(PALETTE.text_secondary)
_TEXT_ACCENT = QColor("#66ccff")    # голубой — космический акцент


class OrbitalGameDialog(QDialog):
    """Диалог-обёртка: игровое поле + сайдбар с таблицей."""

    def __init__(self, parent: QWidget | None, base_url: str, player_name: str,
                 access_key: str, bot_id: str = "orbital",
                 bot_meta: dict | None = None,
                 initial_tab: str = "play"):
        super().__init__(parent)
        self.setWindowTitle("🛰️ Orbital Defense")
        self.setModal(False)
        self.setMinimumSize(1000, 680)
        self.setStyleSheet(f"background: {_BG.name()};")

        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._bot_meta = bot_meta or {}
        self._initial_tab = initial_tab

        self._bridge = AsyncBridge()
        self._top_scores: list[dict] = []
        self._score_saved = False

        # ── Layout ──
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # ── Слева: игровое поле + кнопки ──
        left = QVBoxLayout()
        left.setSpacing(10)

        title = QLabel("🛰️  ORBITAL DEFENSE")
        title.setStyleSheet(
            f"color: {_TEXT_ACCENT.name()}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 22px; font-weight: bold; letter-spacing: 4px;"
        )
        left.addWidget(title)

        self._board = OrbitalGameWidget()
        # Score сохраняется через _check_game_over timer, который опрашивает
        # is_game_over() каждые 200мс. Сигнал score_submitted не используем
        # напрямую, чтобы не было двойного сохранения.
        self._board.setMinimumHeight(500)
        left.addWidget(self._board, 1)

        # Кнопки управления
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        def _mk_btn(text: str, primary: bool = False) -> QPushButton:
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(34)
            bg = _ACCENT.name() if primary else PALETTE.surface_3
            fg = "#0a0a0f" if primary else _TEXT_PRIMARY.name()
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {bg}; color: {fg};
                    border: none; border-radius: {RADIUS}px;
                    padding: 0 16px;
                    font-family: 'Consolas, Menlo, monospace';
                    font-weight: bold; letter-spacing: 1px;
                }}
                QPushButton:hover {{ background: {PALETTE.accent_hover if primary else PALETTE.surface_4}; }}
            """)
            return b

        self._btn_restart = _mk_btn("↻ RESTART", primary=True)
        self._btn_restart.clicked.connect(self._on_restart)
        btn_row.addWidget(self._btn_restart)

        self._btn_pause = _mk_btn("⏸ PAUSE")
        self._btn_pause.clicked.connect(self._board.pause_toggle)
        btn_row.addWidget(self._btn_pause)

        self._btn_close = _mk_btn("✕ CLOSE")
        self._btn_close.clicked.connect(self.close)
        btn_row.addWidget(self._btn_close)

        btn_row.addStretch(1)
        left.addLayout(btn_row)

        hint = QLabel("← → орбита  ·  ↑ ↓ радиус  ·  Space/ЛКМ выстрел  ·  Shift замедление  ·  P пауза  ·  R рестарт  ·  Esc выход")
        hint.setStyleSheet(
            f"color: {_TEXT_DIM.name()}; font-family: 'Consolas, Menlo, monospace'; font-size: 11px;"
        )
        left.addWidget(hint)

        root.addLayout(left, 3)

        # ── Справа: турнирная таблица ──
        right = QVBoxLayout()
        right.setSpacing(10)

        lb_title = QLabel("🏆 HALL OF FAME")
        lb_title.setStyleSheet(
            f"color: {_TEXT_ACCENT.name()}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 16px; font-weight: bold; letter-spacing: 3px;"
        )
        right.addWidget(lb_title)

        self._leaderboard_label = QLabel("Загрузка…")
        self._leaderboard_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._leaderboard_label.setTextFormat(Qt.TextFormat.RichText)
        self._leaderboard_label.setStyleSheet(f"""
            QLabel {{
                background: {_PANEL_BG.name()};
                border: 1px solid {_PANEL_BORDER.name()};
                border-radius: {RADIUS}px;
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
                background: {PALETTE.surface_3}; color: {_TEXT_PRIMARY.name()};
                border: none; border-radius: {RADIUS_SM}px; padding: 6px 12px;
                font-family: 'Consolas, Menlo, monospace'; font-size: 12px;
            }}
            QPushButton:hover {{ background: {PALETTE.surface_4}; }}
        """)
        refresh_btn.clicked.connect(self._refresh_leaderboard)
        right.addWidget(refresh_btn)

        right.addStretch(1)

        right_host = QWidget()
        right_host.setLayout(right)
        right_host.setFixedWidth(320)
        root.addWidget(right_host)

        # Таймер опроса game_over (если игрок закрыл окно во время игры —
        # счёт не сохраняем, это forfeit)
        self._check_game_over_timer = QTimer(self)
        self._check_game_over_timer.timeout.connect(self._check_game_over)
        self._check_game_over_timer.start(200)

    # ── Жизненный цикл ───────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._board.setFocus()
        self._refresh_leaderboard()
        QTimer.singleShot(150, self._board.start_game)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        # Остальные клавиши уходят в виджет игры
        self._board.keyPressEvent(event)

    def closeEvent(self, event) -> None:
        try:
            self._board.stop_game()
            self._check_game_over_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)

    # ── Логика ────────────────────────────────────────────────────────

    def _on_restart(self) -> None:
        self._score_saved = False
        self._board.start_game()

    def _check_game_over(self) -> None:
        if not self._board.is_game_over():
            return
        if self._score_saved:
            return
        if self._board.score() == 0:
            self._score_saved = True
            return
        self._score_saved = True
        self._save_score(self._board.score(), self._board.wave())

    def _save_score(self, score: int, wave: int) -> None:
        if not self._base_url or not self._player_name:
            return

        def _do() -> dict:
            return client.send_bot_command_full(
                self._base_url, self._bot_id,
                command="savescore", args=[str(score), str(wave)],
                name=self._player_name, access_key=self._access_key,
            )

        self._bridge.run(_do, on_success=self._on_score_saved, on_error=lambda e: None)

    def _on_score_saved(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") == "score_saved":
            rank = action.get("rank")
            best = action.get("best")
            if rank is not None:
                self._my_best_label.setText(
                    f"Твой лучший: {best if best is not None else '—'}   ·   место: #{rank}"
                )
        self._refresh_leaderboard()

    # ── Турнирная таблица ─────────────────────────────────────────────

    def _refresh_leaderboard(self) -> None:
        if not self._base_url or not self._player_name:
            self._leaderboard_label.setText("<i>Нет подключения к серверу</i>")
            return

        def _do() -> dict:
            return client.send_bot_command_full(
                self._base_url, self._bot_id,
                command="getscores", args=[],
                name=self._player_name, access_key=self._access_key,
            )

        self._bridge.run(_do, on_success=self._on_scores_loaded, on_error=self._on_scores_error)

    def _on_scores_loaded(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        scores = action.get("scores") if isinstance(action, dict) else None
        if not scores:
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
        lines = ["<table cellspacing='0' cellpadding='3' "
                 "style='font-family: Consolas, Menlo, monospace;'>"]
        for i, s in enumerate(scores[:10]):
            name = (s.get("name") or "???")[:16]
            sc = s.get("score", 0)
            wv = s.get("wave", 1)
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
            row_color = "rgba(102,204,255,0.08)" if is_me else "transparent"
            bold = "font-weight: bold;" if is_me else ""
            lines.append(
                f"<tr style='background:{row_color};'>"
                f"<td style='color:{color};{bold} padding-right:8px;'>{medal}</td>"
                f"<td style='color:{color};{bold} padding-right:10px;'>{name}</td>"
                f"<td style='color:{color};{bold} text-align:right;'>{sc}</td>"
                f"<td style='color:{_TEXT_DIM.name()}; font-size:11px; padding-left:8px;'>W{wv}</td>"
                f"</tr>"
            )
        lines.append("</table>")
        self._leaderboard_label.setText("".join(lines))

    def _update_my_best(self, scores: list[dict]) -> None:
        my_scores = [s["score"] for s in scores if s.get("name") == self._player_name]
        best = max(my_scores) if my_scores else 0
        self._my_best_label.setText(f"Твой лучший: {best if best else '—'}")
