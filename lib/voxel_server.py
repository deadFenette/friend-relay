"""VoxelSessionServer — TCP-сервер для мультиплеерных сессий Voxel Shooter.

Один TCP-сервер (порт = HTTP-порт + 2) держит несколько сессий (rooms).
Каждый клиент подключается, выбирает сессию (по session_id), и начинает
обмен:
  - Клиент → сервер: input (keys/mouse/actions) — 20 Гц
  - Сервер → клиент: snapshot состояния — 20 Гц

Протокол: текстовые строки JSON, разделённые '\n'.
  - Клиент шлёт: {"type": "input", "session_id": "...", "player_id": "...",
                   "data": {...}}
  - Сервер шлёт: {"type": "snapshot", "session_id": "...", "data": {...}}
                или: {"type": "joined", "session_id": "...", "player_id": "..."}
                или: {"type": "error", "msg": "..."}

Сервер тикает VoxelEngine на 60 Гц (отдельный поток), snapshots отправляет
на 20 Гц (каждые 50мс).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable

from lib.games.voxel.engine import VoxelEngine

# ── Протокол ────────────────────────────────────────────────────────


def _send_json(sock: socket.socket, data: dict) -> None:
    try:
        sock.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def _recv_json_lines(sock: socket.socket, on_msg: Callable[[dict], None]) -> None:
    """Читает из sock пока есть соединение, парсит строки JSON, вызывает on_msg."""
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8"))
                    on_msg(msg)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except OSError:
        pass


# ── Менеджер сессий ─────────────────────────────────────────────────


class VoxelSessionServer:
    """TCP-сервер мультиплеерных сессий.

    Запускается RelayServer'ом одновременно с HTTP и VoiceMixer.
    Один инстанс, держит dict sessions: session_id → VoxelEngine.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8422,
                 access_key: str = ""):
        self.host = host
        self.port = port
        self.access_key = access_key
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._tick_thread: threading.Thread | None = None
        self._running = False

        # Сессии: session_id → {"engine": VoxelEngine, "clients": [(sock, player_id)]}
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Жизненный цикл ─────────────────────────────────────────────

    def start(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(16)
            self._sock.settimeout(0.5)
        except OSError:
            self._sock = None
            return False

        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for sess in self._sessions.values():
                for sock, _pid in sess["clients"]:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._sessions.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def is_running(self) -> bool:
        return self._sock is not None and self._running

    # ── Публичные методы для HTTP API ──────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Возвращает список сессий для HTTP /voxel/sessions."""
        with self._lock:
            return [
                {
                    "session_id": sid,
                    "players": len(s["engine"].players),
                    "max_players": s["engine"].max_players,
                    "wave": s["engine"].wave,
                    "bots": sum(1 for p in s["engine"].players.values() if p.is_bot),
                    "pvp": s["engine"]._has_pvp(),
                    "game_mode": s["engine"].game_mode,
                    "created_ts": s["engine"].created_ts,
                }
                for sid, s in self._sessions.items()
            ]

    def create_session(self, session_id: str, max_players: int = 8,
                       game_mode: str = "pve", wall_hp: int = 0,
                       team_red_size: int = 5, team_blue_size: int = 5) -> bool:
        with self._lock:
            if session_id in self._sessions:
                return False
            self._sessions[session_id] = {
                "engine": VoxelEngine(
                    session_id=session_id,
                    max_players=max_players,
                    game_mode=game_mode,
                    wall_hp_override=wall_hp,
                    team_red_size=team_red_size,
                    team_blue_size=team_blue_size,
                ),
                "clients": [],
            }
        return True

    def start_game(self, session_id: str) -> bool:
        """Запускает игру: заполняет пустые слоты ботами, переводит в playing."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            return sess["engine"].start_game()

    def fill_bots(self, session_id: str) -> int:
        """Заполняет пустые слоты ботами. Возвращает сколько добавил."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return 0
            return sess["engine"].fill_bots()

    def add_bot_to_session(self, session_id: str, team: str = "none") -> bool:
        """Добавляет NPC-бота в сессию. Используется из HTTP /voxel/addbot."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            try:
                sess["engine"].add_bot(team=team)
                return True
            except RuntimeError:
                return False  # session full

    # ── Приём подключений ──────────────────────────────────────────

    def _accept_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                conn, _addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            # Авторизация: первая строка "NAME|access_key\n"
            try:
                conn.settimeout(5.0)
                auth = b""
                while b"\n" not in auth and len(auth) < 256:
                    chunk = conn.recv(64)
                    if not chunk:
                        break
                    auth += chunk
                conn.settimeout(None)
                auth_str = auth.decode("utf-8", errors="replace").strip()
                parts = auth_str.split("|", 1)
                name = parts[0] if parts else "?"
                key = parts[1] if len(parts) > 1 else ""
                if self.access_key and key != self.access_key:
                    conn.sendall(b"AUTH_FAIL\n")
                    conn.close()
                    continue
                conn.sendall(b"AUTH_OK\n")
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            # Запускаем поток приёма
            t = threading.Thread(target=self._client_loop, args=(conn, name), daemon=True)
            t.start()

    def _client_loop(self, sock: socket.socket, player_name: str) -> None:
        """Обрабатывает одного клиента: ждёт команды join/input/leave."""
        player_id = player_name  # используем имя как id
        current_session: str | None = None

        def on_msg(msg: dict) -> None:
            nonlocal current_session
            mtype = msg.get("type")
            if mtype == "join":
                sid = msg.get("session_id", "")
                team = msg.get("team", "none")
                with self._lock:
                    sess = self._sessions.get(sid)
                    if sess is None:
                        # Создаём если не существует
                        if sid:
                            self._sessions[sid] = {
                                "engine": VoxelEngine(session_id=sid),
                                "clients": [],
                            }
                            sess = self._sessions[sid]
                    if sess is None:
                        _send_json(sock, {"type": "error", "msg": "bad session"})
                        return
                    try:
                        sess["engine"].add_player(player_id, player_name, team=team)
                        sess["clients"].append((sock, player_id))
                        current_session = sid
                    except RuntimeError as e:
                        _send_json(sock, {"type": "error", "msg": str(e)})
                        return
                _send_json(sock, {"type": "joined", "session_id": sid, "player_id": player_id})

            elif mtype == "input" and current_session:
                with self._lock:
                    sess = self._sessions.get(current_session)
                    if sess is None:
                        return
                    try:
                        sess["engine"].update_player_input(player_id, msg.get("data", {}))
                    except Exception:
                        pass

            elif mtype == "leave" and current_session:
                self._remove_client(current_session, sock, player_id)
                current_session = None

        try:
            _recv_json_lines(sock, on_msg)
        finally:
            if current_session:
                self._remove_client(current_session, sock, player_id)

    def _remove_client(self, session_id: str, sock: socket.socket, player_id: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return
            sess["clients"] = [(s, pid) for s, pid in sess["clients"] if s is not sock]
            sess["engine"].remove_player(player_id)
            # Если сессия пустая — удаляем (через минуту после последнего выхода)
            if not sess["clients"] and not sess["engine"].players:
                # Помечаем время последней активности; удаление в _tick_loop
                sess["engine"].last_activity = time.time()

    # ── Тик симуляции ──────────────────────────────────────────────

    def _tick_loop(self) -> None:
        """60 Гц тик всех сессий + 20 Гц отправка snapshots."""
        last_tick = time.time()
        last_snapshot = time.time()
        tick_interval = 1.0 / 60  # 60 FPS
        snapshot_interval = 0.05  # 20 Гц

        while self._running:
            now = time.time()

            # Тик всех сессий
            if now - last_tick >= tick_interval:
                with self._lock:
                    for sid, sess in list(self._sessions.items()):
                        try:
                            sess["engine"].tick()
                        except Exception:
                            pass  # не роняем сервер из-за одного тика
                        # Удаляем пустые сессии через 5 минут без активности
                        if (not sess["clients"] and not sess["engine"].players
                                and now - sess["engine"].last_activity > 300):
                            del self._sessions[sid]
                last_tick = now

            # Snapshot отправка
            if now - last_snapshot >= snapshot_interval:
                with self._lock:
                    for sid, sess in self._sessions.items():
                        if not sess["clients"]:
                            continue
                        snap = sess["engine"].to_snapshot()
                        msg = {"type": "snapshot", "session_id": sid, "data": snap}
                        # Отправляем всем клиентам сессии
                        for sock, _pid in sess["clients"]:
                            _send_json(sock, msg)
                last_snapshot = now

            # Спим немного чтобы не грузить CPU
            time.sleep(0.005)
