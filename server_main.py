#!/usr/bin/env python3
"""Friend Relay — автономный запуск сервера БЕЗ GUI.

    python server_main.py                     # имя/ключ из settings.json
    python server_main.py --name Емеля --port 8420
    python server_main.py --max-file-size 1G  # лимит файла (2G, 500M, 2048 = МБ)
    python server_main.py --quiet             # совсем без баннера (systemd)

ФИЛОСОФИЯ КОНСОЛИ (v3.4.0): консоль НЕМАЯ. При старте печатается короткая
шпаргалка (порт/ключи/адреса) — и всё. Дальше в консоли появляются ТОЛЬКО
ошибки и краши. Полный журнал (входы, кики, старты, остановки) пишется в
файл:  <data_dir>/logs/server.log  (ротация по дням, 7 дней).

Красивый экран со состоянием сервера — отдельная программа:
    python server_gui.py        или        RUN_SERVER_GUI.bat

Админ = имя хоста (--name, любое — хоть «Емеля»). Имя хоста защищено
admin_key: занять его можно только с ключом (или с самой машины хоста).
Гость, введя имя хоста, автоматически входит как «Имя#2» (дискриминаторы
как в Discord).

Как стать админом (v3.5.1):
  • с этой машины            — само (loopback: Qt-клиент/GUI на хосте);
  • с другого устройства     — в веб-клиенте: имя хоста + ключ в поле
                             «Ключ админа»;
  • ключ задаётся через      --admin-key ТВОЙ_КЛЮЧ (сохраняется в
                             relay_data/admin_key.json и переживает
                             рестарты), иначе генерируется при первом
                             старте и печатается в шпаргалке выше.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.constants import (
    DATA_DIR,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_PORT,
    HOST_DATA_DIR_NAME,
)
from lib.net_utils import detect_vpn_zt_conflict, get_interfaces, get_local_ips
from lib.relay_server import RelayServer
from lib.server_log import log_exception, setup_server_logging
from lib.storage import load_settings, save_settings


def _setup_console() -> None:
    """UTF-8 stdout/stderr (Windows-консоли иначе кракозябры с кириллицей)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _project_version() -> str:
    try:
        data = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
        return str(data.get("version", "?"))
    except Exception:
        return "?"


def _resolve_admin_key(cli_value: str, data_dir: Path) -> str:
    """Ключ админа: из CLI (v3.5.1 — сохраняем в admin_key.json, чтобы
    ключ ПЕРЕЖИВАЛ рестарты: раньше ключ из --admin-key жил только до
    остановки, и на следующем запуске без флага генерировался новый —
    поле «Ключ админа» в вебе молча переставало работать), иначе из
    data_dir/admin_key.json, иначе генерируем и сохраняем."""
    path = data_dir / "admin_key.json"
    if cli_value:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"admin_key": cli_value}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass  # ключ будет только на этот запуск
        return cli_value
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
        pass  # ключ будет только на этот запуск
    return key


# (v3.8.2) Человекочитаемые размеры для --max-file-size.
# Ключи словаря — уже в нижнем регистре: и латиница, и кириллица.
_UNITS_MB = {
    "k": 1.0 / 1024, "kb": 1.0 / 1024, "к": 1.0 / 1024, "кб": 1.0 / 1024,
    "m": 1.0, "mb": 1.0, "м": 1.0, "мб": 1.0,
    "g": 1024.0, "gb": 1024.0, "г": 1024.0, "гб": 1024.0,
    "t": 1024.0 * 1024, "tb": 1024.0 * 1024, "т": 1024.0 * 1024, "тб": 1024.0 * 1024,
}


def parse_size_mb(text: str) -> int:
    """«2G» / «500M» / «1.5GB» / «2ГБ» / «2048» (голое число = МБ) → МБ.

    ValueError с человеческим сообщением, если разобрать не удалось или
    получилось меньше 1 МБ. Кириллические единицы (2ГБ, 500МБ) принимаются
    наравне с латинскими — в консоли Windows их и набирать проще.
    """
    s = str(text).strip().lower().replace(" ", "").replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zа-яё]*)", s)
    if not m:
        raise ValueError(
            f"не понял размер «{text}» — примеры: 2G, 1.5GB, 500M, 2048")
    value, unit = float(m.group(1)), m.group(2)
    if unit == "":
        mb = value  # голое число = МБ, совместимо со старым --max-mb
    elif unit in _UNITS_MB:
        mb = value * _UNITS_MB[unit]
    else:
        raise ValueError(
            f"не знаю единицу «{unit}» в «{text}» — "
            "используй K/КБ, M/МБ, G/ГБ или T/ТБ")
    if mb < 1:
        raise ValueError("минимальный лимит файла — 1 МБ")
    return int(round(mb))


