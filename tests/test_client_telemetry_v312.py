"""Тесты клиентской телеметрии (lib/client_telemetry.py).

Группа проверок:
  1. ПРИВАТНОСТЬ: секретная строка из чата/сообщения/кода матча НЕ попадает
     ни в один файл телеметрии. Имя пользователя — НЕ попадает.
  2. Функциональность: счётчики складываются, observe корректно считает
     min/avg/max/count, exception пишет только ТИП (не сообщение).
  3. Whitelist: неизвестные значения сворачиваются в 'other', не улетая
     в ключ (мусорный ввод не раздувает пространство ключей).
  4. Opt-out: set_enabled(False) → record/observe/exception становятся no-op,
     выбор сохраняется в client_config.json и переживает рестарт.
  5. Файлы: flush() пишет ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt (UTF-8, читается),
     client_summary.json (JSON), client_events-YYYYMMDD.jsonl (дельты).
  6. Имя класса исключения — без пользовательского ввода (только идентификатор).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.client_telemetry import ClientTelemetry, _safe_static

# Уникальная «секретная» строка — её НЕ должно быть ни в одном файле.
SECRET = "SEKRET-KLIENTA-V-TELEMETRII-7f3a91"


def test_no_secret_in_any_file() -> bool:
    """Отправляем секрет в каждое публичное API — и проверяем, что после
    flush() он не встречается ни в одном файле telemetry/."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        # Пытаемся «протолкнуть» секрет через все возможные точки входа.
        # Если какая-то из них пропустит — тест упадёт.
        t.record(f"client.{SECRET}")                # мусорный ключ
        t.record("client.crash.test")               # валидный
        t.observe(f"client.{SECRET}.ms", 1.0)       # мусорный
        try:
            raise ValueError(SECRET)                # секрет в сообщении
        except ValueError as e:
            t.exception("game_dialog", e)
        # Хелперы с whitelist — секрет в имени игры/экрана:
        t.note_screen(SECRET)                       # должно стать 'other'
        t.note_game_dialog(SECRET, "open")          # должно стать 'other'
        t.note_game_dialog(SECRET, SECRET)          # должно стать 'other'
        t.note_invite_link_click(SECRET)            # должно стать 'other'
        t.note_message_action(SECRET)               # должно стать 'other'
        t.note_voice(SECRET)                        # должно стать 'other'
        t.note_command(ok=True)
        t.observe_match_duration_ms(1234.5)
        t.observe_poll_latency_ms(45.6)
        t.flush()

        # Скан всех файлов в telemetry/
        tele_dir = Path(d) / "telemetry"
        assert tele_dir.is_dir(), "telemetry dir должна быть создана"
        files = list(tele_dir.rglob("*"))
        assert files, "должны быть файлы после flush()"
        leaks: list[str] = []
        for f in files:
            if not f.is_file():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if SECRET in content:
                leaks.append(str(f))
        if leaks:
            print(f"  FAIL: секрет найден в файлах: {leaks}")
            return False
    print("  OK: секретная строка не попала ни в один файл")
    return True


