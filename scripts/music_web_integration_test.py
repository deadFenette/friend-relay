"""Интеграционная проверка: реальный start() сервера + раздача веб-клиента.

Проверяем, что:
- сервер стартует на эфемерном порту (TLS/голос-мост опциональны);
- GET / отдаёт index.html с новой вкладкой music;
- /static/music.js, style.css, icons.js доступны;
- /music/sync и /music/library отвечают до всякой аутентификации.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJ = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJ)


def main() -> int:
    from lib.relay_server import RelayServer

    ok = True
    with tempfile.TemporaryDirectory() as td:
        relay = RelayServer(Path(td) / "relay_data", "HostTest")
        port = 20000 + (int(time.time()) % 20000)
        if not relay.start("127.0.0.1", port):
            print(" FAIL  сервер не стартовал")
            return 1
        port = relay._base_port or port
        base = f"http://127.0.0.1:{port}"
        print(f"  сервер: {base}")

        def get(path: str):
            req = urllib.request.Request(base + path,
                                         headers={"X-Relay-From": "X"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()

        try:
            st, html = get("/")
            text = html.decode("utf-8")
            assert st == 200 and "tab-music" in text and 'data-tab="music"' in text
            print("  OK  / содержит вкладку Музыка")

            for static in ("music.js", "style.css", "icons.js", "main.js"):
                st, body = get("/static/" + static)
                assert st == 200 and len(body) > 100, static
            print("  OK  статики музыки раздаются")

            st, body = get("/music/sync")
            j = json.loads(body)
            assert st == 200 and j["ok"] and "queue" in j
            print("  OK  /music/sync:", {k: j[k] for k in ("playing", "open_dj")})

            st, body = get("/music/library")
            assert st == 200 and json.loads(body)["ok"]
            print("  OK  /music/library")
        except Exception as e:
            ok = False
            print(f" FAIL  {e}")
        finally:
            relay.stop()

    print("ИНТЕГРАЦИЯ:", "ПРОЙДЕНА" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
