#!/usr/bin/env python3
"""Тесты v2.0.6: Го (движок + бот-рефери + веб-лаунчер + телеметрия).

Блоки:
  1. Движок (lib/games/go_engine.py): нотация, взятия, самоубийство,
     простое ко, пас/два паса, подсчёт по площади с коми;
  2. Бот-рефери (lib/bots/go.py) напрямую: host/join/move/pass, очерёдность,
     отказ нелегальных ходов, финал двух пасов → счёт + ELO, resign,
     рейтинг getscores, результат против ИИ (result);
  3. Живой сервер: /games ставит Го ПЕРВОЙ карточкой лаунчера,
     /games/go отдаёт HTML с мини-SDK, /bot_command-go → телеметрия
     bot.commands.go + games.opened.go (приборка видит новую игру).

Запуск: python tests/test_go_v205.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.bots.go import GoBot
from lib.games.go_engine import (
    BLACK,
    WHITE,
    GoAI,
    GoEngine,
    GoError,
    format_vertex,
    parse_vertex,
)
from lib.relay_server import RelayServer

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def v(text: str, size: int = 9) -> tuple[int, int]:
    r = parse_vertex(text, size)
    assert r is not None, f"вершина {text} не разобралась"
    return r


# ═════════════════════════ 1. Движок ═══════════════════════════════════
def test_engine() -> None:
    print("— 1. Движок Го: нотация/взятия/ко/суицид/счёт —")

    # нотация: буква I пропускается, номер ряда снизу вверх
    check("parse D4 → (3,5)", v("D4") == (3, 5))
    check("parse A1 → (0,8)", v("A1") == (0, 8))
    check("parse j10 на 9×9 вне доски", parse_vertex("J10", 9) is None)
    check("parse I4 отвергнута (буква I)", parse_vertex("I4", 9) is None)
    check("format roundtrip", format_vertex(*v("D4"), 9) == "D4")

    # взятие одиночного камня: чёрные окружают белую A1
    e = GoEngine(9)
    e.play(*v("A2"), BLACK)
    e.play(*v("A1"), WHITE)   # белая A1: свободы A3/B1
    e.play(*v("E5"), BLACK)
    e.play(*v("E6"), WHITE)
    caps = e.play(*v("B1"), BLACK)   # последняя свобода A1 закрыта
    check("белая A1 взята одним ходом", caps == [(0, 8)])
    check("счётчик взятых чёрными = 1", e.captured[BLACK] == 1)
    check("клетка A1 пуста", e.board[e._idx(0, 8)] == 0)

    # взятие группы: белые A1,A2; чёрные B1,B2 + A3 → сняты обе
    e = GoEngine(9)
    e.play(*v("A1"), WHITE)
    e.play(*v("B1"), BLACK)
    e.play(*v("A2"), WHITE)
    e.play(*v("B2"), BLACK)
    caps = e.play(*v("A3"), BLACK)
    check("группа из 2 камней снята целиком",
          len(caps) == 2 and set(caps) == {(0, 8), (0, 7)})
    check("взято чёрными = 2", e.captured[BLACK] == 2)

    # самоубийство запрещено: точка без свобод, враг вокруг
    e = GoEngine(9)
    for mv, c in (("B1", WHITE), ("A2", WHITE), ("C2", WHITE)):
        e.play(*v(mv), c)
    # A1 окружена белыми B1/A2... C2 тут не при делах, но пусть белых больше
    try:
        e.play(*v("A1"), BLACK)
        suicide = False
    except GoError:
        suicide = True
    check("ход в чужое окружение отвергнут (суицид)", suicide)

    # простое ко: нельзя сразу отбить. Расклад (учебная «кошка» у края):
    #   row2: A2⚫ C2⚪ D2⚫      row1: A1⚫ B1⚪ C1? D1⚪
    #   чёрные C1 берут одиночную B1; отбой B1 тут же запрещён.
    e = GoEngine(9)
    for mv, c in (("A2", BLACK), ("B2", BLACK), ("D2", BLACK),
                  ("B1", WHITE), ("C2", WHITE), ("D1", WHITE)):
        e.play(*v(mv), c)
    e.play(*v("A1"), BLACK)   # опора угла: A1+A2 — группа с liberty A3
    caps = e.play(*v("C1"), BLACK)
    check("ко-взятие: B1 снята", caps == [(1, 8)])
    check("ко-точка установлена (B1)", e.ko_point == (1, 8))
    try:
        e.play(*v("B1"), WHITE)
        ko_ok = False
    except GoError:
        ko_ok = True
    check("немедленный отбой отвергнут (ко)", ko_ok)
    e.play(*v("E5"), WHITE)     # ко разрешился любым другим ходом
    e.play(*v("E6"), BLACK)
    try:
        e.play(*v("B1"), WHITE)
        ko_ok = True
    except GoError:
        ko_ok = False
    check("после размена отбить можно", ko_ok)

    # пас/два паса/подсчёт: два камня на доске (D4 чёрные, E6 белые)
    e = GoEngine(9)
    e.play(*v("D4"), BLACK)
    e.play(*v("E6"), WHITE)
    e.pass_turn(BLACK)
    check("один пас — партия жива", not e.game_over)
    e.pass_turn(WHITE)
    check("два паса — партия завершена", e.game_over)
    sc = e.area_score()
    check("камни посчитаны (1 : 1)", sc["black"] == 1 and sc["white"] == 1)
    check("коми даёт белым победу 7.5 : 1", sc["winner"] == "white")
    check("перевес = 6.5", abs(sc["margin"] - 6.5) < 1e-9)

    # территория: пустой угол A1 окружён только чёрными → чёрным
    e = GoEngine(9)
    e.play(*v("A2"), BLACK)
    e.play(*v("E5"), WHITE)
    e.play(*v("B1"), BLACK)
    e.pass_turn(WHITE)
    e.pass_turn(BLACK)
    sc = e.area_score()
    check("угол A1 причислен чёрным (2 камня + 1 территория)",
          sc["black"] == 3)

    # очерёдность ходов: чёрные начинают, ходы чередуются
    e = GoEngine(9)
    check("первый ход — чёрные", e.to_move() == BLACK)
    e.play(*v("D4"), BLACK)
    check("второй — белые", e.to_move() == WHITE)

    # ИИ делает легальный ход на пустой доске
    e = GoEngine(9)
    mv = GoAI.choose_move(e, WHITE)
    check("ИИ нашёл ход (белые после хода чёрных)",
          mv is not None and e.is_legal(mv[0], mv[1], WHITE))


# ═════════════════════════ 2. Бот-рефери ═══════════════════════════════
def test_bot() -> None:
    print("— 2. GoBot: host/join/move/pass/ELO/resign —")
    tmp = Path(tempfile.mkdtemp(prefix="go_bot_"))
    bot = GoBot(tmp)

    r = bot.process_command("Алиса", "", [])
    check("!go открывает игру (open_game)",
          isinstance(r, dict) and r.get("action") == "open_game")
    check("meta маршрутизирует по game=go", bot.get_meta().get("game") == "go")
    check("dispatcher матчит !go", bot.matches_command("!go"))
    check("dispatcher матчит !go move D4", bot.matches_command("!go move D4"))
    pc = bot.parse_command("!go host 13")
    check("parse: host 13 → ('host', ['13'])", pc == ("host", ["13"]))

    # матч: Алиса (чёрные) + Борис (белые)
    r = bot.process_command("Алиса", "host", ["9"])
    check("match_created", r.get("action") == "match_created")
    mid = r["match"]["match_id"]
    check("хост — чёрные", r["match"]["black"] == "Алиса")
    r = bot.process_command("Борис", "join", [mid])
    check("match_joined", r.get("action") == "match_joined")
    check("Борис — белые", r["match"]["white"] == "Борис")

    # очерёдность: белые не могут ходить первыми
    r = bot.process_command("Борис", "move", ["D4"])
    check("белые не ходят первыми", r.get("action") == "error"
          and "чёрных" in r.get("text", ""))

    # нормальный ход + отказ занятого пункта
    r = bot.process_command("Алиса", "move", ["D4"])
    check("ход чёрных принят", r.get("action") == "move_accepted")
    check("история: ['D4']", r["match"]["moves"] == ["D4"])
    r = bot.process_command("Борис", "move", ["D4"])
    check("ход на занятый пункт отклонён", r.get("action") == "error")
    r = bot.process_command("Борис", "move", ["Z99"])
    check("мусорная вершина отклонена", r.get("action") == "error")

    # партия до двух пасов: Борис E5, пасы обоих → финал со счётом
    bot.process_command("Борис", "move", ["E5"])
    bot.process_command("Алиса", "move", ["pass"])
    r = bot.process_command("Борис", "move", ["pass"])
    check("два паса → match_finished", r.get("action") == "match_finished")
    check("победа белым по коми", r["match"]["result"] == "white")
    check("счёт в матче заполнен", "коми" in (r["match"].get("score") or ""))
    check("матч завершён", r["match"]["status"] == "finished")

    # ELO: у обоих появились записи, белые (победившие) выше 1200
    players = bot.get_data("players", {})
    check("ELO-записи обеим сторонам",
          "Алиса" in players and "Борис" in players)
    check("победившие белые > 1200", players["Борис"]["elo"] > 1200)
    check("проигравшие чёрные < 1200", players["Алиса"]["elo"] < 1200)

    # resign во втором матче: сначала освобождаемся от завершённого
    r = bot.process_command("Алиса", "leave", [])
    check("выход от завершённого матча освобождает",
          r.get("action") == "match_left")
    r = bot.process_command("Алиса", "host", [])
    check("новый матч после финала создаётся",
          r.get("action") == "match_created")
    mid2 = r["match"]["match_id"]
    bot.process_command("Борис", "join", [mid2])
    r = bot.process_command("Алиса", "resign", [])
    check("сдача завершает матч", r.get("action") == "match_finished")
    check("победа белым после сдачи", r["match"]["result"] == "white")

    # state (поллинг, silent) и getscores
    r = bot.process_command("Алиса", "state", [])
    check("state: silent=True", r.get("silent") is True)
    r = bot.process_command("Борис", "getscores", [])
    check("getscores отдаёт таблицу", r.get("action") == "scores"
          and any(s["name"] == "Борис" for s in r["scores"]))

    # результат против ИИ (из окна) → score_saved
    elo_before = bot._get_player("Алиса")["elo"]
    r = bot.process_command("Алиса", "result", ["win"])
    check("result win → score_saved", r.get("action") == "score_saved")
    check("ELO Алисы вырос после победы над ИИ",
          bot._get_player("Алиса")["elo"] > elo_before)

    # завершённые матчи не блокируют новые (leave/фильтр include_finished)
    bot.process_command("Алиса", "leave", [])
    r = bot.process_command("Алиса", "host", [])
    check("после leave снова можно хостить",
          r.get("action") == "match_created")


# ═════════════════════════ 3. Живой сервер + веб + телеметрия ═════════
def test_web_live() -> None:
    print("— 3. Живой сервер: /games с Го первой, HTML, телеметрия —")
    tmp = Path(tempfile.mkdtemp(prefix="go_web_"))
    relay = RelayServer(tmp, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18577
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"

    def fetch(path: str, method: str = "GET", body: dict | None = None,
              headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        for k, val in (headers or {}).items():
            req.add_header(k, val)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read(), dict(err.headers)

    try:
        time.sleep(0.2)
        import urllib.error

        # лаунчер: Го — ПЕРВАЯ карточка
        code, body, _ = fetch("/games")
        games = json.loads(body).get("games", [])
        check("/games: 200 и список не пуст", code == 200 and games)
        check("Го ПЕРВАЯ в лаунчере", games[0]["id"] == "go")
        # (v2.0.7) у го появился сетевой матч — режим в реестре стал multi
        check("Го available (файл доехал)",
              games[0]["available"] is True and games[0]["mode"] == "multi")
        check("2048/Сапёр на месте следом",
              [g["id"] for g in games[1:]] == ["g2048", "minesweeper"])

        # страница игры: самодостаточный HTML с мини-SDK
        code, body, hdr = fetch("/games/go")
        html = body.decode("utf-8")
        check("/games/go: 200 text/html", code == 200
              and "text/html" in hdr.get("Content-Type", ""))
        check("go.html: мини-SDK (fr_hello/fr_close)",
              "fr_hello" in html and "fr_close" in html)
        check("go.html: движок на месте (ко/пас/подсчёт/ИИ)",
              all(k in html for k in ("koPoint", "passTurn", "areaScore",
                                      "aiChooseMove")))
        check("go.html: рекорды в localStorage", "wr_go_" in html)

        # телеметрия видит игру: games.list + games.opened.go
        code, body, _ = fetch("/telemetry/summary")
        counters = json.loads(body).get("counters", {})
        check("телеметрия: games.list зафиксирован",
              counters.get("games.list", 0) >= 1)
        check("телеметрия: games.opened.go зафиксирован",
              counters.get("games.opened.go", 0) >= 1)

        # бот через HTTP: !go meta → телеметрия bot.commands.go
        h = {"X-Relay-From": "GoTester"}
        code, body, _ = fetch("/bot_command", "POST",
                              {"bot_id": "go", "command": "meta", "args": []},
                              headers=h)
        check("/bot_command go meta: 200, action=meta",
              code == 200
              and json.loads(body).get("bot_action", {}).get("action") == "meta")
        code, body, _ = fetch("/telemetry/summary")
        counters = json.loads(body).get("counters", {})
        check("телеметрия: bot.commands.go зафиксирован",
              counters.get("bot.commands.go", 0) >= 1)
    finally:
        relay.stop()


def main() -> int:
    test_engine()
    test_bot()
    test_web_live()
    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
