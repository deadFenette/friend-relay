#!/usr/bin/env python3
"""E2E-проверка КАЧЕСТВА ПЛЕЙАУТА (v3.7.0) в настоящем Chromium.

Симптом пользователя: «звук прерывистый, как старый скайп — как будто
интеграл». Причины, найденные в v3.6.9 и ниже:
  1) даунлинк веба шёл через TLS-сокет HTTP-сервера С NAGLE (мелкие
     20мс-кадры задерживались ядром до ACK → всплески вместо потока);
  2) после каждого недобора плейаут ждал ПОЛНОГО наполнения буфера
     (80-360мс ТИШИНЫ на каждый затык сети);
  3) PLC не было: микрозазор = резко проглоченный кусок речи.

Здесь проверяется плейаут (web_client/voice-worklet.js, класс VcPlay)
КАК ЕСТЬ, в реальном AudioWorklet, на искусственном джиттере:

  фаза 1 — ровная подача 3с:          провалов быть не должно вообще;
  фаза 2 — три затыка по 120мс:       PLC + быстрый рестарт должны
                                      закрыть почти всё (было ~200мс
                                      тишины на каждый);
  фаза 3 — два затыка по 200мс:       пауза вдвое короче прежней и
                                      короче полного буфера;
  фаза 4 — всплеск 600мс + пауза 3с:  дрен на тишине обязан опустить
                                      задержку до ~200мс (не держать
                                      «стену прошлого»).

Метрики снимаются ScriptProcessor-зондом (пик на каждый вектор 1024
сэмпла) — «слышимая тишина» = подряд идущие векторы с пиком < 1e-3.

Запуск: python scripts/web_playout_quality_e2e.py
(нужен playwright с chromium)
"""
from __future__ import annotations

import http.server
import sys
import threading
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


class _Handler(http.server.BaseHTTPRequestHandler):
    """Мини-сервер: страница-песочница + /static/voice-worklet.js."""

    def _page(self):
        body = (b"<!doctype html><html><body>playout probe</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _worklet(self):
        src = (ROOT / "web_client" / "voice-worklet.js").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(src)))
        self.end_headers()
        self.wfile.write(src)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/static/voice-worklet.js"):
            self._worklet()
        else:
            self._page()

    def log_message(self, *a):  # тишина в консоли
        pass


SCENARIO = r"""
window.TQ = {
  ready: false, sr: 0, ctx: null, play: null,
  vecs: [],            // {t: performance.now(), pk: пик вектора}
  marks: {},           // фаза → performance.now()
  und: 0, lvl: 0, lvlMs: 0, msgN: 0,
};

async function setupTQ(){
  const ctx = new AudioContext({sampleRate: 48000});
  await ctx.audioWorklet.addModule("/static/voice-worklet.js");
  const play = new AudioWorkletNode(ctx, "vc-play",
    {numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1]});
  play.port.onmessage = (e) => {
    TQ.msgN++;
    if (e.data && e.data.under) TQ.und++;
    else if (e.data && e.data.stat){
      TQ.lvl = e.data.stat.lvl || 0;
      TQ.lvlMs = Math.round((TQ.lvl / TQ.sr) * 1000);
      TQ.und = e.data.stat.und | 0;
    }
  };
  const sp = ctx.createScriptProcessor(1024, 1, 1);
  sp.onaudioprocess = (ev) => {
    const d = ev.outputBuffer.getChannelData(0);
    let pk = 0;
    for (let i = 0; i < d.length; i++){
      const a = Math.abs(d[i]); if (a > pk) pk = a;
    }
    TQ.vecs.push({t: performance.now(), pk});
  };
  const g = ctx.createGain(); g.gain.value = 0;   // в «колонки» не играем
  play.connect(sp); sp.connect(g); g.connect(ctx.destination);
  TQ.ctx = ctx; TQ.play = play; TQ.sr = ctx.sampleRate; TQ.ready = true;
}

let tick = 0;
function toneFrame(amp){
  const f = new Float32Array(960);
  for (let i = 0; i < 960; i++, tick++)
    f[i] = amp * Math.sin(2 * Math.PI * 440 * tick / 48000);
  return f;
}
function zeroFrame(){
  const f = new Float32Array(960); tick += 960; return f;
}
function send(f){ TQ.play.port.postMessage(f.buffer, [f.buffer]); }
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const mark = (k) => { TQ.marks[k] = performance.now(); };

async function runScenario(){
  const F = 50;                       // кадров в секунду
  /* фаза 1: ровный поток 3с — провалов быть не должно */
  mark("p1");
  for (let s = 0; s < 3 * F; s++){ send(toneFrame(0.3)); await wait(20); }
  mark("p1end");
  /* фаза 2: три затыка по 120мс (6 кадров пачкой) */
  mark("p2");
  for (let s = 0; s < 3; s++){
    await wait(1200);
    await wait(120);                  // затык: ничего не шлём
    for (let k = 0; k < 6; k++) send(toneFrame(0.3));
  }
  mark("p2end");
  /* фаза 3: два затыка по 200мс (10 кадров пачкой) */
  mark("p3");
  for (let s = 0; s < 2; s++){
    await wait(1200);
    await wait(200);
    for (let k = 0; k < 10; k++) send(toneFrame(0.3));
  }
  mark("p3end");
  /* фаза 4: всплеск 600мс (30 кадров) + 3с тишины — дрен задержки */
  await wait(1000);
  mark("p4");
  for (let k = 0; k < 30; k++) send(toneFrame(0.3));
  for (let s = 0; s < 3 * F; s++){ send(zeroFrame()); await wait(20); }
  mark("p4end");
  await wait(500);
  return "done";
}

/* Разбор метрик: «слышимая тишина» — подряд векторы с пиком < 1e-3.
   Возвращает суммарную тишину в мс в окне [a, b) ПОСЛЕ первого звука. */
window.tqAnalyze = () => {
  const SILENT = 1e-3, VEC_MS = 1024 / TQ.sr * 1000;
  const firstSound = TQ.vecs.find((v) => v.pk >= SILENT);
  const gaps = [];          // {t0, ms}
  let run = 0, runT0 = 0;
  for (const v of TQ.vecs){
    if (v.t < (firstSound ? firstSound.t : Infinity)) continue;
    if (v.pk < SILENT){
      if (run === 0) runT0 = v.t;
      run++;
    } else if (run > 0){
      if (run * VEC_MS >= 6) gaps.push({t0: runT0, ms: run * VEC_MS});
      run = 0;
    }
  }
  if (run * VEC_MS >= 6) gaps.push({t0: runT0, ms: run * VEC_MS});
  const sumIn = (a, b) => gaps
    .filter((g) => g.t0 >= TQ.marks[a] && g.t0 < TQ.marks[b])
    .reduce((acc, g) => acc + g.ms, 0);
  return {
    vecN: TQ.vecs.length, gaps, und: TQ.und, lvlMs: TQ.lvlMs,
    p1: sumIn("p1", "p1end"),
    p2: sumIn("p2", "p2end"),
    p3: sumIn("p3", "p3end"),
    p4: sumIn("p4", "p4end"),
  };
};
"""


