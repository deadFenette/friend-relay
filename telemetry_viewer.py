"""Standalone Telemetry Viewer — отдельное приложение для просмотра
телеметрии работающего Friend Relay хоста В РЕАЛЬНОМ ВРЕМЕНИ.

Это НЕ часть PySide6-клиента. Это отдельная программа, которая запускается
когда нужно — параллельно с клиентом (или вместо него). Хост уже должен
быть запущен (в клиенте нажмите «Подключить» с ролью «хост»).

Запуск:
    python telemetry_viewer.py            # подключение к localhost:8420
    python telemetry_viewer.py --host http://192.168.1.10:8420
    python telemetry_viewer.py --key ВАШ_ACCESS_KEY
    RUN_TELEMETRY.bat                     # Windows, двойной клик

ЧТО ПОКАЗЫВАЕТ
  1. Серверная телеметрия (GET /telemetry/summary) — счётчики HTTP-запросов,
     сообщений, файлов, голоса, пики онлайна, латентности. Это то же самое,
     что хост пишет в <data_dir>/telemetry/summary.json, но в реальном
     времени — без необходимости открывать файл.

  2. Клиентская телеметрия (если клиент PySide6 запущен и пишет её в
     <data_dir>/telemetry/client_summary.json) — счётчики GUI-событий:
     открытия экранов, клики по ссылкам-приглашениям, ошибки диалогов,
     длительности матчей.

ПРИВАТНОСТЬ
  Телеметрия — ТОЛЬКО ЛОКАЛЬНАЯ и БЕЗ КОНТЕНТА. Этот viewer тянет JSON с
  вашего же хоста по HTTP (никаких внешних серверов). В нём НЕ ВЫВОДЯТСЯ
  тексты сообщений, имена участников, коды матчей — только числа
  (счётчики, длительности, размеры). См. lib/telemetry.py и
  lib/client_telemetry.py для подробностей.

ФАЙЛЫ ТЕЛЕМЕТРИИ (на машине хоста, в <data_dir>/telemetry/):
  summary.json                        — серверный снимок (JSON)
  client_summary.json                 — клиентский снимок (JSON)
  ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt     — человеко-читаемый снимок (один файл,
                                        пишется клиентской телеметрией при
                                        каждом флуше)
  events-YYYYMMDD.jsonl               — серверные дельты по дням
  client_events-YYYYMMDD.jsonl        — клиентские дельты по дням

<data_dir> по умолчанию: ~/.friend_relay/ (Linux/Mac) или
                          %USERPROFILE%\\.friend_relay\\ (Windows)
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

REFRESH_INTERVAL_MS = 1500   # реальное время: раз в 1.5 секунды
DEFAULT_HOST = "http://localhost:8420"

# Палитра — тёмная, как у PySide6-клиента (но своя, не зависит от qt_app.theme).
_BG          = "#0B0B0F"
_SURFACE_1   = "#121218"
_SURFACE_2   = "#1C1C24"
_BORDER      = "rgba(255,255,255,0.10)"
_BORDER_SOFT = "rgba(255,255,255,0.06)"
_TEXT        = "#F0F0F5"
_TEXT_DIM    = "#8E8E99"
_TEXT_FAINT  = "#5C5C66"
_ACCENT      = "#7C6BFF"
_ACCENT_DIM  = "rgba(124,107,255,0.16)"
_SUCCESS     = "#34D399"
_DANGER      = "#FB7185"
_WARNING     = "#FBBF24"
_MONO        = "'Consolas','Menlo','Courier New',monospace"


def _data_dir() -> Path:
    """Папка данных Friend Relay на этой машине.
    Ищется там же, где клиент её создаёт: ~/.friend_relay/."""
    return Path.home() / ".friend_relay"


def _fetch_json(url: str, access_key: str = "", timeout: float = 3.0) -> dict:
    """GET JSON с хоста. Бросает исключение при ошибке — звонивший ловит."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if access_key:
        # Тот же заголовок, что в lib/client.py (HEADER_KEY).
        req.add_header(
            "X-Relay-Key",
            base64.b64encode(access_key.encode("utf-8")).decode("ascii"),
        )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_local_json(path: Path) -> dict | None:
    """Читает JSON-файл с диска (если есть). Не бросает — возвращает None."""
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _fmt_uptime(seconds: int) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}ч {m}м {sec}с"
    if m > 0:
        return f"{m}м {sec}с"
    return f"{sec}с"


