"""Стабильность v3.5.0: faulthandler + глобальные excepthook'и + watchdog.

Урок v3.4.1: «виснет намертво и не найти почему» — это не диагноз, а
отсутствие диагностики. Этот модуль превращает любой краш или зависание
в файл в logs/ со стеками всех потоков.

Три инструмента:
1. enable_faulthandler()   — сегфолты/падения Qt пишут стек в logs/crash.log.
2. install_excepthooks()   — непойманные исключения (главный поток и ЛЮБОЙ
                             фоновый) пишутся в logs/crash_<дата>.log и
                             опционально показываются в GUI (toast).
3. HangWatchdog            — GUI-поток «бьёт сердцем» (heartbeat из QTimer),
                             монитор-поток следит: сердце молчит дольше
                             stuck_after_s → дамп стеков ВСЕХ потоков в
                             logs/hang_<дата>.log. Когда окно оживает,
                             колбэк on_resume сообщает, сколько висели.

Модуль НЕ импортирует Qt (правило слоёв): QTimer/heartbeat связываются
в точках входа (main_qt.py, server_gui.py, server_main.py).
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


# ── 1. faulthandler ──────────────────────────────────────────────────────

def enable_faulthandler(logs_dir: Path) -> Path | None:
    """Включает faulthandler: сегфолт/аборт Qt → стек в logs/crash.log.

    Файл открывается на ДОБАВЛЕНИЕ и живёт до конца процесса (закрывать
    нельзя — faulthandler пишет в него асинхронно из обработчика сигнала).
    Возвращает путь или None, если не удалось (не критично)."""
    try:
        _ensure_dir(logs_dir)
        f = open(logs_dir / "crash.log", "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== процесс запущен {_stamp()} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        return f.name
    except Exception:
        return None


# ── 2. глобальные excepthook'и ───────────────────────────────────────────

def _write_report(logs_dir: Path, prefix: str, header: str, text: str) -> Path | None:
    try:
        _ensure_dir(logs_dir)
        path = logs_dir / f"{prefix}_{_stamp()}.log"
        path.write_text(f"{header}\n\n{text}\n", encoding="utf-8")
        return path
    except Exception:
        return None


def install_excepthooks(
    logs_dir: Path,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Ловит непойманные исключения в главном потоке и в ЛЮБОМ фоновом.

    Пишет полный стек в logs/crash_<дата>.log; если передан on_error —
    зовёт его коротким человекочитаемым сообщением (GUI показывает toast).
    Старые хуки сохраняются и вызываются после наших."""

    old_sys = sys.excepthook
    old_threading = threading.excepthook

    def _handle(where: str, exc_type: type, exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        path = _write_report(
            logs_dir,
            "crash",
            f"Непойманное исключение в {where} · {_stamp()} · "
            f"python {sys.version.split()[0]}",
            text,
        )
        short = f"Краш ({where}): {exc_type.__name__}: {exc}"
        if path is not None:
            short += f" — стек: {path.name}"
        try:
            print(short, file=sys.stderr)
        except Exception:
            pass
        if on_error is not None:
            try:
                on_error(short)
            except Exception:
                pass

    def _sys_hook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            _handle("главный поток", exc_type, exc, tb)
        finally:
            old_sys(exc_type, exc, tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "?"
        try:
            _handle(
                f"поток «{thread_name}»",
                args.exc_type,
                args.exc_value if args.exc_value is not None else Exception("?"),
                args.exc_traceback,
            )
        finally:
            if old_threading is not None:
                old_threading(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


# ── 3. watchdog GUI-потока ───────────────────────────────────────────────

class HangWatchdog:
    """Следит за «сердцебиением» потока-хозяина (обычно GUI-поток).

    Хозяин регулярно зовёт heartbeat() (в Qt — QTimer из GUI-потока).
    Монитор-поток раз в check_every_s проверяет возраст последнего удара.
    Если сердце молчит дольше stuck_after_s — ОДИН раз за эпизод пишем
    дамп стеков всех потоков в logs/hang_<дата>.log (это и есть ответ на
    вопрос «почему висит», который раньше было не достать). Когда хозяин
    оживает — зовём on_resume(seconds_stuck, dump_path): GUI может
    показать toast «окно не отвечало 16.4с, стеки сохранены».

    Для headless-сервера (server_main.py) heartbeat кормит поток-зонд,
    дёргающий лёгкий метод фасада: если ядро сервера встало на локе —
    зонд тоже встаёт, сердцебиение пропадает, дамп пишется."""

    def __init__(
        self,
        logs_dir: Path,
        stuck_after_s: float = 15.0,
        check_every_s: float = 2.0,
    ) -> None:
        self._logs_dir = logs_dir
        self._stuck_after_s = stuck_after_s
        self._check_every_s = check_every_s
        self._last_beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dumped = False          # дамп писали в текущем эпизоде?
        self._episode_started = 0.0   # monotonic начала эпизода зависания
        self._lock = threading.Lock()

    # -- хозяйский поток ----------------------------------------------------

    def heartbeat(self) -> None:
        """Одно «удар сердца». Вызывать ТОЛЬКО из наблюдаемого потока."""
        with self._lock:
            self._last_beat = time.monotonic()
            if self._dumped:
                # Ожили — эпизод завершён (сообщит монитор на следующем тике)
                pass

    # -- монитор ------------------------------------------------------------

    def start(
        self,
        on_resume: Callable[[float, Path | None], None] | None = None,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _monitor() -> None:
            while not self._stop.wait(self._check_every_s):
                try:
                    with self._lock:
                        stuck = time.monotonic() - self._last_beat
                        dumped = self._dumped
                    if stuck > self._stuck_after_s:
                        if not dumped:
                            with self._lock:
                                if not self._dumped:
                                    self._dumped = True
                                    self._episode_started = time.monotonic()
                            self._dump_stacks()
                        continue
                    # Сердце бьётся
                    if dumped:
                        # только что ожили после эпизода
                        with self._lock:
                            self._dumped = False
                            episode = time.monotonic() - self._episode_started
                        self._log_resume(episode)
                        if on_resume is not None:
                            try:
                                on_resume(episode, self._last_dump)
                            except Exception:
                                pass
                except Exception:
                    pass  # монитор не должен умирать сам

        self._thread = threading.Thread(
            target=_monitor, name="hang-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- утилиты -------------------------------------------------------------

    _last_dump: Path | None = None

    def _dump_stacks(self) -> Path | None:
        """Дамп стеков ВСЕХ живых потоков в logs/hang_<дата>.log."""
        try:
            _ensure_dir(self._logs_dir)
            path = self._logs_dir / f"hang_{_stamp()}.log"
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            parts = [
                f"ЗАВИСАНИЕ: главный поток не отвечал дольше "
                f"{self._stuck_after_s:.0f}с · {_stamp()} · pid={os.getpid()}",
                "Ниже стеки всех потоков на момент снятия. Ищи поток, чей "
                "стек указывает на блокирующий вызов (сокет/диск/lock).",
                "",
            ]
            for tid, frame in frames.items():
                name = names.get(tid, f"thread-{tid}")
                parts.append(f"──── поток «{name}» (ident={tid}) ────")
                try:
                    parts.extend(traceback.format_stack(frame))
                except Exception:
                    parts.append("  <стек недоступен>\n")
            path.write_text("\n".join(parts), encoding="utf-8")
            self._last_dump = path
            return path
        except Exception:
            return None

    def _log_resume(self, episode_s: float) -> None:
        try:
            _ensure_dir(self._logs_dir)
            with open(
                self._logs_dir / "hang_history.log", "a", encoding="utf-8"
            ) as f:
                f.write(
                    f"{_stamp()}  поток был занят {episode_s:.1f}с "
                    f"(дамп: {self._last_dump.name if self._last_dump else '—'})\n"
                )
        except Exception:
            pass
