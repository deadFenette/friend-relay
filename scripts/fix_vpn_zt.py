#!/usr/bin/env python3
"""fix_vpn_zt.py — диагностика и починка ZeroTier + VPN для Friend Relay (v3.5.11).

НОВОЕ В v3.5.11 (по отзыву с поля: «почини артефакты» + «раньше работало
через приоритет маршрутов»):
  • smart_decode + UTF-8-preamble для PowerShell — кракозябры в названиях
    адаптеров («Подключение по локальной сети» → «������祭��») вылечены
    КОНСТРУКТИВНО: консольные утилиты Windows пишут в OEM/CP866, PowerShell
    без подготовки — тоже; теперь каждый байт декодируется честно;
  • новая проверка «Библиотека адресов»: скрипт собирает ВСЕ IP, известные
    программе (settings.json → host_url, журналы relay_data, ZeroTier,
    адаптеры), сортирует по вероятности и пингует TCP-порт сервера —
    человек видит, какой адрес РЕАЛЬНЫЙ и «вероятнее всего подойдёт»;
  • автофиксов стало 7 групп вместо 4: приоритет маршрутов (метрика ZT = 1,
    VPN = 100 — как работало раньше), профиль ZT-сети → «Частная»,
    персистентные маршруты (-p), правила ICMP и программы ZeroTier,
    MTU ZT-адаптера 2800 → 1400 при полном туннеле, старт/рестарт сервиса
    ZeroTier, если он остановлен/OFFLINE.

Заменил сломанный fix_zerotier_vpn.bat (эпохи v3.3.0), который:
  • печатал кракозябры — cmd.exe читал UTF-8-файл в кодировке CP866;
  • падал на %ProgramFiles(x86)% внутри блока if ( ) — скобка (x86)
    закрывала блок парсера, команды после неё не выполнялись;
  • в `zerotier-cli listnetworks | for /f "tokens=4"` доставал ИМЯ сети,
    а не IP → делал `route add <имя сети>` — ноль реального фикса.

Здесь всё по-честному, на Python (чистый stdlib):
  • `zerotier-cli -j listnetworks / listpeers` — настоящий JSON, никакого
    парсинга колонок «на глазок»;
  • математика подсетей через модуль ipaddress;
  • вывод UTF-8 (reconfigure stdout) — кракозябр не бывает ни в консоли,
    ни в pipe GUI;
  • структурированный отчёт (список проверок со статусами) — GUI показывает
    его в окне «Починить VPN+ZT», а человек наконец видит, ЧТО мешает
    подключению, а не «синюю абракадабру».

Команды:
  diagnose   только диагностика (права админа НЕ нужны)
  fix        применить исправления (нужны права админа: UAC-запрос из GUI)
  report     показать последний сохранённый JSON-отчёт

Ключи:
  --port N         порт сервера Friend Relay для проверок (по умолч. 8420)
  --fw-ports A-B   диапазон портов для правил брендмауэра (по умолч. 8420-8424)
  --report PATH    куда писать JSON-отчёт (по умолч. relay_data/vpn_fixer_report.json)
  --json           печатать ТОЛЬКО JSON-отчёт (для GUI/тестов)
  --no-file        не писать файл отчёта
  --no-pause       не ждать Enter в конце (тесты/автозапуск)

Коды выхода diagnose: 0 — всё хорошо; 1 — найдены проблемы; 2 — не удалось
собрать данные. fix: 0 — успех; 2 — часть шагов упала; 3 — нет прав админа;
4 — фатально (например, не Windows). Подробности — в docs/VPN_ZEROTIER_FIX.md.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# чтобы reconfigure не падал в экзотических окружениях — оборачиваем
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# Статусы проверок/шагов (совпадают с ожиданиями GUI и тестов)
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_BAD = "bad"
STATUS_INFO = "info"
STATUS_SKIP = "skip"

_STATUS_ICON = {
    STATUS_OK: "[ OK ]",
    STATUS_WARN: "[ !! ]",
    STATUS_BAD: "[FAIL]",
    STATUS_INFO: "[ -- ]",
    STATUS_SKIP: "[skip]",
}

# ═══════════════════════════════════════════════════════════════════════
# Утилиты запуска команд — никогда не бросают исключений
# ═══════════════════════════════════════════════════════════════════════


def _run_bytes(cmd: list[str], timeout: float = 8.0) -> tuple[bool, bytes]:
    """Запускает команду, возвращает (успех, сырые байты stdout+stderr).

    Байты — принципиально: консольные утилиты Windows (route, netsh,
    zerotier-cli) пишут в OEM-кодировке (CP866 на русской Windows), и
    заранее выбранная кодировка декода неизбежно даёт кракозябры либо
    потерю данных. Декодирует smart_decode()."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or b"") + (r.stderr or b"")
    except FileNotFoundError:
        return False, f"не найдено: {cmd[0]}".encode()
    except subprocess.TimeoutExpired:
        return False, f"таймаут {timeout:.0f} с: {cmd[0]}".encode()
    except OSError as e:
        return False, f"{type(e).__name__}: {e}".encode()


def _cyrillic_score(text: str) -> int:
    """Насколько декод похож на нормальный русский/латинский текст.

    Кириллица и ASCII — плюс; символьный мусор (псевдографика CP866
    «╧╕■», номера №, иероглифы) — минус. Именно так различаем CP866 и
    CP1251: байты одного, декодированные другим, дают полно рамок и
    символов вместо букв."""
    score = 0
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:      # кириллица
            score += 3
        elif o < 0x80:                 # ASCII
            score += 1
        else:
            score -= 4                 # рамки, псевдографика, мусор
    return score


def smart_decode(raw: bytes, prefer: str | None = None) -> str:
    """Байты дочернего процесса → текст БЕЗ кракозябр.

    Алгоритм:
      1. валидный UTF-8 — почти наверняка он и есть (кириллица CP866/
         CP1251 валидным UTF-8 не бывает; PowerShell с нашим
         UTF-8-preamble попадает сюда); возвращаем сразу, если prefer
         не задан;
      2. иначе декодируем каждый кандидат (prefer, cp866 — OEM русской
         консоли, cp1251 — ANSI) и выбираем по _cyrillic_score — меньше
         всего псевдографики, больше букв, значит кодировка верная;
      3. совсем беда — UTF-8 с заменой битых байтов, лишь бы не падать.
    """
    candidates = ["utf-8"]
    if prefer:
        candidates.append(prefer)
    candidates += ["cp866", "cp1251"]
    best_text: str | None = None
    best_score = -(1 << 30)
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc == "utf-8" and not prefer:
            return text
        score = _cyrillic_score(text)
        if score > best_score:
            best_text, best_score = text, score
    if best_text is not None:
        return best_text
    return raw.decode("utf-8", errors="replace")


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    """Запускает команду, возвращает (успех, вывод stdout+stderr) — текст."""
    ok, raw = _run_bytes(cmd, timeout=timeout)
    return ok, smart_decode(raw)


# PowerShell пишет в кодировке консоли (OEM CP866 на русской Windows), пока
# ей не сказано иначе. Preamble переключает И stdout-кодировку конвейера —
# ConvertTo-Json с русскими названиями адаптеров приходит честным UTF-8.
_PS_UTF8 = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$OutputEncoding=[System.Text.Encoding]::UTF8;")


def ps_cmd(script: str) -> list[str]:
    """powershell -NoProfile -Command <UTF8-preamble> <script>."""
    return ["powershell", "-NoProfile", "-Command", _PS_UTF8 + script]


def find_ztcli() -> str | None:
    """Путь к zerotier-cli: PATH → Program Files. Без скобочных бат-багов."""
    exe = shutil.which("zerotier-cli") or shutil.which("zerotier-cli.exe")
    if exe:
        return exe
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if not base:
            continue
        cand = Path(base) / "ZeroTier" / "One" / "zerotier-cli.exe"
        if cand.exists():
            return str(cand)
    return None


def is_admin() -> bool:
    """Есть ли права администратора (для режима fix)."""
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Чистые функции разбора (покрываются тестами без всяких систем)
# ═══════════════════════════════════════════════════════════════════════


