"""Тесты ядра шахматного матча (lib/client_core/chess_match.py) — БЕЗ PySide6.

Фаза 2 отделения UI от логики: ChessMatch переехал из qt_app/widgets/
chess_game_dialog.py. Проверяем политику сохранения результата (ИИ/PVP/NET),
страховочный опрос game over, сетевой протокол матча (host/join/move/state/
resign/leave), разбор bot_action и таблицу рейтинга ELO.

Особенность относительно test_chat_session.py: здесь фейковый runner
ИСПОЛНЯет переданную функцию — сетевой слой подменяется на FakeClient
(lib.client.send_bot_command_full патчится), поэтому можно проверять и
какие команды уходят на сервер, и как ядро разбирает ответы.

Паттерн проверок: снапшот len(fake.requests) → действие → drain() →
проверки по индексу. Так каскадные вызовы (например, авто-refresh
рейтинга после сохранения результата) не путают индексацию.

Запуск: python tests/test_chess_match.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import client
from lib.client_core.chess_match import (
    MODE_AI,
    MODE_NET,
    MODE_PVP,
    ChessMatch,
    format_leaderboard,
    outcome_for_player,
    should_leave_on_close,
)

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


class FakeRunner:
    """Синхронная замена AsyncBridge.run: копит вызовы, исполняет fn вручную
    (сеть подменена FakeClient — исполнения безопасны)."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object]] = []  # (fn, on_success, on_error)

    def __call__(self, fn, on_success=None, on_error=None) -> None:
        self.calls.append((fn, on_success, on_error))

    @property
    def pending(self) -> int:
        return len(self.calls)

    def deliver_ok(self, idx: int = 0) -> None:
        fn, on_success, _on_error = self.calls.pop(idx)
        if on_success is not None:
            on_success(fn())

    def deliver_err(self, idx: int = 0, exc: Exception | None = None) -> None:
        _fn, _on_success, on_error = self.calls.pop(idx)
        if on_error is not None:
            on_error(exc or OSError("network down"))

    def drain(self) -> None:
        """Исполняет всё накопленное (включая каскады от колбэков)."""
        while self.pending:
            self.deliver_ok(0)


