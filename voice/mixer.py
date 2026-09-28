"""VoiceMixer — TCP-сервер голосовых каналов с настоящим смешиванием.

v3.6.6 — ОТПРАВКА НЕ В ПОТОКЕ ТАКТА. Раньше flush() звал conn.sendall()
ПРЯМО в _mix_loop: у одного клиента с замиравшим TCP (ZeroTier чихнул,
ретрансмиты, нулевое окно) sendall вставал на СОТНИ МС — и такт микшера
стоял ВМЕСТЕ с ним: ТИШИНА У ВСЕХ УЧАСТНИКОВ, хотя сеть плохая только у
одного. Теперь у каждого клиента свой поток-отправитель: такт только
КЛАДЁТ кадры в очередь и будит поток (flush() = event.set(), никогда
не блокируется), сетевой I/O из микс-лупа исчез полностью.

ГЛАВНОЕ В v2.0 — АБСОЛЮТНЫЙ ТАКТ МИКШЕРА. Старый цикл выравнивал каждый
тик ОТНОСИТЕЛЬНО предыдущего (sleep(0.01 - elapsed)); time.sleep всегда
спит чуть ДОЛЬШЕ запрошенного, запаздывание НАКАПЛИВАЛОСЬ, и такт плыл
(на Windows — сильно). Клиенты шлют ровно 50 кадров/с, микшер успевал
разбирать меньше — входные очереди росли, переполнялись, и по 60мс речи
выбрасывалось каждые несколько секунд. На слух это «прожёвывание» и
робот-голос — главная причина «ужасного звука» v1.9.x.

Теперь такт привязан к абсолютной шкале perf_counter: сколько кадров
должно было уйти с момента старта — столько и уходит (1-2 кадра
догоняем пачкой, необратимое отставание — ресинхронизация без взрыва).
На Windows дополнительно поднимается разрешение системного таймера
(timeBeginPeriod(1)).

Остальное:
  - Настоящий микс PCM: сервер суммирует int16 всех говорящих в один
    кадр, нормализация sqrt(n) + клиппинг (см. dsp.mix_frames)
  - Джиттер-буфер входа: prebuffer 60мс, догон при переполнении,
    PLC — для opus-целей ЧЕСТНЫЙ PLC libopus, для PCM затухающий хвост
  - VAD-флаги клиентов: индикатор «говорит» с гистерезисом 300мс
  - Пока в канале «активно» (0.5с после последнего голоса), поток
    непрерывный: каждый клиент получает кадр каждый тик (микс или
    silence) — у слушателей не кончается буфер, нет щелчков на стыках
  - Переговоры кодека: control-кадр {"t":"codec","codec":"opus"} → ACK

Слушает на TCP-порту = HTTP-порт + 1. Проводной протокол — voice/protocol.py.
"""
from __future__ import annotations

import hmac
import json
import logging
import socket
import struct
import sys
import threading
import time

from voice import codec, dsp
from voice import app_version
from voice.config import (
    MIXER_ACTIVE_WINDOW_S,
    MIXER_HEARTBEAT_TIMEOUT_S,
    MIXER_JITTER_LIMIT,
    MIXER_KEEPALIVE_EVERY_S,
    MIXER_MAX_CLIENTS,
    MIXER_PLC_MAX_FRAMES,
    MIXER_PREBUF_FRAMES,
    MIXER_REPREBUF_FRAMES,
    MIXER_SEND_BUF_LIMIT,
    MIXER_SPEAKING_HOLD_FRAMES,
    WIN_TIME_BEGIN_PERIOD_MS,
)
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_MUTED,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    PCM_FRAME_BYTES,
    frame_align_cut,
    make_frame,
)

log = logging.getLogger("friend_relay.voice.mixer")

# Кодеки согласования (проводные значения control-кадров).
CODEC_PCM = "pcm"
CODEC_OPUS = "opus"

# Макс. кадров, догоняемых за одну итерацию цикла такта: типичный
# недосып sleep — 1-2 кадра; больше = реальный застой, ресинхронизация.
_MAX_CATCHUP_FRAMES = 3