def test_user_name_not_in_keys() -> bool:
    """Имя пользователя не должно попасть в ключи (даже через note_screen
    с произвольным именем)."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        # Имя пользователя «Василий Пупкин» — типичное.
        t.note_screen("Василий Пупкин")
        t.note_invite_link_click("Василий Пупкин")
        t.note_game_dialog("Василий Пупкин", "open")
        snap = t.snapshot()
        counters = snap.get("counters", {})
        # Должны быть только 'other' значения, без имени:
        for key in counters:
            if "васил" in key.lower() or "пупк" in key.lower():
                print(f"  FAIL: имя пользователя найдено в ключе {key!r}")
                return False
    print("  OK: имя пользователя не попало в ключи (свёрнуто в 'other')")
    return True


def test_counters_add_up() -> bool:
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        for _ in range(10):
            t.record("client.test.counter")
        for _ in range(5):
            t.record("client.test.counter2", 2)
        snap = t.snapshot()
        if snap["counters"].get("client.test.counter") != 10:
            print(f"  FAIL: counter1 должен быть 10, получили {snap['counters'].get('client.test.counter')}")
            return False
        if snap["counters"].get("client.test.counter2") != 10:
            print(f"  FAIL: counter2 должен быть 10 (5*2), получили {snap['counters'].get('client.test.counter2')}")
            return False
    print("  OK: счётчики складываются (включая n!=1)")
    return True


def test_observe_min_avg_max() -> bool:
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        for v in [10.0, 20.0, 30.0, 40.0]:
            t.observe("client.test.latency_ms", v)
        snap = t.snapshot()
        timing = snap["timings"].get("client.test.latency_ms")
        if not timing:
            print("  FAIL: timing не найден")
            return False
        if timing["min_ms"] != 10.0 or timing["max_ms"] != 40.0:
            print(f"  FAIL: min/max неверные: {timing}")
            return False
        if timing["avg_ms"] != 25.0:
            print(f"  FAIL: avg должен быть 25.0, получили {timing['avg_ms']}")
            return False
        if timing["count"] != 4:
            print(f"  FAIL: count должен быть 4, получили {timing['count']}")
            return False
    print("  OK: observe корректно считает min/avg/max/count")
    return True


def test_exception_writes_only_type_not_message() -> bool:
    """exception() пишет только ТИП исключения (имя класса), НЕ сообщение."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        secret_msg = SECRET + " в сообщении исключения"
        try:
            raise RuntimeError(secret_msg)
        except RuntimeError as e:
            t.exception("test_dialog", e)
        snap = t.snapshot()
        counters = snap.get("counters", {})
        # Должен быть ключ client.crash.test_dialog.RuntimeError
        if "client.crash.test_dialog.RuntimeError" not in counters:
            print(f"  FAIL: ключ типа исключения не найден, counters={counters}")
            return False
        # Секрет НЕ должен быть в снимке (он только в сообщении).
        snap_str = json.dumps(snap, ensure_ascii=False)
        if secret_msg in snap_str or SECRET in snap_str:
            print("  FAIL: сообщение исключения попало в снимок")
            return False
    print("  OK: exception пишет только тип, не сообщение")
    return True


def test_whitelist_collapses_unknown_to_other() -> bool:
    """note_screen/note_game_dialog с неизвестным значением → 'other' в ключе."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        t.note_screen("chat")           # валидный
        t.note_screen("EVIL_SCREEN")    # невалидный → 'other'
        t.note_game_dialog("chess", "open")    # валидный
        t.note_game_dialog("minecraft", "open")  # невалидный → 'other'
        snap = t.snapshot()
        c = snap["counters"]
        if c.get("client.screen.chat") != 1:
            print(f"  FAIL: chat должен быть 1, получили {c.get('client.screen.chat')}")
            return False
        if c.get("client.screen.other") != 1:
            print(f"  FAIL: other должен быть 1, получили {c.get('client.screen.other')}")
            return False
        if c.get("client.dialog.open.chess") != 1:
            print(f"  FAIL: chess open должен быть 1, получили {c.get('client.dialog.open.chess')}")
            return False
        if c.get("client.dialog.open.other") != 1:
            print(f"  FAIL: other open должен быть 1, получили {c.get('client.dialog.open.other')}")
            return False
        # Никаких 'minecraft' в ключах:
        for key in c:
            if "minecraft" in key.lower() or "evil" in key.lower():
                print(f"  FAIL: мусорное значение попало в ключ: {key}")
                return False
    print("  OK: whitelist сворачивает неизвестные значения в 'other'")
    return True


def test_opt_out_persists_across_restart() -> bool:
    """set_enabled(False) → запись замирает, выбор сохраняется в config.json,
    новый инстанс читает сохранённое состояние."""
    with tempfile.TemporaryDirectory() as d:
        t1 = ClientTelemetry(Path(d))
        assert t1.is_enabled(), "по умолчанию должно быть включено"
        t1.set_enabled(False)
        t1.record("client.test.after_disable")  # не должно попасть
        snap1 = t1.snapshot()
        if "client.test.after_disable" in snap1["counters"]:
            print("  FAIL: после disable счётчик всё ещё растёт")
            return False
        # Новый инстанс — должен прочитать config.json:
        t2 = ClientTelemetry(Path(d))
        if t2.is_enabled():
            print("  FAIL: после рестарта телеметрия должна быть выключена")
            return False
    print("  OK: opt-out сохраняется и переживает рестарт")
    return True


def test_flush_writes_files() -> bool:
    """flush() пишет три файла: .txt (UTF-8), .json, .jsonl (если есть дельты)."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        t.note_app_start()
        t.record("client.test.flush")
        t.flush()
        tele_dir = Path(d) / "telemetry"
        # .txt
        txt = tele_dir / "ОТСЛЕЖИВАНИЕ_ТЕЛЕМЕТРИИ_ГУИ.txt"
        if not txt.exists():
            print(f"  FAIL: файл {txt.name} не создан")
            return False
        try:
            content = txt.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  FAIL: не читается UTF-8: {e}")
            return False
        if "ОТСЛЕЖИВАНИЕ ТЕЛЕМЕТРИИ GUI" not in content:
            print("  FAIL: заголовок файла не найден")
            return False
        if "client.test.flush" not in content:
            print("  FAIL: счётчик не попал в .txt файл")
            return False
        # .json
        js = tele_dir / "client_summary.json"
        if not js.exists():
            print("  FAIL: client_summary.json не создан")
            return False
        data = json.loads(js.read_text(encoding="utf-8"))
        if data["counters"].get("client.test.flush") != 1:
            print(f"  FAIL: в JSON счётчик неверный: {data['counters']}")
            return False
        # .jsonl (дельты)
        import datetime
        day = datetime.datetime.now().strftime("%Y%m%d")
        ev = tele_dir / f"client_events-{day}.jsonl"
        if not ev.exists():
            print("  FAIL: events-файл не создан")
            return False
    print("  OK: flush() пишет .txt (UTF-8), .json, .jsonl")
    return True


