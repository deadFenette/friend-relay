"""Тест протокола индикатора «говорит» (bugfix: в голосовом канале не было
обратной связи — зелёное кольцо вокруг говорящего, как в Discord).

Проверяет серверные control-фреймы VoiceMixer без sounddevice/numpy:
  - переход spk on при голосовых фреймах
  - гистерезис: индикатор гаснет только после _SPEAKING_HOLD_FRAMES тишин
  - спикер сам своих переходов НЕ получает (его кольцо ведёт локальный VAD)
  - снапшот spk_all новому клиенту
  - уход из канала гасит кольцо у остальных

Запуск: python tests/test_voice_speaking.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice.mixer import (
    MIXER_SPEAKING_HOLD_FRAMES as _SPEAKING_HOLD_FRAMES,
)
from voice.mixer import (
    VoiceMixer,
)
from voice.protocol import (
    FLAG_CONTROL as _FLAG_CONTROL,
)
from voice.protocol import (
    FRAME_HEADER_LEN as _FRAME_HEADER_LEN,
)
from voice.protocol import (
    MAGIC as _MAGIC,
)
from voice.protocol import (
    PCM_FRAME_BYTES as _PCM_FRAME_BYTES,
)

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def make_frame(flags: int, payload: bytes) -> bytes:
    return _MAGIC + bytes([flags]) + struct.pack("<H", len(payload)) + payload


class TestClient:
    """Сырой TCP-клиент для проверки протокола (замена VoiceClient без аудио)."""

    def __init__(self, host: str, port: int, name: str):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.sendall(f"{name}|\n".encode())
        # v2.0: auth читаем ровно 8 байтами — recv(64) слипал «AUTH_OK\n»
        # со снапшотом spk_all, пришедшим в том же TCP-сегменте.
        resp = b""
        while len(resp) < 8:
            chunk = self.sock.recv(8 - len(resp))
            if not chunk:
                raise ConnectionError("closed during auth")
            resp += chunk
        assert resp == b"AUTH_OK\n", f"auth failed: {resp!r}"
        self.name = name
        self.buf = b""

    def send_voice(self, n_frames: int, silence: bool = False) -> None:
        flags = 0x02 if silence else 0x00
        pcm = b"\x00" * _PCM_FRAME_BYTES if silence else b"\x11\x22" * (_PCM_FRAME_BYTES // 2)
        self.sock.sendall(make_frame(flags, pcm) * n_frames)

    def read_control(self, timeout: float = 3.0, max_frames: int = 400) -> dict | None:
        """Читает фреймы, пропуская аудио, до первого «интересного»
        control-фрейма. roster-события (состав канала, v9.6) пропускаем:
        тесты ищут переходы spk/spk_all, а roster приходит дополнительно."""
        self.sock.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                # добираем заголовок
                while len(buf) < _FRAME_HEADER_LEN:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return None
                    buf += chunk
                if buf[:4] != _MAGIC:
                    buf = buf[1:]
                    continue
                flags = buf[4]
                (length,) = struct.unpack("<H", buf[5:7])
                need = _FRAME_HEADER_LEN + length
                while len(buf) < need:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return None
                    buf += chunk
                payload = buf[_FRAME_HEADER_LEN:need]
                buf = buf[need:]
                if flags & _FLAG_CONTROL:
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if msg.get("t") == "roster":
                        continue  # служебное событие состава — не то, что ищем
                    return msg
        except (TimeoutError, OSError):
            return None
        return None


def main() -> int:
    print("== Тест протокола индикатора «говорит» ==\n")
    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    if not mixer.start():
        print("  [FAIL] сервер не стартовал")
        return 1
    port = mixer._sock.getsockname()[1]

    try:
        # 1. Снапшот новому клиенту
        bob = TestClient("127.0.0.1", port, "Боб")
        snap = bob.read_control()
        check("1. новый клиент получает снапшот spk_all", snap is not None and snap.get("t") == "spk_all")

        # 2. Алиса говорит → Боб видит on=true; Алиса сама — НЕ видит
        alice = TestClient("127.0.0.1", port, "Алиса")
        snap2 = alice.read_control()
        check("2. снапшот второму клиенту", snap2 is not None and snap2.get("t") == "spk_all")

        alice.send_voice(5)  # 5 голосовых фреймов
        got = bob.read_control()
        check(
            "3. Боб: spk on (Алиса говорит)",
            got is not None and got.get("t") == "spk" and got.get("name") == "Алиса" and got.get("on") is True,
        )
        own = alice.read_control(timeout=0.8)
        check("4. Алиса не получает свои переходы (локальный VAD)", own is None)

        # 3. Гистерезис: короткая тишина НЕ гасит кольцо
        alice.send_voice(5, silence=True)  # 5 < _SPEAKING_HOLD_FRAMES
        got = bob.read_control(timeout=1.0)
        check("5. короткая пауза не гасит индикатор", got is None)

        # 4. Длинная тишина гасит
        alice.send_voice(_SPEAKING_HOLD_FRAMES + 5, silence=True)
        got = bob.read_control()
        check(
            "6. после 300мс тишины: spk off",
            got is not None and got.get("t") == "spk" and got.get("name") == "Алиса" and got.get("on") is False,
        )

        # 5. Уход из канала гасит кольцо
        alice.send_voice(5)
        got = bob.read_control()
        check("7. снова spk on", got is not None and got.get("on") is True)
        alice.sock.close()
        got = bob.read_control(timeout=3.0)
        check(
            "8. отключение: spk off",
            got is not None and got.get("t") == "spk" and got.get("name") == "Алиса" and got.get("on") is False,
        )
        bob.sock.close()
    finally:
        mixer.stop()

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