class FakeClient:
    """Подменяет lib.client.send_bot_command_full: пишет запросы, отдаёт
    заготовленные ответы по очереди (или дефолтный пустой)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self.default_response: dict = {"ok": True, "bot_response": "", "bot_action": None}

    def __call__(self, base_url, bot_id, command="", args=None, name="", access_key=""):
        self.requests.append(
            {"command": command, "args": list(args or []), "name": name, "bot_id": bot_id}
        )
        if self.responses:
            return self.responses.pop(0)
        return dict(self.default_response)

    @property
    def last(self) -> dict:
        return self.requests[-1]


def make_match(base_url: str = "http://test", player_name: str = "Я"):
    runner = FakeRunner()
    match = ChessMatch(
        base_url=base_url, player_name=player_name, access_key="key", bot_id="chess", runner=runner
    )
    return match, runner


def action(act: str, **kw) -> dict:
    """Ответ сервера с bot_action (как это делает /bot_command)."""
    return {"ok": True, "bot_response": "", "bot_action": {"action": act, **kw}}


# ── Чистые функции ───────────────────────────────────────────────────────────


def test_pure_functions() -> None:
    print("== Чистые функции: исход для игрока, топ, leave при закрытии ==")
    check("win_white + белые → win", outcome_for_player("win_white", True) == "win")
    check("win_white + чёрные → loss", outcome_for_player("win_white", False) == "loss")
    check("win_black + чёрные → win", outcome_for_player("win_black", False) == "win")
    check("win_black + белые → loss", outcome_for_player("win_black", True) == "loss")

    scores = [
        {"name": "Алиса", "score": 1350, "wins": 5, "losses": 1, "draws": 2},
        {"name": "Боря", "score": 1200, "wins": 0, "losses": 0, "draws": 0},
    ]
    lines = format_leaderboard(scores)
    check("первый с короной", lines[0] == "👑 Алиса — 1350 (5W/1L/2D)")
    check("второй с номером", lines[1] == "2. Боря — 1200 (0W/0L/0D)")

    many = [{"name": f"p{i}", "score": 1000 + i, "wins": 0, "losses": 0, "draws": 0} for i in range(12)]
    check("топ ограничен 10 строками", len(format_leaderboard(many)) == 10)
    check("пустой топ — пустой список", format_leaderboard([]) == [])

    check("закрытие в активном NET-матче → leave", should_leave_on_close(MODE_NET, "ABCD", False) is True)
    check("нет матча → не leave", should_leave_on_close(MODE_NET, None, False) is False)
    check("матч завершён → не leave", should_leave_on_close(MODE_NET, "ABCD", True) is False)
    check("режим ИИ → не leave", should_leave_on_close(MODE_AI, "ABCD", False) is False)
    check("PVP → не leave", should_leave_on_close(MODE_PVP, "ABCD", False) is False)


# ── Политика game over ───────────────────────────────────────────────────────


def test_game_over_policy() -> None:
    print("== on_board_game_over: PVP/NET/ИИ и идемпотентность ==")
    m, r = make_match()

    # уже сохранено — ничего не делаем
    m.result_saved = True
    v = m.on_board_game_over("win_white", "white", mode=MODE_AI, player_side=True)
    check("повторный game over подавлен", v.reason == "already_saved" and r.pending == 0)
    m.result_saved = False

    # PVP: ELO не трогаем
    v = m.on_board_game_over("win_white", "white", mode=MODE_PVP, player_side=True)
    check("PVP: ничего не отправляется", v.reason == "pvp" and r.pending == 0)
    check("PVP: флаг поставлен", m.result_saved is True)
    m.reset_result_flag()

    # NET: сервер уже сохранил — только обновить таблицу
    v = m.on_board_game_over("win_black", "black", mode=MODE_NET, player_side=True)
    check("NET: помечено сохранённым + refresh", v.reason == "net" and v.refresh and m.result_saved)
    n = len(fake.requests)
    r.drain()
    check("NET: уходит getscores", r.pending == 0 and fake.requests[n]["command"] == "getscores")
    m.reset_result_flag()

    # ИИ: ничья
    n = len(fake.requests)
    v = m.on_board_game_over("draw", None, mode=MODE_AI, player_side=True)
    check("ИИ ничья: send=draw", v.send == "draw" and v.reason == "ai")
    r.drain()
    check("ИИ: команда result с draw", fake.requests[n]["command"] == "result" and fake.requests[n]["args"] == ["draw"])
    m.reset_result_flag()

    # ИИ: победа/поражение относительно стороны игрока
    n = len(fake.requests)
    v = m.on_board_game_over("win_white", "white", mode=MODE_AI, player_side=True)
    r.drain()
    check("ИИ белые + win_white → win", v.send == "win" and fake.requests[n]["args"] == ["win"])
    m.reset_result_flag()

    n = len(fake.requests)
    v = m.on_board_game_over("win_white", "white", mode=MODE_AI, player_side=False)
    r.drain()
    check("ИИ чёрные + win_white → loss", v.send == "loss" and fake.requests[n]["args"] == ["loss"])
    m.reset_result_flag()

    n = len(fake.requests)
    v = m.on_board_game_over("win_black", "black", mode=MODE_AI, player_side=False)
    r.drain()
    check("ИИ чёрные + win_black → win", v.send == "win" and fake.requests[n]["args"] == ["win"])

    # идемпотентность: второй game over после ИИ-сохранения не шлёт второй result
    n_before = len(fake.requests)
    v = m.on_board_game_over("win_black", "black", mode=MODE_AI, player_side=False)
    check("ИИ: второй game over подавлен", v.reason == "already_saved" and len(fake.requests) == n_before)


def test_check_game_over_fallback() -> None:
    print("== check_game_over_fallback: страховка по состоянию движка ==")
    m, r = make_match()

    v = m.check_game_over_fallback(
        game_over=False, mode=MODE_AI, player_side=True, is_checkmate=False, white_to_move=True
    )
    check("партия не завершена → ничего", v.reason == "nothing_to_do" and r.pending == 0)

    m.result_saved = True
    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_AI, player_side=True, is_checkmate=True, white_to_move=True
    )
    check("уже сохранено → ничего", v.reason == "nothing_to_do" and r.pending == 0)
    m.result_saved = False

    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_PVP, player_side=True, is_checkmate=True, white_to_move=True
    )
    check("PVP: флаг, без запросов", v.reason == "pvp" and r.pending == 0)
    m.reset_result_flag()

    n = len(fake.requests)
    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_NET, player_side=True, is_checkmate=True, white_to_move=True
    )
    check("NET: refresh рейтинга", v.reason == "net" and v.refresh)
    r.drain()
    check("NET: это getscores", r.pending == 0 and fake.requests[n]["command"] == "getscores")
    m.reset_result_flag()

    # Мат белым (white_to_move=True — сейчас ход белых и они стоят под матом).
    n = len(fake.requests)
    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_AI, player_side=True, is_checkmate=True, white_to_move=True
    )
    r.drain()
    check("ИИ: мат белым + игрок белые → loss", v.send == "loss" and fake.requests[n]["args"] == ["loss"])
    m.reset_result_flag()

    n = len(fake.requests)
    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_AI, player_side=False, is_checkmate=True, white_to_move=True
    )
    r.drain()
    check("ИИ: мат белым + игрок чёрные → win", v.send == "win" and fake.requests[n]["args"] == ["win"])
    m.reset_result_flag()

    n = len(fake.requests)
    v = m.check_game_over_fallback(
        game_over=True, mode=MODE_AI, player_side=True, is_checkmate=False, white_to_move=True
    )
    r.drain()
    check("ИИ: не мат (пат) → draw", v.send == "draw" and fake.requests[n]["args"] == ["draw"])


def test_result_saved_response() -> None:
    print("== Ответ на result: показ своего места + авто-refresh ==")
    m, r = make_match()
    stats: list[str] = []
    m.on_my_stats = stats.append

    n = len(fake.requests)
    fake.responses.append(action("score_saved", rank=2, best=1267, text="ok"))
    m.on_board_game_over("draw", None, mode=MODE_AI, player_side=True)
    r.drain()
    check("result отправлен", fake.requests[n]["command"] == "result" and fake.requests[n]["args"] == ["draw"])
    check("своя статистика показана", stats == ["Твой ELO: 1267   ·   место: #2"])
    check("каскадный refresh рейтинга", fake.requests[n + 1]["command"] == "getscores")
    check("каскад отработал полностью", r.pending == 0)

    # не score_saved — статистика не трогается, но refresh всё равно идёт
    m.reset_result_flag()
    n = len(fake.requests)
    fake.responses.append(action("error", text="нет"))
    m.on_board_game_over("draw", None, mode=MODE_AI, player_side=True)
    r.drain()
    check("ошибка сохранения: статистика не тронута", len(stats) == 1)
    check("ошибка сохранения: refresh всё равно идёт", fake.requests[n + 1]["command"] == "getscores")

    # без подключения result молча не отправляется
    m2, r2 = make_match(base_url="", player_name="")
    v = m2.on_board_game_over("draw", None, mode=MODE_AI, player_side=True)
    check("нет сервера: verdict есть, запроса нет", v.send == "draw" and r2.pending == 0)
    check("нет сервера: флаг всё равно поставлен", m2.result_saved is True)


# ── Сетевой матч: host / join ────────────────────────────────────────────────


def test_host_join() -> None:
    print("== Сетевой матч: host и join ==")
    m, r = make_match(base_url="", player_name="")
    errs: list[str] = []
    m.on_net_error = errs.append
    m.host()
    r.drain()
    # 1:1 с оригиналом: _net_command при отсутствии подключения зовёт on_error
    # только если он передан явно; host on_error не передаёт → тихо.
    check("host без сервера: тихо, без запроса", errs == [] and r.pending == 0)

    m, r = make_match()
    ready: list[tuple[bool, str, str]] = []
    statuses: list[str] = []
    m.on_match_ready = lambda c, mid, opp: ready.append((c, mid, opp))
    m.on_status = statuses.append

    n = len(fake.requests)
    m.result_saved = True  # проверим сброс флага при создании матча
    fake.responses.append(action("match_created", match={"match_id": "AB12"}, text="создан"))
    m.host()
    check("host: статус 'Создание матча…'", statuses[-1] == "Создание матча…")
    r.drain()
    check("host: уходит команда host", fake.requests[n]["command"] == "host")
    check("host: on_match_ready (белые, код, ожидание)", ready == [(True, "AB12", "Ожидание…")])
    check("host: флаг result_saved сброшен", m.result_saved is False)
    check("host: статус с кодом", "Матч создан" in statuses[-1] and "AB12" in statuses[-1])

    # неудача host
    fake.responses.append(action("error", text="занят"))
    m.host()
    r.drain()
    check("host: неудача → статус с текстом ошибки", statuses[-1] == "⚠ Не удалось создать матч: занят")
    check("host: неудача не готовит доску", len(ready) == 1)

    # join: пустой код не отправляется
    check("join: пустой код → False", m.join("   ") is False and r.pending == 0)

    # join: нормализация кода
    n = len(fake.requests)
    m.result_saved = True
    fake.responses.append(action("match_joined", match={"match_id": "AB12", "white": "Алиса"}, text="ок"))
    m.join(" ab12 ")
    r.drain()
    check("join: код нормализован (trim+UPPER)", fake.requests[n]["command"] == "join" and fake.requests[n]["args"] == ["AB12"])
    check("join: on_match_ready (чёрные, соперник Алиса)", ready[-1] == (False, "AB12", "Алиса"))
    check("join: флаг сброшен", m.result_saved is False)
    check("join: статус про чёрные", "чёрными" in statuses[-1])

    # join: неудача
    fake.responses.append(action("error", text="не найден"))
    m.join("ZZZZ")
    r.drain()
    check("join: неудача → статус с текстом ошибки", statuses[-1] == "⚠ Не удалось присоединиться: не найден")


# ── Сетевой матч: resign / leave / move ──────────────────────────────────────


def test_resign_leave_move() -> None:
    print("== Сетевой матч: resign, leave, ход ==")
    m, r = make_match()
    states: list[dict] = []
    statuses: list[str] = []
    resets: list[bool] = []
    rejected: list[str] = []
    m.on_match_state = states.append
    m.on_status = statuses.append
    m.on_match_reset = lambda: resets.append(True)
    m.on_move_rejected = rejected.append

    # resign: матч в ответе → применить состояние + refresh
    n = len(fake.requests)
    match_fin_resign = {"match_id": "AB12", "status": "finished", "result": "black", "moves": []}
    fake.responses.append(action("match_finished", match=match_fin_resign, text="Ты сдался"))
    m.resign()
    r.drain()
    check("resign: команда resign", fake.requests[n]["command"] == "resign")
    check("resign: состояние применено", states == [match_fin_resign])
    check("resign: текст статуса", statuses[-1] == "Ты сдался")
    check("resign: рейтинг обновлён", fake.requests[n + 1]["command"] == "getscores" and r.pending == 0)

    # resign: ответ без match — только статус (+ refresh, как в _on_net_finished)
    n = len(fake.requests)
    fake.responses.append(action("error", text="нет матча"))
    m.resign()
    r.drain()
    check("resign: без match доска не трогается", len(states) == 1)
    check("resign: статус из ответа", statuses[-1] == "нет матча")
    check("resign: refresh всё равно идёт (1:1 _on_net_finished)", fake.requests[n + 1]["command"] == "getscores")

    # leave: сброс доски
    n = len(fake.requests)
    m.result_saved = True
    fake.responses.append(action("match_left", text="Матч отменён."))
    m.leave()
    r.drain()
    check("leave: команда leave", fake.requests[n]["command"] == "leave")
    check("leave: доска сброшена", resets == [True])
    check("leave: флаг сброшен", m.result_saved is False)
    check("leave: текст статуса", statuses[-1] == "Матч отменён.")

    # leave: без текста — дефолт
    m.leave()
    r.drain()
    check("leave: без текста дефолт 'Матч покинут.'", statuses[-1] == "Матч покинут.")

    # ход: принят
    n = len(fake.requests)
    fake.responses.append(action("move_accepted", match={"moves": ["e2e4"]}))
    m.send_move("e2e4")
    r.drain()
    check("ход: команда move с uci", fake.requests[n]["command"] == "move" and fake.requests[n]["args"] == ["e2e4"])
    check("ход: статус отправлен", statuses[-1] == "✓ Ход отправлен. Жди хода соперника…")

    # ход: отклонён
    fake.responses.append(action("error", text="Сейчас не твой ход"))
    m.send_move("e2e5")
    r.drain()
    check("ход: отклонён → колбэк с текстом", rejected == ["Сейчас не твой ход"])
    check("ход: отклонён не пишет статус сам (это делает вьюха)", statuses[-1] == "✓ Ход отправлен. Жди хода соперника…")

    # ход: матовый — финиш
    n = len(fake.requests)
    match_fin_move = {"match_id": "AB12", "status": "finished", "result": "black", "moves": ["e2e4", "d8h4"]}
    fake.responses.append(action("match_finished", match=match_fin_move, text="🏁 Матч завершён."))
    m.send_move("d8h4")
    r.drain()
    check("ход: финиш применён", states[-1] == match_fin_move)
    check("ход: статус финиша", statuses[-1] == "🏁 Матч завершён.")
    check("ход: рейтинг обновлён", fake.requests[n + 1]["command"] == "getscores" and r.pending == 0)


# ── Сетевой матч: опрос state ────────────────────────────────────────────────


def test_poll_state() -> None:
    print("== poll_state: политика опроса и финал матча ==")
    m, r = make_match(base_url="", player_name="")
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    check("без сервера опрос не идёт", r.pending == 0)

    m, r = make_match()
    states: list[dict] = []
    statuses: list[str] = []
    errs: list[str] = []
    m.on_match_state = states.append
    m.on_status = statuses.append
    m.on_net_error = errs.append

    m.poll_state(is_net_mode=False, game_over=False, net_color=True)
    check("не NET-режим → опроса нет", r.pending == 0)
    m.poll_state(is_net_mode=True, game_over=True, net_color=True)
    check("game_over → опроса нет", r.pending == 0)

    n = len(fake.requests)
    fake.responses.append(action("match_state", match=None, text="нет матча"))
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    r.drain()
    check("опрос: команда state", fake.requests[n]["command"] == "state")
    check("match=None → ничего не применяем", not states and not statuses)

    playing = {"match_id": "AB12", "status": "playing", "result": None, "moves": ["e2e4"]}
    fake.responses.append(action("match_state", match=playing))
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    r.drain()
    check("playing: состояние применено", states == [playing])
    check("playing: статус не пишется", not statuses)

    # финал: победа белыми
    n = len(fake.requests)
    fin_w = {"match_id": "AB12", "status": "finished", "result": "white", "moves": []}
    fake.responses.append(action("match_state", match=fin_w))
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    r.drain()
    check("финиш: состояние применено", states[-1] == fin_w)
    check("финиш: result_saved=True (сервер сохранил ELO)", m.result_saved is True)
    check("финиш: победа", statuses[-1] == "🏆 Ты победил!")
    check("финиш: рейтинг обновлён", fake.requests[n + 1]["command"] == "getscores" and r.pending == 0)

    # финал: поражение белыми
    fake.responses.append(action("match_state", match={"status": "finished", "result": "black"}))
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    r.drain()
    check("финиш: поражение", statuses[-1] == "🏳 Поражение.")

    # финал: ничья (играем чёрными — не влияет на текст ничьей)
    fake.responses.append(action("match_state", match={"status": "finished", "result": "draw"}))
    m.poll_state(is_net_mode=True, game_over=False, net_color=False)
    r.drain()
    check("финиш: ничья", statuses[-1] == "½ Ничья! Матч завершён.")

    # сетевые ошибки опроса тихие
    m.poll_state(is_net_mode=True, game_over=False, net_color=True)
    r.deliver_err(0)
    check("ошибка опроса тихая", not errs and r.pending == 0)


# ── Таблица рейтинга ─────────────────────────────────────────────────────────


def test_leaderboard() -> None:
    print("== Рейтинг ELO: getscores ==")
    m, r = make_match(base_url="", player_name="")
    m.refresh_leaderboard()
    check("без сервера → заглушка без запроса", r.pending == 0)

    m, r = make_match()
    lines: list[list[str]] = []
    no_conn: list[bool] = []
    errors: list[bool] = []
    m.on_leaderboard = lines.append
    m.on_no_connection = lambda: no_conn.append(True)
    m.on_leaderboard_error = lambda: errors.append(True)

    n = len(fake.requests)
    fake.responses.append(action("scores", scores=[]))
    m.refresh_leaderboard()
    r.drain()
    check("команда getscores", fake.requests[n]["command"] == "getscores")
    check("пустой топ → пустой список строк", lines == [[]])

    fake.responses.append(
        action("scores", scores=[{"name": "Алиса", "score": 1350, "wins": 5, "losses": 1, "draws": 2}])
    )
    m.refresh_leaderboard()
    r.drain()
    check("топ отформатирован", lines[-1] == ["👑 Алиса — 1350 (5W/1L/2D)"])

    m.refresh_leaderboard()
    r.deliver_err(0)
    check("ошибка сети → колбэк ошибки", errors == [True])

    # некорректный ответ (bot_action=None) не падает и трактуется как пустой топ
    m.refresh_leaderboard()
    r.drain()
    check("bot_action=None → пустой топ", lines[-1] == [])


# ── Закрытие окна ────────────────────────────────────────────────────────────


def test_leave_on_close() -> None:
    print("== leave_on_close: блокирующий leave при закрытии ==")
    m, _r = make_match()
    m.leave_on_close()
    check("leave уходит на сервер", fake.last["command"] == "leave" and fake.last["args"] == [])

    # исключение сети не роняет закрытие окна
    def _boom(*a, **kw):
        raise OSError("connection reset")

    saved = client.send_bot_command_full
    client.send_bot_command_full = _boom
    try:
        m.leave_on_close()
        check("сетевое исключение подавлено", True)
    finally:
        client.send_bot_command_full = saved


# ── Подмена клиента на время всего прогона ───────────────────────────────────

fake = FakeClient()
_saved_client = client.send_bot_command_full


def main() -> int:
    client.send_bot_command_full = fake
    try:
        print("== Ядро шахматного матча: lib/client_core/chess_match.py ==\n")
        test_pure_functions()
        test_game_over_policy()
        test_check_game_over_fallback()
        test_result_saved_response()
        test_host_join()
        test_resign_leave_move()
        test_poll_state()
        test_leaderboard()
        test_leave_on_close()
    finally:
        client.send_bot_command_full = _saved_client
    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
