#!/usr/bin/env python3
"""E2E-проверка МИКРОФОНА ВЕБ-клиента в настоящем Chromium (Playwright).

Пользовательский симптом «в вебе не работал голос / собеседник не слышит»
проверяется НАСТОЯЩИМ браузером: два независимых контекста Chromium с
fake-mic (тестовый тон браузера), реальный RelayServer + VoiceMixer +
VoiceBridge, реальный web_client (voice.js + voice-worklet.js).

Что замеряется (не «вроде работает», а цифры):
  1. getUserMedia под фейковым UI: разрешение выдано, трек жив.
  2. AudioContext 48кГц, состояние running.
  3. ЗАХВАТ: VC.sentFrames растёт (микрофон шлёт 20мс-кадры в мост).
  4. ПРИЁМ: VC.recvFrames растёт у собеседника.
  5. ЗВУК ДОХОДИТ НЕ ПУСТЫМ: кадры, влетающие в vc-play (worklet
     плейаут), не тишина (RMS > порога) — «собеседник слышит».
  6. НЕ «БУРУНДУК»: доля ПОСЛЕДОВАТЕЛЬНО ИДЕНТИЧНЫХ кадров
     (dup_ratio) мала. Баг v3.6.6 и ниже давал 0.875+ (все чанки —
     один и тот же кусок памяти); после фикса копия сразу.
     Дополнительно: разнообразие RMS между кадрами.
  v3.6.9 добавил (полировка — проверки живучести):
  11. ОДНОВРЕМЕННЫЙ РАЗГОВОР: после снятия мьюта оба говорят
      вразнобой — микшер складывает, у ОБОИХ приходят живые кадры.
  12. ОБРЫВ МОСТА: WS B принудительно рвётся → voice.js сам
      переподключается (лестница 300мс…) → звук снова течёт в
      ОБЕ стороны, без участия пользователя.

Запуск: python scripts/web_voice_browser_e2e.py
(нужен playwright с chromium: pip install playwright && playwright install chromium)
"""
from __future__ import annotations

import json
import statistics
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


PROBE_PLAY = """
() => {
  if (!VC.playPort || VC.__probe) return;
  VC.__probe = { frames: [], orig: VC.playPort.postMessage.bind(VC.playPort) };
  VC.playPort.postMessage = (data, transfer) => {
    try {
      /* v3.7.0: меряем ТОЛЬКО бинарные PCM-кадры. Control-сообщения
         ({under:true}, {stat:{lvl,und}}) через этот же порт раньше
         превращались в пустой Float32Array → rms = 0/0 = NaN →
         statistics.pstdev в питоне падал на NaN-элементе. */
      if (data instanceof ArrayBuffer){
        const f = new Float32Array(data);
        let acc = 0;
        for (let i = 0; i < f.length; i++) acc += f[i] * f[i];
        const rms = Math.sqrt(acc / f.length) * 32768;
        /* ДЕТЕКТОР «БУРУНДУКА» v3.6.6: битый VcCap копил ССЫЛКИ на 128-
           сэмпловые чанки, память переиспользовалась — весь 960-кадр
           состоял из ОДНОГО И ТОГО ЖЕ блока, повторённого 8 раз.
           Меряем внутрикадровое повторение: все ли 8 блоков по 128
           сэмплов идентичны (только у НЕ тихих кадров). */
        let rep = false;
        if (rms > 120 && f.length === 960){
          rep = true;
          for (let b = 1; b < 8 && rep; b++){
            for (let i = 0; i < 128; i++){
              if (Math.round(f[i] * 1e5) !== Math.round(f[b * 128 + i] * 1e5)){
                rep = false; break;
              }
            }
          }
        }
        VC.__probe.frames.push({ rms, rep });
      }
    } catch (e) {}
    VC.__probe.orig(data, transfer);
  };
}
"""


def collet_probe(page) -> list[dict]:
    return page.evaluate("""() => (VC.__probe ? VC.__probe.frames : [])""")


