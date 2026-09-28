"""Стабильность сервера v1.9.9 (ruff/архитектура/безопасность).

Регрессия на найденные при аудите проблемы:
  1. Кириллический access_key: hmac.compare_digest на str принимает только
     ASCII — раньше КАЖДЫЙ запрос с не-ASCII ключом падал TypeError'ом
     (500 «internal error»). Теперь сравнение по байтам, честный 403/200.
  2. Не-ASCII подпись в X-Relay-Auth — раньше 500, теперь 403.
  3. Мусорный Content-Length («abc») на /avatar и файлах — раньше 500
     (голый int()), теперь 400.
  4. Мусорные числа в /voxel/create (wall_hp="abc") — раньше 500, теперь 400.
  5. «Немой» клиент на порту голоса НЕ блокирует подключение остальных
     (раньше auth-чтение шло прямо в цикле accept и подвешивало сервер
     на 5 секунд).
  6. SessionStore.needs_cleanup — публичный API вместо лазания в _sessions.
  7. settings.json получает права 0600 (POSIX) — access_key в открытом
     виде защищён как tls_key.pem.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import sys
import tempfile
import time
import urllib.error
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


def raw(base: str, path: str, headers: dict, data: bytes | None = None) -> int:
    r = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def raw_socket(port: int, request: str, body: bytes = b"") -> int:
    """Посылает сырые байты (заголовки могут содержать ЛЮБЫЕ utf-8 байты —
    так делает сломанный клиент/сканер) и возвращает HTTP-статус."""
    head = request.encode("utf-8") + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(head)
        resp = b""
        while b"\r\n" not in resp:
            resp += s.recv(256)
        return int(resp.split(b" ")[1])


def get_token(base: str, cyr_key: str, name: str = "Вася") -> str:
    """/ping с ключом -> session_token (как настоящий клиент)."""
    r = urllib.request.Request(
        base + "/ping", data=None,
        headers={"X-Relay-Key": header_encode(cyr_key),
                 "X-Relay-From": header_encode(name)})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return json.loads(resp.read())["session_token"]


def auth_hdr(token: str, body: bytes) -> str:
    """X-Relay-Auth: '<token>:<HMAC-SHA256(token, body)>' (lib/auth.py)."""
    sig = hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
    return f"{token}:{sig}"


def main() -> int:
    cyr_key = "секретный-ключ-123"  # кириллица+дефисы — валидный ключ хоста

    with tempfile.TemporaryDirectory(prefix="frelay_stab_") as td:
        server = RelayServer(Path(td), host_name="Хост", access_key=cyr_key)
        assert server.start("127.0.0.1", 8797), "server not started"
        time.sleep(0.2)
        base = "http://127.0.0.1:8797"

        # ── 1-2. кириллический ключ и подпись ────────────────────────────
        st = raw(base, "/events?since=0", {})
        check("без ключа -> 403", st == 403)

        st = raw(base, "/events?since=0",
                 {"X-Relay-Key": header_encode("не-тот-ключ")})
        check("чужой кириллический ключ -> 403 (раньше 500 TypeError)", st == 403)

        st = raw(base, "/events?since=0",
                 {"X-Relay-Key": header_encode(cyr_key)})
        check("свой кириллический ключ -> 200", st == 200)

        body_txt = json.dumps({"text": "ку"}).encode()
        st = raw_socket(8797,
                        "POST /send_text HTTP/1.1\r\n"
                        f"X-Relay-Key: {header_encode(cyr_key)}\r\n"
                        f"X-Relay-From: {header_encode('Вася')}\r\n"
                        "X-Relay-Auth: токен:подпись-не-ascii\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body_txt)}\r\n"
                        "Connection: close\r\n\r\n",
                        body_txt)
        check("не-ASCII подпись -> 403 (раньше 500 TypeError)", st == 403)

        # ── 3. мусорный Content-Length на /avatar ───────────────────────
        avatar_body = b"x"
        token = get_token(base, cyr_key)
        st = raw(base, "/avatar",
                 {"X-Relay-Key": header_encode(cyr_key),
                  "X-Relay-From": header_encode("Вася"),
                  "X-Relay-Auth": auth_hdr(token, avatar_body),
                  "Content-Length": "abc"},
                 data=avatar_body)
        check("Content-Length='abc' на /avatar -> 400 (раньше 500)", st == 400)

        # ── 4. мусорные числа в /voxel/create ───────────────────────────
        vx_body = json.dumps({"session_id": "s1", "wall_hp": "abc"}).encode()
        st = raw(base, "/voxel/create",
                 {"X-Relay-Key": header_encode(cyr_key),
                  "X-Relay-From": header_encode("Вася"),
                  "X-Relay-Auth": auth_hdr(token, vx_body),
                  "Content-Type": "application/json"},
                 data=vx_body)
        check("wall_hp='abc' -> 400 (раньше 500)", st == 400)
        vx_body2 = json.dumps({"session_id": "s2", "wall_hp": 10}).encode()
        st = raw(base, "/voxel/create",
                 {"X-Relay-Key": header_encode(cyr_key),
                  "X-Relay-From": header_encode("Вася"),
                  "X-Relay-Auth": auth_hdr(token, vx_body2),
                  "Content-Type": "application/json"},
                 data=vx_body2)
        check("wall_hp числом -> 200 (сессия создана)", st == 200)

        server.stop()

        # ── 5. немой клиент не блокирует голосовой порт ─────────────────
        from voice.mixer import VoiceMixer

        mixer = VoiceMixer(host="127.0.0.1", port=0)
        assert mixer.start(), "mixer not started"
        vport = mixer._sock.getsockname()[1]
        time.sleep(0.1)

        silent = socket.create_connection(("127.0.0.1", vport), timeout=2)
        t0 = time.monotonic()
        good = socket.create_connection(("127.0.0.1", vport), timeout=2)
        good.sendall("Вася|\n".encode())
        buf = b""
        while b"AUTH_OK" not in buf and time.monotonic() - t0 < 3.0:
            chunk = good.recv(64)
            if not chunk:
                break
            buf += chunk
        elapsed = time.monotonic() - t0
        check(f"AUTH_OK при молчащем соседе за {elapsed:.2f}с (< 3с)",
              b"AUTH_OK" in buf)
        good.close()
        silent.close()
        mixer.stop()

    # ── 6. SessionStore.needs_cleanup ────────────────────────────────────
    from lib.auth import SessionStore

    ss = SessionStore()
    ss.register("Вася")
    check("needs_cleanup(100) на паре сессий = False", ss.needs_cleanup(100) is False)
    for i in range(5):
        ss.register(f"u{i}")
    check("needs_cleanup(2) на 6 сессиях = True", ss.needs_cleanup(2) is True)
    check("cleanup_expired возвращает число", isinstance(ss.cleanup_expired(), int))

    # ── 7. settings.json 0600 ────────────────────────────────────────────
    import lib.storage as storage

    with tempfile.TemporaryDirectory(prefix="frelay_stab2_") as td:
        storage.SETTINGS_FILE = Path(td) / "settings.json"
        storage.DATA_DIR = Path(td)
        storage.save_settings({"name": "Вася"})
        mode = storage.SETTINGS_FILE.stat().st_mode & 0o777
        check(f"settings.json права {oct(mode)} = 0o600",
              mode == 0o600 or sys.platform == "win32")

    print()
    print(f"ИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
