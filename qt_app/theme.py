"""Friend Relay — Web Studio Design System для Qt-интерфейса.

Адаптация веб-стиля (web_client/style.css) под PySide6.
6 тем: Aurora, Lukewarm Ocean, Snow, Cherry Grove, Sakura, Crème Brûlée.

Принципы дизайна из веб-клиента:
• Один уверенный акцент (тёплый коралл #ff5b3a по умолчанию)
• Тёплая почти-чёрная база (#0c0d11 вместо цинкового)
• Типографика с характером (Inter/Segoe UI)
• Радиус 10/6/14 (меньше «AI-стандарта»)
• Глубина через 1px-границы, а не тени
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QEasingCurve


class ThemeName(StrEnum):
    """Доступные темы (как в веб-клиенте)."""
    AURORA = "aurora"
    LUKEWARM_OCEAN = "lukewarm-ocean"
    SNOW = "snow"
    CHERRY_GROVE = "cherry-grove"
    SAKURA = "sakura"
    CREME_BRULEE = "creme-brulee"


@dataclass(frozen=True)
class Palette:
    """Палитра одной темы. Все цвета из веб-клиента style.css.

    Поддерживает ОБА набора имён: новые (web-style) и старые (legacy).
    Новые имена приоритетны для нового кода, старые оставлены для совместимости.
    """

    # -- Новые имена (web-style) --
    # -- surface (тёплая, не цинковая) --
    bg: str = "#0c0d11"                  # самая глубокая, чуть тепла
    surface: str = "#14161c"             # панели/карточки
    surface2: str = "#1b1e26"            # лёгкий подъём: поля, ховеры
    surface3: str = "#22262f"            # активные поля, фокус-подложка

    # -- borders --
    border: str = "rgba(255,255,255,0.07)"
    border2: str = "rgba(255,255,255,0.13)"  # усиленная граница
    hover_chip: str = "rgba(255,255,255,0.05)"
    panel_soft: str = "rgba(255,255,255,0.025)"
    pop_border: str = "rgba(255,255,255,0.12)"

    # -- text --
    text: str = "#e9e7e2"                # тёплая, не чисто белая
    text_strong: str = "#ffffff"
    dim: str = "#9a9aa3"
    dim2: str = "#6b6b75"                # ещё тише — времена, метаданные

    # -- accent (один уверенный цвет, не градиент) --
    accent: str = "#ff5b3a"              # тёплый коралл — «дизайнерский»
    accent2: str = "#ff8a5a"             # для лёгких градиентов в редких местах
    accent_soft: str = "rgba(255,91,58,0.14)"
    accent_soft_strong: str = "rgba(255,91,58,0.4)"
    accent_border: str = "rgba(255,91,58,0.65)"

    # -- semantic --
    ok: str = "#4ade80"
    err: str = "#f87171"
    warn: str = "#fbbf24"

    # -- «себя» — однотонный, не градиент --
    me_bubble: str = "#ff5b3a"

    # -- shadows (редкие, только там, где реально висит) --
    shadow: str = "0 1px 2px rgba(0,0,0,0.3), 0 8px 28px rgba(0,0,0,0.45)"
    shadow_sm: str = "0 1px 2px rgba(0,0,0,0.22)"
    shadow_pop: str = "0 12px 38px rgba(0,0,0,0.55)"
    glow: str = "0 0 0 3px rgba(255,91,58,0.18)"
    btn_shadow: str = "none"
    logo_shadow: str = "0 4px 14px rgba(255,91,58,0.18)"
    thumb_shadow: str = "0 1px 4px rgba(255,91,58,0.4)"

    # -- scroll --
    scroll_thumb: str = "#2c3038"
    scroll_thumb_hover: str = "#3a3f4a"

    # -- special surfaces --
    header_bg: str = "rgba(12,13,17,0.86)"
    toast_bg: str = "rgba(20,22,28,0.96)"
    focus_bg: str = "#1c2029"
    sec_hover_bg: str = "#22262f"
    sec_hover_border: str = "rgba(255,255,255,0.16)"

    # -- Старые имена (legacy compatibility) --
    # Эти поля заполняются автоматически через __post_init__
    surface_0: str = ""  # = bg
    surface_1: str = ""  # = surface
    surface_2: str = ""  # = surface2
    surface_3: str = ""  # = surface3
    surface_4: str = ""  # = surface3
    glass_bg: str = ""  # = panel_soft
    glass_bg_hover: str = ""  # = hover_chip
    glass_bg_active: str = ""  # = hover_chip
    border_soft: str = ""  # = border
    border_strong: str = ""  # = border2
    accent_hover: str = ""  # = accent2
    accent_press: str = ""  # = accent
    accent_glow: str = ""  # = glow
    accent_dim: str = ""  # = accent_soft
    text_primary: str = ""  # = text
    text_secondary: str = ""  # = dim
    text_dim: str = ""  # = dim2
    text_on_accent: str = ""  # = text_strong
    success: str = ""  # = ok
    danger: str = ""  # = err
    warning: str = ""  # = warn
    neon_green: str = "#39FF14"
    neon_pink: str = "#FF1744"

    def __post_init__(self):
        """Заполняем старые имена для обратной совместимости."""
        # Для плавного миграции старого кода
        object.__setattr__(self, 'surface_0', self.bg)
        object.__setattr__(self, 'surface_1', self.surface)
        object.__setattr__(self, 'surface_2', self.surface2)
        object.__setattr__(self, 'surface_3', self.surface3)
        object.__setattr__(self, 'surface_4', self.surface3)  # самый яркий
        object.__setattr__(self, 'glass_bg', self.panel_soft)
        object.__setattr__(self, 'glass_bg_hover', self.hover_chip)
        object.__setattr__(self, 'glass_bg_active', self.hover_chip)
        object.__setattr__(self, 'border_soft', self.border)
        object.__setattr__(self, 'border_strong', self.border2)
        object.__setattr__(self, 'accent_hover', self.accent2)
        object.__setattr__(self, 'accent_press', self.accent)
        object.__setattr__(self, 'accent_glow', self.glow)
        object.__setattr__(self, 'accent_dim', self.accent_soft)
        object.__setattr__(self, 'text_primary', self.text)
        object.__setattr__(self, 'text_secondary', self.dim)
        object.__setattr__(self, 'text_dim', self.dim2)
        object.__setattr__(self, 'text_on_accent', self.text_strong)
        object.__setattr__(self, 'success', self.ok)
        object.__setattr__(self, 'danger', self.err)
        object.__setattr__(self, 'warning', self.warn)


# Темы как в веб-клиенте
THEMES = {
    ThemeName.AURORA: Palette(
        bg="#0c0d11", surface="#14161c", surface2="#1b1e26", surface3="#22262f",
        border="rgba(255,255,255,0.07)", border2="rgba(255,255,255,0.13)",
        hover_chip="rgba(255,255,255,0.05)", panel_soft="rgba(255,255,255,0.025)",
        pop_border="rgba(255,255,255,0.12)",
        text="#e9e7e2", text_strong="#ffffff", dim="#9a9aa3", dim2="#6b6b75",
        accent="#ff5b3a", accent2="#ff8a5a",
        accent_soft="rgba(255,91,58,0.14)", accent_soft_strong="rgba(255,91,58,0.4)",
        accent_border="rgba(255,91,58,0.65)",
        ok="#4ade80", err="#f87171", warn="#fbbf24",
        me_bubble="#ff5b3a",
        scroll_thumb="#2c3038", scroll_thumb_hover="#3a3f4a",
        header_bg="rgba(12,13,17,0.86)", toast_bg="rgba(20,22,28,0.96)",
        focus_bg="#1c2029", sec_hover_bg="#22262f", sec_hover_border="rgba(255,255,255,0.16)",
    ),
    ThemeName.LUKEWARM_OCEAN: Palette(
        bg="#08151c", surface="#0e2230", surface2="#153244", surface3="#1c4256",
        border="rgba(170,225,235,0.08)", border2="rgba(170,225,235,0.16)",
        hover_chip="rgba(170,225,235,0.05)", panel_soft="rgba(170,225,235,0.025)",
        pop_border="rgba(170,225,235,0.14)",
        text="#eaf6f8", text_strong="#ffffff", dim="#9ec0c8", dim2="#6d8e98",
        accent="#34c5cd", accent2="#5ad3d8",
        accent_soft="rgba(52,197,205,0.14)", accent_soft_strong="rgba(52,197,205,0.4)",
        accent_border="rgba(52,197,205,0.65)",
        ok="#3ddc97", err="#ff7b72", warn="#f2c14e",
        me_bubble="#34c5cd",
        scroll_thumb="#1c4256", scroll_thumb_hover="#28566c",
        header_bg="rgba(8,21,28,0.86)", toast_bg="rgba(14,34,48,0.96)",
        focus_bg="#153244", sec_hover_bg="#1c4256", sec_hover_border="rgba(170,225,235,0.22)",
    ),
    ThemeName.SNOW: Palette(
        bg="#f6f7f9", surface="#ffffff", surface2="#eef0f4", surface3="#e3e7ed",
        border="rgba(15,23,42,0.08)", border2="rgba(15,23,42,0.16)",
        hover_chip="rgba(15,23,42,0.04)", panel_soft="rgba(15,23,42,0.025)",
        pop_border="rgba(15,23,42,0.14)",
        text="#1a1f2e", text_strong="#0a0e1a", dim="#525c72", dim2="#67718a",
        accent="#2563eb", accent2="#3b82f6",
        accent_soft="rgba(37,99,235,0.10)", accent_soft_strong="rgba(37,99,235,0.32)",
        accent_border="rgba(37,99,235,0.55)",
        ok="#059669", err="#dc2626", warn="#d97706",
        me_bubble="#2563eb",
        scroll_thumb="#cdd3de", scroll_thumb_hover="#b3bccd",
        header_bg="rgba(246,247,249,0.88)", toast_bg="rgba(255,255,255,0.97)",
        focus_bg="#eef0f4", sec_hover_bg="#e3e7ed", sec_hover_border="rgba(15,23,42,0.22)",
    ),
    ThemeName.CHERRY_GROVE: Palette(
        bg="#f6efe9", surface="#fdf7f1", surface2="#f3e7df", surface3="#ebd7cc",
        border="rgba(64,42,53,0.10)", border2="rgba(64,42,53,0.18)",
        hover_chip="rgba(64,42,53,0.06)", panel_soft="rgba(64,42,53,0.03)",
        pop_border="rgba(64,42,53,0.16)",
        text="#3d2a32", text_strong="#1f1418", dim="#6d4c59", dim2="#8a7079",
        accent="#c92a4f", accent2="#e0456a",
        accent_soft="rgba(201,42,79,0.10)", accent_soft_strong="rgba(201,42,79,0.32)",
        accent_border="rgba(201,42,79,0.55)",
        ok="#2f9e6e", err="#c92a4f", warn="#b7791f",
        me_bubble="#c92a4f",
        scroll_thumb="#d8c0b5", scroll_thumb_hover="#c4a599",
        header_bg="rgba(246,239,233,0.88)", toast_bg="rgba(253,247,241,0.97)",
        focus_bg="#f3e7df", sec_hover_bg="#ebd7cc", sec_hover_border="rgba(64,42,53,0.22)",
    ),
    ThemeName.SAKURA: Palette(
        bg="#faf1f4", surface="#fffafb", surface2="#f5e7ed", surface3="#ecdce5",
        border="rgba(74,32,46,0.10)", border2="rgba(74,32,46,0.18)",
        hover_chip="rgba(74,32,46,0.05)", panel_soft="rgba(74,32,46,0.03)",
        pop_border="rgba(74,32,46,0.16)",
        text="#432a35", text_strong="#231218", dim="#6e4c5a", dim2="#83687a",
        accent="#bf4470", accent2="#e0719c",
        accent_soft="rgba(191,68,112,0.10)", accent_soft_strong="rgba(191,68,112,0.32)",
        accent_border="rgba(191,68,112,0.55)",
        ok="#2f9e6e", err="#cc2b44", warn="#b7791f",
        me_bubble="#bf4470",
        scroll_thumb="#e0c3d1", scroll_thumb_hover="#cfa6ba",
        header_bg="rgba(250,241,244,0.88)", toast_bg="rgba(255,250,251,0.97)",
        focus_bg="#f5e7ed", sec_hover_bg="#ecdce5", sec_hover_border="rgba(74,32,46,0.22)",
    ),
    ThemeName.CREME_BRULEE: Palette(
        bg="#f8f2e3", surface="#fffbf0", surface2="#f1e6cf", surface3="#e7d9ba",
        border="rgba(74,52,24,0.12)", border2="rgba(74,52,24,0.20)",
        hover_chip="rgba(74,52,24,0.06)", panel_soft="rgba(74,52,24,0.035)",
        pop_border="rgba(74,52,24,0.17)",
        text="#3f2f1c", text_strong="#211505", dim="#6f5a3f", dim2="#7d6749",
        accent="#a85a15", accent2="#c98a3e",
        accent_soft="rgba(168,90,21,0.10)", accent_soft_strong="rgba(168,90,21,0.32)",
        accent_border="rgba(168,90,21,0.55)",
        ok="#2f8f5b", err="#c0392b", warn="#8f6410",
        me_bubble="#a85a15",
        scroll_thumb="#dcc99f", scroll_thumb_hover="#c8b181",
        header_bg="rgba(248,242,227,0.88)", toast_bg="rgba(255,251,240,0.97)",
        focus_bg="#f1e6cf", sec_hover_bg="#e7d9ba", sec_hover_border="rgba(74,52,24,0.24)",
    ),
}

# Текущая тема (по умолчанию Aurora)
_current_theme = ThemeName.AURORA

def get_current_theme() -> ThemeName:
    """Получить текущую тему."""
    return _current_theme


def set_theme(theme_name: ThemeName) -> None:
    """Установить тему по имени."""
    global PALETTE, _current_theme
    _current_theme = theme_name
    PALETTE = THEMES[theme_name]

# Инициализация палитры по умолчанию
PALETTE = THEMES[ThemeName.AURORA]

# Радиусы — из веб-стиля (намеренно меньше «AI-стандарта»)
RADIUS = 10          # основной
RADIUS_SM = 6        # мелкий для инпутов/баблов
RADIUS_LG = 14       # большой для карточек
RADIUS_XL = 16       # для диалогов
RADIUS_PILL = 999    # для pills

# Шрифты — из веб-стиля (Inter/Segoe UI stack)
FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI Variable', 'Segoe UI', Roboto, Ubuntu, system-ui, sans-serif"
FONT_FAMILY_MONO = "ui-monospace, 'SF Mono', 'JetBrains Mono', 'Cascadia Mono', Menlo, Consolas, monospace"
FONT_SIZE_CAPTION = 12
FONT_SIZE_BODY = 15   # как в веб-клиенте --font-size-base
FONT_SIZE_BODY_LG = 16
FONT_SIZE_HEADING = 18
FONT_SIZE_HEADING_LG = 20

# Анимации (длительность в мс), единый хронометраж всех переходов
DUR_FAST = 120
DUR_MESSAGE = 150        # появление нового сообщения (slide up 8px + fade)
DUR_CROSSFADE = 180      # равноправная навигация (OutCubic)
DUR_RAIL = 200           # раскрытие рейла (OutCubic)
DUR_SLIDE = 250          # drill-down slide (InOutQuart)
DUR_PUSH = 300           # вертикальный push оверлеев (OutBack)
DUR_POP = 160            # появление reaction-пикера (fade + подлёт OutBack)
DUR_NORMAL = DUR_CROSSFADE
DUR_SLOW = DUR_SLIDE

# Стаггер иконок в hover action bar бабла (каждая следующая +40мс)
STAGGER_STEP_MS = 40


# Эйзинги — единые для всех переходов
EASE_OUT_CUBIC = QEasingCurve.Type.OutCubic
EASE_IN_OUT_QUART = QEasingCurve.Type.InOutQuart
EASE_OUT_BACK = QEasingCurve.Type.OutBack


def out_back_overshoot(amplitude: float = 1.70158) -> QEasingCurve:
    """OutBack с настраиваемым «перелётом» (для живых переходов)."""
    curve = QEasingCurve()
    curve.setType(QEasingCurve.Type.OutBack)
    curve.setOvershoot(amplitude)
    return curve


def build_qss(p: Palette = PALETTE) -> str:
    """Генерирует QSS из палитры веб-стиля.

    Использует СТАРЫЕ имена полей (surface_0, text_primary и т.д.)
    для обратной совместимости. Новые имена (bg, surface, accent)
    тоже работают через __post_init__.
    """
    return f"""
    QWidget {{
        background: transparent;
        color: {p.text_primary};
        font-family: {FONT_FAMILY};
        font-size: {FONT_SIZE_BODY}px;
    }}

    QMainWindow, #RootSurface {{ background: {p.surface_0}; }}

    QToolTip {{
        background: {p.surface_2};
        color: {p.text_primary};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 6px 8px;
        font-size: 12px;
    }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: {p.border}; border-radius: 4px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.18); }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar::horizontal {{ height: 0px; }}

    QLineEdit, QTextEdit, QSpinBox {{
        background: {p.surface_3};
        border: 1px solid {p.border_soft};
        border-radius: {RADIUS}px;
        padding: 8px 12px;
        selection-background-color: {p.accent};
        selection-color: {p.text_on_accent};
        min-height: 20px;
    }}
    QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {{
        border: 1px solid {p.accent};
        background: {p.surface_4};
    }}
    QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled {{ opacity: 0.55; }}

    /* Кнопки в стиле веб-клиента — 1px границы, без теней по умолчанию */
    QPushButton {{
        background: {p.glass_bg};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 7px 14px;
        color: {p.text_primary};
        min-height: 18px;
    }}
    QPushButton:hover {{
        background: {p.glass_bg_hover};
        border-color: {p.border_strong};
    }}
    QPushButton:pressed {{
        background: {p.glass_bg_active};
        border-color: {p.border_strong};
    }}
    QPushButton:focus {{
        border: 1px solid {p.accent};
    }}
    QPushButton:default {{
        background: {p.accent_dim};
        border-color: {p.accent};
        color: {p.text_primary};
    }}
    QPushButton:default:hover {{ background: {p.accent_glow}; }}
    QPushButton:disabled {{ color: {p.text_dim}; border-color: {p.border_soft}; }}

    QComboBox {{
        background: {p.surface_3};
        border: 1px solid {p.border_soft};
        border-radius: {RADIUS_SM}px;
        padding: 5px 10px;
        color: {p.text_primary};
        min-height: 18px;
        font-size: {FONT_SIZE_CAPTION}px;
    }}
    QComboBox:hover {{ border-color: {p.border_strong}; }}
    QComboBox:focus {{ border: 1px solid {p.accent}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox::down-arrow {{
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {p.text_secondary};
        margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {p.surface_2};
        border: 1px solid {p.border};
        border-radius: {RADIUS_SM}px;
        color: {p.text_primary};
        selection-background-color: {p.accent_dim};
        selection-color: {p.text_primary};
        outline: none;
    }}

    QSlider {{
        min-height: 22px;
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        border-radius: 2px;
        background: {p.surface_4};
    }}
    QSlider::sub-page:horizontal {{
        background: {p.accent};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
        background: {p.text_primary};
    }}
    QSlider::handle:horizontal:hover {{ background: {p.accent_hover}; }}
    QSlider::handle:horizontal:pressed {{ background: {p.accent_press}; }}

    QCheckBox::indicator, QRadioButton::indicator {{
        width: 18px; height: 18px;
        border: 1px solid {p.border_strong};
        border-radius: 4px;
        background: {p.surface_3};
    }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {p.accent};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
        image: none;
    }}

    QCheckBox, QRadioButton {{ color: {p.text_primary}; spacing: 8px; min-height: 28px; }}

    QMenu {{
        background: {p.surface_2};
        color: {p.text_primary};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 6px;
    }}
    QMenu::item {{ padding: 8px 14px; border-radius: {RADIUS_SM}px; }}
    QMenu::item:selected {{ background: {p.surface_3}; }}
    QMenu::separator {{ height: 1px; background: {p.border_soft}; margin: 4px 8px; }}

    QLabel[role="caption"] {{ color: {p.text_secondary}; font-size: {FONT_SIZE_CAPTION}px; }}

    QListWidget, QTreeWidget, QListView {{
        background: transparent;
        color: {p.text_primary};
        border: none;
    }}
    QListWidget::item {{
        color: {p.text_primary};
        padding: 8px 10px;
        border-radius: {RADIUS}px;
        margin: 1px 2px;
    }}
    QListWidget::item:selected {{ background: {p.accent_dim}; color: {p.text_primary}; }}
    QListWidget::item:hover {{ background: {p.surface_2}; }}
    QLabel[role="heading"] {{
        color: {p.text_primary};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    """
