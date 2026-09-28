#!/usr/bin/env python3
"""Тесты v3.6.2 — качество и стабильность вкладки «Экран».

Что добавлено:
  - профили качества SCREEN_MODES (motion/detail/max) + опция «Максимум»
    (60 кадров / 5 Мбит) для LAN/ZeroTier;
  - адаптив по getStats (screenAdaptTick): потери >4% или RTT >350мс —
    шаг вниз по SCREEN_LADDER (битрейт ×0.6/×0.35, scaleResolutionDownBy),
    20с чисто — шаг вверх; таймер стартует с показом, гаснет со стопом;
  - мягкий «disconnected» у ведущего (5с на самовосстановление);
  - авто-реконнект зрителя screenViewerRetry (2 попытки, антидребезг
    через SC.restartAt, сброс счётчика при connected и новом просмотре);
  - костыль звука для Firefox/Safari: screenOfferAudioFix — догрузка
    «Стерео микшер»/VB-Cable как микрофона (echoCancellation:false и
    т.д.), уже смотрящим уходит свежий offer;
  - предупреждение при активном голосовом чате (VC.on) перед показом.

Запуск: python tests/test_screen_v362.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


# ══════════════ 1. screen.js: профили качества ══════════════
print("— screen.js: профили качества —")
js = (ROOT / "web_client" / "screen.js").read_text(encoding="utf-8")
idx = (ROOT / "web_client" / "index.html").read_text(encoding="utf-8")

check("SCREEN_MODES: motion/detail/max",
      all(k in js for k in ("motion:", "detail:", 'max:    ')))
check("максимум = 60 кадров / 5 Мбит",
      'max:    { fps: 60, bitrate: 5000000' in js)
check("screenMode() читает выбранный профиль",
      "function screenMode()" in js)
check("index.html: опция «Максимум» в селекте",
      '<option value="max">' in idx and "60 кадров" in idx)
check("contentHint при смене качества — из screenMode()",
      "vt.contentHint = screenMode().hint" in js)

# ══════════════ 2. адаптив по getStats ══════════════
print("— screen.js: адаптив —")
check("лестница деградации 3 ступени (1.0/0.6/0.35)",
      "SCREEN_LADDER = [" in js and "{ k: 0.6,  scale: 1.5 }" in js
      and "{ k: 0.35, scale: 2 }" in js)
check("screenAdaptTick читает outbound-rtp / remote-inbound-rtp / candidate-pair",
      '"outbound-rtp"' in js and '"remote-inbound-rtp"' in js
      and '"candidate-pair"' in js)
check("пороги: потери 4%, RTT 350мс",
      "lossRate > 0.04" in js and "rtt > 0.35" in js)
check("восстановление: 20с чисто — шаг вверх",
      "now - a.cleanSince > 20000" in js)
check("screenApplyStep меняет maxBitrate и scaleResolutionDownBy",
      "function screenApplyStep(" in js
      and "scaleResolutionDownBy" in js)
check("таймер адаптива стартует с показом",
      "SC.adaptTimer = setInterval(() => { screenAdaptTick()" in js)
check("и гаснет со стопом (+ очистка adapt и аудио-костыля)",
      "if (SC.adaptTimer){ clearInterval(SC.adaptTimer)" in js
      and "SC.adapt = {}; screenHideAudioFix();" in js)
check("метрики зрителя чистятся при закрытии pc",
      "delete SC.adapt[viewer]" in js)
check("disconnected даёт 5с на самовосстановление",
      "SCREEN_DISC_GRACE_MS" in js and 'st === "disconnected"' in js)

# ══════════════ 3. авто-реконнект зрителя ══════════════
print("— screen.js: авто-реконнект —")
check("screenViewerRetry существует",
      "function screenViewerRetry(" in js)
check("лимит авто-попыток и повторный hello",
      "SCREEN_RETRY_MAX" in js and '{type: "hello"}' in js)
check("антидребезг через SC.restartAt",
      "Date.now() < SC.restartAt" in js)
check("счётчик сбрасывается при connected",
      'SC.retries = 0; clearTimeout(SC.retryTimer);   // доехали' in js)
check("и при новом просмотре/остановке",
      js.count("clearTimeout(SC.retryTimer)") >= 3)
check("failed и хендшейк ведут в retry",
      js.count("screenViewerRetry(") >= 3)

# ══════════════ 4. костыль звука Firefox/Safari ══════════════
print("— screen.js: костыль звука —")
check("screenOfferAudioFix предлагается при показе без аудио",
      "function screenOfferAudioFix(" in js
      and js.find("screenOfferAudioFix();") > js.find("SC.stream = stream"))
check("микрофон без обработки (кабель — чистый микс)",
      "echoCancellation: false" in js and "noiseSuppression: false" in js
      and "autoGainControl: false" in js)
check("дорожка дописывается в текущий показ",
      "SC.stream.addTrack(at)" in js)
check("уже смотрящим уходит свежий offer",
      "for (const name of Object.keys(SC.pcs)) screenSharerHello(name);" in js)
check("подсказка про mmsys.cpl / Стерео микшер / VB-Cable",
      "mmsys.cpl" in js and "Стерео микшер" in js and "VB-Cable" in js)
check("костыль убирается при остановке показа",
      "function screenHideAudioFix(" in js)

# ══════════════ 5. голосовой чат + показ ══════════════
print("— screen.js: голос + экран —")
check("предупреждение при активном голосовом чате (VC.on)",
      'typeof VC !== "undefined" && VC.on' in js
      and "Голосовой чат активен" in js)
check("совет про наушники против двойного звука",
      "услышат голос дважды" in js)

# ══════════════ 6. index.html: подсказка обновлена ══════════════
print("— index.html —")
check("подсказка упоминает обходной путь и самоснижение качества",
      "догрузить его через «Стерео микшер»" in idx
      and "качество снижается" in idx)

# ══════════════ 7. version.json ══════════════
print("— version.json —")
ver = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
# (v3.6.3) было `== "3.6.2"` — тест ломался на каждой новой версии;
# проверяем «не ниже», как в test_screen_v361.py.
vtuple362 = tuple(int(x) for x in str(ver.get("version", "0")).split(".")[:3])
check("version не ниже 3.6.2", vtuple362 >= (3, 6, 2))

# ══════════════ 8. живой сервер: v3.6.2 раздаётся ══════════════
print("— живой сервер —")
tmp = Path(tempfile.mkdtemp(prefix="screen_v362_"))
relay = RelayServer(tmp, host_name="Хост", access_key="", max_file_size=1024 * 1024)
assert relay.start("127.0.0.1", 18591), "сервер не поднялся"
try:
    r = urllib.request.Request("http://127.0.0.1:18591/static/screen.js")
    with urllib.request.urlopen(r, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    check("/static/screen.js несёт v3.6.2 (адаптив + retry + костыль)",
          "screenAdaptTick" in body and "screenViewerRetry" in body
          and "screenOfferAudioFix" in body)
finally:
    relay.stop()

# ══════════════ итог ══════════════
print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
