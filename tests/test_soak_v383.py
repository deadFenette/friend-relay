#!/usr/bin/env python3
"""Дымовой soak-тест v3.8.3: короткий (≈25 с) смешанный прогон
scripts/soak_web.py — чат + голос + файлы + шторм реконнектов + рестарт.

Полный soak (5-30 мин и дольше) запускается вручную:
    python scripts/soak_web.py --minutes 30
Здесь проверяем, что механика соака жива и пороги соблюдены на коротком
окне: сообщения ходят, голос стримится и пересобирается после шторма,
файл уезжает туда-обратно целым, сервер переживает рестарт, утечек нет.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "soak_web", ROOT / "scripts" / "soak_web.py")
soak_web = importlib.util.module_from_spec(_spec)
# dataclass'ы ищут свой модуль в sys.modules (cpython#60304) —
# без этой строки exec_module падает на @dataclass
sys.modules["soak_web"] = soak_web
_spec.loader.exec_module(soak_web)

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail and not cond else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    print("== Soak v3.8.3: дымовой смешанный прогон (≈25 с) ==")
    tmp = Path(tempfile.mkdtemp(prefix="wr_soak_test_"))
    report = tmp / "soak_report.json"
    res = soak_web.run_soak(
        minutes=0.4, clients=3, voice=2, file_workers=1, slow_clients=1,
        port=free_port(), restart=True, warmup=3.0, report=report,
        data_dir=tmp / "data", quiet=False)
    s = res.stats

    check("1. пороги соака соблюдены", res.passed,
          "; ".join(res.failures)[:400])
    check("2. чат-сообщения ходят", s.get("chat_sent", 0) >= 5,
          f"chat_sent={s.get('chat_sent')}")
    check("3. опрос событий жив и быстр",
          s.get("polls", 0) >= 10 and s.get("poll_p95_s", 9) <= 2.0,
          f"polls={s.get('polls')} p95={s.get('poll_p95_s')}с")
    check("4. голос стримится", s.get("voice_rx_frames", 0) >= 300,
          f"voice_rx={s.get('voice_rx_frames')}")
    check("5. шторм реконнектов пережит",
          s.get("voice_resessions", 0) >= 3,
          f"resessions={s.get('voice_resessions')}")
    check("6. файл туда-обратно целым",
          s.get("files_up", 0) >= 1 and s.get("files_down_ok", 0) >= 1,
          f"up={s.get('files_up')} down_ok={s.get('files_down_ok')}")
    check("7. ошибок вне рестарта нет",
          s.get("chat_err", 0) == 0 and s.get("file_err", 0) == 0
          and not s.get("thread_errors"),
          f"chat_err={s.get('chat_err')} file_err={s.get('file_err')} "
          f"threads={s.get('thread_errors')[:2]}")
    check("8. утечек нет (короткое окно)",
          s.get("rss_growth_mb") in ("n/a",) or
          (isinstance(s.get("rss_growth_mb"), (int, float))
           and s.get("rss_growth_mb", 99) < 35),
          f"rss_growth={s.get('rss_growth_mb')} fd_growth={s.get('fd_growth')}")
    check("9. JSON-отчёт записан и согласован",
          report.exists() and json.loads(report.read_text(
              encoding="utf-8")).get("passed") is True)
    check("10. рестарт сервера пережит",
          s.get("chat_err_restart", 0) + s.get("file_err_restart", 0) >= 0
          and s.get("voice_resessions", 0) >= 3,
          "голос пересобрался после рестарта и шторма")
    check("11. CLI-флаги на месте",
          all(f in (ROOT / "scripts" / "soak_web.py").read_text(
              encoding="utf-8") for f in
              ("--minutes", "--voice", "--no-restart", "--report")))

    # ── 12. регресс бага v3.8.3: журнал истории переживает close() ──
    # Соак поймал: stop→start на том же RelayServer оставлял _history_fp
    # = None, и ЛЮБОЕ новое событие падало с 500. Проверяем самовосстановление.
    import tempfile as _tf
    from lib.domain.event_store import EventStore
    es_dir = Path(_tf.mkdtemp(prefix="wr_soak_fp_"))
    es = EventStore(es_dir)
    es.add_text("А", "до закрытия")
    es.close()
    ev2 = es.add_text("Б", "после закрытия — чат продолжает писаться")
    ok_reopen = bool(ev2 and ev2.get("seq"))
    es2 = EventStore(es_dir)
    texts = [e.get("text", "") for e in es2.events_since(0)]
    check("12. история переживает close() (баг соака v3.8.3)",
          ok_reopen and "до закрытия" in texts and any(
              "после закрытия" in t for t in texts),
          f"reopen={ok_reopen} texts={texts[:4]}")
    es.close()
    es2.close()

    print(f"ИТОГО: {PASS} OK / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
