"""Рендер шахматных фигур — три скина, отделены от виджета доски.

Вынесено из ChessBoardWidget в рамках декомпозиции (у виджета было 7
ответственностей, рендер фигур — самая толстая из них, ~450 строк).

Доступные скины (порядок = порядок кнопок в UI):
  • standard — крупный Unicode-глиф ♔♕♖♗♘♙ с обводкой и тенью
  • gothic   — острые угловатые фигуры через QPolygonF («резное дерево»)
  • square   — блочные силуэты из прямоугольников (8-бит)

Использование:
    from qt_app.widgets.chess_piece_renderer import ChessPieceRenderer
    ChessPieceRenderer.draw(painter, skin, x, y, cell_size, piece, elevated=False)
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF, QRadialGradient

PIECE_UNICODE = {
    'K': '♔', 'Q': '♕', 'R': '♖', 'B': '♗', 'N': '♘', 'P': '♙',
    'k': '♚', 'q': '♛', 'r': '♜', 'b': '♝', 'n': '♞', 'p': '♟',
}

PIECE_SKINS = ["standard", "gothic", "square"]
PIECE_SKIN_LABELS = {
    "standard": "Стандарт",
    "gothic": "Готика",
    "square": "Квадрат",
}
PIECE_SKIN_DEFAULT = "standard"


class ChessPieceRenderer:
    """Диспетчер отрисовки фигур по скинам. Все методы статические —
    рендер не зависит от состояния виджета (только от параметров вызова)."""

    @classmethod
    def draw(cls, p: QPainter, skin: str, x: int, y: int, size: int,
             piece: str, elevated: bool = False) -> None:
        """Выбирает метод отрисовки в зависимости от скина."""
        if skin == "gothic":
            cls.draw_gothic(p, x, y, size, piece, elevated)
        elif skin == "square":
            cls.draw_square(p, x, y, size, piece, elevated)
        else:
            cls.draw_standard(p, x, y, size, piece, elevated)

    # ------------------------------------------------------------------
    # Скин 1: STANDARD — крупный Unicode-глиф, без круга, с обводкой
    # ------------------------------------------------------------------
    @staticmethod
    def draw_standard(p: QPainter, x: int, y: int, size: int, piece: str, elevated: bool = False):
        """Классические Staunton-силуэты из Unicode (♔♕♖♗♘♙) — БЕЗ круга.

        Универсальный шрифтовой глиф выводится крупно (≈85% клетки), с мягкой
        тенью под основанием и контрастной обводкой по периметру для чёткого
        разделения белых и чёрных фигур на любой клетке доски.
        """
        cx = x + size // 2
        is_white = piece.isupper()

        # 1) Мягкая тень-эллипс под основанием фигуры.
        shadow_offset = 6 if elevated else 3
        sg = QRadialGradient(cx, y + size - 4 + shadow_offset, size // 2)
        sg.setColorAt(0, QColor(0, 0, 0, 130 if elevated else 85))
        sg.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(sg)
        p.setPen(Qt.NoPen)
        p.drawEllipse(x + 6, y + size - 10 + shadow_offset, size - 12, 12)

        # 2) Глиф фигуры крупно. Пробуем несколько шрифтов с символами фигур.
        glyph_font = QFont()
        glyph_font.setFamilies([
            "Noto Sans Symbols 2",
            "Noto Sans Symbols",
            "Segoe UI Symbol",
            "Apple Symbols",
            "DejaVu Sans",
        ])
        glyph_font.setPixelSize(int(size * 0.82))
        p.setFont(glyph_font)
        text = PIECE_UNICODE.get(piece, '?')
        fm = QFontMetrics(p.font())
        tw = fm.horizontalAdvance(text)
        th = fm.ascent()
        tx = x + (size - tw) // 2
        ty = y + (size + th) // 2 - 4

        # 3) Обводка по периметру глифа (8-кратное смещение) — без неё белые
        #    фигуры на светлой клетке и чёрные на тёмной читаются плохо.
        outline = QColor(15, 15, 20, 220) if is_white else QColor(245, 240, 225, 200)
        p.setPen(outline)
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (1, 1), (-1, 1), (1, -1)]:
            p.drawText(tx + dx, ty + dy, text)

        # 4) Заливка глифа.
        if is_white:
            p.setPen(QColor(252, 248, 235))
        else:
            p.setPen(QColor(28, 26, 32))
        p.drawText(tx, ty, text)

    # ------------------------------------------------------------------
    # Скин 2: GOTHIC — острые угловатые фигуры через QPolygonF
    # ------------------------------------------------------------------
    @staticmethod
    def draw_gothic(p: QPainter, x: int, y: int, size: int, piece: str, elevated: bool = False):
        """Готический скин: фигуры как средневековые резные деревянные фишки.

        Каждый тип фигуры строится из многоугольников с острыми углами:
        пешка — ромб на трапеции, ладья — прямоугольник с зубцами,
        конь — угловатая Г-образная голова, слон — высокий шпиль со щелью,
        ферзь — тело с пятью шипами-короной, король — тело с крестом.
        """
        is_white = piece.isupper()
        # Базовые цвета для готического стиля — более насыщенные, «резное дерево».
        if is_white:
            body_fill = QColor(245, 232, 200)
            body_edge = QColor(120, 88, 45)
            accent = QColor(80, 55, 25)
        else:
            body_fill = QColor(38, 34, 42)
            body_edge = QColor(15, 12, 18)
            accent = QColor(180, 165, 140)

        # Мягкая тень под основанием
        cx = x + size // 2
        shadow_offset = 6 if elevated else 3
        sg = QRadialGradient(cx, y + size - 4 + shadow_offset, size // 2)
        sg.setColorAt(0, QColor(0, 0, 0, 130 if elevated else 85))
        sg.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(sg)
        p.setPen(Qt.NoPen)
        p.drawEllipse(x + 6, y + size - 10 + shadow_offset, size - 12, 12)

        # Подготовка кисти/пера для тела фигуры
        p.setBrush(body_fill)
        p.setPen(QPen(body_edge, 1.4))

        pu = piece.upper()
        s = size  # alias для краткости
        # Все координаты — относительно (x, y) клетки, единицы = пиксели.

        if pu == 'P':
            # Пешка: ромб-голова + шея + трапеция-тело + основание
            head = QPolygonF([
                QPointF(x + 0.50*s, y + 0.18*s),
                QPointF(x + 0.62*s, y + 0.30*s),
                QPointF(x + 0.50*s, y + 0.42*s),
                QPointF(x + 0.38*s, y + 0.30*s),
            ])
            p.drawPolygon(head)
            neck = QPolygonF([
                QPointF(x + 0.44*s, y + 0.42*s),
                QPointF(x + 0.56*s, y + 0.42*s),
                QPointF(x + 0.58*s, y + 0.50*s),
                QPointF(x + 0.42*s, y + 0.50*s),
            ])
            p.drawPolygon(neck)
            body = QPolygonF([
                QPointF(x + 0.34*s, y + 0.50*s),
                QPointF(x + 0.66*s, y + 0.50*s),
                QPointF(x + 0.72*s, y + 0.74*s),
                QPointF(x + 0.28*s, y + 0.74*s),
            ])
            p.drawPolygon(body)
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

        elif pu == 'R':
            # Ладья: 3 зубца-кренуляции сверху + тело + основание
            # Зубцы (коронка)
            for cx_off in (0.30, 0.50, 0.70):
                cx_px = x + cx_off * s
                merlon = QPolygonF([
                    QPointF(cx_px - 0.07*s, y + 0.18*s),
                    QPointF(cx_px + 0.07*s, y + 0.18*s),
                    QPointF(cx_px + 0.07*s, y + 0.30*s),
                    QPointF(cx_px - 0.07*s, y + 0.30*s),
                ])
                p.drawPolygon(merlon)
            # Полка под зубцами
            p.drawRect(QRectF(x + 0.26*s, y + 0.28*s, 0.48*s, 0.05*s))
            # Корпус — трапеция
            body = QPolygonF([
                QPointF(x + 0.34*s, y + 0.33*s),
                QPointF(x + 0.66*s, y + 0.33*s),
                QPointF(x + 0.70*s, y + 0.74*s),
                QPointF(x + 0.30*s, y + 0.74*s),
            ])
            p.drawPolygon(body)
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

        elif pu == 'N':
            # Конь: угловатая Г-образная голова лошади + тело + основание
            # Голова: острый нос вправо-вверх, изгиб назад
            head = QPolygonF([
                QPointF(x + 0.32*s, y + 0.20*s),  # макушка
                QPointF(x + 0.50*s, y + 0.18*s),  # темя
                QPointF(x + 0.70*s, y + 0.28*s),  # нос
                QPointF(x + 0.66*s, y + 0.42*s),  # нижняя челюсть
                QPointF(x + 0.50*s, y + 0.40*s),  # горло
                QPointF(x + 0.44*s, y + 0.34*s),  # скула
                QPointF(x + 0.36*s, y + 0.36*s),  # затылок
            ])
            p.drawPolygon(head)
            # Ухо — острый треугольник над макушкой
            ear = QPolygonF([
                QPointF(x + 0.38*s, y + 0.20*s),
                QPointF(x + 0.44*s, y + 0.10*s),
                QPointF(x + 0.48*s, y + 0.22*s),
            ])
            p.drawPolygon(ear)
            # Глаз — точка
            p.setBrush(accent)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x + 0.58*s, y + 0.30*s), 0.025*s, 0.025*s)
            p.setBrush(body_fill)
            p.setPen(QPen(body_edge, 1.4))
            # Шея — трапеция от головы к основанию
            neck = QPolygonF([
                QPointF(x + 0.34*s, y + 0.40*s),
                QPointF(x + 0.66*s, y + 0.44*s),
                QPointF(x + 0.68*s, y + 0.74*s),
                QPointF(x + 0.32*s, y + 0.74*s),
            ])
            p.drawPolygon(neck)
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

        elif pu == 'B':
            # Слон: острый шпиль сверху, тело, щель-разрез, основание
            spire = QPolygonF([
                QPointF(x + 0.50*s, y + 0.10*s),
                QPointF(x + 0.58*s, y + 0.28*s),
                QPointF(x + 0.42*s, y + 0.28*s),
            ])
            p.drawPolygon(spire)
            # Шарик-яблочко под шпилем
            p.drawEllipse(QPointF(x + 0.50*s, y + 0.32*s), 0.06*s, 0.05*s)
            # Тело — высокая трапеция
            body = QPolygonF([
                QPointF(x + 0.38*s, y + 0.38*s),
                QPointF(x + 0.62*s, y + 0.38*s),
                QPointF(x + 0.70*s, y + 0.74*s),
                QPointF(x + 0.30*s, y + 0.74*s),
            ])
            p.drawPolygon(body)
            # Щель-разрез (характерная деталь слона)
            p.setPen(QPen(accent, 2))
            p.drawLine(QPointF(x + 0.50*s, y + 0.42*s), QPointF(x + 0.50*s, y + 0.55*s))
            p.setPen(QPen(body_edge, 1.4))
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

        elif pu == 'Q':
            # Ферзь: 5 острых шипов-короны + тело + основание
            # Шипы короны
            spike_tops = [0.22, 0.36, 0.50, 0.64, 0.78]
            crown_pts = [QPointF(x + spike_tops[0]*s, y + 0.32*s)]
            for i, t in enumerate(spike_tops):
                crown_pts.append(QPointF(x + t*s, y + 0.14*s))  # остриё
                # впадина между шипами
                if i < len(spike_tops) - 1:
                    mid = (t + spike_tops[i + 1]) / 2
                    crown_pts.append(QPointF(x + mid*s, y + 0.26*s))
            crown_pts.append(QPointF(x + spike_tops[-1]*s, y + 0.32*s))
            p.drawPolygon(QPolygonF(crown_pts))
            # Корпус
            body = QPolygonF([
                QPointF(x + 0.34*s, y + 0.32*s),
                QPointF(x + 0.66*s, y + 0.32*s),
                QPointF(x + 0.72*s, y + 0.74*s),
                QPointF(x + 0.28*s, y + 0.74*s),
            ])
            p.drawPolygon(body)
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

        elif pu == 'K':
            # Король: крест на верхушке + корона + тело + основание
            # Крест
            p.drawRect(QRectF(x + 0.47*s, y + 0.08*s, 0.06*s, 0.14*s))  # вертикаль
            p.drawRect(QRectF(x + 0.42*s, y + 0.13*s, 0.16*s, 0.05*s))  # горизонталь
            # Корона-основание под крестом
            crown = QPolygonF([
                QPointF(x + 0.40*s, y + 0.22*s),
                QPointF(x + 0.60*s, y + 0.22*s),
                QPointF(x + 0.58*s, y + 0.32*s),
                QPointF(x + 0.42*s, y + 0.32*s),
            ])
            p.drawPolygon(crown)
            # Корпус
            body = QPolygonF([
                QPointF(x + 0.34*s, y + 0.32*s),
                QPointF(x + 0.66*s, y + 0.32*s),
                QPointF(x + 0.72*s, y + 0.74*s),
                QPointF(x + 0.28*s, y + 0.74*s),
            ])
            p.drawPolygon(body)
            base = QPolygonF([
                QPointF(x + 0.22*s, y + 0.74*s),
                QPointF(x + 0.78*s, y + 0.74*s),
                QPointF(x + 0.82*s, y + 0.86*s),
                QPointF(x + 0.18*s, y + 0.86*s),
            ])
            p.drawPolygon(base)

    # ------------------------------------------------------------------
    # Скин 3: SQUARE — блочные силуэты из прямоугольников
    # ------------------------------------------------------------------
    @staticmethod
    def draw_square(p: QPainter, x: int, y: int, size: int, piece: str, elevated: bool = False):
        """Квадратный (пиксельный) скин: каждая фигура собрана из прямоугольников.

        Чёткая блочная эстетика в духе 8-битных игр: силуэты читаются
        мгновенно за счёт крупных прямоугольных деталей. Подходит для
        любителей ретро-стиля.
        """
        is_white = piece.isupper()
        if is_white:
            body_fill = QColor(248, 240, 220)
            body_edge = QColor(80, 60, 35)
            accent = QColor(60, 45, 25)
        else:
            body_fill = QColor(35, 32, 40)
            body_edge = QColor(0, 0, 0)
            accent = QColor(225, 220, 200)

        # Мягкая квадратная тень под основанием
        shadow_offset = 6 if elevated else 3
        p.setBrush(QColor(0, 0, 0, 110 if elevated else 70))
        p.setPen(Qt.NoPen)
        p.drawRect(x + 6, y + int(size * 0.86) + shadow_offset, size - 12, 6)

        p.setBrush(body_fill)
        p.setPen(QPen(body_edge, 1.5))

        pu = piece.upper()
        s = size

        def rect(fx, fy, fw, fh):
            """Рисует прямоугольник в относительных координатах клетки."""
            p.drawRect(QRectF(x + fx*s, y + fy*s, fw*s, fh*s))

        if pu == 'P':
            # Пешка: голова-квадрат + шея + тело-трапеция (через 3 прям.) + основание
            rect(0.42, 0.18, 0.16, 0.14)  # голова
            rect(0.40, 0.32, 0.20, 0.05)  # шея
            rect(0.36, 0.37, 0.28, 0.30)  # тело
            rect(0.30, 0.67, 0.40, 0.07)  # нижняя часть тела
            rect(0.22, 0.74, 0.56, 0.10)  # основание

        elif pu == 'R':
            # Ладья: 3 зубца + перекладина + тело + основание
            for cx_off in (0.28, 0.44, 0.60):
                rect(cx_off, 0.16, 0.12, 0.14)  # зубцы-кренуляции
            # 4-й зубец смещён вправо
            rect(0.60, 0.16, 0.12, 0.14)
            rect(0.26, 0.30, 0.48, 0.06)  # полка под зубцами
            rect(0.32, 0.36, 0.36, 0.38)  # тело
            rect(0.22, 0.74, 0.56, 0.10)  # основание

        elif pu == 'N':
            # Конь: Г-образная голова + тело + основание
            # Голова (длинная, вправо)
            rect(0.36, 0.18, 0.36, 0.14)  # верх головы
            rect(0.58, 0.32, 0.14, 0.10)  # нос
            rect(0.32, 0.32, 0.28, 0.10)  # затылок
            rect(0.36, 0.42, 0.36, 0.06)  # шея
            rect(0.34, 0.48, 0.40, 0.26)  # тело
            rect(0.22, 0.74, 0.56, 0.10)  # основание
            # Глаз — маленький квадратик акцентного цвета
            p.setBrush(accent)
            p.setPen(Qt.NoPen)
            rect(0.62, 0.24, 0.05, 0.05)
            p.setBrush(body_fill)
            p.setPen(QPen(body_edge, 1.5))

        elif pu == 'B':
            # Слон: шпиль + тело + основание, со щелью
            rect(0.46, 0.12, 0.08, 0.08)  # верхушка-шпиль
            rect(0.42, 0.20, 0.16, 0.06)  # яблоко
            rect(0.38, 0.26, 0.24, 0.10)  # верх тела
            rect(0.34, 0.36, 0.32, 0.20)  # середина
            rect(0.30, 0.56, 0.40, 0.18)  # низ тела
            rect(0.22, 0.74, 0.56, 0.10)  # основание
            # Щель-разрез
            p.setBrush(accent)
            p.setPen(Qt.NoPen)
            rect(0.48, 0.42, 0.04, 0.12)
            p.setBrush(body_fill)
            p.setPen(QPen(body_edge, 1.5))

        elif pu == 'Q':
            # Ферзь: корона из 5 квадратиков + тело + основание
            for cx_off in (0.24, 0.36, 0.48, 0.60, 0.72):
                rect(cx_off, 0.14, 0.08, 0.10)  # зубцы короны
            rect(0.22, 0.24, 0.60, 0.08)  # полка короны
            rect(0.32, 0.32, 0.40, 0.10)  # верх тела
            rect(0.30, 0.42, 0.44, 0.20)  # середина
            rect(0.26, 0.62, 0.52, 0.12)  # низ тела
            rect(0.22, 0.74, 0.56, 0.10)  # основание

        elif pu == 'K':
            # Король: крест + корона + тело + основание
            rect(0.47, 0.08, 0.06, 0.16)  # вертикаль креста
            rect(0.41, 0.12, 0.18, 0.06)  # горизонталь креста
            rect(0.36, 0.24, 0.30, 0.08)  # корона-основание
            rect(0.32, 0.32, 0.40, 0.10)  # верх тела
            rect(0.30, 0.42, 0.44, 0.20)  # середина
            rect(0.26, 0.62, 0.52, 0.12)  # низ тела
            rect(0.22, 0.74, 0.56, 0.10)  # основание
