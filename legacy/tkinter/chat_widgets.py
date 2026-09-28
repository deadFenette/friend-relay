from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field

from legacy.tkinter import theme
from lib.file_kind import file_icon, file_kind

MenuItem = tuple[str, Callable[[], None] | None, bool]  # label, command, enabled


def show_context_menu(parent: tk.Widget, x: int, y: int, items: list[MenuItem]) -> None:
    """Тёмное всплывающее меню по ПКМ. Закрывается при клике вне или выборе пункта."""
    menu = tk.Menu(parent, tearoff=0, bg=theme.BG_PANEL, fg=theme.TEXT,
                   activebackground=theme.ACCENT, activeforeground=theme.ACCENT_TEXT,
                   disabledforeground=theme.TEXT_MUTED, bd=0, relief="flat",
                   font=theme.FONT_UI)
    for label, command, enabled in items:
        if label == "---":
            menu.add_separator()
            continue
        state = "normal" if enabled and command else "disabled"
        menu.add_command(label=label, command=command, state=state)
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                  radius: float, **kwargs) -> int:
    """Скруглённый прямоугольник на Canvas - Tk не умеет так нативно, но
    полигон с smooth=True по угловым точкам даёт вполне достойный результат
    дёшево, без сторонних библиотек."""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def make_avatar(parent: tk.Widget, name: str, size: int = 30,
                 image: tk.PhotoImage | None = None) -> tk.Canvas:
    """Кружок аватарки. Если передана готовая картинка (уже circular-PNG,
    см. lib/avatar.py) - рисуем её; иначе - цветной кружок с первой буквой
    имени, цвет детерминированно зависит от имени."""
    c = tk.Canvas(parent, width=size, height=size, bg=theme.BG, highlightthickness=0, bd=0)
    if image is not None:
        c.create_image(size / 2, size / 2, image=image)
        c.image = image  # держим ссылку - иначе Tk соберёт мусор и картинка исчезнет
    else:
        color = theme.avatar_color(name)
        c.create_oval(1, 1, size - 1, size - 1, fill=color, outline="")
        letter = (name or "?").strip()[:1].upper() or "?"
        c.create_text(size / 2, size / 2 + 1, text=letter, fill="#0d0d12", font=theme.FONT_AVATAR)
    return c


def make_bubble(parent: tk.Widget, text: str, is_me: bool,
                 max_width: int = theme.BUBBLE_MAX_WIDTH) -> tk.Canvas:
    """Пузырь сообщения: сначала кладём текст, меряем его реальный bbox,
    подгоняем канвас и скруглённый фон под точный размер - без ручного
    переноса строк и угадывания высоты."""
    bg = theme.BUBBLE_ME if is_me else theme.BUBBLE_OTHER
    fg = theme.BUBBLE_ME_TEXT if is_me else theme.BUBBLE_OTHER_TEXT
    pad_x, pad_y = theme.BUBBLE_PAD_X, theme.BUBBLE_PAD_Y

    c = tk.Canvas(parent, bg=theme.BG, highlightthickness=0, bd=0)
    text_id = c.create_text(pad_x, pad_y, text=text, anchor="nw", fill=fg,
                              font=theme.FONT_BODY, width=max_width - 2 * pad_x, justify="left")
    x1, y1, x2, y2 = c.bbox(text_id)
    w = (x2 - x1) + 2 * pad_x
    h = (y2 - y1) + 2 * pad_y
    c.configure(width=w, height=h)
    rect_id = rounded_rect(c, 0, 0, w, h, theme.BUBBLE_RADIUS, fill=bg, outline="")
    c.tag_lower(rect_id, text_id)
    c.coords(text_id, pad_x, pad_y)
    return c


def make_file_card(parent: tk.Widget, filename: str, size_text: str,
                    on_download: Callable[[], None] | None = None,
                    width: int = theme.BUBBLE_MAX_WIDTH) -> FileCardHandle:
    """Карточка файла с иконкой по типу, опциональным превью и прогресс-баром."""
    handle = FileCardHandle(parent, filename, size_text, on_download, width)
    handle.build()
    return handle


