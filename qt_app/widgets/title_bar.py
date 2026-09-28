"""TitleBar и фреймлесс-хелперы — ТЗ docs/DESIGN_STYLE.md, раздел 4 («Window Chrome»).

«Frameless but native-respecting. Custom titlebar on Windows/Linux;
respects macOS traffic lights».

Что здесь реализовано:
  - TitleBar: drag-зона, двойной клик = maximize, стеклянные кнопки
    минус/макс/клоуз (клоуз на hover — danger-цвет);
  - Windows: MainWindow возвращает HTCAPTION/HT*-коды из WM_NCHITTEST —
    нативный drag, ресайз со всех краёв, Aero Snap и Snap Layouts работают
    как у обычного окна (требование раздела 4 про Snap Layouts);
  - Linux/X11 и offscreen: ресайз через прозрачные краевые грипсы
    (startSystemResize), drag через startSystemMove — кроссплатформенные
    Qt-API, без ctypes;
  - macOS/wayland: frameless НЕ включаем — остаются нативные декорации
    (на macOS трейк-лайты, на wayland декорации обязательны композитором).

Всё, что связано с платформой, обёрнуто в try/except и не может уронить
приложение: при любом сбое окно просто остаётся с нативной рамкой.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QWidget,
)

from qt_app.icons import SvgIconWidget
from qt_app.theme import PALETTE, RADIUS

TITLEBAR_H = 42

# Windows hit-test коды (winuser.h)
_HTCLIENT = 1
_HTCAPTION = 2
_HTMINBUTTON = 8
_HTMAXBUTTON = 9
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_RESIZE_BORDER = 6  # px активной кромки


def platform_supports_frameless() -> bool:
    """Frameless делаем только там, где сами потянем resize/drag."""
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.platformName() in ("windows", "xcb", "offscreen")


class TitleBar(QWidget):
    """Кастомный тайтл-бар: заголовок слева, стеклянные кнопки справа.

    Drag мышью делегируем системе (startSystemMove) — так работают
    Aero Snap и тайлинг. Двойной клик — toggle maximize.
    """

    minimize_clicked = Signal()
    maximize_toggled = Signal()
    close_clicked = Signal()

    def __init__(self, window: QMainWindow, parent=None):
        super().__init__(parent)
        self._window = window
        self.setFixedHeight(TITLEBAR_H)
        self.setObjectName("TitleBar")
        self.setStyleSheet(
            f"""
            #TitleBar {{
                background: {PALETTE.surface_1};
                border-bottom: 1px solid {PALETTE.border_soft};
            }}
        """
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 8, 0)
        row.setSpacing(6)

        self._title = QLabel(window.windowTitle())
        self._title.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 13px; font-weight: 600;"
        )
        row.addWidget(self._title)
        row.addStretch(1)

        self._btn_min = self._chrome_btn("minimize", lambda: self.minimize_clicked.emit())
        self._btn_max = self._chrome_btn(
            "maximize", lambda: self.maximize_toggled.emit(), tooltip="Развернуть (двойной клик по шапке)"
        )
        self._btn_close = self._chrome_btn("x", lambda: self.close_clicked.emit(), danger=True)
        for btn in (self._btn_min, self._btn_max, self._btn_close):
            row.addWidget(btn)

    # ------------------------------------------------------------------
    def _chrome_btn(self, icon_name: str, handler, tooltip: str = "", danger: bool = False) -> QPushButton:
        """Создаёт кнопку с SVG иконкой вместо текста."""
        btn = QPushButton("")
        btn.setFixedSize(34, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip or icon_name)

        # SVG иконка как дочерний виджет
        icon = SvgIconWidget(icon_name, size=18, parent=btn)
        icon.move((34 - 18) // 2, (28 - 18) // 2)

        # Ховер-эффект через стиль
        hover_bg = PALETTE.danger if danger else PALETTE.glass_bg_hover
        hover_fg = "#FFFFFF" if danger else PALETTE.text_primary
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: {PALETTE.text_secondary};
                border: none;
                border-radius: {RADIUS}px;
            }}
            QPushButton:hover {{ background: {hover_bg}; color: {hover_fg}; }}
            QPushButton:pressed {{ background: {PALETTE.surface_3}; }}
        """
        )
        btn.clicked.connect(handler)
        return btn

    def update_max_icon(self) -> None:
        """Обновляет иконку кнопки maximize/restore (пока оставляем одинаковую)."""
        # Временно оставляем одну иконку для упрощения

    def over_max_button(self, global_x: int, global_y: int) -> bool:
        """Для WM_NCHITTEST: HTMAXBUTTON под кнопкой maximize включает
        Snap Layouts flyout в Windows 11."""
        try:
            top_left = self._btn_max.mapToGlobal(self._btn_max.rect().topLeft())
            return self._btn_max.rect().contains(global_x - top_left.x(), global_y - top_left.y())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Drag / двойной клик
    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._window.isMaximized():
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_toggled.emit()
        super().mouseDoubleClickEvent(event)


