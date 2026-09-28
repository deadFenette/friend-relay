"""Тесты ядра чата (lib/client_core/chat_session.py) — БЕЗ PySide6.

Фаза 1 отделения UI от логики: ChatSession переехал из qt_app/screens/
chat_screen.py. Проверяем политику поллинга, диффы хвостов каналов,
типинг, разбор dispatch и дешифровку — всё на синхронной фейковой
заглушке runner'а вместо AsyncBridge.

Запуск: python tests/test_chat_session.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.client_core.chat_session import (
    ChatSession,
    diff_tail,
    interpret_dispatch,
    merge_delta_into,
)

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


class FakeRunner:
    """Синхронная замена AsyncBridge.run: копит вызовы, доставляет вручную."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object]] = []  # (fn, on_success, on_error)

    def __call__(self, fn, on_success=None, on_error=None) -> None:
        self.calls.append((fn, on_success, on_error))

    @property
    def pending(self) -> int:
        return len(self.calls)

    def deliver_ok(self, idx: int = 0, value: object = None) -> None:
        fn, on_success, _on_error = self.calls.pop(idx)
        if on_success is not None:
            on_success(value(fn) if callable(value) else value)

    def deliver_err(self, idx: int = 0, exc: Exception | None = None) -> None:
        _fn, _on_success, on_error = self.calls.pop(idx)
        if on_error is not None:
            on_error(exc or OSError("network down"))


def make_session(**kw) -> tuple[ChatSession, FakeRunner]:
    runner = FakeRunner()
    session = ChatSession(
        base_url="http://test", my_name=kw.pop("my_name", "Я"), access_key="key", runner=runner
    )
    session.set_connected(True)
    return session, runner


# ── diff_tail ────────────────────────────────────────────────────────────────


def test_diff_tail() -> None:
    print("== diff_tail: добавленные/изменённые/удалённые ==")
    old = [
        {"seq": 1, "text": "a"},
        {"seq": 2, "text": "b"},
        {"seq": 3, "text": "c"},
    ]
    new = [
        {"seq": 2, "text": "b"},                # без изменений
        {"seq": 3, "text": "c!"},               # изменено
        {"seq": 4, "text": "d"},                # новое
        {"seq": "x", "text": "bad"},            # без int seq — пропускается
    ]
    d = diff_tail(old, new)
    check("added = [seq 4]", [m["seq"] for m in d.added] == [4])
    check("changed = [seq 3]", [m["seq"] for m in d.changed] == [3])
    check("removed = [seq 1]", d.removed == [1])
    check("не пустой", not d.is_empty())
    check("пустой дифф на одинаковых хвостах", diff_tail(old, list(old)).is_empty())
    check("старые без seq не попадают в removed", diff_tail([{"text": "z"}], [{"text": "z"}]).is_empty())


def test_merge_delta_into() -> None:
    print("== merge_delta_into: edit/reaction/pin/preview ==")
    data = {"text": "привет"}
    merge_delta_into(data, {"kind": "edit", "new_text": "пока", "ts": 5})
    check("edit меняет текст", data["text"] == "пока" and data["edited"] is True and data["edited_ts"] == 5)

    data = {"text": "x"}
    merge_delta_into(data, {"kind": "reaction", "from": "Аня", "emoji": "🔥"})
    check("реакция добавлена", data["reactions"]["🔥"] == ["Аня"])
    merge_delta_into(data, {"kind": "reaction", "from": "Боря", "emoji": "🔥"})
    check("вторая реакция дописана", data["reactions"]["🔥"] == ["Аня", "Боря"])
    merge_delta_into(data, {"kind": "reaction", "from": "Аня", "emoji": "🔥"})
    check("повтор — тоггл (снятие)", data["reactions"]["🔥"] == ["Боря"])
    merge_delta_into(data, {"kind": "reaction", "from": "Боря", "emoji": "🔥"})
    check("пустой эмодзи-ключ удаляется (словарь остаётся, как в оригинале)", data.get("reactions") == {})

    data = {"text": "x"}
    merge_delta_into(data, {"kind": "pin", "pinned": True})
    check("pin=True", data["pinned"] is True)
    merge_delta_into(data, {"kind": "pin", "pinned": False})
    check("pin=False", data["pinned"] is False)

    data = {"text": "x"}
    merge_delta_into(data, {"kind": "link_preview", "link_preview": {"url": "http://x"}})
    check("link_preview применён", data["link_preview"] == {"url": "http://x"})
    check("неизвестный kind ничего не ломает", merge_delta_into({}, {"kind": "???"}) == {})


