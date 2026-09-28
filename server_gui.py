#!/usr/bin/env python3
"""Friend Relay — GUI СЕРВЕРА (отдельное приложение, без клиента).

    python server_gui.py            или        RUN_SERVER_GUI.bat

Это НЕ PySide6-клиент и НЕ консоль. Это отдельное окно, которое:
  • запускает и останавливает сервер (кнопкой, без командной строки);
  • в реальном времени показывает: кто в сети (и кто печатает), аптайм,
    голос, voxel, каналы, запросы и латентность;
  • управляет музыкой (плей/пауза/дальше/пересканировать, громкость);
  • даёт ссылки-приглашения для друзей (включая ZeroTier-адреса) и
    ключ админа с кнопкой «копировать»;
  • ПОЗВОЛЯЕТ КИКАТЬ участников (сессия аннулируется, в чат уходит
    системное сообщение);
  • показывает живой журнал сервера (тот же файл logs/server.log,
    что пишет headless server_main.py).

Консоль сервера (server_main.py) по дизайну НЕМАЯ: только ошибки.
Вся наглядность живёт ЗДЕСЬ. Телеметрия — тоже отдельное окно
(telemetry_viewer.py), эта программа про управление, а не про счётчики.

Админ = любое имя хоста, какое введёшь («Емеля», «Зая», что угодно). Имя
защищено admin_key (файл relay_data/admin_key.json, переживает рестарты):
занять его извне можно только с ключом, иначе гость входит как «Имя#2»
(дискриминатор как в Discord).
"""
from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── PySide6: проверяем ЧЕСТНО и сразу ─────────────────────────────
# Прежняя версия писала «PySide6 не установлен» на ЛЮБУЮ ошибку импорта —
# даже когда PySide6 стоит, а падает, например, загрузка DLL. Теперь
# показываем реальную причину и что с ней делать.

def _qt_hint(e: Exception) -> str:
    """Подсказка по классу ошибки импорта PySide6."""
    msg = str(e)
    if "DLL load failed" in msg:
        return ("\nЧТО ДЕЛАТЬ (DLL load failed — Windows не может загрузить библиотеку Qt):\n"
                " 1) Поставь системный пакет VC++ Redistributable x64 (бесплатно, минута):\n"
                "    https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
                " 2) Антивирус мог удалить/поломать DLL в site-packages — проверь карантин,\n"
                "    добавь папку проекта в исключения, затем:  pip install --force-reinstall PySide6\n"
                " 3) Если в версии Python есть буква t (3.13t/3.14t, free-threading) — PySide6\n"
                "    с ним не работает: нужен обычный Python (без t) и новый .venv")
    if "No module named 'shiboken6'" in msg:
        return ("\nЧТО ДЕЛАТЬ: пакет повреждён (shiboken6 потерялся):\n"
                "  pip install --force-reinstall PySide6")
    return ("\nЧТО ДЕЛАТЬ:  pip install --force-reinstall PySide6\n"
            "Если не поможет — пришли этот отчёт целиком.")


def _qt_report() -> tuple[bool, str]:
    """Пытается импортировать PySide6 и собирает человекочитаемый отчёт:
    какой python, найден ли PySide6, реальная ошибка импорта с трейсбеком."""
    import importlib.util
    import platform
    import traceback

    ver = platform.python_version()
    try:
        if not sys._is_gil_enabled():        # free-threading сборка (…t)
            ver += " (free-threading)"
    except Exception:
        pass

    lines = [
        f"Python: {sys.executable}",
        f"  версия: {ver} · {platform.system()} {platform.release()}",
    ]
    spec = importlib.util.find_spec("PySide6")
    if spec is None:
        lines += [
            "PySide6: НЕ НАЙДЕН в ЭТОМ интерпретаторе (путь выше).",
            "Обычно значит: pip ставил его для другого Python (системного),",
            "а запускаешь из .venv — или наоборот.",
            "",
            "ЧТО ДЕЛАТЬ — одна команда, именно этим Python:",
            f'  "{sys.executable}" -m pip install PySide6',
        ]
        return False, "\n".join(lines)

    lines.append(f"PySide6 найден: {spec.origin}")
    try:
        import PySide6
        from PySide6 import QtCore
        lines.append(
            f"QtCore: OK (PySide6 {PySide6.__version__}, Qt {QtCore.qVersion()})")
    except Exception as e:
        lines.append(f"QtCore: ОШИБКА — {type(e).__name__}: {e}")
        lines.append(traceback.format_exc(limit=4))
        lines.append(_qt_hint(e))
        return False, "\n".join(lines)
    try:
        from PySide6 import QtGui, QtWidgets  # noqa: F401
        lines.append("QtGui/QtWidgets: OK")
        lines.append("GUI можно запускать:  python server_gui.py")
        return True, "\n".join(lines)
    except Exception as e:
        lines.append(f"QtGui/QtWidgets: ОШИБКА — {type(e).__name__}: {e}")
        lines.append(traceback.format_exc(limit=4))
        lines.append(_qt_hint(e))
        return False, "\n".join(lines)


def _show_fatal_and_wait(report: str) -> None:
    """Показывает отчёт так, чтобы его ТОЧНО увидели (окно больше не
    мигает и не умирает молча): файл + консоль с паузой + MessageBox."""
    try:
        (ROOT / "server_gui_error.log").write_text(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{report}\n",
            encoding="utf-8")
    except OSError:
        pass
    report += ("\n\n(отчёт также записан: server_gui_error.log рядом с server_gui.py)\n"
               "(сервер работает и без GUI:  python server_main.py)")
    try:
        print(report, file=sys.stderr)
    except Exception:
        pass  # pythonw: консоли нет
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, report[-1600:],
                "Friend Relay — Сервер: GUI не запустился",
                0x00050010)   # ICONERROR | SETFOREGROUND | TOPMOST
        except Exception:
            pass
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажми Enter, чтобы закрыть…")
    except (EOFError, KeyboardInterrupt):
        pass


if "--diag" in sys.argv:
    _diag_ok, _diag_text = _qt_report()
    print(_diag_text)
    sys.exit(0 if _diag_ok else 1)

_qt_ok, _qt_report_text = _qt_report()
if not _qt_ok:
    _show_fatal_and_wait(_qt_report_text)
    sys.exit(1)

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lib.constants import DATA_DIR, DEFAULT_PORT, HOST_DATA_DIR_NAME
from lib.net_utils import (
    InterfaceInfo,
    detect_vpn_zt_conflict,
    get_interfaces,
    launch_vpn_zt_fixer,
    run_vpn_diagnose,
)
from lib.net_utils import (
    get_local_ips as local_ips,
)
from lib.relay_server import RelayServer
from lib.server_log import get_logger, setup_server_logging
from lib.storage import load_settings, save_settings

REFRESH_INTERVAL_S = 1.5  # реальное время: раз в 1.5 секунды

# Палитра по дизайну веб-клиента "Studio"
_BG          = "#0c0d11"           # тёплая почти-чёрная база
_SURFACE     = "#14161c"           # панели/карточки
_SURFACE2    = "#1b1e26"           # лёгкий подъём
_SURFACE3    = "#22262f"           # активные поля
_BORDER      = "rgba(255,255,255,0.07)"
_BORDER2     = "rgba(255,255,255,0.13)"
_TEXT        = "#e9e7e2"           # тёплая, не чисто белая
_TEXT_STRONG = "#ffffff"
_TEXT_DIM    = "#9a9aa3"
_TEXT_DIM2   = "#6b6b75"
_ACCENT      = "#ff5b3a"           # тёплый коралл
_ACCENT2     = "#ff8a5a"           # для ховера
_ACCENT_SOFT = "rgba(255,91,58,0.14)"
_SUCCESS     = "#4ade80"
_DANGER      = "#f87171"
_WARNING     = "#fbbf24"
_RADIUS      = 10                  # основной радиус
_RADIUS_SM   = 6                   # маленький радиус
_RADIUS_LG   = 14                  # большой радиус
_MONO        = "'ui-monospace','SF Mono','JetBrains Mono','Cascadia Mono','Menlo','Consolas',monospace"
_SANS        = "'Inter','-apple-system','BlinkMacSystemFont','SF Pro Text','Segoe UI Variable','Segoe UI','Roboto','Ubuntu',system-ui,sans-serif"


