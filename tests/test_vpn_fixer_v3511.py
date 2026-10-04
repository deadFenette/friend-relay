#!/usr/bin/env python3
"""(v3.5.11) Проверки честного фикса VPN+ZeroTier.

История: кнопка «Починить VPN+ZT» запускала .bat, который печатал
кракозябры, падал на %ProgramFiles(x86)% и делал route add с ИМЕНЕМ сети.
v3.5.10 заменил его на fix_vpn_zt.py (JSON от zerotier-cli -j, UTF-8).

НОВОЕ В v3.5.11 (отзыв с поля: «почини артефакты» + «работало через
приоритет маршрутов»):
  • smart_decode + UTF-8-preamble PowerShell — кракозябры в названиях
    адаптеров («Подключение по локальной сети») вылечены конструктивно;
  • «Библиотека адресов» — диагностика собирает IP из settings.json,
    журналов relay_data и ZeroTier, классифицирует и проверяет порт,
    показывая, какой адрес РЕАЛЬНЫЙ и «вероятнее всего подойдёт»;
  • автофиксов 7 групп: персистентные маршруты (-p), метрика ZT = 1,
    VPN = 100, профиль сети «Частная», правила ICMP и программы ZeroTier,
    MTU 2800 → 1400 при полном туннеле, старт/рестарт службы ZeroTier.

Без PySide6 и без сети: чистые парсеры проверяются на подделках,
diagnose запускается подпроцессом (в песочнице ZeroTier нет — это
нормальный путь «не найден → понятное сообщение»).
"""
from __future__ import annotations

import importlib.util
import json
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1