def test_interpret_dispatch() -> None:
    print("== interpret_dispatch: вердикты ==")
    v = interpret_dispatch({"matched": False}, "!hello")
    check("не сматчился → text", v.kind == "text" and v.text == "!hello")

    v = interpret_dispatch(
        {"matched": True, "bot_id": "chess", "bot_action": {"action": "open_game", "tab": "leaderboard", "meta": {"game": "chess"}}, "bot_response": "ок"},
        "!chess top",
    )
    check("open_game: бот/мета/вкладка", v.kind == "open_game" and v.bot_id == "chess" and v.meta == {"game": "chess"} and v.tab == "leaderboard")
    check("open_game: есть текст бота → refresh", v.refresh is True)

    v = interpret_dispatch({"matched": True, "bot_id": "chess", "bot_action": {"action": "open_game"}}, "!chess")
    check("open_game: без текста → без refresh", v.refresh is False and v.tab == "play" and v.meta == {})

    v = interpret_dispatch({"matched": True, "bot_response": "привет от бота"}, "!bot")
    check("ответ без action → noop + refresh", v.kind == "noop" and v.refresh is True)

    v = interpret_dispatch({"matched": True}, "!bot")
    check("ни action, ни ответа → noop без refresh", v.kind == "noop" and v.refresh is False)

    v = interpret_dispatch({"matched": True, "bot_action": {"action": "unknown"}, "bot_response": "r"}, "!x")
    check("неизвестный action ведёт себя как раньше (noop+refresh)", v.kind == "noop" and v.refresh is True)


# ── ChatSession: поллинг ─────────────────────────────────────────────────────


def test_polling() -> None:
    print("== ChatSession: политика поллинга ==")
    s, r = make_session()
    seen: list[dict] = []
    s.on_poll_result = seen.append

    s.set_connected(False)
    check("без подключения do_poll не шлёт запрос", (s.do_poll(), r.pending == 0)[1])

    stopped = []
    s.on_polling_stopped = lambda: stopped.append(True)
    s.start_polling()
    s.tick()
    check("tick без подключения останавливает поллинг", stopped and not s.is_polling)
    s.set_connected(True)

    # обычный тик
    s.start_polling()
    s.tick()
    check("tick отправляет один запрос", r.pending == 1)
    result = {"events": [{"seq": 5, "kind": "text"}], "next_since": 5}
    r.deliver_ok(0, result)
    check("on_poll_result получил ответ", seen == [result])
    check("last_seq продвинут", s.last_seq == 5)

    # kick во время полёта → kick_pending
    s.do_poll()
    s.do_poll()
    s.do_poll()
    check("in-flight guard: один запрос, кики не плодят параллельных", r.pending == 1)
    r.deliver_ok(0, {"events": [], "next_since": 6})
    check("после завершения добирается отложенный kick", r.pending == 1 and s.last_seq == 6)
    r.deliver_ok(0, {"events": [], "next_since": 6})
    check("отложенный kick завершился чисто", r.pending == 0)

    # ошибка сети тихо гасится
    s.do_poll()
    r.deliver_err(0)
    check("ошибка: in_flight сброшен, cb не падает", r.pending == 0)
    s.tick()
    check("после ошибки следующий тик работает", r.pending == 1)
    r.deliver_ok(0, {"events": [], "next_since": 7})