def net_mask_of(prefix: str) -> tuple[str, str] | None:
    """«172.28.1.5/16» → («172.28.0.0», «255.255.0.0»). None — мусор."""
    try:
        net = ipaddress.ip_network(prefix, strict=False)
        if net.version != 4:
            return None
        return str(net.network_address), str(net.netmask)
    except (ValueError, TypeError):
        return None


def extract_zt_networks(networks_json) -> list[dict]:
    """JSON `zerotier-cli -j listnetworks` → компактный список сетей."""
    out: list[dict] = []
    if not isinstance(networks_json, list):
        return out
    for nw in networks_json:
        if not isinstance(nw, dict):
            continue
        routes = []
        for rt in nw.get("routes") or []:
            if isinstance(rt, dict) and rt.get("target"):
                routes.append(str(rt["target"]))
        out.append({
            "nwid": str(nw.get("id") or nw.get("nwid") or "?"),
            "name": str(nw.get("name") or ""),
            "status": str(nw.get("status") or "?"),
            "assigned": [str(a) for a in nw.get("assignedIps") or []],
            "routes": routes,
        })
    return out


def extract_planet_ips(peers_json) -> list[str]:
    """JSON `zerotier-cli -j listpeers` → IPv4 корневых серверов (PLANET).

    Именно их трафик VPN «0.0.0.0/1+128.0.0.0/1» утащивает в туннель, из-за
    чего ZeroTier OFFLINE. Явные /32-маршруты через реальный шлюз чинят
    транспорт без сплит-туннелирования в Amnezia."""
    ips: list[str] = []
    seen: set[str] = set()
    if not isinstance(peers_json, list):
        return ips
    for peer in peers_json:
        if not isinstance(peer, dict):
            continue
        if str(peer.get("role", "")).upper() not in ("PLANET", "MOON"):
            continue
        for path in peer.get("paths") or []:
            if not isinstance(path, dict):
                continue
            addr = str(path.get("address") or "")
            ip = addr.rsplit("/", 1)[0]
            if not ip or ip in seen:
                continue
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if a.version == 4:
                seen.add(ip)
                ips.append(ip)
    return ips


_ROUTE_RE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+"      # сеть назначения
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+"          # маска
    r"(\S+)\s+"                              # шлюз (может быть On-link)
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+"          # интерфейс (IP)
    r"(\d+)\s*$"                             # метрика
)


def parse_route_print(text: str) -> list[dict]:
    """`route print -4` → список активных IPv4-маршрутов.

    Возвращает записи {net, mask, gw, iface, metric}; мусорные строки
    (заголовки, разделители, IPv6-секция) молча пропускаются."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        m = _ROUTE_RE.match(line)
        if not m:
            continue
        out.append({
            "net": m.group(1), "mask": m.group(2), "gw": m.group(3),
            "iface": m.group(4), "metric": int(m.group(5)),
        })
    return out


def vpn_grabbed(routes: list[dict]) -> bool:
    """Подняты ли VPN-маршруты «половина интернета» 0.0.0.0/1 + 128.0.0.0/1."""
    return any(r["mask"] == "128.0.0.0" and r["net"] in ("0.0.0.0", "128.0.0.0")
               for r in routes)


def route_for_destination(routes: list[dict], dest: str) -> dict | None:
    """Какой маршрут реально понесёт пакеты к dest (самый длинный префикс).

    Это то, как Windows выбирает маршрут: самое специфичное покрытие, потом
    метрика. Возвращает запись или None, если ничего не покрывает."""
    try:
        d = ipaddress.ip_address(dest)
    except ValueError:
        return None
    best: dict | None = None
    best_prefix = -1
    best_metric = 1 << 30
    for r in routes:
        try:
            net = ipaddress.ip_network(f'{r["net"]}/{r["mask"]}', strict=False)
        except ValueError:
            continue
        if d.version != net.version or d not in net:
            continue
        prefix = net.prefixlen
        metric = r["metric"]
        if prefix > best_prefix or (prefix == best_prefix and metric < best_metric):
            best, best_prefix, best_metric = r, prefix, metric
    return best


def build_subnet_route_cmd(net: str, mask: str, if_idx: int | None,
                           gw: str | None,
                           persistent: bool = False) -> list[str]:
    """Команда `route add` подсети ZeroTier через ZT-интерфейс, metric 1.

    if_idx — индекс ZT-адаптера (маршрут on-link через 0.0.0.0);
    если индекс не найден — fallback на собственный ZT-IP как шлюз
    (так делал и старый .bat, только у него IP был всегда мусорным).
    persistent=True добавляет ключ -p: маршрут переживает перезагрузку
    (в v3.5.10 всё умирало при каждом ребуте и чинить надо было заново)."""
    cmd = ["route"]
    if persistent:
        cmd.append("-p")
    cmd.append("add")
    if if_idx is not None:
        cmd += [net, "mask", mask, "0.0.0.0", "metric", "1",
                "if", str(if_idx)]
    else:
        cmd += [net, "mask", mask, gw or "0.0.0.0", "metric", "1"]
    return cmd


def build_planet_route_cmd(ip: str, real_gw: str) -> list[str]:
    """/32-маршрут корневого сервера ZeroTier через РЕАЛЬНЫЙ шлюз."""
    return ["route", "add", ip, "mask", "255.255.255.255", real_gw,
            "metric", "1"]


# ═══════════════════════════════════════════════════════════════════════
# Команды расширенных автофиксов (v3.5.11) — чистые, покрываются тестами
# ═══════════════════════════════════════════════════════════════════════


def build_iface_metric_cmd(name: str, metric: int) -> list[str]:
    """Метрика ИНТЕРФЕЙСА через netsh (приоритет маршрутов Windows).

    Эффективная метрика маршрута = метрика маршрута + метрика интерфейса,
    поэтому чем НИЖЕ метрика адаптера, тем выше его приоритет. Именно так
    «раньше работало»: у ZeroTier метрика должна быть минимальной."""
    return ["netsh", "interface", "ipv4", "set", "interface", name,
            f"metric={metric}"]


def build_ps_iface_metric_cmd(idx: int, metric: int) -> list[str]:
    """То же через PowerShell — fallback, если netsh не справился."""
    return ps_cmd(f"Set-NetIPInterface -InterfaceIndex {idx} "
                  f"-AddressFamily IPv4 -InterfaceMetric {metric}")


def build_ps_profile_private_cmd(idx: int) -> list[str]:
    """Профиль сети ZT-адаптера → «Частная».

    Windows считает ZeroTier-сеть ПУБЛИЧНОЙ и режет входящие — даже когда
    правила брендмауэра формально есть."""
    return ps_cmd(f"Set-NetConnectionProfile -InterfaceIndex {idx} "
                  "-NetworkCategory Private")


def build_ps_mtu_cmd(name: str, mtu: int) -> list[str]:
    """MTU ZT-подынтерфейса (store=active — до перезагрузки, мягко).

    2800 у ZeroTier хорошо в чистой сети, но при полном туннеле VPN
    крупные пакеты дробятся внешним MTU и часть фрагментов теряется:
    большие файлы виснут, голос дёргается. 1400 проходит почти везде."""
    return ["netsh", "interface", "ipv4", "set", "subinterface", name,
            f"mtu={mtu}", "store=active"]


def build_icmp_rule_cmd() -> list[str]:
    """Правило брендмауэра для ICMP (ping): без него «а пингуется ли хост?»
    у друзей всегда врёт, даже когда всё работает."""
    return ["netsh", "advfirewall", "firewall", "add", "rule",
            "name=Friend Relay ICMP", "dir=in", "action=allow",
            "protocol=icmpv4", "profile=any"]


def build_zt_program_rule_cmd(exe_path: str) -> list[str]:
    """Разрешить сам исполняемый файл ZeroTier — чтобы брендмауэр не душил
    транспорт ZT (UDP 9993) в профиле «Публичная сеть»."""
    return ["netsh", "advfirewall", "firewall", "add", "rule",
            "name=Friend Relay ZeroTier Core", "dir=in", "action=allow",
            f"program={exe_path}", "enable=yes", "profile=any"]


ZT_SERVICE = "ZeroTierOneService"


def build_service_start_cmd() -> list[str]:
    return ["net", "start", ZT_SERVICE]


def build_service_restart_cmd() -> list[str]:
    return ps_cmd(f"Restart-Service -Name {ZT_SERVICE} -Force")


# ═══════════════════════════════════════════════════════════════════════
# «Библиотека адресов» (v3.5.11): где лежат РЕАЛЬНЫЕ IP
#
# Программа сама знает адреса, которые УЖЕ работали: host_url в
# settings.json (куда ходит клиент), журналы relay_data (откуда заходили
# друзья), ZeroTier-адреса сетей, адреса адаптеров. Склеиваем все
# источники, сортируем по вероятности и проверяем TCP-порт сервера —
# человек видит «вероятнее всего подойдёт ЭТОТ», а не гадает по списку.
# ═══════════════════════════════════════════════════════════════════════

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

# Классы кандидатов по убыванию вероятности «этот адрес подойдёт»:
# zt — адрес в подсети ZeroTier; self — свой LAN-адрес (хост сам себя);
# lan — другая частная сеть; public — публичный IP; vpn — адрес VPN-
# туннеля (типичная ловушка: 10.8.x.x Amnezia — мимо ZeroTier); junk.
_KIND_ORDER = {"zt": 0, "self": 1, "lan": 2, "public": 3,
               "vpn": 4, "junk": 5}
_KIND_LABEL = {
    "zt": "ZeroTier",
    "self": "свой адрес (LAN)",
    "lan": "локальная сеть",
    "public": "публичный IP",
    "vpn": "адрес VPN-туннеля — НЕ для подключения",
    "junk": "мусор",
}


def harvest_ips_from_text(text: str) -> list[str]:
    """Все IPv4 из текста без служебных (0.0.0.0, маски 255.x, loopback,
    multicast, link-local 169.254.x). Порядок источника сохраняется."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _IP_RE.finditer(text or ""):
        ip = m.group(1)
        if ip in seen:
            continue
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (a.is_loopback or a.is_multicast or a.is_link_local
                or ip == "0.0.0.0" or ip.startswith("255.")):
            continue
        seen.add(ip)
        out.append(ip)
    return out