@dataclass
class FileCardHandle:
    parent: tk.Widget
    filename: str
    size_text: str
    on_download: Callable[[], None] | None
    width: int
    canvas: tk.Canvas = field(init=False)
    btn: tk.Button = None  # type: ignore[assignment]
    _preview: tk.PhotoImage | None = None
    _progress_pct: float | None = None
    _progress_label: str = ""
    _btn_text: str = "⬇ Скачать"
    _win_ids: dict = field(default_factory=dict)

    def build(self) -> None:
        self.canvas = tk.Canvas(self.parent, bg=theme.BG, highlightthickness=0, bd=0)
        self._redraw()

    def set_preview(self, image: tk.PhotoImage) -> None:
        self._preview = image
        self._redraw()

    def set_progress(self, pct: float | None, label: str) -> None:
        self._progress_pct = pct
        self._progress_label = label
        if pct is None:
            self._btn_text = "⬇ Скачать"
            try:
                self.btn.configure(state="normal")
            except tk.TclError:
                pass
        else:
            self._btn_text = label
            try:
                self.btn.configure(state="disabled")
            except tk.TclError:
                pass
        self._redraw()

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._win_ids.clear()

        pad = theme.PAD_SM + 2
        icon_w = 40
        kind = file_kind(self.filename)
        inner_w = self.width - 2 * pad
        y = pad

        if self._preview is not None:
            ph = self._preview.height()
            c.create_image(pad, y, anchor="nw", image=self._preview, tags="preview")
            c._preview_ref = self._preview  # type: ignore[attr-defined]
            y += ph + theme.GAP_LG

        text_x = pad + icon_w + theme.GAP_LG
        name_id = c.create_text(text_x, y, text=self.filename, anchor="nw",
                                fill=theme.TEXT, font=theme.FONT_BODY_BOLD,
                                width=inner_w - icon_w - theme.GAP_LG, tags="text")
        x1, y1, x2, y2 = c.bbox(name_id)
        size_id = c.create_text(text_x, y2 + theme.GAP, text=self.size_text, anchor="nw",
                                fill=theme.TEXT_DIM, font=theme.FONT_CAPTION, tags="text")
        _, _, _, sy2 = c.bbox(size_id)
        content_bottom = sy2

        icon_y = y if self._preview is None else pad
        if self._preview is None:
            icon_id = rounded_rect(c, pad, icon_y, pad + icon_w, icon_y + icon_w, 8,
                                   fill=theme.BG_INPUT, outline="", tags="icon")
            c.create_text(pad + icon_w / 2, icon_y + icon_w / 2, text=file_icon(kind),
                          font=("Segoe UI", 15), tags="icon")
            c.tag_raise(icon_id)

        bar_y = content_bottom + theme.GAP_LG
        btn_y = content_bottom + theme.GAP_LG
        if self._progress_pct is not None:
            bar_w = inner_w - icon_w - theme.GAP_LG
            bar_x = text_x
            _draw_progress_bar(c, bar_x, bar_y, bar_w, 6, self._progress_pct / 100.0)
            if self._progress_label:
                c.create_text(bar_x, bar_y + 10, text=self._progress_label, anchor="nw",
                              fill=theme.TEXT_DIM, font=theme.FONT_MICRO, tags="text")
            btn_y = bar_y + 24

        if not hasattr(self, "btn") or self.btn is None:
            self.btn = tk.Button(c, text=self._btn_text, bg=theme.BG_INPUT, fg=theme.TEXT,
                                 relief="flat", activebackground=theme.BG_HOVER,
                                 font=theme.FONT_CAPTION, padx=12, pady=5, bd=0,
                                 highlightthickness=0, command=self.on_download)
        else:
            self.btn.configure(text=self._btn_text)

        if self._progress_pct is None:
            self.btn.configure(state="normal")
            win_id = c.create_window(text_x, btn_y, anchor="nw", window=self.btn, tags="btn")
            self._win_ids["btn"] = win_id
            c.update_idletasks()
            btn_h = self.btn.winfo_reqheight()
            card_h = btn_y + btn_h + pad
        else:
            self.btn.configure(state="disabled")
            card_h = btn_y + pad
        c.configure(width=self.width, height=card_h)
        rect_id = rounded_rect(c, 0, 0, self.width, card_h, theme.BUBBLE_RADIUS,
                               fill=theme.CARD_BG, outline=theme.CARD_BORDER, tags="bg")
        c.tag_lower(rect_id)


def _draw_progress_bar(canvas: tk.Canvas, x: float, y: float, w: float, h: float,
                       fraction: float, tag: str = "bar") -> None:
    fraction = max(0.0, min(1.0, fraction))
    canvas.create_rectangle(x, y, x + w, y + h, fill=theme.BG_INPUT, outline="", tags=tag)
    if fraction > 0:
        canvas.create_rectangle(x, y, x + w * fraction, y + h, fill=theme.ACCENT, outline="", tags=tag)


def make_progress_row(parent: tk.Widget, text: str, width: int = theme.BUBBLE_MAX_WIDTH) -> tk.Canvas:
    """Карточка прогресса отправки: текст + полоска, обновляется через update_progress()."""
    pad_x, pad_y = theme.BUBBLE_PAD_X, theme.BUBBLE_PAD_Y - 2
    bar_h = 6
    c = tk.Canvas(parent, bg=theme.BG, highlightthickness=0, bd=0)
    state = {"text_id": None, "rect_id": None, "bar_bg": None, "bar_fill": None, "w": 0, "h": 0}

    def _layout(text_val: str, pct: float, color: str) -> None:
        c.delete("all")
        inner_w = width - 2 * pad_x
        text_id = c.create_text(pad_x, pad_y, text=text_val, anchor="nw", fill=color,
                                font=theme.FONT_CAPTION, width=inner_w, justify="left")
        x1, y1, x2, y2 = c.bbox(text_id)
        bar_y = y2 + theme.GAP
        _draw_progress_bar(c, pad_x, bar_y, inner_w, bar_h, pct / 100.0)
        w = width
        h = bar_y + bar_h + pad_y
        c.configure(width=w, height=h)
        rect_id = rounded_rect(c, 0, 0, w, h, 10, fill=theme.BG_PANEL_2, outline="")
        c.tag_lower(rect_id)
        state.update(text_id=text_id, rect_id=rect_id, w=w, h=h)

    _layout(text, 0.0, theme.WARNING)

    def update_progress(pct: float, new_text: str, color: str = theme.WARNING) -> None:
        _layout(new_text, pct, color)

    def update_text(new_text: str, color: str = theme.WARNING) -> None:
        _layout(new_text, 100.0 if color != theme.ERROR else 0.0, color)

    c.update_progress = update_progress  # type: ignore[attr-defined]
    c.update_text = update_text  # type: ignore[attr-defined]
    return c


