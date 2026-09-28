"""ChessBoardWidget — интерактивная шахматная доска (Qt-виджет).

Декомпозиция бывшего монолита (1136 строк, 7 ответственностей — см.
архитектурный анализ): класс разбит на миксины по ролям, состояние
по-прежнему общее и живёт в экземпляре ChessBoardWidget, поэтому
публичный API не изменился — ChessGame, ChessGameDialog и тесты
работают без правок.

  - _ChessInputMixin  — ввод: маппинг координат, мышь, клавиатура
  - _ChessNetMixin    — онлайн-PVP: синхронизация с сервером, UCI
  - _ChessRenderMixin — отрисовка доски (фигуры через ChessPieceRenderer)
  - ChessBoardWidget  — ядро: состояние, режимы AI/PVP/NET, поток ИИ,
                        анимация, фиксация ходов, диалог превращения

Движок и ИИ живут в lib/chess_engine.py (без Qt) — их использует и
серверный бот lib/chess_bot.py, что устранило layer leak «сервер → UI».
"""
from __future__ import annotations

import random
import threading

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib.games.chess_engine import ChessAI, ChessEngine
from qt_app.widgets.chess_piece_renderer import (
    PIECE_SKIN_DEFAULT,
    PIECE_SKINS,
    PIECE_UNICODE,
    ChessPieceRenderer,
)

# ═══════════════════════════════════════════════════════════════════
# МИКСИН: ВВОД (мышь + клавиатура + маппинг координат)
# ═══════════════════════════════════════════════════════════════════

class _ChessInputMixin:
    """Обработка пользовательского ввода. Состояние берётся из self
    (создаётся в ChessBoardWidget.__init__)."""

    def square_at(self, pos: QPoint) -> int:
        """Экранная точка → индекс клетки движка (учитывает переворот)."""
        size = self.width() // 8
        col = pos.x() // size
        row = pos.y() // size
        if self.flipped:
            row = 7 - row
            col = 7 - col
        return self.engine.idx(row, col)

    def pos_of(self, sq: int) -> QPoint:
        """Индекс клетки → левый верхний угол на экране (с учётом переворота)."""
        row, col = sq // 8, sq % 8
        if self.flipped:
            row = 7 - row
            col = 7 - col
        size = self.width() // 8
        return QPoint(col * size, row * size)

    def mousePressEvent(self, event: QMouseEvent):
        # При клике по доске — забираем фокус, чтобы keyPressEvent
        # (F/R/P/1/2/3) продолжал работать после клика.
        self.setFocus()
        if self.game_over or self.ai_thinking:
            return
        # В режиме ИИ игрок может управлять только своим цветом (player_side).
        # В режиме PVP доступны оба цвета.
        # В режиме NET игрок управляет только своим цветом (net_color).
        if self.mode == self.MODE_AI and self.engine.white_to_move != self.player_side:
            return
        if self.mode == self.MODE_NET:
            # Нет активного матча или не наш ход — игнорируем клики.
            if not self.net_match_id:
                return
            if self.engine.white_to_move != self.net_color:
                return
        sq = self.square_at(event.pos())
        piece = self.engine.board[sq]

        # Если ждём выбора превращения
        if self._promo_pending:
            return

        # Если клик по легальному ходу
        if sq in self.legal_targets and self.selected != -1:
            self._try_move(self.selected, sq)
            return

        # Выбор своей фигуры
        if piece != '.' and self.engine.is_white(piece) == self.engine.white_to_move:
            self.selected = sq
            self.legal_targets = set()
            moves = self.engine.generate_moves()
            for fr, to, _promo in moves:
                if fr == sq:
                    self.legal_targets.add(to)
            self.update()
        else:
            self.selected = -1
            self.legal_targets = set()
            self.update()

    def _try_move(self, fr: int, to: int):
        moves = [m for m in self.engine.generate_moves() if m[0] == fr and m[1] == to]
        if not moves:
            return
        # Превращение пешки: если есть несколько вариантов (Q/R/B/N) —
        # покажем диалог выбора. Раньше молча превращалось в ферзя, что
        # особенно досадно в PVP-партиях.
        promos = [m for m in moves if m[2]]
        if promos and len({m[2].upper() for m in promos}) > 1:
            self._promo_pending = (fr, to)
            self._show_promo_dialog(promos)
            return
        move = promos[0] if promos else moves[0]
        self._commit_move(fr, to, move)

    def keyPressEvent(self, event: QKeyEvent):
        # Игнорируем авто-повторы (KeyPress приходит и при удержании клавиши).
        if event.isAutoRepeat():
            return
        # Esc закрывает диалог — как в snake/orbital/voxel.
        if event.key() == Qt.Key_Escape:
            self.window().close()
            return
        if event.key() == Qt.Key_F:
            self.flipped = not self.flipped
            self.update()
        elif event.key() == Qt.Key_R:
            # v1.9.5: в NET-матче R сбрасывал локальную доску, но
            # net_applied_moves/net_match_id не сбрасывал — следующий ход
            # уходил невалидным UCI, доска рассинхронизировалась с сервером.
            if self.mode == self.MODE_NET:
                return
            self._reset_game()
        elif event.key() == Qt.Key_P:
            # v1.9.5: P молча выводил из NET-матча (доска с сервером ещё
            # играет, клиент уже «вышел») — запрещаем и его.
            if self.mode == self.MODE_NET:
                return
            # Переключение режима ИИ ↔ PVP с клавиатуры (для удобства).
            self.mode = self.MODE_PVP if self.mode == self.MODE_AI else self.MODE_AI
            self._reset_game()
            if self.mode == self.MODE_PVP:
                self.status_text = "PVP-режим: ход белых"
            else:
                self.status_text = "Режим ИИ: ход белых"
            self.update()
        elif event.key() == Qt.Key_1:
            self.ai.depth = 1
            self.status_text = "Сложность: Лёгкая (глубина 1)"
            self.update()
        elif event.key() == Qt.Key_2:
            self.ai.depth = 3
            self.status_text = "Сложность: Средняя (глубина 3)"
            self.update()
        elif event.key() == Qt.Key_3:
            self.ai.depth = 5
            self.status_text = "Сложность: Сложная (глубина 5)"
            self.update()
        else:
            super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════════