def connect_and_join_voice(page, name: str, port: int) -> None:
    page.goto(f"https://127.0.0.1:{port}/", wait_until="domcontentloaded")
    page.fill("#name", name)
    # v3.7.1: ключи переехали в спойлер #advKeys (дружелюбный экран входа)
    page.click("#advKeys summary")
    page.fill("#key", "k")
    page.click("#btnConnect")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)
    # вкладка «Голос» и подключение к мосту
    page.evaluate("""() => {
      document.querySelector('.tabs button[data-tab="voice"]').click();
    }""")
    page.wait_for_function("() => !!VC.wsPath", timeout=10000)
    page.click("#btnVoice")
    page.wait_for_function("() => VC.on === true && VC.everConnected === true",
                           timeout=15000)
    # playPort появляется ПОСЛЕ асинхронного addModule (voiceStarted):
    # VC.on ещё не гарантия, что аудио-граф собран — ждём именно его
    page.wait_for_function("() => !!VC.playPort", timeout=15000)


def main() -> int:
    print("== E2E веб-микрофона: настоящий Chromium, два клиента, реальный микшер ==\n")
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="wr_voice_e2e_"))
    base = 18530
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
                "--disable-features=WebRtcHideLocalIpsWithMdns",
            ])
            try:
                ctx_a = browser.new_context(ignore_https_errors=True)
                ctx_b = browser.new_context(ignore_https_errors=True)
                a = ctx_a.new_page()
                b = ctx_b.new_page()
                # глушим console-noise, ошибки собираем
                errs: list[str] = []
                for pg in (a, b):
                    pg.on("pageerror", lambda e: errs.append(str(e)))

                print("  -- подключение A и B, вход в голос --")
                connect_and_join_voice(a, "Алиса", base)
                connect_and_join_voice(b, "Боб", base)
                check("1. оба клиента в голосовом канале",
                      a.evaluate("() => VC.on") and b.evaluate("() => VC.on"))

                # состояние микрофона и контекста
                mic_ok_a = a.evaluate(
                    "() => !!(VC.stream && VC.stream.getAudioTracks()"
                    ".some(t => t.enabled && t.readyState === 'live'))")
                ctx_state_a = a.evaluate(
                    "() => VC.ctx ? (VC.ctx.sampleRate + '/' + VC.ctx.state) : 'none'")
                check("2. getUserMedia: живой аудио-трек у A", mic_ok_a)
                check("3. AudioContext A: 48кГц, running", ctx_state_a == "48000/running",
                      ctx_state_a)

                # вешаем зонд на плейаут B и A ДО начала речи
                b.evaluate(PROBE_PLAY)
                a.evaluate(PROBE_PLAY)

                # 6 секунд разговорного трафика (fake-mic тон у обоих)
                time.sleep(6)

                sent_a = a.evaluate("() => VC.sentFrames")
                recv_b = b.evaluate("() => VC.recvFrames")
                check("4. микрофон A шлёт кадры в мост (sentFrames > 100)",
                      sent_a > 100, f"sent={sent_a}")
                check("5. B принимает поток (recvFrames > 100)",
                      recv_b > 100, f"recv={recv_b}")

                # ЗВУК ДОХОДИТ НЕ ПУСТЫМ + НЕ БУРУНДУК у B
                # (fake-mic Chromium — бипы с паузами: duty cycle ~20-30%,
                # поэтому меряем количество живых кадров, а не долю)
                frames = collet_probe(b)
                rms_list = [f["rms"] for f in frames]
                non_silent = sum(1 for r in rms_list if r > 120)
                check("6. B получает НЕ тихие кадры (>=40 живых кадров)",
                      non_silent >= 40,
                      f"non_silent={non_silent}/{len(frames)}")
                reps = sum(1 for f in frames if f["rep"])
                check("7. НЕ «бурундук»: внутрикадровых повторов < 10% у B",
                      reps < max(2, len(frames) * 0.1),
                      f"repeats={reps}/{len(frames)}")
                if len(rms_list) > 10:
                    spread = statistics.pstdev(rms_list)
                    check("8. живая волна: разброс RMS между кадрами > 1",
                          spread > 1.0, f"stdev={spread:.1f}")

                # обратное направление B → A
                sent_b = b.evaluate("() => VC.sentFrames")
                recv_a = a.evaluate("() => VC.recvFrames")
                fa = collet_probe(a)
                ns_a = sum(1 for f in fa if f["rms"] > 120)
                check("9. обратное направление: B шлёт, A получает НЕ тихие кадры",
                      sent_b > 100 and recv_a > 100 and ns_a >= 40,
                      f"sent_b={sent_b} recv_a={recv_a} non_silent={ns_a}/{len(fa)}")

                # mute → у собеседника тишина (проверка «кнопки»)
                b.click("#btnVoiceMute")   # Боб замьютился
                # 1с на промывку тракта: кадры B, уже стоявшие в джиттер-
                # буферах моста/микшера, дорабатывают (законный хвост)
                time.sleep(1)
                # чистим зонд A: дальше смотрим ТОЛЬКО окно после мьюта
                a.evaluate("""() => { VC.__probe.frames = []; }""")
                time.sleep(2.5)
                fa3 = collet_probe(a)
                # A продолжает говорить в фейк-мик, но своё эхо миксер не
                # возвращает; B замьючен → у A всё окно — тишина микшера.
                non_silent_after = sum(1 for f in fa3 if f["rms"] > 120)
                check("10. mute B: A больше не слышит B (все кадры — тишина микшера)",
                      non_silent_after == 0,
                      f"non_silent_after_mute={non_silent_after}/{len(fa3)}")

                # v3.6.9: снимаем мьют — снова оба говорят ОДНОВРЕМЕННО:
                # микшер обязан сложить два потока, у каждого — живые кадры
                b.click("#btnVoiceMute")   # Боб размутился
                time.sleep(0.5)
                a.evaluate("() => { VC.__probe.frames = []; }")
                b.evaluate("() => { VC.__probe.frames = []; }")
                time.sleep(2.5)
                fa4 = collet_probe(a)
                fb4 = collet_probe(b)
                ns_a4 = sum(1 for f in fa4 if f["rms"] > 120)
                ns_b4 = sum(1 for f in fb4 if f["rms"] > 120)
                check("11. оба говорят одновременно — микшер складывает, "
                      "слышат оба направления",
                      ns_a4 >= 15 and ns_b4 >= 15,
                      f"A живых={ns_a4}/{len(fa4)}  B живых={ns_b4}/{len(fb4)}")

                # v3.6.9: ОБРЫВ — рвём WS Боба (как это делает злой роутер)
                # и проверяем, что voice.js сам восстановит звук
                b.evaluate("() => { const w = VC.ws; VC.ws = null;"
                           "  if (w) try { w.close(); } catch(e){} }")
                b.wait_for_function(
                    "() => VC.on === true && VC.ws && "
                    "VC.ws.readyState === 1 && VC.everConnected === true",
                    timeout=20000)
                time.sleep(0.5)   # дать мосту зарегистрировать нового подписчика
                a.evaluate("() => { VC.__probe.frames = []; }")
                time.sleep(2.5)
                fa5 = collet_probe(a)
                ns_a5 = sum(1 for f in fa5 if f["rms"] > 120)
                reconnected = b.evaluate(
                    "() => VC.on && VC.ws && VC.ws.readyState === 1")
                check("12. обрыв моста — голос сам переподключился, "
                      "звук от B снова идёт",
                      reconnected and ns_a5 >= 15,
                      f"reconnected={reconnected} живых={ns_a5}/{len(fa5)}")

                warn_text = a.evaluate("() => document.getElementById('voiceWarn').textContent")
                check("13. никаких аварийных предупреждений голоса у A "
                      "(даже после обрыва)",
                      "рвётся" not in warn_text and "недоступен" not in warn_text,
                      warn_text[:80])
                check("14. на страницах нет JS-ошибок", not errs,
                      "; ".join(errs)[:120])
            finally:
                browser.close()
    finally:
        relay.stop()

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
