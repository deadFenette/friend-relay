from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox

from legacy.tkinter import theme
from legacy.tkinter.chat_ui import ChatUIMixin
from legacy.tkinter.settings_ui import SettingsUIMixin
from legacy.tkinter.tabs_ui import TabsUIMixin
from lib import client
from lib.client import RelayError
from lib.constants import (
    APP_TITLE,
    AVATAR_CHAT_SUBSAMPLE,
    CHAT_TAIL_ON_CONNECT,
    DATA_DIR,
    FILE_PREVIEW_MAX_BYTES,
    HOST_DATA_DIR_NAME,
    MAX_UI_EVENTS_PER_TICK,
    POLL_INTERVAL_MS,
    POLL_RETRY_BACKOFF_MS,
)
from lib.file_kind import is_previewable_image, make_preview_image
from lib.formatters import fmt_size, fmt_speed
from lib.relay_server import RelayServer
from lib.storage import load_settings, save_settings
from lib.updater import check_for_updates


class RelayApp(TabsUIMixin, SettingsUIMixin, ChatUIMixin):
    """Главное окно. Координирует UI-миксины, сеть и очередь событий.

    Ответственность распределена по миксинам (MRO сверху вниз):
    - TabsUIMixin — система вкладок, ЛС, Боты, хуки жизненного цикла вкладок
    - SettingsUIMixin — карточка настроек, аккордеоны, подключение/хост
    - ChatUIMixin — рендер общего чата, пульсация, редактирование, удаление

    Здесь остаётся только общая координация: init-состояние, цикл поллинга,
    отправка/скачивание, очередь UI-событий, аватарки, проверка обновлений.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("720x760")
        self.root.configure(bg=theme.BG)
        self.root.minsize(520, 560)

        # Общее состояние (миксины дополняют своё)
        self.settings = load_settings()
        self.relay: RelayServer | None = None
        self.base_url = ""
        self.access_key = ""
        self.my_name = ""
        self.since = 0
        self._min_loaded_seq = 0
        self._connected_at = 0.0
        self._last_poll_ms: int | None = None
        self.connected = False
        self.consecutive_poll_errors = 0
        self._window_focused = True
        self._unread = False
        self._last_sender: str | None = None
        self._online_users: set = set()
        self._loading_history = False

        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._file_ids_shown: set = set()
        self._progress_rows: dict = {}
        self._row_by_seq: dict[int, tk.Frame] = {}
        self._downloaded_paths: dict[str, str] = {}
        self._file_cards: dict[str, object] = {}
        self._file_preview_inflight: set = set()
        self._reply_to: dict | None = None
        self._closed = False
        self._after_id: str | None = None

        self._avatar_images: dict[str, tk.PhotoImage | None] = {}
        self._avatar_fetch_inflight: set = set()

        self._typing_users: dict[str, float] = {}
        self._typing_indicator: tk.Label | None = None
        self._send_animation_state = {"active": False, "position": 0, "after_id": None}

        self._sound_enabled = self.settings.get("sound_on_message", True)
        self._last_notification_time = 0
        self._sound_volume = self.settings.get("sound_volume", 0.5)

        # -- состояние вкладок / ЛС / ботов (TabsUIMixin) --
        self._init_tab_state()

        # Сборка UI через миксины
        self._build_connection_area()
        self._build_online_strip()
        self._build_tabs()
        self._build_chat_tab()
        self._build_dm_tab()
        self._build_files_tab()
        self._build_bots_tab()

        # Восстанавливаем последнюю активную вкладку
        last_tab = self.settings.get("last_active_tab", self.TAB_CHAT)
        if last_tab in self._tab_frames:
            self._switch_tab(last_tab)
        else:
            self._switch_tab(self.TAB_CHAT)

        # Lifecycle окна
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<FocusIn>", self._on_focus_in)
        self.root.bind("<FocusOut>", self._on_focus_out)
        self._after_id = self.root.after(120, self._drain_ui_queue)

        self._load_own_avatar_preview()
        self.root.after(1000, self._update_typing_indicator)

        if self.settings.get("auto_connect") and self.settings.get("name"):
            self.root.after(400, self._on_connect_click)

        if self.settings.get("check_updates", True):
            self.root.after(2000, self._check_updates_bg)

    # ------------------------------------------------------------------
    # Проверка обновлений
    # ------------------------------------------------------------------
    def _check_updates_bg(self) -> None:
        url = self.settings.get("update_manifest_url", "")
        threading.Thread(target=self._check_updates_thread, args=(url,), daemon=True).start()

    def _check_updates_thread(self, url: str) -> None:
        info = check_for_updates(url)
        if info:
            self._ui_queue.put(("update_available", info))

    # ------------------------------------------------------------------
    # Аватарки
    # ------------------------------------------------------------------
    def _upload_avatar_bg(self, png_bytes: bytes) -> None:
        try:
            client.upload_avatar(self.base_url, self.my_name, png_bytes, self.access_key)
        except RelayError:
            pass

    def _get_avatar_image(self, name: str) -> tk.PhotoImage | None:
        if name in self._avatar_images:
            return self._avatar_images[name]
        if self.connected and name not in self._avatar_fetch_inflight:
            self._avatar_fetch_inflight.add(name)
            threading.Thread(target=self._fetch_avatar_bg, args=(name,), daemon=True).start()
        return None

    def _fetch_avatar_bg(self, name: str) -> None:
        data = client.fetch_avatar(self.base_url, name, self.access_key)
        self._ui_queue.put(("avatar_fetched", (name, data)))

    # ------------------------------------------------------------------
    # Превью файлов
    # ------------------------------------------------------------------
    def _request_file_preview(self, file_id: str, filename: str, size: int) -> None:
        if (not self.connected or file_id in self._file_preview_inflight
                or file_id not in self._file_cards):
            return
        if not is_previewable_image(filename, size, FILE_PREVIEW_MAX_BYTES):
            return
        self._file_preview_inflight.add(file_id)
        threading.Thread(target=self._fetch_file_preview_bg,
                         args=(file_id, filename), daemon=True).start()

    def _fetch_file_preview_bg(self, file_id: str, filename: str) -> None:
        try:
            data = client.fetch_file_bytes(self.base_url, file_id, self.access_key,
                                           max_bytes=FILE_PREVIEW_MAX_BYTES)
            self._ui_queue.put(("chat_file_preview", (file_id, data)))
        except RelayError:
            pass
        finally:
            self._ui_queue.put(("file_preview_done", file_id))

    def _apply_file_preview(self, file_id: str, data: bytes) -> None:
        handle = self._file_cards.get(file_id)
        if not handle or not data:
            return
        img = make_preview_image(data, self.root)
        if img:
            handle.set_preview(img)

    # ------------------------------------------------------------------
    # Звук и ввод
    # ------------------------------------------------------------------
    def _on_key_release(self, event) -> None:
        if not self.connected:
            return
        current_time = time.time()
        if (current_time - self._last_typing_send > 2.0 and
                event.keysym not in ("Return", "BackSpace", "Delete", "Tab")):
            self._last_typing_send = current_time

    def _play_notification_sound(self, event_type: str) -> None:
        if not self._sound_enabled:
            return
        current_time = time.time()
        if current_time - self._last_notification_time < 2.0:
            return
        self._last_notification_time = current_time
        try:
            if self._sound_volume > 0:
                self.root.bell()
        except Exception:
            pass

    def _update_sound_settings(self) -> None:
        self._sound_enabled = self.sound_var.get()
        self._sound_volume = self.volume_var.get()

    def _on_volume_change(self, val) -> None:
        percent = int(float(val) * 100)
        self.volume_label.configure(text=f"{percent}%")
        self._sound_volume = float(val)

    # ------------------------------------------------------------------
    # Подключение и поллинг
    # ------------------------------------------------------------------
    def _on_connect_click(self) -> None:
        if self.connected:
            self._disconnect()
            return

        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Сначала введи своё имя")
            return
        self.my_name = name
        self.access_key = self.key_var.get().strip()

        if self.is_host_var.get():
            try:
                port = int(self.port_var.get().strip())
            except ValueError:
                messagebox.showwarning(APP_TITLE, "Порт должен быть числом")
                return
            try:
                max_size_bytes = int(self.max_size_var.get().strip()) * 1024 * 1024
                if max_size_bytes <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(APP_TITLE, "Лимит файла должен быть положительным числом (в МБ)")
                return
            data_dir = DATA_DIR / HOST_DATA_DIR_NAME
            self.relay = RelayServer(data_dir, host_name=name, access_key=self.access_key,
                                     max_file_size=max_size_bytes)
            ok = self.relay.start("0.0.0.0", port)
            if not ok:
                self.relay = None
                messagebox.showerror(APP_TITLE, f"Не удалось запустить раздачу на порту {port} "
                                                f"(занят другой программой?)")
                return
            self.base_url = f"http://127.0.0.1:{port}"
            self._append_system(
                f"Раздача запущена на порту {port}. Друзьям дай свой адрес ZeroTier/"
                f"Tailscale/Porthole + этот порт, например http://100.x.x.x:{port}.")
        else:
            host_url = self.host_url_var.get().strip().rstrip("/")
            if not host_url.startswith("http://") and not host_url.startswith("https://"):
                messagebox.showwarning(APP_TITLE, "Адрес хоста должен начинаться с http://")
                return
            self.base_url = host_url

        self.settings.update(self._collect_settings())
        save_settings(self.settings)

        self._set_status("Подключаюсь...", theme.STATUS_CONNECTING, "", animate=True)
        self.connect_btn.set_enabled(False)
        threading.Thread(target=self._try_ping_then_start, daemon=True).start()

    def _try_ping_then_start(self) -> None:
        try:
            info = client.ping(self.base_url)
        except RelayError as e:
            self._ui_queue.put(("connect_failed", str(e)))
            return
        try:
            t0 = time.monotonic()
            result = client.poll_events(self.base_url, 0, self.access_key, self.my_name,
                                        tail=CHAT_TAIL_ON_CONNECT)
            rtt_ms = int((time.monotonic() - t0) * 1000)
        except RelayError as e:
            self._ui_queue.put(("connect_failed", f"Не подошёл ключ доступа: {e}"))
            return
        self._ui_queue.put(("connect_ok", (info, result)))
        self._ui_queue.put(("poll_rtt", rtt_ms))

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.is_set():
                break
            try:
                t0 = time.monotonic()
                result = client.poll_events(self.base_url, self.since, self.access_key, self.my_name)
                rtt_ms = int((time.monotonic() - t0) * 1000)
                self.since = result["next_since"]
                if result["events"]:
                    self._ui_queue.put(("events", result["events"]))
                self._ui_queue.put(("online", result.get("online", [])))
                self._ui_queue.put(("poll_rtt", rtt_ms))
                if self.consecutive_poll_errors:
                    self._ui_queue.put(("poll_recovered", None))
                self.consecutive_poll_errors = 0
                if self._stop_event.wait(POLL_INTERVAL_MS / 1000):
                    break
            except RelayError as e:
                self.consecutive_poll_errors += 1
                self._ui_queue.put(("poll_error", str(e)))
                if self._stop_event.wait(POLL_RETRY_BACKOFF_MS / 1000):
                    break

    # ------------------------------------------------------------------
    # Отправка и скачивание
    # ------------------------------------------------------------------
    def _send_text(self) -> None:
        if not self.connected:
            return
        text = self.msg_var.get().strip()
        if not text:
            return

        if text.startswith("!"):
            self.msg_var.set("")
            self._send_bot_command(text)
            return

        if self._reply_to:
            quote = self._reply_to.get("preview", "")
            sender = self._reply_to.get("sender", "?")
            text = f"↩ {sender}: «{quote}»\n{text}"
            self._clear_reply()
        self.msg_var.set("")
        self._start_send_animation()
        threading.Thread(target=self._send_text_bg, args=(text,), daemon=True).start()

    def _send_bot_command(self, text: str) -> None:
        parts = text.split()
        command = parts[0] if parts else ""
        args = parts[1:] if len(parts) > 1 else []
        bot_id = "night_shift"
        threading.Thread(target=self._send_bot_command_bg,
                        args=(bot_id, command, args), daemon=True).start()

    def _send_bot_command_bg(self, bot_id: str, command: str, args: list[str]) -> None:
        try:
            from lib.client import send_bot_command
            send_bot_command(self.base_url, bot_id, command, args,
                             self.my_name, self.access_key)
            self._ui_queue.put(("send_done", None))
        except RelayError as e:
            self._ui_queue.put(("send_error", f"Ошибка бота: {e}"))
            self._ui_queue.put(("send_done", None))

    def _send_text_bg(self, text: str) -> None:
        try:
            client.send_text(self.base_url, self.my_name, text, self.access_key)
            self._ui_queue.put(("send_done", None))
        except RelayError as e:
            self._ui_queue.put(("send_error", f"Не отправилось: {e}"))
            self._ui_queue.put(("send_done", None))

    def _send_file_dialog(self) -> None:
        if not self.connected:
            messagebox.showinfo(APP_TITLE, "Сначала подключись")
            return
        path = filedialog.askopenfilename(title="Выбери файл для отправки")
        if not path:
            return
        self.file_btn.set_enabled(False)
        threading.Thread(target=self._send_file_bg, args=(Path(path),), daemon=True).start()

    def _send_file_bg(self, path: Path) -> None:
        token = uuid.uuid4().hex
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        self._ui_queue.put(("upload_start", (token, path.name, size)))

        def progress(sent: int, total: int, elapsed: float) -> None:
            pct = (sent * 100 // total) if total else 0
            speed = fmt_speed(sent / elapsed) if elapsed > 0.05 else "..."
            detail = f"↑ {path.name}   ({fmt_size(sent)} / {fmt_size(total)}, {speed})"
            self._ui_queue.put(("upload_progress", (token, pct, detail)))

        try:
            client.send_file(self.base_url, self.my_name, path, self.access_key, progress_cb=progress)
            self._ui_queue.put(("upload_done", token))
        except RelayError as e:
            self._ui_queue.put(("upload_failed", (token, f"✕ Не отправился файл '{path.name}': {e}")))
        finally:
            self._ui_queue.put(("file_send_done", None))

    def _download_file(self, file_id: str, suggested_name: str, handle) -> None:
        if not self.connected:
            return
        initial_dir = self.download_dir_var.get().strip() or None
        dest = filedialog.asksaveasfilename(initialfile=suggested_name, initialdir=initial_dir)
        if not dest:
            return
        handle.set_progress(0.0, "Подготовка...")
        threading.Thread(target=self._download_file_bg,
                         args=(file_id, Path(dest), handle), daemon=True).start()

    def _download_file_bg(self, file_id: str, dest: Path, handle) -> None:
        def progress(received: int, total: int | None, elapsed: float) -> None:
            speed = fmt_speed(received / elapsed) if elapsed > 0.05 else "..."
            if total:
                pct = received * 100 // total
                label = f"↓ {pct}%  {speed}"
            else:
                pct = 0
                label = f"↓ {fmt_size(received)}  {speed}"
            self._ui_queue.put(("download_progress", (handle, pct, label)))

        try:
            client.download_file(self.base_url, file_id, dest, self.access_key, progress_cb=progress)
            self._ui_queue.put(("download_ok", (handle, str(dest))))
            self._ui_queue.put(("file_downloaded", (file_id, str(dest))))
        except RelayError as e:
            self._ui_queue.put(("download_failed", (handle, f"Не скачалось: {e}")))

    # ------------------------------------------------------------------
    # Очередь UI-событий
    # ------------------------------------------------------------------
    def _handle_one_ui_event(self, kind: str, payload) -> None:
        # 1) Сначала — вкладочные события (TabsUIMixin)
        if self._handle_tab_ui_event(kind, payload):
            return

        # 2) Общие события
        if kind == "connect_ok":
            self._on_connect_ok(payload)
        elif kind == "connect_failed":
            self._on_connect_failed(payload)
        elif kind == "events":
            for ev in payload:
                self._render_event(ev, defer_scroll=True)
            self._chat_scroll_to_bottom()
            self._trim_old_rows()
        elif kind == "old_event":
            self._render_event(payload, defer_scroll=True, prepend=True)
        elif kind == "online":
            self._update_online_strip(payload)
        elif kind == "poll_rtt":
            self._last_poll_ms = payload
            if self.connected:
                self.status_details.configure(text=self._get_connection_details())
        elif kind == "update_available":
            self._show_update_banner(payload)
        elif kind == "update_progress":
            self._handle_update_progress(payload)
        elif kind == "update_downloaded":
            ok, source_label, dest = payload
            self._handle_update_downloaded(ok, source_label, dest)
        elif kind == "poll_error":
            if self.consecutive_poll_errors >= 2:
                self._set_status(f"⚠ хост не отвечает, пробую снова ({payload})",
                                 theme.STATUS_ERROR)
        elif kind == "poll_recovered":
            self._set_status(self._connected_status_text(), theme.STATUS_CONNECTED)
        elif kind == "send_error":
            self._append_error(payload)
        elif kind == "send_done":
            self._stop_send_animation()
            self.connect_btn.set_text("Отключиться")
        elif kind == "file_send_done":
            self.file_btn.set_enabled(True)
        elif kind == "upload_start":
            token, name, size = payload
            self._start_transient_progress(token, name, size)
        elif kind == "upload_progress":
            token, pct, text = payload
            self._update_transient_progress(token, pct, text)
        elif kind == "upload_done":
            self._remove_transient_progress(payload)
        elif kind == "upload_failed":
            self._fail_transient_progress(*payload)
        elif kind == "download_progress":
            handle, pct, label = payload
            try:
                handle.set_progress(float(pct), label)
            except (tk.TclError, AttributeError):
                pass
        elif kind == "download_ok":
            handle, dest_path = payload
            try:
                handle.set_progress(None, "")
            except (tk.TclError, AttributeError):
                pass
            self._append_system(f"Скачано: {dest_path}")
        elif kind == "file_downloaded":
            file_id, dest_path = payload
            self._downloaded_paths[file_id] = dest_path
        elif kind == "message_deleted":
            self._remove_row_by_seq(payload)
        elif kind == "typing":
            self._handle_typing_event(payload)
        elif kind == "download_failed":
            handle, err = payload
            try:
                handle.set_progress(None, "")
            except (tk.TclError, AttributeError):
                pass
            self._append_error(err)
        elif kind == "chat_file_preview":
            file_id, data = payload
            self._apply_file_preview(file_id, data)
        elif kind == "file_preview_done":
            self._file_preview_inflight.discard(payload)
        elif kind == "avatar_fetched":
            name, data = payload
            self._avatar_fetch_inflight.discard(name)
            if data:
                try:
                    self._avatar_images[name] = tk.PhotoImage(
                        data=data, master=self.root).subsample(AVATAR_CHAT_SUBSAMPLE)
                except tk.TclError:
                    self._avatar_images[name] = None
            else:
                self._avatar_images[name] = None

    def _drain_ui_queue(self) -> None:
        if self._closed:
            return
        handled = 0
        queue_empty = False
        try:
            while handled < MAX_UI_EVENTS_PER_TICK:
                kind, payload = self._ui_queue.get_nowait()
                self._handle_one_ui_event(kind, payload)
                handled += 1
        except queue.Empty:
            queue_empty = True

        if queue_empty:
            self._after_id = self.root.after(120, self._drain_ui_queue)
        else:
            self._after_id = self.root.after(0, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # Онлайн-полоса и connect_ok/failed
    # ------------------------------------------------------------------
    def _update_online_strip(self, names: list) -> None:
        others = [n for n in names if n != self.my_name]
        if not names:
            text = ""
        elif self.my_name in names:
            text = "🟢 На связи: я" + (f", {', '.join(others)}" if others else " (больше пока никого)")
        else:
            text = "🟢 На связи: " + ", ".join(names)
        self.online_label.configure(text=text)
        self._online_users = set(names)
        if self.connected:
            self.status_details.configure(text=self._get_connection_details())
        for n in names:
            if n != self.my_name:
                self._get_avatar_image(n)

    def _on_connect_failed(self, error: str) -> None:
        self.connect_btn.set_enabled(True)
        self._set_status("Не удалось подключиться", theme.ERROR)
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        messagebox.showerror(APP_TITLE, f"Не достучался до хоста: {error}\n\n"
                                      f"Проверь адрес и порт, и что у хоста запущена раздача "
                                      f"и включён ZeroTier/Tailscale/Porthole.")

    # ------------------------------------------------------------------
    # Focus и подгрузка старых сообщений
    # ------------------------------------------------------------------
    def _on_focus_in(self, event=None) -> None:
        self._window_focused = True
        if self._unread:
            self._unread = False
            self.root.title(APP_TITLE)

    def _on_focus_out(self, event=None) -> None:
        self._window_focused = False

    def _check_scroll_top(self) -> None:
        if not self.connected or self._loading_history:
            return
        top = self.chat_canvas.yview()[0]
        if top > 0.01:
            return
        if self._min_loaded_seq <= 1:
            return
        self._loading_history = True
        threading.Thread(target=self._load_old_messages, daemon=True).start()

    def _load_old_messages(self) -> None:
        try:
            from lib.client import poll_events_before
            result = poll_events_before(
                self.base_url,
                self._min_loaded_seq,
                self.access_key,
                self.my_name,
                count=50,
            )
            events = result.get("events", [])
            if events:
                events_sorted = sorted(events, key=lambda e: e.get("seq", 0))
                self._min_loaded_seq = events_sorted[0].get("seq", 0)
                for ev in events_sorted:
                    self._ui_queue.put(("old_event", ev))
        except Exception as e:
            print(f"Ошибка загрузки старых сообщений: {e}")
        finally:
            self._loading_history = False


def run() -> None:
    root = tk.Tk()
    RelayApp(root)
    root.mainloop()
