#!/usr/bin/env python3
"""Живой тест веб-фич v2.0.3: каналы из веба, пины, файлы в ЛС,
рейтинг, /server/stats и статика новых элементов интерфейса.

Сервер поднимается на 127.0.0.1, дальше повторяем ровно те HTTP-запросы,
которые делают новые функции веб-клиента (createChannel/deleteChannel,
loadPins/pinMsg, uploadDmFile/dmDownloadFile, loadLeaderboard,
loadHostStats). Плюс статические проверки: index.html содержит новые
кнопки, style.css — тему cherry-grove, main.js — цикл из трёх тем.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auth import sign_request
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


def signed_post(base: str, token: str, user: str, path: str,
                payload: dict) -> tuple[int, dict]:
    """POST с подписью X-Relay-Auth — как apiPost() в core.js."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    auth = token + ":" + sign_request(token, body)
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"X-Relay-From": user, "X-Relay-Auth": auth,
                 "Content-Type": "application/json; charset=utf-8"},
    )
    code, raw, _ = fetch(req)
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, {"ok": False, "error": raw[:120].decode("utf-8", "replace")}


def ping(base: str, user: str) -> str:
    req = urllib.request.Request(base + "/ping",
                                 headers={"X-Relay-From": user})
    code, body, _ = fetch(req)
    assert code == 200, f"ping {user} -> {code}"
    return json.loads(body)["session_token"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="web_v203_"))
    relay = RelayServer(tmp, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18537
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.2)

        # ── 1. Статика: новые элементы UI на месте ────────────────────
        code, body, _ = fetch(urllib.request.Request(base + "/"))
        page = body.decode("utf-8")
        check("index.html: кнопка создания канала", 'id="btnChanNew"' in page)
        check("index.html: кнопка удаления канала", 'id="btnChanDel"' in page)
        check("index.html: кнопка пинов + панель",
              'id="btnPins"' in page and 'id="pinPanel"' in page
              and 'id="pinList"' in page)
        check("index.html: файл в ЛС", 'id="dmFileInput"' in page)
        check("index.html: рейтинг в шахматах", 'id="lbBox"' in page
              and 'id="btnLbRefresh"' in page)
        check("index.html: сводка хоста в профиле",
              'id="hostTable"' in page and 'id="btnHostStats"' in page)

        code, css, _ = fetch(urllib.request.Request(base + "/static/style.css"))
        css_text = css.decode("utf-8")
        check("style.css: тема cherry-grove",
              'data-theme="cherry-grove"' in css_text)
        check("style.css: токены светлой темы",
              "--hover-chip" in css_text and "--pop-border" in css_text
              and "--panel-soft" in css_text)
        check("style.css: стили панели пинов",
              ".pinpanel" in css_text and ".pinrow" in css_text)
        check("style.css: стили мини-маркдауна",
              ".mdcode" in css_text)

        code, core, _ = fetch(urllib.request.Request(base + "/static/core.js"))
        check("core.js: мини-маркдаун (renderMsgMarkdown)",
              b"renderMsgMarkdown" in core and b"mdSafeUrl" in core)
        code, chat, _ = fetch(urllib.request.Request(base + "/static/chat.js"))
        check("chat.js: createChannel/deleteChannel/pins",
              b"createChannel" in chat and b"deleteChannel" in chat
              and b"loadPins" in chat and b"jumpToSeq" in chat)
        code, dm, _ = fetch(urllib.request.Request(base + "/static/dm.js"))
        check("dm.js: файлы ЛС + бейдж",
              b"uploadDmFile" in dm and b"dmDownloadFile" in dm
              and b"dmBadgeTick" in dm)
        code, chess, _ = fetch(urllib.request.Request(base + "/static/chess.js"))
        check("chess.js: loadLeaderboard", b"loadLeaderboard" in chess)
        code, prof, _ = fetch(urllib.request.Request(base + "/static/profile.js"))
        check("profile.js: loadHostStats", b"loadHostStats" in prof)
        code, mainjs, _ = fetch(urllib.request.Request(base + "/static/main.js"))
        check("main.js: цикл из трёх тем",
              b"cherry-grove" in mainjs and b"THEMES" in mainjs)

        # ── 2. Каналы: создать → видно в списке с создателем → удалить ──
        tok_a = ping(base, "Alice")
        tok_b = ping(base, "Bob")
        tok_c = ping(base, "Mallory")

        code, j = signed_post(base, tok_a, "Alice", "/channel/create",
                              {"name": "web-test"})
        check("channel/create: канал создан", code == 200 and j.get("ok"))

        req = urllib.request.Request(base + "/channels",
                                     headers={"X-Relay-From": "Alice"})
        _, raw, _ = fetch(req)
        chans = json.loads(raw).get("channels", [])
        found = next((c for c in chans
                      if isinstance(c, dict) and c.get("name") == "web-test"),
                     None)
        check("channels: список содержит канал и создателя",
              found is not None and found.get("creator") == "Alice")

        # Mallory (не создатель) пытается удалить — сервер должен отказать
        code, j = signed_post(base, tok_c, "Mallory", "/channel/delete",
                              {"channel": "web-test"})
        check("channel/delete чужого канала отвергнут (403 not_creator)",
              code == 403 and not j.get("ok"))

        code, j = signed_post(base, tok_a, "Alice", "/channel/delete",
                              {"channel": "web-test"})
        check("channel/delete создателем работает", code == 200 and j.get("ok"))

        _, raw, _ = fetch(urllib.request.Request(
            base + "/channels", headers={"X-Relay-From": "Alice"}))
        names = [c["name"] if isinstance(c, dict) else c
                 for c in json.loads(raw).get("channels", [])]
        check("channels: удалённого канала больше нет", "web-test" not in names)

        # ── 3. Пины: закрепить → /pinned → отжать ──────────────────────
        code, j = signed_post(base, tok_a, "Alice", "/send_text",
                              {"text": "запомни это сообщение **важно**"})
        check("send_text для пина принят", code == 200 and j.get("ok"))
        seq = j.get("seq", 0)

        code, j = signed_post(base, tok_b, "Bob", "/pin_message", {"seq": seq})
        check("pin_message закрепил", code == 200 and j.get("pinned") is True)

        _, raw, _ = fetch(urllib.request.Request(
            base + "/pinned", headers={"X-Relay-From": "Bob"}))
        pins = json.loads(raw).get("pinned", [])
        pin = next((p for p in pins if p.get("seq") == seq), None)
        check("pinned: закреп видно, текст и автор на месте",
              pin is not None and pin.get("from") == "Alice"
              and "запомни" in (pin.get("text") or ""))

        # повторный pin_message — тоггл вниз
        code, j = signed_post(base, tok_b, "Bob", "/pin_message", {"seq": seq})
        check("pin_message повторный открепляет", code == 200
              and j.get("pinned") is False)
        _, raw, _ = fetch(urllib.request.Request(
            base + "/pinned", headers={"X-Relay-From": "Bob"}))
        check("pinned: список опустел после открепа",
              not json.loads(raw).get("pinned", []))

        # ── 4. Файлы в ЛС: A → B, B качает, Mallory получает 403 ───────
        blob = b"DM-FILE-v203-" + bytes(range(48)) * 3
        sha = hashlib.sha256(blob).hexdigest()
        req = urllib.request.Request(
            base + "/send_dm_file", data=blob, method="POST",
            headers={"X-Relay-From": "Alice",
                     "X-Relay-To": "Bob",
                     "X-Relay-Filename": "отчёт.txt".encode().decode("latin-1"),
                     "X-Relay-SHA256": sha,
                     "X-Relay-Auth": tok_a + ":" + sign_request(tok_a, blob)},
        )
        code, raw, _ = fetch(req)
        j = json.loads(raw)
        check("send_dm_file принят", code == 200 and j.get("ok"))
        dm_file_id = j.get("file_id", "")

        # история диалога A↔B содержит событие dm_file
        req = urllib.request.Request(
            base + "/dm/history?user=Bob",
            headers={"X-Relay-From": "Alice"})
        _, raw, _ = fetch(req)
        msgs = json.loads(raw).get("messages", [])
        dm_ev = next((m for m in msgs if m.get("kind") == "dm_file"), None)
        check("dm/history содержит dm_file с именем и размером",
              dm_ev is not None and dm_ev.get("name") == "отчёт.txt"
              and dm_ev.get("size") == len(blob))

        # получатель качает
        req = urllib.request.Request(base + "/dm_download/" + dm_file_id,
                                     headers={"X-Relay-From": "Bob"})
        code, raw2, _ = fetch(req)
        check("dm_download получателем: байты совпадают",
              code == 200 and hashlib.sha256(raw2).hexdigest() == sha)

        # посторонний — 403
        req = urllib.request.Request(base + "/dm_download/" + dm_file_id,
                                     headers={"X-Relay-From": "Mallory"})
        code, _, _ = fetch(req)
        check("dm_download посторонним отвергнут (403)", code == 403)

        # conversations: у Bob диалог с Alice виден (для бейджа ЛС)
        req = urllib.request.Request(
            base + "/dm/conversations", headers={"X-Relay-From": "Bob"})
        _, raw, _ = fetch(req)
        convs = json.loads(raw).get("conversations", [])
        check("dm/conversations: диалог с Alice есть, last_time проставлен",
              any(c.get("user") == "Alice" and c.get("last_time", 0) > 0
                  for c in convs))

        # ── 5. Рейтинг и сводка хоста (кнопки из веба) ─────────────────
        req = urllib.request.Request(base + "/leaderboard",
                                     headers={"X-Relay-From": "Alice"})
        code, raw, _ = fetch(req)
        j = json.loads(raw)
        check("leaderboard: ok и список (пусть пустой)",
              code == 200 and j.get("ok") is True and isinstance(
                  j.get("scores"), list))

        req = urllib.request.Request(base + "/server/stats",
                                     headers={"X-Relay-From": "Alice"})
        code, raw, _ = fetch(req)
        j = json.loads(raw)
        check("server/stats: ok, счётчики на месте",
              code == 200 and j.get("ok") is True
              and "uptime_s" in j and "events_cached" in j
              and "files_tracked" in j and "dm_files_tracked" in j)

    finally:
        relay.stop()
        print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