def resolve_max_mb(cli_size: str | None, cli_mb: int | None,
                   settings: dict) -> int:
    """Какой лимит файла реально действует (v3.8.2).

    Приоритет: --max-file-size > --max-mb (старый флаг) >
    settings.json «max_file_size_mb» (туда пишет свой спиннер GUI-хост)
    > 2 ГБ (DEFAULT_MAX_FILE_SIZE). ValueError — с текстом для консоли.
    """
    if cli_size:
        return parse_size_mb(cli_size)
    if cli_mb is not None:
        if cli_mb < 1:
            raise ValueError("--max-mb должен быть >= 1")
        return int(cli_mb)
    saved = settings.get("max_file_size_mb") if settings else None
    if saved is not None:
        try:
            v = int(saved)
            if v >= 1:
                return v
        except (TypeError, ValueError):
            pass  # мусор в settings — тихо идём в дефолт
    return DEFAULT_MAX_FILE_SIZE // (1024 * 1024)


def parse_args() -> argparse.Namespace:
    settings = {}
    try:
        settings = load_settings() or {}
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="Friend Relay: автономный сервер (без GUI). "
                    "Значения по умолчанию берутся из settings.json.")
    p.add_argument("--name", default=str(settings.get("name", "") or "Host"),
                   help="имя хоста = АДМИН (по умолчанию: из settings.json)")
    p.add_argument("--port", type=int,
                   default=int(settings.get("port", DEFAULT_PORT) or DEFAULT_PORT),
                   help=f"HTTP-порт (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--key", default=str(settings.get("access_key", "") or ""),
                   help="ключ доступа (по умолчанию: из settings.json)")
    p.add_argument("--admin-key", default="",
                   help="ключ админа: с именем хоста даёт права админа "
                        "с любого устройства (сохраняется в "
                        "relay_data/admin_key.json, переживает рестарты; "
                        "по умолчанию генерируется)")
    p.add_argument("--max-file-size", default=None, metavar="РАЗМЕР",
                   help="лимит размера файла: 2G, 1.5GB, 500M или 2048 "
                        "(голое число — МБ; по умолчанию 2G или значение "
                        "из settings.json)")
    p.add_argument("--max-mb", type=int, default=None,
                   help="лимит размера файла в МБ (старый способ; "
                        "удобнее --max-file-size 2G)")
    p.add_argument("--bind", default=str(settings.get("server_host", "0.0.0.0") or "0.0.0.0"),
                   help="интерфейс прослушивания (по умолчанию 0.0.0.0, --bind 127.0.0.1 для локального)")
    p.add_argument("--data-dir", default="",
                   help=f"папка данных (по умолчанию {DATA_DIR / HOST_DATA_DIR_NAME})")
    p.add_argument("--quiet", action="store_true",
                   help="без стартовой шпаргалки в консоли (для systemd/служб)")
    p.add_argument("--encryption", action="store_true",
                   help="включить AES-256-GCM шифрование сообщений")
    p.add_argument("--secret-key", default=str(settings.get("secret_key", "") or ""),
                   help="секретный ключ для шифрования (требуется с --encryption)")
    return p.parse_args()


def print_hint(relay: RelayServer, args: argparse.Namespace, data_dir: Path,
               admin_key: str) -> None:
    """Одноразовая шпаргалка при старте. Больше в консоли НИЧЕГО не будет
    (только ошибки). Состояние сервера — в server_gui.py или в журнале.

    v3.4.1: с пометками типов интерфейсов и предупреждением VPN+ZT.
    """
    music_dir = data_dir / "music"
    tracks = 0
    try:
        bot = relay.get_bot_manager().get_bot("music")
        if bot is not None:
            music_dir = Path(bot.music_dir)
            tracks = len(bot.library)
    except Exception:
        pass

    ifaces = get_interfaces()
    ips = get_local_ips()
    line = "=" * 64
    print(line)
    print(f"  Friend Relay v{_project_version()} — сервер работает")
    print(line)
    print(f"  Хост/админ : {args.name}")
    print(f"  Admin key  : {admin_key}")
    print(f"  {'':14}(v3.5.1) чтобы стать админом с другого устройства:")
    print(f"  {'':14}в веб-клиенте введи имя «{args.name}» + этот ключ")
    print(f"  {'':14}в поле «Ключ админа». Ключ сохранён в admin_key.json")
    print(f"  {'':14}и переживает рестарты. На этой машине админство")
    print(f"  {'':14}само (loopback), ключ не нужен.")
    print(f"  Bind       : {args.bind}")
    print(f"  Порт       : {args.port} (HTTP+HTTPS) · голос: {args.port + 1} · voxel: {args.port + 2}")
    print(f"  Ключ доступа: {'установлен' if args.key else 'НЕТ — открытый сервер!'}")
    gb = args.max_mb / 1024.0
    print(f"  Лимит файла : {args.max_mb} МБ ({gb:.4g} ГБ)  "
          f"— меняется --max-file-size 2G|500M|2048")

    _KIND = {"zerotier": "ZT✨", "tailscale": "TS✨",
             "physical": "LAN", "vpn": "VPN❗"}
    if ifaces:
        urls = []
        for i in ifaces:
            if i.kind == "loopback":
                continue
            tag = _KIND.get(i.kind, "")
            sep = f" [{tag}] " if tag else " "
            urls.append(f"http://{i.ip}:{args.port}{sep}")
            urls.append(f"https://{i.ip}:{args.port}{sep}")
        # Печатаем в 1-2 строки (в консоли помещается)
        print("  Веб        : " + urls[0])
        for u in urls[1:]:
            print(f"               {u}")
    elif ips:
        print("  Веб        : " + "  ·  ".join(f"http://{ip}:{args.port}" for ip in ips))

    # Предупреждение если ZeroTier + VPN одновременно
    has_conflict, tip = detect_vpn_zt_conflict()
    if has_conflict:
        print("\n  ⚠  " + tip)
        print("     Диагностика + фикс (до перезагрузки, безопасно):")
        print("       python scripts\\fix_vpn_zt.py diagnose   (без админа)")
        print("       scripts\\fix_zerotier_vpn.bat  (ПКМ → Запуск от имени админа)")

    print(f"\n  Данные     : {data_dir}")
    print(f"  Музыка     : {music_dir}  ({tracks} треков)")
    print(f"  Журнал     : {data_dir / 'logs' / 'server.log'}  (консоль — только ошибки)")
    print("  Экран сервера: python server_gui.py   (статус, участники, музыка)")
    print("  Остановка  : Ctrl+C")
    print(line)


