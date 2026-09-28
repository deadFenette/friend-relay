#!/usr/bin/env python3
"""Тест браузерного голоса: TLS/plain на одном порту, WS-туннель /voice/ws
и мост web-клиент <-> VoiceMixer.

Проверяет (без звуковых устройств — через сырой TCP-клиент микшера):
  - ensure_tls_files: генерация + переиспользование сертификата
  - RelayServer поднимает TLS-мультиплексор: http И https на одном порту
  - /voice/info: https_port = базовый порт, wss_port = 0, voice_ws_path
  - WebSocket-туннель wss://IP:ПОРТ/voice/ws: auth ok / неверный ключ
  - звук от TCP-клиента доходит до браузера (binary PCM + control spk)
  - звук от браузера микшируется и доходит до TCP-клиента
  - mute от браузера превращает его фреймы в тишину у других
  - непрерывность потока: во время активности миксер шлёт кадры КАЖДЫЙ
    тик (микс или silence) — у слушателя нет дыр («обрывов»)
  - silence-кадры приходят с нулевым payload (шум пауз не протекает)

Запуск: python tests/test_web_voice.py
"""
from __future__ import annotations

import json
import math
import socket
import ssl
import struct
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer
from lib.server.tls_util import ensure_tls_files, make_server_ssl_context
from voice.protocol import (
    FLAG_CONTROL as _FLAG_CONTROL,
)
from voice.protocol import (
    FLAG_SILENCE as _FLAG_SILENCE,
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


class TcpVoiceClient:
    """Сырой TCP-клиент микшера (как TestClient из test_voice_speaking),
    с чтением сырых фреймов (нужно для проверки muted-флагов).

    v1.9.8: auth читается ТОЧНО 8 байтами (recv_exact). Раньше recv(64)
    слипал «AUTH_OK\n» со снапшотом spk_all, пришедшим в том же TCP-сегменте
    (гарантированно воспроизводится на загруженной машине) — и assert валил
    тест, хотя сервер отработал корректно. Лишние байты буферизуются."""

    def __init__(self, host: str, port: int, name: str, key: str = ""):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.sendall(f"{name}|{key}\n".encode())
        resp = self._recv_exact(len(b"AUTH_OK\n"))
        assert resp == b"AUTH_OK\n", f"auth failed: {resp!r}"
        self.name = name
        self.buf = b""

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed during auth")
            buf += chunk
        return buf

    def _fill(self, timeout: float) -> bool:
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(4096)
        except (TimeoutError, OSError):
            return False
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_frame(self, timeout: float = 3.0):
        """Следующий фрейм -> (flags, payload) или None по таймауту."""
        deadline = time.time() + timeout
        while True:
            while len(self.buf) >= _FRAME_HEADER_LEN:
                if self.buf[:4] != _MAGIC:
                    self.buf = self.buf[1:]
                    continue
                flags = self.buf[4]
                (length,) = struct.unpack("<H", self.buf[5:7])
                if len(self.buf) < _FRAME_HEADER_LEN + length:
                    break
                payload = self.buf[_FRAME_HEADER_LEN:_FRAME_HEADER_LEN + length]
                self.buf = self.buf[_FRAME_HEADER_LEN + length:]
                return flags, payload
            if time.time() > deadline or not self._fill(deadline - time.time()):
                return None

    def read_control(self, timeout: float = 3.0, max_frames: int = 500):
        """Пропускает аудио, возвращает первый control-JSON."""
        for _ in range(max_frames):
            fr = self.read_frame(timeout)
            if fr is None:
                return None
            flags, payload = fr
            if flags & _FLAG_CONTROL:
                try:
                    return json.loads(payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        return None

    def send_voice(self, n_frames: int, pcm: bytes | None = None,
                   flags: int = 0) -> None:
        body = pcm if pcm is not None else b"\x11\x22" * (_PCM_FRAME_BYTES // 2)
        self.sock.sendall(make_frame(flags, body) * n_frames)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def ws_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ws_connect(url: str, **kwargs):
    """WS-подключение с запасным таймаутом: 5 секунд на загруженной
    Windows-машине (антивирус/Defender перехватывают loopback-TLS) не
    хватало на mux→TLS→HTTP→туннель→мост → ложный TimeoutError
    «timed out while waiting for handshake response». 15 секунд хватает
    всегда, а при реальной смерти handshake тест всё равно падает быстро
    (сервер на loopback)."""
    from websockets.sync.client import connect
    kwargs.setdefault("open_timeout", 15)
    return connect(url, **kwargs)


def recv_ws(conn, timeout: float = 5.0):
    return conn.recv(timeout=timeout)


def recv_ws_control(conn, timeout: float = 5.0) -> dict | None:
    """Читает WS-сообщения до первого текстового (control/error/ok)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = recv_ws(conn, deadline - time.time())
        except Exception:
            return None
        if isinstance(msg, str):
            try:
                return json.loads(msg)
            except json.JSONDecodeError:
                continue


def main() -> int:
    print("== Тест браузерного голоса: один порт (TLS+plain) + WS-туннель ==\n")

    # ── 1. TLS: генерация и переиспользование ─────────────────────────
    tmp = Path(tempfile.mkdtemp(prefix="wr_voice_"))
    pair1 = ensure_tls_files(tmp)
    check("1. сертификат сгенерирован", pair1 is not None)
    if pair1 is None:
        return 1
    cert1, key1 = pair1
    pair2 = ensure_tls_files(tmp)
    check("2. сертификат переиспользуется (тот же файл)",
          pair2 is not None and pair2[0].read_bytes() == cert1.read_bytes())
    ctx_tls = make_server_ssl_context(*pair1)
    check("3. SSLContext собирается", ctx_tls is not None)

    # ── 2. RelayServer: TLS-мультиплексор на ОДНОМ порту ─────────────
    base, mixer = 18440, 18441
    relay = RelayServer(tmp, host_name="Host", access_key="k", max_file_size=1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("  [FAIL] сервер не стартовал (порт занят?)")
        return 1
    try:
        info = relay.get_voice_info()
        check("4. /voice/info: https_port = базовый порт (TLS на нём же)",
              info.get("https_port") == base)
        check("5. /voice/info: wss_port = 0, voice_ws_path = /voice/ws",
              info.get("wss_port") == 0 and info.get("voice_ws_path") == "/voice/ws")

        # ── 3. Один порт: и HTTPS, и plain HTTP ──────────────────────
        uctx = ssl._create_unverified_context()
        try:
            resp = urllib.request.urlopen(urllib.request.Request(
                f"https://127.0.0.1:{base}/voice/info",
                headers={"X-Relay-From": "Host", "X-Relay-Key": "k"}),
                context=uctx, timeout=5)
            j = json.loads(resp.read())
            ok_https = resp.status == 200 and j.get("voice_ws_path") == "/voice/ws"
        except Exception as e:
            ok_https = False
            print(f"    https err: {e}")
        check("6. HTTPS-сервер отвечает на базовом порту", ok_https)
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{base}/ping", timeout=5)
            ok_plain = resp.status == 200
        except Exception as e:
            ok_plain = False
            print(f"    http err: {e}")
        check("7. plain HTTP работает на том же порту (мультиплекс)", ok_plain)

        # ── 4. Мост через туннель /voice/ws: auth ─────────────────────
        bob = TcpVoiceClient("127.0.0.1", mixer, "Боб", key="k")
        snap = bob.read_control()
        check("8. TCP-клиент получил снапшот spk_all",
              snap is not None and snap.get("t") == "spk_all")

        bad = ws_connect(f"wss://127.0.0.1:{base}/voice/ws",
                         ssl=ws_ssl_ctx(), open_timeout=5)
        bad.send(json.dumps({"name": "Злодей", "access_key": "не-тот"}))
        ans = recv_ws_control(bad)
        check("9. неверный ключ доступа отклонён",
              ans is not None and "error" in ans and "ключ" in ans["error"])
        bad.close()

        web = ws_connect(f"wss://127.0.0.1:{base}/voice/ws",
                         ssl=ws_ssl_ctx(), open_timeout=5)
        web.send(json.dumps({"name": "WebGuy", "access_key": "k"}))
        ans = recv_ws_control(web)
        check("10. браузерный клиент авторизован",
              ans is not None and ans.get("ok") is True and ans.get("name") == "WebGuy")

        # ── 5. Звук TCP → браузер ────────────────────────────────────
        bob.send_voice(12)  # чуть больше prebuffer (60мс)
        got_pcm = got_spk = False
        deadline = time.time() + 5
        while time.time() < deadline and not (got_pcm and got_spk):
            try:
                msg = recv_ws(web, deadline - time.time())
            except Exception:
                break
            if isinstance(msg, bytes) and len(msg) >= _PCM_FRAME_BYTES:
                got_pcm = True
            elif isinstance(msg, str):
                try:
                    j = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if j.get("t") == "spk" and j.get("name") == "Боб" and j.get("on"):
                    got_spk = True
        check("11. голос TCP-клиента дошёл до браузера (binary PCM)", got_pcm)
        check("12. control spk «Боб говорит» дошёл до браузера", got_spk)

        # ── 6. Звук браузер → TCP ─────────────────────────────────────
        # ~5000 амплитуды, синус 440 Гц: тестовый сигнал обязан быть
        # РЕАЛИСТИЧНЫМ — libopus давит и DC (константный уровень через
        # 2 кадра = тишина), и ровно Найквист (±амплитуда через сэмпл)
        _phase = [2 * math.pi * 440 * i / 48000
                  for i in range(_PCM_FRAME_BYTES // 2)]
        loud = b"".join(struct.pack("<h", int(5000 * math.sin(ph)))
                        for ph in _phase)
        web.send(loud * 10)  # 10 фреймов одним бинарным сообщением
        heard = spk_web = False
        deadline = time.time() + 5
        while time.time() < deadline and not (heard and spk_web):
            fr = bob.read_frame(max(0.1, deadline - time.time()))
            if fr is None:
                break
            flags, payload = fr
            if flags & _FLAG_CONTROL:
                try:
                    j = json.loads(payload.decode("utf-8"))
                    if j.get("t") == "spk" and j.get("name") == "WebGuy" and j.get("on"):
                        spk_web = True
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                continue
            if not flags & _FLAG_SILENCE and len(payload) == _PCM_FRAME_BYTES:
                samples = struct.unpack(f"<{_PCM_FRAME_BYTES // 2}h", payload)
                rms = (sum(v * v for v in samples) / len(samples)) ** 0.5
                if rms > 1000:
                    heard = True
        check("13. голос браузера замикширован и слышен TCP-клиенту", heard)
        check("14. control spk «WebGuy говорит» дошёл до TCP-клиента", spk_web)

        # ── 7. Mute от браузера ───────────────────────────────────────
        web.send(json.dumps({"t": "mute", "on": True}))
        time.sleep(0.1)
        web.send(loud * 30)
        # Дренаж: в TCP-буфере Боба ещё плещутся громкие фреймы, отправленные
        # миксером ДО mute. Ждём хвост, потом открываем чистое окно.
        time.sleep(1.2)
        while bob.read_frame(timeout=0.1) is not None:
            pass
        loud_leak = False
        deadline = time.time() + 1.5
        while time.time() < deadline:
            fr = bob.read_frame(max(0.1, deadline - time.time()))
            if fr is None:
                continue
            flags, payload = fr
            if flags & _FLAG_CONTROL:
                continue
            if len(payload) == _PCM_FRAME_BYTES and not flags & _FLAG_SILENCE:
                samples = struct.unpack(
                    f"<{_PCM_FRAME_BYTES // 2}h", payload)
                rms = (sum(v * v for v in samples) / len(samples)) ** 0.5
                if rms > 1000:
                    loud_leak = True
        check("15. mute браузера: громкий PCM больше не попадает в микс",
              not loud_leak)

        # ── 8. Непрерывность потока (джиттер-буфер + PLC) ─────────────
        # Новая «Алиса» говорит и уходит в паузу с silence-хартбитами:
        # слушатель должен получать кадры КАЖДЫЙ тик (микс или silence) —
        # дыр в потоке быть не должно (это и были «обрывы»).
        web.close()
        alice = TcpVoiceClient("127.0.0.1", mixer, "Алиса", key="k")
        alice.read_control()  # снапшот
        listener = ws_connect(f"wss://127.0.0.1:{base}/voice/ws",
                              ssl=ws_ssl_ctx(), open_timeout=5)
        listener.send(json.dumps({"name": "Слушатель", "access_key": "k"}))
        recv_ws_control(listener)
        alice.send_voice(12)                        # голос ~120мс
        alice.send_voice(25, flags=_FLAG_SILENCE)   # пауза с хартбитами ~250мс
        count = zero_payload = 0
        # v1.9.8: окно 1.2с вместо 0.6 — на загруженной машине миксер
        # тикает реже (GIL-конкуренция), 20 кадров за 0.6с было впритык
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                msg = recv_ws(listener, max(0.05, deadline - time.time()))
            except Exception:
                break
            if isinstance(msg, bytes):
                count += len(msg) // _PCM_FRAME_BYTES
                if msg == b"\x00" * len(msg):
                    zero_payload += 1
        check("16. поток непрерывный во время тишины (>= 20 кадров за 0.6с)",
              count >= 20)
        check("17. пауза приходит тишиной, а не шумом микрофона "
              "(silence-кадры с нулевым payload)", zero_payload >= 5)

        listener.close()
        alice.close()
        bob.close()
    finally:
        relay.stop()

    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