class EdgeResizeGrips(QObject):
    """Прозрачные кромки-грипсы для frameless на Linux/offscreen.

    На Windows ресайз делает WM_NCHITTEST, здесь они не нужны (но и не
    мешают — второй hit-test не конфликтует). На максимизированном окне
    скрываются.
    """

    def __init__(self, window: QMainWindow, parent=None):
        super().__init__(parent)
        self._window = window
        self._thickness = _RESIZE_BORDER
        self._edges: dict[str, _EdgeGrip] = {}
        for name, edges in (
            ("left", Qt.Edge.LeftEdge),
            ("right", Qt.Edge.RightEdge),
            ("top", Qt.Edge.TopEdge),
            ("bottom", Qt.Edge.BottomEdge),
            ("topleft", Qt.Edge.TopEdge | Qt.Edge.LeftEdge),
            ("topright", Qt.Edge.TopEdge | Qt.Edge.RightEdge),
            ("bottomleft", Qt.Edge.BottomEdge | Qt.Edge.LeftEdge),
            ("bottomright", Qt.Edge.BottomEdge | Qt.Edge.RightEdge),
        ):
            grip = _EdgeGrip(window, edges)
            self._edges[name] = grip
        self.reposition()

    def reposition(self) -> None:
        w, t = self._window.width(), self._thickness
        h = self._window.height()
        pos = {
            "left": (0, 0, t, h),
            "right": (w - t, 0, t, h),
            "top": (0, 0, w, t),
            "bottom": (0, h - t, w, t),
            "topleft": (0, 0, t, t),
            "topright": (w - t, 0, t, t),
            "bottomleft": (0, h - t, t, t),
            "bottomright": (w - t, h - t, t, t),
        }
        hidden = self._window.isMaximized()
        for name, grip in self._edges.items():
            grip.setGeometry(*pos[name])
            grip.setVisible(not hidden)
            grip.raise_()


class _EdgeGrip(QWidget):
    """Невидимая кромка: press → startSystemResize с нужными краями."""

    def __init__(self, window: QMainWindow, edges: Qt.Edge):
        super().__init__(window)
        self._window = window
        self._edges = edges
        self.setCursor(self._cursor_for(edges))

    @staticmethod
    def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape:
        has_h = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        has_v = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if has_h and has_v:
            if bool(edges & Qt.Edge.LeftEdge) != bool(edges & Qt.Edge.TopEdge):
                return Qt.CursorShape.SizeBDiagCursor
            return Qt.CursorShape.SizeFDiagCursor
        if has_h:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemResize(self._edges)
        super().mousePressEvent(event)


class WindowsHitTester:
    """WM_NCHITTEST → HT*-коды для нативного поведения на Windows."""

    WM_NCHITTEST = 0x0084

    @classmethod
    def handle(cls, window: QMainWindow, titlebar: TitleBar | None, message: int) -> int | None:
        """Возвращает HT*-код, если хотим перехватить хит-тест, иначе None."""
        try:
            import ctypes
            import ctypes.wintypes

            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message != cls.WM_NCHITTEST:
                return None

            # Координаты курсора (экранные) приходят в lParam (signed 16+16)
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value

            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(msg.hwnd, ctypes.byref(rect))
            # v1.9.5 HiDPI: GetWindowRect/lParam — ФИЗИЧЕСКИЕ пиксели, а
            # window.width()/height() — логические. При масштабе 125-150%
            # правая кромка (px >= w - 6) захватывала до трети окна (клики
            # в клиентской области превращались в resize), а mapFromGlobal
            # считал со сдвигом → HTCAPTION/HTMAXBUTTON мимо. Переводим
            # курсор в логические координаты ДО всех сравнений.
            dpr = float(window.devicePixelRatioF()) or 1.0
            px = (x - rect.left) / dpr
            py = (y - rect.top) / dpr
            w, h = window.width(), window.height()

            # Кромки — нативный ресайз (края приоритетнее углов)
            left = px < _RESIZE_BORDER
            right = px >= w - _RESIZE_BORDER
            top = py < _RESIZE_BORDER
            bottom = py >= h - _RESIZE_BORDER
            if top and left:
                return _HTTOPLEFT
            if top and right:
                return _HTTOPRIGHT
            if bottom and left:
                return _HTBOTTOMLEFT
            if bottom and right:
                return _HTBOTTOMRIGHT
            if left:
                return _HTLEFT
            if right:
                return _HTRIGHT
            if top:
                return _HTTOP
            if bottom:
                return _HTBOTTOM

            # Кнопки тайтл-бара: maximize → HTMAXBUTTON (Snap Layouts flyout)
            if titlebar is not None:
                # курсор уже в логических координатах (см. dpr выше) —
                # и в mapFromGlobal подаём логическую точку
                if titlebar.over_max_button(int(px), int(py)):
                    return _HTMAXBUTTON
                # Шапка (кроме дочерних кнопок) → drag/Snap
                local = titlebar.mapFromGlobal(QPoint(int(px), int(py)))
                if 0 <= local.y() <= titlebar.height():
                    child = titlebar.childAt(local)
                    if child is None or child not in (
                        titlebar._btn_min, titlebar._btn_max, titlebar._btn_close
                    ):
                        return _HTCAPTION
            return None
        except Exception:
            return None


def apply_dwm_shadow(window: QMainWindow) -> None:
    """Aero-тень для frameless на Windows: DwmExtendFrameIntoClientArea
    с крошечными полями. Молча пропускается на других ОС/при ошибке."""
    try:
        import ctypes
        import ctypes.wintypes
        import sys as _sys

        from PySide6.QtGui import QGuiApplication

        if _sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return

        class _MARGINS(ctypes.Structure):
            _fields_ = [
                ("cxLeftWidth", ctypes.c_int),
                ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int),
                ("cyBottomHeight", ctypes.c_int),
            ]

        hwnd = ctypes.wintypes.HWND(int(window.winId()))
        margins = _MARGINS(1, 1, 1, 1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except Exception:
        pass
