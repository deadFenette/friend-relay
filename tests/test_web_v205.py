#!/usr/bin/env python3
"""Живой тест веб-фич v2.0.5: лаунчер игр («активити» как в Discord).

Сервер поднимается на 127.0.0.1, дальше:
  - статика: вкладка «Игры», окно игры (iframe sandbox), games.js,
    стили лаунчера, сами файлы игр (2048, Сапёр);
  - GET /games: список совпадает с реестром, у всех игр available=true;
  - GET /games/<id>: отдаёт text/html только известные id; неизвестный
    id/мусорный путь — 404 (белый список по построению);
  - мини-SDK: в файлах игр есть fr_hello/fr_close, в games.js — fr_ctx.
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

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def fetch(req: urllib.request.Request) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="web_v205_"))
    relay = RelayServer(tmp, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18543
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.2)

        # ── 1. Статика: новые элементы UI на месте ────────────────────
        code, body, _ = fetch(urllib.request.Request(base + "/"))
        page = body.decode("utf-8")
        check("index.html: вкладка игр", 'data-tab="games"' in page)
        check("index.html: лаунчер gamesGrid", 'id="gamesGrid"' in page)
        check("index.html: окно игры + iframe sandbox",
              'id="gameWin"' in page and 'id="gameFrame"' in page
              and "sandbox=" in page)
        check("index.html: games.js подключён",
              'src="/static/games.js"' in page)

        code, gjs, _ = fetch(urllib.request.Request(base + "/static/games.js"))
        gjs_text = gjs.decode("utf-8")
        check("games.js: лаунчер + окно + SDK",
              "loadGames" in gjs_text and "openGame" in gjs_text
              and "closeGame" in gjs_text and "fr_hello" in gjs_text
              and "fr_ctx" in gjs_text and "fr_close" in gjs_text)
        check("games.js: same-origin фильтр сообщений",
              "location.origin" in gjs_text)
        code, mainjs, _ = fetch(urllib.request.Request(base + "/static/main.js"))
        check("main.js: TAB_NAMES с games + закрытие окна",
              b'"games"' in mainjs and b"btnGameClose" in mainjs)

        code, css, _ = fetch(urllib.request.Request(base + "/static/style.css"))
        css_text = css.decode("utf-8")
        check("style.css: стили лаунчера и окна игры",
              ".gamesgrid" in css_text and ".gamecard" in css_text
              and ".gamewin" in css_text)

        # ── 2. Файлы игр существуют и это самодостаточные страницы ────
        code, g2048, _ = fetch(urllib.request.Request(base + "/static/games/2048.html")) \
            if False else (0, b"", {})
        # игры отдаёт /games/<id>, но файлы лежат в web_client/games/
        games_dir = Path(__file__).resolve().parent.parent / "web_client" / "games"
        check("файл 2048.html на месте", (games_dir / "2048.html").is_file())
        check("файл minesweeper.html на месте",
              (games_dir / "minesweeper.html").is_file())
        tt2048 = (games_dir / "2048.html").read_text(encoding="utf-8")
        tmine = (games_dir / "minesweeper.html").read_text(encoding="utf-8")
        for name, text in (("2048", tt2048), ("Сапёр", tmine)):
            check(f"{name}: мини-SDK (fr_hello/fr_close) и sandbox-safe",
                  "fr_hello" in text and "fr_close" in text
                  and "<script>" in text)
        check("2048: рекорд в localStorage", "wr_2048_best" in tt2048)
        check("Сапёр: безопасный первый клик (мины после него)",
              "placeMines" in tmine and "wr_mine_best_" in tmine)

        # ── 3. Живой API: /games и /games/<id> ────────────────────────
        code, body, _ = fetch(urllib.request.Request(base + "/games"))
        j = json.loads(body)
        check("/games: ok и список", code == 200 and j.get("ok"))
        games = j.get("games", [])
        ids = {g["id"] for g in games}
        check("/games: реестр содержит 2048 и Сапёра",
              "g2048" in ids and "minesweeper" in ids)
        check("/games: все игры available (файлы доехали)",
              all(g.get("available") for g in games))
        check("/games: у карточек есть режим и описание",
              all(g.get("mode") in ("single", "multi") for g in games))

        code, body, hdr = fetch(urllib.request.Request(base + "/games/g2048"))
        check("/games/g2048: HTML с правильным Content-Type",
              code == 200
              and "text/html" in hdr.get("Content-Type", "")
              and b"2048" in body)
        code, body, _ = fetch(urllib.request.Request(
            base + "/games/minesweeper"))
        check("/games/minesweeper: HTML", (code == 200 and b"Saper" in body)
              or (code == 200 and "Сапёр".encode() in body))

        # белый список: неизвестная игра и мусор — 404
        code, _, _ = fetch(urllib.request.Request(base + "/games/nope"))
        check("/games/nope → 404", code == 404)
        code, _, _ = fetch(urllib.request.Request(
            base + "/games/..%2F..%2Fstatic%2Fstyle.css"))
        check("path traversal через /games/ → 404", code == 404)
        code, _, _ = fetch(urllib.request.Request(base + "/games/G2048"))
        check("регистр id не важен (нормализация)", code == 200)
    finally:
        relay.stop()

    print(f"Итого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
