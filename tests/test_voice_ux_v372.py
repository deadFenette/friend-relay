#!/usr/bin/env python3
"""v3.7.2 — «voice UX pack»: рация (PTT), «кто говорит» на рейле,
шумодав RNNoise в аудио-ворклете.

Структурные проверки клиентских и серверных правок:
  РАЦИЯ — effectiveMuted() как единственная точка правды, applyMuteState()
  шлёт mute только при изменении (и заново после реконнекта), клавиши
  V/Пробел строго ВНЕ полей ввода (INPUT/TEXTAREA/SELECT/contentEditable,
  без Ctrl/Alt/Meta, без e.repeat), blur гасит залипание, удержание
  кнопки микрофона = рация (мобилки), короткий клик в рации не мьютит.
  ШУМОДАВ — rnnoise.wasm в web_client (валидный, маленький), mime .wasm
  на сервере (application/wasm), sync-компиляция в ворклете (fetch там
  недоступен), кадры 480 @ 48кГц, строго на контексте 48к, байты качает
  main thread (клоном — не transfer), fallback при недоступности.
  КТО ГОВОРИТ — voice.js дёргает railSpeakersChanged из renderVoicePeople
  (в обеих ветках), рейл красит кольца ПОСЛЕ пересборки дока (раньше
  свежесобранный .railme терял класс), стили на токенах тем.

Поведенческая проверка — браузерным E2E
(scripts/web_voice_ux_e2e.py, 16 проверок в Chromium).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


def read(*parts) -> str:
    return (ROOT / Path(*parts)).read_text(encoding="utf-8")


def main() -> int:
    print("== v3.7.2: рация (PTT) + «кто говорит» + шумодав RNNoise ==\n")

    voice = read("web_client", "voice.js")
    worklet = read("web_client", "voice-worklet.js")
    rail = read("web_client", "rail.js")
    style = read("web_client", "style.css")
    index = read("web_client", "index.html")
    http_api = read("lib", "server", "http_api.py")

    # ── 1. Шумодав: файлы и серверная раздача ────────────────────────
    wasm = ROOT / "web_client" / "rnnoise.wasm"
    check("1. web_client/rnnoise.wasm существует и валиден (magic \\0asm)",
          wasm.is_file() and wasm.read_bytes()[:4] == b"\x00asm",
          f"{wasm.stat().st_size if wasm.is_file() else 0} байт")
    check("2. wasm компактный (< 300 КБ — не таскаем мегабайты)",
          wasm.is_file() and wasm.stat().st_size < 300 * 1024)
    check("3. лицензия RNNoise приложена (web_client/RNNOISE_LICENSE)",
          (ROOT / "web_client" / "RNNOISE_LICENSE").is_file())
    check("4. сервер отдаёт .wasm как application/wasm",
          '".wasm": "application/wasm"' in http_api)

    # ── 2. Шумодав: ворклет ──────────────────────────────────────────
    check("5. ворклет: синхронная компиляция wasm (fetch в ворклете нет)",
          "new WebAssembly.Module(" in worklet
          and "new WebAssembly.Instance(" in worklet)
    check("6. ворклет: resize_heap и memcpy импорты предоставлены",
          "copyWithin" in worklet and ".grow(" in worklet)
    check("7. ворклет: кадр RNNoise 480, строго на контексте 48кГц",
          "sampleRate === 48000" in worklet and "480" in worklet)
    check("8. ворклет: шумодав ДО батчинга, копия чанка СРАЗУ "
          "(урок «бурундука» не потерян)",
          "this.rnProcess(ch)" in worklet
          and worklet.count("new Float32Array(den)") == 1)
    check("9. ворклет: честный отчёт о готовности (rnReady) и fallback",
          "rnReady: true" in worklet and "rnFailed" in worklet)

    # ── 3. Шумодав: voice.js ─────────────────────────────────────────
    check("10. voice.js: байты качает main thread и шлёт КЛОНОМ "
          "(transfer отсоединил бы кеш для следующего подключения)",
          "/static/rnnoise.wasm" in voice and "bytes.slice(0)" in voice)
    check("11. voice.js: настройка wr_rn, по умолчанию ВКЛ",
          'voiceSetting("wr_rn") !== "0"' in voice)
    check("12. voice.js: UI честен — rnCheck блокируется без wasm, "
          "метка «готов»",
          "chk.disabled = !VC.rnReady" in voice and "rnState" in voice)

    # ── 4. Рация ─────────────────────────────────────────────────────
    check("13. voice.js: effectiveMuted — единственная точка правды "
          "(рация ? !зажата : мьют)",
          "function effectiveMuted(){\n  return VC.ptt ? !VC.pttHold : VC.muted;\n}"
          in voice)
    check("14. voice.js: applyMuteState шлёт mute только при изменении",
          "if (m === VC.sentMute) return;" in voice)
    check("15. voice.js: после ok моста состояние переотправляется "
          "(sentMute=null — урок v3.5.5 про утечку голоса)",
          "VC.sentMute = null;\n      applyMuteState();" in voice)
    check("16. voice.js: клавиши рации игнорируют e.repeat и модификаторы",
          "e.repeat" in voice and "e.ctrlKey || e.altKey || e.metaKey" in voice)
    check("17. voice.js: рация НЕ срабатывает в полях ввода "
          "(INPUT/TEXTAREA/SELECT/contentEditable)",
          't.tagName === "INPUT"' in voice
          and 't.tagName === "TEXTAREA"' in voice
          and "isContentEditable" in voice)
    check("18. voice.js: клавиши — KeyV и Space с preventDefault "
          "(пробел не скроллит страницу)",
          'e.code === "KeyV"' in voice and 'e.code === "Space"' in voice
          and "e.preventDefault();" in voice)
    check("19. voice.js: blur окна гасит залипшую рацию",
          'window.addEventListener("blur", () => pttUp());' in voice)
    check("20. voice.js: удержание кнопки микрофона = рация (pointer), "
          "короткий клик в рации ничего не делает",
          "pointerdown" in voice and "pointerleave" in voice
          and "if (!VC.on || !VC.ws || VC.ptt) return;" in voice)
    check("21. voice.js: режим сохраняется (wr_ptt) и восстанавливается "
          "при загрузке",
          'voiceSetting("wr_ptt"' in voice
          and "restoreVoiceModes" in voice)
    check("22. voice.js: индикаторы честны в рации — mySpeak и статус "
          "по итоговому мьюту",
          "const effMuted = effectiveMuted();" in voice
          and "рация — зажми V/пробел" in voice)

    # ── 5. Кто говорит ───────────────────────────────────────────────
    check("23. voice.js: renderVoicePeople дёргает рейл в ОБЕИХ ветках "
          "(в т.ч. когда все вышли)",
          voice.count("railSpeakersChanged()") == 2)
    check("24. rail.js: кольца по VC.speak, переключение классов НА МЕСТЕ "
          "(без перестройки списка)",
          "applySpeaking" in rail and 'classList.toggle("spk", on)' in rail)
    check("25. rail.js: applySpeaking ПОСЛЕ пересборки дока "
          "(свежий .railme не теряет кольцо)",
          rail.find("applySpeaking();") > rail.find("dock.appendChild(me);"))
    check("26. rail.js: аватары помечены data-name/data-display",
          'setAttribute("data-name"' in rail
          and 'setAttribute("data-display"' in rail)
    check("27. style.css: кольца на токенах тем (var(--ok)) + reuse "
          "spkRing + уважение prefers-reduced-motion",
          ".railava.spk .railava-face" in style
          and "spkRing" in style.split(".railava.spk")[1][:400]
          and "@media (prefers-reduced-motion: reduce)" in style)
    check("28. index.html: чекбоксы pttCheck и rnCheck на месте",
          'id="pttCheck"' in index and 'id="rnCheck"' in index)

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