def main() -> int:
    _setup_console()
    args = parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR / HOST_DATA_DIR_NAME
    log = setup_server_logging(data_dir, console_errors=True)

    # (v3.5.0) Стабильность: краш любого потока и сегфолт — в файл в
    # logs/, а не исчезает бесследно (см. lib/stability.py).
    from lib.stability import HangWatchdog, enable_faulthandler, install_excepthooks

    enable_faulthandler(data_dir / "logs")
    install_excepthooks(data_dir / "logs", on_error=log.error)

    watchdog: HangWatchdog | None = None

    # Сохраняем настройки в settings.json для синхронизации с GUI
    st: dict = {}
    try:
        st = load_settings() or {}
    except Exception:
        pass

    # (v3.8.2) Лимит файла: --max-file-size > --max-mb > settings.json > 2ГБ.
    try:
        args.max_mb = resolve_max_mb(args.max_file_size, args.max_mb, st)
    except ValueError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    try:
        st["name"] = args.name
        st["port"] = args.port
        st["access_key"] = args.key
        st["server_host"] = args.bind
        st["encryption_enabled"] = args.encryption
        st["secret_key"] = args.secret_key
        st["max_file_size_mb"] = args.max_mb  # GUI-спиннер увидит то же число
        save_settings(st)
    except Exception:
        pass

    try:
        admin_key = _resolve_admin_key(args.admin_key, data_dir)

        relay = RelayServer(
            data_dir,
            host_name=args.name,
            access_key=args.key,
            max_file_size=args.max_mb * 1024 * 1024,
            admin_key=admin_key,
        )

        if not relay.start(args.bind, args.port):
            msg = (f"не удалось занять порт {args.port} "
                   f"(занят другим процессом? запущен GUI-хост?)")
            log.error(msg)
            print(f"ОШИБКА: {msg}", file=sys.stderr)
            return 2

        if not args.quiet:
            print_hint(relay, args, data_dir, admin_key)

        stop = threading.Event()

        # (v3.5.0) Watchdog ядра сервера: поток-зонд раз в 2с дёргает лёгкий
        # метод фасада get_music_sync_state() (проходит через внутренние
        # локи). Если ядро встало на локе/IO — зонд замирает вместе с ним,
        # сердцебиение пропадает и монитор пишет дамп стеков ВСЕХ потоков
        # в logs/hang_<дата>.log — вместо «висит и непонятно почему».
        watchdog = HangWatchdog(data_dir / "logs", stuck_after_s=30.0)

        def _liveness_probe() -> None:
            while not stop.is_set():
                try:
                    relay.get_music_sync_state()
                except Exception:
                    pass
                watchdog.heartbeat()
                stop.wait(2.0)

        threading.Thread(target=_liveness_probe, name="server-probe",
                         daemon=True).start()
        watchdog.start()

        def _sig(_signum, _frame) -> None:
            stop.set()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _sig)
                except (ValueError, OSError):
                    pass  # не главный поток / не поддерживается

        # Немая консоль: просто ждём сигнала. Вся жизнь сервера — в журнале.
        try:
            while not stop.is_set():
                stop.wait(3600)
        except KeyboardInterrupt:
            pass
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        log_exception("краш сервера")
        return 1
    finally:
        if watchdog is not None:
            watchdog.stop()
        try:
            relay.stop()  # type: ignore[possibly-undefined]
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
