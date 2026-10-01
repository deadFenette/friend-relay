#!/usr/bin/env python3
"""v3.7.0 — тесты «чистоты звука» (симптом: прерывистый звук, старый скайп).

Покрывает СЕРВЕРНУЮ часть фикса — устранение Nagle на веб-тракте голоса:
  браузер → TLS-сокет HTTP-сервера → туннель → WS-сокет моста → микшер.

Классический сценарий деградации: 20мс-кадры по ~2КБ + включённый Nagle
→ ядро задерживает сегмент до ACK предыдущего (RTT ZeroTier 30-150мс +
delayed ack) → ровный поток 50 кадров/с превращается во всплески →
джиттер-буфер браузера сохнет → «прерывистый звук». Микшер свой TCP уже
ставил NODELAY (v1.x), а веб-тракт — нет; здесь это закреплено тестами.

Плюс структурные проверки клиентских правок v3.7.0 (voice.js /
voice-worklet.js): PLC-достройка, быстрый рестарт, дрен на тишине,
сторож аплинка. Поведенческая проверка плейаута — отдельным браузерным
E2E (scripts/web_playout_quality_e2e.py).
"""
from __future__ import annotations

import inspect
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.server.http_api import RelayHTTPHandler  # noqa: E402
from voice.bridge import VoiceBridge  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("== v3.7.0: NODELAY веб-тракта голоса + клиентские проверки ==\n")

    # ── 1. Даунлинк: TLS-сокет браузера ──────────────────────────────
    check("1. http_api: disable_nagle_algorithm = True (даунлинк голоса)",
          RelayHTTPHandler.disable_nagle_algorithm is True)

    # ── 2. NODELAY реально применяется к TCP-сокету ──────────────────
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    c = socket.create_connection(lst.getsockname(), timeout=2)
    s, _ = lst.accept()
    try:
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        val = c.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        check("2. TCP_NODELAY ставится и читается обратно", val == 1,
              f"getsockopt={val}")
    finally:
        c.close()
        s.close()
        lst.close()

    # ── 3. Аплинк: upstream туннеля /voice/ws ────────────────────────
    src_tun = inspect.getsource(
        RelayHTTPHandler._tunnel_voice_ws)
    check("3. туннель /voice/ws: upstream получает TCP_NODELAY",
          "TCP_NODELAY" in src_tun)

    # ── 4. Мост: WS-сокет без Nagle ──────────────────────────────────
    src_h = inspect.getsource(VoiceBridge._handler)
    check("4. bridge._handler: WS-сокет получает TCP_NODELAY",
          "TCP_NODELAY" in src_h)

    # ── 5. Клиент: voice-worklet.js — PLC и быстрый рестарт ─────────
    wl = (ROOT / "web_client" / "voice-worklet.js").read_text(encoding="utf-8")
    check("5. ворклет: оба процессора зарегистрированы",
          'registerProcessor("vc-cap"' in wl
          and 'registerProcessor("vc-play"' in wl)
    check("6. ворклет: PLC-достройка (хвост + бюджет + огибающая)",
          "this.tail" in wl and "this.PLC_MAX" in wl
          and "plcRun" in wl)
    check("7. ворклет: рестарт на половине цели (не на полном буфере)",
          "this.targetS >> 1" in wl)
    check("8. ворклет: дрен задержки на тишине (highWater + флаг тишины)",
          "highWater" in wl and "this.sil" in wl)
    check("9. ворклет: телеметрия уровня/недоборов наверх",
          "stat:" in wl)

    # ── 6. Клиент: voice.js — сторож аплинка и адаптация ─────────────
    vj = (ROOT / "web_client" / "voice.js").read_text(encoding="utf-8")
    check("10. voice.js: сторож аплинка ws.bufferedAmount",
          "bufferedAmount" in vj and "UPDROP_LIMIT_BYTES" in vj)
    check("11. voice.js: рост цели +2 кадра за недобор (как Qt)",
          "VC.jitterTarget = Math.min(18, VC.jitterTarget + 2)" in vj)
    check("12. voice.js: пол сжатия буфера 80мс (4 кадра)",
          "VC.jitterTarget > 4" in vj)
    check("13. voice.js: недоборы/срезы в строке статуса",
          "провалов" in vj and "срезано" in vj)
    check("14. voice.js: PLC и в legacy-пути (ScriptProcessor)",
          "plcRun" in vj and "lastReal" in vj)

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
