from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import TYPE_CHECKING

from legacy.tkinter import chat_widgets, theme
from legacy.tkinter.chat_widgets import show_context_menu
from lib import client
from lib.client import RelayError
from lib.constants import APP_TITLE, EVENT_KIND_DELETE, MAX_RENDERED_ROWS
from lib.formatters import fmt_size, fmt_time

if TYPE_CHECKING:
    from legacy.tkinter.app import RelayApp


class ChatUIMixin:
    """Лента чата, ввод, контекстные меню и рендер сообщений."""

    def _build_online_strip(self: RelayApp) -> None:
        strip = tk.Frame(self.root, bg=theme.BG, bd=0)
        strip.pack(fill="x", padx=theme.PAD, pady=(theme.GAP_LG, 0))
        self.online_label = tk.Label(strip, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                     font=theme.FONT_CAPTION, anchor="w", justify="left")
        self.online_label.pack(fill="x")

    def _build_chat_area(self: RelayApp) -> None:
        wrap = tk.Frame(self.root, bg=theme.BG)
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
            # После скролла проверяем не достигли ли верха для подгрузки старых сообщений
            self.root.after(100, self._check_scroll_top)

        self.chat_canvas.bind("<Enter>", lambda e: self.chat_canvas.bind_all("<MouseWheel>", on_mousewheel))
        self.chat_canvas.bind("<Leave>", lambda e: self.chat_canvas.unbind_all("<MouseWheel>"))
        
        # Также отслеживаем скролл через scrollbar
        def on_scrollbar_scroll(*args):
            self.root.after(100, self._check_scroll_top)
        
        scrollbar.config(command=lambda *args: (self.chat_canvas.yview(*args), on_scrollbar_scroll(*args)))

        self._typing_indicator = tk.Label(self.chat_inner, text="", bg=theme.BG,
                                          fg=theme.TEXT_DIM, font=theme.FONT_CAPTION,
                                          anchor="w", justify="left")
        self._typing_indicator.pack(side="bottom", anchor="w", padx=theme.PAD, pady=(0, theme.GAP))

    def _chat_scroll_to_bottom(self: RelayApp) -> None:
        self.chat_canvas.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

    def _chat_available_width(self: RelayApp) -> int:
        w = self.chat_canvas.winfo_width()
        return max(w - 2 * theme.PAD, 220) if w > 1 else 460

    def _build_input_bar(self: RelayApp) -> None:
        bar = tk.Frame(self.root, bg=theme.BG_PANEL)
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

    def _update_typing_indicator(self: RelayApp) -> None:
        current_time = time.time()
        active_typers = [name for name, ts in self._typing_users.items()
                         if current_time - ts < 5.0]
        if not active_typers:
            self._typing_indicator.configure(text="")
        elif len(active_typers) == 1:
            self._typing_indicator.configure(text=f"{active_typers[0]} печатает...")
        else:
            names = ", ".join(active_typers[:2])
            self._typing_indicator.configure(text=f"{names} и ещё {len(active_typers) - 1} печатают...")
        self._typing_users = {name: ts for name, ts in self._typing_users.items()
                              if current_time - ts < 5.0}
        self.root.after(1000, self._update_typing_indicator)

    def _handle_typing_event(self: RelayApp, sender: str) -> None:
        if sender != self.my_name:
            self._typing_users[sender] = time.time()
            if not self._typing_indicator.cget("text"):
                self._update_typing_indicator()

    # -- контекстные меню -------------------------------------------------
    def _truncate_preview(self: RelayApp, text: str, max_len: int = 72) -> str:
        text = " ".join(text.split())
        return text if len(text) <= max_len else text[: max_len - 1] + "…"

    def _copy_to_clipboard(self: RelayApp, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _bind_message_context(self: RelayApp, widget: tk.Widget, ev: dict) -> None:
        widget.bind("<Button-3>", lambda e, ev=ev: self._show_message_menu(ev, e))

    def _bind_file_context(self: RelayApp, widget: tk.Widget, ev: dict, handle) -> None:
        widget.bind("<Button-3>", lambda e, ev=ev, h=handle: self._show_file_menu(ev, h, e))

    def _show_message_menu(self: RelayApp, ev: dict, event) -> None:
        is_me = ev.get("from") == self.my_name
        text = ev.get("text", "")
        seq = ev.get("seq")

        items = [
            ("Копировать", lambda: self._copy_to_clipboard(text), bool(text)),
            ("Ответить", lambda: self._start_reply(ev), True),
        ]
        if is_me and seq is not None:
            items.extend([("---", None, True), ("Удалить", lambda: self._delete_message(seq), True)])
        show_context_menu(self.root, event.x_root, event.y_root, items)

    def _show_file_menu(self: RelayApp, ev: dict, handle, event) -> None:
        file_id = ev.get("file_id", "")
        name = ev.get("name", "файл")
        is_me = ev.get("from") == self.my_name
        seq = ev.get("seq")
        has_local = (file_id in self._downloaded_paths
                     and Path(self._downloaded_paths[file_id]).exists())
        items = [
            ("Скачать", lambda: self._download_file(file_id, name, handle), True),
            ("Открыть папку", lambda: self._open_file_folder(file_id),
             has_local or bool(self.download_dir_var.get().strip())),
            ("Информация", lambda: self._show_file_info(ev), True),
        ]
        if is_me and seq is not None:
            items.extend([("---", None, True), ("Удалить", lambda: self._delete_message(seq), True)])
        show_context_menu(self.root, event.x_root, event.y_root, items)

    def _start_reply(self: RelayApp, ev: dict) -> None:
        sender = ev.get("from", "?")
        preview = (self._truncate_preview(ev.get("text", "")) if ev.get("kind") == "text"
                   else ev.get("name", "файл"))
        self._reply_to = {"sender": sender, "preview": preview, "kind": ev.get("kind")}
        self.reply_label.configure(text=f"↩ Ответ на {sender}: {preview}")
        self.reply_bar.pack(fill="x", before=self.input_inner)
        self.msg_entry.focus_set()

    def _clear_reply(self: RelayApp) -> None:
        self._reply_to = None
        self.reply_bar.pack_forget()

    def _delete_message(self: RelayApp, seq: int) -> None:
        if not self.connected:
            return
        if not messagebox.askyesno(APP_TITLE, "Удалить это сообщение?"):
            return
        threading.Thread(target=self._delete_message_bg, args=(seq,), daemon=True).start()

    def _delete_message_bg(self: RelayApp, seq: int) -> None:
        try:
            client.delete_event(self.base_url, self.my_name, seq, self.access_key)
            self._ui_queue.put(("message_deleted", seq))
        except RelayError as e:
            self._ui_queue.put(("send_error", f"Не удалось удалить: {e}"))

    def _remove_row_by_seq(self: RelayApp, seq: int) -> None:
        row = self._row_by_seq.pop(seq, None)
        if row is None:
            return
        try:
            row.destroy()
        except tk.TclError:
            pass

    def _open_file_folder(self: RelayApp, file_id: str) -> None:
        path_str = self._downloaded_paths.get(file_id)
        if path_str and Path(path_str).exists():
            folder = str(Path(path_str).parent)
        else:
            folder = self.download_dir_var.get().strip()
            if not folder or not Path(folder).is_dir():
                messagebox.showinfo(APP_TITLE,
                                    "Сначала скачай файл или укажи папку для скачивания в настройках.")
                return
        self._open_folder(folder)

    def _open_folder(self: RelayApp, folder: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", folder], check=False)
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Не удалось открыть папку: {e}")

    def _show_file_info(self: RelayApp, ev: dict) -> None:
        file_id = ev.get("file_id", "")
        local = self._downloaded_paths.get(file_id, "")
        lines = [
            f"Имя: {ev.get('name', '?')}",
            f"Размер: {fmt_size(ev.get('size', 0))}",
            f"От: {ev.get('from', '?')}",
            f"Время: {time.strftime('%d.%m.%Y %H:%M', time.localtime(ev.get('ts', 0)))}",
            f"ID: {file_id}",
        ]
        if local:
            lines.append(f"Локально: {local}")
        messagebox.showinfo("Информация о файле", "\n".join(lines))

    # -- рендер чата ------------------------------------------------------
    def _append_row(self: RelayApp, widget_factory, defer_scroll: bool = False,
                    seq: int | None = None, prepend: bool = False) -> tk.Frame:
        row = tk.Frame(self.chat_inner, bg=theme.BG)
        if prepend:
            # Вставляем в начало чата (перед первым виджетом)
            children = self.chat_inner.winfo_children()
            if children:
                row.pack(fill="x", padx=theme.PAD, pady=(theme.ROW_GAP, theme.ROW_GAP), before=children[0])
            else:
                row.pack(fill="x", padx=theme.PAD, pady=(theme.ROW_GAP, theme.ROW_GAP))
        else:
            # Обычное добавление в конец
            row.pack(fill="x", padx=theme.PAD, pady=(theme.ROW_GAP, theme.ROW_GAP))
        widget_factory(row)
        if seq is not None:
            self._row_by_seq[seq] = row
        if not defer_scroll and not prepend:
            self._chat_scroll_to_bottom()
        return row

    def _trim_old_rows(self: RelayApp) -> None:
        children = self.chat_inner.winfo_children()
        excess = len(children) - MAX_RENDERED_ROWS
        if excess > 0:
            for w in children[:excess]:
                for seq, row in list(self._row_by_seq.items()):
                    if row is w:
                        del self._row_by_seq[seq]
                        break
                w.destroy()

    def _append_system(self: RelayApp, text: str) -> None:
        self._last_sender = None

        def build(row):
            tk.Label(row, text=text, bg=theme.BG, fg=theme.WARNING, font=theme.FONT_CAPTION,
                     justify="center", anchor="center",
                     wraplength=self._chat_available_width()).pack(fill="x", pady=theme.GAP)

        self._append_row(build)

    def _append_error(self: RelayApp, text: str) -> None:
        self._last_sender = None

        def build(row):
            tk.Label(row, text=f"✕ {text}", bg=theme.BG, fg=theme.ERROR, font=theme.FONT_CAPTION,
                     justify="center", anchor="center",
                     wraplength=self._chat_available_width()).pack(fill="x", pady=theme.GAP)

        self._append_row(build)

    def _render_event(self: RelayApp, ev: dict, defer_scroll: bool = False, prepend: bool = False) -> None:
        if ev.get("kind") == EVENT_KIND_DELETE:
            target = ev.get("target_seq")
            if isinstance(target, int):
                self._remove_row_by_seq(target)
            return

        is_me = ev.get("from") == self.my_name
        sender = ev.get("from", "?")
        grouped = (sender == self._last_sender)
        self._last_sender = sender
        max_w = min(theme.BUBBLE_MAX_WIDTH, max(self._chat_available_width() - 52, 160))
        seq = ev.get("seq")

        def build(row):
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
                tk.Label(meta, text=fmt_time(ev["ts"]), bg=theme.BG, fg=theme.TEXT_MUTED,
                         font=theme.FONT_MICRO, anchor="w").pack(anchor="w", pady=(1, 0))
            elif is_me and not grouped:
                tk.Label(col, text=fmt_time(ev["ts"]), bg=theme.BG, fg=theme.TEXT_MUTED,
                         font=theme.FONT_MICRO, anchor="e").pack(anchor="e", pady=(0, theme.GAP))

            content_row = tk.Frame(col, bg=theme.BG)
            content_row.pack(anchor=side, pady=(0, theme.GAP_SM))

            if not is_me and grouped:
                tk.Frame(content_row, bg=theme.BG, width=28, height=1).pack(side="left")

            if ev["kind"] == "text":
                bubble = chat_widgets.make_bubble(content_row, ev.get("text", ""), is_me, max_w)
                bubble.pack(side="left")
                self._bind_message_context(bubble, ev)
                self._bind_message_context(col, ev)
            elif ev["kind"] == "file":
                name = ev.get("name", "файл")
                size = fmt_size(ev.get("size", 0))
                file_id = ev.get("file_id", "")
                self._file_ids_shown.add(file_id)
                handle = chat_widgets.make_file_card(content_row, name, size, width=max_w)
                handle.btn.configure(
                    command=lambda fid=file_id, n=name, h=handle: self._download_file(fid, n, h))
                handle.canvas.pack(side="left")
                self._file_cards[file_id] = handle
                self._bind_file_context(handle.canvas, ev, handle)
                self._bind_file_context(col, ev, handle)
                self._request_file_preview(file_id, name, ev.get("size", 0))

        self._append_row(build, defer_scroll=defer_scroll, seq=seq if isinstance(seq, int) else None, prepend=prepend)

        if not is_me and ev["kind"] in ("text", "file") and not self._window_focused:
            self._unread = True
            if self.sound_var.get():
                self._play_notification_sound(ev["kind"])
            self.root.title(f"● {APP_TITLE}")

    def _start_transient_progress(self: RelayApp, token: str, name: str, total_size: int) -> None:
        self._last_sender = None
        max_w = min(theme.BUBBLE_MAX_WIDTH, max(self._chat_available_width() - 24, 160))
        text = f"↑ Отправляю {name}   (0Б / {fmt_size(total_size)})"

        def build(row):
            box = tk.Frame(row, bg=theme.BG)
            box.pack(anchor="e")
            card = chat_widgets.make_progress_row(box, text, width=max_w)
            card.pack()
            self._progress_rows[token] = {"row": row, "card": card}

        self._append_row(build)

    def _update_transient_progress(self: RelayApp, token: str, pct: int, text: str) -> None:
        entry = self._progress_rows.get(token)
        if entry and "card" in entry:
            try:
                entry["card"].update_progress(pct, text)
            except tk.TclError:
                pass

    def _remove_transient_progress(self: RelayApp, token: str) -> None:
        entry = self._progress_rows.pop(token, None)
        if not entry:
            return
        try:
            entry["row"].destroy()
        except tk.TclError:
            pass

    def _fail_transient_progress(self: RelayApp, token: str, text: str) -> None:
        entry = self._progress_rows.get(token)
        if entry and "card" in entry:
            try:
                entry["card"].update_text(text, color=theme.ERROR)
            except tk.TclError:
                pass