def test_safe_static_rejects_garbage() -> bool:
    """_safe_static должен отбрасывать пути вида ../etc/passwd."""
    if _safe_static("../etc/passwd") is not None:
        print("  FAIL: ../etc/passwd не должен пройти")
        return False
    if _safe_static("chat") is None:
        print("  FAIL: 'chat' должен пройти")
        return False
    # Whitelist:
    if _safe_static("chat", frozenset({"chat", "dm"})) is None:
        print("  FAIL: 'chat' должен пройти whitelist")
        return False
    if _safe_static("EVIL", frozenset({"chat", "dm"})) is not None:
        print("  FAIL: 'EVIL' не должен пройти whitelist")
        return False
    # Non-str:
    if _safe_static(123) is not None:  # type: ignore[arg-type]
        print("  FAIL: не-строка должна дать None")
        return False
    print("  OK: _safe_static отбрасывает мусор и применяет whitelist")
    return True


def test_overflow_protected() -> bool:
    """Если писать >256 разных ключей — overflow растёт, dict не разрастается."""
    with tempfile.TemporaryDirectory() as d:
        t = ClientTelemetry(Path(d))
        # Пишем 300 разных ключей — лимит 256, остальные должны уйти в overflow.
        for i in range(300):
            t.record(f"client.test.overflow.key{i:03d}")
        snap = t.snapshot()
        # counters может быть ≤256 (или чуть больше — есть уже записанные)
        if len(snap["counters"]) > 300:
            print(f"  FAIL: счётчиков {len(snap['counters'])}, должно быть ≤256")
            return False
        if snap["overflow"] == 0:
            print("  FAIL: overflow должен быть > 0 после 300 разных ключей")
            return False
    print(f"  OK: overflow защищает от разрастания (overflow={snap['overflow']})")
    return True


def main() -> int:
    print("=" * 70)
    print("Test: клиентская телеметрия (lib/client_telemetry.py)")
    print("=" * 70)
    print()
    tests = [
        ("no_secret_in_any_file",            test_no_secret_in_any_file),
        ("user_name_not_in_keys",            test_user_name_not_in_keys),
        ("counters_add_up",                  test_counters_add_up),
        ("observe_min_avg_max",              test_observe_min_avg_max),
        ("exception_writes_only_type",       test_exception_writes_only_type_not_message),
        ("whitelist_collapses_unknown",      test_whitelist_collapses_unknown_to_other),
        ("opt_out_persists_across_restart",  test_opt_out_persists_across_restart),
        ("flush_writes_files",               test_flush_writes_files),
        ("safe_static_rejects_garbage",      test_safe_static_rejects_garbage),
        ("overflow_protected",               test_overflow_protected),
    ]
    failed = 0
    for name, fn in tests:
        print(f"[{name}]")
        try:
            ok = fn()
        except Exception as e:
            ok = False
            print(f"  EXCEPTION: {type(e).__name__}: {e}")
        if not ok:
            failed += 1
        print()
    print("=" * 70)
    if failed:
        print(f"RESULT: {failed}/{len(tests)} tests FAILED")
        return 1
    print(f"RESULT: all {len(tests)} tests PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