def test_load_initial() -> None:
    print("== ChatSession: первичная загрузка ==")
    s, r = make_session()
    initial: list[dict] = []
    s.on_initial_result = initial.append

    s.load_initial()
    check("отправлен запрос общего tail", r.pending == 1)
    r.deliver_ok(0, {"events": [{"seq": 1}], "next_since": 12})
    check("cb получил tail", initial and initial[0]["next_since"] == 12)
    check("last_seq из next_since", s.last_seq == 12)
    # кэш общего хвоста: вьюха рисует его мгновенно при возврате в общий чат
    check("общий хвост закэширован", s.general_tail == [{"seq": 1}])

    # в режиме канала load_initial делегируется каналу
    s.set_channel("docs")
    chan_loaded: list[tuple[str, list]] = []
    s.on_channel_loaded = lambda ch, msgs: chan_loaded.append((ch, msgs))
    s.load_initial()
    check("load_initial в канале шлёт get_channel_messages", r.pending == 1)
    r.deliver_ok(0, [{"seq": 3, "text": "hi"}])
    check("cb канала получил сообщения", chan_loaded == [("docs", [{"seq": 3, "text": "hi"}])])
    check("хвост канала сохранён", s.channel_tail("docs") == [{"seq": 3, "text": "hi"}])

    # устаревший ответ общего tail: пока летел — открыли канал
    s.set_channel(None)
    s.load_initial()
    s.set_channel("music")
    r.deliver_ok(0, {"events": [{"seq": 9}], "next_since": 99})
    check("устаревший общий tail отброшен (открыт канал)", not initial or initial[-1].get("next_since") != 99)
    check("устаревший tail не двигает last_seq", s.last_seq == 12)
    check("устаревший tail не портит кэш общего хвоста", s.general_tail == [{"seq": 1}])

    # свежий общий tail обновляет кэш
    s.set_channel(None)
    s.load_initial()
    r.deliver_ok(0, {"events": [{"seq": 1}, {"seq": 2}], "next_since": 13})
    check("свежий снапшот обновил кэш", s.general_tail == [{"seq": 1}, {"seq": 2}])


def test_channel_stale_guard() -> None:
    print("== ChatSession: устаревшие ответы каналов ==")
    s, r = make_session()
    loaded: list[tuple[str, list]] = []
    s.on_channel_loaded = lambda ch, msgs: loaded.append((ch, msgs))

    s.set_channel("a")
    s.load_channel("a")
    s.set_channel("b")  # пока летел — переключились
    r.deliver_ok(0, [{"seq": 1}])
    check("ответ по каналу 'a' отброшен", not loaded)
    check("хвост 'a' не сохранён", s.channel_tail("a") == [])

    # а нормальный путь работает
    s.set_channel("b")
    s.load_channel("b")
    r.deliver_ok(0, [{"seq": 7, "text": "x"}])
    check("ответ по каналу 'b' доставлен", loaded == [("b", [{"seq": 7, "text": "x"}])])


def test_refresh_channel_tail() -> None:
    print("== ChatSession: тихий дифф хвоста канала ==")
    s, r = make_session()
    check("без канала refresh не шлёт запрос", (s.refresh_channel_tail(), r.pending == 0)[1])

    s.set_channel("chat")
    s._channel_tails["chat"] = [
        {"seq": 1, "text": "a"},
        {"seq": 2, "text": "b"},
    ]
    diffs: list[tuple[str, object]] = []
    s.on_channel_diff = lambda ch, d: diffs.append((ch, d))

    # без изменений — cb не дёргается
    s.refresh_channel_tail()
    r.deliver_ok(0, [{"seq": 1, "text": "a"}, {"seq": 2, "text": "b"}])
    check("без изменений cb не приходит", not diffs)

    # добавление + правка + удаление
    s.refresh_channel_tail()
    r.deliver_ok(0, [{"seq": 2, "text": "B"}, {"seq": 3, "text": "c"}])
    check("cb пришёл", len(diffs) == 1)
    d = diffs[0][1]
    check("diff: added=[3]", [m["seq"] for m in d.added] == [3])
    check("diff: changed=[2]", [m["seq"] for m in d.changed] == [2])
    check("diff: removed=[1]", d.removed == [1])
    check("хвост обновлён", s.channel_tail("chat") == [{"seq": 2, "text": "B"}, {"seq": 3, "text": "c"}])

    # переключение канала во время запроса — дифф отбрасывается
    s.refresh_channel_tail()
    s.set_channel(None)
    r.deliver_ok(0, [{"seq": 9, "text": "z"}])
    check("дифф после смены канала отброшен", len(diffs) == 1)
    check("хвост старого канала не тронут чужим ответом", s.channel_tail("chat") == [{"seq": 2, "text": "B"}, {"seq": 3, "text": "c"}])

    # ошибка сети тихо глотается
    s.set_channel("chat")
    s.refresh_channel_tail()
    r.deliver_err(0)
    check("ошибка refresh тихо гасится", len(diffs) == 1)


