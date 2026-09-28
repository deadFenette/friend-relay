"""GoEngine — компактный движок Го (weiqi/baduk) для Friend Relay v2.0.6.

Что это и где живёт
Движок реализует классические правила Го в «упрощении для друзей»:

  • доски 9×9 / 13×13 / 19×19 (кликабельные камни, ход = точка пересечения);
  • взятие группы камней без свобод (дамэ) — flood fill по группе;
  • запрет самоубийства (suicide) — ход, после которого своя группа без
    свобод и без взятых камней, запрещён;
  • простое правило ко (simple ko): нельзя сразу отбить камень, только что
    взятый противником единственным камнем (позиция повториться не может);
  • пас; два паса подряд → конец партии и подсчёт;
  • подсчёт по площади (китайские правила): камни + окружённые пустые
    пункты, коми белым 6.5 (компенсация преимущества первого хода чёрных);
  • сдаться (resign) — победа соперника.

Мёртвые камни НЕ считаются автозахватом (полноценный Yose/детект мёртвых
групп — это ИИ-задача уровня GnuGo): «друзья досчитают руками», поэтому
подсчёт честно называет себя «оценкой по площади» — снятые партией группы
уже лежат в captured, живые камни на доске считаются живыми.

Почему отдельный файл (аналогично lib/games/chess_engine.py):
  - lib/bots/go.py      — серверный бот-рефери использует движок для
                          валидации ходов онлайн-матчей (на хосте без GUI);
  - qt_app/widgets/go_game_dialog.py — Qt-доска играет через этот же движок;
  - тесты (tests/test_go_v205.py) гоняют правила без Qt и без сервера.
Запрет зависимостей: никаких импортов Qt/сети — чистый Python.
"""
from __future__ import annotations

import random

# Цвета (int, чтобы состояние доски было списком чисел — дёшево копировать
# через list(board) на каждый гипотетический ход)
EMPTY = 0
BLACK = 1
WHITE = 2

DEFAULT_KOMI = 6.5   # компенсация белым за второй ход (китайские правила)

_LETTERS = "ABCDEFGHJKLMNOPQRST"    # буква I пропущена в Го-нотации (традиция)


class GoError(ValueError):
    """Недопустимый ход: занято / самоубийство / ко / вне доски."""


def parse_vertex(text: str, size: int) -> tuple[int, int] | None:
    """«D4» → (x=3, y=size-4). None — если вершина не разобрать.

    Го-нотация: буква колонки (A..T без I) + номер ряда снизу вверх
    (как в шахматной нотации FIDE). Поддерживаем и строчные буквы.
    """
    s = (text or "").strip().upper()
    if len(s) < 2 or len(s) > 3:
        return None
    col, num = s[0], s[1:]
    if col not in _LETTERS or not num.isdigit():
        return None
    x = _LETTERS.index(col)
    y = size - int(num)
    if not (0 <= x < size and 0 <= y < size):
        return None
    return x, y


def format_vertex(x: int, y: int, size: int) -> str:
    """(x, y) → «D4» — обратная parse_vertex (для истории матчей/ботов)."""
    return f"{_LETTERS[x]}{size - y}"


