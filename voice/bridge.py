"""VoiceBridge — WebSocket-мост между браузером и VoiceMixer.

Проблема: браузеры не умеют raw TCP, поэтому Qt-протокол голоса (порт
HTTP+1, кадры FRVC) для веб-клиента недоступен напрямую. Мост на каждое
браузерное подключение открывает обычный TCP-клиент к локальному
VoiceMixer (127.0.0.1:HTTP+1) и перекачивает фреймы туда-обратно.

ТРАНСКОДИРОВАНИЕ (v1.9.8+): если у хоста есть opuslib_next, мост
согласовывает opus с микшером на СВОЁ подключение и перекодирует на
лету:
  браузер (PCM int16 по WS) → мост кодирует в opus → микшер
  микшер (opus-микс) → мост декодирует в PCM → браузер
Браузер при этом ничего не знает про opus: его WS-протокол — raw PCM
int16 mono 48kHz 20мс (v2.0) + JSON control. Если переговоры не удались
(старый микшер, нет библиотеки) — мост работает байт в байт.

С версии 1.8.0 мост слушает ТОЛЬКО 127.0.0.1 на ephemeral-порте
(port=0) и наружу не торчит: TLS терминирует основной HTTP-сервер,
который принимает GET /voice/ws на своём порту и туннелирует соединение
сюда байт в байт. Браузеру достаточно одного адреса
https://IP:8420 — сертификат принимается один раз на весь origin
(включая wss).

Протокол браузер ↔ мост:
  - первое сообщение — JSON-текст {"name": "...", "access_key": "..."}
    (аналог auth-строки TCP-протокола «NAME|key\\n», но в JSON);
    ответ: {"ok":true,"name":...} либо {"error":"..."}
  - далее:
      текст (JSON)     → управление: {"t":"mute","on":true|false};
                         {"t":"ping","id":N} — проксируется МИКШЕРУ
                         (v3.6.6, R4): pong со счётчиками in/out/odd
                         возвращается браузеру текстом — веб-клиент
                         считает RTT и честные потери ↑↓ (как Qt);
                         {"t":"hb"} — heartbeat самого моста (echo)
      binary           → PCM int16 mono 48kHz; мост режет на 960-сэмпловые
                         (20мс) фреймы, сам считает RMS и ставит флаги
                         silence/muted — как VoiceClient, но без
                         шумоподавления (браузер делает echoCancellation +
                         noiseSuppression сам в getUserMedia)
      binary от моста  → PCM int16 mono 48kHz (смешанный звук микшера)
      текст от моста   → control JSON микшера: {"t":"spk",...},
                         {"t":"spk_all",...} — для зелёных колец

Зависимость: websockets. Если библиотеки нет — мост не поднимается,
чат/файлы/шахматы работают как раньше.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
import struct
import threading
import time

from voice import codec, dsp
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_MUTED,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    PCM_FRAME_BYTES,
    VAD_THRESHOLD,
    make_frame,
)

log = logging.getLogger("friend_relay.voice_bridge")

_MAX_NAME_LEN = 32

_WS_MAX_SIZE = 128 * 1024      # браузер шлёт мелкие фреймы, запас на батчи

# Сколько ждём ACK микшера на запрос opus (старый микшер молчит).
_CODEC_ACK_TIMEOUT = 1.5

# v3.4.1 FIX: WS ping/pong таймауты.
# Раньше ping_interval=20 ping_timeout=20: при свернутой вкладке браузер
# throttling-ит JS-цикл, и pong-ответы приходят с задержкой 30-50 секунд →
# мост считал соединение мёртвым. Теперь 40/90 + отдельный soft-keepalive
# приложенческого уровня JSON `{"t":"hb"}`.
_WS_PING_INTERVAL = 40
_WS_PING_TIMEOUT = 90

# Как часто шлём keepalive по мост→миксер (TCP), если давно ничего не отправилось.
# Иначе VPN/Firewall режут «мёртвое» соединение после 30-60 секунд тишины.
_MIXER_KEEPALIVE_EVERY_S = 15.0


class _BClient:
    """Состояние одного браузерного подключения."""
    __slots__ = ("muted", "name")

    def __init__(self, name: str):
        self.name = name
        self.muted = False


class VoiceBridge:
    """WS-сервер голосового моста. Один экземпляр на сервер хоста."""

    def __init__(self, host: str, port: int, mixer_port: int,
                 access_key: str = "", ssl_context=None):
        self._host = host
        self._port = port
        self._mixer_port = mixer_port
        self._access_key = access_key
        self._ssl_context = ssl_context
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main_task: asyncio.Task | None = None
        self._started = threading.Event()
        self._failed = False
        # Фактический порт после старта (port=0 → ephemeral); читается
        # HTTP-сервером для туннеля /voice/ws
        self.bound_port = 0

    # ── Жизненный цикл ───────────────────────────────────────────────

    def start(self) -> bool:
        """Поднимает мост в фоновом потоке со своим asyncio-циклом."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.warning("Голосовой мост для браузера не поднят: нет "
                        "библиотеки websockets (pip install websockets)")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="voice-bridge")
        self._thread.start()
        self._started.wait(timeout=5)
        if self._failed:
            return False
        log.info("Голосовой мост (WSS) слушает %s:%s — микшер на "
                 "127.0.0.1:%s", self._host, self._port, self._mixer_port)
        return True

    def stop(self) -> None:
        if self._loop is not None and self._main_task is not None:
            try:
                self._loop.call_soon_threadsafe(self._main_task.cancel)
            except RuntimeError:
                pass  # цикл уже мёртв
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._loop = None
        self._main_task = None

    def _run(self) -> None:
        import websockets

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def main():
            server = await websockets.serve(
                self._handler, self._host, self._port,
                ssl=self._ssl_context,
                max_size=_WS_MAX_SIZE,
                ping_interval=_WS_PING_INTERVAL,
                ping_timeout=_WS_PING_TIMEOUT,
            )
            try:
                socks = getattr(server, "sockets", None) or []
                self.bound_port = socks[0].getsockname()[1] if socks \
                    else self._port
            finally:
                self._started.set()
            await asyncio.Future()  # до stop() (cancel main-таски)

        self._main_task = self._loop.create_task(main())
        try:
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass  # штатная остановка через stop()
        except Exception as e:
            if not self._started.is_set():
                self._failed = True
                self._started.set()
                log.warning("Голосовой мост не поднялся: %s", e)
            else:
                log.info("Голосовой мост остановлен (%s)", e)
        finally:
            # отменить оставшиеся задачи (обработчики соединений)
            try:
                pending = [t for t in asyncio.all_tasks(self._loop)
                           if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()

    # ── Обработка одного браузерного подключения ─────────────────────

    async def _handler(self, ws) -> None:
        # 1. Первый кадр — авторизация JSON-текстом
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=5)
        except (TimeoutError, Exception):
            return
        try:
            auth = json.loads(first)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_error(ws, "первое сообщение — JSON "
                                       "{\"name\":..., \"access_key\":...}")
            return
        name = str(auth.get("name") or "").strip()[:_MAX_NAME_LEN]
        key = str(auth.get("access_key") or "")
        if not name:
            await self._send_error(ws, "пустое имя")
            return
        # compare_digest по БАЙТАМ: постоянное время (timing-атаки), плюс
        # не-ASCII ключ на str ронял бы проверку TypeError'ом
        if self._access_key and not hmac.compare_digest(
                key.encode("utf-8"), self._access_key.encode("utf-8")):
            await self._send_error(ws, "неверный ключ доступа")
            return

        # 2. Подключаемся к микшеру как обычный TCP-клиент
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._mixer_port),
                timeout=5)
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP,
                                    socket.TCP_NODELAY, 1)
                except OSError:
                    pass
        except Exception as e:
            await self._send_error(
                ws, f"голосовой сервер недоступен: {e}")
            return
        try:
            writer.write(f"{name}|{self._access_key}\n".encode())
            await writer.drain()
            resp = await asyncio.wait_for(
                reader.readexactly(len(b"AUTH_OK\n")), timeout=5)
            if resp != b"AUTH_OK\n":
                msg = resp.decode("utf-8", errors="replace").strip() or \
                    "голосовой сервер отклонил подключение"
                await self._send_error(ws, msg)
                return
        except (TimeoutError, asyncio.IncompleteReadError, OSError) as e:
            await self._send_error(ws, f"голосовой сервер: {e}")
            return

        # 3. Переговоры кодека с микшером. Слушатели spk_all и roster,
        # пришедшие до ACK, буферизуются и уйдут браузеру сразу после ok
        # — порядок control-событий для веб-клиента сохранён.
        transcode = False
        buffered_controls: list[str] = []
        if codec.available():
            try:
                transcode = await self._negotiate_codec_with_mixer(
                    reader, writer, buffered_controls)
            except Exception:  # микшер не ответил/обрыв — живём на PCM
                transcode = False

        await ws.send(json.dumps({"ok": True, "name": name}))
        for ctrl in buffered_controls:
            try:
                await ws.send(ctrl)
            except Exception:
                pass
        state = _BClient(name)
        log.info("Голос (браузер): «%s» подключился к мосту (кодек: %s)",
                 name, "opus" if transcode else "pcm")
        # v3.6.5 FIX: обе насос-таски ШЛЮТ в один websocket (PCM-поток и
        # control-ответы). websockets 14+ запрещает конкурентные send() из
        # двух задач — бросает ConcurrencyError, и hb-echo молча терялся
        # при активном аудио. Разруливаем общим локом на отправку.
        send_lock = asyncio.Lock()
        try:
            await asyncio.gather(
                self._pump_mixer_to_ws(reader, ws, transcode, send_lock),
                self._pump_ws_to_mixer(ws, writer, state, transcode, send_lock),
            )
        finally:
            try:
                writer.close()
            except OSError:
                pass
            log.info("Голос (браузер): «%s» отключился", name)

    @staticmethod
    async def _negotiate_codec_with_mixer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
            buffered_controls: list[str]) -> bool:
        """Просит микшер opus и ждёт ACK. Control-кадры не про кодек
        (spk_all/roster) буферизуются в buffered_controls. Возвращает
        True если согласован opus. По таймауту/отказу — False (PCM)."""
        writer.write(make_frame(
            FLAG_CONTROL,
            json.dumps({"t": "codec", "codec": "opus"}).encode("utf-8")))
        await writer.drain()
        deadline = asyncio.get_event_loop().time() + _CODEC_ACK_TIMEOUT
        while True:
            timeout_left = deadline - asyncio.get_event_loop().time()
            if timeout_left <= 0:
                return False
            header = await asyncio.wait_for(
                reader.readexactly(FRAME_HEADER_LEN), timeout=timeout_left)
            if header[:4] != MAGIC:
                continue
            flags = header[4]
            (length,) = struct.unpack("<H", header[5:7])
            payload = await reader.readexactly(length) if length else b""
            if not flags & FLAG_CONTROL:
                continue  # аудио до ACK не бывает (prebuffer не набран)
            text = payload.decode("utf-8", errors="replace")
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("t") == "codec":
                return str(msg.get("codec") or "") == "opus"
            buffered_controls.append(text)

    @staticmethod
    async def _send_error(ws, text: str) -> None:
        try:
            await ws.send(json.dumps({"error": text}))
        except Exception:
            pass

    # ── Насосы фреймов ───────────────────────────────────────────────

    async def _pump_mixer_to_ws(self, reader: asyncio.StreamReader, ws,
                                transcode: bool,
                                send_lock: asyncio.Lock) -> None:
        """Микшер → браузер: PCM как binary, control JSON как текст.
        При transcode=True opus-датаграммы декодируются в PCM.

        v3.4.1 FIX: добавлен мягкий keepalive JSON `hb` если давно не было
        данных — не даёт VPN/TLS-terminators разорвать «зависшее» соединение.
        """
        from websockets.exceptions import ConnectionClosed

        async def ws_send(data):
            # v3.6.5: единственная точка отправки — под общим локом
            # (второй насос тоже шлёт в этот ws: hb-echo из браузерного).
            async with send_lock:
                await ws.send(data)

        decoder = codec.Decoder() if transcode else None
        last_out = time.monotonic()
        try:
            while True:
                # Ждём заголовок с таймаутом _MIXER_KEEPALIVE_EVERY_S.
                # Если истёк — шлём HB в браузер и ждём дальше.
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(FRAME_HEADER_LEN),
                        timeout=_MIXER_KEEPALIVE_EVERY_S,
                    )
                except TimeoutError:
                    # keepalive приложенческого уровня (WS ping_interval уже есть,
                    # но некоторые прокси смотрят именно на полезную нагрузку).
                    # v3.6.5: шлём ТЕКСТОМ (раньше .encode делал бинарный кадр —
                    # браузер не мог разобрать JSON и трактовал его как PCM).
                    try:
                        if time.monotonic() - last_out >= _MIXER_KEEPALIVE_EVERY_S:
                            await ws_send(json.dumps(
                                {"t": "hb", "ts": int(time.time())}))
                            last_out = time.monotonic()
                    except Exception:
                        pass
                    continue

                if header[:4] != MAGIC:
                    continue  # мусор в потоке — ждём магию
                flags = header[4]
                (length,) = struct.unpack("<H", header[5:7])
                payload = await reader.readexactly(length) if length else b""
                if flags & FLAG_CONTROL:
                    await ws_send(payload.decode("utf-8", errors="replace"))
                elif flags & FLAG_SILENCE:
                    # Тишина от микшера — всегда нулевой PCM для браузера
                    await ws_send(b"\x00" * PCM_FRAME_BYTES)
                elif decoder is not None:
                    try:
                        await ws_send(decoder.decode(payload))
                    except Exception:
                        await ws_send(b"\x00" * PCM_FRAME_BYTES)
                else:
                    await ws_send(payload)  # чистый PCM int16
                last_out = time.monotonic()
        except (asyncio.IncompleteReadError, asyncio.CancelledError,
                ConnectionError, ConnectionClosed):
            pass  # микшер или браузер закрыл поток — заканчиваем

    async def _pump_ws_to_mixer(self, ws, writer: asyncio.StreamWriter,
                                state: _BClient, transcode: bool,
                                send_lock: asyncio.Lock) -> None:
        """Браузер → микшер: PCM режем на 20мс фреймы, VAD + mute.
        При transcode=True голос кодируется в opus-датаграммы.

        v3.4.1 FIX: если долго нет фреймов — шлём silence-кадр сами каждые
        _MIXER_KEEPALIVE_EVERY_S секунд, чтобы TCP мост←→миксер не закрывался
        VPN/NAT, и микшер видел «клиент ещё жив» по last_seen.
        """
        from websockets.exceptions import ConnectionClosed

        async def ws_send(data):
            # v3.6.5: общая точка отправки под локом (см. _pump_mixer_to_ws)
            async with send_lock:
                await ws.send(data)

        pending = b""
        encoder = codec.Encoder() if transcode else None
        last_wr = time.monotonic()
        # Фоновая таска мягкого keepalive если долго нет входа.
        async def _ka_task() -> None:
            nonlocal last_wr
            try:
                while True:
                    await asyncio.sleep(_MIXER_KEEPALIVE_EVERY_S / 3)
                    silent = (time.monotonic() - last_wr) >= _MIXER_KEEPALIVE_EVERY_S
                    if not silent:
                        continue
                    # Шлём SILENCE-кадр мост→миксер (keepalive + last_seen).
                    # v3.5.6: БЕЗ FLAG_MUTED — микшер читает muted из флага
                    # КАЖДОГО кадра, и keepalive «размьюченного» клиента
                    # ложно помечал его замьюченным на микшере до следующего
                    # реального кадра (семантическая ложь в состоянии).
                    try:
                        writer.write(make_frame(
                            FLAG_SILENCE,
                            b"\x00" * PCM_FRAME_BYTES))
                        await writer.drain()
                        last_wr = time.monotonic()
                    except Exception:
                        return
            except (asyncio.CancelledError, OSError, EOFError):
                return

        ka = asyncio.create_task(_ka_task())
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    try:
                        cmd = json.loads(msg)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    # Принимаем heartbeat от браузера — отвечаем ack.
                    # v3.6.5: шлём ТЕКСТОМ (str → текстовый WS-кадр): раньше
                    # .encode("utf-8") делал бинарный кадр, браузер не мог
                    # распарсить JSON и скармливал его аудио-тракту как PCM.
                    # Плюс лок — при активном аудио конкурентный send из двух
                    # задач кидал ConcurrencyError и echo молча терялся.
                    if cmd.get("t") in ("hb", "heartbeat"):
                        try:
                            await ws_send(json.dumps(
                                {"t": "hb", "ts": cmd.get("ts", int(time.time())),
                                 "ok": True}))
                        except Exception:
                            pass
                        continue
                    # v3.6.6 (R4): НАСТОЯЩИЙ ping к микшеру — трафик качества.
                    # Раньше ping считался heartbeat-алиасом и до микшера не
                    # доходил: браузер не мог измерить RTT и потери. Теперь
                    # кадр уходит микшеру как FRVC-control, а его pong
                    # (текстовый JSON со счётчиками in/out/odd) уже
                    # возвращается браузеру через _pump_mixer_to_ws.
                    if cmd.get("t") == "ping":
                        try:
                            writer.write(make_frame(
                                FLAG_CONTROL, msg.encode("utf-8")))
                            await writer.drain()
                        except (ConnectionError, OSError):
                            return  # микшер умер — насос закроется сам
                        continue
                    if cmd.get("t") == "mute":
                        state.muted = bool(cmd.get("on"))
                    continue
                pending += msg
                sent_any = False
                while len(pending) >= PCM_FRAME_BYTES:
                    pcm = pending[:PCM_FRAME_BYTES]
                    pending = pending[PCM_FRAME_BYTES:]
                    flags = 0
                    if state.muted:
                        flags = FLAG_MUTED | FLAG_SILENCE
                        pcm = b"\x00" * PCM_FRAME_BYTES
                    elif dsp.rms(pcm) < VAD_THRESHOLD:
                        flags = FLAG_SILENCE
                    if flags == 0 and encoder is not None:
                        try:
                            payload = encoder.encode(pcm)
                        except Exception:
                            payload = b"\x00" * PCM_FRAME_BYTES
                            flags = FLAG_SILENCE
                    else:
                        payload = pcm
                    writer.write(make_frame(flags, payload))
                    sent_any = True
                if sent_any:
                    await writer.drain()
                    last_wr = time.monotonic()
        except (ConnectionError, asyncio.CancelledError, ConnectionClosed):
            pass  # браузер ушёл (в т.ч. аварийно, без close-фрейма) — тихо
        # ConnectionClosed в websockets ≥13 — НЕ подкласс ConnectionError:
        # раньше резкий обрыв (закрытая вкладка, пропала сеть) вылетал из
        # хендлера с трейсбеком «connection handler failed» в логи.
        finally:
            try:
                ka.cancel()
                await asyncio.wait_for(ka, timeout=0.5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass
