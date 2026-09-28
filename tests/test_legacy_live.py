#!/usr/bin/env python3
"""Живой интеграционный тест legacy-tkinter клиента (v1.5.4).

Полный круг БЕЗ заглушек сети:
  1. Поднимает настоящий RelayServer (127.0.0.1, свободный порт).
  2. «Хост» (через lib.client) шлёт сообщение и заливает файл.
  3. Запускает настоящее окно RelayApp (tkinter), подключается как гость —
     тем же кодом, что жмёт кнопку «Подключиться».
  4. Проверяет: сообщение хоста отрисовано в чате; легаси ОТПРАВЛЯЕТ своё
     сообщение (хост видит его в poll_events); легаси заливает СВОЙ файл
     (хост видит его в list_files); легаси скачивает файл хоста с витрины
     (_download_file_by_id) — файл на диске, размер верный, в строке
     прогресса появляется скорость («/с»).
  5. Убеждается, что файл скачался ЦЕЛЫМ (sha256 клиент сверяет сам —
     download_file бросил бы RelayError).

Запуск:  python tests/test_legacy_live.py        (есть дисплей)
         xvfb-run -a python tests/test_legacy_live.py   (headless)

Требует дисплей/xvfb; без него — SKIP (exit 0), чтобы не ломать автопрогон.
Заглушки: messagebox (никаких модальных окон), save/load_settings
(не трогаем реальные настройки), check_for_updates (без сети).
"""
import hashlib
import os
import socket
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StubMessageBox:
    """Ловит все модальные окна — вместо них события в лог."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def showinfo(self, title, msg, **kw):
        self.calls.append(("info", str(msg)))

    def showerror(self, title, msg, **kw):
        self.calls.append(("error", str(msg)))

    def showwarning(self, title, msg, **kw):
        self.calls.append(("warning", str(msg)))


def pump(root: tk.Tk, seconds: float, on_frame=None) -> None:
    """Крутит event-loop tkinter заданное время."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return  # окно уничтожено
        if on_frame is not None:
            on_frame()
        time.sleep(0.02)


