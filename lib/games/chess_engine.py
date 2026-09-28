"""Шахматный движок — чистая логика без Qt-зависимостей.

Содержит:
  - ChessEngine: доска, генерация легальных ходов, рокировка, взятие на
    проходе, превращение, шах/мат/пат, оценка позиции (piece values + PST)
  - ChessAI: negamax с alpha-beta отсечениями поверх ChessEngine

Пользователи модуля:
  - qt_app/widgets/chess_board_widget.py — игровая доска (UI)
  - lib/chess_bot.py — серверная валидация ходов онлайн-матчей.
    Раньше бот импортировал ChessEngine из Qt-виджета (layer leak:
    серверный код тянул за собой PySide6) — теперь обе стороны берут
    движок отсюда, и relay-сервер работает на безголовом хосте без Qt.

Представление доски: список из 64 символов, index = row*8 + col.
Индекс 0 — это a8 (левый верхний угол с точки зрения белых), ряд 6 —
белые пешки, ряд 1 — чёрные. Белые фигуры — заглавные буквы.
"""
from __future__ import annotations

import random


class ChessEngine:
    """Полноценный шахматный движок: генерация ходов, правила, нотация."""

    PIECE_VALUES = {'P': 100, 'N': 320, 'B': 330, 'R': 500, 'Q': 900, 'K': 20000}
    PST = {
        'P': [0,  0,  0,  0,  0,  0,  0,  0,
              50, 50, 50, 50, 50, 50, 50, 50,
              10, 10, 20, 30, 30, 20, 10, 10,
               5,  5, 10, 25, 25, 10,  5,  5,
               0,  0,  0, 20, 20,  0,  0,  0,
               5, -5,-10,  0,  0,-10, -5,  5,
               5, 10, 10,-20,-20, 10, 10,  5,
               0,  0,  0,  0,  0,  0,  0,  0],
        'N': [-50,-40,-30,-30,-30,-30,-40,-50,
              -40,-20,  0,  0,  0,  0,-20,-40,
              -30,  0, 10, 15, 15, 10,  0,-30,
              -30,  5, 15, 20, 20, 15,  5,-30,
              -30,  0, 15, 20, 20, 15,  0,-30,
              -30,  5, 10, 15, 15, 10,  5,-30,
              -40,-20,  0,  5,  5,  0,-20,-40,
              -50,-40,-30,-30,-30,-30,-40,-50],
        'B': [-20,-10,-10,-10,-10,-10,-10,-20,
              -10,  0,  0,  0,  0,  0,  0,-10,
              -10,  0, 10, 10, 10, 10,  0,-10,
              -10,  5,  5, 10, 10,  5,  5,-10,
              -10,  0,  5, 10, 10,  5,  0,-10,
              -10, 10, 10, 10, 10, 10, 10,-10,
              -10,  5,  0,  0,  0,  0,  5,-10,
              -20,-10,-10,-10,-10,-10,-10,-20],
        'R': [  0,  0,  0,  0,  0,  0,  0,  0,
                5, 10, 10, 10, 10, 10, 10,  5,
               -5,  0,  0,  0,  0,  0,  0, -5,
               -5,  0,  0,  0,  0,  0,  0, -5,
               -5,  0,  0,  0,  0,  0,  0, -5,
               -5,  0,  0,  0,  0,  0,  0, -5,
               -5,  0,  0,  0,  0,  0,  0, -5,
                0,  0,  0,  5,  5,  0,  0,  0],
        'Q': [-20,-10,-10, -5, -5,-10,-10,-20,
              -10,  0,  0,  0,  0,  0,  0,-10,
              -10,  0,  5,  5,  5,  5,  0,-10,
               -5,  0,  5,  5,  5,  5,  0, -5,
                0,  0,  5,  5,  5,  5,  0, -5,
              -10,  5,  5,  5,  5,  5,  0,-10,
              -10,  0,  5,  0,  0,  0,  0,-10,
              -20,-10,-10, -5, -5,-10,-10,-20],
        'K': [-30,-40,-40,-50,-50,-40,-40,-30,
              -30,-40,-40,-50,-50,-40,-40,-30,
              -30,-40,-40,-50,-50,-40,-40,-30,
              -30,-40,-40,-50,-50,-40,-40,-30,
              -20,-30,-30,-40,-40,-30,-30,-20,
              -10,-20,-20,-20,-20,-20,-20,-10,
               20, 20,  0,  0,  0,  0, 20, 20,
               20, 30, 10,  0,  0, 10, 30, 20],
    }

    def __init__(self):
        self.reset()

    def reset(self):
        self.board = [
            'r','n','b','q','k','b','n','r',
            'p','p','p','p','p','p','p','p',
            '.','.','.','.','.','.','.','.',
            '.','.','.','.','.','.','.','.',
            '.','.','.','.','.','.','.','.',
            '.','.','.','.','.','.','.','.',
            'P','P','P','P','P','P','P','P',
            'R','N','B','Q','K','B','N','R',
        ]
        self.white_to_move = True
        self.castling = {'K': True, 'Q': True, 'k': True, 'q': True}
        self.en_passant = -1
        self.halfmove = 0
        self.fullmove = 1
        self.history: list[tuple] = []

    def idx(self, row: int, col: int) -> int:
        return row * 8 + col

    def in_bounds(self, r: int, c: int) -> bool:
        return 0 <= r < 8 and 0 <= c < 8

    def piece_at(self, row: int, col: int) -> str:
        return self.board[self.idx(row, col)]

    def is_white(self, piece: str) -> bool:
        return piece.isupper()

    def make_move(self, move: tuple[int, int, str | None]) -> bool:
        fr, to, promo = move
        piece = self.board[fr]
        captured = self.board[to]

        # Сохраняем состояние
        state = (fr, to, captured, dict(self.castling), self.en_passant, self.halfmove, promo)
        self.history.append(state)

        self.board[to] = piece
        self.board[fr] = '.'

        # Превращение пешки
        if promo:
            self.board[to] = promo if self.is_white(piece) else promo.lower()

        # Взятие на проходе
        # БАГФИКС: ранее формула ep_row = (fr//8 + to//8)//2 считала, что
        # взятая пешка стоит между source и destination — но на самом деле
        # она стоит на ТОЙ ЖЕ горизонтали, что и бьющая пешка (то есть на
        # ряду fr//8). Старая формула работала только для одного цвета и
        # silently оставала взятую пешку на доске при en passant в другую
        # сторону.
        if piece.upper() == 'P' and to == self.en_passant:
            ep_row = fr // 8
            self.board[self.idx(ep_row, to % 8)] = '.'

        # Обновление en passant
        self.en_passant = -1
        if piece.upper() == 'P' and abs(fr // 8 - to // 8) == 2:
            self.en_passant = (fr + to) // 2

        # Рокировка
        if piece.upper() == 'K' and abs((fr % 8) - (to % 8)) == 2:
            row = fr // 8
            if to > fr:  # короткая
                self.board[self.idx(row, 7)] = '.'
                self.board[self.idx(row, 5)] = 'R' if self.white_to_move else 'r'
            else:  # длинная
                self.board[self.idx(row, 0)] = '.'
                self.board[self.idx(row, 3)] = 'R' if self.white_to_move else 'r'

        # Обновление прав рокировки
        if piece == 'K':
            self.castling['K'] = self.castling['Q'] = False
        if piece == 'k':
            self.castling['k'] = self.castling['q'] = False
        if fr == 0 or to == 0:
            self.castling['q'] = False
        if fr == 7 or to == 7:
            self.castling['K'] = False
        if fr == 56 or to == 56:
            self.castling['Q'] = False
        if fr == 63 or to == 63:
            self.castling['k'] = False

        self.halfmove += 1
        if not self.white_to_move:
            self.fullmove += 1
        self.white_to_move = not self.white_to_move
        return True

    def undo_move(self):
        if not self.history:
            return
        fr, to, captured, old_castling, old_ep, old_half, promo = self.history.pop()
        piece = self.board[to]
        self.board[fr] = piece if not promo else ('P' if self.is_white(piece) else 'p')
        self.board[to] = captured
        self.castling = old_castling
        self.en_passant = old_ep
        self.halfmove = old_half
        self.white_to_move = not self.white_to_move
        if not self.white_to_move:
            self.fullmove -= 1

        # Восстановление взятия на проходе
        if piece.upper() == 'P' and to == old_ep:
            ep_row = 3 if self.white_to_move else 4
            self.board[self.idx(ep_row, to % 8)] = 'p' if self.white_to_move else 'P'

        # Восстановление ладьи при рокировке
        if piece.upper() == 'K' and abs((fr % 8) - (to % 8)) == 2:
            row = fr // 8
            if to > fr:
                self.board[self.idx(row, 7)] = 'R' if self.white_to_move else 'r'
                self.board[self.idx(row, 5)] = '.'
            else:
                self.board[self.idx(row, 0)] = 'R' if self.white_to_move else 'r'
                self.board[self.idx(row, 3)] = '.'

    def generate_moves(self) -> list[tuple[int, int, str | None]]:
        moves = []
        for r in range(8):
            for c in range(8):
                p = self.piece_at(r, c)
                if p == '.' or self.is_white(p) != self.white_to_move:
                    continue
                moves.extend(self._piece_moves(r, c, p))
        # Фильтруем ходы, оставляющие короля под шахом
        legal = []
        for m in moves:
            self.make_move(m)
            if not self.is_check(not self.white_to_move):
                legal.append(m)
            self.undo_move()
        return legal

    def _piece_moves(self, r: int, c: int, p: str) -> list[tuple[int, int, str | None]]:
        moves = []
        white = self.is_white(p)
        # БАГФИКС: индекс 0 доски — это a8 (чёрная задняя линия), белые пешки
        # стоят на row=6 и должны идти к row=0, чёрные — с row=1 к row=7.
        # Было перепутано (d=1 для белых), из-за чего пешки вообще не могли
        # ходить вперёд — это делало партию почти неиграбельной.
        d = -1 if white else 1
        pu = p.upper()

        if pu == 'P':
            nr = r + d
            # Вперёд
            if self.in_bounds(nr, c) and self.piece_at(nr, c) == '.':
                if nr == 0 or nr == 7:
                    for promo in ['Q', 'R', 'B', 'N'] if white else ['q', 'r', 'b', 'n']:
                        moves.append((self.idx(r, c), self.idx(nr, c), promo))
                else:
                    moves.append((self.idx(r, c), self.idx(nr, c), None))
                # С двойного ряда
                start = 6 if white else 1
                if r == start:
                    nr2 = r + 2 * d
                    if self.piece_at(nr2, c) == '.':
                        moves.append((self.idx(r, c), self.idx(nr2, c), None))
            # Взятия
            for dc in (-1, 1):
                nc = c + dc
                if not self.in_bounds(nr, nc):
                    continue
                target = self.piece_at(nr, nc)
                if target != '.' and self.is_white(target) != white:
                    if nr == 0 or nr == 7:
                        for promo in ['Q', 'R', 'B', 'N'] if white else ['q', 'r', 'b', 'n']:
                            moves.append((self.idx(r, c), self.idx(nr, nc), promo))
                    else:
                        moves.append((self.idx(r, c), self.idx(nr, nc), None))
                # En passant
                if self.en_passant == self.idx(nr, nc):
                    moves.append((self.idx(r, c), self.idx(nr, nc), None))

        elif pu == 'N':
            for dr, dc in [(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]:
                nr, nc = r + dr, c + dc
                if self.in_bounds(nr, nc):
                    t = self.piece_at(nr, nc)
                    if t == '.' or self.is_white(t) != white:
                        moves.append((self.idx(r, c), self.idx(nr, nc), None))

        elif pu == 'K':
            for dr, dc in [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]:
                nr, nc = r + dr, c + dc
                if self.in_bounds(nr, nc):
                    t = self.piece_at(nr, nc)
                    if t == '.' or self.is_white(t) != white:
                        moves.append((self.idx(r, c), self.idx(nr, nc), None))
            # Рокировка
            # БАГФИКС: флаг прав рокировки — ещё не гарантия, что король
            # стоит на своей начальной клетке (в here-позициях/после ручного
            # редактирования доски движок иначе генерирует «рокировку» с
            # пустой клетки e8/e1). Проверяем, что король на месте.
            row = 7 if white else 0
            king_home = self.piece_at(row, 4) == ('K' if white else 'k')
            if king_home and self.white_to_move:
                if self.castling.get('K') and all(self.piece_at(row, i) == '.' for i in (5, 6)):
                    if not self.is_square_attacked(row, 4, not white) and not self.is_square_attacked(row, 5, not white):
                        moves.append((self.idx(row, 4), self.idx(row, 6), None))
                if self.castling.get('Q') and all(self.piece_at(row, i) == '.' for i in (1, 2, 3)):
                    if not self.is_square_attacked(row, 4, not white) and not self.is_square_attacked(row, 3, not white):
                        moves.append((self.idx(row, 4), self.idx(row, 2), None))
            elif king_home:
                if self.castling.get('k') and all(self.piece_at(row, i) == '.' for i in (5, 6)):
                    if not self.is_square_attacked(row, 4, not white) and not self.is_square_attacked(row, 5, not white):
                        moves.append((self.idx(row, 4), self.idx(row, 6), None))
                if self.castling.get('q') and all(self.piece_at(row, i) == '.' for i in (1, 2, 3)):
                    if not self.is_square_attacked(row, 4, not white) and not self.is_square_attacked(row, 3, not white):
                        moves.append((self.idx(row, 4), self.idx(row, 2), None))

        elif pu in ('B', 'R', 'Q'):
            directions = []
            if pu in ('B', 'Q'):
                directions += [(-1,-1),(-1,1),(1,-1),(1,1)]
            if pu in ('R', 'Q'):
                directions += [(-1,0),(1,0),(0,-1),(0,1)]
            for dr, dc in directions:
                for step in range(1, 8):
                    nr, nc = r + dr * step, c + dc * step
                    if not self.in_bounds(nr, nc):
                        break
                    t = self.piece_at(nr, nc)
                    if t == '.':
                        moves.append((self.idx(r, c), self.idx(nr, nc), None))
                    elif self.is_white(t) != white:
                        moves.append((self.idx(r, c), self.idx(nr, nc), None))
                        break
                    else:
                        break
        return moves

    def is_square_attacked(self, row: int, col: int, by_white: bool) -> bool:
        for r in range(8):
            for c in range(8):
                p = self.piece_at(r, c)
                if p == '.' or self.is_white(p) != by_white:
                    continue
                # Пешка
                if p.upper() == 'P':
                    d = -1 if by_white else 1
                    if r + d == row and abs(c - col) == 1:
                        return True
                # Конь
                elif p.upper() == 'N':
                    if (abs(r - row) == 2 and abs(c - col) == 1) or (abs(r - row) == 1 and abs(c - col) == 2):
                        return True
                # Король
                elif p.upper() == 'K':
                    if max(abs(r - row), abs(c - col)) == 1:
                        return True
                # Слон/Ладья/Ферзь
                else:
                    dr = 0 if row == r else (row - r) // abs(row - r)
                    dc = 0 if col == c else (col - c) // abs(col - c)
                    if p.upper() == 'B' and (dr == 0 or dc == 0):
                        continue
                    if p.upper() == 'R' and not (dr == 0 or dc == 0):
                        continue
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    while self.in_bounds(rr, cc):
                        if rr == row and cc == col:
                            return True
                        if self.piece_at(rr, cc) != '.':
                            break
                        rr += dr
                        cc += dc
        return False

    def is_check(self, white: bool) -> bool:
        king = 'K' if white else 'k'
        for i, p in enumerate(self.board):
            if p == king:
                row, col = i // 8, i % 8
                return self.is_square_attacked(row, col, not white)
        return False

    def is_checkmate(self) -> bool:
        return len(self.generate_moves()) == 0 and self.is_check(self.white_to_move)

    def is_stalemate(self) -> bool:
        return len(self.generate_moves()) == 0 and not self.is_check(self.white_to_move)

    def evaluate(self) -> int:
        score = 0
        for i, p in enumerate(self.board):
            if p == '.':
                continue
            val = self.PIECE_VALUES.get(p.upper(), 0)
            pst = self.PST.get(p.upper(), [0] * 64)
            idx = i if p.isupper() else 63 - i
            if p.isupper():
                score += val + pst[idx]
            else:
                score -= val + pst[idx]
        return score if self.white_to_move else -score


class ChessAI:
    """Negamax с alpha-beta отсечениями. Работает с ЛЮБЫМ ChessEngine
    (в UI поток получает независимую копию движка — см. chess_board_widget)."""

    def __init__(self, depth: int = 3):
        self.depth = depth

    def find_best_move(self, engine: ChessEngine) -> tuple[int, int, str | None] | None:
        moves = engine.generate_moves()
        if not moves:
            return None
        random.shuffle(moves)
        best_move = None
        best_score = -999999
        for move in moves:
            engine.make_move(move)
            score = -self._negamax(engine, self.depth - 1, -99999, 99999)
            engine.undo_move()
            if score > best_score:
                best_score = score
                best_move = move
        return best_move

    def _negamax(self, engine: ChessEngine, depth: int, alpha: int, beta: int) -> int:
        if depth == 0:
            return engine.evaluate()
        moves = engine.generate_moves()
        if not moves:
            if engine.is_check(engine.white_to_move):
                return -90000 - depth
            return 0
        for move in moves:
            engine.make_move(move)
            score = -self._negamax(engine, depth - 1, -beta, -alpha)
            engine.undo_move()
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
        return alpha
