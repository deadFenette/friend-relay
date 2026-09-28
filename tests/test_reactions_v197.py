"""Регрессионные тесты бага v1.9.7: «реакции не сохраняются — ставлю, а они
исчезают».

Вскрыты ТРИ корня:
  1. Qt-клиент тогглил реакцию локально (оптимистичный отклик), а серверная
     дельта, приезжая по поллингу, переключала ЕЩЁ РАЗ — через секунду
     реакция снималась сама.
  2. Веб при переподключении (since=0) получал text-события с уже вшитыми
     реакциями + дельты-тогглы поверх — реакции «отматывались» до пустых.
  3. Дельта была слепым переключателем: любое повторное применение меняло
     состояние (не идемпотентна).

Решение: дельта-событие реакции несёт ЯВНЫЙ флаг "added" (поставлена/снята),
применение идемпотентно; легаси-дельты без флага работают как раньше.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.client_core.chat_session import merge_delta_into
from lib.domain.event_store import EventStore

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def test_server_flag_and_idempotency(tmp: Path) -> None:
    print("== сервер: флаг added + идемпотентность ==")
    store = EventStore(tmp / "s1")
    ev = store.add_text("Аня", "привет")
    seq = ev["seq"]

    r1 = store.add_reaction("Аня", seq, "🔥")
    check("дельта постановки несёт added=True", r1.get("added") is True)
    r2 = store.add_reaction("Аня", seq, "🔥")
    check("дельта снятия несёт added=False", r2.get("added") is False)
    check("после снятия реакций нет", "🔥" not in store.events_since(0)[0].get("reactions", {}))

    # Идемпотентность: одна и та же дельта применена ДВАЖДЫ — состояние
    # не меняется (раньше второй тоггл отменял первый).
    target = {"kind": "text", "seq": seq, "text": "привет"}
    store.apply_delta(target, r1)   # первый раз
    store.apply_delta(target, r1)   # «дельта приехала повторно» (replay)
    check("повторное применение дельты не отматывает реакцию",
          target.get("reactions", {}).get("🔥") == ["Аня"])

    store.apply_delta(target, r2)
    store.apply_delta(target, r2)
    check("повторное снятие тоже идемпотентно",
          "🔥" not in target.get("reactions", {}))

    # РАЗЫГРЫШ бага №2 (web replay): text-событие с УЖЕ вшитой реакцией +
    # дельты поверх. Раньше тогглы отматывали реакции прямо при загрузке.
    store.add_reaction("Боб", seq, "🔥")    # Боб поставил 🔥
    all_events = store.events_since(0)
    live = next(e for e in all_events if e.get("seq") == seq)
    deltas = [e for e in all_events if e.get("kind") == "reaction"]
    check("в логе три дельты: Аня+, Аня-, Боб+", len(deltas) == 3)
    replayed = dict(live)
    for d in deltas:                         # replay ВСЕХ дельт по порядку
        store.apply_delta(replayed, d)
    check("полный replay дельт поверх вшитого состояния сходится к нему",
          replayed.get("reactions", {}).get("🔥") == ["Боб"],
          f"got={replayed.get('reactions')!r}")


def test_persistence_and_disk_replay(tmp: Path) -> None:
    print("== диск: рестарт + переигрывание дельт с флагом ==")
    d = tmp / "s2"
    store = EventStore(d)
    ev = store.add_text("Аня", "сообщение")
    store.add_reaction("Аня", ev["seq"], "👍")
    store.add_reaction("Боб", ev["seq"], "👍")
    store.add_reaction("Боря", ev["seq"], "🎉")
    store.add_reaction("Боб", ev["seq"], "👍")     # снял
    expected = {"👍": ["Аня"], "🎉": ["Боря"]}
    store.close()

    store2 = EventStore(d)   # рестарт: _load_history + _replay_deltas
    ev2 = next(e for e in store2.events_since(0) if e.get("seq") == ev["seq"])
    check("состав реакций пережил рестарт", ev2.get("reactions") == expected,
          f"got={ev2.get('reactions')!r}")
    store2.close()

    # СМЕШАННЫЙ лог: старые дельты без флага + новые с флагом — итог один
    d3 = tmp / "s3"
    import json
    d3.mkdir(parents=True, exist_ok=True)
    hist = d3 / "history.jsonl"
    rows = [
        {"seq": 1, "kind": "text", "from": "Аня", "ts": 1, "text": "микс"},
        {"seq": 2, "kind": "reaction", "from": "Боб", "ts": 2, "target_seq": 1,
         "emoji": "🔥"},                                    # легаси: без флага
        {"seq": 3, "kind": "reaction", "from": "Вова", "ts": 3, "target_seq": 1,
         "emoji": "🔥", "added": True},                      # новая: с флагом
    ]
    with hist.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    store3 = EventStore(d3)
    ev3 = next(e for e in store3.events_since(0) if e.get("seq") == 1)
    check("легаси-дельта (тоггл) + новая (флаг) дают корректный итог",
          ev3.get("reactions", {}).get("🔥") == ["Боб", "Вова"],
          f"got={ev3.get('reactions')!r}")
    store3.close()


def test_qt_merge_delta() -> None:
    print("== Qt: merge_delta_into (ядро экрана чата) ==")
    # РАЗЫГРЫШ бага №1 (пользовательский): оптимистичный тоггл в
    # _on_reaction_requested + подтверждение сервером.
    data = {"kind": "text", "seq": 5, "text": "привет"}
    # 1) оптимистичный локальный тоггл (экран сделал это до запроса):
    merge_delta_into(data, {"kind": "reaction", "from": "я", "emoji": "🔥",
                            "added": True})
    # 2) дельта от сервера подтверждает:
    merge_delta_into(data, {"kind": "reaction", "from": "я", "emoji": "🔥",
                            "added": True})
    check("оптимистичный тоггл + серверная дельта = реакция НА МЕСТЕ",
          data["reactions"].get("🔥") == ["я"],
          f"got={data.get('reactions')!r}")

    # Снятие: локально убрали + сервер подтвердил снятие
    merge_delta_into(data, {"kind": "reaction", "from": "я", "emoji": "🔥",
                            "added": False})
    merge_delta_into(data, {"kind": "reaction", "from": "я", "emoji": "🔥",
                            "added": False})
    check("двойное снятие не ломает состояние",
          "🔥" not in data.get("reactions", {}))

    # Легаси-дельта без флага — прежнее поведение (тоггл)
    legacy = {"kind": "text", "seq": 6, "text": "x"}
    merge_delta_into(legacy, {"kind": "reaction", "from": "Аня", "emoji": "🔥"})
    check("легаси-дельта ставит реакцию", legacy["reactions"]["🔥"] == ["Аня"])
    merge_delta_into(legacy, {"kind": "reaction", "from": "Аня", "emoji": "🔥"})
    check("легаси-дельта повторно — тоггл (совместимость)",
          "🔥" not in legacy.get("reactions", {}))

    # Чужие реакции копятся, снятие одного не трогает другого
    multi = {"kind": "text", "seq": 7, "text": "x"}
    merge_delta_into(multi, {"kind": "reaction", "from": "А", "emoji": "👍", "added": True})
    merge_delta_into(multi, {"kind": "reaction", "from": "Б", "emoji": "👍", "added": True})
    merge_delta_into(multi, {"kind": "reaction", "from": "А", "emoji": "👍", "added": False})
    check("снятие одного участника не трогает второго",
          multi["reactions"].get("👍") == ["Б"])


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_server_flag_and_idempotency(tmp)
        test_persistence_and_disk_replay(tmp)
    test_qt_merge_delta()
    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
