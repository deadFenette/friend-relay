#!/usr/bin/env python3
"""Тесты v3.6.3 — фиксы по итогам дебаг-прохода v3.6.2.

Пять найденных мест, все чинятся здесь и проверяются здесь:
  1) lib/updater.py: цикл ожидания выхода exe ждал несуществующий
     фильтр `tasklist /fi "pid %~1"` (правильно `PID eq %~1`) —
     обновление молча срывалось на медленном выходе + возможен второй
     инстанс. Дополнительно: потолок 60с + добивание зависшего
     (taskkill только если pid всё ещё наш образ) и пауза через ping
     вместо timeout (timeout требует рабочий stdin).
  2) lib/updater.py: временный .bat писался UTF-8 без chcp 65001 —
     на русской Windows cmd читает cp866, кириллические пути ломались.
  3) screen/hub.py: зритель, закрывший вкладку без «bye», вечно висел
     в счётчике «смотрят». Теперь watch(on=true) — ещё и пульс
     (клиент повторяет раз в 12с), хаб чистит молчунов >WATCHER_STALE_S.
  4) web_client/screen.js: screenViewerAnswer не закрывал прежний
     RTCPeerConnection перед новым offer (утечка при рестартах);
     + сам пульс зрителя (SCREEN_WATCH_BEAT_MS) в screenTick.
  5) tests: больше не хардкодят номер версии — проверка «не ниже».

Запуск: python tests/test_fix_v363.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer  # noqa: E402
from lib import updater  # noqa: E402
from screen import config as scfg  # noqa: E402
from screen.hub import ScreenHub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


# ══════════════ 1. updater.py: фильтр + кодировка + потолок ожидания ══════════════
print("— updater.py: helper .bat (функционально) —")
tmp = Path(tempfile.mkdtemp(prefix="fix_v363_"))
existing = tmp / "друг_relay.exe"   # кириллица — специально
new = tmp / "друг_relay.new.exe"
helper = tmp / "helper.bat"
updater._write_windows_helper_bat(existing, new, helper)
raw = helper.read_bytes()

check("helper: UTF-8 без BOM", not raw.startswith(b"\xef\xbb\xbf"))
check("helper: CRLF-окончания", b"\r\n" in raw and raw.replace(b"\r\n", b"\n").count(b"\n") == raw.count(b"\n"))
head = raw[: len(b"@echo off\r\nchcp 65001 >nul\r\n")]
check("helper: chcp 65001 первой командой (cmd читает cp866!)",
      head == b"@echo off\r\nchcp 65001 >nul\r\n")
check("helper: фильтр PID eq (было битое 'pid %~1')",
      b'tasklist /fi "PID eq %~1"' in raw)
check("helper: битый фильтр 'pid %~1' исчез",
      b'"pid %~1"' not in raw)
check("helper: ожидание ограничено (WR_TRIES geq 60)",
      b"WR_TRIES" in raw and b"geq 60" in raw)
check("helper: taskkill только под защитой образа (find /i)",
      b"taskkill /f /pid %~1" in raw
      and b'find /i "' + existing.name.encode("utf-8") + b'"' in raw)
check("helper: пауза ping'ом, timeout'а больше нет",
      b"ping -n 2 127.0.0.1 >nul" in raw and b"timeout /t" not in raw)
check("helper: пауза перед проверкой старта осталась (~8с)",
      b"ping -n 9 127.0.0.1 >nul" in raw)
check("helper: кириллический путь дошёл как есть",
      existing.name.encode("utf-8") in raw)
check("helper: бэкап и откат на месте",
      b"copy /y %OLD% %BAK%" in raw and b"copy /y %BAK% %OLD%" in raw)

# ══════════════ 2. screen: пульс зрителя и чистка молчунов ══════════════
print("— screen/hub.py: счётчик зрителей —")
check("config: WATCHER_STALE_S = 40", scfg.WATCHER_STALE_S == 40.0)

hub = ScreenHub()
check("publish", hub.publish("Вася", "игра") is True)
check("watch: зритель подключился", hub.watch("Петя", "Вася", True) is True)
snap = hub.snapshot()
check("snapshot: зритель в счётчике", snap[0]["viewers"] == ["Петя"])

# вкладка умерла без «bye»: пульсов нет, отметка протухла
hub._streams["Вася"].viewers["Петя"] = time.time() - scfg.WATCHER_STALE_S - 5
snap = hub.snapshot()
check("молчун (вкладка без bye) исчез из счётчика", snap[0]["viewers"] == [])
check("сам показ жив — чистка зрителя не трогает реестр",
      len(snap) == 1 and snap[0]["from"] == "Вася")

check("пульс watch(on=true) возвращает зрителя",
      hub.watch("Петя", "Вася", True) is True
      and hub.snapshot()[0]["viewers"] == ["Петя"])
check("watch(on=false) по-прежнему убирает",
      hub.watch("Петя", "Вася", False) is True
      and hub.snapshot()[0]["viewers"] == [])

print("— relay-уровень: get_screen_info —")
tmp2 = Path(tempfile.mkdtemp(prefix="fix_v363_relay_"))
relay = RelayServer(tmp2, host_name="Хост", access_key="", max_file_size=1024 * 1024)
assert relay.start("127.0.0.1", 18636), "сервер не поднялся"
try:
    relay._screen.publish("Вася", "игра")
    relay._screen.watch("Петя", "Вася", True)
    info = relay.get_screen_info()
    check("/screen/info: зритель виден",
          info["streams"][0]["viewers"] == ["Петя"])
    relay._screen._streams["Вася"].viewers["Петя"] = time.time() - 100
    info = relay.get_screen_info()
    check("/screen/info: молчун исчез (вкладка без bye)",
          info["streams"][0]["viewers"] == [])
finally:
    relay.stop()

# ══════════════ 3. screen.js: закрытие прежнего pc + пульс ══════════════
print("— web_client/screen.js —")
js = (ROOT / "web_client" / "screen.js").read_text(encoding="utf-8")
check("константа пульса SCREEN_WATCH_BEAT_MS", "SCREEN_WATCH_BEAT_MS" in js)
check("пульс в screenTick (watch on=true раз в 12с)",
      "пульс счётчика зрителей" in js
      and "now - (SC.watchBeat || 0) > SCREEN_WATCH_BEAT_MS" in js)
check("watchBeat инициализирован в SC", "watchBeat: 0" in js)

# тело screenViewerAnswer: close ПЕРЕД new RTCPeerConnection
fn_start = js.index("function screenViewerAnswer")
fn_end = js.index("\nfunction ", fn_start + 10)
body = js[fn_start:fn_end]
close_at = body.find("SC.viewPc.close()")
new_at = body.find("new RTCPeerConnection")
check("повторный offer закрывает прежний viewPc", close_at != -1 and new_at != -1)
check("закрытие стоит ДО создания нового pc", close_at < new_at)
check("после закрытия viewPc обнуляется",
      "SC.viewPc = null" in body[close_at:new_at])
# пульс ставится и при подключении
check("watchBeat обновляется при connected",
      "SC.watchBeat = Date.now()" in js)

# синтаксис JS
r = subprocess.run(["node", "--check", str(ROOT / "web_client" / "screen.js")],
                   capture_output=True, text=True)
check("node --check screen.js", r.returncode == 0)

# ══════════════ 4. тесты без хардкода версии ══════════════
print("— тесты: version-чек «не ниже» —")
t362 = (ROOT / "tests" / "test_screen_v362.py").read_text(encoding="utf-8")
t3511 = (ROOT / "tests" / "test_vpn_fixer_v3511.py").read_text(encoding="utf-8")
check("test_screen_v362: больше не `== \"3.6.2\"` в коде чека",
      'ver.get("version") == "3.6.2"' not in t362
      and 'check("version = 3.6.2"' not in t362)
check("test_screen_v362: проверка «не ниже 3.6.2»", "не ниже 3.6.2" in t362)
check("test_vpn_fixer_v3511: больше не `== \"3.5.11\"`",
      'vjson.get("version") == "3.5.11"' not in t3511)
check("test_vpn_fixer_v3511: проверка «не ниже 3.5.11»",
      "не ниже 3.5.11" in t3511)

# ══════════════ 5. version.json / README ══════════════
print("— version.json / README —")
ver = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
vt = tuple(int(x) for x in str(ver.get("version", "0")).split(".")[:3])
check("version не ниже 3.6.3", vt >= (3, 6, 3))
# (v3.6.4) Блок 3.6.3 должен ПРИСУТСТВОВАТЬ в notes, но не обязан быть
# первым: новые релизы дописывают свои заметки СВЕРХУ (тот же принцип,
# что и «не ниже» для номера версии — тесты переживают бампы).
# (v3.6.6) Окно [:4000] убрано: заметки накапливаются сверху, и за три
# релиза блок 3.6.3 законно уехал глубже 4000-го символа. Инвариант —
# «блок есть в истории заметок», позиция не важна.
check("notes содержат блок v3.6.3",
      "v3.6.3" in str(ver.get("notes", "")))
readme = (ROOT / "README.md").read_text(encoding="utf-8")
check("README: блок «>>> v3.6.3»", ">>> v3.6.3" in readme)
check("README: предыдущие блоки не тронуты",
      ">>> v3.6.2" in readme and ">>> v3.6.1" in readme and ">>> v3.5.11" in readme)

print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
