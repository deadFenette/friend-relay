from __future__ import annotations

import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

from legacy.tkinter import chat_widgets, theme
from lib import avatar
from lib.constants import APP_TITLE, APP_VERSION, AVATAR_SIZE, DATA_DIR, DEFAULT_PORT
from lib.storage import save_settings
from lib.updater import (
    UpdateInfo,
    current_exe_path,
    download_update,
    is_frozen_exe,
    schedule_replace_and_restart,
)

if TYPE_CHECKING:
    from legacy.tkinter.app import RelayApp

AVATAR_FILE_NAME = "avatar.png"


class SettingsUIMixin:
    """Панель подключения, профиль, настройки и индикатор статуса."""

    def _build_connection_area(self: RelayApp) -> None:
        self.conn_root = tk.Frame(self.root, bg=theme.BG_PANEL, bd=0)
        self.conn_root.pack(fill="x")
        self._build_setup_card()

    def _build_setup_card(self: RelayApp) -> None:
        card = tk.Frame(self.conn_root, bg=theme.BG_PANEL, bd=0)
        card.pack(fill="x", padx=theme.PAD, pady=theme.GAP_LG)
        self.setup_card = card

        header = tk.Frame(card, bg=theme.BG_PANEL)
        header.pack(fill="x", pady=(0, theme.GAP_LG))

        tk.Label(header, text=APP_TITLE, bg=theme.BG_PANEL, fg=theme.TEXT,
                 font=theme.FONT_DISPLAY, anchor="w").pack(side="left")

        self.settings_expanded = tk.BooleanVar(value=True)
        self.settings_toggle_btn = chat_widgets.make_pill_button(
            header, "Настройки ▲", self._toggle_settings,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION_BOLD, padx=12, pady=5)
        self.settings_toggle_btn.pack(side="right")

        self.settings_container = tk.Frame(card, bg=theme.BG_PANEL)
        self.settings_container.pack(fill="x")

        self._accordion_sections: dict[str, dict] = {}
        self._build_accordion_section("Профиль", self._build_profile_section, expanded=True)
        self._build_accordion_section("Подключение", self._build_connection_section, expanded=True)
        self._build_accordion_section("Настройки", self._build_preferences_section, expanded=False)

        bottom = tk.Frame(self.settings_container, bg=theme.BG_PANEL)
        bottom.pack(fill="x", pady=(theme.GAP_LG + 4, 0))

        self.connect_btn = chat_widgets.make_pill_button(
            bottom, "Подключиться", self._on_connect_click,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, fg=theme.ACCENT_TEXT,
            font=theme.FONT_BODY_BOLD, padx=16, pady=8)
        self.connect_btn.pack(side="left")

        status_container = tk.Frame(bottom, bg=theme.BG_PANEL)
        status_container.pack(side="left", padx=(theme.GAP_LG, 0), fill="x", expand=True)

        self.status_indicator = tk.Canvas(status_container, width=20, height=20, bg=theme.BG_PANEL,
                                          highlightthickness=0, bd=0)
        self.status_indicator.pack(side="left")
        self._draw_status_indicator(theme.STATUS_DISCONNECTED)

        self.status_label = tk.Label(status_container, text="Не подключено", bg=theme.BG_PANEL,
                                     fg=theme.TEXT_DIM, font=theme.FONT_CAPTION, anchor="w")
        self.status_label.pack(side="left", padx=(theme.GAP, 0))

        self.status_details = tk.Label(status_container, text="", bg=theme.BG_PANEL,
                                       fg=theme.TEXT_MUTED, font=theme.FONT_CAPTION, anchor="w")
        self.status_details.pack(side="left", padx=(theme.GAP, 0))

        ver_row = tk.Frame(card, bg=theme.BG_PANEL)
        ver_row.pack(fill="x", pady=(theme.GAP, 0))
        tk.Label(ver_row, text=f"v{APP_VERSION}", bg=theme.BG_PANEL, fg=theme.TEXT_MUTED,
                 font=theme.FONT_MICRO, anchor="w").pack(side="left")
        self.update_btn = chat_widgets.make_pill_button(
            ver_row, "", self._on_update_click,
            bg=theme.WARNING, hover_bg="#c9924f", fg="#1a1208",
            font=theme.FONT_CAPTION_BOLD, padx=10, pady=4)
        self._pending_update = None

        self._pulse_state = {"active": False, "direction": 1, "size": 1.0, "after_id": None}
        self._layout_role_fields()

    def _build_accordion_section(self: RelayApp, title: str, build_content_fn,
                                  expanded: bool = False) -> None:
        section_frame = tk.Frame(self.settings_container, bg=theme.BG_PANEL)
        section_frame.pack(fill="x", pady=(theme.GAP_LG, 0))

        header_btn = tk.Button(
            section_frame,
            text=f"{'▼' if expanded else '▶'} {title}",
            command=lambda: self._toggle_accordion_section(title),
            bg=theme.BG_PANEL, fg=theme.ACCENT, font=theme.FONT_HEADING,
            anchor="w", relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            activebackground=theme.BG_PANEL, activeforeground=theme.ACCENT_HOVER,
            padx=0, pady=theme.GAP,
        )
        header_btn.pack(fill="x")

        tk.Frame(section_frame, bg=theme.BORDER, height=1).pack(fill="x", pady=(0, theme.GAP))

        content_frame = tk.Frame(section_frame, bg=theme.BG_PANEL)
        if expanded:
            content_frame.pack(fill="x")

        self._accordion_sections[title] = {
            "header": header_btn, "content": content_frame, "expanded": expanded,
        }
        build_content_fn(content_frame)

    def _toggle_accordion_section(self: RelayApp, title: str) -> None:
        section = self._accordion_sections.get(title)
        if not section:
            return
        section["expanded"] = not section["expanded"]
        if section["expanded"]:
            section["content"].pack(fill="x")
            section["header"].configure(text=f"▼ {title}")
        else:
            section["content"].pack_forget()
            section["header"].configure(text=f"▶ {title}")

    def _build_profile_section(self: RelayApp, parent: tk.Frame) -> None:
        profile_row = tk.Frame(parent, bg=theme.BG_PANEL)
        profile_row.pack(fill="x", pady=(0, theme.GAP_LG))

        self.avatar_preview = tk.Canvas(profile_row, width=48, height=48, bg=theme.BG_PANEL,
                                        highlightthickness=0, bd=0)
        self._draw_avatar_placeholder()
        self.avatar_preview.pack(side="left")

        profile_fields = tk.Frame(profile_row, bg=theme.BG_PANEL)
        profile_fields.pack(side="left", padx=(theme.GAP_LG, 0), fill="x", expand=True)
        tk.Label(profile_fields, text="Моё имя", bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(anchor="w")
        self.name_var = tk.StringVar(value=self.settings.get("name", ""))
        self._styled_entry(profile_fields, self.name_var, width=18).pack(
            anchor="w", pady=(theme.GAP, theme.GAP))
        chat_widgets.make_pill_button(
            profile_fields, "Сменить фото...", self._on_pick_avatar,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=10, pady=4).pack(anchor="w")

    def _build_connection_section(self: RelayApp, parent: tk.Frame) -> None:
        conn_settings = tk.Frame(parent, bg=theme.BG_PANEL)
        conn_settings.pack(fill="x", pady=(0, theme.GAP_LG))

        role_row = tk.Frame(conn_settings, bg=theme.BG_PANEL)
        role_row.pack(fill="x", pady=(0, theme.GAP_LG))
        self.is_host_var = tk.BooleanVar(value=self.settings.get("is_host", False))
        self._styled_check(role_row, "Я хост", self.is_host_var,
                           command=self._on_role_toggle).pack(side="left")
        self.auto_connect_var = tk.BooleanVar(value=self.settings.get("auto_connect", False))
        self._styled_check(role_row, "Авто", self.auto_connect_var).pack(
            side="left", padx=(theme.GAP_LG + 4, 0))

        self.role_fields = tk.Frame(conn_settings, bg=theme.BG_PANEL)
        self.role_fields.pack(fill="x", pady=(0, theme.GAP_LG))

        self.port_block = self._labeled_entry_block(
            self.role_fields, "Порт",
            tk.StringVar(value=str(self.settings.get("port", DEFAULT_PORT))), width=6)
        self.port_var = self.port_block.var

        self.max_size_block = self._labeled_entry_block(
            self.role_fields, "Лимит (МБ)",
            tk.StringVar(value=str(self.settings.get("max_file_size_mb", 2048))), width=6)
        self.max_size_var = self.max_size_block.var

        self.host_url_block = self._labeled_entry_block(
            self.role_fields, "Адрес хоста",
            tk.StringVar(value=self.settings.get("host_url", "http://")), width=20)
        self.host_url_var = self.host_url_block.var

        key_block = self._labeled_entry_block(
            conn_settings, "Ключ",
            tk.StringVar(value=self.settings.get("access_key", "")), width=14, show="•")
        key_block.pack(fill="x")
        self.key_var = key_block.var

    def _build_preferences_section(self: RelayApp, parent: tk.Frame) -> None:
        preferences = tk.Frame(parent, bg=theme.BG_PANEL)
        preferences.pack(fill="x", pady=(0, theme.GAP))

        dl_block = tk.Frame(preferences, bg=theme.BG_PANEL)
        dl_block.pack(fill="x", pady=(0, theme.GAP_LG))
        tk.Label(dl_block, text="Папка для скачивания", bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(anchor="w")
        dl_row = tk.Frame(dl_block, bg=theme.BG_PANEL)
        dl_row.pack(fill="x", pady=(theme.GAP, 0))
        self.download_dir_var = tk.StringVar(value=self.settings.get("download_dir", ""))
        self._styled_entry(dl_row, self.download_dir_var, width=20).pack(side="left")
        chat_widgets.make_pill_button(
            dl_row, "Обзор", self._pick_download_dir,
            bg=theme.BG_INPUT, hover_bg=theme.BG_HOVER, fg=theme.TEXT,
            font=theme.FONT_CAPTION, padx=10, pady=4).pack(side="left", padx=(theme.GAP, 0))

        self.sound_var = tk.BooleanVar(value=self.settings.get("sound_on_message", True))
        self._styled_check(preferences, "Звук уведомлений", self.sound_var,
                           command=self._update_sound_settings).pack(anchor="w", pady=(0, theme.GAP))

        volume_row = tk.Frame(preferences, bg=theme.BG_PANEL)
        volume_row.pack(fill="x")
        tk.Label(volume_row, text="Громкость", bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(side="left")
        self.volume_var = tk.DoubleVar(value=self.settings.get("sound_volume", 0.5))
        volume_scale = tk.Scale(
            volume_row, from_=0.0, to=1.0, orient="horizontal", variable=self.volume_var,
            bg=theme.BG_PANEL, fg=theme.TEXT, highlightthickness=0, bd=0,
            resolution=0.1, length=100, showvalue=False, command=self._on_volume_change)
        volume_scale.pack(side="left", padx=(theme.GAP_LG, 0))
        self.volume_label = tk.Label(
            volume_row, text=f"{int(self.volume_var.get() * 100)}%",
            bg=theme.BG_PANEL, fg=theme.TEXT_DIM, font=theme.FONT_CAPTION, anchor="w")
        self.volume_label.pack(side="left", padx=(theme.GAP, 0))

        upd_row = tk.Frame(preferences, bg=theme.BG_PANEL)
        upd_row.pack(fill="x", pady=(theme.GAP_LG, 0))
        tk.Label(upd_row, text="Проверка обновлений", bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(anchor="w")
        self.check_updates_var = tk.BooleanVar(value=self.settings.get("check_updates", True))
        self._styled_check(upd_row, "Искать новую версию при запуске",
                           self.check_updates_var).pack(anchor="w", pady=(theme.GAP, theme.GAP))
        tk.Label(upd_row, text="URL манифеста (JSON)", bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(anchor="w")
        self.update_manifest_var = tk.StringVar(
            value=self.settings.get("update_manifest_url", ""))
        self._styled_entry(upd_row, self.update_manifest_var, width=28).pack(
            anchor="w", pady=(theme.GAP, 0))

    def _toggle_settings(self: RelayApp) -> None:
        if self.settings_expanded.get():
            self.settings_container.pack_forget()
            self.settings_expanded.set(False)
            self.settings_toggle_btn.set_text("Настройки ▼")
        else:
            self.settings_container.pack(fill="x")
            self.settings_expanded.set(True)
            self.settings_toggle_btn.set_text("Настройки ▲")
        self._layout_role_fields()

    def _collapse_settings_on_connect(self: RelayApp) -> None:
        if self.settings_expanded.get():
            self.settings_container.pack_forget()
            self.settings_expanded.set(False)
            self.settings_toggle_btn.set_text("Настройки ▼")

    def _draw_status_indicator(self: RelayApp, color: str) -> None:
        self.status_indicator.delete("all")
        center, radius = 10, 7
        self.status_indicator.create_oval(center - radius, center - radius,
                                          center + radius, center + radius,
                                          fill=color, outline="", tags="main")
        inner = 2.5
        self.status_indicator.create_oval(center - inner, center - inner,
                                          center + inner, center + inner,
                                          fill="#ffffff", outline="", tags="inner")

    def _start_pulse_animation(self: RelayApp, color: str) -> None:
        self._stop_pulse_animation()
        self._pulse_state.update(active=True, direction=1, size=1.0)
        self._pulse_animation_step(color)

    def _stop_pulse_animation(self: RelayApp) -> None:
        if self._pulse_state["after_id"]:
            self.root.after_cancel(self._pulse_state["after_id"])
            self._pulse_state["after_id"] = None
        self._pulse_state["active"] = False

    def _pulse_animation_step(self: RelayApp, color: str) -> None:
        if not self._pulse_state["active"]:
            return
        center, base_radius = 10, 7
        pulse_radius = base_radius * self._pulse_state["size"]
        self.status_indicator.delete("pulse")
        self.status_indicator.create_oval(
            center - pulse_radius, center - pulse_radius,
            center + pulse_radius, center + pulse_radius,
            fill="", outline=color, width=2, tags="pulse")
        step = 0.1
        if self._pulse_state["direction"] == 1:
            self._pulse_state["size"] += step
            if self._pulse_state["size"] >= 1.5:
                self._pulse_state["direction"] = -1
        else:
            self._pulse_state["size"] -= step
            if self._pulse_state["size"] <= 1.0:
                self._pulse_state["direction"] = 1
        self._pulse_state["after_id"] = self.root.after(
            100, lambda: self._pulse_animation_step(color))

    def _set_status(self: RelayApp, text: str, color: str, details: str = "",
                    animate: bool = False) -> None:
        self.status_label.configure(text=text, fg=color)
        self.status_details.configure(text=details)
        self._draw_status_indicator(color)
        if animate:
            self._start_pulse_animation(color)
        else:
            self._stop_pulse_animation()

    def _connected_status_text(self: RelayApp) -> str:
        role = "хост раздачи" if self.is_host_var.get() else f"подключён к {self.base_url}"
        return f"{self.my_name} · {role}"

    def _get_connection_details(self: RelayApp) -> str:
        connected_at = getattr(self, "_connected_at", 0)
        if not self.connected or not connected_at:
            return ""
        elapsed = time.time() - connected_at
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        if hours > 0:
            time_str = f"{hours}ч {minutes}м"
        elif minutes > 0:
            time_str = f"{minutes}м {seconds}с"
        else:
            time_str = f"{seconds}с"
        online_count = len(getattr(self, "_online_users", set()))
        rtt = getattr(self, "_last_poll_ms", None)
        ping = f" · ↕ {rtt}мс" if rtt is not None else ""
        return f"⏱ {time_str} · 👥 {online_count}{ping}"

    def _start_send_animation(self: RelayApp) -> None:
        if self._send_animation_state["after_id"]:
            self.root.after_cancel(self._send_animation_state["after_id"])
        self._send_animation_state["active"] = True
        self._send_animation_state["position"] = 0
        self._send_animation_step()

    def _stop_send_animation(self: RelayApp) -> None:
        if self._send_animation_state["after_id"]:
            self.root.after_cancel(self._send_animation_state["after_id"])
            self._send_animation_state["after_id"] = None
        self._send_animation_state["active"] = False

    def _send_animation_step(self: RelayApp) -> None:
        if not self._send_animation_state["active"]:
            return
        if self._send_animation_state["position"] < 3:
            dots = "." * (self._send_animation_state["position"] + 1)
            self.connect_btn.set_text(f"Отправка{dots}")
            self._send_animation_state["position"] += 1
        else:
            self._send_animation_state["position"] = 0
        self._send_animation_state["after_id"] = self.root.after(300, self._send_animation_step)

    def _styled_entry(self: RelayApp, parent, var: tk.StringVar,
                      width: int = 20, show: str | None = None) -> tk.Entry:
        kwargs = {"show": show} if show else {}
        return tk.Entry(parent, textvariable=var, width=width, bg=theme.BG_INPUT, fg=theme.TEXT,
                        insertbackground=theme.TEXT, relief="flat", font=theme.FONT_BODY,
                        highlightthickness=1, highlightbackground=theme.BORDER,
                        highlightcolor=theme.ACCENT, **kwargs)

    def _styled_check(self: RelayApp, parent, text: str, var: tk.BooleanVar,
                      command=None) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=var, command=command,
                              bg=theme.BG_PANEL, fg=theme.TEXT_DIM, selectcolor=theme.BG_INPUT,
                              activebackground=theme.BG_PANEL, activeforeground=theme.TEXT,
                              font=theme.FONT_CAPTION, bd=0, highlightthickness=0, anchor="w")

    def _labeled_entry_block(self: RelayApp, parent, label: str, var: tk.StringVar,
                             width: int = 16, show: str | None = None) -> tk.Frame:
        block = tk.Frame(parent, bg=theme.BG_PANEL)
        tk.Label(block, text=label, bg=theme.BG_PANEL, fg=theme.TEXT_DIM,
                 font=theme.FONT_CAPTION, anchor="w").pack(anchor="w")
        entry = self._styled_entry(block, var, width=width, show=show)
        entry.pack(anchor="w", pady=(theme.GAP, 0))
        block.var = var  # type: ignore[attr-defined]
        return block

    def _layout_role_fields(self: RelayApp) -> None:
        for w in (self.port_block, self.max_size_block, self.host_url_block):
            w.pack_forget()
        if self.is_host_var.get():
            self.port_block.pack(side="left", padx=(0, theme.GAP_LG + 4))
            self.max_size_block.pack(side="left")
        else:
            self.host_url_block.pack(side="left")

    def _on_role_toggle(self: RelayApp) -> None:
        self._layout_role_fields()

    def _draw_avatar_placeholder(self: RelayApp) -> None:
        self.avatar_preview.delete("all")
        color = theme.avatar_color(self.settings.get("name", "") or "?")
        self.avatar_preview.create_oval(1, 1, 51, 51, fill=color, outline="")
        letter = (self.settings.get("name", "") or "?").strip()[:1].upper() or "?"
        self.avatar_preview.create_text(26, 27, text=letter, fill="#0d0d12", font=theme.FONT_DISPLAY)

    def _load_own_avatar_preview(self: RelayApp) -> None:
        path = DATA_DIR / AVATAR_FILE_NAME
        if not path.exists():
            return
        try:
            data = path.read_bytes()
            img = tk.PhotoImage(data=data, master=self.root)
        except (OSError, tk.TclError):
            return
        self._own_avatar_full = img
        self.avatar_preview.delete("all")
        self.avatar_preview.create_image(26, 26, image=img)

    def _on_pick_avatar(self: RelayApp) -> None:
        if not avatar.HAS_PIL:
            messagebox.showinfo(APP_TITLE, avatar.NO_PIL_MESSAGE)
            return
        path = filedialog.askopenfilename(
            title="Выбери фото для аватарки",
            filetypes=[("Изображения", "*.png *.jpg *.jpeg *.bmp *.gif"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            png_bytes = avatar.make_circular_avatar_png(Path(path), AVATAR_SIZE)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Не удалось обработать фото: {e}")
            return
        dest = DATA_DIR / AVATAR_FILE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png_bytes)
        self.settings["has_avatar"] = True
        save_settings(self.settings)
        self._load_own_avatar_preview()
        if self.connected:
            threading.Thread(target=self._upload_avatar_bg, args=(png_bytes,), daemon=True).start()

    def _pick_download_dir(self: RelayApp) -> None:
        d = filedialog.askdirectory(title="Папка для скачанных файлов")
        if d:
            self.download_dir_var.set(d)

    def _collect_settings(self: RelayApp) -> dict:
        return {
            "name": self.name_var.get(), "is_host": self.is_host_var.get(),
            "port": self.port_var.get(), "host_url": self.host_url_var.get(),
            "access_key": self.key_var.get(), "auto_connect": self.auto_connect_var.get(),
            "max_file_size_mb": self.max_size_var.get(),
            "download_dir": self.download_dir_var.get(),
            "sound_on_message": self.sound_var.get(),
            "sound_volume": self.volume_var.get(),
            "has_avatar": self.settings.get("has_avatar", False),
            "check_updates": self.check_updates_var.get(),
            "update_manifest_url": self.update_manifest_var.get().strip(),
        }

    def _show_update_banner(self: RelayApp, info: UpdateInfo) -> None:
        self._pending_update = info
        label = f"⬆ Обновление {info.version}"
        if is_frozen_exe():
            label = f"⬇ Установить {info.version}"
        self.update_btn.set_text(label)
        self.update_btn.pack(side="right")

    def _on_update_click(self: RelayApp) -> None:
        info = getattr(self, "_pending_update", None)
        if not info:
            return

        if not is_frozen_exe():
            url = info.primary_url
            if url:
                webbrowser.open(url)
            return

        exe = current_exe_path()
        if not exe:
            url = info.primary_url
            if url:
                webbrowser.open(url)
            return

        dest = exe.with_name(f"FriendRelay-new-{info.version}.exe")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass

        btn = self.update_btn
        btn.set_enabled(False)
        btn.set_text("Подключение...")

        base_relay_url = getattr(self, "base_url", "")
        preferred_file_id = getattr(self, "_update_file_id", "") or ""

        def on_progress(received: int, total: int) -> None:
            if total <= 0:
                pct_str = f"{received // 1024}КБ"
            else:
                pct = received * 100 // total
                pct_str = f"{pct}%"
            self._ui_queue.put(("update_progress", pct_str))

        def worker() -> None:
            ok, source_label = download_update(
                info,
                dest,
                progress_cb=on_progress,
                timeout=120,
                preferred_base_relay_url=base_relay_url,
                preferred_file_id=preferred_file_id,
            )
            self._ui_queue.put(("update_downloaded", (ok, source_label, dest)))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_progress(self: RelayApp, pct_str: str) -> None:
        try:
            self.update_btn.set_text(f"{pct_str} — скачиваем...")
        except (tk.TclError, AttributeError):
            pass

    def _handle_update_downloaded(self: RelayApp, ok: bool, source_label: str, dest: Path) -> None:
        btn = self.update_btn
        try:
            btn.set_enabled(True)
        except (tk.TclError, AttributeError):
            pass

        if not ok:
            try:
                btn.set_text("✕ Не скачалось")
            except (tk.TclError, AttributeError):
                pass
            messagebox.showerror(
                APP_TITLE,
                "Не удалось скачать обновление.\n"
                f"Пробовали источники: {source_label or '?'}\n\n"
                "Попробуй позже или скачай вручную по ссылке.",
            )
            info = getattr(self, "_pending_update", None)
            url = info.primary_url if info else None
            if url:
                webbrowser.open(url)
            return

        info = getattr(self, "_pending_update", None)
        notes = info.notes if info else ""
        version = info.version if info else ""
        src_hint = f"\nИсточник: {source_label}\n" if source_label else ""
        msg = (
            f"Обновление {version} скачано.\n"
            f"{src_hint}\n"
            "Сейчас приложение закроется, обновится и перезапустится.\n"
            "Сохрани важное перед продолжением."
        )
        if notes:
            msg += f"\n\nЧто нового:\n{notes}"

        if not messagebox.askyesno(APP_TITLE, msg):
            try:
                label = f"⬇ Установить {version}" if version else "⬇ Установить"
                btn.set_text(label)
            except (tk.TclError, AttributeError):
                pass
            return

        if schedule_replace_and_restart(dest):
            self._closed = True
            self._stop_event.set()
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            return

        try:
            btn.set_text("✕ Не удалось обновить")
        except (tk.TclError, AttributeError):
            pass
        messagebox.showerror(
            APP_TITLE,
            "Не удалось запустить обновление.\n"
            "Закрой программу и вручную замени .exe на скачанный:\n"
            f"{dest}",
        )