# ── фоновый поток: тянет серверный и клиентский снимки ─────────────────────


class _Fetcher(QThread):
    """Фоновый поток, который периодически:
      • GET /telemetry/summary с хоста (серверная телеметрия)
      • читает client_summary.json с диска (клиентская телеметрия)
      • читает ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt (для показа «файл обновлён»)
    Ничего не блокирует GUI — результат приезжает через сигналы.
    """
    server_snapshot = Signal(dict)
    server_error    = Signal(str)
    client_snapshot = Signal(dict)
    file_info       = Signal(str, float)  # (путь, mtime)

    def __init__(self, host: str, access_key: str, interval_ms: int):
        super().__init__()
        self._host = host.rstrip("/")
        self._access_key = access_key
        self._interval_s = max(0.5, interval_ms / 1000.0)
        self._stop = threading.Event()

    def set_host(self, host: str) -> None:
        self._host = host.rstrip("/")

    def set_access_key(self, key: str) -> None:
        self._access_key = key

    def refresh_now(self) -> None:
        """Принудительно дёрнуть сразу — для кнопки «Обновить»."""
        self._stop.set()
        # Сработает exit из wait, цикл сделает один проход, и ждёт снова.

    def run(self) -> None:
        while True:
            self._stop.clear()
            try:
                self._tick()
            except Exception:
                # Никогда не роняем поток — следующая итерация попробует снова.
                pass
            # Ждём интервал ИЛИ принудительный refresh_now().
            if self._stop.wait(self._interval_s):
                # Был refresh_now — делаем ещё один проход немедленно.
                continue
            # Если stop() зовут извне (выход из приложения) — выходим.
            if self._stop.is_set():
                return

    def _tick(self) -> None:
        # 1) Серверная телеметрия через HTTP.
        try:
            snap = _fetch_json(
                f"{self._host}/telemetry/summary",
                self._access_key,
            )
            self.server_snapshot.emit(snap)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            reason = getattr(e, "reason", None) or str(e) or type(e).__name__
            self.server_error.emit(str(reason))

        # 2) Клиентская телеметрия — с локального диска (если клиент запущен).
        client_path = _data_dir() / "telemetry" / "client_summary.json"
        csnap = _read_local_json(client_path)
        if csnap is not None:
            self.client_snapshot.emit(csnap)

        # 3) Инфо о файле ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt — для показа «обновлён».
        txt_path = _data_dir() / "telemetry" / "ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt"
        try:
            if txt_path.is_file():
                mtime = txt_path.stat().st_mtime
                self.file_info.emit(str(txt_path), mtime)
        except OSError:
            pass


# ── главное окно ───────────────────────────────────────────────────────────


