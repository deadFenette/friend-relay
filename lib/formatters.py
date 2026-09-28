from __future__ import annotations

import time


def fmt_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "Б" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}ТБ"


def fmt_speed(bytes_per_sec: float) -> str:
    return f"{fmt_size(max(0, int(bytes_per_sec)))}/с"


def transfer_progress_text(received: int, total: int | None, elapsed: float,
                           arrow: str = "↓") -> str:
    """Единая строка прогресса передачи для Qt-клиента и legacy-tkinter:
    «↓ 45% · 1.4МБ/с · 3.1МБ/6.9МБ» (или без процентов, если размер неизвестен).

    elapsed <= 0 и/или received == 0 не должны делить на ноль; скорость
    считается по среднему с начала передачи (сглаживает дрожание)."""
    speed = fmt_speed(received / elapsed) if elapsed > 0.05 and received > 0 else "…"
    done = fmt_size(received)
    if total:
        pct = int(received * 100 / total) if total else 0
        pct = max(0, min(100, pct))
        return f"{arrow} {pct}% · {speed} · {done}/{fmt_size(total)}"
    return f"{arrow} {speed} · {done}"


def download_progress_text(received: int, total: int | None, elapsed: float) -> str:
    return transfer_progress_text(received, total, elapsed, arrow="↓")


def upload_progress_text(sent: int, total: int | None, elapsed: float) -> str:
    return transfer_progress_text(sent, total, elapsed, arrow="↑")


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))
