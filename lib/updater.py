from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lib.constants import APP_VERSION

_USER_AGENT = "friend-relay-updater/1.0"
_DOWNLOAD_CHUNK = 64 * 1024


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_urls: tuple[str, ...]
    notes: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    relay_file_id: str = ""  # см. docstring check_for_updates ниже
    # v1.9.5: контроль целостности скачанного exe. Поля необязательные —
    # манифест без них работает как раньше (без проверки), но манифест
    # ПО HTTP можно подменить (MITM): без sha256 подменённый манифест
    # раздавал произвольный exe, который helper-bat молча ставил поверх
    # старого и запускал. Теперь: если sha256 указан — файл с неверным
    # хешем НЕ ставится вообще.
    sha256: str = ""
    size: int = 0

    @property
    def is_newer(self) -> bool:
        return version_tuple(self.version) > version_tuple(APP_VERSION)

    @property
    def primary_url(self) -> str | None:
        return self.download_urls[0] if self.download_urls else None

    def urls_with_labels(self) -> Iterable[tuple[str, str]]:
        for i, url in enumerate(self.download_urls):
            label = self.labels[i] if i < len(self.labels) else _guess_source_label(url)
            yield url, label


def version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in raw.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def _guess_source_label(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return "зеркало"
    if host.endswith(".onion") or ".onion." in host:
        return "onion"
    if (
        host.startswith("100.")
        or host.startswith("10.")
        or host.startswith("172.")
        or host.startswith("192.168.")
        or host in ("localhost", "127.0.0.1")
    ):
        return "локально (ZeroTier/LAN)"
    if "github" in host:
        return "GitHub"
    if "yandex" in host or "disk" in host:
        return "Яндекс Диск"
    return host


def suggest_relay_download_url(base_url: str, file_id: str) -> str | None:
    """Собирает URL к уже подключённому relay-хосту (для скачивания
    обновления прямо через тот же ZeroTier/Tailscale/приватный туннель, что
    уже используется для чата - без похода во внешний интернет).

    Раньше тут был путь "/file/<id>", которого не существует ни в одном
    HTTP-хендлере relay_server.py (там зарегистрирован только "/download/
    <id>") - в итоге эта "быстрая" ветка скачивания обновления всегда
    падала на 404 "неизвестный путь", даже когда файл на хосте реально
    был."""
    base = (base_url or "").strip().rstrip("/")
    if not base or not file_id:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        return None
    return f"{base}/download/{urllib.parse.quote(file_id)}"


def check_for_updates(manifest_url: str, timeout: int = 8) -> UpdateInfo | None:
    """Читает JSON-манифест обновлений. None — нет URL, сеть или уже актуально.

    Манифест - обычный статический JSON-файл, который можно захостить где
    угодно, откуда его видно по HTTP(S): GitHub Pages, GitHub Releases,
    Яндекс.Диск с публичной ссылкой, свой веб-сервер и т.п. Никакой особой
    инфраструктуры не требуется - это просто "GET по URL, распарсить JSON".

    Поддерживает два формата манифеста:
      - старый: {version, download_url, notes}
      - новый: {version, download_url, download_urls: [...], labels: [...], notes}

    Поле "relay_file_id" (необязательное) - если у вас есть "домашний" хост
    в приватной сети (ZeroTier/Tailscale/etc), на который вы сами заливаете
    новый .exe в файловую витрину (см. FilesScreen), укажите здесь его
    file_id. Тогда все, кто подключён к ЭТОМУ хосту, будут в первую очередь
    пытаться скачать обновление напрямую через него (быстро, без похода в
    интернет), и только если это не сработает - откатятся на публичные
    зеркала из download_urls (GitHub и т.п.). Если поле не указано - работает
    только публичный путь, что тоже нормально.
    """
    url = (manifest_url or "").strip()
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    version = str(data.get("version", "")).strip()
    if not version:
        return None

    urls: list[str] = []
    primary = str(data.get("download_url", "")).strip()
    if primary:
        urls.append(primary)
    extra = data.get("download_urls") or []
    if isinstance(extra, list):
        for u in extra:
            s = str(u).strip()
            if s and s not in urls:
                urls.append(s)
    if not urls:
        return None

    labels_raw = data.get("labels") or []
    labels = (
        tuple(str(x).strip() for x in labels_raw if isinstance(x, str))
        if isinstance(labels_raw, list)
        else ()
    )
    relay_file_id = str(data.get("relay_file_id", "")).strip()
    sha256 = str(data.get("sha256", "")).strip().lower()
    size_raw = data.get("size", 0)
    try:
        size = int(size_raw) if size_raw else 0
    except (TypeError, ValueError):
        size = 0

    info = UpdateInfo(
        version=version,
        download_urls=tuple(urls),
        notes=str(data.get("notes", "")).strip(),
        labels=labels,
        relay_file_id=relay_file_id,
        sha256=sha256,
        size=size,
    )
    return info if info.is_newer else None


def is_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.executable.lower().endswith(".exe")


def current_exe_path() -> Path | None:
    if not is_frozen_exe():
        return None
    return Path(sys.executable).resolve()


def _download_from_single_url(
    url: str,
    destination: Path,
    progress_cb: callable | None = None,
    timeout: int = 30,
) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            received = 0
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "wb") as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(received, total)
                        except Exception:
                            pass
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def download_update(
    info: UpdateInfo,
    destination: Path,
    progress_cb: callable | None = None,
    timeout: int = 30,
    preferred_base_relay_url: str = "",
    preferred_file_id: str = "",
) -> tuple[bool, str]:
    """Скачивает новый exe, пробуя все URL по порядку: сперва локальный
    relay (если подключены и manifest указал relay_file_id — см.
    check_for_updates), затем публичные зеркала из download_urls в порядке
    манифеста (GitHub Releases/Pages, Яндекс.Диск, свой сервер — что угодно
    по HTTP/HTTPS). Если первый URL недоступен — пробуем следующий, ошибка
    возвращается только если не сработал ни один.

    preferred_base_relay_url + preferred_file_id — если переданы и оба
    непустые, добавляем в начало списка прямой URL к уже подключённому
    relay-хосту (тот же ZeroTier/Tailscale/приватный туннель, что и для
    чата) — `{base}/download/{file_id}`.

    Возвращает (успех?, какой источник сработал).
    """
    urls: list[tuple[str, str]] = []

    relay_direct = suggest_relay_download_url(preferred_base_relay_url, preferred_file_id)
    if relay_direct:
        urls.append((relay_direct, "локальный релей (ZeroTier)"))

    for u, label in info.urls_with_labels():
        if u not in (x[0] for x in urls):
            urls.append((u, label))

    if not urls:
        return False, ""

    errors = []
    for url, label in urls:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        ok = _download_from_single_url(url, destination, progress_cb=progress_cb, timeout=timeout)
        if ok and _verify_download(info, destination):
            return True, label
        if ok:
            # Скачалось, но хеш/размер не сошлись: битая передача или
            # подменённый файл — ставить НЕЛЬЗЯ, пробуем следующее зеркало.
            errors.append(f"{label} (битый файл)")
        else:
            errors.append(label)

    try:
        if destination.exists():
            destination.unlink()
    except OSError:
        pass
    return False, " → ".join(errors)