def make_pill_button(parent: tk.Widget, text: str, command: Callable[[], None] | None,
                      bg: str, hover_bg: str, fg: str, font=theme.FONT_UI,
                      padx: int = 14, pady: int = 8) -> tk.Canvas:
    """Кликабельная 'таблетка' на Canvas - скруглённая на все 100% (radius =
    половина высоты), с ховер-подсветкой. Тkinter-кнопки со скруглением не
    умеют, а этот трюк даёт настоящую пилюлю дёшево."""
    c = tk.Canvas(parent, bg=theme.BG_PANEL, highlightthickness=0, bd=0, cursor="hand2")
    text_id = c.create_text(padx, pady, text=text, anchor="nw", fill=fg, font=font)
    x1, y1, x2, y2 = c.bbox(text_id)
    w = (x2 - x1) + 2 * padx
    h = (y2 - y1) + 2 * pady
    c.configure(width=w, height=h)
    rect_id = rounded_rect(c, 0, 0, w, h, h / 2, fill=bg, outline="")
    c.tag_lower(rect_id, text_id)
    c.coords(text_id, padx, pady)

    state = {"enabled": True, "bg": bg, "hover_bg": hover_bg}

    def on_enter(_e=None):
        if state["enabled"]:
            c.itemconfig(rect_id, fill=state["hover_bg"])

    def on_leave(_e=None):
        if state["enabled"]:
            c.itemconfig(rect_id, fill=state["bg"])

    def on_click(_e=None):
        if state["enabled"] and command:
            command()

    c.bind("<Enter>", on_enter)
    c.bind("<Leave>", on_leave)
    c.bind("<Button-1>", on_click)

    def set_enabled(enabled: bool) -> None:
        state["enabled"] = enabled
        target_bg = state["bg"] if enabled else theme.BG_INPUT
        c.itemconfig(rect_id, fill=target_bg)
        c.itemconfig(text_id, fill=fg if enabled else theme.TEXT_MUTED)
        c.configure(cursor="hand2" if enabled else "arrow")

    def set_text(new_text: str) -> None:
        c.itemconfig(text_id, text=new_text)

    def set_bg(new_bg: str) -> None:
        state["bg"] = new_bg
        c.itemconfig(rect_id, fill=new_bg)

    def set_fg(new_fg: str) -> None:
        c.itemconfig(text_id, fill=new_fg)

    def set_hover_bg(new_hover_bg: str) -> None:
        state["hover_bg"] = new_hover_bg

    c.set_enabled = set_enabled  # type: ignore[attr-defined]
    c.set_text = set_text  # type: ignore[attr-defined]
    c.set_bg = set_bg  # type: ignore[attr-defined]
    c.set_fg = set_fg  # type: ignore[attr-defined]
    c.set_hover_bg = set_hover_bg  # type: ignore[attr-defined]
    return c


def make_pill_container(parent: tk.Widget, height: int = 40) -> tuple:
    """Скруглённый контейнер-'таблетка' для поля ввода: возвращает
    (canvas, redraw(inner_widget)) - redraw перерисовывает фон и
    подгоняет вложенный виджет под текущий размер канваса при ресайзе."""
    c = tk.Canvas(parent, bg=theme.BG_PANEL, highlightthickness=0, bd=0, height=height)
    state = {"rect_id": None, "win_id": None, "inner": None}

    def attach(inner_widget, pad_x: int = 14) -> None:
        state["inner"] = inner_widget
        state["win_id"] = c.create_window(pad_x, height // 2, anchor="w", window=inner_widget)

        def redraw(_e=None):
            w = max(c.winfo_width(), 10)
            if state["rect_id"] is not None:
                c.delete(state["rect_id"])
            state["rect_id"] = rounded_rect(c, 1, 1, w - 1, height - 1, height / 2,
                                              fill=theme.BG_INPUT, outline="")
            c.tag_lower(state["rect_id"])
            c.coords(state["win_id"], pad_x, height // 2)
            c.itemconfig(state["win_id"], width=w - 2 * pad_x)

        c.bind("<Configure>", redraw)

    c.attach = attach  # type: ignore[attr-defined]
    return c