# МИКСИН: ОНЛАЙН-PVP (синхронизация с сервером)
# ═══════════════════════════════════════════════════════════════════

class _ChessNetMixin:
    """Сетевой режим (MODE_NET): доска работает «зеркально» с серверным
    матчем. Ходы наружу — через callback on_net_move(uci), входящие —
    через apply_net_state(), который диалог дергает из опросного таймера."""

    def apply_net_state(self, match: dict | None) -> None:
        """Применяет состояние матча с сервера к локальной доске.

        match — словарь из ответа !chess state: {match_id, white, black,
        status, moves, result, ...}. Если match=None — выходим из NET-режима.

        Логика:
          1. Если match.status == "finished" → показываем финальный результат.
          2. Если количество ходов в match.moves больше, чем мы применили
             локально (net_applied_moves) — «проигрываем» новые ходы
             соперника через make_move() и запускаем анимацию для последнего.
          3. Иначе — ничего не делаем (наш собственный ход уже применён).
        """
        if match is None:
            self.net_match_id = None
            self.net_opponent_name = ""
            self.status_text = "🌐 Нет активного матча"
            self.update()
            return

        self.net_match_id = match.get("match_id")
        # Имя соперника = другой игрок в матче.
        white_name = match.get("white")
        black_name = match.get("black")
        if self.net_color:  # мы белые → соперник чёрные
            self.net_opponent_name = black_name or "Ожидание…"
        else:
            self.net_opponent_name = white_name or "?"

        server_moves = match.get("moves", []) or []
        status = match.get("status")
        result = match.get("result")

        # Если матч завершён — фиксируем game_over.
        if status == "finished":
            # Применяем недостающие ходы (например, последний матующий ход
            # соперника, который мы ещё не видели).
            while self.net_applied_moves < len(server_moves):
                uci = server_moves[self.net_applied_moves]
                mv = self._uci_to_move(uci)
                if mv is None or mv not in self.engine.generate_moves():
                    break
                self.engine.make_move(mv)
                self.net_applied_moves += 1
                fr, to, _ = mv
                self.last_move = (fr, to)
            # Если состояние ещё не отмечено как game_over, отмечаем.
            if not self.game_over:
                self.game_over = True
                # Определяем исход для игрока.
                my_won = (
                    (result == "white" and self.net_color) or
                    (result == "black" and not self.net_color)
                )
                if result == "draw":
                    self.status_text = "½ Ничья! Матч завершён."
                    if self.on_game_over:
                        self.on_game_over("draw", None)
                elif my_won:
                    self.status_text = "🏆 Ты победил! Матч завершён."
                    if self.on_game_over:
                        outcome = "win_white" if self.net_color else "win_black"
                        self.on_game_over(outcome, "white" if self.net_color else "black")
                else:
                    self.status_text = "🏳 Поражение. Матч завершён."
                    if self.on_game_over:
                        outcome = "win_black" if self.net_color else "win_white"
                        self.on_game_over(outcome, "black" if self.net_color else "white")
            self.update()
            return

        # Матч ещё идёт (status == "playing" или "waiting").
        # Если мы ждём второго игрока — показываем.
        if status == "waiting":
            self.status_text = f"🌐 Ожидание соперника… Код матча: {self.net_match_id}"
            self.update()
            return

        # status == "playing": проверяем, есть ли новые ходы соперника.
        new_count = len(server_moves) - self.net_applied_moves
        if new_count <= 0:
            # Нечего применять (наш ход уже у нас, и соперник не ходил).
            self._check_state()
            self.update()
            return

        # Проигрываем все новые ходы (обычно это 1 ход соперника, но если мы
        # долго не опрашивали — может быть больше).
        last_fr = last_to = -1
        for i in range(self.net_applied_moves, len(server_moves)):
            uci = server_moves[i]
            mv = self._uci_to_move(uci)
            if mv is None or mv not in self.engine.generate_moves():
                # Сервер прислал нелегальный ход — игнорируем, дальше тоже
                # нельзя применять (состояние рассинхронизировано).
                break
            self.engine.make_move(mv)
            self.net_applied_moves += 1
            last_fr, last_to, _ = mv

        if last_fr != -1:
            # Анимация для последнего применённого хода (обычно — соперника).
            self._start_anim(last_fr, last_to, self.engine.board[last_to])
            self.last_move = (last_fr, last_to)
        self._check_state()
        self.update()

    def _uci_to_move(self, uci: str) -> tuple[int, int, str | None] | None:
        """Конвертирует UCI-строку в move-tuple движка. None если невалидно."""
        if not isinstance(uci, str):
            return None
        uci = uci.strip().lower()
        if len(uci) not in (4, 5):
            return None
        files = "abcdefgh"
        try:
            f1 = files.index(uci[0])
            r1 = 8 - int(uci[1])
            f2 = files.index(uci[2])
            r2 = 8 - int(uci[3])
        except (ValueError, IndexError):
            return None
        if not (0 <= f1 < 8 and 0 <= r1 < 8 and 0 <= f2 < 8 and 0 <= r2 < 8):
            return None
        fr = self.engine.idx(r1, f1)
        to = self.engine.idx(r2, f2)
        promo = None
        if len(uci) == 5:
            p = uci[4].upper()
            if p not in "QRBN":
                return None
            promo = p
        return (fr, to, promo)

    def start_net_match(self, color: bool, match_id: str, opponent_name: str = "") -> None:
        """Инициализирует локальную доску для онлайн-матча.

        Вызывается диалогом после успешного host/join. Сбрасывает движок к
        начальной позиции, устанавливает цвет игрока и ID матча. Если игрок
        чёрные — доска автоматически переворачивается (чтобы свои фигуры
        были снизу)."""
        self.engine.reset()
        self.game_over = False
        self.selected = -1
        self.legal_targets = set()
        self.last_move = None
        self.net_color = color
        self.net_match_id = match_id
        self.net_opponent_name = opponent_name
        self.net_applied_moves = 0
        # Чёрные видят доску с обратной стороны.
        self.flipped = not color
        self.status_text = f"🌐 Матч {match_id} начат. Ты играешь {'белыми' if color else 'чёрными'}."
        self.update()