def _verify_download(info: UpdateInfo, path: Path) -> bool:
    """Контроль целостности скачанного обновления (v1.9.5). Проверяются
    только поля, ПРИСУТСТВУЮЩИЕ в манифесте: старые манифесты без sha256/
    size проходят как раньше. Новые манифесты: расхождение любого поля
    = файл не ставится (обрыв передачи или подмена)."""
    import hashlib

    try:
        if info.size and path.stat().st_size != info.size:
            return False
        if info.sha256:
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 256), b""):
                    digest.update(chunk)
            import hmac as _hmac

            if not _hmac.compare_digest(digest.hexdigest(), info.sha256):
                return False
    except OSError:
        return False
    return True


def _write_windows_helper_bat(existing_exe: Path, new_exe: Path, helper_path: Path) -> None:
    existing = str(existing_exe)
    new = str(new_exe)
    helper = str(helper_path)
    backup = existing + ".bak"
    # v1.9.5: бэкап старого exe перед перезаписью + проверка, что новый
    # стартанул. Раньше «copy /y → del → start»: битый/заблокированный
    # антивирусом новый exe оставлял СЛОМАННУЮ установку — старого файла
    # уже нет, новый не запускается, откатываться некуда. Теперь при
    # неудачном старте ждём 8с и возвращаем .bak на место.
    #
    # v3.6.3: три бага, найденные дебаг-проходом (до этого цикл ожидания
    # НЕ ждал выхода процесса, а кириллические пути ломали обновление):
    #  1) фильтр был `tasklist /fi "pid %~1"` — несуществующий синтаксис
    #     (правильно `PID eq %~1`). tasklist ругался в stderr, find не
    #     находил ничего, цикл выходил через секунду → copy по ещё
    #     залоченному exe → обновление молча срывалось + возможен второй
    #     инстанс (start "" %OLD% поверх живого процесса).
    #  2) helper пишется UTF-8, но cmd читает .bat в OEM-кодировке
    #     (cp866 на русской Windows) — кириллические пути ломались.
    #     Лечится `chcp 65001 >nul` первой строкой: та же конвенция, что
    #     у RUN_*.bat (UTF-8 без BOM + CRLF).
    #  3) ожидание ограничено 60с: зависший процесс добивается taskkill,
    #     но ТОЛЬКО если pid всё ещё принадлежит нашему образу (защита
    #     от переиспользования pid). Пауза — через `ping -n 2`, а не
    #     `timeout`: timeout требует рабочий stdin и падает при
    #     перенаправлении/отсутствии консоли, ping — всегда работает.
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'set OLD="{existing}"\r\n'
        f'set NEW="{new}"\r\n'
        f'set BAK="{backup}"\r\n'
        "set /a WR_TRIES=0\r\n"
        ":wait\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "set /a WR_TRIES+=1\r\n"
        f'tasklist /fi "PID eq %~1" 2>nul | find "%~1" >nul || goto proceed\r\n'
        "if %WR_TRIES% geq 60 (\r\n"
        f'  tasklist /fi "PID eq %~1" /fo csv 2>nul | find /i "{existing_exe.name}" >nul && taskkill /f /pid %~1 >nul 2>&1\r\n'
        "  goto proceed\r\n"
        ")\r\n"
        "goto wait\r\n"
        ":proceed\r\n"
        f"copy /y %OLD% %BAK% >nul 2>&1\r\n"
        f"copy /y %NEW% %OLD% >nul\r\n"
        f"del /f /q %NEW% >nul 2>&1\r\n"
        f'start "" %OLD%\r\n'
        "ping -n 9 127.0.0.1 >nul\r\n"
        # Проверяем, что процесс нового exe жив; если нет — откат.
        # tasklist по ИМЕНИ образа: IMAGENAME eq друг_relay... имя exe
        # одинаковое у старого и нового, поэтому проверяем наличие ЛЮБОГО
        # процесса с этим именем; если старт провалился — его нет.
        f'tasklist /fi "IMAGENAME eq {existing_exe.name}" 2>nul | find /i "{existing_exe.name}" >nul\r\n'
        "if errorlevel 1 (\r\n"
        "  copy /y %BAK% %OLD% >nul\r\n"
        "  start \"\" %OLD%\r\n"
        ")\r\n"
        f"del /f /q %BAK% >nul 2>&1\r\n"
        f'(goto) 2>nul & del "{helper}"\r\n'
    )
    helper_path.write_text(script, encoding="utf-8")


def schedule_replace_and_restart(new_exe: Path) -> bool:
    current = current_exe_path()
    if current is None:
        return False
    if not new_exe.exists():
        return False

    pid = str(os.getpid())
    helper = Path(tempfile.gettempdir()) / f"friend_relay_update_{pid}.bat"
    try:
        _write_windows_helper_bat(current, new_exe, helper)
    except OSError:
        return False

    try:
        subprocess.Popen(
            ["cmd.exe", "/c", str(helper), pid],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=str(current.parent),
            close_fds=True,
        )
        return True
    except OSError:
        return False