def _project_version() -> str:
    try:
        data = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
        return str(data.get("version", "?"))
    except Exception:
        return "?"


def resolve_admin_key(data_dir: Path) -> str:
    """Ключ админа: из data_dir/admin_key.json, иначе генерируем и сохраняем.
    Тот же файл, что использует server_main.py — ключ общий и стабильный."""
    path = data_dir / "admin_key.json"
    try:
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("admin_key"):
                return str(saved["admin_key"])
    except Exception:
        pass
    key = secrets.token_urlsafe(24)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"admin_key": key}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return key


def _fmt_uptime(seconds) -> str:
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м {sec:02d}с"
    return f"{sec}с"


def _fmt_mmss(seconds) -> str:
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


# ── Контроллер сервера (логика отделена от UI) ───────────────────────
class ServerController:
    """Управляет сервером, отделено от UI."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.admin_key = resolve_admin_key(data_dir)
        self.relay: RelayServer | None = None
        self.host = "0.0.0.0"
        self.port = DEFAULT_PORT
        self.name = "Host"
        self.access_key = ""
        self.encryption_enabled = False
        self.secret_key = ""

    def load_settings(self) -> None:
        """Загружает настройки из settings.json."""
        try:
            st = load_settings() or {}
            self.name = str(st.get("name", "") or "Host")
            self.port = int(st.get("port", DEFAULT_PORT) or DEFAULT_PORT)
            self.access_key = str(st.get("access_key", "") or "")
            self.host = str(st.get("server_host", "0.0.0.0") or "0.0.0.0")
            self.encryption_enabled = bool(st.get("encryption_enabled", False))
            self.secret_key = str(st.get("secret_key", "") or "")
        except Exception:
            pass

    def save_settings(self) -> None:
        """Сохраняет настройки в settings.json."""
        try:
            st = load_settings() or {}
            st["name"] = self.name
            st["port"] = self.port
            st["access_key"] = self.access_key
            st["server_host"] = self.host
            st["encryption_enabled"] = self.encryption_enabled
            st["secret_key"] = self.secret_key
            save_settings(st)
        except Exception:
            pass

    def start(self) -> bool:
        """Запускает сервер."""
        if self.relay is not None:
            return True

        self.relay = RelayServer(
            self.data_dir,
            host_name=self.name,
            access_key=self.access_key,
            admin_key=self.admin_key,
        )
        return self.relay.start(self.host, self.port)

    def stop(self) -> None:
        """Останавливает сервер."""
        if self.relay is not None:
            try:
                self.relay.stop()
            except Exception:
                pass
            self.relay = None

    def is_running(self) -> bool:
        """Проверяет работает ли сервер."""
        return self.relay is not None and self.relay.is_running()

    def get_state(self) -> dict:
        """Возвращает текущее состояние сервера."""
        if not self.is_running():
            return {"running": False}

        d: dict = {"running": True}
        try:
            d.update(self.relay.get_server_stats())
        except Exception:
            pass
        try:
            d["users"] = self.relay.online_details()
        except Exception:
            d["users"] = []
        try:
            d["voice_names"] = list(self.relay.get_voice_participants())
        except Exception:
            d["voice_names"] = []
        try:
            d["channels_list"] = [str(c.get("name", "?")) for c in self.relay.get_channels()]
        except Exception:
            d["channels_list"] = []
        try:
            bot = self.relay.get_bot_manager().get_bot("music")
            if bot is not None:
                d["music"] = bot.api_state()
                d["lib_n"] = len(getattr(bot, "library", {}) or {})
        except Exception:
            pass
        return d

    def kick_user(self, name: str) -> bool:
        """Кикает пользователя."""
        if self.relay is None:
            return False
        return self.relay.kick_user(name, by=self.relay.host_name)

    def get_music_bot(self):
        """Возвращает бота музыки."""
        if self.relay is None:
            return None
        try:
            return self.relay.get_bot_manager().get_bot("music")
        except Exception:
            return None


# ── фоновый поток: снимок состояния сервера ───────────────────────────


class _Poller(QThread):
    """Раз в 1.5с собирает снимок состояния работающего сервера.
    Каждый источник под try — GUI не должен падать из-за мелочи."""

    snapshot = Signal(dict)

    def __init__(self, interval_s: float = REFRESH_INTERVAL_S):
        super().__init__()
        self.relay: ServerController | None = None   # назначает GUI-поток
        self._interval_s = max(0.5, interval_s)

    def stop(self) -> None:
        self.requestInterruption()
        self.wait(2000)  # ждём до 2 сек завершения

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                self.snapshot.emit(self._collect())
            except Exception:
                pass  # следующая итерация попробует снова
            self.msleep(int(self._interval_s * 1000))

    def _collect(self) -> dict:
        controller = self.relay
        if controller is None or not controller.is_running():
            return {"running": False}
        return controller.get_state()


class _Worker(QThread):
    """(v3.4.2 fix) Одноразовый фоновый исполнитель для БЛОКИРУЮЩИХ операций.

    Раньше старт сервера (relay.start(): TLS-генерация сертификата —
    ЗАМЕРЕНО 84.6 с на слабом CPU, get_interfaces(): netsh/getaddrinfo/
    connect(8.8.8.8) БЕЗ таймаутов) и музыкальные кнопки (api_play/pause/
    rescan — дисковый I/O под общим локом бота) вызывались ПРЯМО в
    GUI-потоке Qt. Любая медленная операция замораживала окно намертво
    (Windows помечает его «Не отвечает»), а при VPN/DNS-зависании — навсегда.

    Теперь каждая такая операция уходит сюда; результат возвращается
    сигналом в GUI-поток."""

    done = Signal(object)      # результат fn (или None при исключении)
    failed = Signal(str)       # человекочитаемая ошибка, если была

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:
            result = None
            try:
                self.failed.emit(f"{type(e).__name__}: {e}")
            except Exception:
                pass
        try:
            self.done.emit(result)
        except Exception:
            pass


class _VpnZtDialog(QDialog):
    """«Починить VPN+ZT» (v3.5.10): живая диагностика В ОКНЕ GUI + починка.

    Раньше кнопка молча запускала .bat, который печатал кракозябры
    (cmd.exe читает .bat в CP866), падал на %ProgramFiles(x86)% и делал
    `route add <ИМЯ сети>` вместо подсети — пользователь видел «синюю
    абракадабру» и ноль пользы. Теперь:
      1) diagnose (права админа НЕ нужны) — список проверок со статусами:
         сервис/сети/корни ZeroTier, VPN-перехват маршрутов, пути к
         подсети ZT, жив ли сервер на :8420, брендмауэр;
      2) «Применить исправления» — UAC-запрос и fix_vpn_zt.py fix в
         КОНСОЛИ с читаемым русским текстом (явные маршруты в подсеть ZT
         с metric 1, /32-маршруты корней мимо VPN, правила брендмауэра);
      3) «Проверить снова» — перезапуск диагностики после починки.
    """

    def __init__(self, parent, root: Path, port: int) -> None:
        super().__init__(parent)
        self._root = root
        self._port = int(port)
        self._worker: _Worker | None = None

        self.setWindowTitle("Починить VPN + ZeroTier — Friend Relay")
        self.setMinimumSize(720, 540)
        self.setStyleSheet(
            f"QWidget {{ background: {_BG}; color: {_TEXT};"
            f" font-family: {_SANS}; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(10)

        head = QLabel("Что мешает подключению — диагностика сети")
        head.setStyleSheet(f"color: {_TEXT_STRONG}; font-size: 15px;"
                           " font-weight: 600;")
        v.addWidget(head)

        sub = QLabel(
            "Full-tunnel VPN часто уводит трафик ZeroTier в туннель — "
            "из-за этого друзья не могут зайти ни в приложение, ни в веб. "
            "Проверка безопасна и не требует прав админа. «Применить "
            f"исправления» сделает 7 групп фиксов: порты {self._port}-"
            f"{self._port + 4} + ICMP в брендмауэре, персистентные маршруты "
            "в подсеть ZeroTier (metric 1, переживают перезагрузку), "
            "приоритет маршрутов (ZT = 1, VPN = 100), профиль сети "
            "«Частная», корни ZeroTier мимо VPN, MTU и рестарт службы "
            "ZeroTier. Метрики/MTU/профиль живут до перезагрузки.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        v.addWidget(sub)

        self._status = QLabel("Проверяю сеть…")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            f"color: {_WARNING}; font-size: 12px; font-weight: 600;"
            " padding: 4px;")
        v.addWidget(self._status)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_SURFACE}; color: {_TEXT};
                border: 1px solid {_BORDER}; border-radius: 8px;
                font-family: {_MONO}; font-size: 12px; padding: 8px;
            }}
        """)
        v.addWidget(self._log, 1)

        row = QHBoxLayout()
        self._btn_recheck = QPushButton("🔄 Проверить снова")
        self._btn_apply = QPushButton("🛠 Применить исправления… (нужен админ)")
        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.accept)
        self._btn_recheck.clicked.connect(self._run_diagnose)
        self._btn_apply.clicked.connect(self._on_apply)
        for b in (self._btn_recheck, self._btn_apply, btn_close):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {_SURFACE3}; color: {_TEXT};
                    border: 1px solid {_BORDER2}; border-radius: 6px;
                    padding: 8px 14px; font-weight: 500;
                }}
                QPushButton:hover {{ background: {_SURFACE2}; }}
                QPushButton:disabled {{ color: {_TEXT_DIM2}; }}
            """)
        self._btn_apply.setStyleSheet(f"""
            QPushButton {{
                background: {_ACCENT_SOFT}; color: {_ACCENT2};
                border: 1px solid rgba(255,91,58,0.35); border-radius: 6px;
                padding: 8px 14px; font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(255,91,58,0.22); }}
            QPushButton:disabled {{ color: {_TEXT_DIM2}; }}
        """)
        row.addWidget(self._btn_recheck)
        row.addWidget(self._btn_apply)
        row.addStretch(1)
        row.addWidget(btn_close)
        v.addLayout(row)

        self._run_diagnose()

    # ── диагностика ────────────────────────────────────────────────────
    def _run_diagnose(self) -> None:
        """Запуск diagnose в фоне (GUI не морозит; до ~60 с при VPN)."""
        if self._worker is not None and self._worker.isRunning():
            return
        self._btn_recheck.setEnabled(False)
        self._btn_apply.setEnabled(False)
        self._status.setText("Проверяю сеть… (при включённом VPN — до минуты)")
        self._status.setStyleSheet(
            f"color: {_WARNING}; font-size: 12px; font-weight: 600;"
            " padding: 4px;")
        self._log.setPlainText(
            "Собираю данные: ZeroTier (сервис/сети/корни), таблица "
            "маршрутов, брендмауэр, доступность сервера…")

        def _job():
            return run_vpn_diagnose(self._root, self._port)

        self._worker = _Worker(_job)
        self._worker.done.connect(self._on_diag_done)
        self._worker.failed.connect(self._on_diag_failed)
        self._worker.start()

    def _on_diag_done(self, result: object) -> None:
        self._btn_recheck.setEnabled(True)
        self._btn_apply.setEnabled(True)
        if result is None:
            self._on_diag_failed("смотри журнал")
            return
        code, text = result
        self._log.setPlainText(str(text) or "(пустой отчёт)")
        if code == 0:
            self._set_status(
                "✓ Серьёзных проблем не найдено. Если всё равно не "
                "подключается — прочитай подсказки в деталях строк выше.",
                _SUCCESS)
        elif code == 1:
            self._set_status(
                "✗ Найдены проблемы — строки [FAIL] выше показывают, что "
                "именно мешает. «Применить исправления» починит то, что "
                "можно исправить автоматически.", _DANGER)
        else:
            self._set_status(
                "! Не удалось собрать данные полностью (строки [skip] "
                "выше) — но найденное уже видно.", _WARNING)

    def _on_diag_failed(self, err: str) -> None:
        self._btn_recheck.setEnabled(True)
        self._btn_apply.setEnabled(True)
        self._set_status(f"✗ Диагностика не удалась: {err}", _DANGER)
        self._log.setPlainText(
            "Диагностика упала. Можно запустить вручную:\n"
            "  python scripts/fix_vpn_zt.py diagnose\n"
            f"Ошибка: {err}")

    def _set_status(self, text: str, color: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600;"
            " padding: 4px;")

    # ── применение фикса ───────────────────────────────────────────────
    def _on_apply(self) -> None:
        """UAC-элевация: fix_vpn_zt.py fix в видимой консоли (UTF-8)."""
        ok = launch_vpn_zt_fixer(
            self._root, fw_ports=f"{self._port}-{self._port + 4}")
        if ok:
            self._set_status(
                "Запрошены права администратора (UAC). Подтверди — "
                "откроется окно починки с русским текстом, выполни там "
                "шаги, затем нажми «Проверить снова».", _WARNING)
            self._log.appendPlainText(
                "\n— запущена починка (fix). Отчёт: "
                "relay_data/vpn_fixer_report.json —")
        else:
            self._set_status(
                "Не удалось запустить починку (UAC отклонён?). Попробуй "
                "ещё раз или запусти scripts\\fix_zerotier_vpn.bat ПКМ → "
                "«Запуск от имени администратора».", _DANGER)


class _LogSignal(QObject):
    """Потокобезопасная доставка записей журнала в GUI-поток:
    logging-хендлер зовётся из чужих потоков (серверных), emit сигнала
    из них безопасен — Qt доставит queued-сообщением."""

    record = Signal(str)


class _LogBridge:
    """Колбэк для setup_server_logging: пересылает строку в Qt-сигнал."""

    def __init__(self, signal: _LogSignal):
        self._signal = signal

    def __call__(self, message: str) -> None:
        try:
            self._signal.record.emit(message)
        except Exception:
            pass


# ── главное окно ──────────────────────────────────────────────────────


class ServerWindow(QWidget):
    """Окно управления сервером: старт/стоп, состояние, участники, музыка."""

    def _fetch_ips_async(self) -> None:
        """Асинхронно получает IP-адреса для быстрого старта GUI.

        (v3.4.2 fix) ЗДЕСЬ же получаем и get_interfaces() — раньше её
        вызывали в GUI-потоке из _update_host_combo: netsh-subprocess +
        getaddrinfo + connect(8.8.8.8) без таймаутов замораживали только
        что открытое окно при проблемной сети/VPN."""
        try:
            ips = local_ips()
            self._cached_ips = ips
            # Полный список интерфейсов с типами — тоже в фоне
            try:
                ifaces = get_interfaces()
            except Exception:
                ifaces = []
            # Обновляем UI в главном потоке через QTimer
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._update_host_combo(ips, ifaces))
        except Exception:
            pass

    def _update_host_combo(self, ips: list[str], ifaces=None) -> None:
        """Обновляет выпадающий список интерфейсов с найденными IP.

        v3.4.1: теперь показываем тип интерфейса (ZeroTier/Fizicheskiy/VPN)
        прямо в названии, чтобы пользователь видел какой адрес выберет.
        v3.4.2 fix: ifaces получены в фоновом потоке (см. _fetch_ips_async)."""
        # Сохраняем текущий выбор
        current_data = self._host_combo.currentData()

        # Очищаем кроме базовых
        while self._host_combo.count() > 2:
            self._host_combo.removeItem(2)

        # Берём интерфейсы с типами (уже полученные в фоне; на всякий случай
        # fallback — но обычно сюда попадаем с готовым списком)
        if ifaces is None:
            ifaces = get_interfaces()
        ifaces_map = {i.ip: i for i in ifaces}

        # Добавляем найденные IP с пометкой типа
        _KIND_LABEL = {
            "zerotier":  "ZeroTier ✨",
            "tailscale": "Tailscale ✨",
            "physical": "физический",
            "vpn":       "VPN ❗",
        }
        for ip in ips:
            if ip in ["0.0.0.0", "127.0.0.1"]:
                continue
            info = ifaces_map.get(ip)
            if info is None:
                label = f"{ip} (сетевой)"
            else:
                kl = _KIND_LABEL.get(info.kind, "сетевой")
                label = f"{ip}  ·  {kl}"
                if info.iface_name:
                    label += f"  [{info.iface_name}]"
            self._host_combo.addItem(label, ip)

        # Восстанавливаем выбор если возможно
        for i in range(self._host_combo.count()):
            if self._host_combo.itemData(i) == current_data:
                self._host_combo.setCurrentIndex(i)
                break

    def __init__(self, data_dir: Path):
        super().__init__()
        self.setWindowTitle(f"Friend Relay Server · v{_project_version()}")
        self.resize(1300, 1000)
        self.setMinimumSize(1100, 850)
        self.setStyleSheet(f"""
            QWidget {{
                background: {_BG};
                color: {_TEXT};
                font-family: {_SANS};
                font-size: 15px;
                letter-spacing: -0.005em;
            }}
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
            QLabel {{
                color: {_TEXT};
                background: transparent;
            }}
            QLineEdit {{
                background: {_SURFACE3};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 10px 14px;
                color: {_TEXT};
                selection-background-color: {_ACCENT_SOFT};
                min-height: 20px;
            }}
            QLineEdit:focus {{
                border: 1px solid {_ACCENT};
                background: {_SURFACE2};
            }}
            QSpinBox {{
                background: {_SURFACE3};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 10px 14px;
                color: {_TEXT};
                min-height: 20px;
            }}
            QSpinBox:focus {{
                border: 1px solid {_ACCENT};
            }}
            QComboBox {{
                background: {_SURFACE3};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 10px 14px;
                color: {_TEXT};
                min-height: 20px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QComboBox QAbstractItemView {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                selection-background-color: {_ACCENT_SOFT};
            }}
            QPushButton {{
                border: none;
                border-radius: 6px;
                padding: 12px 20px;
                font-weight: 500;
                letter-spacing: -0.01em;
                min-height: 20px;
            }}
            QPushButton:hover {{
                background: {_SURFACE2};
            }}
            QProgressBar {{
                background: {_SURFACE3};
                border: none;
                border-radius: 6px;
                height: 8px;
            }}
            QProgressBar::chunk {{
                background: {_ACCENT};
                border-radius: 6px;
            }}
            QTableWidget {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                gridline-color: {_BORDER};
                selection-background-color: {_ACCENT_SOFT};
            }}
            QTableWidget::item {{
                padding: 12px 16px;
            }}
            QHeaderView::section {{
                background: {_SURFACE2};
                border: none;
                border-bottom: 1px solid {_BORDER};
                border-right: 1px solid {_BORDER};
                padding: 14px 18px;
                font-weight: 600;
                color: {_TEXT_DIM};
            }}
            QPlainTextEdit {{
                background: {_SURFACE3};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 14px;
                font-family: {_MONO};
                font-size: 13px;
            }}
            QCheckBox {{
                spacing: 10px;
                color: {_TEXT};
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border: 1px solid {_BORDER};
                border-radius: 4px;
                background: {_SURFACE3};
            }}
            QCheckBox::indicator:checked {{
                background: {_ACCENT};
                border-color: {_ACCENT};
            }}
        """)

        self._data_dir = data_dir
        self._controller = ServerController(data_dir)
        self._controller.load_settings()
        self._admin_key = self._controller.admin_key
        # Реальная папка музыки (может быть переопределена в music_data.json —
        # уточняется при старте сервера из bot.music_dir).
        self._music_dir = data_dir / "music"

        self._build_ui()

        # Кэшируем IP-адреса для быстрого старта (сеть может быть медленной)
        self._cached_ips = []
        self._ip_fetch_thread = threading.Thread(target=self._fetch_ips_async, daemon=True)
        self._ip_fetch_thread.start()

        # Журнал сервера → окно (файл пишется всегда, консоль не пачкается).
        self._log_signal = _LogSignal()
        self._log_signal.record.connect(self._on_log_record)
        self._log_bridge = _LogBridge(self._log_signal)
        self._log = setup_server_logging(data_dir, console_errors=False,
                                         gui_callback=self._log_bridge)

        # Фоновый поллер состояния.
        self._poller = _Poller()
        self._poller.snapshot.connect(self._on_snapshot)
        self._poller.relay = self._controller
        self._poller.start()

        # Заполняем UI из настроек контроллера
        self._name_edit.setText(self._controller.name)
        self._port_edit.setValue(self._controller.port)
        self._access_key_edit.setText(self._controller.access_key)
        self._encryption_enabled.setChecked(self._controller.encryption_enabled)
        self._secret_key_edit.setText(self._controller.secret_key)
        # Выбор интерфейса
        for i in range(self._host_combo.count()):
            if self._host_combo.itemData(i) == self._controller.host:
                self._host_combo.setCurrentIndex(i)
                break

        # Проверяем конфликт VPN+ZeroTier сразу после открытия GUI
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, self._check_vpn_conflict)

        get_logger().info("GUI сервера открыт (данные: %s)", data_dir)

    # ── построение интерфейса ─────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(20)

        # Шапка: бренд + статус
        header = QHBoxLayout()
        header.setSpacing(16)

        # Лого + название
        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel("FR")
        logo.setStyleSheet(f"""
            background: {_ACCENT_SOFT};
            border: 1px solid {_BORDER};
            border-radius: 8px;
            color: {_ACCENT};
            font-weight: 700;
            font-size: 14px;
            padding: 6px 10px;
        """)
        brand.addWidget(logo)

        title = QLabel("Friend Relay Server")
        title.setStyleSheet(f"""
            color: {_TEXT_STRONG};
            font-size: 15px;
            font-weight: 600;
            letter-spacing: -0.015em;
        """)
        brand.addWidget(title)
        header.addLayout(brand)

        header.addStretch(1)

        # Статус
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {_TEXT_DIM2}; font-size: 10px;")
        header.addWidget(self._dot)
        self._status_text = QLabel("OFFLINE")
        self._status_text.setStyleSheet(f"""
            color: {_TEXT_DIM};
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.05em;
        """)
        header.addWidget(self._status_text)

        root.addLayout(header)

        # Панель запуска: имя/порт/ключ + большая кнопка.
        cfg = QFrame()
        cfg.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        cfg_l = QVBoxLayout(cfg)
        cfg_l.setContentsMargins(24, 20, 24, 20)
        cfg_l.setSpacing(16)

        # Имя и порт
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(QLabel("Имя"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("имя хоста")
        row.addWidget(self._name_edit, 2)
        row.addWidget(QLabel("Порт"))
        self._port_edit = QSpinBox()
        self._port_edit.setRange(1024, 65535)
        self._port_edit.setValue(DEFAULT_PORT)
        row.addWidget(self._port_edit)
        cfg_l.addLayout(row)

        # Интерфейс и ключ доступа
        row2 = QHBoxLayout()
        row2.setSpacing(16)
        row2.addWidget(QLabel("Интерфейс"))
        self._host_combo = QComboBox()
        self._host_combo.addItem("Все интерфейсы (0.0.0.0)", "0.0.0.0")
        self._host_combo.addItem("Только локальный (127.0.0.1)", "127.0.0.1")
        # Добавим обнаруженные интерфейсы
        try:
            for ip in local_ips():
                if ip not in ["0.0.0.0", "127.0.0.1"]:
                    self._host_combo.addItem(f"{ip} (сетевой)", ip)
        except Exception:
            pass
        row2.addWidget(self._host_combo, 2)
        row2.addWidget(QLabel("Ключ доступа"))
        self._access_key_edit = QLineEdit()
        self._access_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._access_key_edit.setPlaceholderText("пусто = открытый сервер")
        row2.addWidget(self._access_key_edit, 2)
        cfg_l.addLayout(row2)

        # Шифрование
        enc_row = QHBoxLayout()
        enc_row.setSpacing(16)
        self._encryption_enabled = QCheckBox()
        self._encryption_enabled.setText("Включить AES-256-GCM шифрование")
        enc_row.addWidget(self._encryption_enabled)
        enc_row.addStretch(1)
        cfg_l.addLayout(enc_row)

        enc_key_row = QHBoxLayout()
        enc_key_row.setSpacing(16)
        enc_key_row.addWidget(QLabel("Ключ шифрования"))
        self._secret_key_edit = QLineEdit()
        self._secret_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_key_edit.setPlaceholderText("секретный ключ")
        enc_key_row.addWidget(self._secret_key_edit, 2)
        cfg_l.addLayout(enc_key_row)

        # Подсказка VPN + авто-обнаружение конфликта + кнопка фикса
        self._vpn_conflict_hint = QLabel("")
        self._vpn_conflict_hint.setStyleSheet(
            f"color: {_WARNING}; font-size: 11px; padding: 6px 8px;"
            f" background: rgba(251,191,36,0.06); border-left: 2px solid {_WARNING};"
            f" border-radius: 4px;")
        self._vpn_conflict_hint.setWordWrap(True)
        self._vpn_conflict_hint.hide()
        cfg_l.addWidget(self._vpn_conflict_hint)

        vpn_row = QHBoxLayout()
        vpn_hint = QLabel("💡 Если включён VPN — нажми кнопку справа или выбери 'Все интерфейсы'")
        vpn_hint.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px; padding: 4px;")
        vpn_hint.setWordWrap(True)
        vpn_row.addWidget(vpn_hint, 1)
        self._btn_fix_vpn_zt = QPushButton("🛠 Починить VPN+ZT")
        self._btn_fix_vpn_zt.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3}; color: {_WARNING};
                border: 1px solid rgba(251,191,36,0.25);
                border-radius: 6px; padding: 8px 14px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background: {_SURFACE2}; }}
        """)
        self._btn_fix_vpn_zt.setToolTip(
            "Покажет, что именно мешает подключению при включённом VPN "
            "(маршруты, ZeroTier, брендмауэр) — прямо в этом окне.\n"
            "«Применить исправления» запросит права админа: добавит явные "
            "маршруты в подсеть ZeroTier (metric 1), маршруты корней ZT "
            "мимо VPN и разрешит порты в брендмауэре.\n"
            "Все изменения живут до перезагрузки — полностью безопасно.")
        self._btn_fix_vpn_zt.clicked.connect(self._on_fix_vpn_zt)
        vpn_row.addWidget(self._btn_fix_vpn_zt)
        cfg_l.addLayout(vpn_row)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        self._btn_start = QPushButton("Запустить сервер")
        self._btn_start.setStyleSheet(f"""
            QPushButton {{
                background: {_ACCENT};
                color: white;
                font-weight: 600;
                padding: 14px 28px;
            }}
            QPushButton:hover {{
                background: {_ACCENT2};
            }}
            QPushButton:disabled {{
                background: {_SURFACE3};
                color: {_TEXT_DIM};
            }}
        """)
        self._btn_start.clicked.connect(self._on_start)
        row2.addWidget(self._btn_start)
        self._btn_stop = QPushButton("Остановить")
        self._btn_stop.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3};
                color: {_TEXT};
                border: 1px solid {_BORDER};
            }}
            QPushButton:hover {{
                background: {_SURFACE2};
            }}
            QPushButton:disabled {{
                background: {_SURFACE3};
                color: {_TEXT_DIM};
            }}
        """)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        row2.addWidget(self._btn_stop)
        row2.addStretch(1)
        self._data_label = QLabel(f"Данные: {self._data_dir}")
        self._data_label.setStyleSheet(f"color: {_TEXT_DIM2}; font-size: 11px; font-family: {_MONO};")
        row2.addWidget(self._data_label)
        cfg_l.addLayout(row2)
        root.addWidget(cfg)

        # KPI-плитки
        kpi = QHBoxLayout()
        kpi.setSpacing(12)
        self._kpi_online = self._make_kpi("Онлайн", "—")
        self._kpi_uptime = self._make_kpi("Аптайм", "—")
        self._kpi_voice = self._make_kpi("Голос", "—")
        self._kpi_req = self._make_kpi("Запросов", "—")
        for w in (self._kpi_online, self._kpi_uptime, self._kpi_voice, self._kpi_req):
            kpi.addWidget(w, 1)
        root.addLayout(kpi)

        self._state_line = QLabel("")
        self._state_line.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px; font-family: {_MONO};")
        root.addWidget(self._state_line)

        # Средняя зона: участники | музыка.
        mid = QHBoxLayout()
        mid.setSpacing(10)
        mid.addWidget(self._build_users_card(), 3)
        mid.addWidget(self._build_music_card(), 2)
        root.addLayout(mid, 1)

        # Приглашение.
        root.addWidget(self._build_invite_card())

        # Журнал.
        root.addWidget(self._build_log_card(), 2)

    def _build_users_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(16)
        self._users_title = QLabel("Участники")
        self._users_title.setStyleSheet(f"""
            color: {_TEXT_STRONG};
            font-size: 14px;
            font-weight: 600;
            letter-spacing: -0.015em;
        """)
        v.addWidget(self._users_title)

        self._users_table = QTableWidget(0, 4)
        self._users_table.setHorizontalHeaderLabels(["Имя", "Роль", "В сети", "Статус"])
        self._users_table.verticalHeader().setVisible(False)
        self._users_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._users_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self._users_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._users_table.horizontalHeader().setStretchLastSection(True)
        self._users_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self._users_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self._users_table.setStyleSheet(f"""
            QTableWidget {{
                background: {_SURFACE};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                font-family: {_SANS}; font-size: 13px;
                gridline-color: {_BORDER};
            }}
            QHeaderView::section {{
                background: {_SURFACE2}; color: {_TEXT_DIM};
                border: none; border-bottom: 1px solid {_BORDER};
                padding: 14px 18px; font-weight: 600;
            }}
            QTableWidget::item {{ padding: 12px 16px; }}
            QTableWidget::item:selected {{ background: {_ACCENT_SOFT}; }}
        """)
        v.addWidget(self._users_table, 1)

        kick_row = QHBoxLayout()
        kick_row.setSpacing(12)
        self._btn_kick = QPushButton("Кикнуть")
        self._btn_kick.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3};
                color: {_DANGER};
                border: 1px solid {_BORDER};
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background: {_SURFACE2};
            }}
            QPushButton:disabled {{
                background: {_SURFACE3};
                color: {_TEXT_DIM};
            }}
        """)
        self._btn_kick.setEnabled(False)
        self._btn_kick.clicked.connect(self._on_kick)
        kick_row.addWidget(self._btn_kick)
        kick_row.addStretch(1)
        v.addLayout(kick_row)
        return card

    def _build_music_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(16)
        t = QLabel("Музыка")
        t.setStyleSheet(f"""
            color: {_TEXT_STRONG};
            font-size: 14px;
            font-weight: 600;
            letter-spacing: -0.015em;
        """)
        v.addWidget(t)

        self._music_track = QLabel("—")
        self._music_track.setWordWrap(True)
        self._music_track.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600;")
        v.addWidget(self._music_track)

        self._music_bar = QProgressBar()
        self._music_bar.setRange(0, 1000)
        self._music_bar.setTextVisible(False)
        self._music_bar.setFixedHeight(8)
        self._music_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {_SURFACE3}; border: none; border-radius: 6px;
            }}
            QProgressBar::chunk {{ background: {_ACCENT}; border-radius: 6px; }}
        """)
        v.addWidget(self._music_bar)
        self._music_time = QLabel("")
        self._music_time.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; font-family: {_MONO};")
        v.addWidget(self._music_time)

        btns = QHBoxLayout()
        btns.setSpacing(12)
        self._btn_play = QPushButton("Играть")
        self._btn_play.setStyleSheet(f"""
            QPushButton {{
                background: {_ACCENT}; color: white;
                border: none; border-radius: 6px; padding: 10px 20px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background: {_ACCENT2}; }}
        """)
        self._btn_play.clicked.connect(self._on_music_toggle)
        btns.addWidget(self._btn_play)
        self._btn_skip = QPushButton("Дальше")
        self._btn_skip.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3}; color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 10px 20px;
            }}
            QPushButton:hover {{ background: {_SURFACE2}; }}
        """)
        self._btn_skip.clicked.connect(self._on_music_skip)
        btns.addWidget(self._btn_skip)
        self._btn_rescan = QPushButton("Скан")
        self._btn_rescan.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3}; color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 10px 20px;
            }}
            QPushButton:hover {{ background: {_SURFACE2}; }}
        """)
        self._btn_rescan.setToolTip(f"Пересканировать папку музыки:\n{self._music_dir}")
        self._btn_rescan.clicked.connect(self._on_music_rescan)
        btns.addWidget(self._btn_rescan)
        self._btn_music_dir = QPushButton("Папка")
        self._btn_music_dir.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3}; color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 10px 20px;
            }}
            QPushButton:hover {{ background: {_SURFACE2}; }}
        """)
        self._btn_music_dir.setToolTip("Открыть папку музыки в проводнике")
        self._btn_music_dir.clicked.connect(self._on_open_music_dir)
        btns.addWidget(self._btn_music_dir)
        btns.addStretch(1)
        v.addLayout(btns)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Громкость:"))
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.valueChanged.connect(self._on_volume)
        vol_row.addWidget(self._volume, 1)
        self._vol_label = QLabel("80%")
        self._vol_label.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; font-family: {_MONO};")
        vol_row.addWidget(self._vol_label)
        v.addLayout(vol_row)

        self._music_info = QLabel("")
        self._music_info.setStyleSheet(
            f"color: {_TEXT}; font-size: 11px; font-family: {_MONO};")
        self._music_info.setWordWrap(True)
        v.addWidget(self._music_info)
        v.addStretch(1)
        return card

    def _build_invite_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        self._invite_grid = QGridLayout(card)
        self._invite_grid.setContentsMargins(24, 20, 24, 20)
        self._invite_grid.setVerticalSpacing(12)
        self._invite_grid.setHorizontalSpacing(16)
        t = QLabel("Приглашение")
        t.setStyleSheet(f"""
            color: {_TEXT_STRONG};
            font-size: 14px;
            font-weight: 600;
            letter-spacing: -0.015em;
        """)
        self._invite_grid.addWidget(t, 0, 0, 1, 2)
        return card

    def _build_log_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(16)
        t = QLabel("Журнал")
        t.setStyleSheet(f"""
            color: {_TEXT_STRONG};
            font-size: 14px;
            font-weight: 600;
            letter-spacing: -0.015em;
        """)
        head.addWidget(t)
        head.addStretch(1)
        btn_open = QPushButton("Папка")
        btn_open.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 10px 16px;
            }}
            QPushButton:hover {{
                background: {_SURFACE2};
            }}
        """)
        btn_open.clicked.connect(self._on_open_logs)
        head.addWidget(btn_open)
        btn_clear = QPushButton("Очистить")
        btn_clear.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 10px 16px;
            }}
            QPushButton:hover {{
                background: {_SURFACE2};
            }}
        """)
        btn_clear.clicked.connect(lambda: self._log_view.clear())
        head.addWidget(btn_clear)
        v.addLayout(head)

        self._log_path_lab = QLabel(f"файл: {self._data_dir / 'logs' / 'server.log'}")
        self._log_path_lab.setStyleSheet(f"color: {_TEXT_DIM2}; font-size: 10px; font-family: {_MONO};")
        v.addWidget(self._log_path_lab)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        self._log_view.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_SURFACE3};
                color: {_TEXT_DIM};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                font-family: {_MONO};
                font-size: 11px;
            }}
        """)
        v.addWidget(self._log_view, 1)
        return card

    def _make_kpi(self, title: str, value: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(4)
        t = QLabel(title)
        t.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase;")
        v.addWidget(t)
        val = QLabel(value)
        val.setStyleSheet(
            f"color: {_TEXT_STRONG}; font-size: 24px; font-weight: 600;"
            f" font-family: {_MONO}; letter-spacing: -0.02em;")
        v.addWidget(val)
        card._value_label = val  # type: ignore[attr-defined]
        return card

    # ── действия: fix VPN+ZeroTier ────────────────────────────────────
    def _check_vpn_conflict(self) -> None:
        """Раз в несколько секунд проверяем, нет ли ZT+VPN одновременно.
        Показываем жёлтый баннер и включаем кнопку — если есть конфликт."""
        has_conflict, tip = detect_vpn_zt_conflict()
        if has_conflict:
            self._vpn_conflict_hint.setText("⚠  " + tip)
            self._vpn_conflict_hint.show()
            get_logger().warning("Обнаружен конфликт VPN+ZeroTier (вкл. кнопка фикса)")
        else:
            self._vpn_conflict_hint.hide()

    def _on_fix_vpn_zt(self) -> None:
        """Кнопка «Починить VPN+ZT»: окно диагностики + применение фикса.

        (v3.5.10) Раньше молча запускался .bat: кракозябры вместо текста,
        route add с именем сети — ноль пользы. Теперь — диалог с живой
        диагностикой (без админа) и честной починкой через UAC."""
        try:
            port = int(self._controller.port or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        dlg = _VpnZtDialog(self, ROOT, port)
        dlg.exec()

    # ── действия: старт/стоп ──────────────────────────────────────────
    def _on_start(self) -> None:
        if self._controller.is_running():
            return
        if getattr(self, "_start_worker", None) is not None:
            return  # старт уже идёт — второй раз не жмём

        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Friend Relay — Сервер",
                                "Введи имя хоста (оно же — имя админа).")
            return

        # Обновляем настройки контроллера
        self._controller.name = name
        self._controller.port = self._port_edit.value()
        self._controller.access_key = self._access_key_edit.text().strip()
        self._controller.host = self._host_combo.currentData()
        self._controller.encryption_enabled = self._encryption_enabled.isChecked()
        self._controller.secret_key = self._secret_key_edit.text().strip()

        # Сохраняем настройки
        self._controller.save_settings()

        # (v3.4.2 fix) Старт сервера БЛОКИРУЮЩИЙ (TLS-сертификат до 84 с на
        # слабом CPU, get_interfaces() без таймаутов — при VPN висит надолго,
        # скан библиотеки музыки) — раньше выполнялся ПРЯМО в GUI-потоке и
        # замораживал окно намертво. Теперь — в фоновом потоке.
        self._btn_start.setEnabled(False)
        self._btn_start.setText("Запускаю… (первый старт может занять минуту)")
        self._btn_stop.setEnabled(False)

        def _job():
            ok = self._controller.start()
            return {"ok": ok}

        self._start_worker = _Worker(_job)
        self._start_worker.done.connect(self._on_start_done)
        self._start_worker.failed.connect(self._on_start_failed)
        self._start_worker.start()

    def _on_start_failed(self, err: str) -> None:
        get_logger().error("старт сервера упал: %s", err)
        self._reset_start_ui()
        QMessageBox.critical(
            self, "Friend Relay — Сервер",
            f"Ошибка при запуске сервера:\n{err}")

    def _reset_start_ui(self) -> None:
        self._start_worker = None
        self._btn_start.setText("▶ Запустить сервер")
        if not self._controller.is_running():
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)

    def _on_start_done(self, result: object) -> None:
        self._btn_start.setText("▶ Запустить сервер")
        ok = bool(isinstance(result, dict) and result.get("ok"))
        if not ok:
            self._start_worker = None
            self._btn_start.setEnabled(True)
            get_logger().error("старт не удался: порт %s занят", self._controller.port)
            QMessageBox.critical(
                self, "Friend Relay — Сервер",
                f"Не удалось занять порт {self._controller.port}.\n"
                f"Возможно, сервер уже запущен (GUI или server_main.py)?")
            return

        self._poller.relay = self._controller

        # Реальная папка музыки может быть переопределена (music_data.json).
        try:
            _bot = self._controller.relay.get_bot_manager().get_bot("music")
            if _bot is not None and getattr(_bot, "music_dir", None):
                self._music_dir = Path(_bot.music_dir)
                self._btn_rescan.setToolTip(
                    f"Пересканировать папку музыки:\n{self._music_dir}")
        except Exception:
            pass

        # Блокируем конфиг на время работы.
        for w in (self._name_edit, self._port_edit, self._access_key_edit, self._host_combo, self._encryption_enabled, self._secret_key_edit):
            w.setEnabled(False)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._start_worker = None
        # (v3.4.2 fix) get_interfaces() (netsh/getaddrinfo без таймаутов)
        # уводим в воркер, а отрисовку ссылок делаем в GUI-потоке: Qt-виджеты
        # нельзя создавать из чужого потока.
        def _fetch_ifs():
            ifs = get_interfaces()
            if not ifs:
                ips = local_ips()
                ifs = [InterfaceInfo(ip, "physical", "") for ip in ips]
            return ifs

        worker = _Worker(_fetch_ifs)
        worker.done.connect(
            lambda ifs: self._fill_invite(self._controller.port, ifs))
        worker.start()
        self._invite_worker = worker  # держим ссылку (QThread нельзя терять)
        get_logger().info("сервер запущен из GUI: %s, порт %s", self._controller.name, self._controller.port)

    def _on_stop(self) -> None:
        # (v3.4.2 fix) stop() джойнит потоки сервера (до нескольких секунд) —
        # раньше тоже замораживал GUI-поток. Уводим в фон.
        if getattr(self, "_stop_worker", None) is not None:
            return
        self._btn_stop.setEnabled(False)

        def _job():
            self._controller.stop()
            return {"ok": True}

        self._stop_worker = _Worker(_job)
        self._stop_worker.done.connect(self._on_stop_done)
        self._stop_worker.start()

    def _on_stop_done(self, _result: object) -> None:
        self._stop_worker = None
        self._poller.relay = None
        for w in (self._name_edit, self._port_edit, self._access_key_edit, self._host_combo, self._encryption_enabled, self._secret_key_edit):
            w.setEnabled(True)
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._render_stopped()
        get_logger().info("сервер остановлен из GUI")

    def _on_kick(self) -> None:
        if not self._controller.is_running():
            return
        row = self._users_table.currentRow()
        if row < 0:
            return
        item = self._users_table.item(row, 0)
        if item is None:
            return
        name = item.text()
        if name == self._controller.name:
            QMessageBox.information(self, "Friend Relay — Сервер",
                                    "Хоста кикнуть нельзя — он админ.")
            return
        ans = QMessageBox.question(
            self, "Кик участника",
            f"Кикнуть «{name}»?\nЕго сессия будет аннулирована, в чат уйдёт\n"
            f"системное сообщение. Он сможет зайти снова.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        kicked = self._controller.kick_user(name)
        if not kicked:
            QMessageBox.information(self, "Friend Relay — Сервер",
                                    f"«{name}»: активной сессии не найдено.")

    # ── действия: музыка ──────────────────────────────────────────────
    def _music_bot(self):
        return self._controller.get_music_bot()

    # (v3.4.2 fix) Все музыкальные api_* берут общий RLock бота и пишут в
    # music_data.json. Раньше они звались ПРЯМО в GUI-потоке: кнопка ждала
    # дисковый I/O + все /music/sync клиентов, слайдер громкости плодил
    # запись на КАЖДЫЙ тик (один жест = десятки записей = замороженное окно).
    # Теперь: тяжёлое — в _Worker, громкость — с дебаунсом 300 мс.

    def _spawn_music_job(self, job, on_done=None, on_fail=None) -> None:
        worker = _Worker(job)
        worker.done.connect(on_done or (lambda _r: None))
        worker.failed.connect(on_fail or (lambda e: None))
        worker.start()
        if not hasattr(self, "_music_workers"):
            self._music_workers = []
        self._music_workers.append(worker)
        self._music_workers = [w for w in self._music_workers
                               if w is not None and w.isRunning()]

    def _on_music_toggle(self) -> None:
        bot = self._music_bot()
        if bot is None:
            return

        def _job():
            st = bot.api_state()
            if st.get("playing"):
                bot.api_pause(sender=self._controller.name)
            else:
                bot.api_play(sender=self._controller.name)
            return {"ok": True}

        self._spawn_music_job(
            _job,
            on_fail=lambda e: get_logger().error("музыка: ошибка play/pause: %s", e))

    def _on_music_skip(self) -> None:
        bot = self._music_bot()
        if bot is None:
            return

        def _job():
            bot.api_skip(sender=self._controller.name)
            return {"ok": True}

        self._spawn_music_job(
            _job,
            on_fail=lambda e: get_logger().error("музыка: ошибка skip: %s", e))

    def _on_music_rescan(self) -> None:
        bot = self._music_bot()
        if bot is None:
            return

        def _job():
            return bot.api_rescan(sender=self._controller.name)

        self._spawn_music_job(
            _job,
            on_done=lambda r: get_logger().info(
                "музыка: пересканировано, треков: %s",
                (r or {}).get("count", "?") if isinstance(r, dict) else "?"),
            on_fail=lambda e: get_logger().error("музыка: ошибка скана: %s", e))

    def _on_volume(self, value: int) -> None:
        self._vol_label.setText(f"{value}%")
        # Дебаунс: слайдер генерирует десятки valueChanged за один жест.
        # Пишем в бота (это дисковая запись под локом!) один раз — через
        # 300 мс после ПОСЛЕДНЕГО тика.
        self._pending_volume = value
        if getattr(self, "_volume_timer", None) is None:
            from PySide6.QtCore import QTimer
            self._volume_timer = QTimer(self)
            self._volume_timer.setSingleShot(True)
            self._volume_timer.setInterval(300)
            self._volume_timer.timeout.connect(self._flush_volume)
        self._volume_timer.start()

    def _flush_volume(self) -> None:
        value = getattr(self, "_pending_volume", None)
        if value is None:
            return
        self._pending_volume = None
        bot = self._music_bot()
        if bot is None:
            return

        def _job():
            bot.api_volume(sender=self._controller.name, volume=int(value))
            return {"ok": True}

        self._spawn_music_job(
            _job,
            on_fail=lambda e: get_logger().warning("громкость: %s", e))

    # ── служебное ─────────────────────────────────────────────────────
    def _fill_invite(self, port: int, ifaces=None) -> None:
        """Ссылки-приглашения + ключ админа (пересобирается при старте).

        v3.4.1: показываем тип интерфейса (ZeroTier ✨ / физический / VPN ❗)
        рядом с URL, чтобы пользователь отправлял другу именно ZT-адрес;
        дублируем https:// (TLS-мультиплексор слушает на том же порту).

        v3.4.2 fix: ifaces можно передать уже полученными (из воркера —
        get_interfaces() с netsh/getaddrinfo без таймаутов не должен
        выполняться в GUI-потоке)."""
        # Чистим старые строки (кроме заголовка).
        while self._invite_grid.count() > 1:
            item = self._invite_grid.takeAt(1)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if ifaces is None:
            ifaces = get_interfaces()
        # Если вдруг пусто — fallback на старый метод
        if not ifaces:
            ips = local_ips()
            ifaces = [InterfaceInfo(ip, "physical", "") for ip in ips]

        _KIND_LABEL = {
            "zerotier":  ("ZeroTier ✨", _ACCENT),
            "tailscale": ("Tailscale ✨", _ACCENT),
            "physical":  ("физический", _SUCCESS),
            "vpn":       ("VPN ❗", _DANGER),
        }
        _KIND_HINT = {
            "zerotier":  "Отправь этот адрес другу — работает через ZeroTier",
            "tailscale": "Отправь этот адрес другу — работает через Tailscale",
            "physical":  "Локальная сеть / прямой IP — если друг в той же сети",
            "vpn":       "VPN-интерфейс: входящие обычно ЗАБЛОКИРОВАНЫ (для отладки)",
        }

        row = 1
        for info in ifaces:
            if info.kind == "loopback":
                continue
            for scheme in ("http", "https"):
                url = f"{scheme}://{info.ip}:{port}"
                kl_text, kl_color = _KIND_LABEL.get(info.kind, ("", _SUCCESS))
                url_label = QLabel(url)
                url_label.setStyleSheet(
                    f"color: {_SUCCESS}; font-size: 13px; font-family: {_MONO};"
                    f" font-weight: 600; border: none;")
                url_label.setToolTip(_KIND_HINT.get(info.kind, ""))
                self._invite_grid.addWidget(url_label, row, 0)

                tag = QLabel(kl_text)
                tag.setStyleSheet(
                    f"color: {kl_color}; font-size: 10px; font-weight: 600;"
                    f" letter-spacing: 0.03em; text-transform: uppercase; padding: 2px 6px;"
                    f" background: rgba(255,255,255,0.03); border-radius: 4px;"
                )
                tag.setToolTip(_KIND_HINT.get(info.kind, ""))
                self._invite_grid.addWidget(tag, row, 1)

                btn = QPushButton("копировать")
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {_SURFACE3}; color: {_TEXT};
                        border: 1px solid {_BORDER};
                        border-radius: 6px; padding: 10px 16px;
                    }}
                    QPushButton:hover {{ background: {_SURFACE2}; }}
                """)
                btn.clicked.connect(lambda _=False, u=url: self._copy(u))
                self._invite_grid.addWidget(btn, row, 2)
                row += 1

        key_row = self._invite_grid.rowCount()
        key_lab = QLabel(f"Ключ админа: {self._admin_key}")
        key_lab.setStyleSheet(
            f"color: {_WARNING}; font-size: 12px; font-family: {_MONO};"
            f" border: none;")
        host = self._controller.name
        key_lab.setToolTip(
            "Введи его на другом устройстве вместе с именем хоста,\n"
            f"чтобы войти как админ — под именем «{host}», а не «{host}#2».")
        self._invite_grid.addWidget(key_lab, key_row, 0)
        btn_key = QPushButton("копировать")
        btn_key.setStyleSheet(f"""
            QPushButton {{
                background: {_SURFACE3}; color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px; padding: 6px 12px;
            }}
            QPushButton:hover {{ background: {_SURFACE2}; }}
        """)
        btn_key.clicked.connect(lambda: self._copy(self._admin_key))
        self._invite_grid.addWidget(btn_key, key_row, 1)

    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        get_logger().info("скопировано: %s", text[:48])

    def _on_open_logs(self) -> None:
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._data_dir / "logs")))

    def _on_open_music_dir(self) -> None:
        try:
            self._music_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._music_dir)))

    def _on_log_record(self, message: str) -> None:
        self._log_view.appendPlainText(message)

    # ── отрисовка состояния ───────────────────────────────────────────
    def _render_stopped(self) -> None:
        self._dot.setStyleSheet(f"color: {_TEXT_DIM2}; font-size: 10px;")
        self._status_text.setText("OFFLINE")
        self._kpi_online._value_label.setText("—")   # type: ignore[attr-defined]
        self._kpi_uptime._value_label.setText("—")   # type: ignore[attr-defined]
        self._kpi_voice._value_label.setText("—")    # type: ignore[attr-defined]
        self._kpi_req._value_label.setText("—")      # type: ignore[attr-defined]
        self._users_table.setRowCount(0)
        self._state_line.setText("")
        self._music_track.setText("—")
        self._music_bar.setValue(0)
        self._music_time.setText("")
        self._music_info.setText("")

    def _on_snapshot(self, d: dict) -> None:
        if not d.get("running"):
            if not self._controller.is_running():
                self._render_stopped()
            return

        self._dot.setStyleSheet(f"color: {_SUCCESS}; font-size: 18px;")
        self._status_text.setText(
            f"работает · онлайн {d.get('online', 0)} · аптайм "
            f"{d.get('uptime_human', '—')}")

        self._kpi_online._value_label.setText(str(d.get("online", 0)))  # type: ignore[attr-defined]
        self._kpi_uptime._value_label.setText(_fmt_uptime(d.get("uptime_s")))  # type: ignore[attr-defined]
        vn = d.get("voice_names", [])
        self._kpi_voice._value_label.setText(str(len(vn)))  # type: ignore[attr-defined]
        self._kpi_voice._value_label.setToolTip(", ".join(vn))  # type: ignore[attr-defined]
        self._kpi_req._value_label.setText(str(d.get("requests_total", 0)))  # type: ignore[attr-defined]
        self._kpi_req._value_label.setToolTip(  # type: ignore[attr-defined]
            f"avg {d.get('avg_ms', 0):.1f} мс · p95 {d.get('p95_ms', 0):.1f} мс")

        self._state_line.setText(
            f"Каналы: {', '.join(d.get('channels_list', [])) or '—'}   ·   "
            f"Сессии: {d.get('sessions', 0)}   ·   "
            f"Файлы: {d.get('files_tracked', 0)}+{d.get('dm_files_tracked', 0)} ЛС   ·   "
            f"avg {d.get('avg_ms', 0):.1f} мс   ·   "
            f"p95 {d.get('p95_ms', 0):.1f} мс")

        # Участники.
        users = d.get("users", [])
        self._users_title.setText(f"👥 Участники ({len(users)})")
        table = self._users_table
        sel_name = None
        if 0 <= table.currentRow() < table.rowCount():
            it = table.item(table.currentRow(), 0)
            if it is not None:
                sel_name = it.text()
        table.setRowCount(len(users))
        for r, u in enumerate(users):
            name_i = QTableWidgetItem(str(u.get("name", "?")))
            if u.get("admin"):
                name_i.setForeground(Qt.GlobalColor.green)
            role = QTableWidgetItem("админ (хост)" if u.get("admin") else "гость")
            age = QTableWidgetItem(_fmt_uptime(u.get("age_s", 0)))
            status = QTableWidgetItem(
                "✍ печатает" if u.get("typing") else "—")
            for c, item in enumerate((name_i, role, age, status)):
                item.setData(Qt.ItemDataRole.DisplayRole, item.text())
                table.setItem(r, c, item)
        # Восстанавливаем выделение после перерисовки.
        if sel_name:
            for r in range(table.rowCount()):
                it = table.item(r, 0)
                if it is not None and it.text() == sel_name:
                    table.selectRow(r)
                    break
        # Кик активен, только если выбран не-админ.
        row = table.currentRow()
        kickable = False
        if 0 <= row < len(users):
            kickable = not users[row].get("admin") and users[row].get("name") != self._controller.name
        self._btn_kick.setEnabled(kickable)

        # Музыка.
        ms = d.get("music") or {}
        track = ms.get("track") or {}
        if track:
            title_txt = (track.get("artist") or "") + \
                (" — " if track.get("artist") else "") + \
                (track.get("title") or track.get("filename") or "?")
        else:
            title_txt = f"трек не выбран — файлы клади в:\n{self._music_dir}"
        self._music_track.setText(("▶ " if ms.get("playing") else "⏸ ") + title_txt)
        pos = float(ms.get("position", 0) or 0)
        dur = float(track.get("duration") or 0)
        self._music_bar.setValue(int(1000 * pos / dur) if dur > 0 else 0)
        self._music_time.setText(
            f"{_fmt_mmss(pos)} / {_fmt_mmss(dur)}" if dur else _fmt_mmss(pos))
        queue = ms.get("queue") or []
        nxt = ""
        if queue:
            t0 = queue[0].get("track") or {}
            nt = (t0.get("artist") or "") + \
                (" — " if t0.get("artist") else "") + (t0.get("title") or "?")
            nxt = f"  ·  далее: {nt}"
        self._music_info.setText(
            f"Очередь: {len(queue)}  ·  Библиотека: {d.get('lib_n', 0)}"
            f"  ·  open-DJ: {'вкл' if ms.get('open_dj') else 'выкл'}"
            f"  ·  Громкость: {int(ms.get('volume', 80))}%{nxt}")
        # Кнопка play/pause и громкость — синхронно с состоянием.
        self._btn_play.setText("⏸ Пауза" if ms.get("playing") else "▶ Играть")
        vol = int(ms.get("volume", 80))
        if self._volume.sliderPosition() != vol and not self._volume.isSliderDown():
            self._volume.setValue(vol)

    # ── закрытие ──────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        if self._controller.is_running():
            ans = QMessageBox.question(
                self, "Сервер ещё работает",
                "Остановить сервер и выйти?\n"
                "(Да — сервер остановится, друзья отключатся)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._on_stop()
        self._poller.stop()
        event.accept()


def main() -> int:
    data_dir = DATA_DIR / HOST_DATA_DIR_NAME

    # (v3.5.0) Стабильность: краш/сегфолт/зависание — в файл в logs/,
    # а не «окно не отвечает» без объяснений (см. lib/stability.py).
    from lib.stability import HangWatchdog, enable_faulthandler, install_excepthooks

    enable_faulthandler(data_dir / "logs")
    install_excepthooks(data_dir / "logs")

    app = QApplication(sys.argv)
    app.setApplicationName("Friend Relay — Сервер")

    # Глобальная таблица стилей для читаемости QMessageBox и других стандартных диалогов
    app.setStyleSheet(f"""
        QWidget {{
            background-color: {_BG};
            color: {_TEXT};
        }}
        QMessageBox {{
            background-color: {_SURFACE};
            color: {_TEXT};
        }}
        QMessageBox QLabel {{
            color: {_TEXT};
            background-color: transparent;
        }}
        QMessageBox QPushButton {{
            background-color: {_SURFACE3};
            color: {_TEXT};
            border: 1px solid {_BORDER};
            padding: 6px 12px;
            border-radius: 6px;
        }}
        QMessageBox QPushButton:hover {{
            background-color: {_ACCENT};
        }}
    """)

    win = ServerWindow(data_dir)

    # (v3.5.0) Watchdog GUI-потока консоли сервера: окно замерло >15с —
    # дамп стеков всех потоков в <data_dir>/logs/hang_<дата>.log.
    from PySide6.QtCore import QTimer as _QTimer

    watchdog = HangWatchdog(data_dir / "logs", stuck_after_s=15.0)
    beat = _QTimer(win)
    beat.setInterval(2000)
    beat.timeout.connect(watchdog.heartbeat)
    beat.start()
    watchdog.start()

    win.show()
    code = app.exec()
    watchdog.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
