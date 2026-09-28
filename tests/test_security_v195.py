#!/usr/bin/env python3
"""Тесты безопасности v1.9.5:
1. Имперсонация: без X-Relay-Auth при включённом ключе POST отвергается;
   с подписью от чужой сессии — тоже; со своей — работает.
2. Path traversal через имена DM-участников.
3. Стриминговая подпись файла (/send_file) — фейк отвергается.
4. GET /dm/history требует подпись при ключе.
5. Атомарность settings.json (наличие .tmp не ломает загрузку).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
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


def post(port: int, path: str, sender: str, body: dict, key: str = "k1",
         auth: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("X-Relay-From", sender)
    if key:
        req.add_header("X-Relay-Key", key)
    if auth:
        req.add_header("X-Relay-Auth", auth)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def get(port: int, path: str, sender: str, key: str = "k1", auth: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    if sender:
        req.add_header("X-Relay-From", sender)
    if key:
        req.add_header("X-Relay-Key", key)
    if auth:
        req.add_header("X-Relay-Auth", auth)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def ping(port: int, name: str, key: str = "k1") -> str:
    url = f"http://127.0.0.1:{port}/ping"
    req = urllib.request.Request(url)
    req.add_header("X-Relay-From", name)
    if key:
        req.add_header("X-Relay-Key", key)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))["session_token"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fr_sec_"))
    port = 18790 + (int(time.time()) % 200) * 2
    relay = RelayServer(tmp, host_name="Host", access_key="k1")
    assert relay.start("127.0.0.1", port)
    try:
        body = json.dumps({"text": "привет"}).encode("utf-8")

        # ── 1. Имперсонация ──
        st, data = post(port, "/send_text", "Alice", {"text": "привет"})
        check("без подписи при ключе → 403", st == 403)

        tok_alice = ping(port, "Alice")
        tok_bob = ping(port, "Bob")

        # Боб подписывает тело СВОИМ токеном, но заявляет Alice
        st, data = post(port, "/send_text", "Alice", {"text": "взлом"},
                        auth=f"{tok_bob}:{sign_request(tok_bob, body)}")
        check("подпись Боба + X-Relay-From Alice → 403", st == 403)

        # Чужая подпись вообще (мусорный токен)
        st, data = post(port, "/edit_text", "Bob", {"seq": 1, "text": "x"},
                        auth=f"nope:{sign_request(tok_bob, b'{}')}")
        check("мусорный токен → 403", st == 403)

        # Правильная подпись Алисы от имени Алисы — работает
        st, data = post(port, "/send_text", "Alice", {"text": "привет"},
                        auth=f"{tok_alice}:{sign_request(tok_alice, body)}")
        check("своя подпись → 200", st == 200 and data.get("ok"))

        # Сервер БЕЗ ключа: без подписи по-прежнему работает (совместимость)
        relay2 = RelayServer(Path(tempfile.mkdtemp(prefix="fr_sec2_")), host_name="H2")
        assert relay2.start("127.0.0.1", port + 10)
        try:
            st2, _ = post(port + 10, "/send_text", "Alice", {"text": "x"}, key="")
            check("без ключа без подписи → 200 (совместимость)", st2 == 200)
        finally:
            relay2.stop()

        # ── 2. Path traversal в DM ──
        st, data = post(port, "/dm/send", "Alice", {"to": "../../evil", "text": "т"},
                        auth=f"{tok_alice}:{sign_request(tok_alice, json.dumps({'to': '../../evil', 'text': 'т'}).encode())}")
        check("traversal-получатель → 4xx/5xx, не тишина", st != 200)
        evil = tmp / "evil_Alice.jsonl"
        outside = tmp / ".." / "evil_Alice.jsonl"
        check("файл вне dm/ не создан", not evil.exists() and not outside.exists())

        # Нормальный DM работает
        dm_body = json.dumps({"to": "Bob", "text": "секрет"}).encode()
        st, data = post(port, "/dm/send", "Alice", {"to": "Bob", "text": "секрет"},
                        auth=f"{tok_alice}:{sign_request(tok_alice, dm_body)}")
        check("валидный DM → 200", st == 200)

        # ── 3. Стриминговая подпись файла ──
        # подпись Алисы по ЧУЖИМ байтам (файл подменён в полёте)
        payload = b"FAKE-EXE-BYTES"
        fake_sig = sign_request(tok_alice, b"OTHER-BYTES")
        url = f"http://127.0.0.1:{port}/send_file"
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("X-Relay-From", "Alice")
        req.add_header("X-Relay-Key", "k1")
        req.add_header("X-Relay-Filename", "virus.exe")
        req.add_header("X-Relay-Auth", f"{tok_alice}:{fake_sig}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        check("файл с неверной подписью → 403", st == 403)
        # файл не сохранился в витрину
        kept = [f for f in relay.files_dir.iterdir() if f.is_file()]
        check("подменённый файл не сохранён", not kept)

        # Правильная подпись файла — проходит
        good_sig = sign_request(tok_alice, payload)
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("X-Relay-From", "Alice")
        req.add_header("X-Relay-Key", "k1")
        req.add_header("X-Relay-Filename", "ok.bin")
        req.add_header("X-Relay-Auth", f"{tok_alice}:{good_sig}")
        with urllib.request.urlopen(req, timeout=10) as r:
            st = r.status
        check("файл с верной подписью → 200", st == 200)

        # ── 4. GET /dm/history без подписи ──
        st, data = get(port, "/dm/history?user=Bob", "Mallory")
        check("GET dm/history без подписи → 403", st == 403)

        # С подписью по path+query от Алисы — работает
        target = "/dm/history?user=Bob"
        sig = sign_request(tok_alice, target.encode("utf-8"))
        st, data = get(port, "/dm/history?user=Bob", "Alice",
                       auth=f"{tok_alice}:{sig}")
        check("GET dm/history со своей подписью → 200", st == 200 and data.get("ok"))

        # Подпись Боба, а From Alice → 403
        sig_bob = sign_request(tok_bob, target.encode("utf-8"))
        st, data = get(port, "/dm/history?user=Bob", "Alice",
                       auth=f"{tok_bob}:{sig_bob}")
        check("GET dm/history чужой подписью → 403", st == 403)

        # ── 5. limit=0 в /channel/messages не отдаёт всё ──
        ch = json.dumps({"name": "ch1"}).encode()
        st, data = post(port, "/channel/create", "Alice", {"name": "ch1"},
                        auth=f"{tok_alice}:{sign_request(tok_alice, ch)}")
        check("канал создан", st == 200)
        for i in range(3):
            m = json.dumps({"channel": "ch1", "text": f"m{i}"}).encode()
            post(port, "/channel/send", "Alice", {"channel": "ch1", "text": f"m{i}"},
                 auth=f"{tok_alice}:{sign_request(tok_alice, m)}")
        st, data = get(port, "/channel/messages?channel=ch1&limit=0", "Alice")
        check("limit=0 клампится (не вся история)", st == 200 and 0 < len(data.get("messages", [])) <= 50)

        # ── 6. Аватар не-PNG отвергается ──
        bad = b"<html><script>alert(1)</script>"
        sig_bad = sign_request(tok_alice, bad)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/avatar", data=bad, method="POST")
        req.add_header("X-Relay-From", "Alice")
        req.add_header("X-Relay-Key", "k1")
        req.add_header("Content-Type", "image/png")
        req.add_header("X-Relay-Auth", f"{tok_alice}:{sig_bad}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        check("не-PNG аватар → 400", st == 400)
    finally:
        relay.stop()

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
