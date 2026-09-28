from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import TYPE_CHECKING

from legacy.tkinter import chat_widgets, theme
from legacy.tkinter.settings_ui import AVATAR_FILE_NAME
from lib import client
from lib.client import RelayError
from lib.constants import (
    APP_TITLE,
    CHAT_TAIL_ON_CONNECT,
    DATA_DIR,
    FILE_PREVIEW_MAX_BYTES,
    FILE_PREVIEW_MAX_WIDTH,
)
from lib.file_kind import file_icon, file_kind, is_previewable_image, make_preview_image
from lib.formatters import download_progress_text, fmt_size, fmt_time

if TYPE_CHECKING:
    from legacy.tkinter.app import RelayApp


class TabsUIMixin:
    """Mixin, отвечающий за систему вкладок: Общий чат, ЛС, Боты, Файлы.

    Ответственность:
    - Построение переключателя вкладок и контент-фреймов
    - Вкладка «Общий чат» (обёртка над ChatUIMixin-рендером)
    - Вкладка «ЛС» (диалоги, сообщения, поллинг)
    - Вкладка «Боты» (список, добавить/удалить)
    - Вкладка «Файлы» (сетка, превью, фильтры)
    - Обработка DM/bot/files UI-событий из очереди
    - Переопределения ChatUIMixin._build_chat_area/_build_input_bar (noop)
    - Хуки жизненного цикла: _on_connect_ok, _disconnect, _on_close
    """

    # ------------------------------------------------------------------
    # Константы вкладок (масштабируемо: добавь TAB_XXX + запись в _TAB_DEFS)
    # ------------------------------------------------------------------
    TAB_CHAT = "chat"
    TAB_DM = "dm"
    TAB_BOTS = "bots"
    TAB_FILES = "files"

    # ------------------------------------------------------------------
    # Инициализация состояния (вызывается из RelayApp.__init__)
    # ------------------------------------------------------------------
    def _init_tab_state(self: RelayApp) -> None:
        self._active_tab: str = self.TAB_CHAT
        self._tab_buttons: dict[str, object] = {}
        self._tab_frames: dict[str, tk.Frame] = {}
        self._tab_badges: dict[str, tk.Canvas] = {}  # Индикаторы уведомлений

        self._dm_active_user: str | None = None
        self._dm_rows: list[tk.Frame] = []
        self._dm_conversation_rows: dict[str, tk.Frame] = {}
        self._dm_poll_after_id: str | None = None
        self._dm_unread_count: int = 0  # Счётчик непрочитанных ЛС

        self._available_bots: list[str] = []  # Список доступных ботов из папки lib/

        # -- Файловая витрина --
        self._files_filter: str = "all"  # all / image / archive / mod
        self._files_filter_buttons: dict[str, object] = {}
        self._files_grid_inner: tk.Frame | None = None
        self._files_cards: list[tk.Frame] = []
        self._files_preview_images: dict[str, tk.PhotoImage] = {}
        self._files_preview_inflight: set[str] = set()

    # ==================================================================
    # Система вкладок
    # ==================================================================
    def _build_tabs(self: RelayApp) -> None:
        """Строит переключатель вкладок.

        Для добавления новой вкладки:
        1. Добавь константу TAB_XXX в класс
        2. Добавь запись в _TAB_DEFS ниже (id, icon, title)
        3. Создай метод _build_xxx_tab(), который паковать self._tab_frames[TAB_XXX]
        4. Добавь вызов _build_xxx_tab() в __init__
        """
        _TAB_DEFS: list[tuple[str, str, str]] = [
            (self.TAB_CHAT, "💬", "Общий чат"),
            (self.TAB_DM, "✉️", "ЛС"),
            (self.TAB_FILES, "📁", "Файлы"),
            (self.TAB_BOTS, "🤖", "Боты"),
        ]

        bar = tk.Frame(self.root, bg=theme.BG_PANEL, bd=0, height=46)
        bar.pack(fill="x", after=self.conn_root)
        bar.pack_propagate(False)
        self._tabs_bar = bar

        inner = tk.Frame(bar, bg=theme.BG_PANEL)
        inner.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.GAP_SM)

        for tab_id, icon, title in _TAB_DEFS:
            # Контейнер для кнопки + бейджа
            tab_container = tk.Frame(inner, bg=theme.BG_PANEL)
            tab_container.pack(side="left", padx=(0, theme.GAP))

            btn = chat_widgets.make_pill_button(
                tab_container, f"{icon} {title}",
                lambda tid=tab_id: self._switch_tab(tid),
                bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT_DIM,
                font=theme.FONT_CAPTION_BOLD, padx=14, pady=7)
            btn.pack(side="left")
            self._tab_buttons[tab_id] = btn

            # Красная точка для уведомлений
            badge = tk.Canvas(tab_container, width=8, height=8, bg=theme.BG_PANEL,
                           highlightthickness=0, bd=0)
            badge.place(relx=1.0, rely=0.0, x=-4, y=2)
            self._tab_badges[tab_id] = badge
            self._hide_tab_badge(tab_id)

        content_root = tk.Frame(self.root, bg=theme.BG)
        content_root.pack(fill="both", expand=True)
        self._tabs_content_root = content_root

        for tab_id, _, _ in _TAB_DEFS:
            fr = tk.Frame(self._tabs_content_root, bg=theme.BG)
            self._tab_frames[tab_id] = fr

    def _switch_tab(self: RelayApp, tab_id: str) -> None:
        self._active_tab = tab_id

        # Сохраняем текущую вкладку в настройках
        self.settings["last_active_tab"] = tab_id
        from lib.storage import save_settings
        save_settings(self.settings)

        for fr in self._tab_frames.values():
            try:
                fr.pack_forget()
            except tk.TclError:
                pass

        target = self._tab_frames.get(tab_id)
        if target is not None:
            target.pack(fill="both", expand=True)

        for tid, btn in self._tab_buttons.items():
            if tid == tab_id:
                btn.set_bg(theme.ACCENT)
                btn.set_fg(theme.ACCENT_TEXT)
                btn.set_hover_bg(theme.ACCENT_HOVER)
            else:
                btn.set_bg(theme.BG_INPUT)
                btn.set_fg(theme.TEXT_DIM)
                btn.set_hover_bg(theme.BG_HOVER)

        if tab_id == self.TAB_DM:
            self._on_enter_dm_tab()
        elif tab_id == self.TAB_BOTS:
            self._on_enter_bots_tab()
        elif tab_id == self.TAB_FILES:
            self._on_enter_files_tab()
        elif tab_id == self.TAB_CHAT:
            self._on_enter_chat_tab()

    def _on_enter_chat_tab(self: RelayApp) -> None:
        pass

    # ==================================================================
    # Индикаторы уведомлений на вкладках
    # ==================================================================
    def _show_tab_badge(self: RelayApp, tab_id: str, count: int = 0) -> None:
        """Показывает красную точку на вкладке."""
        badge = self._tab_badges.get(tab_id)
        if not badge:
            return
        badge.delete("all")
        if count > 0:
            # Красная точка с цифрой
            badge.configure(width=20 if count > 9 else 16, height=16)
            badge.create_oval(0, 0, 16, 16, fill=theme.ERROR, outline="")
            if count <= 9:
                badge.create_text(8, 8, text=str(count), fill="white",
                               font=theme.FONT_CAPTION_BOLD)
            else:
                badge.create_text(8, 8, text="9+", fill="white",
                               font=theme.FONT_CAPTION_BOLD)
        else:
            # Просто красная точка
            badge.configure(width=8, height=8)
            badge.create_oval(0, 0, 8, 8, fill=theme.ERROR, outline="")

    def _hide_tab_badge(self: RelayApp, tab_id: str) -> None:
        """Скрывает индикатор на вкладке."""
        badge = self._tab_badges.get(tab_id)
        if badge:
            badge.delete("all")

    def _increment_dm_unread(self: RelayApp) -> None:
        """Увеличивает счётчик непрочитанных ЛС."""
        self._dm_unread_count += 1
        if self._active_tab != self.TAB_DM:
            self._show_tab_badge(self.TAB_DM, self._dm_unread_count)

    def _clear_dm_unread(self: RelayApp) -> None:
        """Сбрасывает счётчик непрочитанных ЛС."""
        self._dm_unread_count = 0
        self._hide_tab_badge(self.TAB_DM)

    def _on_file_drop(self: RelayApp, event) -> None:
        """Обработка drag-and-drop файлов (через кнопку файла)."""
        # Простой drag-and-drop через существующую кнопку файла
        # Tkinter нативно не поддерживает DnD файлов без доп. библиотек
        # Используем существующий механизм через кнопку

    def _refresh_available_bots(self: RelayApp) -> None:
        """Сканирует папку lib/ для поиска файлов *_bot.py и обновляет список."""
        from pathlib import Path

        lib_dir = Path(__file__).parent
        bot_files = []

        # Ищем файлы с шаблоном *_bot.py
        for file_path in lib_dir.glob("*_bot.py"):
            # Исключаем bot_base.py
            if file_path.name == "bot_base.py":
                continue

            # Извлекаем ID бота из имени файла (night_shift_bot.py -> night_shift)
            bot_id = file_path.stem.replace("_bot", "")
            if bot_id:
                bot_files.append(bot_id)

        self._available_bots = sorted(bot_files)

        # Обновляем label с доступными ботами
        if hasattr(self, 'available_bots_label'):
            if self._available_bots:
                bots_text = "Доступно: " + ", ".join(self._available_bots)
            else:
                bots_text = "Нет доступных ботов (файлы *_bot.py в lib/)"
            self.available_bots_label.configure(text=bots_text)

    def _on_available_bots_click(self: RelayApp, event) -> None:
        """При клике на список доступных ботов заполняет первый ID."""
        if self._available_bots:
            self.bots_add_var.set(self._available_bots[0])

    # ==================================================================
    # Вкладка «Общий чат» — обёртка над ChatUIMixin
    # ==================================================================
    def _build_chat_tab(self: RelayApp) -> None:
        parent = self._tab_frames[self.TAB_CHAT]

        strip = tk.Frame(parent, bg=theme.BG, bd=0)
        strip.pack(fill="x", padx=theme.PAD, pady=(theme.GAP_LG, 0))
        self.online_label = tk.Label(strip, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                     font=theme.FONT_CAPTION, anchor="w", justify="left")
        self.online_label.pack(fill="x")

        wrap = tk.Frame(parent, bg=theme.BG)
        wrap.pack(fill="both", expand=True, pady=(theme.GAP, 0))

        self.chat_canvas = tk.Canvas(wrap, bg=theme.BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(wrap, command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        self.chat_canvas.pack(side="left", fill="both", expand=True, padx=(theme.PAD_SM, 0))
        scrollbar.pack(side="right", fill="y")

        self.chat_inner = tk.Frame(self.chat_canvas, bg=theme.BG)
        self._chat_inner_id = self.chat_canvas.create_window((0, 0), window=self.chat_inner, anchor="nw")

        def on_inner_configure(_e=None):
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

        def on_canvas_configure(e):
            self.chat_canvas.itemconfig(self._chat_inner_id, width=e.width)

        self.chat_inner.bind("<Configure>", on_inner_configure)
        self.chat_canvas.bind("<Configure>", on_canvas_configure)

        def on_mousewheel(e):
            self.chat_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            self.root.after(100, self._check_scroll_top)

        self.chat_canvas.bind("<Enter>", lambda e: self.chat_canvas.bind_all("<MouseWheel>", on_mousewheel))
        self.chat_canvas.bind("<Leave>", lambda e: self.chat_canvas.unbind_all("<MouseWheel>"))

        def on_scrollbar_scroll(*args):
            self.chat_canvas.yview(*args)
            self.root.after(100, self._check_scroll_top)

        scrollbar.config(command=on_scrollbar_scroll)

        self._typing_indicator = tk.Label(self.chat_inner, text="", bg=theme.BG,
                                          fg=theme.TEXT_DIM, font=theme.FONT_CAPTION,
                                          anchor="w", justify="left")
        self._typing_indicator.pack(side="bottom", anchor="w", padx=theme.PAD, pady=(0, theme.GAP))

        self._build_input_bar_for(parent)

    def _build_input_bar_for(self: RelayApp, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=theme.BG_PANEL)
        bar.pack(fill="x", side="bottom")

        self.reply_bar = tk.Frame(bar, bg=theme.BG_PANEL_2)
        self.reply_label = tk.Label(self.reply_bar, text="", bg=theme.BG_PANEL_2, fg=theme.TEXT_DIM,
                                    font=theme.FONT_CAPTION, anchor="w", justify="left")
        self.reply_label.pack(side="left", fill="x", expand=True, padx=(theme.PAD_LG, theme.GAP), pady=theme.GAP_LG)
        chat_widgets.make_pill_button(
            self.reply_bar, "✕", self._clear_reply,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=10, pady=4).pack(
            side="right", padx=(0, theme.PAD), pady=theme.GAP)

        self.input_inner = tk.Frame(bar, bg=theme.BG_PANEL)
        self.input_inner.pack(fill="x", padx=theme.PAD_LG, pady=theme.PAD_LG)
        inner = self.input_inner

        self.file_btn = chat_widgets.make_pill_button(
            inner, "📎", self._send_file_dialog,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_BODY, padx=14, pady=9)
        self.file_btn.pack(side="left", padx=(0, theme.GAP_LG))

        self.send_btn = chat_widgets.make_pill_button(
            inner, "Отправить", self._send_text,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_BODY_BOLD, padx=18, pady=9)
        self.send_btn.pack(side="right", padx=(theme.GAP_LG, 0))

        pill = chat_widgets.make_pill_container(inner, height=40)
        pill.pack(side="left", fill="x", expand=True)
        self.msg_var = tk.StringVar()
        entry = tk.Entry(pill, textvariable=self.msg_var, bg=theme.BG_INPUT, fg=theme.TEXT,
                         insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                         bd=0, highlightthickness=0)
        pill.attach(entry, pad_x=theme.PAD_LG)
        entry.bind("<Return>", lambda e: self._send_text())
        entry.bind("<KeyRelease>", self._on_key_release)
        self.msg_entry = entry
        self._last_typing_send = 0

    # ==================================================================
    # Вкладка «ЛС»: список диалогов + чат
    # ==================================================================
    def _build_dm_tab(self: RelayApp) -> None:
        parent = self._tab_frames[self.TAB_DM]
        split = tk.Frame(parent, bg=theme.BG)
        split.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)

        # Левая колонка: список диалогов
        left_card = tk.Frame(split, bg=theme.BG_PANEL, bd=0)
        left_card.pack(side="left", fill="y", padx=(0, theme.GAP_LG))
        left_card.configure(width=220)
        left_card.pack_propagate(False)

        left_header = tk.Frame(left_card, bg=theme.BG_PANEL)
        left_header.pack(fill="x", padx=theme.PAD, pady=(theme.PAD, 0))
        tk.Label(left_header, text="Диалоги", bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_HEADING, anchor="w").pack(side="left")

        new_dm_btn = chat_widgets.make_pill_button(
            left_header, "+", self._new_dm_dialog,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_BODY_BOLD, padx=10, pady=5)
        new_dm_btn.pack(side="right")

        tk.Frame(left_card, bg=theme.BORDER, height=1).pack(fill="x", padx=theme.PAD, pady=theme.GAP)

        list_wrap = tk.Frame(left_card, bg=theme.BG_PANEL)
        list_wrap.pack(fill="both", expand=True, padx=(0, 0), pady=(0, theme.PAD_SM))

        self.dm_conv_canvas = tk.Canvas(list_wrap, bg=theme.BG_PANEL, highlightthickness=0, bd=0)
        dm_sb = tk.Scrollbar(list_wrap, command=self.dm_conv_canvas.yview)
        self.dm_conv_canvas.configure(yscrollcommand=dm_sb.set)
        self.dm_conv_canvas.pack(side="left", fill="both", expand=True, padx=(theme.PAD_SM, 0))
        dm_sb.pack(side="right", fill="y")

        self.dm_conv_inner = tk.Frame(self.dm_conv_canvas, bg=theme.BG_PANEL)
        self._dm_conv_inner_id = self.dm_conv_canvas.create_window(
            (0, 0), window=self.dm_conv_inner, anchor="nw")

        def _dm_conv_cfg(_e=None):
            self.dm_conv_canvas.configure(scrollregion=self.dm_conv_canvas.bbox("all"))

        def _dm_conv_canvas_cfg(e):
            self.dm_conv_canvas.itemconfig(self._dm_conv_inner_id, width=e.width)

        self.dm_conv_inner.bind("<Configure>", _dm_conv_cfg)
        self.dm_conv_canvas.bind("<Configure>", _dm_conv_canvas_cfg)

        self.dm_empty_label = tk.Label(self.dm_conv_inner,
                                       text="Нет диалогов\nНажми + чтобы начать",
                                       bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                                       font=theme.FONT_CAPTION, anchor="center",
                                       justify="center")
        self.dm_empty_label.pack(fill="x", pady=theme.PAD_LG)

        # Правая колонка: чат
        right_col = tk.Frame(split, bg=theme.BG)
        right_col.pack(side="left", fill="both", expand=True)

        self.dm_header = tk.Frame(right_col, bg=theme.BG_PANEL, bd=0, height=56)
        self.dm_header.pack(fill="x")
        self.dm_header.pack_propagate(False)

        self.dm_header_label = tk.Label(self.dm_header, text="Выбери диалог или начни новый",
                                        bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                                        font=theme.FONT_HEADING, anchor="w")
        self.dm_header_label.pack(side="left", padx=theme.PAD_LG)

        dm_chat_wrap = tk.Frame(right_col, bg=theme.BG)
        dm_chat_wrap.pack(fill="both", expand=True, pady=(theme.GAP, 0))

        self.dm_chat_canvas = tk.Canvas(dm_chat_wrap, bg=theme.BG, highlightthickness=0, bd=0)
        dm_chat_sb = tk.Scrollbar(dm_chat_wrap, command=self.dm_chat_canvas.yview)
        self.dm_chat_canvas.configure(yscrollcommand=dm_chat_sb.set)
        self.dm_chat_canvas.pack(side="left", fill="both", expand=True, padx=(theme.PAD_SM, 0))
        dm_chat_sb.pack(side="right", fill="y")

        self.dm_chat_inner = tk.Frame(self.dm_chat_canvas, bg=theme.BG)
        self._dm_chat_inner_id = self.dm_chat_canvas.create_window(
            (0, 0), window=self.dm_chat_inner, anchor="nw")

        def _dm_chat_cfg(_e=None):
            self.dm_chat_canvas.configure(scrollregion=self.dm_chat_canvas.bbox("all"))

        def _dm_chat_canvas_cfg(e):
            self.dm_chat_canvas.itemconfig(self._dm_chat_inner_id, width=e.width)

        self.dm_chat_inner.bind("<Configure>", _dm_chat_cfg)
        self.dm_chat_canvas.bind("<Configure>", _dm_chat_canvas_cfg)

        def _dm_mw(e):
            self.dm_chat_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        self.dm_chat_canvas.bind("<Enter>", lambda e: self.dm_chat_canvas.bind_all("<MouseWheel>", _dm_mw))
        self.dm_chat_canvas.bind("<Leave>", lambda e: self.dm_chat_canvas.unbind_all("<MouseWheel>"))

        self.dm_chat_empty = tk.Label(self.dm_chat_inner,
                                      text="Диалог не выбран\nВыбери пользователя слева",
                                      bg=theme.BG, fg=theme.TEXT_MUTED,
                                      font=theme.FONT_CAPTION, anchor="center",
                                      justify="center")
        self.dm_chat_empty.pack(fill="x", pady=(theme.PAD_LG * 3, 0))

        self._build_dm_input_bar(right_col)

    def _build_dm_input_bar(self: RelayApp, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=theme.BG_PANEL)
        bar.pack(fill="x", side="bottom")

        self.dm_input_inner = tk.Frame(bar, bg=theme.BG_PANEL)
        self.dm_input_inner.pack(fill="x", padx=theme.PAD_LG, pady=theme.PAD_LG)
        inner = self.dm_input_inner

        self.dm_send_btn = chat_widgets.make_pill_button(
            inner, "Отправить", self._send_dm_text,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_BODY_BOLD, padx=18, pady=9)
        self.dm_send_btn.pack(side="right", padx=(theme.GAP_LG, 0))

        pill = chat_widgets.make_pill_container(inner, height=40)
        pill.pack(side="left", fill="x", expand=True)
        self.dm_msg_var = tk.StringVar()
        entry = tk.Entry(pill, textvariable=self.dm_msg_var, bg=theme.BG_INPUT, fg=theme.TEXT,
                         insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                         bd=0, highlightthickness=0)
        pill.attach(entry, pad_x=theme.PAD_LG)
        entry.bind("<Return>", lambda e: self._send_dm_text())
        self.dm_msg_entry = entry

    # -- жизненный цикл ЛС --
    def _on_enter_dm_tab(self: RelayApp) -> None:
        self._clear_dm_unread()  # Сбрасываем уведомления при входе
        if not self.connected:
            return
        self._refresh_dm_conversations()
        if self._dm_active_user:
            self._refresh_dm_chat()
        self._schedule_dm_poll()

    def _schedule_dm_poll(self: RelayApp) -> None:
        if self._dm_poll_after_id:
            try:
                self.root.after_cancel(self._dm_poll_after_id)
            except tk.TclError:
                pass
        self._dm_poll_after_id = self.root.after(2000, self._dm_poll_tick)

    def _dm_poll_tick(self: RelayApp) -> None:
        if self._closed:
            return
        if not self.connected or self._active_tab != self.TAB_DM:
            self._dm_poll_after_id = None
            return
        try:
            self._refresh_dm_conversations()
            if self._dm_active_user:
                self._refresh_dm_chat()
        except Exception:
            pass
        self._schedule_dm_poll()

    # -- загрузка данных ЛС (в фоне) --
    def _refresh_dm_conversations(self: RelayApp) -> None:
        if not self.connected:
            return

        def worker():
            try:
                convs = client.get_dm_conversations(self.base_url, self.my_name, self.access_key)
                self._ui_queue.put(("dm_conversations", convs))
            except (RelayError, OSError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_dm_conversations(self: RelayApp, convs: list[dict]) -> None:
        for row in list(self._dm_conversation_rows.values()):
            try:
                row.destroy()
            except tk.TclError:
                pass
        self._dm_conversation_rows.clear()

        if not convs:
            self.dm_empty_label.pack(fill="x", pady=theme.PAD_LG)
            return
        try:
            self.dm_empty_label.pack_forget()
        except tk.TclError:
            pass

        for conv in convs:
            other = conv.get("user", "?")
            last = conv.get("last_message", "")
            ts = conv.get("last_time", 0)
            row = tk.Frame(self.dm_conv_inner, bg=theme.BG_PANEL, cursor="hand2")
            row.pack(fill="x", pady=(theme.GAP_SM, 0))

            is_active = (other == self._dm_active_user)
            if is_active:
                row.configure(bg=theme.BG_HOVER)

            avatar_frame = tk.Frame(row, bg=row["bg"])
            avatar_frame.pack(side="left", padx=(theme.PAD_SM, theme.GAP), pady=theme.GAP)
            chat_widgets.make_avatar(avatar_frame, other, size=36,
                                     image=self._get_avatar_image(other)).pack()

            meta = tk.Frame(row, bg=row["bg"])
            meta.pack(side="left", fill="x", expand=True, pady=theme.GAP_SM)

            top_row = tk.Frame(meta, bg=row["bg"])
            top_row.pack(fill="x")
            tk.Label(top_row, text=other, bg=row["bg"], fg=theme.TEXT,
                     font=theme.FONT_CAPTION_BOLD, anchor="w").pack(side="left")
            if ts:
                try:
                    t_str = time.strftime("%H:%M", time.localtime(ts))
                except Exception:
                    t_str = ""
                tk.Label(top_row, text=t_str, bg=row["bg"], fg=theme.TEXT_MUTED,
                         font=theme.FONT_MICRO, anchor="e").pack(side="right", padx=(0, theme.PAD_SM))

            preview = (last[:40] + "…") if len(last) > 40 else last
            tk.Label(meta, text=preview or "  ", bg=row["bg"], fg=theme.TEXT_DIM,
                     font=theme.FONT_MICRO, anchor="w").pack(fill="x")

            tk.Frame(row, bg=theme.BORDER, height=1).pack(fill="x", padx=theme.PAD_SM, pady=(theme.GAP_SM, 0))

            def _click(_e=None, u=other):
                self._open_dm_with(u)

            for w in (row, meta, top_row, avatar_frame):
                w.bind("<Button-1>", _click)

            self._dm_conversation_rows[other] = row

    def _open_dm_with(self: RelayApp, user: str) -> None:
        if not user:
            return
        self._dm_active_user = user
        self.dm_header_label.configure(text=f"💬 {user}", fg=theme.TEXT)
        try:
            self.dm_chat_empty.pack_forget()
        except tk.TclError:
            pass
        self._refresh_dm_conversations()
        self._refresh_dm_chat()

    def _refresh_dm_chat(self: RelayApp) -> None:
        if not self.connected or not self._dm_active_user:
            return
        target = self._dm_active_user

        def worker():
            try:
                msgs = client.get_dm_history(self.base_url, self.my_name, target, self.access_key)
                self._ui_queue.put(("dm_messages", (target, msgs)))
            except (RelayError, OSError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_dm_messages(self: RelayApp, target: str, msgs: list[dict]) -> None:
        if target != self._dm_active_user:
            return
        for row in self._dm_rows:
            try:
                row.destroy()
            except tk.TclError:
                pass
        self._dm_rows.clear()

        if not msgs:
            self.dm_chat_empty.configure(text=f"Начни общение с {target}\nНапиши первое сообщение!")
            self.dm_chat_empty.pack(fill="x", pady=(theme.PAD_LG * 3, 0))
            return
        try:
            self.dm_chat_empty.pack_forget()
        except tk.TclError:
            pass

        max_w = min(theme.BUBBLE_MAX_WIDTH, max(self._chat_available_width_dm() - 52, 160))
        last_sender = None

        for ev in msgs:
            is_me = ev.get("from") == self.my_name
            sender = ev.get("from", "?")
            grouped = (sender == last_sender)
            last_sender = sender

            def build(row, ev=ev, is_me=is_me, grouped=grouped, sender=sender):
                side = "e" if is_me else "w"
                col = tk.Frame(row, bg=theme.BG)
                col.pack(anchor=side, fill="x" if not is_me else None)

                if not is_me and not grouped:
                    header = tk.Frame(col, bg=theme.BG)
                    header.pack(anchor="w", pady=(0, theme.GAP))
                    chat_widgets.make_avatar(header, sender, size=24,
                                             image=self._get_avatar_image(sender)).pack(side="left")
                    meta = tk.Frame(header, bg=theme.BG)
                    meta.pack(side="left", padx=(theme.GAP, 0))
                    tk.Label(meta, text=sender, bg=theme.BG, fg=theme.TEXT,
                             font=theme.FONT_CAPTION_BOLD, anchor="w").pack(anchor="w")
                    try:
                        t_str = fmt_time(ev.get("ts", 0))
                    except Exception:
                        t_str = ""
                    tk.Label(meta, text=t_str, bg=theme.BG, fg=theme.TEXT_MUTED,
                             font=theme.FONT_MICRO, anchor="w").pack(anchor="w", pady=(1, 0))
                elif is_me and not grouped:
                    try:
                        t_str = fmt_time(ev.get("ts", 0))
                    except Exception:
                        t_str = ""
                    tk.Label(col, text=t_str, bg=theme.BG, fg=theme.TEXT_MUTED,
                             font=theme.FONT_MICRO, anchor="e").pack(anchor="e", pady=(0, theme.GAP))

                content_row = tk.Frame(col, bg=theme.BG)
                content_row.pack(anchor=side, pady=(0, theme.GAP_SM))
                if not is_me and grouped:
                    tk.Frame(content_row, bg=theme.BG, width=28, height=1).pack(side="left")

                bubble = chat_widgets.make_bubble(content_row, ev.get("text", ""), is_me, max_w)
                bubble.pack(side="left")

            row = tk.Frame(self.dm_chat_inner, bg=theme.BG)
            row.pack(fill="x", padx=theme.PAD, pady=(theme.ROW_GAP, theme.ROW_GAP))
            build(row)
            self._dm_rows.append(row)

        self.dm_chat_canvas.update_idletasks()
        self.dm_chat_canvas.yview_moveto(1.0)

    def _chat_available_width_dm(self: RelayApp) -> int:
        w = self.dm_chat_canvas.winfo_width()
        return max(w - 2 * theme.PAD, 220) if w > 1 else 400

    def _send_dm_text(self: RelayApp) -> None:
        if not self.connected or not self._dm_active_user:
            return
        text = self.dm_msg_var.get().strip()
        if not text:
            return
        self.dm_msg_var.set("")
        target = self._dm_active_user

        def worker():
            try:
                client.send_dm(self.base_url, self.my_name, target, text, self.access_key)
                self._ui_queue.put(("dm_sent", target))
            except RelayError as e:
                self._ui_queue.put(("send_error", f"ЛС не отправилось: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _new_dm_dialog(self: RelayApp) -> None:
        if not self.connected:
            messagebox.showinfo(APP_TITLE, "Сначала подключись")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Новое ЛС")
        dialog.geometry("360x200")
        dialog.configure(bg=theme.BG_PANEL)
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="Имя пользователя:",
                 bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_CAPTION, anchor="w").pack(padx=theme.PAD, pady=(theme.PAD, theme.GAP))

        user_var = tk.StringVar()
        entry = tk.Entry(dialog, textvariable=user_var, bg=theme.BG_INPUT, fg=theme.TEXT,
                         insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                         bd=0, highlightthickness=1,
                         highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        entry.pack(padx=theme.PAD, fill="x", pady=(0, theme.GAP))
        entry.focus_set()

        def on_ok():
            u = user_var.get().strip()
            if not u:
                return
            dialog.destroy()
            self._open_dm_with(u)

        def on_cancel():
            dialog.destroy()

        buttons = tk.Frame(dialog, bg=theme.BG_PANEL)
        buttons.pack(padx=theme.PAD, pady=theme.PAD)

        chat_widgets.make_pill_button(
            buttons, "Начать чат", on_ok,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_CAPTION, padx=16, pady=8).pack(side="left", padx=(0, theme.GAP))
        chat_widgets.make_pill_button(
            buttons, "Отмена", on_cancel,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=16, pady=8).pack(side="left")
        entry.bind("<Return>", lambda e: on_ok())

    # ==================================================================
    # Вкладка «Боты»: список + добавить/удалить
    # ==================================================================
    def _build_bots_tab(self: RelayApp) -> None:
        parent = self._tab_frames[self.TAB_BOTS]
        wrap = tk.Frame(parent, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.PAD, pady=theme.PAD)

        # Левая половина: список ботов
        list_card = tk.Frame(wrap, bg=theme.BG_PANEL, bd=0)
        list_card.pack(side="left", fill="both", expand=True, padx=(0, theme.GAP_LG))

        header = tk.Frame(list_card, bg=theme.BG_PANEL)
        header.pack(fill="x", padx=theme.PAD, pady=(theme.PAD, 0))
        tk.Label(header, text="🤖 Активные боты", bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_HEADING, anchor="w").pack(side="left")
        refresh_btn = chat_widgets.make_pill_button(
            header, "🔄 Обновить", self._refresh_bots_ui,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=10, pady=5)
        refresh_btn.pack(side="right")

        tk.Frame(list_card, bg=theme.BORDER, height=1).pack(fill="x", padx=theme.PAD, pady=theme.GAP)

        list_wrap = tk.Frame(list_card, bg=theme.BG_PANEL)
        list_wrap.pack(fill="both", expand=True, padx=(0, 0), pady=(0, theme.PAD_SM))

        self.bots_tab_canvas = tk.Canvas(list_wrap, bg=theme.BG_PANEL, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(list_wrap, command=self.bots_tab_canvas.yview)
        self.bots_tab_canvas.configure(yscrollcommand=sb.set)
        self.bots_tab_canvas.pack(side="left", fill="both", expand=True, padx=(theme.PAD, 0))
        sb.pack(side="right", fill="y")

        self.bots_tab_inner = tk.Frame(self.bots_tab_canvas, bg=theme.BG_PANEL)
        self._bots_inner_id = self.bots_tab_canvas.create_window(
            (0, 0), window=self.bots_tab_inner, anchor="nw")

        def _cfg(_e=None):
            self.bots_tab_canvas.configure(scrollregion=self.bots_tab_canvas.bbox("all"))

        def _cfg_c(e):
            self.bots_tab_canvas.itemconfig(self._bots_inner_id, width=e.width)

        self.bots_tab_inner.bind("<Configure>", _cfg)
        self.bots_tab_canvas.bind("<Configure>", _cfg_c)

        self.bots_empty_label = tk.Label(self.bots_tab_inner,
                                         text="Нет активных ботов\nДобавь бота справа",
                                         bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                                         font=theme.FONT_CAPTION, anchor="center",
                                         justify="center")
        self.bots_empty_label.pack(fill="x", pady=theme.PAD_LG)

        # Правая половина: добавить/удалить
        actions_card = tk.Frame(wrap, bg=theme.BG_PANEL, bd=0)
        actions_card.pack(side="left", fill="y")
        actions_card.configure(width=280)
        actions_card.pack_propagate(False)

        add_block = tk.Frame(actions_card, bg=theme.BG_PANEL)
        add_block.pack(fill="x", padx=theme.PAD, pady=theme.PAD)

        tk.Label(add_block, text="➕ Добавить бота", bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_HEADING, anchor="w").pack(fill="x")
        tk.Frame(add_block, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.GAP)

        tk.Label(add_block, text="ID бота (например: night_shift)",
                 bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(fill="x", pady=(theme.GAP, theme.GAP_SM))

        self.bots_add_var = tk.StringVar()
        add_entry = tk.Entry(add_block, textvariable=self.bots_add_var,
                             bg=theme.BG_INPUT, fg=theme.TEXT,
                             insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                             bd=0, highlightthickness=1,
                             highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        add_entry.pack(fill="x", pady=(0, theme.GAP))

        # Показываем доступных ботов
        self.available_bots_label = tk.Label(add_block, text="",
                                            bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                                            font=theme.FONT_MICRO, anchor="w", justify="left",
                                            cursor="hand2")
        self.available_bots_label.pack(fill="x", pady=(theme.GAP, 0))
        self.available_bots_label.bind("<Button-1>", self._on_available_bots_click)

        add_btn = chat_widgets.make_pill_button(
            add_block, "Добавить", self._add_bot_ui,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_BODY_BOLD, padx=16, pady=8)
        add_btn.pack(fill="x", pady=(theme.GAP, 0))
        add_entry.bind("<Return>", lambda e: self._add_bot_ui())

        tk.Frame(actions_card, bg=theme.BG_PANEL, height=theme.GAP_LG).pack(fill="x")
        remove_block = tk.Frame(actions_card, bg=theme.BG_PANEL)
        remove_block.pack(fill="x", padx=theme.PAD, pady=(0, theme.PAD))

        tk.Label(remove_block, text="🗑 Удалить бота", bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_HEADING, anchor="w").pack(fill="x")
        tk.Frame(remove_block, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.GAP)

        tk.Label(remove_block, text="Введи ID или выбери в списке слева",
                 bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(fill="x", pady=(theme.GAP, theme.GAP_SM))

        self.bots_remove_var = tk.StringVar()
        remove_entry = tk.Entry(remove_block, textvariable=self.bots_remove_var,
                                bg=theme.BG_INPUT, fg=theme.TEXT,
                                insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                                bd=0, highlightthickness=1,
                                highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        remove_entry.pack(fill="x", pady=(0, theme.GAP))

        remove_btn = chat_widgets.make_pill_button(
            remove_block, "Удалить", self._remove_bot_ui,
            bg=theme.ERROR, hover_bg="#d7564e", fg="#ffffff",
            font=theme.FONT_BODY_BOLD, padx=16, pady=8)
        remove_btn.pack(fill="x", pady=(theme.GAP, 0))
        remove_entry.bind("<Return>", lambda e: self._remove_bot_ui())

        tk.Frame(actions_card, bg=theme.BG_PANEL, height=theme.GAP_LG).pack(fill="x")
        help_lbl = tk.Label(actions_card,
                            text="📝 Для добавления бота укажи его ID.\n"
                                 "Код бота ищется в lib/{id}_bot.py\n"
                                 "с классом {Id}Bot(data_dir).\n\n"
                                 "Например: id=night_shift →\n"
                                 "файл lib/night_shift_bot.py,\n"
                                 "класс NightShiftBot.",
                            bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                            font=theme.FONT_MICRO, anchor="w", justify="left")
        help_lbl.pack(fill="x", padx=theme.PAD, pady=(0, theme.PAD))

    def _on_enter_bots_tab(self: RelayApp) -> None:
        self._refresh_available_bots()  # Обновляем список доступных ботов
        self._refresh_bots_ui()

    def _refresh_bots_ui(self: RelayApp) -> None:
        for w in self.bots_tab_inner.winfo_children():
            if w is not self.bots_empty_label:
                try:
                    w.destroy()
                except tk.TclError:
                    pass

        bots: list[str] = []
        bot_names: dict[str, str] = {}
        bot_statuses: dict[str, str] = {}  # Статус бота
        if self.relay:
            try:
                bm = self.relay.get_bot_manager()
                for bid in bm.list_bots():
                    bots.append(bid)
                    b = bm.get_bot(bid)
                    bot_names[bid] = getattr(b, "name", bid)
                    # Получаем статус бота
                    bot_statuses[bid] = self._get_bot_status(b)
            except Exception:
                bots = []

        if not bots:
            self.bots_empty_label.pack(fill="x", pady=theme.PAD_LG)
            if not self.relay:
                self.bots_empty_label.configure(text="Хост не запущен\nСначала стартуй хост в настройках")
            return
        try:
            self.bots_empty_label.pack_forget()
        except tk.TclError:
            pass

        for bid in bots:
            row = tk.Frame(self.bots_tab_inner, bg=theme.BG_PANEL, cursor="hand2")
            row.pack(fill="x", pady=(theme.GAP_SM, 0))

            avatar_fr = tk.Frame(row, bg=theme.BG_PANEL)
            avatar_fr.pack(side="left", padx=(theme.PAD, theme.GAP), pady=theme.GAP)
            chat_widgets.make_avatar(avatar_fr, f"🤖{bid}", size=36).pack()

            meta = tk.Frame(row, bg=theme.BG_PANEL)
            meta.pack(side="left", fill="x", expand=True, pady=theme.GAP_SM)
            display = bot_names.get(bid, bid)
            tk.Label(meta, text=display, bg=theme.BG_PANEL, fg=theme.TEXT,
                     font=theme.FONT_CAPTION_BOLD, anchor="w").pack(anchor="w")
            tk.Label(meta, text=f"id: {bid}", bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                     font=theme.FONT_MICRO, anchor="w").pack(anchor="w")

            # Статус бота
            status = bot_statuses.get(bid, "Неизвестно")
            status_color = theme.TEXT if status == "Активен" else theme.WARNING if "ошибка" in status.lower() else theme.TEXT_MUTED
            tk.Label(meta, text=f"Статус: {status}", bg=theme.BG_PANEL, fg=status_color,
                     font=theme.FONT_MICRO, anchor="w").pack(anchor="w")

            btns = tk.Frame(row, bg=theme.BG_PANEL)
            btns.pack(side="right", padx=(0, theme.PAD), pady=theme.GAP_SM)

            def _do_select(_e=None, b=bid):
                self.bots_remove_var.set(b)

            for w in (row, meta, avatar_fr):
                w.bind("<Button-1>", _do_select)

            chat_widgets.make_pill_button(
                btns, "🗑", lambda b=bid: self._remove_bot_by_id(b),
                bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.ERROR,
                font=theme.FONT_BODY, padx=8, pady=5).pack(side="right")

            tk.Frame(row, bg=theme.BORDER, height=1).pack(fill="x", padx=theme.PAD, pady=(theme.GAP_SM, 0))

    def _get_bot_status(self: RelayApp, bot) -> str:
        """Получает статус бота."""
        try:
            # Проверяем, есть ли у бота метод для получения статуса
            if hasattr(bot, "get_status"):
                return bot.get_status()
            # Проверяем активность бота
            if hasattr(bot, "enabled"):
                return "Активен" if bot.enabled else "Отключён"
            # Проверяем последнее действие
            if hasattr(bot, "last_action"):
                return f"Последнее: {bot.last_action}"
            return "Активен"
        except Exception:
            return "Ошибка"

    def _add_bot_ui(self: RelayApp) -> None:
        bid = self.bots_add_var.get().strip()
        if not bid:
            messagebox.showwarning(APP_TITLE, "Введи ID бота")
            return
        if not self.relay:
            messagebox.showwarning(APP_TITLE, "Сначала запусти хост")
            return

        ok = False
        err_text = ""
        try:
            bm = self.relay.get_bot_manager()
            ok = bm.register_bot_by_id(bid)
            if not ok:
                class_name = "".join(p.capitalize() for p in bid.split("_")) + "Bot"
                err_text = (f"Не удалось загрузить бота '{bid}'.\n"
                            f"Убедись что файл lib/{bid}_bot.py существует\n"
                            f"и в нём класс {class_name}.")
        except Exception as e:
            err_text = f"Ошибка: {e}"

        if ok:
            messagebox.showinfo(APP_TITLE, f"✅ Бот '{bid}' добавлен")
            self.bots_add_var.set("")
            self._refresh_bots_ui()
        else:
            messagebox.showerror(APP_TITLE, err_text or "Неизвестная ошибка")

    def _remove_bot_ui(self: RelayApp) -> None:
        bid = self.bots_remove_var.get().strip()
        if not bid:
            messagebox.showwarning(APP_TITLE, "Введи ID бота")
            return
        self._remove_bot_by_id(bid)

    def _remove_bot_by_id(self: RelayApp, bid: str) -> None:
        if not self.relay:
            messagebox.showwarning(APP_TITLE, "Сначала запусти хост")
            return
        if not messagebox.askyesno(APP_TITLE, f"Удалить бота '{bid}'?"):
            return
        try:
            bm = self.relay.get_bot_manager()
            if bm.unregister_bot(bid):
                messagebox.showinfo(APP_TITLE, f"✅ Бот '{bid}' удалён")
                self.bots_remove_var.set("")
                self._refresh_bots_ui()
            else:
                messagebox.showwarning(APP_TITLE, f"Бот '{bid}' не найден")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Ошибка: {e}")

    # ==================================================================
    # Вкладка «Файлы»: сетка, превью, фильтры
    # ==================================================================
    def _build_files_tab(self: RelayApp) -> None:
        parent = self._tab_frames[self.TAB_FILES]
        wrap = tk.Frame(parent, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.PAD, pady=theme.PAD)

        # --- Header + фильтры ---
        header_card = tk.Frame(wrap, bg=theme.BG_PANEL, bd=0)
        header_card.pack(fill="x", padx=(0, 0), pady=(0, theme.GAP_LG))

        hdr_inner = tk.Frame(header_card, bg=theme.BG_PANEL)
        hdr_inner.pack(fill="x", padx=theme.PAD, pady=theme.PAD)

        left_h = tk.Frame(hdr_inner, bg=theme.BG_PANEL)
        left_h.pack(side="left")
        tk.Label(left_h, text="📁 Файловая витрина", bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_HEADING, anchor="w").pack(anchor="w")
        self.files_count_label = tk.Label(left_h, text="0 файлов", bg=theme.BG_PANEL,
                                           fg=theme.TEXT_MUTED, font=theme.FONT_MICRO,
                                           anchor="w")
        self.files_count_label.pack(anchor="w", pady=(theme.GAP_SM, 0))

        right_h = tk.Frame(hdr_inner, bg=theme.BG_PANEL)
        right_h.pack(side="right")

        refresh_btn = chat_widgets.make_pill_button(
            right_h, "🔄 Обновить", self._refresh_files_ui,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=12, pady=6)
        refresh_btn.pack(side="right", padx=(theme.GAP, 0))

        # --- Фильтры ---
        filters_bar = tk.Frame(header_card, bg=theme.BG_PANEL)
        filters_bar.pack(fill="x", padx=theme.PAD, pady=(0, theme.PAD))

        filters = [
            ("all", "Все", "📦"),
            ("image", "Картинки", "🖼"),
            ("archive", "Архивы", "🗜"),
            ("mod", "Моды", "🎮"),
        ]
        for fid, label, icon in filters:
            btn = chat_widgets.make_pill_button(
                filters_bar, f"{icon} {label}",
                lambda f=fid: self._set_files_filter(f),
                bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT_DIM,
                font=theme.FONT_CAPTION, padx=12, pady=6)
            btn.pack(side="left", padx=(0, theme.GAP))
            self._files_filter_buttons[fid] = btn
        self._apply_files_filter_style("all")

        # --- Область сетки файлов с прокруткой ---
        list_card = tk.Frame(wrap, bg=theme.BG_PANEL, bd=0)
        list_card.pack(fill="both", expand=True)

        # Строка прогресса скачивания с витрины: скорость + проценты
        # (требование v1.5.4 — «скорость скачивания где-то видно»). Отдельный
        # лейбл, а не status_details — тот затирается poll-событиями раз в секунду.
        self.files_download_status = tk.Label(
            list_card, text="", bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
            font=theme.FONT_CAPTION, anchor="w")
        self.files_download_status.pack(fill="x", padx=(theme.PAD, 0), pady=(theme.PAD_SM, 0))

        grid_wrap = tk.Frame(list_card, bg=theme.BG_PANEL)
        grid_wrap.pack(fill="both", expand=True, padx=(0, 0), pady=(0, theme.PAD_SM))

        self.files_canvas = tk.Canvas(grid_wrap, bg=theme.BG_PANEL, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(grid_wrap, command=self.files_canvas.yview)
        self.files_canvas.configure(yscrollcommand=sb.set)
        self.files_canvas.pack(side="left", fill="both", expand=True, padx=(theme.PAD, 0))
        sb.pack(side="right", fill="y")

        self._files_grid_inner = tk.Frame(self.files_canvas, bg=theme.BG_PANEL)
        self._files_grid_id = self.files_canvas.create_window(
            (0, 0), window=self._files_grid_inner, anchor="nw")

        def _cfg(_e=None):
            self.files_canvas.configure(scrollregion=self.files_canvas.bbox("all"))

        def _cfg_c(e):
            self.files_canvas.itemconfig(self._files_grid_id, width=e.width)

        self._files_grid_inner.bind("<Configure>", _cfg)
        self.files_canvas.bind("<Configure>", _cfg_c)

        def _files_mw(e):
            self.files_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        self.files_canvas.bind("<Enter>",
            lambda e: self.files_canvas.bind_all("<MouseWheel>", _files_mw))
        self.files_canvas.bind("<Leave>",
            lambda e: self.files_canvas.unbind_all("<MouseWheel>"))

        self.files_empty_label = tk.Label(
            self._files_grid_inner,
            text="Пока нет файлов\nОтправь что-нибудь в чат — появится здесь",
            bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
            font=theme.FONT_CAPTION, anchor="center", justify="center")
        self.files_empty_label.pack(fill="x", pady=theme.PAD_LG * 3)

    def _set_files_filter(self: RelayApp, fid: str) -> None:
        if fid == self._files_filter:
            return
        self._files_filter = fid
        self._apply_files_filter_style(fid)
        self._refresh_files_ui()

    def _apply_files_filter_style(self: RelayApp, active_id: str) -> None:
        for fid, btn in self._files_filter_buttons.items():
            if fid == active_id:
                btn.set_bg(theme.ACCENT)
                btn.set_fg(theme.ACCENT_TEXT)
                btn.set_hover_bg(theme.ACCENT_HOVER)
            else:
                btn.set_bg(theme.BG_INPUT)
                btn.set_fg(theme.TEXT_DIM)
                btn.set_hover_bg(theme.BG_HOVER)

    def _on_enter_files_tab(self: RelayApp) -> None:
        if self.connected:
            self._refresh_files_ui()

    def _refresh_files_ui(self: RelayApp) -> None:
        """Запрашивает список файлов с сервера (локально или по сети)."""
        # Если мы хост — берём напрямую из индекса без сети
        if self.relay:
            try:
                files = self.relay.list_all_files()
                self._apply_files_list(files)
                return
            except Exception:
                pass

        if not self.connected:
            self._apply_files_list([])
            self.files_count_label.configure(
                text="Не подключено — файлы недоступны")
            return

        def worker():
            try:
                files = client.list_files(self.base_url, self.my_name, self.access_key)
                self._ui_queue.put(("files_list", files))
            except (RelayError, OSError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_files_list(self: RelayApp, files: list[dict]) -> None:
        # Очищаем старые карточки
        for card in self._files_cards:
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._files_cards.clear()

        # Применяем фильтр
        f = self._files_filter
        if f != "all":
            filtered = [ev for ev in files if file_kind(ev.get("name", "")) == f]
        else:
            filtered = files

        total_shown = len(filtered)
        total_available = len(files)
        if total_shown == total_available:
            self.files_count_label.configure(text=f"{total_available} файлов")
        else:
            self.files_count_label.configure(
                text=f"{total_shown} / {total_available} файлов")

        if not filtered:
            if f == "all":
                self.files_empty_label.configure(
                    text="Пока нет файлов\nОтправь что-нибудь в чат — появится здесь")
            else:
                names = {"image": "Картинок", "archive": "Архивов", "mod": "Модов"}
                self.files_empty_label.configure(
                    text=f"{names.get(f, 'Файлов')} в этой категории пока нет")
            self.files_empty_label.pack(fill="x", pady=theme.PAD_LG * 3)
            return
        try:
            self.files_empty_label.pack_forget()
        except tk.TclError:
            pass

        # CARD_SIZE: thumbnail 160x120 preview + metadata below
        # 3 columns
        for i, ev in enumerate(filtered):
            row, col = divmod(i, 3)
            self._file_card(self._files_grid_inner, ev, row, col)
            self._files_grid_inner.grid_columnconfigure(
                col, weight=1, uniform="filecol")

    def _file_card(self: RelayApp, parent: tk.Frame, ev: dict,
                   row: int, col: int) -> None:
        """Одна карточка файла в сетке."""
        file_id = ev.get("file_id", "")
        name = ev.get("name", "файл")
        size_bytes = ev.get("size", 0)
        sender = ev.get("from", "?")
        ts = ev.get("ts", 0)
        kind = file_kind(name)
        icon = file_icon(kind)

        card = tk.Frame(parent, bg=theme.CARD_BG, highlightthickness=1,
                        highlightbackground=theme.CARD_BORDER, bd=0)
        card.grid(row=row, column=col, sticky="nsew", padx=theme.GAP_SM, pady=theme.GAP_SM)
        self._files_cards.append(card)

        # --- Превью (160x120) ---
        thumb_w, thumb_h = 160, 120
        preview_frame = tk.Frame(card, width=thumb_w, height=thumb_h,
                                  bg=theme.BG_INPUT)
        preview_frame.pack(fill="x")
        preview_frame.pack_propagate(False)

        if kind == "image" and is_previewable_image(name, size_bytes, FILE_PREVIEW_MAX_BYTES):
            # Плейсхолдер «загружается…»
            ph = tk.Label(preview_frame, text="⏳", bg=theme.BG_INPUT, fg=theme.TEXT_MUTED,
                         font=theme.FONT_BODY)
            ph.pack(expand=True)
            # Асинхронно качаем превью
            self._fetch_file_preview(file_id, name, size_bytes, preview_frame, ph)
        else:
            # Большая иконка типа файла
            tk.Label(preview_frame, text=icon, bg=theme.BG_INPUT, fg=theme.TEXT,
                    font=("Segoe UI Emoji", 36)).pack(expand=True)

        # --- Метаданные ---
        meta = tk.Frame(card, bg=theme.CARD_BG)
        meta.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)

        name_short = (name[:22] + "…") if len(name) > 22 else name
        tk.Label(meta, text=name_short, bg=theme.CARD_BG, fg=theme.TEXT,
                font=theme.FONT_CAPTION_BOLD, anchor="w",
                cursor="hand2").pack(fill="x", anchor="w")

        sub_line = tk.Frame(meta, bg=theme.CARD_BG)
        sub_line.pack(fill="x", pady=(theme.GAP_SM, 0))
        tk.Label(sub_line, text=f"{sender}", bg=theme.CARD_BG, fg=theme.TEXT_MUTED,
                font=theme.FONT_MICRO, anchor="w").pack(side="left")
        tk.Label(sub_line, text=f"· {fmt_size(size_bytes)}",
                bg=theme.CARD_BG, fg=theme.TEXT_MUTED,
                font=theme.FONT_MICRO, anchor="w").pack(side="left", padx=(4, 0))

        try:
            t_str = time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else ""
        except Exception:
            t_str = ""
        if t_str:
            tk.Label(meta, text=t_str, bg=theme.CARD_BG, fg=theme.TEXT_MUTED,
                    font=theme.FONT_MICRO, anchor="w").pack(fill="x", anchor="w",
                                                           pady=(theme.GAP_SM, 0))

        # --- Кнопки: скачать + открыть папку ---
        btns = tk.Frame(card, bg=theme.CARD_BG)
        btns.pack(fill="x", padx=theme.PAD_SM, pady=(0, theme.PAD_SM))

        dl_btn = chat_widgets.make_pill_button(
            btns, "⬇ Скачать",
            lambda fid=file_id, fn=name: self._download_file_by_id(fid, fn),
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_MICRO, padx=10, pady=4)
        dl_btn.pack(side="left")

        open_btn = chat_widgets.make_pill_button(
            btns, "📂",
            lambda fid=file_id: self._open_file_in_folder(fid),
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT_DIM,
            font=theme.FONT_MICRO, padx=8, pady=4)
        open_btn.pack(side="left", padx=(theme.GAP_SM, 0))

    def _fetch_file_preview(self: RelayApp, file_id: str, filename: str,
                             size_bytes: int, frame: tk.Frame,
                             placeholder: tk.Label) -> None:
        """Асинхронно качает байты изображения и вставляет превью в frame."""
        if file_id in self._files_preview_images:
            self._place_preview_in_frame(frame, placeholder,
                                          self._files_preview_images[file_id])
            return
        if file_id in self._files_preview_inflight:
            return

        # Если локальный файл (хост) — читаем сразу с диска
        if self.relay:
            path = self.relay.file_bytes_path(file_id)
            if path.exists():
                try:
                    data = path.read_bytes()
                    self._process_preview_data(file_id, filename, data, frame, placeholder)
                    return
                except OSError:
                    pass

        if not self.connected:
            return

        self._files_preview_inflight.add(file_id)

        def worker():
            try:
                data = client.fetch_file_bytes(
                    self.base_url, file_id, self.access_key,
                    max_bytes=FILE_PREVIEW_MAX_BYTES)
                self._ui_queue.put(("file_preview", (file_id, filename, data, id(frame),
                                                     id(placeholder))))
            except (RelayError, OSError):
                self._ui_queue.put(("file_preview_fail", file_id))

        threading.Thread(target=worker, daemon=True).start()

    def _process_preview_data(self: RelayApp, file_id: str, filename: str,
                               data: bytes, frame: tk.Frame,
                               placeholder: tk.Label) -> None:
        try:
            img = make_preview_image(data, self.root,
                                     max_width=FILE_PREVIEW_MAX_WIDTH,
                                     filename=filename)
        except Exception:
            img = None
        if img is None:
            return
        self._files_preview_images[file_id] = img
        self._place_preview_in_frame(frame, placeholder, img)

    def _place_preview_in_frame(self: RelayApp, frame: tk.Frame,
                                 placeholder: tk.Label,
                                 img: tk.PhotoImage) -> None:
        try:
            placeholder.destroy()
        except tk.TclError:
            pass
        try:
            for w in list(frame.winfo_children()):
                w.destroy()
        except tk.TclError:
            pass
        lbl = tk.Label(frame, image=img, bg=theme.BG_INPUT, bd=0)
        lbl.pack(expand=True)

    def _download_file_by_id(self: RelayApp, file_id: str, filename: str) -> None:
        """Скачивает файл в папку загрузок (используя существующий механизм)."""
        from pathlib import Path

        dl_dir = self.settings.get("download_dir") or ""
        if not dl_dir:
            try:
                dl_dir = Path.home() / "Downloads"
                dl_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                dl_dir = Path.cwd()
        else:
            dl_dir = Path(dl_dir)

        dest = dl_dir / filename
        # Уникальное имя если уже существует
        n = 1
        while dest.exists():
            stem, suf = dest.stem, dest.suffix
            dest = dl_dir / f"{stem} ({n}){suf}"
            n += 1

        # Используем существующий механизм _enqueue_download если есть
        if hasattr(self, "_enqueue_download"):
            try:
                self._enqueue_download(file_id, filename, dest)
                messagebox.showinfo(APP_TITLE, f"⬇ Скачивание началось:\n{dest}")
                return
            except Exception:
                pass

        # Fallback: сразу скачиваем в фоне — с прогрессом (скорость/проценты)
        # на вкладке файлов. Троттлинг уже в клиенте: раз в ~0.5МБ
        # (PROGRESS_UPDATE_EVERY_BYTES), UI не дёргается на каждый чанк.
        def worker():
            try:
                def progress(received, total, elapsed):
                    self._ui_queue.put(("files_download_progress",
                                        download_progress_text(received, total, elapsed)))

                self._ui_queue.put(("files_download_progress",
                                    f"↓ {filename}: подключаюсь…"))
                client.download_file(self.base_url, file_id, dest, self.access_key,
                                     progress_cb=progress)
                self._ui_queue.put(("download_done", str(dest)))
            except (RelayError, OSError) as e:
                self._ui_queue.put(("download_fail", str(e)))
            finally:
                self._ui_queue.put(("files_download_progress", ""))

        threading.Thread(target=worker, daemon=True).start()
        self._ui_queue.put(("files_download_progress",
                            f"↓ {filename}: стартую…"))

    def _open_file_in_folder(self: RelayApp, file_id: str) -> None:
        """Показывает файл в проводнике (если уже скачан)."""
        path_str = self._downloaded_paths.get(file_id) if hasattr(
            self, "_downloaded_paths") else None
        if not path_str:
            messagebox.showinfo(APP_TITLE, "Файл ещё не скачан — нажми ⬇ Скачать")
            return
        import os
        path = Path(path_str)
        if not path.exists():
            messagebox.showinfo(APP_TITLE, "Файл не найден на диске — скачай заново")
            return
        try:
            if os.name == "nt":
                os.startfile(path.parent)  # type: ignore[attr-defined]
            elif os.name == "posix":
                import subprocess
                subprocess.Popen(["xdg-open", str(path.parent)])
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Не удалось открыть папку: {e}")

    # ==================================================================
    # Обработка DM/bot/files событий очереди UI
    # ==================================================================
    def _handle_tab_ui_event(self: RelayApp, kind: str, payload) -> bool:
        """Обрабатывает события вкладок. Возвращает True если событие съедено."""
        if kind == "dm_conversations":
            self._apply_dm_conversations(payload)
            return True
        if kind == "dm_messages":
            target, msgs = payload
            self._apply_dm_messages(target, msgs)
            # Если получили новые сообщения и не на вкладке ЛС - показываем уведомление
            if msgs and self._active_tab != self.TAB_DM:
                self._increment_dm_unread()
            return True
        if kind == "dm_sent":
            self._refresh_dm_chat()
            self._refresh_dm_conversations()
            return True
        if kind == "files_list":
            self._apply_files_list(payload)
            return True
        if kind == "file_preview":
            file_id, filename, data, frame_id, ph_id = payload
            self._files_preview_inflight.discard(file_id)
            # Ищем реальные виджеты по id — грубый поиск через winfo_children
            frame = self._find_widget_by_id(self.root, frame_id)
            ph = self._find_widget_by_id(self.root, ph_id)
            if frame is not None and ph is not None:
                self._process_preview_data(file_id, filename, data, frame, ph)
            return True
        if kind == "file_preview_fail":
            self._files_preview_inflight.discard(payload)
            return True
        if kind == "files_download_progress":
            # Прогресс/скорость скачивания с витрины (строка под шапкой вкладки)
            if hasattr(self, "files_download_status"):
                self.files_download_status.configure(text=payload)
            return True
        if kind == "download_done":
            messagebox.showinfo(APP_TITLE, f"✅ Готово:\n{payload}")
            return True
        if kind == "download_fail":
            messagebox.showerror(APP_TITLE, f"❌ Ошибка скачивания:\n{payload}")
            return True
        return False

    def _find_widget_by_id(self: RelayApp, root: tk.Misc, target_id: int) -> tk.Misc | None:
        """Ищет виджет по id() — обход для async preview callbacks."""
        if id(root) == target_id:
            return root
        try:
            for child in root.winfo_children():
                r = self._find_widget_by_id(child, target_id)
                if r is not None:
                    return r
        except tk.TclError:
            pass
        return None

    # ==================================================================
    # Переопределения: выключаем старые _build_chat_area/_build_input_bar
    # (ChatUIMixin их больше не вызывает — мы строим чат внутри вкладки сами)
    # ==================================================================
    def _build_chat_area(self: RelayApp) -> None:
        pass

    def _build_input_bar(self: RelayApp) -> None:
        pass

    # ==================================================================
    # Хуки жизненного цикла (с super() для кооперации с другими миксинами)
    # ==================================================================
    def _on_connect_ok(self: RelayApp, payload) -> None:
        info, result = payload
        self.connected = True
        self._connected_at = time.time()
        self._stop_event.clear()
        self.connect_btn.set_enabled(True)
        self.connect_btn.set_text("Отключиться")
        self._set_status(self._connected_status_text(), theme.STATUS_CONNECTED,
                         details=self._get_connection_details())
        self._append_system(f"Подключено. Хост раздачи: {info.get('name', '?')}")
        self._collapse_settings_on_connect()

        self.since = result.get("next_since", 0)
        events = result.get("events", [])
        if events:
            self._min_loaded_seq = min(ev.get("seq", 0) for ev in events)
        else:
            self._min_loaded_seq = 0

        if len(events) >= CHAT_TAIL_ON_CONNECT:
            self._append_system(f"Показаны последние {CHAT_TAIL_ON_CONNECT} сообщений "
                                f"(скролль вверх для загрузки старых)")
        if events:
            for ev in events:
                self._render_event(ev, defer_scroll=True)
            self._chat_scroll_to_bottom()
            self._trim_old_rows()
        self._update_online_strip(result.get("online", []))

        avatar_path = DATA_DIR / AVATAR_FILE_NAME
        if avatar_path.exists():
            try:
                data = avatar_path.read_bytes()
                threading.Thread(target=self._upload_avatar_bg, args=(data,), daemon=True).start()
            except OSError:
                pass

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self._switch_tab(self.TAB_CHAT)
        self._refresh_bots_ui()

    def _disconnect(self: RelayApp) -> None:
        if self._dm_poll_after_id:
            try:
                self.root.after_cancel(self._dm_poll_after_id)
            except tk.TclError:
                pass
        self._dm_poll_after_id = None

        self._stop_event.set()
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        self.connected = False
        self.since = 0
        self._connected_at = 0.0
        self._last_poll_ms = None
        self._online_users.clear()
        self._dm_active_user = None
        self.connect_btn.set_enabled(True)
        self.connect_btn.set_text("Подключиться")
        self._set_status("Не подключено", theme.STATUS_DISCONNECTED, "")
        self._append_system("Отключено")

    def _on_close(self: RelayApp) -> None:
        self._closed = True
        self._stop_event.set()
        self._stop_pulse_animation()
        if self._dm_poll_after_id:
            try:
                self.root.after_cancel(self._dm_poll_after_id)
            except tk.TclError:
                pass
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
        if self.relay is not None:
            self.relay.stop()
        try:
            self.settings.update(self._collect_settings())
            from lib.storage import save_settings
            save_settings(self.settings)
        except tk.TclError:
            pass
        self.root.destroy()