def test_after_message_action() -> None:
    print("== ChatSession: after_message_action ==")
    s, r = make_session()
    s.after_message_action()
    check("в общем чате → мгновенный дельта-poll", r.pending == 1)
    r.deliver_ok(0, {"events": [], "next_since": 1})

    s.set_channel("chat")
    s.after_message_action()
    check("в канале → тихий refresh хвоста", r.pending == 1)


# ── ChatSession: типинг / онлайн / дешифровка ────────────────────────────────


def test_typing_and_online() -> None:
    print("== ChatSession: типинг и онлайн ==")
    s, _r = make_session(my_name="Я")

    s.note_typing("Я")  # своё — игнорируется
    check("свой типинг не пишется", s.active_typers() == [])
    s.note_typing("Аня", now=100.0)
    s.note_typing("Боря", now=100.0)
    check("двое печатают", s.active_typers(now=100.0) and set(s.active_typers(now=100.0)) == {"Аня", "Боря"})
    check("свежие не протухли", s.active_typers(now=104.9) == ["Аня", "Боря"] or set(s.active_typers(now=104.9)) == {"Аня", "Боря"})
    check("протухшие зачищены (5с)", s.active_typers(now=106.0) == [])

    check("первый should_send_typing → True", s.should_send_typing(now=10.0) is True)
    check("сразу второй → False (троттлинг 2с)", s.should_send_typing(now=11.0) is False)
    check("через 2с → снова True", s.should_send_typing(now=12.1) is True)
    s.set_connected(False)
    check("без подключения typing не шлётся", s.should_send_typing(now=10.0) is False)

    check("онлайн: свои первые", s.sort_online(["boris", "Я", "Anna"]) == ["Я", "Anna", "boris"])


def test_decrypt_text() -> None:
    print("== ChatSession: дешифровка текста ==")
    s, _r = make_session()
    check("plain текст проходит насквозь", s.decrypt_text({"seq": 1, "text": "привет"}) == "привет")
    dec = s.decrypt_text({"seq": 2, "text": "шифр", "encrypted": True, "iv": "abc"})
    check("неудача расшифровки → плейсхолдер", "не удалось расшифровать" in dec)
    check("пустой текст → пусто", s.decrypt_text({"seq": 3, "text": ""}) == "")


def test_reset() -> None:
    print("== ChatSession: reset при отключении ==")
    with tempfile.TemporaryDirectory() as _td:
        s, r = make_session()
        s.set_channel("chat")
        s._channel_tails["chat"] = [{"seq": 1}]
        s.note_typing("Аня", now=1.0)
        s.do_poll()
        s.reset()
        check("канал сброшен в общий", s.current_channel is None)
        check("хвосты очищены", s.channel_tail("chat") == [])
        check("типинг очищен", s.active_typers(now=1.1) == [])
        check("last_seq обнулён", s.last_seq == 0)
        check("кэш общего хвоста очищен", s.general_tail == [])
        check("in-flight запрос при reset не крэшится", r.pending == 1)


def main() -> int:
    print("== Ядро чата: lib/client_core/chat_session.py ==\n")
    test_diff_tail()
    test_merge_delta_into()
    test_interpret_dispatch()
    test_polling()
    test_load_initial()
    test_channel_stale_guard()
    test_refresh_channel_tail()
    test_after_message_action()
    test_typing_and_online()
    test_decrypt_text()
    test_reset()
    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
