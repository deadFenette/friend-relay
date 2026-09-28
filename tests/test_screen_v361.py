#!/usr/bin/env python3
"""Тесты v3.6.1 — честный диагноз «Экрана» на http:// (secure context).

Суть фикса: «не работает в Edge/Brave» = клиент открыт по http://IP.
На небезопасном контексте ЛЮБОЙ браузер прячет navigator.mediaDevices,
захват разрешён только на https:// или localhost. Теперь:
  - screen.js различает «http:// без https» (баннер с готовой ссылкой)
    и «браузер реально без getDisplayMedia» (совет обновить);
  - баннер screenShowInsecure() ставится автоматически при открытии
    вкладки и убирается при удачном захвате;
  - зритель по http может смотреть как раньше (RTCPeerConnection не
    ограничен secure context).

Проверяем каркас клиента (screen.js/style.css/index.html/version.json)
и живой сервер: /static/screen.js отдаётся уже с фиксом.

Запуск: python tests/test_screen_v361.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer  # noqa: E402
from lib.util import header_encode  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


# ══════════════ 1. screen.js: новые функции и порядок проверок ══════════════
print("— screen.js: фикс secure context —")
js = (ROOT / "web_client" / "screen.js").read_text(encoding="utf-8")

check("есть screenIsInsecure()", "function screenIsInsecure(" in js)
check("есть screenHttpsHref() — сборка https-ссылки",
      "function screenHttpsHref(" in js and '"https://" + host' in js)
check("localhost/127.0.0.1 не считаются небезопасными",
      'host === "localhost" || host === "127.0.0.1"' in js)
check("есть screenShowInsecure() — баннер", "function screenShowInsecure(" in js)
check("баннер вставляется ПЕРВЫМ элементом панели",
      "panel.insertBefore(b, panel.firstChild)" in js)
check("есть screenHideInsecure()", "function screenHideInsecure(" in js)
check("баннер убирается при удачном захвате",
      js.find("screenHideInsecure") < js.find('$("screenPreview").srcObject = stream;'))

# порядок: сначала secure context, потом отсутствие getDisplayMedia
i_insecure = js.find("if (screenIsInsecure())")
i_gdm = js.find("getDisplayMedia){", i_insecure)
check("в startScreenShare сначала http-диагноз, потом getDisplayMedia",
      0 < i_insecure < i_gdm)

check("старое вводящее в заблуждение сообщение убрано",
      "Браузер не умеет захват экрана" not in js)
check("новое сообщение называет браузеры честно",
      "Edge, Brave, Chrome, Opera" in js)
check("баннер показывается автоматически при старте (DOMContentLoaded)",
      "if (screenIsInsecure()) screenShowInsecure();" in js)
check("совет про localhost машины-хоста в баннере",
      "http://localhost:8420" in js)
check("зрителю разрешён просмотр по http (текст в баннере)",
      "Смотреть чужой показ можно и по http" in js
      or "Смотреть чужой показ можно и отсюда" in js)
check("шапка файла описывает secure context (v3.6.1)",
      "БЕЗОПАСНЫЙ КОНТЕКСТ (v3.6.1)" in js)

# ══════════════ 2. style.css: ссылка в баннере читаема ══════════════
print("— style.css —")
css = (ROOT / "web_client" / "style.css").read_text(encoding="utf-8")
check(".warn a стилизован (подчёркнута, переносится)",
      ".warn a{" in css and "word-break" in css)

# ══════════════ 3. index.html: панель не тронута ══════════════
print("— index.html —")
page = (ROOT / "web_client" / "index.html").read_text(encoding="utf-8")
check("панель «Экран» на месте (кнопки/предупреждение)",
      'id="btnScreenShare"' in page and 'id="screenWarn"' in page
      and 'id="tab-screen"' in page)

# ══════════════ 4. version.json ══════════════
print("— version.json —")
ver = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))


def vtuple(s):
    try:
        return tuple(int(x) for x in str(s).split(".")[:3])
    except Exception:
        return (0, 0, 0)


check("version не ниже 3.6.1", vtuple(ver.get("version")) >= (3, 6, 1))
check("в notes объяснён http://-диагноз",
      "http://" in (ver.get("notes") or "")
      and "localhost" in (ver.get("notes") or ""))

# ══════════════ 5. живой сервер: фиксированный screen.js раздаётся ══════════════
print("— живой сервер: /static/screen.js —")
tmp = Path(tempfile.mkdtemp(prefix="screen_v361_"))
relay = RelayServer(tmp, host_name="Хост", access_key="", max_file_size=1024 * 1024)
port = 18589
assert relay.start("127.0.0.1", port), "сервер не поднялся"
try:
    r = urllib.request.Request(f"http://127.0.0.1:{port}/static/screen.js")
    with urllib.request.urlopen(r, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    check("/static/screen.js содержит фикс v3.6.1",
          "screenIsInsecure" in body and "screenShowInsecure" in body)
    r = urllib.request.Request(f"http://127.0.0.1:{port}/")
    with urllib.request.urlopen(r, timeout=5) as resp:
        page = resp.read().decode("utf-8")
    check("вкладка «Экран» на странице",
          'data-tab="screen"' in page and 'id="tab-screen"' in page)
finally:
    relay.stop()

# ══════════════ итог ══════════════
print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
