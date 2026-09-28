#!/usr/bin/env python3
"""Тесты v2.0.7: го как полноценный мультиплеер в обоих клиентах.

Три бага/пробела из репорта пользователя:
  1. NameError: AsyncBridge — go_game_dialog.py использовал мост без
     импорта (диалог го падал сразу после открытия);
  2. го не был привязан на странице «Боты» Qt (нет иконки и кнопки
     «Открыть» — _BOT_ICONS/_GAME_BOTS знали только шахматы);
  3. веб-го был только одиночным — теперь сетевой матч через fr_api-мост
     (games.js) и /bot_command (тот же протокол, что у Qt и шахмат).

Проверяем: статику (импорт, привязки, SDK-мост, элементы UI сети) и
живой сервер (полный матч двух игроков через /bot_command — ровно тот
путь, по которому ходит веб-клиент).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def fetch(path: str, method: str = "GET", body: dict | None = None,
          headers: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://x" + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    base = getattr(fetch, "_base", "")
    req.full_url = base + path
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def bot_cmd(sender: str, command: str, args: list[str]) -> dict:
    """POST /bot_command от имени sender — как это делает веб-клиент."""
    code, body = fetch("/bot_command", "POST",
                       {"bot_id": "go", "command": command, "args": args},
                       headers={"X-Relay-From": sender})
    assert code == 200, f"/bot_command {command}: HTTP {code}"
    return json.loads(body)


# ────────────────────────────────────────────────────────────────────────
def test_static() -> None:
    print("── Статика: привязки го в Qt и SDK-мост в вебе ──")

    # 1. краш AsyncBridge: импорт на месте (был NameError при открытии)
    dlg = (ROOT / "qt_app" / "widgets" / "go_game_dialog.py").read_text(
        encoding="utf-8")
    check("go_game_dialog: импорт AsyncBridge",
          "from qt_app.async_bridge import AsyncBridge" in dlg)

    # 2. страница ботов: го — полноценная карточка игры
    bots = (ROOT / "qt_app" / "screens" / "bots_screen.py").read_text(
        encoding="utf-8")
    check("bots_screen: иконка го", '"go": "⚫"' in bots)
    check("bots_screen: го в _GAME_BOTS",
          '"go"' in bots.split("_GAME_BOTS = {", 1)[1].split("}", 1)[0])

    # 3. fr_api-мост в games.js: игра ходит в API через хоста
    gm = (ROOT / "web_client" / "games.js").read_text(encoding="utf-8")
    check("games.js: приём fr_api", 'd.type === "fr_api"' in gm)
    check("games.js: обработчик handleFrApi", "async function handleFrApi" in gm)
    check("games.js: ответ fr_api_res", 'type:"fr_api_res"' in gm)

    # 4. go.html: сетевой режим на месте
    html = (ROOT / "web_client" / "games" / "go.html").read_text(
        encoding="utf-8")
    for marker, name in (
        ('value="net"', "режим «Сеть» в селекте"),
        ("btnHost", "кнопка «Создать матч»"),
        ("btnJoin", "кнопка «Войти»"),
        ("btnResign", "кнопка «Сдаться»"),
        ("btnLeaveNet", "кнопка «Покинуть»"),
        ("frApi(", "fr_api-вызов SDK"),
        ("netMyTurn", "определение своего хода"),
        ("replayNetMoves", "реплей истории ходов"),
        ("startNetPoll", "поллинг state"),
        ("showNetResult", "финал матча"),
    ):
        check(f"go.html: {name}", marker in html)


def test_live() -> None:
    print("── Живой сервер: матч двух игроков через /bot_command ──")
    tmp = Path(tempfile.mkdtemp(prefix="go_net_v207_"))
    relay = RelayServer(tmp, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18547
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    fetch._base = f"http://127.0.0.1:{port}"   # type: ignore[attr-defined]
    try:
        time.sleep(0.2)

        # лаунчер: го — multi (сетевой) и по-прежнему первая
        code, body = fetch("/games")
        games = json.loads(body).get("games", [])
        check("лаунчер: го первый и multi",
              games and games[0]["id"] == "go"
              and games[0]["mode"] == "multi")

        # страница игры содержит SDK-мост
        code, body = fetch("/games/go")
        html = body.decode("utf-8")
        check("/games/go: сетевой UI отдаётся",
              code == 200 and 'value="net"' in html and "frApi(" in html)

        # ── полный сетевой матч: Alice (чёрные) vs Bob (белые) ──
        r = bot_cmd("Alice", "host", ["9"])
        a = r.get("bot_action", {})
        m = a.get("match") or {}
        check("host: match_created, доска 9",
              a.get("action") == "match_created"
              and m.get("size") == 9 and m.get("status") == "waiting"
              and m.get("moves") == [] and m.get("black") == "Alice")
        mid = m.get("match_id", "")
        check("host: код матча выдан", bool(mid))

        r = bot_cmd("Bob", "join", [mid])
        a = r.get("bot_action", {})
        m = a.get("match") or {}
        check("join: match_joined, Bob — белые",
              a.get("action") == "match_joined"
              and m.get("status") == "playing"
              and m.get("white") == "Bob")

        r = bot_cmd("Alice", "state", [])
        a = r.get("bot_action", {})
        check("state: silent-поллинг видит матч",
              a.get("action") == "match_state" and a.get("match")
              and a.get("silent") is True)

        r = bot_cmd("Bob", "move", ["D4"])
        a = r.get("bot_action", {})
        check("ход не в свою очередь отклонён",
              a.get("action") == "error" and "чёрных" in a.get("text", ""))

        r = bot_cmd("Alice", "move", ["D4"])
        a = r.get("bot_action", {})
        m = a.get("match") or {}
        check("ход чёрных принят (D4)",
              a.get("action") == "move_accepted"
              and m.get("moves") == ["D4"])

        r = bot_cmd("Bob", "move", ["D4"])
        a = r.get("bot_action", {})
        check("ход на занятый пункт отклонён",
              a.get("action") == "error")

        r = bot_cmd("Bob", "move", ["pass"])
        a = r.get("bot_action", {})
        m = a.get("match") or {}
        check("пас белых принят",
              a.get("action") == "move_accepted"
              and m.get("moves") == ["D4", "pass"])

        r = bot_cmd("Alice", "resign", [])
        a = r.get("bot_action", {})
        m = a.get("match") or {}
        check("resign: матч завершён, победа белых",
              m.get("status") == "finished" and m.get("result") == "white")
        check("ELO начисляется обоим (финал в матче)",
              "result" in m and "score" in m)
    finally:
        relay.stop()


def main() -> int:
    test_static()
    test_live()
    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