def main() -> int:
    print("== E2E качества плейаута: настоящий Chromium, джиттер-сценарий ==\n")
    from playwright.sync_api import sync_playwright

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
            ])
            try:
                page = browser.new_context().new_page()
                errs: list[str] = []
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/",
                          wait_until="domcontentloaded")
                page.evaluate(SCENARIO)
                page.evaluate("() => setupTQ()")
                page.wait_for_function("() => TQ.ready", timeout=10000)
                sr = page.evaluate("() => TQ.sr")
                check("1. AudioContext поднят, 48кГц", sr == 48000, f"sr={sr}")

                # прогон сценария (~22с реального времени)
                page.evaluate("() => runScenario()")
                res = page.evaluate("() => window.tqAnalyze()")

                # ScriptProcessor-зонд должен жить (сценарий ~15-20с стенки;
                # setTimeout-дрейф браузера даёт 300+ векторов по 46.9мс)
                check("2. зонд получает аудио-векторы (SP жив)",
                      res["vecN"] > 250, f"vecN={res['vecN']}")
                print(f"     метрики: тишина по фазам, мс — "
                      f"p1={res['p1']:.0f} p2={res['p2']:.0f} "
                      f"p3={res['p3']:.0f} p4={res['p4']:.0f} · "
                      f"недоборов={res['und']} · уровень={res['lvlMs']}мс")

                # ФАЗА 1 — ровный поток: ни одного провала
                check("3. ровный поток: 0 провалов за 3с",
                      res["p1"] < 1, f"{res['p1']:.0f}мс")
                # ФАЗА 2 — затыки 120мс: PLC+рестарт закрывают почти всё
                # (старый код: ~200мс тишины на КАЖДЫЙ затык → 600мс)
                check("4. затыки 120мс: суммарная тишина < 150мс (PLC работает)",
                      res["p2"] < 150, f"{res['p2']:.0f}мс из ~600мс у v3.6.9")
                # ФАЗА 3 — затыки 200мс: вдвое короче прежнего
                # (старый код: затык + полный рефилл буфера ≈ 300-350мс × 2)
                check("5. затыки 200мс: суммарная тишина < 400мс",
                      res["p3"] < 400, f"{res['p3']:.0f}мс из ~700мс у v3.6.9")
                # ФАЗА 4 — всплеск 600мс + пауза: дрен опустил задержку
                check("6. дрен на тишине: уровень буфера ≤ 240мс",
                      res["lvlMs"] <= 240, f"{res['lvlMs']}мс")
                # адаптация жива: недоборы реально считались и буфер рос
                check("7. счётчик недоборов работает (≥5 затыков поймано)",
                      res["und"] >= 5, f"und={res['und']}")
                # ошибок страницы нет
                check("8. JS-ошибок нет", not errs, "; ".join(errs[:3]))
            finally:
                browser.close()
    finally:
        srv.shutdown()

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
