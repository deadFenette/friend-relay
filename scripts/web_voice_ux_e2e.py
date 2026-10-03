#!/usr/bin/env python3
"""E2E v3.7.2 «voice UX pack» в настоящем Chromium (Playwright).

Три фичи проверяются против РЕАЛЬНОГО сервера (RelayServer + микшер +
мост) и РЕАЛЬНОГО web_client:

  РАЦИЯ (PTT):
    1. включение #pttCheck: итоговый мьют сразу «закрыт» (мост знает);
    2. УДЕРЖАНИЕ V на body: собеседник РЕАЛЬНО слышит (не пустые кадры);
    3. отпускание V: снова тишина у собеседника;
    4. «v»/Пробел ВНУТРИ поля ввода НЕ открывают микрофон (не мешают
       печатать и вставлять Ctrl+V);
    5. Пробел на body работает как рация;
    6. удержание кнопки микрофона (pointer) = рация;
    7. blur окна гасит залипшую рацию.

  КТО ГОВОРИТ (кольца):
    8. Боб говорит → у Алисы кольцо на панели «В канале» И на аватаре
       в левом рейле (настоящие spk/spk_all от микшера);
    9. Боб закрыл рацию → кольцо гаснет;
   10. проводка UI: синтетический spk мгновенно красит рейл и «свой»
       док (.railme.spk через VC.mySpeak).

  ШУМОДАВ RNNoise:
   11. wasm долетел до ворклета через HTTP и скомпилировался
       (VC.rnReady), чекбокс разблокирован, метка «· готов»;
   12. изолированный замер в живом AudioContext: белый шум через
       vc-cap БЕЗ шумодава проходит почти целиком (ratio > 0.7),
       С шумодавом — давится (ratio < 0.4).

Запуск: python scripts/web_voice_ux_e2e.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.relay_server import RelayServer  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


# Зонд плейаута Алисы: RMS каждого смешанного кадра от микшера.
PROBE_PLAY = """
() => {
  if (!VC.playPort || VC.__probe) return;
  VC.__probe = { frames: [], orig: VC.playPort.postMessage.bind(VC.playPort) };
  VC.playPort.postMessage = (data, transfer) => {
    try {
      if (data instanceof ArrayBuffer){
        const f = new Float32Array(data);
        let acc = 0;
        for (let i = 0; i < f.length; i++) acc += f[i] * f[i];
        VC.__probe.frames.push(Math.sqrt(acc / f.length) * 32768);
      }
    } catch (e) {}
    VC.__probe.orig(data, transfer);
  };
}
"""

CONNECT = """
async (name) => {
  document.querySelector('#name').focus();
}
"""


def connect_and_join_voice(page, name: str, port: int) -> None:
    page.goto(f"https://127.0.0.1:{port}/", wait_until="domcontentloaded")
    page.fill("#name", name)
    page.click("#advKeys summary")   # v3.7.1: ключи в спойлере
    page.fill("#key", "k")
    page.click("#btnConnect")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)
    page.evaluate(
        """() => document.querySelector('.tabs button[data-tab="voice"]')
             .click()""")
    page.wait_for_function("() => !!VC.wsPath", timeout=10000)
    page.click("#btnVoice")
    page.wait_for_function("() => VC.on === true && VC.everConnected === true",
                           timeout=15000)
    page.wait_for_function("() => !!VC.playPort", timeout=15000)


def inject_loud_tone(page) -> None:
    """Фейк-мик Chromium даёт тихие бипы (RMS ~150-250 в int16) — НИЖЕ
    порога VAD микшера (300): молчание гейтится, и реальные spk-события
    не приходят. Подмешиваем в трак захвата громкий синус 440Гц (0.5) —
    тот же путь, что у настоящего голоса, но заведомо выше порога."""
    page.wait_for_function("() => VC.rnReady === true", timeout=15000)
    # шумодав выключаем: RNNoise съест чистый тон как «не-речь»
    page.click("#rnCheck")
    page.wait_for_function("() => VC.rnWanted === false", timeout=5000)
    page.evaluate(
        """() => {
             const ctx = VC.ctx;
             const osc = ctx.createOscillator();
             osc.frequency.value = 440;
             const g = ctx.createGain(); g.gain.value = 0.5;
             osc.connect(g); g.connect(VC.capNode);
             osc.start();
             VC.__tone = osc; VC.__toneGain = g;
           }""")
    page.wait_for_function("() => VC.mySpeak === true", timeout=8000)


def ring_is_on(page, name: str) -> bool:
    """Кольцо «говорит» на панели «В канале» И на рейл-аватаре."""
    return page.evaluate(
        """(name) => {
             const row = [...document.querySelectorAll('#voicePeople .vperson.spk')]
               .some(r => r.textContent.includes(name));
             const rail = !!document.querySelector(
               '.railava[data-name=' + JSON.stringify(name) + '].spk');
             return row && rail;
           }""", name)


def wait_ring(page, name: str, want: bool, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if ring_is_on(page, name) == want:
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    print("== E2E v3.7.2: рация (PTT) + «кто говорит» + шумодав RNNoise ==\n")
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="wr_voice_ux_e2e_"))
    base = 18770
    relay = RelayServer(tmp, host_name="Хост", access_key="k",
                        max_file_size=1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("  [FAIL] сервер не стартовал (порт занят?)")
        return 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ])
            try:
                ctx_a = browser.new_context(ignore_https_errors=True)
                ctx_b = browser.new_context(ignore_https_errors=True)
                a = ctx_a.new_page()
                b = ctx_b.new_page()
                errs: list[str] = []
                for pg in (a, b):
                    pg.on("pageerror", lambda e: errs.append(str(e)))

                print("  -- подключение Алисы и Боба --")
                connect_and_join_voice(a, "Алиса", base)
                connect_and_join_voice(b, "Боб", base)
                check("0. оба в голосовом канале",
                      a.evaluate("() => VC.on") and b.evaluate("() => VC.on"))

                # проводка UI — ДО подачи громкого тона: фейк-мик тише
                # VAD-порога, реальные spk-события не текут и не мешают.
                # Рейл узнаёт об онлайне из поллинга (1.5с) + тик (2с) —
                # ждём появления аватара Боба, иначе кольцо вешать не на что.
                print("  -- проводка колец (синтетика, до реального звука) --")
                a.wait_for_selector('.railava[data-name="Боб"]', timeout=10000)
                a.evaluate(
                    """() => { VC.speak = {'Боб': true}; renderVoicePeople(); }""")
                synthetic = ring_is_on(a, "Боб")
                a.evaluate("""() => { VC.mySpeak = true; renderVoicePeople(); }""")
                me_ring = a.evaluate(
                    """() => !!document.querySelector('.railme.spk')""")
                a.evaluate(
                    """() => { VC.speak = {}; VC.mySpeak = false;
                              renderVoicePeople(); }""")
                cleared = not ring_is_on(a, "Боб") and not a.evaluate(
                    """() => !!document.querySelector('.railme.spk')""")
                check("1. проводка UI: синтетический spk красит рейл и свой док",
                      synthetic and me_ring and cleared,
                      f"synthetic={synthetic} me={me_ring} cleared={cleared}")

                # громкий тон в оба тракта (фейк-мик ниже VAD-порога микшера)
                inject_loud_tone(a)
                inject_loud_tone(b)

                # ── КТО ГОВОРИТ (реальный микшер, оба с открытым микрофоном)
                print("  -- кольца «кто говорит» (реальные spk от микшера) --")
                check("2. Боб говорит → у Алисы кольцо на панели И на рейле",
                      wait_ring(a, "Боб", True, 12))
                check("3. Алиса говорит → у Боба кольцо (обратное направление)",
                      wait_ring(b, "Алиса", True, 12))

                # ── РАЦИЯ (PTT)
                print("  -- рация (PTT) --")
                b.click("#pttCheck")
                b.wait_for_function("() => VC.ptt === true", timeout=3000)
                time.sleep(0.7)   # дать mute доехать до микшера
                check("4. PTT включён: мост сразу знает «закрыто» (sentMute)",
                      b.evaluate("() => VC.sentMute === true && !VC.muted"))
                check("5. кольцо Боба у Алисы ГАСНЕТ в рации (мост глушит)",
                      wait_ring(a, "Боб", False, 8))

                # зонд Алисы ДО удержания
                a.evaluate(PROBE_PLAY)

                # фокус вне полей ввода
                b.evaluate("() => { const el = document.activeElement;"
                           "  if (el && el.blur) el.blur(); }")
                b.keyboard.down("v")
                b.wait_for_function("() => VC.pttHold === true", timeout=3000)
                b.wait_for_function("() => VC.sentMute === false", timeout=3000)
                time.sleep(2.5)   # Боб «говорит», зажав V
                fa_hold = a.evaluate("() => VC.__probe ? VC.__probe.frames : []")
                a.evaluate("() => { if (VC.__probe) VC.__probe.frames = []; }")
                live_hold = sum(1 for r in fa_hold if r > 120)
                check("6. удержание V: Алиса РЕАЛЬНО слышит Боба (живые кадры)",
                      live_hold >= 10, f"живых={live_hold}/{len(fa_hold)}")

                b.keyboard.up("v")
                b.wait_for_function("() => VC.pttHold === false", timeout=3000)
                # 1с на промывку тракта: кадры B, уже стоявшие в очередях
                # моста/микшера и джиттер-буфере A, дорабатывают (законный
                # хвост ~140мс + запас) — как в проверке мьюта старого E2E
                time.sleep(1.0)
                a.evaluate("() => { if (VC.__probe) VC.__probe.frames = []; }")
                time.sleep(2.5)
                fa_rel = a.evaluate("() => VC.__probe.frames")
                a.evaluate("() => { if (VC.__probe) VC.__probe.frames = []; }")
                live_rel = sum(1 for r in fa_rel if r > 120)
                check("7. отпустил V: у Алисы снова тишина",
                      live_rel == 0, f"живых={live_rel}/{len(fa_rel)}")

                # «v» и Пробел ВНУТРИ поля ввода не открывают рацию
                b.evaluate(
                    """() => {
                         const t = document.querySelector('.tabs button[data-tab="chat"]');
                         if (t) t.click();
                       }""")
                b.focus("#msgText")
                b.keyboard.type("привет v ")
                b.keyboard.down(" ")
                in_input = b.evaluate(
                    "() => VC.sentMute === true && VC.pttHold === false")
                b.keyboard.up(" ")
                typed = b.evaluate(
                    "() => document.getElementById('msgText').value")
                check("8. печать «v» и пробел в поле ввода НЕ открывают рацию",
                      in_input and "привет v" in typed, repr(typed[:20]))

                # Пробел на body — тоже рация
                b.evaluate("() => { const el = document.activeElement;"
                           "  if (el && el.blur) el.blur(); }")
                b.keyboard.down(" ")
                space_ok = b.evaluate(
                    "() => VC.pttHold === true && VC.sentMute === false")
                b.keyboard.up(" ")
                time.sleep(0.3)
                space_ok = space_ok and b.evaluate(
                    "() => VC.pttHold === false && VC.sentMute === true")
                check("9. Пробел на body работает как рация (нажал/отпустил)",
                      space_ok)

                # удержание КНОПКИ микрофона = рация (мобилки/мышь)
                b.evaluate(
                    """() => document.getElementById('btnVoiceMute')
                         .dispatchEvent(new PointerEvent('pointerdown',
                           {bubbles: true}))""")
                btn_down = b.evaluate("() => VC.pttHold && !VC.sentMute")
                b.evaluate(
                    """() => document.getElementById('btnVoiceMute')
                         .dispatchEvent(new PointerEvent('pointerup',
                           {bubbles: true}))""")
                btn_up = b.evaluate("() => !VC.pttHold && VC.sentMute")
                check("10. удержание кнопки микрофона = рация",
                      btn_down and btn_up)

                # залипшая клавиша: blur окна глушит
                b.keyboard.down("v")
                b.wait_for_function("() => VC.pttHold === true", timeout=3000)
                b.evaluate("() => window.dispatchEvent(new Event('blur'))")
                blur_ok = b.evaluate(
                    "() => VC.pttHold === false && VC.sentMute === true")
                b.keyboard.up("v")   # не оставить зажатие в playwright
                check("11. blur окна гасит залипшую рацию (не вещаем бесконечно)",
                      blur_ok)

                # ── ШУМОДАВ RNNoise
                print("  -- шумодав RNNoise --")
                rn_ready = a.evaluate("() => VC.rnReady") and \
                    b.evaluate("() => VC.rnReady")
                ui_state = a.evaluate(
                    """() => {
                         const c = document.getElementById('rnCheck');
                         const s = document.getElementById('rnState');
                         return !!c && !c.disabled
                           && s && s.textContent.includes('готов');
                       }""")
                check("12. wasm долетел до ворклета и собрался (VC.rnReady, UI «готов»)",
                      rn_ready and ui_state)

                # изолированный замер: белый шум через vc-cap, off vs on
                measure = a.evaluate("""
                  async () => {
                    const N = 48000 * 3;
                    const noiseBuf = (ctx) => {
                      const buf = ctx.createBuffer(1, N, 48000);
                      const d = buf.getChannelData(0);
                      let s = 12345;
                      for (let i = 0; i < N; i++){
                        s = (s * 1103515245 + 12345) & 0x7fffffff;
                        d[i] = (s / 0x3fffffff - 1) * 0.25;
                      }
                      return buf;
                    };
                    const rms = (f) => {
                      let acc = 0;
                      for (let i = 0; i < f.length; i++) acc += f[i] * f[i];
                      return Math.sqrt(acc / f.length);
                    };
                    async function run(rn){
                      const ctx = new AudioContext({sampleRate: 48000});
                      if (ctx.state === "suspended"){
                        try { await ctx.resume(); } catch(e){}
                      }
                      await ctx.audioWorklet.addModule("/static/voice-worklet.js");
                      const cap = new AudioWorkletNode(ctx, "vc-cap",
                        {numberOfInputs:1, numberOfOutputs:1,
                         outputChannelCount:[1]});
                      let ready = !rn;
                      const frames = [];
                      cap.port.onmessage = (e) => {
                        /* VcCap постит Float32Array (с трансфером буфера) —
                           на приёме это НОВЫЙ Float32Array, не ArrayBuffer */
                        if (e.data instanceof ArrayBuffer)
                          frames.push(new Float32Array(e.data));
                        else if (e.data && e.data.length)
                          frames.push(e.data);
                        else if (e.data && e.data.rnReady) ready = true;
                      };
                      const sink = ctx.createGain(); sink.gain.value = 0;
                      cap.connect(sink); sink.connect(ctx.destination);
                      if (rn){
                        const bytes = await (await fetch("/static/rnnoise.wasm"))
                          .arrayBuffer();
                        cap.port.postMessage({rn: true});
                        cap.port.postMessage({wasm: bytes});
                        for (let i = 0; i < 100 && !ready; i++)
                          await new Promise(r => setTimeout(r, 50));
                        if (!ready){
                          ctx.close();
                          return {err: "wasm не собрался в изолированном узле"};
                        }
                      }
                      const src = ctx.createBufferSource();
                      src.buffer = noiseBuf(ctx);
                      src.loop = true;
                      src.connect(cap);
                      src.start();
                      await new Promise(r => setTimeout(r, 3200));
                      src.stop();
                      const state = ctx.state;
                      /* Батчи каппера НЕ всегда 960: в bypass — 1024 (960+хвост
                         квантов 128). Считаем RMS по ВСЕМ кадрам после прогрева. */
                      const out = frames.slice(2)
                        .filter(f => f.length > 0).map(rms);
                      ctx.close();
                      if (!out.length)
                        return {err: "ворклет не дал кадров (state="
                              + state + ", всего " + frames.length + ")"};
                      return {rms: out.reduce((x, y) => x + y, 0) / out.length};
                    }
                    const off = await run(false);
                    const on = await run(true);
                    if (off.err || on.err) return {err: off.err || on.err};
                    return {ratio: on.rms / off.rms, off: off.rms, on: on.rms};
                  }
                """)
                if measure is None:
                    check("13. RNNoise давит белый шум (ratio on/off)", False,
                          "замер не вернул данных")
                elif "err" in measure:
                    check("13. RNNoise давит белый шум (ratio on/off)", False,
                          measure["err"])
                else:
                    check("13. RNNoise давит белый шум: ratio(on/off) < 0.4",
                          measure["ratio"] < 0.4,
                          f"ratio={measure['ratio']:.3f} "
                          f"off={measure['off']:.3f} on={measure['on']:.3f}")
                    check("14. без шумодава сигнал проходит честно "
                          "(bypass ratio > 0.7 от теоретического 0.144)",
                          0.10 < measure["off"] < 0.20,
                          f"off_rms={measure['off']:.3f}")

                check("15. на страницах нет JS-ошибок", not errs,
                      "; ".join(errs)[:120])
            finally:
                browser.close()
    finally:
        relay.stop()

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