# ═══════════════════════════════════════════════════════════════════
# МИКСИН: ОТРИСОВКА
# ═══════════════════════════════════════════════════════════════════

class _ChessRenderMixin:
    """paintEvent доски: клетки, подсветки, координаты, рамка, фигуры.
    Сами фигуры рисует ChessPieceRenderer (вынесен в отдельный модуль)."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        size = self.width() // 8

        # Фон доски
        p.fillRect(self.rect(), QColor(30, 30, 30))

        # Клетки с градиентами
        for r in range(8):
            for c in range(8):
                sq = self.engine.idx(r, c)
                x = c * size
                y = r * size
                if self.flipped:
                    x = (7 - c) * size
                    y = (7 - r) * size

                is_light = (r + c) % 2 == 0
                base = QColor(240, 217, 181) if is_light else QColor(181, 136, 99)
                grad = QLinearGradient(x, y, x + size, y + size)
                grad.setColorAt(0, base.lighter(108))
                grad.setColorAt(1, base.darker(108))
                p.fillRect(x + 1, y + 1, size - 2, size - 2, grad)

                # Подсветка последнего хода (мягкое золотистое свечение)
                if self.last_move and sq in self.last_move:
                    p.fillRect(x + 1, y + 1, size - 2, size - 2, QColor(255, 213, 79, 70))

                # Подсветка выбранной клетки (мягкий голубой)
                if sq == self.selected:
                    p.fillRect(x + 1, y + 1, size - 2, size - 2, QColor(100, 180, 255, 110))

                # Подсветка легальных ходов
                if sq in self.legal_targets:
                    if self.engine.board[sq] != '.':
                        p.setPen(QPen(QColor(231, 76, 60, 220), 3))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(x + 5, y + 5, size - 10, size - 10)
                    else:
                        # Маленький аккуратный маркер в центре клетки
                        cx, cy = x + size // 2, y + size // 2
                        rg = QRadialGradient(cx, cy, size // 6)
                        rg.setColorAt(0, QColor(50, 200, 90, 180))
                        rg.setColorAt(1, QColor(50, 200, 90, 0))
                        p.setBrush(rg)
                        p.setPen(Qt.NoPen)
                        p.drawEllipse(cx - size // 6, cy - size // 6, size // 3, size // 3)

                # Шах королю — пульсирующий красный
                piece = self.engine.board[sq]
                if piece.upper() == 'K' and self.engine.is_check(self.engine.is_white(piece)):
                    rg = QRadialGradient(x + size // 2, y + size // 2, size // 2)
                    rg.setColorAt(0, QColor(255, 50, 50, 120))
                    rg.setColorAt(1, QColor(255, 0, 0, 40))
                    p.fillRect(x + 1, y + 1, size - 2, size - 2, rg)

        # Рисуем фигуры
        for i, piece in enumerate(self.engine.board):
            if piece == '.' or (i == self.anim_from and self.anim_progress < 1.0):
                continue
            pos = self.pos_of(i)
            self._draw_piece(p, pos.x(), pos.y(), size, piece)

        # Анимированная фигура
        if self.anim_progress < 1.0 and self.anim_from != -1:
            p1 = self.pos_of(self.anim_from)
            p2 = self.pos_of(self.anim_to)
            ax = int(p1.x() + (p2.x() - p1.x()) * self.anim_progress)
            ay = int(p1.y() + (p2.y() - p1.y()) * self.anim_progress)
            # Мягкая тень под летящей фигурой
            p.setBrush(QColor(0, 0, 0, 90))
            p.setPen(Qt.NoPen)
            p.drawEllipse(ax + 6, ay + size - 8, size - 12, 10)
            self._draw_piece(p, ax, ay, size, self.anim_piece, elevated=True)

        # Координаты доски (files a–h и ranks 1–8)
        files = "abcdefgh"
        ranks = "87654321"
        p.setFont(QFont("Consolas", 9, QFont.Bold))
        for i in range(8):
            # Буквы снизу
            file_char = files[i] if not self.flipped else files[7 - i]
            rank_char = ranks[i] if not self.flipped else ranks[7 - i]
            # file — нижняя строка каждой колонки
            p.setPen(QColor(20, 20, 20, 180))
            p.drawText(i * size + size - 12, self.height() - 4, file_char)
            # rank — левый столбец каждой строки
            p.drawText(3, i * size + 13, rank_char)

        # Обрамление с двойной рамкой
        # ВАЖНО: после отрисовки фигур кисть QPainter всё ещё хранит body_fill
        # последней фигуры (кремовый для белых / тёмный для чёрных). Без
        # явного NoBrush вызов drawRect залил бы весь прямоугольник этой
        # кистью и доска исчезла бы под сплошной заливкой.
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(60, 45, 30), 3))
        p.drawRect(1, 1, self.width() - 2, self.height() - 2)
        p.setPen(QPen(QColor(120, 95, 60), 1))
        p.drawRect(4, 4, self.width() - 8, self.height() - 8)

    def _draw_piece(self, p: QPainter, x: int, y: int, size: int,
                    piece: str, elevated: bool = False):
        """Диспетчер к ChessPieceRenderer по текущему скину (self.piece_skin)."""
        ChessPieceRenderer.draw(p, self.piece_skin, x, y, size, piece, elevated)


# ═══════════════════════════════════════════════════════════════════
# ЯДРО: СОСТОЯНИЕ, РЕЖИМЫ, ИИ, АНИМАЦИЯ
# ═══════════════════════════════════════════════════════════════════

class ChessBoardWidget(_ChessInputMixin, _ChessNetMixin, _ChessRenderMixin, QWidget):
    # Режимы игры
    MODE_AI = "ai"    # игрок (белые) против ИИ (чёрные)
    MODE_PVP = "pvp"  # два игрока за одним компьютером (hot-seat)
    MODE_NET = "net"  # онлайн-PVP через relay-сервер

    # Сигнал: ИИ нашёл ход (потокобезопасное обновление UI).
    # Передаёт (fr, to, promo) или None если ходов нет.
    _ai_move_ready = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(640, 640)
        self.engine = ChessEngine()
        self.ai = ChessAI(depth=3)
        self.ai_enabled = True
        self.ai_thinking = False
        self._ai_thread = None  # фоновый поток для вычисления хода ИИ
        self.player_side = True  # True = белые (в PVP не используется для ELO)

        # Режим: "ai" (по умолчанию) или "pvp" (hot-seat между двумя людьми).
        # В PVP оба цвета управляются мышью — ИИ не ходит.
        self.mode = self.MODE_AI
        # В PVP-режиме можно автоматически переворачивать доску после каждого
        # хода, чтобы каждый игрок видел свои фигуры снизу. По умолчанию вкл.
        self.auto_flip_pvp = True
        # Имена игроков для отображения в PVP (не влияют на ELO).
        self.white_player_name = "Игрок 1 (Белые)"
        self.black_player_name = "Игрок 2 (Чёрные)"

        self.selected = -1
        self.legal_targets: set[int] = set()
        self.last_move: tuple[int, int] | None = None
        self.anim_from = -1
        self.anim_to = -1
        self.anim_progress = 1.0
        self.anim_piece = ''
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._animate_step)
        self.anim_duration = 12  # кадров

        self.flipped = False
        self.game_over = False
        self.status_text = ""

        # Скин фигур. Берётся из QSettings (персистентно между запусками),
        # иначе — PIECE_SKIN_DEFAULT.
        settings = QSettings("FriendRelay", "Chess")
        self.piece_skin = settings.value("piece_skin", PIECE_SKIN_DEFAULT, type=str)
        if self.piece_skin not in PIECE_SKINS:
            self.piece_skin = PIECE_SKIN_DEFAULT

        # Callback, вызываемый когда партия завершается. Сигнатура:
        #   on_game_over(outcome: str | None, winner_color: str | None)
        # outcome: "win_white" | "win_black" | "draw" | None (None — в PVP)
        self.on_game_over = None

        # ─── Онлайн-PVP (MODE_NET) ─────────────────────────────────────
        # В этом режиме доска работает «зеркально» с серверным матчем:
        # каждый ход игрока отправляется через callback on_net_move(uci),
        # а внешнее опросное обновление приходит через apply_net_state().
        # Цвет игрока: True = белые, False = чёрные (как player_side).
        self.net_color = True
        # ID текущего матча на сервере (None = не в матче).
        self.net_match_id: str | None = None
        # Сколько ходов уже применено к self.engine из сетевого матча.
        # Используется apply_net_state() чтобы понять, появились ли новые
        # ходы от соперника.
        self.net_applied_moves = 0
        # Callback, который доска вызывает когда игрок сделал ход.
        # Сигнатура: on_net_move(uci: str) — диалог отправляет ход на сервер.
        self.on_net_move = None
        # Имя соперника (для отображения в статусе).
        self.net_opponent_name = ""

        self._promo_pending: tuple[int, int] | None = None

        # Связываем сигнал «ИИ нашёл ход» с обработчиком в главном потоке.
        # Сам поиск хода выполняется в фоновом потоке (threading.Thread),
        # а применение хода + перерисовка — в главном через сигнал.
        self._ai_move_ready.connect(self._on_ai_move_ready)

    # ------------------------------------------------------------------
    # Анимация хода
    # ------------------------------------------------------------------

    def _start_anim(self, fr: int, to: int, piece: str):
        self.anim_from = fr
        self.anim_to = to
        self.anim_piece = piece
        self.anim_progress = 0.0
        self.anim_timer.start(16)

    def _animate_step(self):
        self.anim_progress += 1 / self.anim_duration
        if self.anim_progress >= 1.0:
            self.anim_progress = 1.0
            self.anim_timer.stop()
            self.anim_from = -1
            self.anim_to = -1
            self._check_state()
            # В режиме PVP ИИ не ходит — оба цвета играют люди.
            if (self.mode == self.MODE_AI
                    and self.ai_enabled
                    and not self.game_over):
                # ИИ ходит когда сейчас НЕ ход игрока. Если игрок белые
                # (player_side=True), ИИ ходит при white_to_move=False.
                # Если игрок чёрные (player_side=False), ИИ ходит при
                # white_to_move=True.
                ai_turn = (self.engine.white_to_move != self.player_side)
                if ai_turn:
                    self._ai_move()
            elif self.mode == self.MODE_PVP and not self.game_over:
                # Авто-переворот доски между ходами в PVP, чтобы каждый
                # игрок видел свои фигуры снизу.
                if self.auto_flip_pvp:
                    self.flipped = not self.flipped
            # В режиме NET анимация хода игрока завершена. Если игрок только
            # что походил — нужно отправить ход на сервер. Делаем это в
            # _commit_move (через on_net_move), а не здесь.
        self.update()

    # ------------------------------------------------------------------
    # Игровое состояние
    # ------------------------------------------------------------------

    def _check_state(self):
        # Идемпотентность: финальная позиция уже зафиксирована. Раньше
        # повторный вызов (ход ИИ → _on_ai_move_ready, затем конец анимации →
        # _animate_step, а в NET ещё и apply_net_state) дёргал on_game_over
        # ДВАЖДЫ — диалог сохранял «поражение» повторно: ELO падал вдвое
        # сильнее и в чат уходили два поста про поражение.
        if self.game_over:
            return
        if self.engine.is_checkmate():
            winner_white = not self.engine.white_to_move  # ход белых = мат белым = победили чёрные
            winner = "Чёрные" if self.engine.white_to_move else "Белые"
            self.status_text = f"♚ МАТ! Победа {winner}"
            self.game_over = True
            if self.on_game_over:
                outcome = "win_white" if winner_white else "win_black"
                self.on_game_over(outcome, "white" if winner_white else "black")
        elif self.engine.is_stalemate():
            self.status_text = "½ Пат — ничья"
            self.game_over = True
            if self.on_game_over:
                self.on_game_over("draw", None)
        elif self.engine.is_check(self.engine.white_to_move):
            side = "Белым" if self.engine.white_to_move else "Чёрным"
            self.status_text = f"⚡ Шах {side}!"
        else:
            if self.mode == self.MODE_PVP:
                side = self.white_player_name if self.engine.white_to_move else self.black_player_name
                self.status_text = f"Ход: {side}"
            elif self.mode == self.MODE_NET:
                # В NET-режиме показываем чей ход и кто соперник.
                my_turn = (self.engine.white_to_move == self.net_color)
                if my_turn:
                    self.status_text = "🌐 Твой ход"
                else:
                    self.status_text = f"🌐 Ход соперника ({self.net_opponent_name})…"
            else:
                self.status_text = "Ход белых" if self.engine.white_to_move else "Ход чёрных"

    def _commit_move(self, fr: int, to: int, move: tuple):
        """Финальная фиксация хода — анимация + применение к движку."""
        self._start_anim(fr, to, self.engine.board[fr])
        self.engine.make_move(move)
        self.selected = -1
        self.legal_targets = set()
        self.last_move = (fr, to)
        self._promo_pending = None
        # В NET-режиме отправляем ход на сервер через колбэк.
        if self.mode == self.MODE_NET:
            # Локально ход уже применён (make_move выше). Считаем его в
            # net_applied_moves, чтобы последующий poll не применил его
            # повторно (наш собственный ход сервер тоже вернёт в moves[]).
            self.net_applied_moves += 1
            uci = self._move_to_uci(fr, to, move[2])
            if self.on_net_move is not None:
                try:
                    self.on_net_move(uci)
                except Exception:
                    pass
        self.update()

    @staticmethod
    def _move_to_uci(fr: int, to: int, promo: str | None) -> str:
        """Конвертирует (fr, to, promo) движка в UCI-строку: e2e4, e7e8q."""
        files = "abcdefgh"
        ranks = "87654321"
        f1, r1 = files[fr % 8], ranks[fr // 8]
        f2, r2 = files[to % 8], ranks[to // 8]
        promo_part = promo.lower() if promo else ""
        return f"{f1}{r1}{f2}{r2}{promo_part}"

    # ------------------------------------------------------------------
    # Превращение пешки (диалог выбора фигуры)
    # ------------------------------------------------------------------

    def _show_promo_dialog(self, promos: list[tuple[int, int, str | None]]):
        """Открывает модальный диалог выбора фигуры превращения."""
        # Определяем цвет пешки, которая превращается
        fr = promos[0][0]
        piece = self.engine.board[fr]
        is_white = self.engine.is_white(piece)
        # Уникальные типы превращения (всегда Q/R/B/N)
        types = sorted({m[2].upper() for m in promos})
        dlg = QDialog(self)
        dlg.setWindowTitle("Превращение пешки")
        dlg.setModal(True)
        layout = QVBoxLayout(dlg)
        hint = QLabel("Выберите фигуру для превращения:")
        hint.setStyleSheet("color: #f0f0f5; font-size: 13px; padding: 8px;")
        layout.addWidget(hint)
        row = QHBoxLayout()
        layout.addLayout(row)
        # Карта тип -> полноценный move (с правильным регистром promo)
        move_by_type: dict[str, tuple] = {}
        for m in promos:
            t = m[2].upper()
            if t not in move_by_type:
                move_by_type[t] = m
        for t in types:
            glyph = PIECE_UNICODE.get(t if is_white else t.lower(), '?')
            btn = QPushButton(f"{glyph}  {t}")
            btn.setFixedHeight(56)
            btn.setMinimumWidth(120)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: #2a2a32; color: #f0f0f5;
                    border: 1px solid #3a3a44; border-radius: 8px;
                    font-size: 22px; font-weight: bold; padding: 8px 14px;
                }
                QPushButton:hover { background: #3a3a48; border-color: #dcc8a0; }
            """)
            btn.clicked.connect(lambda _, _t=t: self._on_promo_chosen(_t, move_by_type, dlg))
            row.addWidget(btn)
        # v1.9.5: Esc/крестик у диалога превращения раньше оставляли
        # _promo_pending висеть — mousePressEvent игнорировал ВСЕ клики до
        # конца партии (софтлок доски).
        dlg.rejected.connect(lambda: setattr(self, "_promo_pending", None))
        dlg.exec()

    def _on_promo_chosen(self, t: str, move_by_type: dict, dlg: QDialog):
        dlg.accept()
        if t not in move_by_type:
            return
        fr, to, _promo = move_by_type[t]
        self._commit_move(fr, to, move_by_type[t])

    # ------------------------------------------------------------------
    # ИИ: вычисление хода в фоновом потоке
    # ------------------------------------------------------------------

    def _ai_move(self):
        """Запускает вычисление хода ИИ в фоновом потоке.

        Раньше поиск хода выполнялся в главном потоке (через
        QTimer.singleShot), что замораживал весь UI на время работы
        negamax-поиска (особенно на глубине 5 — до нескольких секунд).
        Кнопки и доска не реагировали на клики, создавалось впечатление
        «кнопки не работают». Теперь ИИ думает в отдельном потоке, UI
        остаётся отзывчивым, а ход применяется через сигнал когда готово.
        """
        self.ai_thinking = True
        self.status_text = "🤖 ИИ думает..."
        self.update()

        # Снимаем копию позиции для потока — ИИ работает с независимым
        # движком, чтобы не гонять данные с главным потоком.
        # ВАЖНО: ChessEngine не потокобезопасен, но мы создаём КОПИЮ
        # позиций (board list + флаги) и передаём её в поток. Главный
        # поток свой engine не трогает пока ai_thinking=True (mousePressEvent
        # возвращает сразу).
        board_copy = list(self.engine.board)
        white_to_move = self.engine.white_to_move
        castling = dict(self.engine.castling)
        en_passant = self.engine.en_passant

        def _compute():
            """Выполняется в фоновом потоке — не трогает UI!"""
            try:
                # Создаём независимый движок и восстанавливаем позицию.
                eng = ChessEngine()
                eng.board = list(board_copy)
                eng.white_to_move = white_to_move
                eng.castling = dict(castling)
                eng.en_passant = en_passant
                eng.history = []  # история не нужна для поиска хода
                move = self.ai.find_best_move(eng)
                self._ai_move_ready.emit(move)
            except Exception:
                # При любой ошибке в потоке — освобождаем флаг.
                self._ai_move_ready.emit(None)

        # Запускаем в daemon-потоке (автоматически завершится при закрытии).
        self._ai_thread = threading.Thread(target=_compute, daemon=True)
        self._ai_thread.start()

    def _on_ai_move_ready(self, move):
        """Вызывается в главном потоке когда ИИ нашёл ход (через сигнал)."""
        self.ai_thinking = False
        self._ai_thread = None
        if move is not None:
            fr, to, promo = move
            # Проверяем что ход всё ещё валиден (мало ли режим сменился).
            if self.mode == self.MODE_AI and not self.game_over:
                # v1.9.5: R/смена режима во время расчёта СБРАСЫВАЛИ доску,
                # а фоновый поток возвращал ход СТАРОЙ позиции — он
                # применялся к новой доске без проверки (фигура «ходила» с
                # пустой клетки, позиция портилась). Теперь ход обязан быть
                # в списке легальных ТЕКУЩЕЙ позиции.
                if move not in self.engine.generate_moves():
                    return
                self._start_anim(fr, to, self.engine.board[fr])
                self.engine.make_move(move)
                self.last_move = (fr, to)
        self._check_state()
        self.update()

    # ------------------------------------------------------------------
    # Управление партией: сброс, режим, скин
    # ------------------------------------------------------------------

    def _reset_game(self):
        """Сброс партии к начальной позиции без смены режима.

        В режиме ИИ случайно выбираем сторону игрока (белые/чёрные) —
        чтобы можно было тренироваться играть и за чёрных. Доска
        автоматически переворачивается если игрок чёрные.
        """
        self.engine.reset()
        self.game_over = False
        self.selected = -1
        self.legal_targets = set()
        self.last_move = None
        # В режиме ИИ — случайная сторона игрока при каждом рестарте.
        if self.mode == self.MODE_AI:
            self.player_side = random.choice([True, False])
            self.flipped = not self.player_side  # чёрные → доска перевёрнута
            side_name = "белыми" if self.player_side else "чёрными"
            self.status_text = f"Новая партия. Вы играете {side_name}."
        else:
            self.flipped = False  # после рестарта белые всегда снизу
            self.status_text = "Новая партия"
        # Если игрок теперь чёрные — ИИ (белые) ходит первым.
        if self.mode == self.MODE_AI and not self.player_side and not self.game_over:
            self._ai_move()
        self.update()

    def set_mode(self, mode: str):
        """Переключает режим игры (MODE_AI / MODE_PVP / MODE_NET).

        Для MODE_AI и MODE_PVP — немедленно начинает новую партию.
        Для MODE_NET — только переводит виджет в режим ожидания сетевого
        матча; фактический запуск партии делает start_net_match() после
        успешного host/join."""
        if mode not in (self.MODE_AI, self.MODE_PVP, self.MODE_NET):
            return
        self.mode = mode
        # Сбрасываем сетевое состояние при выходе из NET-режима.
        if mode != self.MODE_NET:
            self.net_match_id = None
            self.net_opponent_name = ""
            self.net_applied_moves = 0
        if mode == self.MODE_NET:
            # Не начинаем партию — ждём host/join через диалог.
            self.engine.reset()
            self.game_over = False
            self.selected = -1
            self.legal_targets = set()
            self.last_move = None
            self.flipped = False
            self.status_text = "🌐 Сетевой режим. Создай матч или присоединись."
            self.update()
            return
        self._reset_game()
        if mode == self.MODE_PVP:
            self.status_text = f"PVP: ход {self.white_player_name}"
        else:
            self.status_text = "Режим ИИ: ход белых"
        self.update()

    def set_skin(self, skin: str):
        """Переключает скин фигур (standard/gothic/square).
        Сохраняет выбор в QSettings, чтобы он пережил перезапуск приложения.
        Новая партия не начинается — просто перерисовываем доску."""
        if skin not in PIECE_SKINS:
            return
        self.piece_skin = skin
        QSettings("FriendRelay", "Chess").setValue("piece_skin", skin)
        self.update()
