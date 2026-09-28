# -- база -------------------------------------------------------------
BG = "#15151b"
BG_PANEL = "#1c1c24"
BG_PANEL_2 = "#212129"
BG_INPUT = "#26262f"
BG_HOVER = "#31313c"
BORDER = "#2a2a34"

TEXT = "#eceef3"
TEXT_DIM = "#9195a6"
TEXT_MUTED = "#6b6e7d"

ACCENT = "#7c8cff"
ACCENT_HOVER = "#6674e8"
ACCENT_TEXT = "#0d0d12"

SUCCESS = "#5fd68a"
ERROR = "#f0685f"
WARNING = "#e3a95f"

# -- чат ----------------------------------------------------------------
BUBBLE_ME = "#5b67e8"
BUBBLE_ME_TEXT = "#f3f4ff"
BUBBLE_OTHER = "#24242e"
BUBBLE_OTHER_TEXT = "#eceef3"
BUBBLE_RADIUS = 14

# -- статус -------------------------------------------------------------
STATUS_DISCONNECTED = "#6b6e7d"
STATUS_CONNECTING = "#e3a95f"
STATUS_CONNECTED = "#5fd68a"
STATUS_ERROR = "#f0685f"

CARD_BG = "#1f1f28"
CARD_BORDER = "#33333f"

AVATAR_COLORS = [
    "#f4645a", "#f2a44c", "#e8cd52", "#6fcf8f", "#4fc3d9",
    "#6f8cff", "#b877e0", "#f27fa5",
]

# -- типографика: чёткая иерархия размеров ------------------------------
FONT_FAMILY = "Segoe UI"
FONT_DISPLAY = (FONT_FAMILY, 15, "bold")     # заголовок приложения
FONT_HEADING = (FONT_FAMILY, 11, "bold")     # секции настроек
FONT_BODY = (FONT_FAMILY, 10)                # основной текст, поля ввода, пузыри
FONT_BODY_BOLD = (FONT_FAMILY, 10, "bold")   # кнопки, имена файлов
FONT_CAPTION = (FONT_FAMILY, 9)              # подписи, метки, системные сообщения
FONT_CAPTION_BOLD = (FONT_FAMILY, 9, "bold") # имя отправителя в чате
FONT_MICRO = (FONT_FAMILY, 8)                # время, второстепенные подписи
FONT_AVATAR = (FONT_FAMILY, 11, "bold")

# алиасы для обратной совместимости
FONT_UI = FONT_BODY
FONT_BOLD = FONT_BODY_BOLD
FONT_SMALL = FONT_CAPTION
FONT_SMALL_BOLD = FONT_CAPTION_BOLD
FONT_TITLE = FONT_DISPLAY

# -- отступы ------------------------------------------------------------
PAD = 14           # основной горизонтальный отступ
PAD_SM = 8
PAD_LG = 18
GAP = 6            # между связанными элементами внутри блока
GAP_SM = 4         # мелкий зазор (между заголовком и пузырём)
GAP_LG = 12        # между секциями / группами
ROW_GAP = 6        # между строками чата

BUBBLE_PAD_X = 14
BUBBLE_PAD_Y = 10
BUBBLE_MAX_WIDTH = 340


def avatar_color(name: str) -> str:
    if not name:
        return AVATAR_COLORS[0]
    return AVATAR_COLORS[sum(ord(c) for c in name) % len(AVATAR_COLORS)]