class GoEngine:
    """Состояние партии Го + применение ходов по правилам.

    Доска — плоский список size*size, индекс = y*size + x, y=0 сверху.
    Для валидации хода движок применяет его на КОПИИ доски (дёшево:
    361 int на 19×19), тем самым реальная доска не портится отказами.
    """

    def __init__(self, size: int = 9, komi: float = DEFAULT_KOMI):
        if size not in (9, 13, 19):
            size = 9    # защита от мусора из сети: только 3 канонических
        self.size = size
        self.komi = komi
        self.board: list[int] = [EMPTY] * (size * size)
        # Взятые камни: [чёрными взято, белыми взято] — по индексу цвета
        self.captured = [0, 0, 0]          # индекс 0 не используется
        self.ko_point: tuple[int, int] | None = None   # запрещённая точка
        self.passes_in_row = 0             # два паса подряд → конец
        self.move_number = 0
        self.last_move: tuple[int, int] | None = None  # метка последнего хода
        self.game_over = False

    # ── геометрия ────────────────────────────────────────────────────────
    def _idx(self, x: int, y: int) -> int:
        return y * self.size + x

    def _neighbors(self, x: int, y: int):
        """Соседние по стороне пункты (без диагоналей — правила Го)."""
        if x > 0:
            yield x - 1, y
        if x < self.size - 1:
            yield x + 1, y
        if y > 0:
            yield x, y - 1
        if y < self.size - 1:
            yield x, y + 1

    def group_and_liberties(self, x: int, y: int) -> tuple[set, set]:
        """Группа (связные камни одного цвета, координаты) и её свободы
        (дамэ). Реализация одна — на _group_on (см. ниже), чтобы «группа»
        везде означала одно и то же."""
        return self._group_on(self.board, x, y)

    # ── ходы ─────────────────────────────────────────────────────────────
    def is_legal(self, x: int, y: int, color: int) -> bool:
        """Можно ли поставить камень? (без изменения состояния)"""
        if self.game_over:
            return False
        try:
            self._apply_move(x, y, color, dry_run=True)
        except GoError:
            return False
        return True

    def play(self, x: int, y: int, color: int) -> list[tuple[int, int]]:
        """Сделать ход. Возвращает список взятых вершин.

        GoError — ход невозможен (занято / ко / самоубийство / партия
        кончена / цвет странный). После паса ко-точка снимается (правило:
        любое другое ход запрещённое положение исчезает).
        """
        captured = self._apply_move(x, y, color, dry_run=False)
        self.passes_in_row = 0
        self.move_number += 1
        self.last_move = (x, y)
        return captured

    def _apply_move(self, x: int, y: int, color: int, dry_run: bool):
        """Общая механика play()/is_legal(): проверки + снятие взятых.

        dry_run=True — ничего не меняем, только выбрасываем GoError или
        возвращаем список взятых (для is_legal и для эвристики ИИ).
        """
        if color not in (BLACK, WHITE):
            raise GoError("неизвестный цвет")
        if self.game_over:
            raise GoError("партия уже завершена")
        if not (0 <= x < self.size and 0 <= y < self.size):
            raise GoError("ход вне доски")
        if self.board[self._idx(x, y)] != EMPTY:
            raise GoError("пункт занят")
        # Простое ко: именно эта вершина запрещена ближайшим ходом
        if self.ko_point == (x, y):
            raise GoError("нельзя повторить позицию: ко")

        # Работаем на копии: отказ хода не портит реальную доску
        board = list(self.board)
        board[self._idx(x, y)] = color
        enemy = WHITE if color == BLACK else BLACK

        # 1) Взятие: соседние группы врага без свобод снимаются целиком
        captured: list[tuple[int, int]] = []
        seen_removed: set = set()
        for nx, ny in self._neighbors(x, y):
            if board[self._idx(nx, ny)] != enemy or (nx, ny) in seen_removed:
                continue
            stones, libs = self._group_on(board, nx, ny)
            if not libs:
                for sx, sy in stones:
                    board[self._idx(sx, sy)] = EMPTY
                    captured.append((sx, sy))
                seen_removed.update(stones)

        # 2) Самоубийство: своя группа без свобод и ничего не взято — нельзя
        stones, libs = self._group_on(board, x, y)
        if not libs and not captured:
            raise GoError("самоубийство запрещено")

        # 3) Простое ко: единственный свой камень встал в одиночную свободу
        #    и снял ровно один вражеский камень → противник не может сразу
        #    отбить (иначе вечное повторение). Запоминаем ВЕРШИНУ взятого.
        new_ko = None
        if len(captured) == 1 and len(stones) == 1 and len(
                self._group_on(board, x, y)[1]) == 1:
            new_ko = captured[0]

        if not dry_run:
            self.board = board
            self.captured[color] += len(captured)
            self.ko_point = new_ko
        return captured

    def _group_on(self, board: list[int], x: int, y: int) -> tuple[set, set]:
        """Группа/свободы на ПРОИЗВОЛЬНОЙ доске (нужно для dry-run).
        Возвращает (множество координат камней, множество координат
        свобод). Flood fill итеративно — глубина стека не зависит от
        размера группы."""
        size = self.size
        color = board[y * size + x]
        seen = {(x, y)}
        stack = [(x, y)]
        libs: set = set()
        while stack:
            cx, cy = stack.pop()
            for nx, ny in self._neighbors(cx, cy):
                v = board[ny * size + nx]
                if v == EMPTY:
                    libs.add((nx, ny))
                elif v == color and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        return seen, libs

    def pass_turn(self, color: int) -> None:
        """Пас. Два подряд → game_over (для сетевого матча рефери решает
        счёт сам; локальные режимы вызывают area_score()).

        color проверяется вызывающей стороной (бот знает, чей ход) —
        движок хранит только «два паса подряд».
        """
        if self.game_over:
            return
        self.passes_in_row += 1
        self.ko_point = None          # пас снимает ко-запрет
        self.move_number += 1
        self.last_move = None
        if self.passes_in_row >= 2:
            self.game_over = True

    # ── подсчёт ──────────────────────────────────────────────────────────
    def area_score(self) -> dict:
        """Подсчёт по площади (китайские правила): камни + окружённые
        пустые области + коми белым. Возвращает разложенный результат.

        Пустая область засчитывается цвету, только если её граница —
        камни ИСКЛЮЧИТЕЛЬНО этого цвета (нейтральная «сэки»/дама обоим
        не достаётся)."""
        black = 0
        white = 0
        visited: set = set()
        for y in range(self.size):
            for x in range(self.size):
                v = self.board[self._idx(x, y)]
                if v == BLACK:
                    black += 1
                elif v == WHITE:
                    white += 1
                elif (x, y) not in visited:
                    # flood по пустой области + какие цвета по границе
                    region = []
                    stack = [(x, y)]
                    visited.add((x, y))
                    borders = set()
                    while stack:
                        cx, cy = stack.pop()
                        region.append((cx, cy))
                        for nx, ny in self._neighbors(cx, cy):
                            nv = self.board[self._idx(nx, ny)]
                            if nv == EMPTY:
                                if (nx, ny) not in visited:
                                    visited.add((nx, ny))
                                    stack.append((nx, ny))
                            else:
                                borders.add(nv)
                    if borders == {BLACK}:
                        black += len(region)
                    elif borders == {WHITE}:
                        white += len(region)
                    # границы обоих цветов (или пустая доска) — никому
        wscore = white + self.komi
        return {
            "black": black,
            "white": white,
            "komi": self.komi,
            "white_with_komi": wscore,
            "winner": ("black" if black > wscore
                       else "white" if wscore > black else "draw"),
            "margin": abs(black - wscore),
        }

    def to_move(self) -> int:
        """Чей ход: чёрные начинают, каждый ход/пас меняет сторону."""
        return BLACK if self.move_number % 2 == 0 else WHITE

    def snapshot(self) -> dict:
        """Сериализация для клиента (Qt/net): доска строкой + факты."""
        rows = []
        for y in range(self.size):
            rows.append("".join(".XO"[self.board[self._idx(x, y)]]
                                for x in range(self.size)))
        return {
            "size": self.size,
            "rows": rows,
            "captured_black": self.captured[BLACK],   # взято чёрными
            "captured_white": self.captured[WHITE],
            "to_move": "black" if self.to_move() == BLACK else "white",
            "ko_point": self.ko_point,
            "last_move": self.last_move,
            "game_over": self.game_over,
            "moves": self.move_number,
        }

    def clone(self) -> GoEngine:
        """Независимая копия (ИИ примеряет ходы на копии, доска игрока
        не трогается)."""
        e = GoEngine(self.size, self.komi)
        e.board = list(self.board)
        e.captured = list(self.captured)
        e.ko_point = self.ko_point
        e.passes_in_row = self.passes_in_row
        e.move_number = self.move_number
        e.last_move = self.last_move
        e.game_over = self.game_over
        return e