class TelemetryViewer(QMainWindow):
    """Standalone-окно просмотра телеметрии хоста в реальном времени."""

    def __init__(self, host: str, access_key: str):
        super().__init__()
        self.setWindowTitle("📊 Friend Relay — Телеметрия (live)")
        self.resize(1100, 760)
        self.setMinimumSize(820, 540)
        self.setStyleSheet(f"background: {_BG};")

        self._host = host
        self._access_key = access_key

        self._server_snapshot: dict | None = None
        self._client_snapshot: dict | None = None
        self._server_error: str = ""
        self._last_file_mtime: float = 0.0

        self._build_ui()

        # Фоновый поток — тянет данные, не блокируя GUI.
        self._fetcher = _Fetcher(host, access_key, REFRESH_INTERVAL_MS)
        self._fetcher.server_snapshot.connect(self._on_server_snapshot)
        self._fetcher.server_error.connect(self._on_server_error)
        self._fetcher.client_snapshot.connect(self._on_client_snapshot)
        self._fetcher.file_info.connect(self._on_file_info)
        self._fetcher.start()

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ── Заголовок + строка подключения ───────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("📊 Телеметрия (live)")
        title.setStyleSheet(
            f"color: {_TEXT}; font-size: 20px; font-weight: 700;"
        )
        header.addWidget(title)
        header.addStretch(1)

        # Статус-индикатор — мигает зелёным когда есть данные, красным — ошибка.
        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"color: {_TEXT_FAINT}; font-size: 18px;")
        header.addWidget(self._status_dot)
        self._status_text = QLabel("подключение…")
        self._status_text.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 12px; font-family: {_MONO};"
        )
        header.addWidget(self._status_text)
        header.addSpacing(16)

        root.addLayout(header)

        # ── Панель подключения ───────────────────────────────────────────
        conn = QHBoxLayout()
        conn.setSpacing(8)
        conn.addWidget(QLabel("Хост:"))
        self._host_edit = QLineEdit(self._host)
        self._host_edit.setStyleSheet(self._input_qss())
        self._host_edit.returnPressed.connect(self._on_reconnect)
        conn.addWidget(self._host_edit, 3)

        conn.addWidget(QLabel("Access key:"))
        self._key_edit = QLineEdit(self._access_key)
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setStyleSheet(self._input_qss())
        self._key_edit.returnPressed.connect(self._on_reconnect)
        conn.addWidget(self._key_edit, 2)

        self._btn_connect = QPushButton("Подключиться")
        self._btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_connect.setStyleSheet(self._btn_qss(primary=True))
        self._btn_connect.clicked.connect(self._on_reconnect)
        conn.addWidget(self._btn_connect)

        self._btn_open_file = QPushButton("📂 Открыть файл")
        self._btn_open_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open_file.setStyleSheet(self._btn_qss())
        self._btn_open_file.setToolTip(
            "Открыть файл ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt в системе"
        )
        self._btn_open_file.clicked.connect(self._on_open_file)
        conn.addWidget(self._btn_open_file)

        root.addLayout(conn)

        # ── Плашка приватности ───────────────────────────────────────────
        priv = QLabel(
            "🔒 Телеметрия ТОЛЬКО ЛОКАЛЬНАЯ и БЕЗ КОНТЕНТА: счётчики и числа, "
            "без текстов/имён/кодов. Этот viewer читает JSON с вашего же хоста "
            "и с диска — никаких внешних серверов."
        )
        priv.setWordWrap(True)
        priv.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 12px;"
            f"background: {_SURFACE_1};"
            f"border: 1px solid {_BORDER_SOFT};"
            f"border-radius: 8px; padding: 10px 14px;"
        )
        root.addWidget(priv)

        # ── KPI-полоса: 4 крупные плитки (онлайн, пик, сообщения, файлы) ─
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self._kpi_online = self._make_kpi("🌐 Онлайн сейчас", "—")
        self._kpi_peak   = self._make_kpi("🚀 Пик за сессию", "—")
        self._kpi_msgs   = self._make_kpi("💬 Сообщения чата", "—")
        self._kpi_files  = self._make_kpi("📁 Файлы отправлены", "—")
        for w in (self._kpi_online, self._kpi_peak, self._kpi_msgs, self._kpi_files):
            kpi_row.addWidget(w, 1)
        root.addLayout(kpi_row)

        # ── Скролл с двумя колонками: сервер / клиент ────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll_host = QWidget()
        scroll_layout = QGridLayout(scroll_host)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(12)

        # Слева — серверная телеметрия.
        server_card, self._server_body = self._make_card("🌐 Серверная телеметрия (хост)")
        scroll_layout.addWidget(server_card, 0, 0)

        # Справа — клиентская телеметрия.
        client_card, self._client_body = self._make_card("🖥️ Клиентская телеметрия (GUI)")
        scroll_layout.addWidget(client_card, 0, 1)

        scroll_layout.setColumnStretch(0, 1)
        scroll_layout.setColumnStretch(1, 1)

        scroll.setWidget(scroll_host)
        root.addWidget(scroll, 1)

        # ── Нижняя строка: файл + аптайм ────────────────────────────────
        footer = QHBoxLayout()
        self._file_label = QLabel("📁 Файл: …")
        self._file_label.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; font-family: {_MONO};"
        )
        footer.addWidget(self._file_label)
        footer.addStretch(1)
        self._uptime_label = QLabel("")
        self._uptime_label.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; font-family: {_MONO};"
        )
        footer.addWidget(self._uptime_label)
        root.addLayout(footer)

        # Первичная отрисовка.
        self._render_server()
        self._render_client()
        self._render_status()

    def _input_qss(self) -> str:
        return f"""
            QLineEdit {{
                background: {_SURFACE_2};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                font-family: {_MONO};
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border: 1px solid {_ACCENT};
            }}
        """

    def _btn_qss(self, primary: bool = False) -> str:
        bg = _ACCENT if primary else _SURFACE_2
        hover = "#8F80FF" if primary else "#272730"
        fg = "#FFFFFF" if primary else _TEXT
        return f"""
            QPushButton {{
                background: {bg};
                color: {fg};
                border: 1px solid {bg if primary else _BORDER};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {hover};
            }}
        """

    def _make_kpi(self, title: str, value: str) -> QFrame:
        """Крупная плитка KPI: заголовок сверху, значение — гигантскими цифрами."""
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE_1};
                border: 1px solid {_BORDER_SOFT};
                border-radius: 10px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(4)
        t = QLabel(title)
        t.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        v.addWidget(t)
        val = QLabel(value)
        val.setStyleSheet(
            f"color: {_TEXT}; font-size: 26px; font-weight: 700; "
            f"font-family: {_MONO};"
        )
        v.addWidget(val)
        card._value_label = val  # type: ignore[attr-defined]
        return card

    def _make_card(self, title: str) -> tuple[QFrame, QLabel]:
        """Карточка-секция с заголовком и телом (QLabel с переносом строк)."""
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE_1};
                border: 1px solid {_BORDER};
                border-radius: 12px;
            }}
        """)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(8)
        t = QLabel(title)
        t.setStyleSheet(
            f"color: {_TEXT}; font-size: 15px; font-weight: 700;"
        )
        v.addWidget(t)
        body = QLabel("…")
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet(
            f"color: {_TEXT}; font-family: {_MONO}; font-size: 11px;"
        )
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(body, 1)
        return card, body

    # ── колбэки фонового потока ─────────────────────────────────────────
    def _on_server_snapshot(self, snap: dict) -> None:
        self._server_snapshot = snap
        self._server_error = ""
        self._render_server()
        self._render_kpi()
        self._render_status()

    def _on_server_error(self, reason: str) -> None:
        self._server_snapshot = None
        self._server_error = reason
        self._render_server()
        self._render_kpi()
        self._render_status()

    def _on_client_snapshot(self, snap: dict) -> None:
        self._client_snapshot = snap
        self._render_client()

    def _on_file_info(self, path: str, mtime: float) -> None:
        self._last_file_mtime = mtime
        from datetime import datetime
        ts = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        self._file_label.setText(f"📁 Файл обновлён: {ts}   ({path})")

    # ── действия ────────────────────────────────────────────────────────
    def _on_reconnect(self) -> None:
        new_host = self._host_edit.text().strip()
        new_key = self._key_edit.text().strip()
        if not new_host:
            return
        self._host = new_host
        self._access_key = new_key
        self._fetcher.set_host(new_host)
        self._fetcher.set_access_key(new_key)
        self._fetcher.refresh_now()

    def _on_open_file(self) -> None:
        from PySide6.QtGui import QDesktopServices, QUrl
        txt = _data_dir() / "telemetry" / "ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt"
        if not txt.exists():
            # Может быть, файл ещё не создан — откроем хотя бы папку.
            folder = _data_dir() / "telemetry"
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(txt)))

    # ── рендеринг ───────────────────────────────────────────────────────
    def _render_status(self) -> None:
        if self._server_snapshot is not None:
            self._status_dot.setStyleSheet(f"color: {_SUCCESS}; font-size: 18px;")
            self._status_text.setText("подключено ● live")
            # Аптайм хоста:
            uptime = self._server_snapshot.get("uptime_s", 0)
            self._uptime_label.setText(
                f"Uptime хоста: {_fmt_uptime(int(uptime))}    "
                f"Запись: {'ВКЛ' if self._server_snapshot.get('enabled') else 'ВЫКЛ'}    "
                f"Обновление каждые {REFRESH_INTERVAL_MS} мс"
            )
        elif self._server_error:
            self._status_dot.setStyleSheet(f"color: {_DANGER}; font-size: 18px;")
            self._status_text.setText(f"ошибка: {self._server_error[:60]}")
            self._uptime_label.setText("")
        else:
            self._status_dot.setStyleSheet(f"color: {_WARNING}; font-size: 18px;")
            self._status_text.setText("подключение…")
            self._uptime_label.setText("")

    def _render_kpi(self) -> None:
        snap = self._server_snapshot or {}
        gauges = snap.get("gauges", {})
        counters = snap.get("counters", {})
        online = gauges.get("server.online_now", {}).get("v", "—")
        peak = gauges.get("server.online_peak", {}).get("peak", "—")
        msgs = (counters.get("chat.text", 0)
                + counters.get("chat.text.encrypted", 0)
                + counters.get("channel.messages", 0))
        files = counters.get("file.uploaded", 0)
        self._kpi_online._value_label.setText(str(online))   # type: ignore[attr-defined]
        self._kpi_peak._value_label.setText(str(peak))       # type: ignore[attr-defined]
        self._kpi_msgs._value_label.setText(str(msgs))       # type: ignore[attr-defined]
        self._kpi_files._value_label.setText(str(files))     # type: ignore[attr-defined]

    def _render_server(self) -> None:
        snap = self._server_snapshot
        if snap is None:
            if self._server_error:
                html = (
                    f"<div style='color:{_DANGER};'>⚠ Не удалось получить серверную телеметрию:</div>"
                    f"<div style='color:{_TEXT_DIM}; margin-top:6px;'>{self._server_error}</div>"
                    f"<div style='color:{_TEXT_FAINT}; margin-top:10px; font-size:10px;'>"
                    f"Проверь, что хост запущен (в клиенте: Настройки → роль «хост» → Подключить),"
                    f" и что URL правильный.</div>"
                )
            else:
                html = (
                    f"<div style='color:{_TEXT_DIM};'>Ожидание данных с хоста…</div>"
                )
            self._server_body.setText(html)
            return

        env = snap.get("env", {})
        lines: list[str] = []
        lines.append(
            f"<div style='color:{_TEXT_DIM}; font-size:10px;'>"
            f"версия: <b style='color:{_TEXT};'>{env.get('app_version', '?')}</b>  "
            f"платформа: <b style='color:{_TEXT};'>{env.get('platform', '?')}</b>  "
            f"python: <b style='color:{_TEXT};'>{env.get('python', '?')}</b>"
            f"</div>"
        )
        lines.append(
            f"<div style='color:{_TEXT_DIM}; font-size:10px; margin-top:4px;'>"
            f"uptime: <b style='color:{_TEXT};'>{_fmt_uptime(int(snap.get('uptime_s', 0)))}</b>  "
            f"запись: <b style='color:{_SUCCESS if snap.get('enabled') else _DANGER};'>"
            f"{'ВКЛ' if snap.get('enabled') else 'ВЫКЛ'}</b>"
            f"</div>"
        )

        gauges = snap.get("gauges", {})
        if gauges:
            lines.append(self._section_title("ГЕЙДЖИ (текущие значения + пики)"))
            for k in sorted(gauges):
                g = gauges[k]
                lines.append(
                    f"<div style='color:{_TEXT};'>"
                    f"<span style='color:{_TEXT_DIM};'>{k}</span> "
                    f"→ <b>{g.get('v', '?')}</b>"
                    f" <span style='color:{_TEXT_FAINT};'>(peak {g.get('peak', '?')})</span>"
                    f"</div>"
                )

        counters = snap.get("counters", {})
        if counters:
            lines.append(self._section_title("СЧЁТЧИКИ СОБЫТИЙ"))
            for k in sorted(counters):
                lines.append(
                    f"<div style='color:{_TEXT};'>"
                    f"<span style='color:{_TEXT_DIM};'>{k}</span> → <b>{counters[k]}</b>"
                    f"</div>"
                )

        timings = snap.get("timings", {})
        if timings:
            lines.append(self._section_title("ЛАТЕНТНОСТИ (min / avg / max / n, мс)"))
            for k in sorted(timings):
                t = timings[k]
                lines.append(
                    f"<div style='color:{_TEXT};'>"
                    f"<span style='color:{_TEXT_DIM};'>{k}</span> "
                    f"→ <b>{t.get('min_ms', 0):.1f}</b> / "
                    f"<b>{t.get('avg_ms', 0):.1f}</b> / "
                    f"<b>{t.get('max_ms', 0):.1f}</b>"
                    f" <span style='color:{_TEXT_FAINT};'>(n={t.get('count', 0)})</span>"
                    f"</div>"
                )

        if snap.get("overflow"):
            lines.append(
                f"<div style='color:{_WARNING}; margin-top:6px;'>"
                f"⚠ {snap['overflow']} событий не влезло в лимит ключей"
                f"</div>"
            )

        self._server_body.setText("<br>".join(lines))

    def _render_client(self) -> None:
        snap = self._client_snapshot
        if snap is None:
            html = (
                f"<div style='color:{_TEXT_DIM};'>"
                f"Клиентская телеметрия пока недоступна.</div>"
                f"<div style='color:{_TEXT_FAINT}; margin-top:8px; font-size:10px;'>"
                f"Она появляется, когда PySide6-клиент Friend Relay запущен и пишет "
                f"снимок в <code>{_data_dir() / 'telemetry' / 'client_summary.json'}</code>.</div>"
            )
            self._client_body.setText(html)
            return

        env = snap.get("env", {})
        lines: list[str] = []
        lines.append(
            f"<div style='color:{_TEXT_DIM}; font-size:10px;'>"
            f"версия: <b style='color:{_TEXT};'>{env.get('app_version', '?')}</b>  "
            f"роль: <b style='color:{_TEXT};'>{env.get('role', 'client')}</b>"
            f"</div>"
        )
        lines.append(
            f"<div style='color:{_TEXT_DIM}; font-size:10px; margin-top:4px;'>"
            f"uptime GUI: <b style='color:{_TEXT};'>{_fmt_uptime(int(snap.get('uptime_s', 0)))}</b>  "
            f"запись: <b style='color:{_SUCCESS if snap.get('enabled') else _DANGER};'>"
            f"{'ВКЛ' if snap.get('enabled') else 'ВЫКЛ'}</b>"
            f"</div>"
        )

        counters = snap.get("counters", {})
        if counters:
            lines.append(self._section_title("СЧЁТЧИКИ GUI-СОБЫТИЙ"))
            for k in sorted(counters):
                lines.append(
                    f"<div style='color:{_TEXT};'>"
                    f"<span style='color:{_TEXT_DIM};'>{k}</span> → <b>{counters[k]}</b>"
                    f"</div>"
                )
        else:
            lines.append(
                f"<div style='color:{_TEXT_FAINT}; font-size:10px;'>"
                f"(пока ничего не произошло — попользуйтесь клиентом)"
                f"</div>"
            )

        timings = snap.get("timings", {})
        if timings:
            lines.append(self._section_title("НАБЛЮДЕНИЯ (min / avg / max / n)"))
            for k in sorted(timings):
                t = timings[k]
                lines.append(
                    f"<div style='color:{_TEXT};'>"
                    f"<span style='color:{_TEXT_DIM};'>{k}</span> "
                    f"→ <b>{t.get('min_ms', 0):.1f}</b> / "
                    f"<b>{t.get('avg_ms', 0):.1f}</b> / "
                    f"<b>{t.get('max_ms', 0):.1f}</b>"
                    f" <span style='color:{_TEXT_FAINT};'>(n={t.get('count', 0)})</span>"
                    f"</div>"
                )

        if snap.get("overflow"):
            lines.append(
                f"<div style='color:{_WARNING}; margin-top:6px;'>"
                f"⚠ {snap['overflow']} событий не влезло в лимит ключей"
                f"</div>"
            )

        self._client_body.setText("<br>".join(lines))

    @staticmethod
    def _section_title(text: str) -> str:
        return (
            f"<div style='color:{_ACCENT}; font-size:10px; margin-top:10px;"
            f"font-weight:700; letter-spacing:0.5px;'>{text}</div>"
        )

    # ── выход ───────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        try:
            self._fetcher.stop()
            self._fetcher.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)


# ── точка входа ────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Standalone Telemetry Viewer для Friend Relay. "
            "Подключается к работающему хосту и показывает телеметрию в реальном времени."
        ),
    )
    p.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"URL хоста (по умолчанию: {DEFAULT_HOST})",
    )
    p.add_argument(
        "--key", default="",
        help="Access key хоста (если требуется)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("Friend Relay Telemetry Viewer")

    # Шрифт по умолчанию — системный, но читаемой величины.
    f = QFont()
    f.setPointSize(10)
    app.setFont(f)

    win = TelemetryViewer(args.host, args.key)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
