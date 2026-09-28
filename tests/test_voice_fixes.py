"""Регрессионные тесты багфиксов голосового канала (v9.6):

1. FULL-утечка: мёртвые клиенты накапливались в _clients навсегда, а
   _MAX_CLIENTS=16 считал и мёртвых — после 16 суммарных подключений
   (обновления страницы, ретраи, рестарты) сервер навсегда отвечал FULL.
2. Roster-события: веб-клиент не узнавал о молчащих новичках и не
   убирал ушедших — теперь микшер рассылает {"t":"roster","names":[...]}.
3. Веб-страница при включённом ключе доступа: GET / требовал заголовок
   X-Relay-Key, который браузер на обычной навигации послать не может —
   вместо интерфейса приходил 403. Теперь каркас (/, /web, /static/*)
   открыт, API — по-прежнему под ключом.

Запуск: python tests/test_voice_fixes.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer
from voice.mixer import MIXER_MAX_CLIENTS as _MAX_CLIENTS
from voice.mixer import VoiceMixer
from voice.protocol import (
    FLAG_CONTROL as _FLAG_CONTROL,
)
from voice.protocol import (
    FRAME_HEADER_LEN as _FRAME_HEADER_LEN,
)
from voice.protocol import (
    MAGIC as _MAGIC,
)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def make_frame(flags: int, payload: bytes) -> bytes:
    return _MAGIC + bytes([flags]) + struct.pack("<H", len(payload)) + payload


class TinyClient:
    """Минимальный TCP-клиент микшера."""

    def __init__(self, host: str, port: int, name: str):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.sendall(f"{name}|\n".encode())
        # AUTH_OK и первый control-кадр (roster/spk_all) могут прийти в одном
        # TCP-сегменте — читаем стрим, а не «один recv = одно сообщение»,
        # иначе тест флaky (assert падал, когда кадры склеивались).
        self.buf = b""
        deadline = time.time() + 5.0
        while b"\n" not in self.buf:
            self.sock.settimeout(max(0.05, deadline - time.time()))
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError(f"auth: соединение закрыто, буфер={self.buf!r}")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        assert line == b"AUTH_OK", f"auth: {line!r} (хвост={self.buf!r})"
        self.name = name

    def read_frame(self, timeout: float = 3.0):
        deadline = time.time() + timeout
        while True:
            while len(self.buf) >= _FRAME_HEADER_LEN:
                if self.buf[:4] != _MAGIC:
                    self.buf = self.buf[1:]
                    continue
                flags = self.buf[4]
                (ln,) = struct.unpack("<H", self.buf[5:7])
                if len(self.buf) < _FRAME_HEADER_LEN + ln:
                    break
                payload = self.buf[_FRAME_HEADER_LEN:_FRAME_HEADER_LEN + ln]
                self.buf = self.buf[_FRAME_HEADER_LEN + ln:]
                return flags, payload
            self.sock.settimeout(max(0.01, deadline - time.time()))
            try:
                chunk = self.sock.recv(4096)
            except (TimeoutError, OSError):
                return None
            if not chunk:
                return None
            self.buf += chunk

    def read_controls(self, timeout: float = 3.0) -> list[dict]:
        """Все control-JSON за окно (аудио пропускаем)."""
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            fr = self.read_frame(max(0.05, deadline - time.time()))
            if fr is None:
                break
            flags, payload = fr
            if flags & _FLAG_CONTROL:
                try:
                    out.append(json.loads(payload.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    print("== Регрессионные тесты голосовых багфиксов ==\n")

    # ── 1. FULL-утечка мёртвых клиентов ──────────────────────────────
    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    assert mixer.start()
    port = mixer._sock.getsockname()[1]
    try:
        n_cycles = _MAX_CLIENTS + 5
        all_ok = True
        for i in range(n_cycles):
            c = TinyClient("127.0.0.1", port, f"Гость-{i}")
            c.close()
            time.sleep(0.05)
            # следующий должен подключаться без FULL
            try:
                probe = TinyClient("127.0.0.1", port, f"probe-{i}")
            except AssertionError as e:
                all_ok = False
                print(f"    подключение #{i}: {e}")
                break
            probe.close()
            time.sleep(0.05)
        check(f"1. после {n_cycles} циклов вход/выход новый клиент НЕ получает FULL",
              all_ok)
        time.sleep(0.3)
        leaked = len(mixer._clients)
        check("2. мёртвые клиенты удалены из списка (нет утечки)", leaked <= 1,
              f"осталось в списке: {leaked}")
        check("3. participants() пуст после ухода всех",
              mixer.participants() == [])
    finally:
        mixer.stop()

    # ── 2. Roster-события ────────────────────────────────────────────
    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    assert mixer.start()
    port = mixer._sock.getsockname()[1]
    try:
        bob = TinyClient("127.0.0.1", port, "Боб")
        bob.read_controls(timeout=1.0)  # снапшот/ростер при входе

        alice = TinyClient("127.0.0.1", port, "Алиса")
        controls = bob.read_controls(timeout=2.0)
        rosters = [c for c in controls if c.get("t") == "roster"]
        ok_join = any(set(c.get("names", [])) == {"Боб", "Алиса"}
                      for c in rosters)
        check("4. вход Алисы: Боб получил roster с обоими", ok_join,
              f"rosters={rosters}")

        alice.close()
        controls = bob.read_controls(timeout=2.0)
        rosters = [c for c in controls if c.get("t") == "roster"]
        ok_leave = any(set(c.get("names", [])) == {"Боб"} for c in rosters)
        check("5. уход Алисы: roster Боба больше её не содержит", ok_leave,
              f"rosters={rosters}")

        # молчащий новичок виден сразу (не только когда заговорит)
        silent = TinyClient("127.0.0.1", port, "Молчун")
        controls = bob.read_controls(timeout=2.0)
        names_seen = any("Молчун" in c.get("names", [])
                         for c in controls if c.get("t") == "roster")
        check("6. молчащий новичок появляется в roster сразу", names_seen)
        silent.close()
        bob.close()
    finally:
        mixer.stop()

    # ── 3. Веб-страница при включённом ключе доступа ─────────────────
    tmp = Path(tempfile.mkdtemp(prefix="wr_fixes_"))
    base = 18740
    relay = RelayServer(tmp, host_name="Host", access_key="sekrit",
                        max_file_size=1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("  [FAIL] сервер не стартовал")
        return 1
    try:
        # страница БЕЗ заголовков (как обычная навигация браузера)
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{base}/", timeout=5)
            body = resp.read(2048)
            ok_page = resp.status == 200 and b"<html" in body.lower()
        except Exception as e:
            ok_page = False
            print(f"    page err: {e}")
        check("7. GET / без ключа отдаёт страницу (раньше был 403)", ok_page)

        # статика без заголовков
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{base}/static/forge.min.js", timeout=5)
            ok_static = resp.status == 200
        except Exception as e:
            ok_static = False
            print(f"    static err: {e}")
        check("8. GET /static/forge.min.js без ключа отдаётся", ok_static)

        # API по-прежнему защищён
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{base}/voice/info", timeout=5)
            ok_guard = False
        except urllib.error.HTTPError as e:
            ok_guard = e.code == 403
        except Exception:
            ok_guard = False
        check("9. /voice/info без ключа — по-прежнему 403 (защита не ослабла)",
              ok_guard)

        # API с ключом работает
        try:
            resp = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{base}/voice/info",
                headers={"X-Relay-From": "Friend", "X-Relay-Key": "sekrit"}),
                timeout=5)
            j = json.loads(resp.read())
            ok_api = j.get("ok") and j.get("voice_ws_path") == "/voice/ws"
        except Exception as e:
            ok_api = False
            print(f"    api err: {e}")
        check("10. /voice/info с ключом отвечает как раньше", ok_api)
    finally:
        relay.stop()

    # ── (v3.5.6) Исходники: авто-реконнект Qt-клиента и честный keepalive
    root = Path(__file__).resolve().parent.parent
    dlg_src = (root / "qt_app" / "widgets" / "voice_channel_dialog.py") \
        .read_text(encoding="utf-8")
    check("11. Qt-диалог: лестница авто-реконнекта подключена",
          "VOICE_RECONNECT_LADDER_S" in dlg_src
          and "_schedule_voice_reconnect" in dlg_src
          and "_voice_reconnect_fire" in dlg_src
          and "threading.Thread" in dlg_src)
    check("12. Qt-диалог: ручное отключение гасит авто-реконнект",
          "manual: bool = False" in dlg_src
          and "_voice_manual_disconnect" in dlg_src
          and "self._cancel_voice_reconnect()" in dlg_src)
    eng_src = (root / "voice" / "client" / "engine.py").read_text(
        encoding="utf-8")
    check("13. Qt-движок: connect() чистит плейаут-буфер и хвост кадра",
          "self._playback.clear()" in eng_src
          and 'self._tail = b""' in eng_src)
    bridge_src = (root / "voice" / "bridge.py").read_text(encoding="utf-8")
    # Срезаем ТОЛЬКО тело keepalive-таски (после неё в pump идёт легитимный
    # FLAG_MUTED для реальных мьют-кадров браузера).
    ka = ""
    if "async def _ka_task" in bridge_src:
        start = bridge_src.find("async def _ka_task")
        end = bridge_src.find("ka = asyncio.create_task", start)
        ka = bridge_src[start:end if end > start else len(bridge_src)]
    check("14. Мост: keepalive-кадр без FLAG_MUTED (не лжёт о состоянии)",
          "FLAG_SILENCE," in ka and "FLAG_MUTED |" not in ka)

    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