# ── ИИ (эвристический — для игры «против компьютера») ──────────────────────
#
# Полноценный сильный ИИ для Го — открытая научная проблема (до AlphaGo
# MCTS делали годами), поэтому честная цель здесь: «живой, не тупой» соперник
# для дружеских партий. Эвристики по приоритету:
#   1. ВЗЯТЬ: ход снимает камни противника — почти всегда хорош;
#   2. СПАСТИСЬ: своя группа в атари (1 свобода) — добавь свободы;
#   3. АТАРИ: поставь группу врага в атари (без собственного самостава);
#   4. ФОРМА: 3-4 линия в дебюте/середине, ближе к центру на 9×9;
#   5. не заполняй собственные глаза (все соседи свои + свобода одна).
# among равных — лёгкий шум, чтобы партии не повторялись.


class GoAI:
    """Эвристический ИИ: выбирает вершину для хода (или пас)."""

    NAME = "Каменный друг"

    @classmethod
    def choose_move(cls, engine: GoEngine, color: int) -> tuple[int, int] | None:
        """None = лучше пас (нет осмысленных ходов)."""
        candidates: list[tuple[float, int, int]] = []
        enemy = WHITE if color == BLACK else BLACK
        size = engine.size

        for y in range(size):
            for x in range(size):
                if engine.board[engine._idx(x, y)] != EMPTY:
                    continue
                if not engine.is_legal(x, y, color):
                    continue
                score = cls._rate(engine, x, y, color, enemy)
                if score > float("-inf"):
                    candidates.append((score, x, y))

        if not candidates:
            return None
        # пас осмыслен только когда всё плохо (нет позитивных ходов)
        best = max(candidates)[0]
        if best <= 0.0 and engine.move_number > size * size // 2:
            return None
        # среди верхнего эшелона — случайный, партии не клонируются
        top = [c for c in candidates if c[0] >= best - 1.5]
        _, bx, by = random.choice(top)
        return bx, by

    @classmethod
    def _rate(cls, engine: GoEngine, x: int, y: int, color: int, enemy: int) -> float:
        """Оценка вершины; -inf — откровенно вредные ходы (заполнить глаз)."""
        score = 0.0
        # примерка: что изменится после этого хода
        probe = engine.clone()
        try:
            captured = probe.play(x, y, color)
        except GoError:
            return float("-inf")

        if captured:
            score += 24.0 * len(captured)          # взятие — весомый плюс

        stones, libs = probe._group_on(probe.board, x, y)
        if len(libs) <= 1:
            # самостав: ход дал свою группу в атари (или самоубийство уже
            # отфильтровано) — допускаем, только если это размен с взятием
            score -= 18.0 if not captured else 4.0

        # спасение своих групп в атари: считаем атари ДО хода
        my_atari_before = cls._atari_neighbors(engine, x, y, color)
        if my_atari_before and len(libs) >= 2:
            score += 20.0 * len(my_atari_before)

        # атари врагу (без самостава — проверено выше)
        score += 6.0 * len(cls._atari_neighbors(probe, x, y, enemy))

        # лёгкая «форма»: линия и центр
        line = min(x, y, engine.size - 1 - x, engine.size - 1 - y)
        score += (0.0, 1.0, 6.0, 5.0, 3.0)[min(line, 4)]
        if engine.size == 9:
            # на малой доске центр важнее
            cx = abs(x - 4) + abs(y - 4)
            score += max(0, 4 - cx)
        # немного шума в порядке сверху
        score += random.random()
        return score

    @staticmethod
    def _atari_neighbors(engine: GoEngine, x: int, y: int, color: int) -> set:
        """Группы ЦВЕТА color рядом с (x, y), имеющие ровно одну свободу
        (на доске engine; для «спасения» смотрим до хода, для «атари» —
        передают уже применённый probe)."""
        res = set()
        v = engine.board[engine._idx(x, y)]
        if v != EMPTY:
            stones, libs = engine.group_and_liberties(x, y)
            if len(libs) == 1:
                return stones
            return res
        for nx, ny in engine._neighbors(x, y):
            if engine.board[engine._idx(nx, ny)] == color:
                stones, libs = engine.group_and_liberties(nx, ny)
                if len(libs) == 1:
                    res.update(stones)
        return res