class _Client:
    """Один подключённый голосовой клиент."""

    def __init__(self, conn: socket.socket, addr: tuple, name: str):
        self.conn = conn
        self.addr = addr
        self.name = name
        self.muted = False
        self.alive = True
        self.last_seen = time.time()
        # Индикатор «говорит» (для зелёного кольца у остальных)
        self.speaking = False
        self.silence_run = 0  # подряд идущие немые фреймы
        # Согласованный кодек клиента: "pcm" (по умолчанию) или "opus"
        # (после успешных переговоров control-кадром). При opus клиент
        # шлёт opus-датаграммы, а микшер ему кодирует каждый микс.
        self.codec = CODEC_PCM
        # v3.5.7: счётчики голосовых кадров ДЛЯ ОЦЕНКИ ПОТЕРЬ (ping/pong):
        # frames_in — сколько голосовых кадров принято ОТ клиента
        # (включая silence/muted — любые не-control), frames_out —
        # сколько аудио-кадров отправлено ЕМУ (включая silence).
        # Клиент сравнивает их дельты со своими счётчиками и получает
        # честные проценты потерь вверх/вниз.
        self.frames_in = 0
        self.frames_out = 0
        # Энкодер микса для этого клиента (создаётся при согласовании).
        self._encoder = None
        # Декодер opus-вклада этого клиента (создаётся лениво); он же
        # даёт честный PLC libopus при опоздании кадров.
        self._decoder = None
        # Очередь входящих фреймов (jitter-буфер, ВСЕГДА в декодированном
        # PCM — микшер оперирует только им). Без maxlen: переполнение
        # гасим догоном до prebuffer (см. _recv_loop).
        self.incoming: list[bytes] = []
        # Плейаут этого клиента стартовал (prebuffer набран)
        self.primed = False
        # Уже был звук от этого клиента (после паузы хватает короткого
        # re-prebuffer)
        self.had_audio = False
        # Подряд идущие тики без кадра от клиента (для PLC)
        self.gap_run = 0
        # Последний ГОЛОСОВОЙ кадр в PCM (для PCM-PLC — затухающий хвост)
        self.last_pcm = b"\x00" * PCM_FRAME_BYTES
        # Очередь отправки
        self._send_buf = bytearray()
        self._send_lock = threading.Lock()
        # v3.6.6: поток-отправитель этого клиента. Такт микшера и
        # broadcast'ы control-кадров только КЛАДУТ в _send_buf и будят
        # событием; фактический sendall делает ровно один поток —
        # порядок кадров сохранён, а замиравший сокет больше НЕ
        # останавливает такт для остальных.
        self._out_event = threading.Event()
        self._sender_alive = False
        self._sender_thread: threading.Thread | None = None
        # v3.6.4: сколько голосовых кадров этот клиент прислал НЕ ТОГО
        # размера (PCM-клиент с кадром ≠ 20мс/48кГц — почти всегда
        # старая версия приложения). Счётчик уходит клиенту в pong
        # («odd»), чтобы у него в UI появился честный сигнал «обнови
        # обе стороны», а не «вообще не слышно» без объяснений.
        self.odd_frames = 0

    # ── Кодек клиента ─────────────────────────────────────────────

    def set_codec(self, codec_name: str) -> None:
        """Переключает исходящий кодек микса на opus (после согласования)."""
        if codec_name == CODEC_OPUS and codec.available():
            self.codec = CODEC_OPUS
            if self._encoder is None:
                self._encoder = codec.Encoder()

    def encode_for(self, pcm: bytes) -> bytes:
        """Кодирует микс в формат этого клиента (PCM — как есть)."""
        if self.codec == CODEC_OPUS and self._encoder is not None:
            try:
                return self._encoder.encode(pcm)
            except Exception:
                # Сбой кодирования (не должно случаться) — шлём тишину,
                # следующий кадр перекроет.
                return b"\x00" * PCM_FRAME_BYTES
        return pcm

    # ── Отправка ──────────────────────────────────────────────────

    def enqueue_outgoing(self, pcm: bytes, is_silence: bool = False) -> None:
        # Silence-кадры НЕ кодируем: payload тишины клиенты игнорируют,
        # а экономия CPU микшера ощутимая.
        if is_silence:
            flags = FLAG_SILENCE
            payload = b"\x00" * PCM_FRAME_BYTES
        else:
            flags = 0x00
            payload = self.encode_for(pcm)
        frame = make_frame(flags, payload)
        self.frames_out += 1  # v3.5.7: для оценки потерь вниз (в pong)
        with self._send_lock:
            self._send_buf.extend(frame)
            self._trim_send_buf()
        self._out_event.set()  # v3.6.6: будим поток-отправитель

    def enqueue_control(self, payload: bytes) -> None:
        """Служебный JSON-фрейм (индикатор говорящих, переговоры кодека)."""
        frame = make_frame(FLAG_CONTROL, payload)
        with self._send_lock:
            self._send_buf.extend(frame)
            self._trim_send_buf()
        self._out_event.set()  # v3.6.6: будим поток-отправитель

    def _trim_send_buf(self) -> None:
        """Ограничиваем очередь — иначе при медленном клиенте память растёт.
        Режем по границе кадров: клиент теряет «хвост» очереди, но
        протокол остаётся целым и следующие кадры читаются корректно."""
        if len(self._send_buf) <= MIXER_SEND_BUF_LIMIT:
            return
        cut = frame_align_cut(self._send_buf, MIXER_SEND_BUF_LIMIT // 2)
        if cut:
            del self._send_buf[:cut]
        # Если выровнять не удалось (один гигантский кадр?) — дропаем его
        # целиком, иначе буфер не уменьшится никогда.
        if len(self._send_buf) > MIXER_SEND_BUF_LIMIT and not cut:
            self._send_buf.clear()

    # ── Поток-отправитель (v3.6.6) ──────────────────────────────

    def start_sender(self) -> None:
        """Запускает поток-отправитель этого клиента (после регистрации)."""
        self._sender_alive = True
        self._sender_thread = threading.Thread(
            target=self._sender_loop, daemon=True,
            name=f"voice-send-{self.name}")
        self._sender_thread.start()

    def stop_sender(self) -> None:
        """Гасит поток-отправитель (уход клиента/остановка микшера)."""
        self._sender_alive = False
        self._out_event.set()

    def _sender_loop(self) -> None:
        """ЕДИНСТВЕННЫЙ отправитель этого клиента: выгребает _send_buf
        целиком и шлёт одним sendall. Замиравший сокет (ZeroTier/VPN)
        блокирует ТОЛЬКО этот поток — такт микшера и остальные клиенты
        продолжают работать; переполнение гасит _trim_send_buf
        (дроп хвоста по границе кадров).

        Был ОДИН sendall на весь микшер в _mix_loop (до v3.6.6): один
        залипший клиент = тишина у ВСЕХ. Ошибка сокета — помечаем
        клиента мёртвым и закрываем conn: recv-поток увидит EOF и
        запустит штатную уборку (roster, кольцо)."""
        while self._sender_alive:
            if not self._out_event.wait(0.5):
                continue
            self._out_event.clear()
            while self._sender_alive:
                with self._send_lock:
                    if not self._send_buf:
                        break
                    data = bytes(self._send_buf)
                    self._send_buf.clear()
                try:
                    self.conn.sendall(data)
                except OSError:
                    self.alive = False
                    try:
                        self.conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        self.conn.close()
                    except OSError:
                        pass
                    return

    def flush(self) -> None:
        """v3.6.6: только БУДИТ поток-отправитель — никогда не блокирует.
        (Раньше здесь был sendall прямо в потоке такта микшера.)"""
        self._out_event.set()


class VoiceMixer:
    """TCP-сервер голосового канала с настоящим смешиванием."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8421,
                 access_key: str = ""):
        self.host = host
        self.port = port
        self.access_key = access_key
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._mix_thread: threading.Thread | None = None
        self._running = False
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        # Возможность микшера для spk_all-снапшота (клиенты решают,
        # стоит ли просить opus).
        self._codec_name = CODEC_OPUS if codec.available() else CODEC_PCM
        # Время последнего «живого» тика (был звук или набирается
        # prebuffer) — пока окно активно, поток рассылки непрерывный.
        self._last_voice = 0.0
        # v3.5.7 ФИКС: _last_keepalive ОБЯЗАН существовать с момента
        # старта. Раньше он появлялся только после первого голосового
        # кадра (self._last_keepalive = now в ветке голоса), а до тех
        # пор getattr(self, "_last_keepalive", now) возвращал дефолт
        # = «сейчас» на КАЖДОМ тике — ka_needed никогда не срабатывал.
        # Итог: свежий канал, где все молчат с самого начала, НЕ слал
        # keepalive вообще → NAT/VPN резали соединения через 30-60с →
        # ложные «соединение разорвано» у клиентов.
        self._last_keepalive = time.time()
        self._timer_period_raised = False
        # v3.6.4: версия приложения хоста для рукопожатия (spk_all «v»)
        self._app_version = app_version()
        # (v2.0.5) Опциональные колбэки телеметрии: фасад (relay_server)
        # ставит сюда счётчики voice.join/voice.leave. Имя клиента НЕ
        # передаётся принципиально — телеметрия считает факты, не людей.
        self.on_join = None    # callable[[] -> None] | None
        self.on_leave = None   # callable[[] -> None] | None

    def _notify(self, cb) -> None:
        """Безопасный вызов колбэка: телеметрия не имеет права ронять
        голосовой микшер (любая ошибка — гасим молча)."""
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    # ── Жизненный цикл ─────────────────────────────────────────────

    def start(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(8)
            self._sock.settimeout(0.5)
        except OSError:
            self._sock = None
            return False

        self._running = True
        self._raise_windows_timer_resolution()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="voice-accept")
        self._thread.start()
        self._mix_thread = threading.Thread(target=self._mix_loop, daemon=True,
                                            name="voice-mix")
        self._mix_thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for c in self._clients:
                # v3.6.6: сначала гасим потоки-отправители
                c.stop_sender()
                try:
                    c.conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    c.conn.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._mix_thread is not None:
            self._mix_thread.join(timeout=1.0)
            self._mix_thread = None
        self._restore_windows_timer_resolution()

    def is_running(self) -> bool:
        return self._sock is not None and self._running

    def participants(self) -> list[str]:
        with self._lock:
            return [c.name for c in self._clients if c.alive]

    # ── Windows: точность системного таймера для такта 50 Гц ───────

    def _raise_windows_timer_resolution(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            rc = ctypes.windll.winmm.timeBeginPeriod(WIN_TIME_BEGIN_PERIOD_MS)
            self._timer_period_raised = (rc == 0)
        except Exception:
            pass

    def _restore_windows_timer_resolution(self) -> None:
        if sys.platform != "win32" or not self._timer_period_raised:
            return
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(WIN_TIME_BEGIN_PERIOD_MS)
        except Exception:
            pass
        self._timer_period_raised = False

    # ── Приём новых подключений ─────────────────────────────────────

    def _accept_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                conn, addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            # Авторизация и регистрация — в ОТДЕЛЬНОМ потоке: чтение
            # auth-строки с таймаутом 5с раньше шло прямо в цикле accept,
            # и одно «молчаливое» подключение (сканер портов, кривой
            # клиент) подвешивало приём ВСЕХ остальных на эти 5 секунд.
            threading.Thread(target=self._register_client,
                             args=(conn, addr), daemon=True).start()

    def _register_client(self, conn: socket.socket, addr) -> None:
        """Авторизация одного подключения и ввод его в состав канала.
        Работает в своём потоке (см. _accept_loop) — медленный/молчащий
        клиент задерживает только себя, а не весь сервер."""
        # Авторизация: первая строка — "NAME|access_key\n" (ЗАМОРОЖЕНА)
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
            # Сравнение в постоянном времени по БАЙТАМ (compare_digest на
            # str принимает только ASCII — а ключ может быть и кириллицей)
            if self.access_key and not hmac.compare_digest(
                    key.encode("utf-8"), self.access_key.encode("utf-8")):
                conn.sendall(b"AUTH_FAIL\n")
                conn.close()
                return
            with self._lock:
                if len(self._clients) >= MIXER_MAX_CLIENTS:
                    conn.sendall(b"FULL\n")
                    conn.close()
                    return
            conn.sendall(b"AUTH_OK\n")
            # NODELAY: фреймы идут каждые 20мс — накопление в ядре
            # (Nagle) превращает ровный поток во всплески и выливается
            # в джиттер у слушателей
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            return

        client = _Client(conn, addr, name)
        # v3.6.6: поток-отправитель стартует ДО ввода в канал — первый
        # spk_all уходит через очередь и этот поток (не блокируя никого).
        client.start_sender()
        with self._lock:
            # Чистим умерших ДО проверки лимита: иначе после N суммарных
            # подключений сервер навсегда отвечал FULL.
            self._clients = [c for c in self._clients if c.alive]
            if len(self._clients) >= MIXER_MAX_CLIENTS:
                conn.sendall(b"FULL\n")
                conn.close()
                return
            self._clients.append(client)
            self._notify(self.on_join)   # (v2.0.5) телеметрия: voice.join
            # Новому клиенту сразу отправляем кто сейчас говорит, чтобы
            # кольца были на месте с первой секунды. Заодно объявляем
            # возможность кодека (поле "codec").
            try:
                snapshot = {c.name: c.speaking for c in self._clients
                            if c.alive}
                client.enqueue_control(
                    json.dumps({"t": "spk_all", "states": snapshot,
                                "codec": self._codec_name,
                                # v3.6.4: версия приложения хоста — клиент
                                # сравнит со своей и предупредит о
                                # несовпадении (разные версии = риск
                                # «бурундука»/тишины). Старые клиенты
                                # поле молча игнорируют.
                                "v": self._app_version}).encode("utf-8")
                )
            except (TypeError, ValueError):
                pass
        # Состав канала изменился — сообщаем всем (в т.ч. новичку).
        self._broadcast_roster()
        t = threading.Thread(target=self._recv_loop, args=(client,),
                             daemon=True)
        t.start()

    # ── Приём PCM от клиента ──────────────────────────────────────

    def _recv_loop(self, client: _Client) -> None:
        conn = client.conn
        try:
            while client.alive and self._running:
                header = self._recv_exact(conn, FRAME_HEADER_LEN)
                if header is None:
                    break
                if header[:4] != MAGIC:
                    continue
                flags = header[4]
                length = struct.unpack("<H", header[5:7])[0]
                if length > MAX_PAYLOAD_BYTES:
                    break  # зло
                payload = self._recv_exact(conn, length) if length else b""
                if length and payload is None:
                    break
                client.last_seen = time.time()

                # ── Control-кадры от клиента (переговоры кодека, пинг) ─
                if flags & FLAG_CONTROL:
                    self._handle_client_control(client, payload)
                    continue

                # v3.5.7: считаем КАЖДЫЙ голосовой кадр (включая
                # silence/muted) — счётчик идёт в pong, клиент по дельтам
                # считает потери ВВЕРХ. Mute-кадры тоже идут в счёт,
                # иначе замьюченный клиент видел бы фальшивые 100% потерь.
                client.frames_in += 1

                client.muted = bool(flags & FLAG_MUTED)
                is_silence = bool(flags & FLAG_SILENCE)

                # Индикатор «говорит» обновляется на КАЖДОМ кадре, включая
                # muted (иначе кольцо замьюченного посреди речи заливало
                # бы навсегда — mute-кадры не увеличивали счётчик тишины).
                self._update_speaking(
                    client,
                    not client.muted and not is_silence)

                # ── Декодируем вклад клиента в PCM ───────────────────
                if client.muted:
                    continue  # не кладём ничего, миксер даст тишину
                if is_silence:
                    # Сайленс-фреймы кладём НУЛЯМИ: шум пауз (дыхание, фон
                    # микрофона) не должен суммироваться в миксе от каждого
                    # участника — именно эта подкладка слышалась как шипение
                    # на концах фраз.
                    client.incoming.append(b"\x00" * PCM_FRAME_BYTES)
                    client.had_audio = True
                elif client.codec == CODEC_OPUS:
                    pcm = self._decode_client_frame(client, payload)
                    if pcm is None:
                        continue  # битый кадр — пропускаем
                    client.incoming.append(pcm)
                    client.last_pcm = pcm
                    client.had_audio = True
                elif length == PCM_FRAME_BYTES:
                    client.incoming.append(payload)
                    client.last_pcm = payload
                    client.had_audio = True
                else:
                    # v3.6.4: кадр НЕ того размера. opus-клиенты сюда не
                    # попадают (их ветка выше) — это PCM-клиент шлёт
                    # кадры не 20мс/48кГц: почти наверняка СТАРАЯ ВЕРСИЯ
                    # (аудио-формат 16кГц/10мс давал 320-байтные кадры).
                    # Раньше молча игнорировали — у собеседников была
                    # «полная тишина» без единой подсказки почему.
                    client.odd_frames += 1
                    if client.odd_frames == 1:
                        log.warning(
                            "Голос: клиент «%s» (%s) шлёт PCM-кадры по %s байт "
                            "вместо %s — скорее всего старая версия приложения; "
                            "его голос НЕ БУДЕТ слышен, обнови ОБЕ стороны",
                            client.name, client.addr[0] if client.addr else "?",
                            length, PCM_FRAME_BYTES)
                    continue  # мусорный размер — не голос

                # Догон при переполнении: максимум backlog = LIMIT, лишнее
                # спереди выбрасываем до prebuffer (ограничивает задержку —
                # свежесть речи важнее полной доставки)
                if len(client.incoming) > MIXER_JITTER_LIMIT:
                    drop = len(client.incoming) - MIXER_PREBUF_FRAMES
                    del client.incoming[:drop]
        except OSError:
            pass
        finally:
            client.alive = False
            # v3.6.6: гасим поток-отправитель (он мог висеть на sendall —
            # conn.close() ниже добьёт его OSError'ом, если ещё не вышел).
            client.stop_sender()
            # (v2.0.5) телеметрия: voice.leave (факт, без имени)
            self._notify(self.on_leave)
            # Ушедший из канала больше не «говорит» — гасим кольцо.
            if client.speaking:
                client.speaking = False
                self._broadcast_speaking(client.name, False)
            try:
                conn.close()
            except OSError:
                pass
            # Состав канала изменился — сообщаем остальным.
            self._broadcast_roster()

    @staticmethod
    def _decode_client_frame(client: _Client, payload: bytes) -> bytes | None:
        """Opus-датаграмма клиента → 1920 байт PCM (None при сбое)."""
        try:
            dec = client._decoder
            if dec is None:
                dec = client._decoder = codec.Decoder()
            return dec.decode(payload)
        except Exception:
            return None

    def _handle_client_control(self, client: _Client, payload: bytes) -> None:
        """Control-кадры ОТ клиента: переговоры кодека и ping/pong
        (v3.5.7, качество связи во вкладке «Голос»). Прочие JSON
        молча игнорируются."""
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("t")
        if t == "ping":
            # Эхо-ответ с нашими счётчиками кадров этого клиента: клиент
            # считает RTT и честные потери вверх/вниз из дельт.
            try:
                pid = int(msg.get("id"))
            except (TypeError, ValueError):
                return
            try:
                client.enqueue_control(json.dumps({
                    "t": "pong", "id": pid,
                    "in": client.frames_in,
                    "out": client.frames_out,
                    # v3.6.4: отвергнутые по размеру кадры этого клиента
                    "odd": client.odd_frames,
                }).encode("utf-8"))
            except (TypeError, ValueError):
                pass
            return
        if t != "codec":
            return
        requested = str(msg.get("codec") or "")
        if requested == CODEC_OPUS and codec.available():
            client.set_codec(CODEC_OPUS)
            answer = CODEC_OPUS
        else:
            answer = CODEC_PCM
        try:
            client.enqueue_control(
                json.dumps({"t": "codec", "codec": answer}).encode("utf-8"))
        except (TypeError, ValueError):
            pass

    def _update_speaking(self, client: _Client, is_voice: bool) -> None:
        """Трекинг «говорит/молчит» с гистерезисом: после последнего
        голосового кадра индикатор горит ещё MIXER_SPEAKING_HOLD_FRAMES
        (300мс) — не мигает на паузах между словами. О переходах
        сообщаем ВСЕМ остальным. Вызывается только из recv-потока этого
        клиента (без гонок на полях)."""
        if is_voice:
            client.silence_run = 0
            if not client.speaking:
                client.speaking = True
                self._broadcast_speaking(client.name, True, exclude=client)
        else:
            client.silence_run += 1
            if client.speaking \
                    and client.silence_run >= MIXER_SPEAKING_HOLD_FRAMES:
                client.speaking = False
                self._broadcast_speaking(client.name, False, exclude=client)

    def _broadcast_speaking(
        self, name: str, on: bool, exclude: _Client | None = None
    ) -> None:
        """Рассылает переход «говорит вкл/выкл» всем, кроме самого
        говорящего (его кольцо обновляет локальный VAD без сети)."""
        try:
            payload = json.dumps(
                {"t": "spk", "name": name, "on": on}).encode("utf-8")
        except (TypeError, ValueError):
            return
        with self._lock:
            targets = [c for c in self._clients
                       if c.alive and c is not exclude]
        for c in targets:
            c.enqueue_control(payload)

    def _broadcast_roster(self) -> None:
        """Рассылает всем актуальный состав канала ({"t":"roster", ...}).

        Отправляется при входе/выходе участника: веб-клиент строит список
        «кто в канале» только из control-фреймов (поллинга
        /voice/participants у него нет) — без roster-а молчащие новички
        не появлялись, а ушедшие оставались в списке навсегда. Qt-клиент
        roster игнорирует (у него свой поллинг)."""
        with self._lock:
            names = [c.name for c in self._clients if c.alive]
            targets = [c for c in self._clients if c.alive]
        try:
            payload = json.dumps(
                {"t": "roster", "names": names}).encode("utf-8")
        except (TypeError, ValueError):
            return
        for c in targets:
            c.enqueue_control(payload)

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    # ── Микс + рассылка ────────────────────────────────────────────

    def _mix_loop(self) -> None:
        """Цикл такта микшера — АБСОЛЮТНОЕ ВРЕМЯ, не sleep.

        Сколько 20мс-кадров прошло с момента старта — столько тиков и
        выполняется, независимо от дрейфа time.sleep. Отставание в 1-2
        кадра (штатный недосып) догоняем пачкой; необратимое отставание
        (застой GIL, suspend процесса) — ресинхронизация: пропущенные
        кадры не восстанавливаем, клиенты прикрываются PLC, а взрыва
        рассылки из прошлого не происходит.
        """
        tick_s = 0.02  # 20мс = 1 кадр
        # v3.5.7: поток такта микшера — САМОЕ времязависимое место всего
        # сервера. Когда браузер хоста играет музыку (fast-sync каждые
        # 400мс, декодирование, рендер прогресс-бара) и соседние потоки
        # съедают CPU, такт 50Гц срывается => всплески/догон у клиентов.
        # Поднимаем приоритет потока: голос перестаёт «хромать» при игре
        # музыки. Windows-only (на Linux nice не поднять без привилегий),
        # ошибка игнорируется — это оптимизация, не требование.
        if sys.platform == "win32":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                # THREAD_PRIORITY_ABOVE_NORMAL = 1
                k32.SetThreadPriority(k32.GetCurrentThread(), 1)
            except Exception:
                pass
        t0 = time.perf_counter()
        done = 0
        while self._running:
            now = time.perf_counter()
            due = int((now - t0) / tick_s)
            if due > done:
                catchup = min(due - done, _MAX_CATCHUP_FRAMES)
                for _ in range(catchup):
                    self._tick_once()
                done += catchup
                if done < due:
                    # Отстали больше _MAX_CATCHUP — догонять «взрывом»
                    # нельзя: ресинхронизируемся, потерянные кадры
                    # прикроет PLC на клиентах.
                    done = due
            else:
                # Спим ДО момента следующего тика (не «ещё 20мс» —
                # именно остаток до абсолютной границы).
                time.sleep(max(0.001, t0 + (done + 1) * tick_s - now))

    def _tick_once(self) -> None:
        """Один тик микшера:
        1. Каждый клиент: взять кадр из jitter-буфера (или PLC, если
           кадр опоздал, — это исключает щелчки от разрыва волны).
        2. Смешать ЧУЖИЕ вклады (своё клиенту не возвращается — эхо).
        3. Пока в канале «активно» (недавно был звук), поток непрерывный:
           даже если все молчат — клиенты получают silence-кадры.
        4. v3.4.1 FIX: SOFT KEEPALIVE — даже при полной тишине >N секунд
           микшер шлёт 1 silence-кадр всем клиентам, чтобы VPN/NAT-прокси
           не убивали «зависшее» соединение по своему таймауту.
        5. Для opus-целей микс кодируется в opus (один энкодер на цель);
           silence-кадры уходят PCM-нулями всем (payload тишины
           клиентами игнорируется).
        """
        zeros = b"\x00" * PCM_FRAME_BYTES
        with self._lock:
            # Чистим умерших (таймаут-контроль ниже мог их пометить):
            # без удаления список рос бесконечно и держал сокеты/память.
            self._clients = [c for c in self._clients if c.alive]
            clients = list(self._clients)

        # Чистим зависших клиентов (TCP timeout, не «молчание»).
        now = time.time()
        stale = [c for c in clients
                 if now - c.last_seen > MIXER_HEARTBEAT_TIMEOUT_S]
        for c in stale:
            c.alive = False
            try:
                c.conn.close()
            except OSError:
                pass
        if stale:
            clients = [c for c in clients if c.alive]
            self._clients = [c for c in self._clients if c.alive]
            self._broadcast_roster()

        if not clients:
            return

        # ── 1. Вклад каждого клиента на этот тик (в PCM) ─────────────
        contribs: list[tuple[int, bytes]] = []  # (id(c), pcm)
        for c in clients:
            pcm: bytes | None = None
            if c.incoming:
                if c.primed or len(c.incoming) >= (
                        MIXER_PREBUF_FRAMES if not c.had_audio
                        else MIXER_REPREBUF_FRAMES):
                    c.primed = True
                    pcm = c.incoming.pop(0)
                    c.gap_run = 0
                else:
                    # prebuffer ещё набирается — тик пропускаем
                    self._last_voice = now
            elif c.primed:
                # Кадр опоздал: PLC — для opus честный PLC libopus
                # (достраивает речь), для PCM затухающий хвост.
                c.gap_run += 1
                if c.gap_run <= MIXER_PLC_MAX_FRAMES:
                    pcm = self._plc_frame(c)
                else:
                    # длинная пауза — плейаут стоп, ждём re-prebuffer
                    c.primed = False
            if pcm and pcm != zeros:
                contribs.append((id(c), pcm))
                self._last_voice = now
                # Обновили голос — сбрасываем таймер keepalive
                self._last_keepalive = now

        # ── 2/3/4. Микс для каждого: только чужие вклады ─────────────
        active = (now - self._last_voice) < MIXER_ACTIVE_WINDOW_S
        # v3.4.1 FIX: SOFT KEEPALIVE. Если прошло MIXER_KEEPALIVE_EVERY_S
        # с последнего кадра (или keepalive-кадра) — шлём silence ВСЕМ,
        # даже если active=False. VPN-прокси не режут больше соединение.
        ka_needed = (now - getattr(self, "_last_keepalive", now)) >= MIXER_KEEPALIVE_EVERY_S
        if active or contribs or ka_needed:
            if ka_needed:
                self._last_keepalive = now
            for c in clients:
                others = [p for cid, p in contribs if cid != id(c)]
                if others:
                    c.enqueue_outgoing(dsp.mix_frames(others),
                                       is_silence=False)
                else:
                    # Никто не говорит — держим поток (silence-кадр)
                    # (активное окно ИЛИ keepalive по таймеру)
                    c.enqueue_outgoing(zeros, is_silence=True)

        # Flush всем клиентам
        for c in clients:
            c.flush()

    def _plc_frame(self, client: _Client) -> bytes:
        """PLC-кадр для опоздавшего клиента. Opus-цели получают ЧЕСТНЫЙ
        PLC libopus (декодер достраивает правдоподобную речь), PCM-цели —
        затухающий хвост последнего кадра (плавно, без щелчка)."""
        if client.codec == CODEC_OPUS and client._decoder is not None:
            try:
                return client._decoder.plc()
            except Exception:
                pass
        return dsp.plc_tail(client.last_pcm, client.gap_run)


def compute_rms(pcm: bytes) -> float:
    """RMS int16-фрейма (шкала 0-32767) — публичный хелпер (тесты/UI)."""
    return dsp.rms(pcm)