def classify_candidate_ip(ip: str, zt_subnets: list,
                          vpn_ips: tuple | set = (),
                          own_ips: tuple | set = ()) -> str:
    """zt / self / lan / public / vpn / junk — к какой категории относится."""
    try:
        a = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return "junk"
    if a.version != 4:
        return "junk"
    for net in zt_subnets:
        if a in net:
            return "zt"
    if ip in vpn_ips:
        return "vpn"
    if ip in own_ips:
        return "self"
    if a.is_loopback or a.is_multicast or a.is_link_local or a.is_reserved:
        return "junk"
    if a.is_private:
        return "lan"
    return "public"


def address_label(cand: dict, port: int) -> str:
    """«10.231.146.4 — ZeroTier, отвечает — вероятнее всего подойдёт»."""
    kind = cand.get("kind", "junk")
    label = _KIND_LABEL.get(kind, kind)
    if kind in ("vpn", "junk"):
        alive_txt = "не проверяем"
    else:
        alive_txt = "отвечает" if cand.get("alive") else "не отвечает"
    best = " — вероятнее всего подойдёт" if cand.get("best") else ""
    src = cand.get("source", "")
    src_txt = f" (из: {src})" if src else ""
    return (f"{cand['ip']} — {label}, {alive_txt} на порту {port}"
            f"{best}{src_txt}")


def rank_addresses(cands: list[dict], port: int,
                   max_probe: int = 8) -> list[dict]:
    """Сортирует кандидатов по вероятности и проверяет TCP-порт сервера.

    VPN/мусор не проверяем вовсе (там сервера не бывает). Проверяем не
    больше max_probe адресов — диагностика не должна висеть минуту на
    мёртвых публичных IP из старых логов. best=True получает самый
    вероятный ОТВЕЧАЮЩИЙ (или первый ZT, если ничто не отвечает)."""
    for c in cands:
        c.setdefault("alive", False)
    probed = 0
    ordered = sorted(cands,
                     key=lambda c: _KIND_ORDER.get(c.get("kind", "junk"), 9))
    for c in ordered:
        if c.get("kind") in ("vpn", "junk"):
            continue
        if probed >= max_probe:
            break
        probed += 1
        c["alive"] = probe_tcp(c["ip"], port, timeout=0.9)
    cands.sort(key=lambda c: (
        0 if c.get("alive") else 1,
        _KIND_ORDER.get(c.get("kind", "junk"), 9),
    ))
    best = next((c for c in cands if c.get("alive")), None)
    if best is None:
        best = next((c for c in cands if c.get("kind") == "zt"), None)
    if best is not None:
        best["best"] = True
    return cands


def address_verdict(ranked: list[dict], port: int) -> str:
    """Итоговая строка для человека: какой адрес РЕАЛЬНЫЙ."""
    if not ranked:
        return "в библиотеке нет ни одного адреса"
    best = next((c for c in ranked if c.get("alive")), None)
    if best is not None:
        return (f"вероятнее всего подойдёт: {best['ip']} "
                f"({_KIND_LABEL.get(best.get('kind', ''), '?')}) — "
                f"сервер отвечает на порту {port}")
    zt = next((c for c in ranked if c.get("kind") == "zt"), None)
    if zt is not None:
        return (f"сервер сейчас не отвечает ни по одному адресу; для "
                f"ZeroTier-сети правильный адрес — {zt['ip']}:{port} "
                "(запусти сервер и проверь снова)")
    return ("ни один известный адрес не отвечает на порту "
            f"{port} — сервер не запущен или адреса устарели")


# ═══════════════════════════════════════════════════════════════════════
# Windows-специфика: адаптеры, маршруты по умолчанию, брендмауэр
# ═══════════════════════════════════════════════════════════════════════

_ZT_MARKERS = ("zerotier", "zttap")
_TS_MARKERS = ("tailscale",)
_VPN_MARKERS = ("tun", "tap", "wg", "wireguard", "openvpn", "amnezia",
                "wintun", "vpn", "nord", "proton", "mullvad", "express")


def classify_adapter(name: str, description: str) -> str:
    """zerotier / tailscale / vpn / physical — по имени и описанию."""
    text = f"{name} {description}".lower()
    for m in _ZT_MARKERS:
        if m in text:
            return "zerotier"
    for m in _TS_MARKERS:
        if m in text:
            return "tailscale"
    for m in _VPN_MARKERS:
        if m in text:
            return "vpn"
    return "physical"


