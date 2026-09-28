"""
Мультиплексор одного порта: TLS и plain HTTP на одном слушающем сокете.

Зачем: раньше HTTPS веб-клиента жил на отдельном порту (HTTP+3) и друг
должен был помнить «8420 — для приложения, 8423 — для браузера». Теперь
оба протокола живут на ОДНОМ порту:

  - первый байт входящего подключения = 0x16 (TLS ClientHello) → это
    браузер по https://IP:8420 — оборачиваем сокет в SSL;
  - любой другой первый байт → это plain HTTP (Qt-клиент, легаси,
    curl) — отдаём как есть.

Как это работает: перед accept() подсматриваем первый байт через
MSG_PEEK (он остаётся в буфере ядра), при необходимости выполняем
TLS-рукопожатие и кладём готовый сокет в очередь, из которой её берёт
QueuedHTTPServer (обычный ThreadingHTTPServer без своего слушателя).

Так friend_relay снова занимает один порт для всего веба: 8420 = http
И https одновременно, а сертификат принимается браузером один раз для
всего (включая WebSocket-туннель /voice/ws на этом же порту).
"""
from __future__ import annotations

import logging
import queue
import socket
import ssl
import threading

log = logging.getLogger("friend_relay.tls_mux")


class TlsPlainMux:
    """Слушает порт, сортирует входящие соединения TLS/plain и кладёт
    готовые (при необходимости уже SSL-обёрнутые) сокеты в очередь."""

    def __init__(self, host: str, port: int, ssl_context: ssl.SSLContext):
        self._host = host
        self._port = port
        self._ctx = ssl_context
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._q: queue.Queue = queue.Queue()

    @property
    def queue(self) -> queue.Queue:
        """Очередь готовых клиентских сокетов для QueuedHTTPServer."""
        return self._q

    @property
    def bound_port(self) -> int:
        return self._sock.getsockname()[1] if self._sock else 0

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self._host, self._port))
            s.listen(16)
            s.settimeout(0.5)
        except OSError as e:
            log.warning("Не удалось занять порт %s: %s", self._port, e)
            return False
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True, name="tls-mux-accept")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _accept_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                conn, _addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            # Подглядывание байта и TLS-рукопожатие — в ОТДЕЛЬНОМ потоке:
            # peek с таймаутом 5с прямо в цикле accept подвешивал приём
            # ВСЕХ остальных клиентов, пока один «молчаливый» (сканер
            # портов, health-check без данных) не дошлёт свой первый байт.
            threading.Thread(target=self._prepare, args=(conn,),
                             daemon=True, name="tls-mux-prepare").start()

    def _prepare(self, conn: socket.socket) -> None:
        """Разбирает одно принятое соединение: TLS/plain и в очередь.
        Ошибка (мусор/сканер/обрыв в рукопожатии) — просто закрываем."""
        try:
            # Подглядываем первый байт (TLS-рукопожатие начинается с
            # 0x16 = record type 22 «handshake»); байт остаётся в буфере
            conn.settimeout(5.0)
            first = conn.recv(1, socket.MSG_PEEK)
            if first == b"\x16" and self._ctx is not None:
                conn = self._ctx.wrap_socket(conn, server_side=True)
            conn.settimeout(None)
            self._q.put(conn)
        except (ssl.SSLError, OSError):
            try:
                conn.close()
            except OSError:
                pass