def load_fixer():
    spec = importlib.util.spec_from_file_location(
        "fix_vpn_zt", ROOT / "scripts" / "fix_vpn_zt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    fixer = load_fixer()

    print("── компиляция и гигиена")
    try:
        py_compile.compile(str(ROOT / "scripts" / "fix_vpn_zt.py"),
                           doraise=True)
        check("scripts/fix_vpn_zt.py компилируется", True)
    except Exception as e:
        check(f"компиляция: {e}", False)

    bat = (ROOT / "scripts" / "fix_zerotier_vpn.bat").read_bytes()
    check(".bat чистый ASCII (кракозябрам взяться неоткуда)",
          all(b < 128 for b in bat))
    check(".bat не содержит бага ProgramFiles(x86) в if-блоке",
          b"ProgramFiles(x86)" not in bat)
    check(".bat больше не парсит listnetworks сломанным tokens=4",
          b"tokens=4" not in bat)
    check(".bat — обёртка, запускает fix_vpn_zt.py",
          b"fix_vpn_zt.py" in bat)

    src = (ROOT / "scripts" / "fix_vpn_zt.py").read_text(encoding="utf-8")
    check("в fix 7 групп шагов ([1/7]…[7/7])",
          all(f"[{i}/7]" in src for i in range(1, 8)))
    check("маршруты подсетей добавляются персистентными (persistent=True)",
          "persistent=True" in src)
    check("_run декодирует байты через smart_decode (не фиксированный UTF-8)",
          "smart_decode(raw)" in src)
    check("PowerShell зовётся только через ps_cmd — литерал команды ровно один "
          "(в самой ps_cmd)",
          src.count('"powershell", "-NoProfile", "-Command"') == 1
          and src.count("ps_cmd(") >= 6)

    print("── кракозябры: smart_decode (главная жалоба v3.5.11)")
    phrase = "Подключение по локальной сети"
    check("CP866-байты декодируются в честный русский",
          fixer.smart_decode(phrase.encode("cp866")) == phrase)
    check("UTF-8-байты не портятся",
          fixer.smart_decode(phrase.encode("utf-8")) == phrase)
    check("CP1251-байты декодируются (скоринг отличает от CP866)",
          fixer.smart_decode(phrase.encode("cp1251")) == phrase)
    check("prefer-кодировка пробуется первой",
          fixer.smart_decode(phrase.encode("cp866"), prefer="cp866")
          == phrase)
    check("ASCII проходит как есть",
          fixer.smart_decode(b"Ethernet 2") == "Ethernet 2")
    check("битые/чужие байты не роняют декод — всегда строка",
          isinstance(fixer.smart_decode(b"\xff\xfe\x81"), str))
    check("скоринг: у честного декода очков больше, чем у псевдографики",
          fixer._cyrillic_score(phrase)
          > fixer._cyrillic_score(
              phrase.encode("cp1251").decode("cp866")))
    check("ps_cmd подставляет UTF-8-preamble (скрипт — 4-й элемент)",
          fixer.ps_cmd("X")[3].startswith("[Console]::OutputEncoding=")
          and "X" in fixer.ps_cmd("X")[3]
          and fixer.ps_cmd("X")[:3] ==
          ["powershell", "-NoProfile", "-Command"])
    code_cp866 = ("import sys; sys.stdout.buffer.write("
                  "'Подключение по локальной сети'.encode('cp866'))")
    ok_run, out_run = fixer._run([sys.executable, "-c", code_cp866])
    check("_run реально декодирует CP866-поток дочернего процесса",
          ok_run and phrase in out_run)

    print("── математика подсетей (старый .bat тут делал мусор)")
    check("net_mask_of('172.28.1.5/16') → 172.28.0.0/255.255.0.0",
          fixer.net_mask_of("172.28.1.5/16") == ("172.28.0.0",
                                                 "255.255.0.0"))
    check("net_mask_of без префикса (/32)",
          fixer.net_mask_of("100.64.1.2") == ("100.64.1.2",
                                              "255.255.255.255"))
    check("net_mask_of мусора → None",
          fixer.net_mask_of("не ip") is None)
    check("net_mask_of IPv6 → None (маршруты ZT — IPv4)",
          fixer.net_mask_of("fcba:a9ce:f2d5::/64") is None)

    print("── JSON от zerotier-cli (старый .bat брал не тот столбец)")
    nw_json = [
        {"id": "8056c2e21c000001", "name": "my-net", "status": "OK",
         "assignedIps": ["172.28.1.5/16"],
         "routes": [{"target": "172.28.0.0/16", "via": None}]},
        {"id": "bad0bad0bad0bad0", "name": "broken", "status":
         "ACCESS_DENIED", "assignedIps": [], "routes": []},
    ]
    nets = fixer.extract_zt_networks(nw_json)
    check("extract_zt_networks: обе сети прочитаны", len(nets) == 2)
    check("статус OK распознан",
          nets[0]["status"] == "OK" and nets[0]["assigned"] ==
          ["172.28.1.5/16"])
    check("маршруты собраны (172.28.0.0/16)",
          nets[0]["routes"] == ["172.28.0.0/16"])
    check("ACCESS_DENIED не теряется",
          nets[1]["status"] == "ACCESS_DENIED")
    check("мусор вместо JSON → пусто",
          fixer.extract_zt_networks({"oops": 1}) == [])

    peers_json = [
        {"role": "PLANET", "version": "1.10.6", "latency": 42,
         "paths": [{"address": "50.7.252.138/9993", "active": True},
                   {"address": "fcba:a9ce:f2d5:0001::1/9993"}]},
        {"role": "PLANET", "version": "", "latency": -1,
         "paths": [{"address": "103.195.103.66/9993"}]},
        {"role": "LEAF", "version": "1.10.3",
         "paths": [{"address": "1.2.3.4/9993"}]},
    ]
    ips = fixer.extract_planet_ips(peers_json)
    check("extract_planet_ips: корни собраны, дублей нет",
          ips == ["50.7.252.138", "103.195.103.66"])
    check("LEAF-пиры и IPv6 в /32-маршруты не попадают",
          "1.2.3.4" not in ips and all(":" not in i for i in ips))
    check("мусор вместо JSON → пусто", fixer.extract_planet_ips(None) == [])

    print("── таблица маршрутов Windows")
    route_print = """
===========================================================================
Active Routes:
===========================================================================
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0    192.168.1.1    192.168.1.10     25
          0.0.0.0        128.0.0.0    10.211.55.2     10.211.55.3      1
        128.0.0.0        128.0.0.0    10.211.55.2     10.211.55.3      1
        172.28.0.0      255.255.0.0        On-link       172.28.1.5      5
        192.168.1.0    255.255.255.0        On-link      192.168.1.10    35
===========================================================================
Persistent Routes:
  None
"""
    routes = fixer.parse_route_print(route_print)
    check("parse_route_print: 5 активных маршрутов", len(routes) == 5)
    check("On-link-шлюз не ломает разбор",
          any(r["gw"] == "On-link" and r["iface"] == "172.28.1.5"
              for r in routes))
    check("vpn_grabbed: 0.0.0.0/1 + 128.0.0.0/1 найдены",
          fixer.vpn_grabbed(routes))
    check("vpn_grabbed: без /1-маршрутов — False",
          not fixer.vpn_grabbed([{"net": "0.0.0.0", "mask": "0.0.0.0",
                                  "gw": "192.168.1.1",
                                  "iface": "192.168.1.10", "metric": 25}]))

    best = fixer.route_for_destination(routes, "172.28.60.1")
    check("длинный префикс побеждает: 172.28.x.x → маршрут /16 через ZT",
          best is not None and best["net"] == "172.28.0.0")
    best2 = fixer.route_for_destination(routes, "8.8.8.8")
    check("8.8.8.8 уходит в VPN (/1, metric 1 — а не /0 metric 25)",
          best2 is not None and best2["mask"] == "128.0.0.0"
          and best2["metric"] == 1)
    check("нет покрывающего маршрута → None",
          fixer.route_for_destination([], "172.28.1.5") is None)

    print("── команды маршрутов/метрик (то, что старый .bat делал мусором)")
    cmd = fixer.build_subnet_route_cmd("172.28.0.0", "255.255.0.0", 21, None)
    check("route add через ZT-интерфейс: on-link + metric 1 + if",
          cmd == ["route", "add", "172.28.0.0", "mask", "255.255.0.0",
                  "0.0.0.0", "metric", "1", "if", "21"])
    cmd2 = fixer.build_subnet_route_cmd("172.28.0.0", "255.255.0.0", None,
                                        "172.28.1.5")
    check("fallback без индекса: шлюз = свой ZT-IP",
          cmd2 == ["route", "add", "172.28.0.0", "mask", "255.255.0.0",
                   "172.28.1.5", "metric", "1"])
    cmd_p = fixer.build_subnet_route_cmd("172.28.0.0", "255.255.0.0", 21,
                                         None, persistent=True)
    check("персистентный маршрут: route -p add … (переживает ребут)",
          cmd_p == ["route", "-p", "add", "172.28.0.0", "mask",
                    "255.255.0.0", "0.0.0.0", "metric", "1", "if", "21"])
    cmd3 = fixer.build_planet_route_cmd("50.7.252.138", "192.168.1.1")
    check("/32 корня через реальный шлюз",
          cmd3 == ["route", "add", "50.7.252.138", "mask",
                   "255.255.255.255", "192.168.1.1", "metric", "1"])
    check("метрика интерфейса через netsh (приоритет маршрутов)",
          fixer.build_iface_metric_cmd("ZeroTier One", 1) ==
          ["netsh", "interface", "ipv4", "set", "interface",
           "ZeroTier One", "metric=1"])
    ps_m = fixer.build_ps_iface_metric_cmd(21, 100)
    check("метрика VPN через PowerShell (fallback)",
          ps_m[0] == "powershell" and "Set-NetIPInterface" in ps_m[3]
          and "-InterfaceMetric 100" in ps_m[3]
          and ps_m[3].startswith("[Console]::OutputEncoding="))
    ps_p = fixer.build_ps_profile_private_cmd(21)
    check("профиль ZT-сети → Private",
          "Set-NetConnectionProfile" in ps_p[3]
          and "-NetworkCategory Private" in ps_p[3])
    check("MTU 2800 → 1400 (store=active, до перезагрузки)",
          fixer.build_ps_mtu_cmd("ZeroTier One", 1400) ==
          ["netsh", "interface", "ipv4", "set", "subinterface",
           "ZeroTier One", "mtu=1400", "store=active"])
    icmp = fixer.build_icmp_rule_cmd()
    check("правило ICMP для пинга",
          icmp[0] == "netsh" and "name=Friend Relay ICMP" in icmp
          and "protocol=icmpv4" in icmp and "profile=any" in icmp)
    check("правило программы ZeroTier (zerotier-one.exe)",
          "program=C:\\zt\\zerotier-one.exe"
          in fixer.build_zt_program_rule_cmd("C:\\zt\\zerotier-one.exe")
          and "name=Friend Relay ZeroTier Core"
          in fixer.build_zt_program_rule_cmd("C:\\zt\\zerotier-one.exe"))
    check("служба: net start / Restart-Service ZeroTierOneService",
          fixer.build_service_start_cmd() == ["net", "start",
                                              "ZeroTierOneService"]
          and "Restart-Service" in " ".join(fixer.build_service_restart_cmd())
          and "ZeroTierOneService"
          in " ".join(fixer.build_service_restart_cmd()))

    print("── адаптеры и реальный шлюз")
    adapters = [
        {"name": "Ethernet", "desc": "Realtek PCIe GbE", "idx": 5,
         "status": "Up", "kind": "physical"},
        {"name": "ZeroTier One", "desc": "ZeroTier One Virtual Port",
         "idx": 21, "status": "Up", "kind": "zerotier"},
        {"name": "Amnezia", "desc": "Wintun Userspace Tunnel", "idx": 9,
         "status": "Up", "kind": "vpn"},
    ]
    defaults = [
        {"ifidx": 9, "nexthop": "10.211.55.2", "metric": 1},
        {"ifidx": 5, "nexthop": "192.168.1.1", "metric": 25},
    ]
    check("classify_adapter: ZeroTier по описанию",
          fixer.classify_adapter("Any", "ZeroTier One Virtual Port") ==
          "zerotier")
    check("classify_adapter: Wintun/Amnezia = vpn",
          fixer.classify_adapter("Amnezia", "Wintun Userspace Tunnel") ==
          "vpn")
    check("classify_adapter: Ethernet = physical",
          fixer.classify_adapter("Ethernet", "Realtek PCIe GbE") ==
          "physical")
    check("real_gateway: шлюз НЕ VPN-адаптера (192.168.1.1, а не 10.211.55.2)",
          fixer.real_gateway(adapters, defaults) == "192.168.1.1")
    check("real_gateway: только VPN → None",
          fixer.real_gateway(adapters, [{"ifidx": 9,
                                         "nexthop": "10.211.55.2",
                                         "metric": 1}]) is None)
    check("zt_iface_index находит ZT-адаптер",
          fixer.zt_iface_index(adapters) == 21)
    check("zt_iface_name находит имя",
          fixer.zt_iface_name(adapters) == "ZeroTier One")

    print("── библиотека адресов (какие IP РЕАЛЬНЫЕ)")
    text = ("host_url http://100.123.45.67:8420\n"
            "подключение от 10.231.146.5:49152\n"
            "мусор: 0.0.0.0 255.255.255.0 127.0.0.1 169.254.1.1 "
            "224.0.0.1 900.1.1.1\n"
            "дубль: 10.231.146.5")
    got = fixer.harvest_ips_from_text(text)
    check("harvest_ips_from_text: только настоящие адреса, по порядку",
          got == ["100.123.45.67", "10.231.146.5"])
    import ipaddress as _ia
    zt_sub = [_ia.ip_network("10.231.146.0/24")]
    check("классификация: адрес в подсети ZT = zt",
          fixer.classify_candidate_ip("10.231.146.5", zt_sub) == "zt")
    check("классификация: адрес VPN-туннеля = vpn (ловушка Amnezia)",
          fixer.classify_candidate_ip("10.8.1.21", zt_sub,
                                      vpn_ips={"10.8.1.21"}) == "vpn")
    check("классификация: свой LAN-адрес = self",
          fixer.classify_candidate_ip("192.168.1.10", zt_sub,
                                      own_ips={"192.168.1.10"}) == "self")
    check("классификация: другая частная сеть = lan",
          fixer.classify_candidate_ip("192.168.1.50", zt_sub) == "lan")
    check("классификация: публичный = public",
          fixer.classify_candidate_ip("91.108.4.1", zt_sub) == "public")
    check("классификация: мусор = junk",
          fixer.classify_candidate_ip("999.1.1.1", zt_sub) == "junk")

    real_probe = fixer.probe_tcp

    def fake_probe_alive(ip: str, port: int, timeout: float = 0.0) -> bool:
        return ip == "10.231.146.4"

    def fake_probe_dead(ip: str, port: int, timeout: float = 0.0) -> bool:
        return False

    fixer.probe_tcp = fake_probe_alive
    try:
        cands = [
            {"ip": "91.108.4.1", "kind": "public", "source": "x"},
            {"ip": "10.8.1.21", "kind": "vpn", "source": "x"},
            {"ip": "10.231.146.4", "kind": "zt", "source": "ZeroTier"},
            {"ip": "192.168.1.50", "kind": "lan", "source": "x"},
        ]
        ranked = fixer.rank_addresses([dict(c) for c in cands], 8420)
        check("ранжирование: отвечающий ZT-адрес первый и best",
              ranked[0]["ip"] == "10.231.146.4" and ranked[0]["best"]
              and ranked[0]["alive"])
        check("ранжирование: VPN-мусор не проверяется (alive=False)",
              all(c["ip"] != "10.8.1.21" or not c["alive"] for c in ranked)
              and ranked[-1]["kind"] == "vpn")
        v = fixer.address_verdict(ranked, 8420)
        check("вердикт называет «вероятнее всего подойдёт» с ZT-адресом",
              "вероятнее всего подойдёт: 10.231.146.4" in v
              and "8420" in v)
        lbl = fixer.address_label(ranked[0], 8420)
        check("метка адреса: IP + тип + «отвечает» + подсказка",
              "10.231.146.4" in lbl and "ZeroTier" in lbl
              and "отвечает" in lbl and "вероятнее всего подойдёт" in lbl)
    finally:
        fixer.probe_tcp = fake_probe_dead
    try:
        dead = [{"ip": "10.231.146.4", "kind": "zt", "source": ""},
                {"ip": "192.168.1.50", "kind": "lan", "source": ""}]
        ranked_dead = fixer.rank_addresses(dead, 8420)
        v2 = fixer.address_verdict(ranked_dead, 8420)
        check("если ничто не отвечает — подсказка с правильным ZT-адресом",
              "не отвечает" in v2 and "10.231.146.4" in v2)
        check("пустая библиотека — честный вердикт",
              fixer.address_verdict([], 8420) ==
              "в библиотеке нет ни одного адреса")
    finally:
        fixer.probe_tcp = real_probe

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "relay_data" / "logs").mkdir(parents=True)
        (root / "relay_data" / "files").mkdir()
        (root / "relay_data" / "settings.json").write_text(
            json.dumps({"host_url": "http://100.123.45.67:8420"}),
            encoding="utf-8")
        (root / "relay_data" / "logs" / "server.log").write_text(
            "2026-09-22 INFO подключение от 10.231.146.5\n"
            "0.0.0.0 255.255.0.0 127.0.0.1 — служебные не берём\n",
            encoding="utf-8")
        (root / "relay_data" / "files" / "big.bin").write_bytes(
            b"\x00\x01192.168.99.99\x00" * 10)
        found = fixer.harvest_known_ips([root / "relay_data"])
        ips_found = [f["ip"] for f in found]
        check("harvest_known_ips: host_url и лог сервера прочитаны",
              "100.123.45.67" in ips_found and "10.231.146.5" in ips_found)
        check("harvest_known_ips: служебные/loopback/маски отфильтрованы",
              "127.0.0.1" not in ips_found)
        check("harvest_known_ips: бинарная папка files пропущена",
              "192.168.99.99" not in ips_found)
        check("harvest_known_ips: источник указан (имя файла)",
              all(f.get("source") for f in found))
        check("library_roots: возвращает только существующие пути",
              all(Path(p).exists() for p in fixer.library_roots()))

    print("── diagnose как подпроцесс (UTF-8, JSON, коды выхода)")
    script = ROOT / "scripts" / "fix_vpn_zt.py"
    r = subprocess.run(
        [sys.executable, str(script), "diagnose", "--json", "--no-file",
         "--no-pause"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(ROOT))
    out = r.stdout or ""
    check("diagnose --json: код выхода 0 или 1 (проблемы — это норма)",
          r.returncode in (0, 1))
    try:
        report = json.loads(out)
        ok_json = True
    except ValueError:
        report = {}
        ok_json = False
    check("diagnose --json: stdout — валидный JSON", ok_json)
    check("в отчёте есть поля checks/verdict/port",
          bool(report) and "checks" in report and "verdict" in report
          and report.get("port") == 8420)
    check("в песочнице ZeroTier нет — скрипт это честно говорит",
          any("ZeroTier" in c.get("title", "") for c in
              report.get("checks", [])))
    check("кракозябр нет (вывод декодируется как UTF-8 и читается)",
          "установи ZeroTier" in out or "ZeroTier" in out)

    r2 = subprocess.run(
        [sys.executable, str(script), "diagnose", "--no-file",
         "--no-pause"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(ROOT))
    human = (r2.stdout or "") + (r2.stderr or "")
    check("человеческий режим: статусы [FAIL]/[ OK ] печатаются",
          "[FAIL]" in human or "[ OK ]" in human)
    check("человеческий режим: есть ИТОГ",
          "ИТОГ:" in human)

    r3 = subprocess.run(
        [sys.executable, str(script), "report",
         "--report", str(ROOT / "relay_data" / "no_such_report.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, cwd=str(ROOT))
    check("report без файла отчёта → код 2 и подсказка",
          r3.returncode == 2 and "diagnose" in (r3.stdout or ""))

    print("── элевация и diagnose-вызовы (lib/net_utils)")
    sys.path.insert(0, str(ROOT))
    from lib.net_utils import build_fix_command, run_vpn_diagnose
    prog, args = build_fix_command(ROOT, "8420-8424")
    check("fix запускается через python + fix_vpn_zt.py",
          "fix_vpn_zt.py" in args and "fix" in args.split())
    check("диапазон брендмауэра передаётся",
          "--fw-ports 8420-8424" in args)
    check("отчёт пишется в relay_data/vpn_fixer_report.json",
          "vpn_fixer_report.json" in args)
    fake_root = ROOT / "tests" / "tmp_no_such_root"
    prog_f, args_f = build_fix_command(fake_root, "8420-8424")
    check("нет скрипта → fallback на .bat-обёртку",
          prog_f.endswith("fix_zerotier_vpn.bat"))

    code, text = run_vpn_diagnose(ROOT, 8420)
    check("run_vpn_diagnose: код 0/1/2 и текст отчёта",
          code in (0, 1, 2) and len(text) > 20)
    code2, text2 = run_vpn_diagnose(fake_root, 8420)
    check("run_vpn_diagnose: нет скрипта → (2, понятная причина)",
          code2 == 2 and "не найден" in text2)

    print("── проводка GUI (без PySide6 — по исходнику)")
    gui = (ROOT / "server_gui.py").read_text(encoding="utf-8")
    check("кнопка открывает диалог _VpnZtDialog",
          "_VpnZtDialog(self, ROOT, port)" in gui)
    check("диагностика в диалоге — через run_vpn_diagnose",
          "run_vpn_diagnose(self._root, self._port)" in gui)
    check("диалог запускается в фоне (_Worker), GUI не морозит",
          "self._worker = _Worker(_job)" in gui)
    check("в диалоге есть «Проверить снова»",
          "Проверить снова" in gui)
    check("в диалоге есть «Применить исправления»",
          "Применить исправления" in gui)
    check("элевация с диапазоном портов от настроек сервера",
          'fw_ports=f"{self._port}-{self._port + 4}"' in gui)
    check("старый молчаливый запуск .bat из кнопки убран",
          "Не удалось запустить fix_zerotier_vpn.bat" not in gui)

    print("── подсказки в консоли/логе и документы")
    smain = (ROOT / "server_main.py").read_text(encoding="utf-8")
    check("server_main: советует diagnose без админа",
          "fix_vpn_zt.py diagnose" in smain)
    rel = (ROOT / "lib" / "relay_server.py").read_text(encoding="utf-8")
    check("relay_server: лог упоминает diagnose и кнопку GUI",
          "fix_vpn_zt.py" in rel and "Починить VPN+ZT" in rel)
    doc = (ROOT / "docs" / "VPN_ZEROTIER_FIX.md").read_text(encoding="utf-8")
    check("docs: описан фиксатор v3.5.11 (библиотека адресов + 7 фиксов)",
          "fix_vpn_zt.py" in doc and "v3.5.11" in doc
          and "Библиотека адресов" in doc)
    # (v3.8.2) README минимализирован, история — в docs/README_full.md.
    readme = (ROOT / "docs" / "README_full.md").read_text(encoding="utf-8")
    check("README_full: есть блок «>>> v3.5.11»",
          ">>> v3.5.11" in readme)
    vjson = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    # (v3.6.3) было `== "3.5.11"` — тест падал на всех новых релизах.
    v_tuple = tuple(int(x) for x in str(vjson.get("version", "0")).split(".")[:3])
    check("version.json не ниже 3.5.11", v_tuple >= (3, 5, 11))

    print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