def pump_until(root: tk.Tk, cond, timeout: float, on_frame=None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        pump(root, 0.1, on_frame)
    return cond()


def all_label_texts(root: tk.Misc) -> str:
    """Все тексты виджетов: Label.text + Canvas-элементы с текстом
    (пузыри чата рисуются текстом на tk.Canvas — проверяем и их)."""
    out: list[str] = []
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            for child in w.winfo_children():
                stack.append(child)
        except tk.TclError:
            continue
        try:
            if isinstance(w, tk.Label):
                out.append(str(w.cget("text")))
            elif isinstance(w, tk.Canvas):
                for item in w.find_all():
                    try:
                        out.append(str(w.itemcget(item, "text")))
                    except tk.TclError:
                        pass
        except tk.TclError:
            pass
    return "\n".join(out)


def main() -> int:
    try:
        root_probe = tk.Tk()
        root_probe.destroy()
    except tk.TclError:
        print("SKIP: нет дисплея (запусти под xvfb-run -a)")
        return 0

    # ── заглушки ДО создания окна ────────────────────────────────────────
    import legacy.tkinter.app as app_mod
    import legacy.tkinter.chat_ui as chat_ui_mod
    import legacy.tkinter.settings_ui as settings_mod
    import legacy.tkinter.tabs_ui as tabs_mod
    from lib import client
    from lib.relay_server import RelayServer

    stub = StubMessageBox()
    app_mod.messagebox = stub            # type: ignore[assignment]
    tabs_mod.messagebox = stub           # type: ignore[assignment]
    chat_ui_mod.messagebox = stub        # type: ignore[assignment]
    settings_mod.messagebox = stub       # type: ignore[assignment]
    app_mod.save_settings = lambda *a, **k: None   # type: ignore[assignment]
    app_mod.load_settings = dict             # type: ignore[assignment]
    app_mod.check_for_updates = lambda *a, **k: None  # type: ignore[assignment]

    with tempfile.TemporaryDirectory(prefix="frl_live_") as tmp:
        tmp_path = Path(tmp)
        server_data = tmp_path / "server_data"
        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir(parents=True)
        server_data.mkdir(parents=True)

        # ── 1. настоящий сервер ──────────────────────────────────────────
        port = free_port()
        server = RelayServer(server_data, host_name="host", access_key="")
        check("сервер стартовал", server.start("127.0.0.1", port), f"порт {port}")
        base_url = f"http://127.0.0.1:{port}"

        # ── 2. хост: сообщение + файл ────────────────────────────────────
        HOST_TEXT = "привет от хоста — скачай файл"
        client.send_text(base_url, "host", HOST_TEXT)

        up_file = tmp_path / "razdacha.bin"
        payload = os.urandom(3_500_000)  # 3.5МБ: несколько прогресс-тиков
        up_file.write_bytes(payload)
        _seq, file_id = client.send_file(base_url, "host", up_file)
        check("файл хоста залит (file_id получен)", bool(file_id), str(file_id))

        # ── 3. настоящее окно легаси, подключение как гость ─────────────
        app = app_mod.RelayApp(tk.Tk())
        root = app.root
        try:
            root.geometry("1100x700+0+0")
            root.update()

            app.name_var.set("friend")
            app.key_var.set("")
            app.is_host_var.set(False)
            app.host_url_var.set(base_url)
            app.settings["download_dir"] = str(dl_dir)
            app.settings["check_updates"] = False
            if hasattr(app, "download_dir_var"):
                app.download_dir_var.set(str(dl_dir))

            app._on_connect_click()  # тот же путь, что кнопка «Подключиться»
            connected = pump_until(root, lambda: app.connected, 20)
            check("легаси подключился к серверу", connected)

            # ── 4. сообщение хоста дошло и отрисовано ────────────────────
            seen = pump_until(
                root,
                lambda: HOST_TEXT in all_label_texts(root),
                20,
            )
            check("сообщение хоста отрисовано в чате легаси", seen)

            # ── 5. легаси пишет ответ — хост должен его увидеть ──────────
            REPLY = "привет от легаси, качаю!"
            app.msg_var.set(REPLY)
            app._send_text()
            got_reply = False
            deadline = time.monotonic() + 15
            since = 0
            while time.monotonic() < deadline and not got_reply:
                pump(root, 0.1)
                try:
                    res = client.poll_events(base_url, since, "", "host")
                    since = res.get("next_since", since)
                    for ev in res.get("events", []):
                        if (ev.get("kind") == "text" and ev.get("from") == "friend"
                                and REPLY in str(ev.get("text", ""))):
                            got_reply = True
                except Exception:
                    pass
            check("сообщение легаси дошло до хоста", got_reply)

            # ── 6. легаси заливает СВОЙ файл (отдаёт файлы) ──────────────
            up_from_legacy = tmp_path / "ot_legasi.bin"
            legacy_payload = os.urandom(2_500_000)
            up_from_legacy.write_bytes(legacy_payload)
            app._send_file_bg(up_from_legacy)  # без диалога, напрямую
            legacy_file_seen = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not legacy_file_seen:
                pump(root, 0.1)
                try:
                    for f in client.list_files(base_url, "host"):
                        if f.get("name") == "ot_legasi.bin":
                            legacy_file_seen = True
                except Exception:
                    pass
            check("файл легаси появился на витрине хоста", legacy_file_seen)

            # ── 7. легаси скачивает файл хоста с витрины ─────────────────
            # localhost качает 3.5МБ быстрее одного кадра UI — чтобы честно
            # проверить путь worker → очередь → лейбл, замедляем обёрткой:
            # два синтетических прогресса с паузами ВОКРУГ настоящего скачивания.
            real_dl = tabs_mod.client.download_file

            def slow_dl(base_url_, fid_, dest_, key_, progress_cb=None, name="", **kw):
                if progress_cb:
                    progress_cb(500_000, 3_500_000, 0.5)
                    time.sleep(0.15)
                    progress_cb(1_200_000, 3_500_000, 0.9)
                    time.sleep(0.15)
                return real_dl(base_url_, fid_, dest_, key_,
                               progress_cb=progress_cb, name=name, **kw)

            tabs_mod.client.download_file = slow_dl  # type: ignore[assignment]

            progress_seen: list[str] = []

            def grab_progress():
                try:
                    txt = app.files_download_status.cget("text")
                except tk.TclError:
                    return
                if txt:
                    progress_seen.append(txt)

            app._download_file_by_id(file_id, "razdacha.bin")
            dest = dl_dir / "razdacha.bin"
            downloaded = pump_until(root, lambda: dest.exists(), 30, grab_progress)

            check("файл скачался на диск (целый — иначе был бы RelayError)",
                  downloaded)
            if downloaded:
                sha_ok = hashlib.sha256(dest.read_bytes()).hexdigest() == \
                    hashlib.sha256(payload).hexdigest()
                check("sha256 скачанного совпал", sha_ok)
            # детерминированная проверка строки прогресса (скорость).
            # Сначала дренируем очередь (там мог висеть финальный «»-клир из
            # finally воркера — он стёр бы текст при следующем pump), потом
            # дёргаем обработчик напрямую — configure синхронный.
            pump(root, 0.6)
            app._handle_tab_ui_event("files_download_progress",
                                     "↓ 42% · 9.9МБ/с · тест")
            shown = str(app.files_download_status.cget("text"))
            check("строка прогресса показывает скорость",
                  "МБ/с" in shown and "42%" in shown, shown)
            check("в живом скачивании прогресс мелькал",
                  any("%" in t and "/с" in t for t in progress_seen),
                  f"{len(progress_seen)} кадров с текстом")

            # никаких модальных ошибок за всю сессию
            errors = [m for kind, m in stub.calls if kind == "error"]
            check("без ошибочных модалок за сессию", not errors, str(errors[:2]))
        finally:
            try:
                if app.connected:
                    app._disconnect()
                pump(root, 0.3)
                root.destroy()
            except tk.TclError:
                pass
            server.stop()

    print(f"\nИтог: {OK} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
