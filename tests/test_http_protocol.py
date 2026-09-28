"""Быстрый протокольный тест нового транспорта БЕЗ Qt: поднимаем
RelayServer, гоняем клиента (urllib) по ключевым эндпоинтам CORE - чат,
реакции, правки, пины, каналы, DM, файлы. Поведение должно совпадать со
старым монолитом 1:1."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer
from lib.util import header_encode

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def req(base: str, path: str, sender: str, body: dict | bytes | None = None) -> tuple[int, dict | bytes]:
    url = base + path
    data = None
    headers = {"X-Relay-From": header_encode(sender)}
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = body
    r = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(payload)
            return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="frelay_proto_") as td:
        server = RelayServer(Path(td), host_name="Хост")
        assert server.start("127.0.0.1", 8791), "server not started"
        time.sleep(0.2)
        base = "http://127.0.0.1:8791"

        st, data = req(base, "/ping", "Вася")
        check("/ping ok", st == 200 and data["ok"] and data["name"] == "Хост")
        check("/ping выдаёт token", bool(data.get("session_token")))

        st, data = req(base, "/send_text", "Вася", {"text": "привет мир"})
        check("/send_text", st == 200 and data["ok"] and data["seq"] > 0)
        seq1 = data["seq"]

        st, data = req(base, "/send_text", "Вася", {"text": ""})
        check("пустое сообщение -> 400", st == 400 and data["error"] == "пустое сообщение")

        st, data = req(base, "/events?since=0", "Петя")
        check("/events отдаёт историю", st == 200 and any(e["seq"] == seq1 for e in data["events"]))
        check("online учитывает поллер", "Петя" in data["online"])

        st, data = req(base, "/edit_text", "Петя", {"seq": seq1, "text": "взлом"})
        check("чужой edit -> 404", st == 404)
        st, data = req(base, "/edit_text", "Вася", {"seq": seq1, "text": "правка"})
        check("свой edit -> ok", st == 200 and data["target_seq"] == seq1)

        st, data = req(base, "/add_reaction", "Петя", {"seq": seq1, "emoji": "🔥"})
        check("реакция -> ok", st == 200 and data["emoji"] == "🔥")
        st, data = req(base, "/add_reaction", "Петя", {"seq": seq1, "emoji": ""})
        check("пустой эмодзи -> 400", st == 400 and data["error"] == "пустой эмодзи")

        st, data = req(base, "/pin_message", "Вася", {"seq": seq1})
        check("пин -> ok", st == 200 and data["pinned"] is True)
        st, data = req(base, "/pinned", "Вася")
        check("/pinned видит", len(data["pinned"]) == 1)

        st, data = req(base, "/delete_event", "Вася", {"seq": seq1})
        check("delete -> ok", st == 200)

        # каналы
        st, data = req(base, "/channel/create", "Вася", {"name": "общий"})
        check("канал создан", st == 200 and data["channel"]["name"] == "общий")
        st, data = req(base, "/channel/send", "Петя", {"channel": "общий", "text": "ку"})
        check("сообщение в канал", st == 200)
        st, data = req(base, "/channel/messages?channel=" + urllib.parse.quote("общий"), "Петя")
        check("чтение канала", st == 200 and len(data["messages"]) == 1)

        # DM
        st, data = req(base, "/dm/send", "Вася", {"to": "Петя", "text": "секрет"})
        check("dm отправлено", st == 200)
        st, data = req(base, "/dm/history?user=" + urllib.parse.quote("Петя"), "Вася")
        check("dm история", st == 200 and data["messages"][0]["text"] == "секрет")

        # файлы: общий + приватный + права на приватный
        st, data = req(base, "/send_file", "Вася", b"file-bytes-here")
        check("/send_file", st == 200 and data["file_id"])
        fid = data["file_id"]
        st, body = req(base, f"/download/{fid}", "Петя")
        check("скачивание файла", st == 200 and body == b"file-bytes-here")

        st, data = req(base, "/send_dm_file?x=1", "Вася", b"secret-dm")
        # NOTE: /send_dm_file берёт получателя из заголовка X-Relay-To
        if st == 400:
            r = urllib.request.Request(
                base + "/send_dm_file",
                data=b"secret-dm",
                headers={"X-Relay-From": header_encode("Вася"),
                         "X-Relay-To": header_encode("Петя")},
            )
            with urllib.request.urlopen(r, timeout=5) as resp:
                dm_data = json.loads(resp.read())
                st = resp.status
        else:
            dm_data = data
        check("dm-файл отправлен", st == 200 and dm_data["file_id"])
        dm_fid = dm_data["file_id"]

        st, body = req(base, f"/dm_download/{dm_fid}", "Маша")
        check("чужому dm-файл запрещён", st == 403)
        st, body = req(base, f"/dm_download/{dm_fid}", "Петя")
        check("участнику dm-файл разрешён", st == 200 and body == b"secret-dm")

        # typing и 404
        st, data = req(base, "/typing", "Петя", {})
        check("/typing ok", st == 200)
        st, data = req(base, "/no_such_route", "Петя")
        check("неизвестный путь -> 404", st == 404 and data["error"] == "неизвестный путь")

        # stop дважды не роняет
        server.stop()
        check("stop повторный безопасен", server.stop() is None)

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