def get_adapters() -> list[dict] | None:
    """Список адаптеров через PowerShell (ключи JSON локале-независимы,
    текст — честный UTF-8 благодаря ps_cmd: названия вида «Подключение
    по локальной сети» приходят без кракозябр).

    None = PowerShell недоступен (не Windows / песочница) — вызывающий код
    честно пометит проверку как skip вместо выдумывания данных."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        "Get-NetAdapter | Select-Object Name,InterfaceDescription,ifIndex,"
        "Status | ConvertTo-Json",
    ), timeout=12)
    if not ok:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    out_list: list[dict] = []
    for a in data if isinstance(data, list) else []:
        try:
            out_list.append({
                "name": str(a.get("Name") or ""),
                "desc": str(a.get("InterfaceDescription") or ""),
                "idx": int(a.get("ifIndex") or 0),
                "status": str(a.get("Status") or ""),
                "kind": classify_adapter(str(a.get("Name") or ""),
                                         str(a.get("InterfaceDescription") or "")),
            })
        except (TypeError, ValueError):
            continue
    return out_list


def get_default_routes() -> list[dict] | None:
    """Маршруты 0.0.0.0/0 через PowerShell (ifIndex, NextHop, RouteMetric)."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction "
        "SilentlyContinue | Select-Object ifIndex,NextHop,RouteMetric | "
        "ConvertTo-Json",
    ), timeout=12)
    if not ok:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    out_list: list[dict] = []
    for r in data if isinstance(data, list) else []:
        try:
            out_list.append({
                "ifidx": int(r.get("ifIndex") or 0),
                "nexthop": str(r.get("NextHop") or ""),
                "metric": int(r.get("RouteMetric") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out_list


def real_gateway(adapters: list[dict] | None,
                 defaults: list[dict] | None) -> str | None:
    """Шлюз РЕАЛЬНОЙ сети: default-маршрут через НЕ-VPN/НЕ-ZT адаптер.

    VPN «хватает всё» через /1-маршруты, поэтому 0.0.0.0/0 обычно остаётся
    указывать на физический роутер — им и пользуемся для /32-маршрутов
    корней ZeroTier."""
    if not adapters or not defaults:
        return None
    kind_by_idx = {a["idx"]: a["kind"] for a in adapters if a.get("idx")}
    best: str | None = None
    best_metric = 1 << 30
    for r in defaults:
        gw = r["nexthop"]
        if not gw or gw in ("0.0.0.0", "::"):
            continue
        if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", gw):
            continue
        if kind_by_idx.get(r["ifidx"], "vpn") != "physical":
            continue
        if r["metric"] < best_metric:
            best, best_metric = gw, r["metric"]
    return best


def zt_iface_index(adapters: list[dict] | None) -> int | None:
    if not adapters:
        return None
    for a in adapters:
        if a["kind"] == "zerotier" and a.get("idx"):
            return a["idx"]
    return None


def zt_iface_name(adapters: list[dict] | None) -> str | None:
    if not adapters:
        return None
    for a in adapters:
        if a["kind"] == "zerotier" and a.get("name"):
            return a["name"]
    return None


def firewall_rules_present(scope_prefix: str = "Friend Relay") -> bool | None:
    """Есть ли правила брендмауэра «Friend Relay …». None — не удалось узнать."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        "(Get-NetFirewallRule -DisplayName '" + scope_prefix + "*' "
        "-ErrorAction SilentlyContinue | Measure-Object).Count",
    ), timeout=15)
    if not ok:
        return None
    try:
        return int(out.strip()) > 0
    except ValueError:
        return None


def get_iface_metrics() -> dict[int, int] | None:
    """{ifIndex: InterfaceMetric} всех IPv4-интерфейсов (приоритет маршрутов).

    Windows сравнивает маршруты с одинаковым префиксом по СУММЕ
    (метрика маршрута + метрика интерфейса) — именно поэтому «приоритет
    маршрутов» из старых времён задаётся метрикой адаптера."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        "Get-NetIPInterface -AddressFamily IPv4 -ErrorAction "
        "SilentlyContinue | Select-Object ifIndex,InterfaceMetric | "
        "ConvertTo-Json",
    ), timeout=12)
    if not ok:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    out_map: dict[int, int] = {}
    for r in data if isinstance(data, list) else []:
        try:
            idx = int(r.get("ifIndex") or 0)
            met = int(r.get("InterfaceMetric") or 0)
            if idx:
                out_map[idx] = met
        except (TypeError, ValueError):
            continue
    return out_map or None


def get_iface_mtu(idx: int) -> int | None:
    """MTU адаптера по ifIndex (NlMtu) — для проверки 2800 у ZeroTier."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        f"(Get-NetIPInterface -InterfaceIndex {idx} -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue).NlMtu",
    ), timeout=10)
    if not ok:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def get_network_category(idx: int) -> str | None:
    """Категория сети адаптера (Private/Public/DomainAuthenticated)."""
    if shutil.which("powershell") is None:
        return None
    ok, out = _run(ps_cmd(
        f"(Get-NetConnectionProfile -InterfaceIndex {idx} "
        "-ErrorAction SilentlyContinue).NetworkCategory",
    ), timeout=10)
    return out.strip() if ok and out.strip() else None


def service_status() -> str | None:
    """Статус службы ZeroTierOneService (Running/Stopped/…). None — нет/ошибка."""
    if shutil.which("powershell") is None and os.name != "nt":
        return None
    ok, out = _run(ps_cmd(
        f"(Get-Service -Name {ZT_SERVICE} -ErrorAction SilentlyContinue)"
        ".Status",
    ), timeout=10)
    return out.strip() if ok and out.strip() else None


def zt_exe_path(ztcli: str | None) -> str | None:
    """Путь к zerotier-one.exe (служба) — для правила брендмауэра программы.

    zerotier-cli и zerotier-one лежат рядом, но на всякий случай проверяем
    и Program Files напрямую."""
    cands: list[Path] = []
    if ztcli:
        cands.append(Path(ztcli).with_name("zerotier-one.exe"))
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "ZeroTier" / "One" / "zerotier-one.exe")
    for c in cands:
        if c.exists():
            return str(c)
    return None


def windows_routes() -> list[dict] | None:
    """Активные IPv4-маршруты (Windows: route print -4). None — не удалось."""
    if os.name != "nt":
        return None
    ok_r, out_r = _run(["route", "print", "-4"], timeout=8)
    if not ok_r:
        return None
    routes = parse_route_print(out_r)
    return routes or []


def probe_tcp(ip: str, port: int, timeout: float = 1.2) -> bool:
    """Открывается ли TCP-порт (жив ли сервер на этом адресе)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Режим diagnose
# ═══════════════════════════════════════════════════════════════════════


class Checker:
    """Собирает список проверок {id,title,status,detail} и вердикт."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, cid: str, title: str, status: str, detail: str = "") -> None:
        self.checks.append({"id": cid, "title": title, "status": status,
                            "detail": detail})

    @property
    def has_bad(self) -> bool:
        return any(c["status"] == STATUS_BAD for c in self.checks)

    @property
    def has_warn(self) -> bool:
        return any(c["status"] == STATUS_WARN for c in self.checks)

    def verdict_text(self) -> str:
        if self.has_bad:
            return ("Подключению мешает сеть (ниже строки [FAIL]). Нажми "
                    "«Починить VPN+ZT» — скрипт исправит то, что можно "
                    "исправить автоматически, и объяснит остальное.")
        if self.has_warn:
            return ("Критичных проблем нет, но есть предупреждения [!!] — "
                    "подключение может работать нестабильно.")
        return "Проблем с сетью не найдено: если всё равно не подключается — смотри подсказки в деталях строк."


def cmd_diagnose(args: argparse.Namespace) -> int:
    ck = Checker()
    port = int(args.port)

    # ── ZeroTier установлен? ────────────────────────────────────────────
    ztcli = find_ztcli()
    if not ztcli:
        ck.add("zt-installed", "ZeroTier",
               STATUS_BAD,
               "zerotier-cli не найден ни в PATH, ни в Program Files. "
               "Установи ZeroTier One (zerotier.com/download) — без него "
               "друзья смогут подключиться только по реальному/локальному IP.")
        _finish(ck, args)
        return 1

    # ── сервис и корни ─────────────────────────────────────────────────
    ok, out = _run([ztcli, "info"], timeout=6)
    zt_online = "ONLINE" in (out or "").upper()
    if not ok:
        ck.add("zt-service", "Сервис ZeroTier", STATUS_BAD,
               f"zerotier-cli info не ответил: {out.strip() or 'пусто'}. "
               "Служба ZeroTier не запущена — запусти ZeroTier One и повтори.")
    elif zt_online:
        ck.add("zt-service", "Сервис ZeroTier", STATUS_OK, out.strip())
    else:
        ck.add("zt-service", "Сервис ZeroTier", STATUS_BAD,
               f"{out.strip()} — ZeroTier не видит корневые сервера. "
               "Обычно виноват VPN, завернувший весь трафик в туннель "
               "(или полностью мёртвый интернет). «Починить» добавит "
               "/32-маршруты корней через реальный шлюз.")

    networks: list[dict] = []
    if ok:
        nw_json, err = _zt_json(ztcli, "listnetworks")
        if nw_json is not None:
            networks = extract_zt_networks(nw_json)
            if not networks:
                ck.add("zt-network", "Сети ZeroTier", STATUS_BAD,
                       "Ни одна сеть не подключена. Выполни "
                       "`zerotier-cli join <network id>` (id даёт хост) "
                       "и повтори.")
            for nw in networks:
                if nw["status"] == "OK":
                    ck.add(f"zt-net-{nw['nwid']}",
                           f"Сеть {nw['name'] or nw['nwid']}", STATUS_OK,
                           f"статус OK, адреса: {', '.join(nw['assigned']) or '—'}")
                else:
                    ck.add(f"zt-net-{nw['nwid']}",
                           f"Сеть {nw['name'] or nw['nwid']}", STATUS_BAD,
                           f"статус {nw['status']} — сеть не готова "
                           "(авторизация на my.zerotier.com? id верный?)")
        else:
            ck.add("zt-network", "Сети ZeroTier", STATUS_WARN,
                   f"не удалось прочитать список сетей: {err}")

    planet_ips: list[str] = []
    if ok:
        pr_json, err = _zt_json(ztcli, "listpeers")
        if pr_json is not None:
            planet_ips = extract_planet_ips(pr_json)
            n = len(planet_ips)
            if n:
                ck.add("zt-roots", "Корневые серверы ZeroTier", STATUS_OK,
                       f"видно {n} корней (MOON/PLANET): {', '.join(planet_ips)}")
            else:
                ck.add("zt-roots", "Корневые серверы ZeroTier", STATUS_WARN,
                       "в listpeers нет адресов корней — транспорт, "
                       "вероятно, завернут в VPN.")
        else:
            ck.add("zt-roots", "Корневые серверы ZeroTier", STATUS_WARN,
                   f"listpeers не прочитан: {err}")

    # ── адаптеры, VPN, маршруты ─────────────────────────────────────────
    adapters = get_adapters()
    if adapters is None:
        ck.add("adapters", "Сетевые адаптеры", STATUS_SKIP,
               "PowerShell недоступен — пропускаю глубокую проверку "
               "(на Windows он есть всегда).")
    else:
        vpn_names = [a["name"] for a in adapters if a["kind"] == "vpn"]
        zt_names = [a["name"] for a in adapters if a["kind"] == "zerotier"]
        if vpn_names:
            ck.add("adapters", "Адаптеры", STATUS_WARN,
                   "VPN-адаптеры: " + ", ".join(vpn_names) +
                   ". Full-tunnel VPN конкурирует с ZeroTier за маршруты — "
                   "если что-то не так, это главный подозреваемый.")
        else:
            ck.add("adapters", "Адаптеры", STATUS_OK,
                   "VPN-адаптеров нет; ZeroTier: " +
                   (", ".join(zt_names) or "нет"))

    defaults = get_default_routes()
    gw = real_gateway(adapters, defaults)
    if gw:
        ck.add("real-gw", "Реальный шлюз (мимо VPN)", STATUS_OK, gw)
    else:
        ck.add("real-gw", "Реальный шлюз (мимо VPN)", STATUS_SKIP,
               "не определён — /32-маршруты корней добавить не выйдет "
               "(если VPN глотает ZeroTier, поможет только сплит-"
               "туннелирование в самом VPN, см. docs/VPN_ZEROTIER_FIX.md).")

    # Подсети ZeroTier: managed-маршруты + свои адреса (status OK)
    zt_subnets: list[tuple[str, str]] = []
    for nw in networks:
        for target in nw["routes"]:
            nm = net_mask_of(target)
            if nm and nm not in zt_subnets:
                zt_subnets.append(nm)
        if nw["status"] == "OK":
            for a in nw["assigned"]:
                nm = net_mask_of(a)
                if nm and nm not in zt_subnets:
                    zt_subnets.append(nm)

    route_txt: str | None = None
    if os.name == "nt":
        ok_r, out_r = _run(["route", "print", "-4"], timeout=8)
        if ok_r:
            route_txt = out_r
    if route_txt is None and shutil.which("ip"):
        ok_r, out_r = _run(["ip", "-4", "route", "show"], timeout=6)
        if ok_r:
            route_txt = out_r

    if route_txt is None:
        ck.add("routes", "Таблица маршрутов", STATUS_SKIP,
               "не удалось прочитать (не Windows и нет ip?)")
    else:
        routes = parse_route_print(route_txt)
        if routes:
            grabbed = vpn_grabbed(routes)
        else:
            routes = _parse_ip_route(route_txt)
            grabbed = _ip_vpn_grabbed(route_txt)
        if grabbed:
            ck.add("routes", "VPN перехватил маршруты", STATUS_WARN,
                   "есть маршруты 0.0.0.0/1 + 128.0.0.0/1 → весь трафик "
                   "идёт в туннель. Подсети ZeroTier специфичнее (/16), "
                   "но их явные маршруты должны существовать — «Починить» "
                   "добавит их с metric 1.")
        else:
            ck.add("routes", "VPN перехват маршрутов", STATUS_OK,
                   "VPN-маршрутов /1 не видно")
        zt_adapter_ips = _zt_adapter_ips()  # в route print iface — это IP
        for net, mask in zt_subnets:
            probe = net  # адрес сети — репрезентативная точка подсети
            best = route_for_destination(routes, probe) if routes else None
            if best is None:
                ck.add(f"route-{net}", f"Маршрут к {net}/{mask}",
                       STATUS_BAD,
                       "маршрута в подсеть ZeroTier НЕТ — пакеты уйдут "
                       "в VPN/по умолчанию и утонут. «Починить» добавит "
                       "явный on-link маршрут с metric 1.")
            elif best.get("iface") in zt_adapter_ips:
                ck.add(f"route-{net}", f"Маршрут к {net}/{mask}",
                       STATUS_OK,
                       f'через {best["iface"]} (metric {best["metric"]})')
            else:
                ck.add(f"route-{net}", f"Маршрут к {net}/{mask}",
                       STATUS_BAD,
                       f'пакеты уйдут через {best["gw"]} '
                       f'(iface {best["iface"]}, metric {best["metric"]}) '
                       "— это НЕ ZeroTier-интерфейс. «Починить» добавит "
                       "явный маршрут через ZT с metric 1.")

    # ── приоритет маршрутов: метрики интерфейсов (v3.5.11) ──────────────
    # Пользователь помнит: «раньше работало через приоритет маршрутов» —
    # это метрики адаптеров: у ZT должна быть МЕНЬШЕ, чем у VPN.
    metrics = get_iface_metrics()
    zt_idx_d = zt_iface_index(adapters)
    if metrics is None or zt_idx_d is None or zt_idx_d not in metrics:
        ck.add("metrics", "Приоритет маршрутов (метрики)", STATUS_SKIP,
               "не удалось опросить (нужны Windows и ZeroTier-адаптер)")
    else:
        vpn_m = [metrics[a["idx"]] for a in (adapters or [])
                 if a["kind"] == "vpn" and a.get("idx") and a["idx"] in metrics]
        zt_m = metrics[zt_idx_d]
        if vpn_m and zt_m > min(vpn_m):
            ck.add("metrics", "Приоритет маршрутов (метрики)", STATUS_BAD,
                   f"метрика ZeroTier-интерфейса {zt_m} ХУЖЕ, чем у VPN "
                   f"({min(vpn_m)}) — при совпадении префиксов пакеты уходят "
                   "в VPN. «Починить» выставит ZT = 1, VPN = 100 — приоритет "
                   "маршрутов, как работало раньше.")
        else:
            ck.add("metrics", "Приоритет маршрутов (метрики)", STATUS_OK,
                   f"метрика ZT-интерфейса {zt_m}"
                   + (f" не хуже VPN ({min(vpn_m)})" if vpn_m
                      else " (VPN-адаптеров нет)"))

    # ── MTU ZT-адаптера при полном туннеле (v3.5.11) ───────────────────
    if zt_idx_d is None:
        ck.add("mtu", "MTU ZeroTier-адаптера", STATUS_SKIP,
               "ZeroTier-адаптер не найден")
    else:
        mtu = get_iface_mtu(zt_idx_d)
        has_vpn = any(a["kind"] == "vpn" for a in (adapters or []))
        if mtu is None:
            ck.add("mtu", "MTU ZeroTier-адаптера", STATUS_SKIP,
                   "не удалось опросить")
        elif has_vpn and mtu > 1500:
            ck.add("mtu", "MTU ZeroTier-адаптера", STATUS_WARN,
                   f"MTU {mtu} при активном VPN: крупные пакеты дробятся и "
                   "теряются (файлы виснут, голос дёргается). «Починить» "
                   "мягко снизит до 1400 (до перезагрузки).")
        else:
            ck.add("mtu", "MTU ZeroTier-адаптера", STATUS_OK,
                   f"MTU {mtu} — нормально")

    # ── профиль сети ZeroTier (v3.5.11) ────────────────────────────────
    if zt_idx_d is None:
        ck.add("zt-profile", "Профиль сети ZeroTier", STATUS_SKIP,
               "ZeroTier-адаптер не найден")
    else:
        cat = get_network_category(zt_idx_d)
        if cat is None:
            ck.add("zt-profile", "Профиль сети ZeroTier", STATUS_SKIP,
                   "не удалось опросить")
        elif cat.lower() == "public":
            ck.add("zt-profile", "Профиль сети ZeroTier", STATUS_WARN,
                   "Windows считает сеть ПУБЛИЧНОЙ — входящие могут "
                   "резаться даже при правилах. «Починить» переключит "
                   "в «Частная».")
        else:
            ck.add("zt-profile", "Профиль сети ZeroTier", STATUS_OK,
                   f"категория {cat}")

    # ── сервер слушает? ────────────────────────────────────────────────
    zt_ips = [a.split("/")[0] for nw in networks if nw["status"] == "OK"
              for a in nw["assigned"]]
    if probe_tcp("127.0.0.1", port):
        if zt_ips:
            ok_all = all(probe_tcp(ip, port) for ip in zt_ips)
            if ok_all:
                ck.add("server-port", f"Сервер на порту {port}", STATUS_OK,
                       "отвечает и на 127.0.0.1, и на ZeroTier-адресе")
            else:
                ck.add("server-port", f"Сервер на порту {port}", STATUS_BAD,
                       "отвечает на 127.0.0.1, но НЕ на ZeroTier-адресе — "
                       "сервер привязан к одному интерфейсу. Перезапусти "
                       "сервер с привязкой «Все интерфейсы».")
        else:
            ck.add("server-port", f"Сервер на порту {port}", STATUS_OK,
                   "отвечает на 127.0.0.1 (ZeroTier-адресов нет — "
                   "проверять больше нечего)")
    else:
        ck.add("server-port", f"Сервер на порту {port}", STATUS_WARN,
               "порт закрыт — сервер сейчас не запущен. Это отдельная "
               "причина «не подключиться»: запусти сервер кнопкой "
               "«Запустить сервер».")

    # ── «библиотека адресов»: какие IP РЕАЛЬНЫЕ (v3.5.11) ──────────────
    # Собираем ВСЕ адреса, которые программа когда-либо видела/записывала
    # (host_url клиента, журналы подключений хоста, ZeroTier), сортируем
    # по вероятности и честно проверяем порт — человек видит ответ «какой
    # адрес использовать», а не гадает по списку айпишников.
    zt_subnets_v4 = []
    for net_s, mask_s in zt_subnets:
        try:
            zt_subnets_v4.append(
                ipaddress.ip_network(f"{net_s}/{mask_s}", strict=False))
        except ValueError:
            pass
    vpn_ip_set: set[str] = set()
    for a in (adapters or []):
        if a["kind"] == "vpn" and a.get("idx"):
            ok_v, out_v = _run(ps_cmd(
                f"(Get-NetIPAddress -InterfaceIndex {a['idx']} -AddressFamily "
                "IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1 "
                "-ExpandProperty IPAddress)"), timeout=8)
            if ok_v and out_v.strip():
                vpn_ip_set.add(out_v.strip())
    own_ip_set: set[str] = set(zt_ips) | set(_zt_adapter_ips())
    cands: list[dict] = []
    seen_ip: set[str] = set()
    for c in harvest_known_ips(library_roots()):
        ip = c["ip"]
        if ip in seen_ip:
            continue
        seen_ip.add(ip)
        cands.append({"ip": ip, "source": c["source"],
                      "kind": classify_candidate_ip(
                          ip, zt_subnets_v4, vpn_ip_set, own_ip_set)})
    for ip in zt_ips:
        if ip not in seen_ip:
            seen_ip.add(ip)
            cands.append({"ip": ip, "source": "ZeroTier",
                          "kind": classify_candidate_ip(
                              ip, zt_subnets_v4, vpn_ip_set, own_ip_set)})
    ranked = rank_addresses(cands, port)
    if not ranked:
        ck.add("addresses", "Библиотека адресов (какие РЕАЛЬНЫЕ)",
               STATUS_SKIP,
               "адресов в истории не нашлось (это не ошибка) — впиши "
               "адрес хоста вручную в настройках подключения.")
    else:
        alive_n = sum(1 for c in ranked if c.get("alive"))
        show = ranked[:4]
        detail = "; ".join(address_label(c, port) for c in show)
        hidden = len(ranked) - len(show)
        if hidden > 0:
            detail += f"; …и ещё {hidden}"
        detail += ". ИТОГ: " + address_verdict(ranked, port)
        ck.add("addresses", "Библиотека адресов (какие РЕАЛЬНЫЕ)",
               STATUS_OK if alive_n else STATUS_WARN, detail)

    # ── брендмауэр ─────────────────────────────────────────────────────
    fw = firewall_rules_present()
    if fw is None:
        ck.add("firewall", "Брендмауэр Windows", STATUS_SKIP,
               "не удалось опросить (PowerShell недоступен)")
    elif fw:
        ck.add("firewall", "Брендмауэр Windows", STATUS_OK,
               'правила «Friend Relay …» уже есть')
    else:
        ck.add("firewall", "Брендмауэр Windows", STATUS_WARN,
               "нет правил «Friend Relay …»: ZeroTier-интерфейс Windows "
               "считает ПУБЛИЧНОЙ сетью, входящие могут резаться. "
               "«Починить» добавит разрешающие правила для портов.")

    _finish(ck, args)
    return 1 if ck.has_bad else 0


def _parse_ip_route(text: str) -> list[dict]:
    """`ip -4 route show` (Linux) → те же записи, что parse_route_print."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        net = parts[0] if parts[0] != "default" else "0.0.0.0/0"
        nm = net_mask_of(net)
        if not nm:
            continue
        gw = "on-link"
        if "via" in parts:
            gw = parts[parts.index("via") + 1]
        out.append({"net": nm[0], "mask": nm[1], "gw": gw,
                    "iface": "linux", "metric": 0})
    return out


def _ip_vpn_grabbed(text: str) -> bool:
    """Linux-вариант vpn_grabbed: default через tun/wg-устройство."""
    for line in (text or "").splitlines():
        if line.startswith("default") and re.search(
                r"dev (tun|tap|wg|zt|tailscale)", line):
            return True
    return False


def _zt_json(ztcli: str, sub: str) -> tuple[dict | list | None, str]:
    ok, out = _run([ztcli, "-j", sub], timeout=8)
    if not ok:
        return None, (out or "").strip() or "zerotier-cli вернул ошибку"
    try:
        return json.loads(out), ""
    except ValueError as e:
        return None, f"битый JSON: {e}"


def _zt_adapter_ips() -> set[str]:
    """IPv4-адреса ZT-адаптеров (для сопоставления route print ↔ интерфейс)."""
    ips: set[str] = set()
    adapters = get_adapters()
    if not adapters:
        return ips
    for a in adapters:
        if a["kind"] != "zerotier":
            continue
        ok, out = _run(ps_cmd(
            f"(Get-NetIPAddress -InterfaceIndex {a['idx']} "
            "-AddressFamily IPv4 -ErrorAction SilentlyContinue"
            " | Select-Object -First 1 -ExpandProperty "
            "IPAddress)"), timeout=8)
        if ok and out.strip():
            ips.add(out.strip())
    return ips


# ── файловая часть «библиотеки адресов» ─────────────────────────────────

_LIB_SUFFIXES = {".json", ".jsonl", ".log", ".txt"}
_LIB_SKIP_DIRS = {"files", "avatars", "music", "__pycache__", "logs_old"}


def library_roots() -> list[Path]:
    """Где программа хранит адреса: settings.json клиента, relay_data хоста
    (журналы сервера — там IP всех подключений), relay_data рядом со скриптом.
    Существующие пути, без дублей."""
    roots = [
        Path.home() / ".friend_relay" / "settings.json",
        Path.home() / ".friend_relay" / "relay_data",
        PROJECT_ROOT / "relay_data",
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        if str(r) in seen or not r.exists():
            continue
        seen.add(str(r))
        out.append(r)
    return out


def harvest_known_ips(roots: list[Path], max_files: int = 300,
                      max_file_bytes: int = 2_000_000) -> list[dict]:
    """Сканирует файлы «библиотеки» → уникальные [{ip, source}].

    Бинарные медиа-папки (files/avatars/music) пропускает, большие файлы
    режет до max_file_bytes, кракозябр боится smart_decode."""
    found: list[dict] = []
    seen: set[str] = set()
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_dir():
                    continue
                if any(part.lower() in _LIB_SKIP_DIRS for part in p.parts):
                    continue
                if p.suffix.lower() in _LIB_SUFFIXES or p.name.startswith(
                        "server.log"):
                    files.append(p)
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    for f in files:
        try:
            if f.stat().st_size > max_file_bytes * 4:
                continue
            raw = f.read_bytes()[:max_file_bytes]
        except OSError:
            continue
        text = smart_decode(raw)
        for ip in harvest_ips_from_text(text):
            if ip in seen:
                continue
            seen.add(ip)
            found.append({"ip": ip, "source": f.name})
    return found


def _finish(ck: Checker, args: argparse.Namespace) -> None:
    """Собирает отчёт, печатает/сохраняет."""
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "diagnose",
        "admin": is_admin(),
        "port": int(args.port),
        "checks": ck.checks,
        "verdict": ck.verdict_text(),
        "fix_available": bool(ck.has_bad or ck.has_warn),
    }
    _emit(report, args)


def _emit(report: dict, args: argparse.Namespace) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if getattr(args, "json", False):
        print(text)
        return
    for c in report.get("checks", []):
        icon = _STATUS_ICON.get(c["status"], "[?]")
        line = f"{icon} {c['title']}"
        if c["status"] != STATUS_OK or c.get("detail"):
            line += f" — {c['detail']}"
        print(line)
    print()
    print("ИТОГ: " + report.get("verdict", ""))
    if getattr(args, "no_file", False):
        return
    path = Path(args.report) if args.report else \
        PROJECT_ROOT / "relay_data" / "vpn_fixer_report.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"(отчёт сохранён: {path})")
    except OSError as e:
        print(f"(отчёт НЕ сохранён: {e})")


# ═══════════════════════════════════════════════════════════════════════
# Режим fix (права администратора)
# ═══════════════════════════════════════════════════════════════════════


class Action:
    """Один шаг починки: команда + результат."""

    def __init__(self, title: str, cmd: list[str]) -> None:
        self.title = title
        self.cmd = cmd
        self.ok = False
        self.detail = ""

    def run(self, timeout: float = 10.0) -> None:
        self.ok, self.detail = _run(self.cmd, timeout=timeout)

    def as_dict(self) -> dict:
        return {"title": self.title, "cmd": " ".join(self.cmd),
                "ok": self.ok, "detail": (self.detail or "").strip()[:400]}


def cmd_fix(args: argparse.Namespace) -> int:
    if os.name != "nt":
        print("Автопочинка реализована для Windows. На Linux/macOS настрой "
              "маршруты вручную — см. docs/VPN_ZEROTIER_FIX.md.")
        return 4
    if not is_admin():
        print("[FAIL] Нужны права администратора: маршрутами и брендмауэром "
              "управляет только админ.")
        print("       Запусти scripts/fix_zerotier_vpn.bat ПКМ → «Запуск "
              "от имени администратора», или жми кнопку в GUI сервера.")
        if not args.no_pause:
            _pause()
        return 3

    fw_ports = str(args.fw_ports)
    actions: list[Action] = []
    print("═" * 64)
    print(" Friend Relay — починка ZeroTier + VPN (7 групп фиксов, v3.5.11)")
    print(" Маршруты подсетей теперь ПЕРСИСТЕНТНЫЕ (-p) — переживают ребут.")
    print(" Метрики интерфейсов / MTU / профиль сети — до перезагрузки.")
    print("═" * 64)
    print()

    def _mark(ok: bool) -> str:
        return "OK" if ok else "FAIL"

    def _log(a: Action) -> None:
        print(f"     [{_mark(a.ok)}] {a.title}"
              + ("" if a.ok else f" — {a.detail.strip()}"))

    # ── 1/7 брендмауэр: порты + ICMP + программа ZeroTier ────────────
    print("[1/7] Брендмауэр: порты", fw_ports, "+ ICMP + программа ZeroTier")
    for proto in ("TCP", "UDP"):
        name = f"Friend Relay {fw_ports} {proto}"
        d = Action(f"удалить старое правило {name}",
                   ["netsh", "advfirewall", "firewall", "delete", "rule",
                    f"name={name}"])
        d.run()  # ошибка «не найдено» — нормально, не логируем как провал
        a = Action(f"разрешить входящие {proto} {fw_ports}",
                   ["netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={name}", "dir=in", "action=allow",
                    f"protocol={proto}", f"localport={fw_ports}",
                    "profile=any"])
        a.run()
        actions.append(a)
        _log(a)
    d = Action("удалить старое правило Friend Relay ICMP",
               ["netsh", "advfirewall", "firewall", "delete", "rule",
                "name=Friend Relay ICMP"])
    d.run()
    a = Action("разрешить входящие ICMP (пинг хоста у друзей заработает)",
               build_icmp_rule_cmd())
    a.run()
    actions.append(a)
    _log(a)
    zt_exe = zt_exe_path(find_ztcli())
    if zt_exe:
        d = Action("удалить старое правило Friend Relay ZeroTier Core",
                   ["netsh", "advfirewall", "firewall", "delete", "rule",
                    "name=Friend Relay ZeroTier Core"])
        d.run()
        a = Action(f"разрешить программу ZeroTier ({zt_exe})",
                   build_zt_program_rule_cmd(zt_exe))
        a.run()
        actions.append(a)
        _log(a)
    else:
        print("     [skip] zerotier-one.exe не найден — правило программы "
              "пропускаю")

    # ── 2/7 ZeroTier: данные и служба ──────────────────────────────────
    print()
    print("[2/7] ZeroTier: сети, интерфейс и служба")
    ztcli = find_ztcli()
    networks: list[dict] = []
    planet_ips: list[str] = []
    adapters = get_adapters()
    if not ztcli:
        print("     [FAIL] zerotier-cli не найден — ZeroTier установлен?")
        ck_fake = {"id": "zt", "title": "ZeroTier", "status": STATUS_BAD,
                   "detail": "zerotier-cli не найден"}
        _write_fix_report(args, actions, extra_checks=[ck_fake])
        if not args.no_pause:
            _pause()
        return 2
    nw_json, err = _zt_json(ztcli, "listnetworks")
    if nw_json is not None:
        networks = extract_zt_networks(nw_json)
    pr_json, _err = _zt_json(ztcli, "listpeers")
    if pr_json is not None:
        planet_ips = extract_planet_ips(pr_json)

    ok_info, out_info = _run([ztcli, "info"], timeout=6)
    zt_online = "ONLINE" in (out_info or "").upper()
    if not ok_info:
        print(f"     [FAIL] zerotier-cli info не ответил: {out_info.strip()}")
    else:
        print(f"     ZeroTier: {out_info.strip()}")

    zt_svc = service_status()
    if zt_svc and zt_svc != "Running":
        a = Action(f"запустить службу {ZT_SERVICE} (сейчас {zt_svc})",
                   build_service_start_cmd())
        a.run()
        actions.append(a)
        _log(a)
    else:
        print(f"     [OK] служба {ZT_SERVICE}: {zt_svc or 'статус неизвестен'}")

    zt_idx = zt_iface_index(adapters)
    zt_ip: str | None = None
    for nw in networks:
        if nw["status"] == "OK" and nw["assigned"]:
            zt_ip = nw["assigned"][0].split("/")[0]
            break
    print(f"     ZT-интерфейс: index={zt_idx}, свой ZT-IP: {zt_ip or '—'}")

    # ── 3/7 маршруты подсетей ZT (metric 1, -p) + метрика ZT = 1 ────────
    print()
    print("[3/7] Маршруты в подсеть ZeroTier (metric 1, персистентные -p) "
          "+ метрика ZT = 1")
    subnets: list[tuple[str, str]] = []
    for nw in networks:
        for target in nw["routes"]:
            nm = net_mask_of(target)
            if nm and nm not in subnets:
                subnets.append(nm)
        if nw["status"] == "OK":
            for assigned in nw["assigned"]:
                nm = net_mask_of(assigned)
                if nm and nm not in subnets:
                    subnets.append(nm)
    if not subnets:
        print("     [FAIL] Подсетей ZeroTier не найдено (сеть не OK?) — "
              "сначала подключи сеть zerotier-cli join <id>.")
    for net, mask in subnets:
        a_del = Action(f"route delete {net}",
                       ["route", "delete", net, "mask", mask])
        a_del.run()  # «не найдено» — нормально: чистим старый мусор
        a = Action(f"route -p add {net}/{mask} через ZT (metric 1)",
                   build_subnet_route_cmd(net, mask, zt_idx, zt_ip,
                                          persistent=True))
        a.run()
        actions.append(a)
        _log(a)
    if zt_idx is not None:
        name = zt_iface_name(adapters)
        if name:
            a = Action(f'метрика интерфейса «{name}» = 1 '
                       "(приоритет маршрутов, как работало раньше)",
                       build_iface_metric_cmd(name, 1))
            a.run()
            actions.append(a)
            _log(a)
            if not a.ok:
                a2 = Action(f"метрика ZT через PowerShell (idx {zt_idx}) = 1",
                            build_ps_iface_metric_cmd(zt_idx, 1))
                a2.run()
                actions.append(a2)
                _log(a2)

    # ── 4/7 приоритет: VPN-адаптеры вниз, профиль ZT-сети Частная ──────
    print()
    print("[4/7] Приоритет маршрутов: метрика VPN = 100, профиль ZT-сети "
          "→ «Частная»")
    vpn_adapters = [a for a in (adapters or [])
                    if a["kind"] == "vpn" and a.get("idx")]
    if not vpn_adapters:
        print("     [skip] VPN-адаптеров нет — приоритеты не трогаю.")
    for va in vpn_adapters:
        a = Action(f'метрика VPN-адаптера «{va["name"]}» = 100 '
                   "(ZT выигрывает совпадения; сам VPN продолжает работать)",
                   build_ps_iface_metric_cmd(va["idx"], 100))
        a.run()
        actions.append(a)
        _log(a)
    if zt_idx is not None:
        a = Action("профиль ZT-сети → «Частная» (входящие больше не режутся)",
                   build_ps_profile_private_cmd(zt_idx))
        a.run()
        actions.append(a)
        _log(a)

    # ── 5/7 корни ZeroTier: /32 через реальный шлюз ────────────────────
    print()
    print("[5/7] Транспорт ZeroTier мимо VPN (/32-маршруты корней)")
    gw = real_gateway(adapters, get_default_routes())
    if not planet_ips:
        print("     [skip] Корни не определены (listpeers пуст/бит) — "
              "пропускаю; если сервис OFFLINE, включи в VPN сплит-"
              "туннелирование (docs/VPN_ZEROTIER_FIX.md, Фикс 1).")
    elif not gw:
        print("     [skip] Реальный шлюз не определён — пропускаю.")
    else:
        for ip in planet_ips:
            a_del = Action(f"route delete {ip}/32",
                           ["route", "delete", ip, "mask",
                            "255.255.255.255"])
            a_del.run()
            a = Action(f"route add {ip}/32 через {gw} (metric 1)",
                       build_planet_route_cmd(ip, gw))
            a.run()
            actions.append(a)
            _log(a)

    # ── 6/7 MTU ZT-адаптера при полном туннеле ────────────────────────
    print()
    print("[6/7] MTU ZeroTier-адаптера (2800 → 1400 при полном туннеле VPN)")
    if zt_idx is None or not vpn_adapters:
        print("     [skip] Нет ZT-адаптера или VPN — MTU не трогаю.")
    else:
        zt_name = zt_iface_name(adapters)
        zt_mtu = get_iface_mtu(zt_idx)
        if zt_name and zt_mtu and zt_mtu > 1500:
            a = Action(f"MTU «{zt_name}» = 1400 (было {zt_mtu}: крупные "
                       "пакеты больше не дробятся в туннеле; до перезагрузки)",
                       build_ps_mtu_cmd(zt_name, 1400))
            a.run()
            actions.append(a)
            _log(a)
        else:
            print(f"     [OK] MTU сейчас {zt_mtu or '?'} — менять не нужно.")

    # ── 7/7 рестарт службы, если ZeroTier OFFLINE ──────────────────────
    print()
    print("[7/7] Рестарт службы ZeroTier, если она OFFLINE")
    if zt_svc == "Running" and not zt_online:
        a = Action(f"рестарт {ZT_SERVICE} (пере-рукопожатие после новых "
                   "маршрутов)",
                   build_service_restart_cmd())
        a.run(timeout=30)
        actions.append(a)
        _log(a)
    elif not zt_online:
        print("     [skip] Служба не Running/статус неизвестен — рестарт "
              "пропущен (если ZeroTier только что установлен — запусти "
              "ZeroTier One).")
    else:
        print("     [OK] ZeroTier ONLINE — рестарт не нужен.")

    # ── итог ───────────────────────────────────────────────────────────
    failed = [a for a in actions if not a.ok]
    print()
    print("═" * 64)
    if failed:
        print(f" ГОТОВО С ОГОВОРКАМИ: {len(failed)} из {len(actions)} шагов "
              "упали — детали выше и в отчёте.")
    else:
        print(f" ГОТОВО: {len(actions)} шагов выполнено.")
    print(" Приоритет маршрутов выставлен: ZT metric 1, VPN metric 100.")
    print(" Проверь: `zerotier-cli peers` — пиры ONLINE за 10-30 сек,")
    print(f" затем открой http://<ZT-IP-хоста>:{args.port}/ у друга.")
    print(" Если всё ещё глухо — виноват kill-switch VPN: включи в Amnezia")
    print(" сплит-туннелирование (docs/VPN_ZEROTIER_FIX.md, Фикс 1).")
    print("═" * 64)
    _write_fix_report(args, actions)
    if not args.no_pause:
        _pause()
    return 2 if failed else 0


def _write_fix_report(args: argparse.Namespace, actions: list[Action],
                      extra_checks: list[dict] | None = None) -> None:
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "fix",
        "admin": is_admin(),
        "port": int(args.port),
        "checks": extra_checks or [],
        "actions": [a.as_dict() for a in actions],
        "verdict": ("Починка выполнена (см. шаги). Нажми «Проверить снова» "
                    "в окне сервера." if all(a.ok for a in actions)
                    else "Часть шагов упала — детали в actions."),
        "fix_available": False,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    path = Path(args.report) if args.report else \
        PROJECT_ROOT / "relay_data" / "vpn_fixer_report.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"(отчёт сохранён: {path})")
    except OSError as e:
        print(f"(отчёт НЕ сохранён: {e})")


def _pause() -> None:
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nНажми Enter, чтобы закрыть окно…")
    except (EOFError, OSError):
        pass


def cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.report) if args.report else \
        PROJECT_ROOT / "relay_data" / "vpn_fixer_report.json"
    if not path.exists():
        print("Отчёта ещё нет — сначала запусти diagnose (кнопка в GUI "
              "или `python scripts/fix_vpn_zt.py diagnose`).")
        return 2
    print(path.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Friend Relay: диагностика и починка ZeroTier+VPN")
    p.add_argument("command", choices=["diagnose", "fix", "report"])
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--fw-ports", default="8420-8424",
                   help="диапазон портов для правил брендмауэра")
    p.add_argument("--report", default=None, help="путь JSON-отчёта")
    p.add_argument("--json", action="store_true", help="только JSON на stdout")
    p.add_argument("--no-file", action="store_true",
                   help="не писать файл отчёта")
    p.add_argument("--no-pause", action="store_true",
                   help="не ждать Enter в конце")
    args = p.parse_args(argv)
    if args.command == "diagnose":
        return cmd_diagnose(args)
    if args.command == "fix":
        return cmd_fix(args)
    return cmd_report(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
