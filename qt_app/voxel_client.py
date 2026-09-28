"""VoxelSessionClient — TCP-клиент к VoxelSessionServer.

(Живёт в qt_app/, а не в lib/: это Presentation-слой — QObject с Qt-сигналами.
Раньше файл лежал в lib/, и серверный слой тянул PySide6 - классическая
layer leakage.)

QObject с сигналами — все колбэки доставляются в главный поток Qt
(автоматический queued connection). Это исправляет ошибку
"QBasicTimer::start: Timers cannot be started from another thread".
"""
from __future__ import annotations

import json
import socket
import threading

from PySide6.QtCore import QObject, Signal


class VoxelSessionClient(QObject):
    """Клиент одной сессии. Все колбэки — через Qt-сигналы."""

    snapshot_received = Signal(dict)       # snapshot data
    joined = Signal(str, str)               # (session_id, player_id)
    error = Signal(str)                     # error message
    disconnected = Signal()                # connection lost

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock: socket.socket | None = None
        self._running = False
        self._player_id: str = ""
        self._session_id: str = ""

    def connect(self, host: str, port: int, name: str, access_key: str = "") -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((host, port))
            auth = f"{name}|{access_key}\n".encode()
            self._sock.sendall(auth)
            resp = self._sock.recv(64)
            if resp != b"AUTH_OK\n":
                self.error.emit(f"Auth failed: {resp!r}")
                self._sock.close()
                self._sock = None
                return False
            self._sock.settimeout(None)
            self._player_id = name
        except OSError as e:
            self.error.emit(f"Connection failed: {e}")
            self._sock = None
            return False

        self._running = True
        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()
        return True

    def join_session(self, session_id: str, team: str = "none") -> None:
        self._session_id = session_id
        self._send({"type": "join", "session_id": session_id, "team": team})

    def send_input(self, input_data: dict) -> None:
        self._send({
            "type": "input",
            "session_id": self._session_id,
            "data": input_data,
        })

    def leave(self) -> None:
        if self._session_id:
            self._send({"type": "leave"})
        self._running = False
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None and self._running

    @property
    def player_id(self) -> str:
        return self._player_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def _send(self, msg: dict) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendall(
                (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            )
        except OSError:
            self._running = False
            self.disconnected.emit()

    def _recv_loop(self) -> None:
        sock = self._sock
        buf = b""
        try:
            while self._running and sock is not None:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                        self._handle_msg(msg)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except OSError:
            pass
        self._running = False
        self.disconnected.emit()

    def _handle_msg(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "joined":
            sid = msg.get("session_id", "")
            pid = msg.get("player_id", "")
            self._session_id = sid
            self._player_id = pid
            self.joined.emit(sid, pid)
        elif mtype == "snapshot":
            data = msg.get("data", {})
            self.snapshot_received.emit(data)
        elif mtype == "error":
            self.error.emit(msg.get("msg", "unknown error"))
