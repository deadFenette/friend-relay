"""Юнит-тесты доменного слоя (lib/domain/event_store.py) - БЕЗ Qt и БЕЗ HTTP.

Раньше вся эта логика жила внутри RelayServer и проверялась только
интеграционными тестами через поднятый HTTP-сервер. Теперь домен можно
проверять напрямую - в этом и смысл слоёв.

Запуск: python tests/test_event_store.py"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.domain import EventStore, PresenceBoard

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def make_store(tmp: Path, name: str = "h1") -> EventStore:
    return EventStore(tmp / name)


def test_text_crud(tmp: Path) -> None:
    print("== add_text / edit / delete ==")
    store = make_store(tmp, "crud")
    ev = store.add_text("Вася", "привет")
    check("seq начинается с 1", ev["seq"] == 1)
    check("kind=text", ev["kind"] == "text")
    check("from сохранён", ev["from"] == "Вася")

    # шифрованное сообщение сохраняет флаги
    ev2 = store.add_text("Петя", "секрет", encrypted=True, iv="abc")
    check("encrypted флаг", ev2.get("encrypted") is True and ev2.get("iv") == "abc")

    # редактировать может только автор
    check("чужой не редактирует", store.edit_text("Вася", ev2["seq"], "взлом") is None)
    ok = store.edit_text("Петя", ev2["seq"], "обновлено")
    check("автор редактирует", ok is not None and ok["kind"] == "edit")

    # редактировать файл нельзя
    fev = store.add_file("Вася", "f.bin", b"123")
    check("файл не редактируется", store.edit_text("Вася", fev["seq"], "х") is None)

    # удаление: только автор, только text/file
    check("чужой не удаляет", store.delete_event("Петя", fev["seq"]) is None)
    tomb = store.delete_event("Вася", fev["seq"])
    check("автор удаляет", tomb is not None and tomb["kind"] == "delete")
    check("повторное удаление запрещено", store.delete_event("Вася", fev["seq"]) is None)
    check("файл удалён с диска", not store.file_bytes_path(fev["file_id"]).exists())
    check("удалённый не редактируется", store.edit_text("Вася", ev["seq"], "x") is not None)
    store.close()


def test_reactions_pins(tmp: Path) -> None:
    print("== reactions / pins ==")
    store = make_store(tmp, "reactions")
    ev = store.add_text("Аня", "смайлы")
    seq = ev["seq"]

    r1 = store.add_reaction("Аня", seq, "🔥")
    check("реакция добавлена", r1 is not None and r1["kind"] == "reaction")
    check("реакция на файле запрещена",
          store.add_reaction("Аня", store.add_file("Аня", "x", b"1")["seq"], "🔥") is None)

    # повторная реакция тем же автором = переключение (тоггл)
    in_mem = next(e for e in store.events_since(0) if e["seq"] == seq)
    check("автор в списке", in_mem["reactions"]["🔥"] == ["Аня"])
    store.add_reaction("Аня", seq, "🔥")
    in_mem = next(e for e in store.events_since(0) if e["seq"] == seq)
    check("тоггл снял реакцию", "🔥" not in in_mem.get("reactions", {}))

    # пин-тоггл
    p1 = store.pin_message("Аня", seq)
    check("пин поставлен", p1 is not None and p1["pinned"] is True)
    p2 = store.pin_message("Аня", seq)
    check("пин снят", p2 is not None and p2["pinned"] is False)

    # закреплённые с диска (дельты переигрываются)
    store.pin_message("Аня", seq)
    pinned = store.get_pinned_messages()
    check("get_pinned видит пин", len(pinned) == 1 and pinned[0]["seq"] == seq)
    store.close()


def test_persistence_and_deltas(tmp: Path) -> None:
    print("== persistence: перезапуск хоста ==")
    store = make_store(tmp, "persist")
    ev = store.add_text("Кот", "исходный")
    store.edit_text("Кот", ev["seq"], "исправлено")
    store.add_reaction("Мышь", ev["seq"], "🐱")
    store.pin_message("Кот", ev["seq"])
    seq_before = ev["seq"]
    store.close()

    # Новый EventStore на том же каталоге = перезапуск хоста
    store2 = make_store(tmp, "persist")
    reloaded = next(e for e in store2.events_since(0) if e["seq"] == seq_before)
    check("текст после рестарта", reloaded["text"] == "исправлено")
    check("edited флаг", reloaded.get("edited") is True)
    check("реакция пережила рестарт", reloaded.get("reactions", {}).get("🐱") == ["Мышь"])
    check("пин пережил рестарт", reloaded.get("pinned") is True)

    # seq не переиспользуется
    nxt = store2.add_text("Кот", "новое")
    check("seq продолжился", nxt["seq"] > seq_before)

    # events_before: старые сообщения с дельтами из-за пределов кеша
    old = store2.events_before(nxt["seq"], 10)
    check("events_before отдаёт история", any(e["seq"] == seq_before for e in old))
    target = next(e for e in old if e["seq"] == seq_before)
    check("дельты применены в events_before", target["text"] == "исправлено")
    store2.close()


def test_dm_and_files(tmp: Path) -> None:
    print("== DM: сообщения и приватные файлы ==")
    store = make_store(tmp, "dm")
    dm = store.send_dm("Аня", "Борис", "личное")
    check("dm сохранено", dm["kind"] == "dm" and dm["seq"] > 0)
    hist = store.get_dm_history("Борис", "Аня")  # порядок имён не важен
    check("dm история читается", len(hist) == 1 and hist[0]["text"] == "личное")
    convs = store.get_dm_conversations("Аня")
    check("диалоги найдены", len(convs) == 1 and convs[0]["user"] == "Борис")

    # приватный файл не попадает в общую витрину
    f = store.add_dm_file("Аня", "Борис", "secret.zip", b"privet!")
    check("dm_file kind", f["kind"] == "dm_file")
    check("в общей витрине пусто", store.list_all_files() == [])
    check("индекс dm-файла заполнен", store.get_dm_file_event(f["file_id"]) is not None)
    check("файл на диске", store.dm_file_bytes_path(f["file_id"]).read_bytes() == b"privet!")

    # dm-индекс восстанавливается после рестарта
    store.close()
    store2 = make_store(tmp, "dm")
    check("dm-файл доступен после рестарта",
          store2.get_dm_file_event(f["file_id"]) is not None)
    store2.close()


def test_presence(tmp: Path) -> None:
    print("== PresenceBoard ==")
    board = PresenceBoard(presence_timeout=10, typing_timeout=10)
    board.touch("?")   # мусорное имя игнорируется
    board.touch("")
    check("мусор не попадает в онлайн", board.online_names() == [])
    board.touch("Вася")
    board.touch("Петя")
    check("онлайн отсортирован", board.online_names() == ["Вася", "Петя"])
    board.set_typing("Вася")
    check("печатает виден", board.typing_names() == ["Вася"])
    check("exclude работает", board.typing_names(exclude="Вася") == [])


def test_link_preview_delta(tmp: Path) -> None:
    print("== link_preview delta ==")
    store = make_store(tmp, "preview")
    ev = store.add_text("Вася", "глянь https://example.com")
    store.attach_link_preview(ev["seq"], {"url": "https://example.com", "title": "Т"}, "host")
    in_mem = next(e for e in store.events_since(0) if e["seq"] == ev["seq"])
    check("превью прицеплено к оригиналу", in_mem["link_preview"]["title"] == "Т")

    # превью переживает рестарт (delta-событие в журнале)
    seq = ev["seq"]
    store.close()
    store2 = make_store(tmp, "preview")
    reloaded = next(e for e in store2.events_since(0) if e["seq"] == seq)
    check("превью пережило рестарт", reloaded.get("link_preview", {}).get("title") == "Т")

    # превью на удалённое сообщение не пишется
    ev2 = store2.add_text("Вася", "удали меня")
    store2.delete_event("Вася", ev2["seq"])
    before = len(store2.events_since(0))
    store2.attach_link_preview(ev2["seq"], {"title": "X"}, "host")
    check("превью на удалённое игнор", len(store2.events_since(0)) == before)
    store2.close()


def test_history_format(tmp: Path) -> None:
    print("== формат журнала на диске ==")
    store = make_store(tmp, "format")
    store.add_text("Кот", "мяу")
    store.close()
    lines = (tmp / "format" / "history.jsonl").read_text(encoding="utf-8").strip().split("\n")
    ev = json.loads(lines[0])
    check("JSONL формат сохранён", ev["kind"] == "text" and ev["text"] == "мяу")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="frelay_domain_") as td:
        tmp = Path(td)
        test_text_crud(tmp)
        test_reactions_pins(tmp)
        test_persistence_and_deltas(tmp)
        test_dm_and_files(tmp)
        test_presence(tmp)
        test_link_preview_delta(tmp)
        test_history_format(tmp)
    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
