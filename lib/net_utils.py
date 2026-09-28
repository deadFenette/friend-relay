"""Сетевые утилиты без Qt — общие для сервера и UI (v3.4.1 fix).

«Как узнать» живёт здесь (application/util-слой), «как показать» — во вьюхах.

ИЗМЕНЕНИЯ v3.4.1 (ZeroTier + VPN фикс):
  • Раньше при включённом VPN getaddrinfo + connect(8.8.8.8) возвращали
    VPN-IP, и в приглашениях был неверный адрес; плюс server_main.py
    отбрасывал интерфейсы с 'tun'/'tap' в имени — то есть САМИ ZeroTier.
  • Теперь на Windows используется WinAPI SIO_GET_INTERFACE_LIST через
    ctypes — даёт все интерфейсы вместе с «дружественными» именами;
  • Интерфейсы классифицируются (ZERO_TIER / TAILSCALE / VPN / PHYSICAL),
    но НИКОГДА не фильтруются — адрес нужен в любом случае, даже если
    VPN его блокирует на входящие (пользователь сам в курсе по предупр.).
  • ZeroTier/Tailscale адреса считаются приоритетными и идут в начале
    списка приглашений (если они есть). Физические — следом. VPN — в конце
    (обычно на них входящие заблокированы, но показать всё равно нужно).
"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("friend_relay.net_utils")

# ═══════════════════════════════════════════════════════════════════════
# Классификация интерфейсов (только для УПОРЯДОЧИВАНИЯ, не для фильтра!)
# ═══════════════════════════════════════════════════════════════════════
IFACE_ZERO_TIER = "zerotier"     # самая высокая приоритетность для приглашений
IFACE_TAILSCALE = "tailscale"   # тоже пир-пир Overlay
IFACE_PHYSICAL = "physical"     # Ethernet/Wi-Fi
IFACE_VPN = "vpn"               # остальные туннели (OpenVPN/WireGuard/Amnezia)
IFACE_LOOPBACK = "loopback"     # 127.* — исключаем как раньше
_IFACE_PRIORITY = {
    IFACE_ZERO_TIER: 0,
    IFACE_TAILSCALE: 1,
    IFACE_PHYSICAL: 2,
    IFACE_VPN: 3,
    IFACE_LOOPBACK: 4,
}

_ZT_NAME_MARKERS = ("zerotier", "zttap", "zt", "one")
_TS_NAME_MARKERS = ("tailscale", "ts" )
_VPN_NAME_MARKERS = (
    "tun", "tap", "wg", "wireguard", "nord", "express",
    "openvpn", "amnezia", "proton", "mullvad", "wintun",
    "vpn", "tunnel", "tunnelbear", "surfshark",
)


class InterfaceInfo(NamedTuple):
    """Один IPv4-интерфейс с метаданными (для UI/приглашений)."""
    ip: str
    kind: str                        # одна из IFACE_* констант
    iface_name: str = ""             # дружественное имя (Windows) или пусто

    @property
    def priority(self) -> int:
        return _IFACE_PRIORITY.get(self.kind, 99)


def _classify(ip: str, iface_name: str = "") -> str:
    """Определяем тип интерфейса ТОЛЬКО по IP + имени.

    Классификация — мягкая, неточных попаданий не страшно; главное —
    не выкинуть адрес. 100.64.0.0/10 = Carrier-Grade NAT диапазон,
    который используют ZeroTier и Tailscale как свои подсети.
    """
    # 127.x.x.x — loopback (мы их отбрасываем вышестоящим кодом, но на всякий)
    if ip.startswith("127."):
        return IFACE_LOOPBACK
    # CGNAT-диапазон 100.64.0.0/10 — почти наверняка ZT/TS
    try:
        first = int(ip.split(".")[1])
        if ip.startswith("100.") and 64 <= first <= 127:
            # точнее разберёмся по имени интерфейса ниже
            pass
    except (ValueError, IndexError):
        pass
    name = (iface_name or "").lower()
    # ZeroTier (сначала по имени — точнее)
    for m in _ZT_NAME_MARKERS:
        if m in name:
            return IFACE_ZERO_TIER
    # Tailscale
    for m in _TS_NAME_MARKERS:
        if m in name:
            return IFACE_TAILSCALE
    # Общий VPN (туннели Amnezia/WireGuard/Nord/…)
    for m in _VPN_NAME_MARKERS:
        if m in name:
            return IFACE_VPN
    # Эвристика по IP: CGNAT (100.64/10) — если дошли досюда, то это или
    # ZT-адрес без подписи интерфейса, или реальный CGNAT провайдера;
    # в любом случае помечаем как overlay-подобный и ставим выше физики.
    try:
        octs = [int(o) for o in ip.split(".")]
        if octs[0] == 100 and 64 <= octs[1] <= 127:
            return IFACE_ZERO_TIER   # CGNAT = считаем ZT/TS, он выше приор.
        # Частные подсети 192.168/16 10/8 172.16/12 = почти наверняка физ.
        if (octs[0] == 192 and octs[1] == 168) or octs[0] == 10 or \
                (octs[0] == 172 and 16 <= octs[1] <= 31):
            return IFACE_PHYSICAL
    except (ValueError, IndexError):
        pass
    # Остальное: публичный IP = тоже физический (прямой выход в инет).
    return IFACE_PHYSICAL


# ═══════════════════════════════════════════════════════════════════════
# Windows: WinAPI SIO_GET_INTERFACE_LIST — самый надёжный способ получить
# ВСЕ интерфейсы с именами. Метод getaddrinfo(hostname) часто пропускает
# ZeroTier-интерфейс, если VPN переписал DNS или hostname резолвится в
# VPN-адрес. На Linux/Mac возвращаем пустой список — там работает fallback.
# ═══════════════════════════════════════════════════════════════════════
def _winapi_interfaces() -> list[InterfaceInfo]:
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        import struct
        from ctypes import wintypes
    except Exception:
        return []

    # WSABUF и SIO_GET_INTERFACE_LIST = _WSAIORW('I', 20)
    SIO_GET_INTERFACE_LIST = 0x74000034
    out_sz = 2048   # хватает ~60 интерфейсов с запасом

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    try:
        # WSAIoctl через ctypes; socket.ioctl на pywin подходит не всегда
        WSAIoctl = ctypes.windll.ws2_32.WSAIoctl
        outbuf = ctypes.create_string_buffer(out_sz)
        bytes_ret = wintypes.DWORD(0)
        rc = WSAIoctl(
            s.fileno(),
            SIO_GET_INTERFACE_LIST,
            None, 0,
            outbuf, out_sz,
            ctypes.byref(bytes_ret),
            None, None,
        )
        if rc != 0 or bytes_ret.value < 12:
            return []
    except Exception as e:
        log.debug("WSAIoctl(SIO_GET_INTERFACE_LIST) не доступен: %s", e)
        try:
            s.close()
        except OSError:
            pass
        return []
    try:
        s.close()
    except OSError:
        pass

    # INTERFACE_INFO запись: 4 байта флаги, затем 3x SOCKADDR_GEN (16 байт каждый)
    # итого 4 + 48 = 52 байта на интерфейс (мы берём только первый SOCKADDR — IP).
    entry_sz = 52
    n = bytes_ret.value // entry_sz
    out: list[InterfaceInfo] = []
    for i in range(n):
        off = i * entry_sz
        try:
            struct.unpack_from("<I", outbuf, off)[0]
            # Второй SOCKADDR = адрес интерфейса (смещение 4+0=4? → нет:
            # флаги 4б → iAddressOffset=4, iNetmask=4+16=20, iBroadcast=20+16=36).
            # SOCKADDR: 2б family (AF_INET=2), 2б порт, 4б IP, 8б резерв.
            family = struct.unpack_from("<H", outbuf, off + 4)[0]
            if family != socket.AF_INET:
                continue
            ip_bytes = struct.unpack_from("<4s", outbuf, off + 4 + 4)[0]
            ip = socket.inet_ntoa(ip_bytes)
            if not ip or ip.startswith("127."):
                continue
            # Попытаемся сопоставить IP с именем интерфейса через netsh позже
            out.append(InterfaceInfo(ip=ip, kind=_classify(ip), iface_name=""))
        except Exception:
            continue
    return out


def _netsh_names_map() -> dict[str, str]:
    """Windows-only: строит {ip -> friendly_name} через 'netsh int ip show addr'.
    Глушит ошибки: не найдено — просто вернём {}.
    """
    if sys.platform != "win32":
        return {}
    try:
        res = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "addresses"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return {}
    if res.returncode != 0:
        return {}
    out_map: dict[str, str] = {}
    cur_name = ""
    for line in (res.stdout or "").splitlines():
        l = line.strip()
        if l.startswith("Конфигурация интерфейса") or l.startswith("Configuration for interface"):
            # формат: "Configuration for interface \"ZeroTier One\""
            try:
                cur_name = l.split('"', 1)[1].rsplit('"', 1)[0]
            except IndexError:
                cur_name = ""
        elif cur_name and ("IP-адрес:" in l or "IP Address:" in l or "IP адрес:" in l):
            # формат: "   IP-адрес: 172.28.1.5, ..."
            for part in l.split(":", 1)[1:] if ":" in l else []:
                tok = part.split(",", 1)[0].strip()
                if tok.count(".") == 3 and all(p.isdigit() for p in tok.split(".")):
                    out_map.setdefault(tok, cur_name)
    return out_map


def _run_with_timeout(fn, timeout: float, default):
    """(v3.4.2 fix) Выполняет fn в отдельном потоке с ЖЁСТКИМ лимитом.

    Зачем: socket.getaddrinfo и subprocess не имеют надёжного таймаута
    внутри Python (DNS может висеть минуты при VPN/битом resolv), а
    get_interfaces() зовётся на СТАРТЕ сервера и в GUI-потоке. Один
    застрявший DNS-запрос раньше блокировал весь старт.

    Если fn не успела — возвращаем default и НЕ ЖДЁМ поток: shutdown(wait=
    False) обязателен, иначе выход из executor'а блокировался бы до конца
    зависшей getaddrinfo и таймаут терял бы смысл."""
    import concurrent.futures

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning("сетевая операция не уложилась в %.1f с — "
                        "пропускаю (VPN/DNS?)", timeout)
            return default
        except Exception as e:
            log.debug("сетевая операция упала: %s", e)
            return default
    except Exception:
        return default
    finally:
        # НЕ ждём зависший поток — он тихо умрёт сам, когда разблокируется
        ex.shutdown(wait=False)


def get_interfaces() -> list[InterfaceInfo]:
    """Все IPv4-интерфейсы машины, с классификацией и упорядоченные по приоритету.

    НИКОГДА не фильтрует VPN/ZT-интерфейсы — классифицирует только, чтобы
    сначала показать ZeroTier/Tailscale (они и нужны для приглашений),
    потом физические, потом VPN (понадобятся пользователю чтобы понять
    «а на какой адрес сейчас трафик уходит»).
    """
    found: dict[str, InterfaceInfo] = {}

    # 1) Самый надёжный источник на Windows — WinAPI
    win_ifs = _winapi_interfaces()

    # 2) + подтянем имена интерфейсов (для лейбла в UI)
    names_map: dict[str, str] = {}
    try:
        names_map = _netsh_names_map()
    except Exception:
        names_map = {}

    for info in win_ifs:
        name = names_map.get(info.ip, "") or info.iface_name
        kind = _classify(info.ip, name)
        found[info.ip] = InterfaceInfo(info.ip, kind, name)

    # 3) Fallback: стандартный getaddrinfo — часто ловит то, что пропустил WinAPI
    #    (v3.4.2 fix): getaddrinfo(hostname) НЕ ИМЕЕТ таймаута в Python — при
    #    VPN/битом DNS он висит десятки секунд и вечно, блокируя СЕРВЕР НА
    #    СТАРТЕ и GUI. Теперь — в фоне с жёстким лимитом 3 с.
    try:
        _host_ips = _run_with_timeout(
            lambda: [sa[0] for _f, _t, _p, _c, sa in
                     socket.getaddrinfo(socket.gethostname(), None,
                                        socket.AF_INET)],
            timeout=3.0, default=[])
        for ip in _host_ips:
            if ip.startswith("127."):
                continue
            if ip not in found:
                found[ip] = InterfaceInfo(ip, _classify(ip, ""), "")
    except OSError:
        pass

    # 4) Fallback: UDP-connect трюк на «интернет по умолчанию» — только
    #    НЕ ВСТАВЛЯЕМ на нулевую позицию (сейчас это VPN и это ЗАМЕНЯЕТ ZT!)
    #    Ставим его в самый конец с пометкой, что это default route.
    #    (v3.4.2 fix): connect(8.8.8.8:80) был БЕЗ таймаута — при VPN,
    #    режущем исходящий UDP, висел до таймаута ОС (~20 с и более).
    #    Теперь settimeout(1.5) — худший случай полторы секунды.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1.5)
            # 8.8.8.8:80 — достаточно чтобы ОС вычислила исходящий iface.
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
        finally:
            s.close()
        if not primary_ip.startswith("127.") and primary_ip not in found:
            kind = _classify(primary_ip, "")
            # если это VPN — помечаем явно (пользователю важно видеть)
            found[primary_ip] = InterfaceInfo(primary_ip, kind, "(маршрут по умолчанию)")
    except OSError:
        pass

    # 5) socket.if_nameindex — Linux/Mac (на Windows работает частично)
    #    (v3.4.2 fix): getaddrinfo по КАЖДОМУ имени интерфейса — тоже без
    #    таймаута. Общий лимит на блок 3 с.
    try:
        if hasattr(socket, "if_nameindex"):
            idx_names = socket.if_nameindex()

            def _resolve_names():
                out = []
                for _idx, name in idx_names:
                    try:
                        addr_info = socket.getaddrinfo(name, None, socket.AF_INET)
                        for _f, _t, _p, _c, sockaddr in addr_info:
                            out.append((sockaddr[0], name))
                    except (OSError, ValueError):
                        continue
                return out

            resolved = _run_with_timeout(_resolve_names, timeout=3.0,
                                         default=[])
            for ip, name in resolved:
                if ip.startswith("127.") or ip in found:
                    continue
                found[ip] = InterfaceInfo(ip, _classify(ip, name), name)
    except (OSError, AttributeError):
        pass

    # Сортируем: ZT → TS → физические → VPN → loopback
    items = list(found.values())
    # И вторым ключом: стабильно, чтобы при перезапусках UI не прыгало.
    items.sort(key=lambda x: (x.priority, x.ip))
    return items


def get_local_ips() -> list[str]:
    """Совместимость: возвращает просто list[str] адресов (в новом порядке).

    Старая сигнатура — чтобы все клиенты кода (server_gui, invite-карточки,
    smoke-тесты) не сломались. Полный объект с типом/именем — get_interfaces().
    """
    return [i.ip for i in get_interfaces() if i.kind != IFACE_LOOPBACK]


def detect_vpn_zt_conflict(ifs: list[InterfaceInfo] | None = None) -> tuple[bool, str]:
    """Есть ли сейчас конфликт VPN ↔ ZeroTier? Возвращает (есть?, подсказка).

    Правило: если в системе ЕСТЬ ZeroTier/Tailscale И ЕСТЬ другой VPN-туннель
    одновременно — на 90% у пользователя сломается вход по ZT-адресу (VPN
    перехватывает маршрут 0.0.0.0/1). В таком случае предлагаем скрипт.

    (v3.4.2 fix) ifs — уже полученный список интерфейсов: раньше каждый
    вызов дёргал get_interfaces() заново (netsh-subprocess + getaddrinfo),
    а при старте сервера эта пара вызывалась подряд ДВАЖДЫ.
    """
    if ifs is None:
        ifs = get_interfaces()
    has_zt = any(i.kind in (IFACE_ZERO_TIER, IFACE_TAILSCALE) for i in ifs)
    has_vpn = any(i.kind == IFACE_VPN for i in ifs)
    if not has_zt or not has_vpn:
        return False, ""
    tip = (
        "Обнаружены ZeroTier/Tailscale + VPN одновременно. Обычно VPN "
        "перехватывает трафик — открой «Починить VPN+ZT» в окне сервера: "
        "там покажут, что именно мешает подключению, и исправят (или "
        "запусти scripts\\fix_vpn_zt.py diagnose)."
    )
    return True, tip


# ── v3.5.10: честный фикс VPN+ZeroTier через scripts/fix_vpn_zt.py ────
# Старый путь — fix_zerotier_vpn.bat с кириллицей — печатал кракозябры
# (cmd.exe читает .bat в CP866), падал на %ProgramFiles(x86)% внутри
# if ( ) и делал route add с ИМЕНЕМ сети вместо подсети. Теперь fixer —
# Python: вывод UTF-8, JSON от zerotier-cli -j, структурированный отчёт.


def _console_python() -> str | None:
    """Консольный python.exe: если GUI запущен под pythonw.exe — меняем на
    python.exe (иначе UAC-окно фикса откроется БЕЗ консоли и пользователь
    ничего не увидит)."""
    py = Path(sys.executable)
    if py.name.lower().startswith("pythonw"):
        cand = py.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(py)


def build_fix_command(
    project_root: Path,
    fw_ports: str = "8420-8424",
    report_path: Path | None = None,
) -> tuple[str, str]:
    """(программа, аргументы) для elevation-запуска fix_vpn_zt.py fix.

    Чистая функция — тестируется без реального запуска. Для frozen-сборки
    (PyInstaller, где sys.executable — не python) возвращаем .bat-обёртку.
    """
    script = project_root / "scripts" / "fix_vpn_zt.py"
    report = report_path or (project_root / "relay_data" /
                             "vpn_fixer_report.json")
    args = (f'"{script}" fix --fw-ports {fw_ports} '
            f'--report "{report}"')
    if getattr(sys, "frozen", False) or not script.exists():
        bat = project_root / "scripts" / "fix_zerotier_vpn.bat"
        return str(bat), f'fix --fw-ports {fw_ports} --report "{report}"'
    return _console_python(), args


def launch_vpn_zt_fixer(
    project_root: Path,
    fw_ports: str = "8420-8424",
    report_path: Path | None = None,
) -> bool:
    """Запускает scripts/fix_vpn_zt.py fix с правами админа (Windows):
    UAC-запрос, открывается КОНСОЛЬ с читаемым русским текстом; скрипт
    сам пишет отчёт в relay_data/vpn_fixer_report.json. На Linux/Mac
    открывает папку scripts. Возвращает True — запустили / False — ошибка.
    """
    try:
        if sys.platform == "win32":
            prog, args = build_fix_command(project_root, fw_ports,
                                           report_path)
            import ctypes
            SW_SHOWNORMAL = 1
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", prog, args, str(project_root), SW_SHOWNORMAL,
            )
            # ShellExecuteW возвращает >32 при успехе, <=32 — код ошибки
            # (SE_ERR_ACCESSDENIED=5 — пользователь отклонил UAC).
            return rc > 32
        # Linux/Mac — честно говорим, что сделать
        script = project_root / "scripts" / "fix_vpn_zt.py"
        log.info("fix VPN+ZT: запусти вручную `python %s diagnose`",
                 script)
        subprocess.Popen(["xdg-open" if sys.platform == "linux" else "open",
                          str(script.parent)])
        return True
    except Exception:
        log.exception("Не удалось запустить fix-скрипт")
        return False


def run_vpn_diagnose(
    project_root: Path,
    port: int = 8420,
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Запускает `fix_vpn_zt.py diagnose` и возвращает (код, текст-отчёт).

    Никаких прав админа не нужно; CREATE_NO_WINDOW — чтобы под pythonw
    не мигало чёрное окно при каждой проверке. Код: 0 — проблем нет,
    1 — есть проблемы, 2 — не удалось собрать данные.
    """
    script = project_root / "scripts" / "fix_vpn_zt.py"
    if not script.exists():
        return 2, f"не найден {script}"
    cmd = [
        sys.executable, str(script), "diagnose",
        "--port", str(port), "--no-file", "--no-pause",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(project_root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 2, "диагностика не уложилась в таймаут (сеть/VPN тормозят?)"
    except OSError as e:
        return 2, f"{type(e).__name__}: {e}"

