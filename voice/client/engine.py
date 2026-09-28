"""VoiceClient — клиент голосового канала (микрофон → TCP → наушники).

Публичный API сохранён от v1.9.x (connect/disconnect/set_muted/...),
диалог Qt работает без изменений. Внутри v2.0:

  - ПЛЕЙАУТ: PlaybackBuffer с пре-буфером и catch-up (см. playback.py) —
    исчезли «прожёвывание» и дыры после каждого затишья;
  - ФЕЙДЫ: недобор больше не ступенька — фейд-аут 2мс на хвосте и
    фейд-ин 2мс на возобновлении;
  - МИКРОФОН: HighPass + SoftGate (плавный гейт — слова не «обкусаны»),
    см. capture.py;
  - КОДЕК: Opus VOIP 48кбит/с с FEC (см. voice/codec.py), переговоры
    как раньше — control-кадрами, старый микшер молчит → фолбэк PCM;
  - ЗВУК: 48 кГц fullband, кадры 20мс.

Сеть: TCP (порт HTTP+1), кадры FRVC (voice/protocol.py), NODELAY.
Устройства задаются ИМЕНЕМ (см. voice/devices.list_audio_devices); на
живом соединении их можно поменять через set_devices() +
restart_streams().

Звуком управляет sounddevice (PortAudio); если не установлен — клиент
не стартует, UI показывает ошибку.

v3.6.4 — ГОЛОС ЧЕРЕЗ ZEROTIER/VPN:
  - отправка НЕ в аудио-колбэке: кадры кладутся в ограниченную очередь,
    sendall делает поток voice-send (sendall в колбэке через ZeroTier
    замирал на сотни мс — у собеседника «вообще не слышно»);
  - пребуфер плейаута адаптивный: затык TCP + всплеск кадров поднимает
    цель до 300мс, стабильность возвращает к 80мс (см. playback.py);
  - рукопожатие версий (spk_all «v») и «odd» в pong: разные версии
    приложения больше не молча-«неслышны» — UI предупреждает.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable

from voice import codec
from voice import devices as _devices
from voice import app_version
from voice.client.capture import MicChain
from voice.client.playback import PlaybackBuffer
from voice.config import (
    CLIENT_FADE_SAMPLES,
    CLIENT_OUTPUT_LATENCY,
    CLIENT_SPEAK_HOLD_FRAMES,
    CODEC_ACK_TIMEOUT_S,
    QUALITY_LOSS_BAD_PCT,
    QUALITY_LOSS_GOOD_PCT,
    QUALITY_RTT_BAD_MS,
    QUALITY_RTT_GOOD_MS,
    SEND_QUEUE_DROP_KEEP,
    SEND_QUEUE_LIMIT_FRAMES,
    VOICE_PING_EVERY_S,
)
from voice.devices import HAS_AUDIO, resolve_device
from voice.protocol import (
    CHANNELS as _CHANNELS,
)
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_MUTED,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    PCM_FRAME_BYTES,
    PCM_FRAME_SAMPLES,
    VAD_THRESHOLD,
    make_frame,
)
from voice.protocol import (
    SAMPLE_RATE as _SAMPLE_RATE,
)

CODEC_PCM = "pcm"
CODEC_OPUS = "opus"


class VoiceClient:
    """Один голосовой клиент: микрофон → TCP → наушники."""

    def __init__(self, input_device: str = "", output_device: str = "",
                 output_volume: float = 1.0,
                 noise_suppression: bool = True):
        self._sock: socket.socket | None = None
        self._running = False
        self._muted = False
        self._stream_in = None
        self._stream_out = None
        self._send_lock = threading.Lock()
        # Устройства/громкость (v1.9.6)
        self._input_device = input_device or ""
        self._output_device = output_device or ""
        self._output_volume = max(0.0, min(2.0, float(output_volume)))
        self._noise_suppression = bool(noise_suppression)
        self._audio_lock = threading.Lock()
        # Индикатор уровня микрофона для UI-метра (0..1, обновляется
        # каждым входным колбэком; UI читает по таймеру)
        self._input_level = 0.0
        self.on_input_level: Callable[[float], None] | None = None
        # Штатное закрытие (disconnect()) не должно выглядеть как ошибка:
        # recv-поток увидит EOF и молча выйдет, не дёргая on_error.
        self._expected_close = False
        # Джиттер-буфер плейаута (адаптивный, см. playback.py)
        self._playback = PlaybackBuffer(PCM_FRAME_BYTES)
        # Совместимость со старыми тестами: очередь кадров доступна
        # напрямую (deque внутри буфера).
        self._play_queue = self._playback.q
        # Цепочка обработки микрофона (high-pass + soft gate); пересоздаём
        # при выключении шумодава — состояние фильтров сбрасывается.
        self._mic = MicChain() if noise_suppression else None
        # Кодек: "pcm" до согласования; "opus" — после ACK микшера.
        self._codec = CODEC_PCM
        self._codec_ack: threading.Event = threading.Event()
        self._opus_encoder = None
        self._opus_decoder = None
        # Статистика для UI
        self._frames_sent = 0
        self._frames_recv = 0
        self._silence_frames = 0
        # Воспроизведение после тишины начинается с короткого фейд-ина
        self._out_gap = True
        # Остаток кадра, не влезшего в предыдущий колбэк вывода
        # (sounddevice просит произвольное число фреймов)
        self._tail = b""
        # v3.5.7 анти-щелчки: последний реально сыгравший сэмпл (int16).
        # Нужен, чтобы обрыв в тишину (недобор буфера) начинался С НЕГО —
        # короткий фейд-аут вместо жёсткого прыжка в ноль между колбэками
        # (главный источник «тцк» в v2.0).
        self._last_out_amp = 0.0
        # v3.5.7: громкость, применённая к ПРЕДЫДУЩЕМУ колбэку. Между
        # колбэками громкость едет линейным рамп-гейном — движение
        # слайдера больше не щёлкает (ступенька амплитуды устранена).
        self._applied_vol = self._output_volume

        self.on_participants_change: Callable[[list[str]], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        # v3.5.7: КАЧЕСТВО СВЯЗИ (пинг/потери) для вкладки «Голос».
        # Клиент раз в VOICE_PING_EVERY_S шлёт control {"t":"ping"};
        # микшер отвечает pong со СЧЁТЧИКАМИ кадров (сколько принял от
        # нас, сколько нам отправил). Из дельт счётчиков считаются
        # потери вверх (наш голос) и вниз (микс) — честные проценты,
        # а не догадки. RTT — время ping→pong.
        self._ping_id = 0
        self._ping_lock = threading.Lock()
        self._ping_outstanding: dict[int, float] = {}  # id -> perf_counter
        self._ping_stop = threading.Event()
        self._ping_thread: threading.Thread | None = None
        self._rtt_ms = 0.0            # EMA времени туда-обратно
        self._uplink_pct = 0.0        # EMA потерь НАШЕГО голоса до микшера
        self._downlink_pct = 0.0      # EMA потерь микса до нас
        self._last_mix_in: int | None = None    # снапшоты для дельт
        self._last_mix_out: int | None = None
        self._last_my_sent: int | None = None
        self._last_my_recv: int | None = None
        # Индикатор «говорит»: сетевые переходы ({имя: on}) и свой VAD.
        # Оба колбэка зовутся из фоновых потоков — UI обязан маршалингить
        # в главный поток сам (например, через Qt Signal).
        self.on_speaking_change: Callable[[dict[str, bool]], None] | None = None
        self.on_local_speaking: Callable[[bool], None] | None = None
        self._local_speaking = False
        self._silence_run = 0
        # ── v3.6.4 ФИКС «ЧЕРЕЗ ZEROTIER НЕ СЛЫШНО» ────────────────────
        # Раньше _audio_in_callback звал socket.sendall ПРЯМО в потоке
        # PortAudio. В LAN буфер отправки TCP не наполнялся никогда, но
        # через ZeroTier/VPN (потеря пакетов = ретрансмиты, релейный
        # путь) sendall ЗАМИРАЛ на сотни мс внутри колбэка: захват
        # вставал, у собеседника тишина/дыры, ping-поток висел на
        # _send_lock. Теперь колбэк только КЛАДЁТ кадр в ограниченную
        # очередь (никогда не блокируется), фактическую отправку делает
        # поток voice-send. Control-кадры (ping/кодек) — приоритетная
        # lane, аудио при переполнении теряет самые старые кадры.
        self._ctl_q: deque[bytes] = deque()
        self._audio_q: deque[bytes] = deque()
        self._send_q_lock = threading.Lock()
        self._send_activity = threading.Event()
        self._send_dropped = 0
        self._sender_alive = False
        self._sender_thread: threading.Thread | None = None
        # v3.6.4: рукопожатие версий (spk_all «v») и счётчик СВОИХ
        # кадров, отвергнутых микшером по размеру (pong «odd») — оба
        # сигналят «у одной из сторон старая версия, обнови обе».
        self._host_version = ""
        self._odd_out = 0
        self.on_version_mismatch: Callable[[str, str], None] | None = None

    # ── Публичный API ───────────────────────────────────────────────

    def connect(self, host: str, port: int, name: str,
                access_key: str = "") -> bool:
        """Подключается к голосовому серверу. Возвращает True при успехе."""
        if not HAS_AUDIO:
            if self.on_error:
                self.on_error("sounddevice не установлен. "
                              "Установи: pip install sounddevice")
            return False

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((host, port))
            auth = f"{name}|{access_key}\n".encode()
            self._sock.sendall(auth)
            resp = self._recv_exact(self._sock, len(b"AUTH_OK\n"))
            if resp != b"AUTH_OK\n":
                if self.on_error:
                    msg = resp.decode("utf-8", errors="replace").strip() \
                        or "auth failed"
                    self.on_error(f"Голосовой сервер: {msg}")
                self._sock.close()
                self._sock = None
                return False
            self._sock.settimeout(None)
            # NODELAY: 20мс-кадры не должны копиться в ядре (Nagle) —
            # всплески на входе микшера = джиттер и дропы у слушателей
            try:
                self._sock.setsockopt(socket.IPPROTO_TCP,
                                      socket.TCP_NODELAY, 1)
            except OSError:
                pass
        except OSError as e:
            if self.on_error:
                self.on_error(
                    f"Не удалось подключиться к голосовому серверу: {e}")
            self._sock = None
            return False

        self._running = True
        self._local_speaking = False
        self._silence_run = 0
        self._expected_close = False
        # v3.6.4: очереди отправки и адаптив плейаута — с чистого листа
        # (после прошлого соединения могли остаться кадры и счётчики).
        with self._send_q_lock:
            self._ctl_q.clear()
            self._audio_q.clear()
        self._send_dropped = 0
        self._send_activity.clear()
        self._host_version = ""
        self._odd_out = 0
        self._playback.clear()
        self._tail = b""
        self._out_gap = True
        self._last_out_amp = 0.0
        # v3.5.7: качество связи заново на каждом соединении.
        self._reset_quality()
        self._ping_stop.clear()
        self._ping_thread = threading.Thread(
            target=self._ping_loop, daemon=True, name="voice-ping")
        self._ping_thread.start()

        # v3.6.4: поток-отправитель стартует ДО переговоров кодека и
        # аудио-потоков — они уже шлют через очередь.
        self._sender_alive = True
        self._sender_thread = threading.Thread(
            target=self._sender_loop, daemon=True, name="voice-send")
        self._sender_thread.start()

        t_recv = threading.Thread(target=self._recv_loop, daemon=True)
        t_recv.start()
        # Переговоры кодека ДО старта аудио-потоков: первые секунды
        # канала уже едут в согласованном формате.
        self._negotiate_codec()
        self._start_audio_streams()
        return True

    def disconnect(self) -> None:
        """Корректно отключается и освобождает аудио-устройства."""
        self._running = False
        self._expected_close = True
        self._local_speaking = False
        self._silence_run = 0
        self._ping_stop.set()  # v3.5.7: пинг-поток гасим первым делом
        self._codec_ack.set()  # если ждём ACK — разбудить и откатить на pcm
        self._stop_audio_streams_locked()
        # v3.6.4: гасим поток-отправитель (он мог спать на event).
        self._sender_alive = False
        self._send_activity.set()
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=1.0)
            self._sender_thread = None
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

    def set_muted(self, muted: bool) -> None:
        """Включает/выключает микрофон."""
        self._muted = muted

    def is_muted(self) -> bool:
        return self._muted

    def get_codec(self) -> str:
        """Согласованный кодек («pcm» | «opus») — для статистики UI."""
        return self._codec

    def get_stats(self) -> dict:
        """Статистика для отладки/UI."""
        return {
            "sent": self._frames_sent,
            "recv": self._frames_recv,
            "silence": self._silence_frames,
            "buffered": self._playback.buffered_frames(),
            "dropped": self._playback.dropped,
            "underruns": self._playback.underruns,
            "codec": self._codec,
            # v3.6.4: очередь отправителя + адаптив плейаута
            "send_dropped": self._send_dropped,
            "send_queued": len(self._audio_q),
            "playback_target": self._playback.target_frames(),
        }

    # ── Качество связи (v3.5.7) ──────────────────────────────────

    def _reset_quality(self) -> None:
        """Сброс метрик качества (новое соединение)."""
        with self._ping_lock:
            self._ping_outstanding.clear()
            self._ping_id = 0
        self._rtt_ms = 0.0
        self._uplink_pct = 0.0
        self._downlink_pct = 0.0
        self._last_mix_in = None
        self._last_mix_out = None
        self._last_my_sent = None
        self._last_my_recv = None

    def _ping_loop(self) -> None:
        """Раз в VOICE_PING_EVERY_S шлёт control-кадр {"t":"ping"}.
        Ответ pong приходит в recv-поток (→ _on_pong). Поток гасится
        через _ping_stop при disconnect() и умирает сам при разрыве
        (sendall бросает OSError → выходим)."""
        while not self._ping_stop.wait(VOICE_PING_EVERY_S):
            if not self._running or self._sock is None:
                continue
            with self._ping_lock:
                self._ping_id += 1
                pid = self._ping_id
                self._ping_outstanding[pid] = time.perf_counter()
            try:
                self._send_frame(make_frame(
                    FLAG_CONTROL,
                    json.dumps({"t": "ping", "id": pid}).encode("utf-8")))
            except OSError:
                with self._ping_lock:
                    self._ping_outstanding.pop(pid, None)
                return  # сокет умер — recv-поток уже разруливает обрыв

    def _on_pong(self, msg: dict) -> None:
        """Ответ микшера: RTT (ping→pong) + честные потери из дельт
        счётчиков кадров. Микшер в pong кладёт in = сколько голосовых
        кадров принял ОТ нас, out = сколько отправил НАМ; сравниваем с
        нашими sent/recv за тот же интервал."""
        try:
            pid = int(msg.get("id"))
            mix_in = max(0, int(msg.get("in", 0)))
            mix_out = max(0, int(msg.get("out", 0)))
        except (TypeError, ValueError):
            return
        with self._ping_lock:
            sent_at = self._ping_outstanding.pop(pid, None)
        if sent_at is None:
            return  # чужой/устаревший pong
        rtt = max(0.0, (time.perf_counter() - sent_at) * 1000.0)
        # EMA: сглаживаем одиночные всплески (UI рисует через секунду)
        self._rtt_ms = rtt if self._rtt_ms <= 0.0 \
            else 0.7 * self._rtt_ms + 0.3 * rtt

        my_sent = self._frames_sent
        my_recv = self._frames_recv
        if self._last_mix_in is not None:
            d_in = mix_in - self._last_mix_in
            d_out = mix_out - self._last_mix_out
            d_sent = my_sent - self._last_my_sent
            d_recv = my_recv - self._last_my_recv
            # Потери ВВЕРХ: послали d_sent — микшер принял d_in.
            # (дельты не могут быть отрицательными при живом счётчике,
            # но reconnect/пересоздание client'а страхуем max(...,0))
            if d_sent > 0:
                up = max(0.0, min(100.0, (d_sent - max(d_in, 0))
                                  * 100.0 / d_sent))
                self._uplink_pct = up if self._uplink_pct <= 0.0 \
                    else 0.7 * self._uplink_pct + 0.3 * up
            if d_out > 0:
                down = max(0.0, min(100.0, (d_out - max(d_recv, 0))
                                    * 100.0 / d_out))
                self._downlink_pct = down if self._downlink_pct <= 0.0 \
                    else 0.7 * self._downlink_pct + 0.3 * down
        self._last_mix_in = mix_in
        self._last_mix_out = mix_out
        self._last_my_sent = my_sent
        self._last_my_recv = my_recv

    def get_quality(self) -> dict:
        """Метрики для вкладки «Голос»: пинг, потери ↑↓, вердикт.

        Вердикт (quality): good / ok / bad — по порогам из config.
        Пока первый pong не пришёл — rtt/pct = 0 и verdict «ok»
        (не пугаем пользователя в первые 2 секунды подключения)."""
        connected = self.is_connected()
        measured = self._last_mix_in is not None
        if not connected:
            verdict = ""
        elif not measured:
            verdict = "ok"
        elif (self._rtt_ms <= QUALITY_RTT_GOOD_MS
                and self._uplink_pct <= QUALITY_LOSS_GOOD_PCT
                and self._downlink_pct <= QUALITY_LOSS_GOOD_PCT):
            verdict = "good"
        elif (self._rtt_ms > QUALITY_RTT_BAD_MS
                or self._uplink_pct > QUALITY_LOSS_BAD_PCT
                or self._downlink_pct > QUALITY_LOSS_BAD_PCT):
            verdict = "bad"
        else:
            verdict = "ok"
        return {
            "connected": connected,
            "rtt_ms": round(self._rtt_ms, 1),
            "uplink_pct": round(self._uplink_pct, 1),
            "downlink_pct": round(self._downlink_pct, 1),
            "verdict": verdict,
            "codec": self._codec,
            # v3.6.4: сколько СВОИХ кадров микшер отверг по размеру
            # (сигнал «у тебя/у хоста старая версия — обнови обе»).
            "odd_out": int(self._odd_out),
        }

    # ── Переговоры кодека ─────────────────────────────────────────

    def _negotiate_codec(self) -> None:
        """Просит микшер включить opus, если обе стороны умеют.

        Проводной протокол не меняется: запрос и ACK — control-кадры
        внутри обычного FRVC-конверта. Старый микшер просто молчит
        (не знает такого типа кадра) — по таймауту откатываемся на PCM.
        """
        if not codec.available() or self._sock is None or not self._running:
            return
        self._codec_ack.clear()
        try:
            self._send_frame(
                make_frame(FLAG_CONTROL,
                           json.dumps({"t": "codec",
                                       "codec": CODEC_OPUS})
                           .encode("utf-8")))
        except OSError:
            return
        # ACK приходит через recv-поток (_handle_control ставит событие).
        if not self._codec_ack.wait(timeout=CODEC_ACK_TIMEOUT_S):
            return  # старый микшер — живём на PCM
        if self._codec == CODEC_OPUS:
            try:
                self._opus_encoder = codec.Encoder()
                self._opus_decoder = codec.Decoder()
            except Exception:
                self._codec = CODEC_PCM
                self._opus_encoder = self._opus_decoder = None

    # ── Очередь отправки (v3.6.4) ─────────────────────────────────

    def _enqueue_frame(self, frame: bytes, control: bool = False) -> None:
        """Кладёт кадр в очередь отправителя — НИКОГДА не блокируется.

        Control-кадры (ping/кодек/hb) идут в приоритетную lane: они
        мелкие, редкие и не должны ждать за 3-секундным хвостом аудио.
        Аудио при переполнении очереди теряет САМЫЕ СТАРЫЕ кадры
        (свежесть речи важнее доставки хвоста) — счётчик _send_dropped
        честно попадает в потери вверх (кадры зачтены отправленными,
        но микшер их не получит).
        """
        with self._send_q_lock:
            if control:
                self._ctl_q.append(frame)
            else:
                self._audio_q.append(frame)
                if len(self._audio_q) > SEND_QUEUE_LIMIT_FRAMES:
                    drop = len(self._audio_q) - SEND_QUEUE_DROP_KEEP
                    for _ in range(drop):
                        self._audio_q.popleft()
                    self._send_dropped += drop
        self._send_activity.set()

    def _sender_loop(self) -> None:
        """Поток voice-send: единственный, кто зовёт socket.sendall.

        Ждёт event (низкий CPU), затем выгребает СНАЧАЛА всю control-lane,
        потом аудио. Ошибка сокета — тот же сценарий «обрыв», что был в
        колбэке: _running=False + shutdown — recv-поток увидит EOF и
        запустит привычный авто-реконнект.
        """
        while self._sender_alive:
            if not self._send_activity.wait(0.2):
                continue
            self._send_activity.clear()
            if not self._drain_send_queue():
                return  # сокет мёртв

    def _drain_send_queue(self) -> bool:
        """Отправляет всё накопленное. False — сокет умер (обрыв уже
        помечен: _running=False + shutdown → авто-реконнект)."""
        sock = self._sock
        if sock is None:
            return False
        while True:
            with self._send_q_lock:
                ctl = list(self._ctl_q)
                self._ctl_q.clear()
                audio = list(self._audio_q)
                self._audio_q.clear()
            if not ctl and not audio:
                return True
            try:
                if ctl:
                    sock.sendall(b"".join(ctl))
                if audio:
                    sock.sendall(b"".join(audio))
            except OSError:
                self._running = False
                s = self._sock
                if s is not None:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return False
            # За время отправки могли приехать новые кадры — цикл
            # проверит очередь ещё раз и выйдет на пустой.

    # ── Устройства/громкость (v1.9.6) ────────────────────────────

    def set_devices(self, input_device: str, output_device: str) -> None:
        """Меняет имена устройств (применяются restart_streams/connect)."""
        self._input_device = input_device or ""
        self._output_device = output_device or ""

    def get_devices(self) -> tuple[str, str]:
        return self._input_device, self._output_device

    def set_output_volume(self, volume: float) -> None:
        """Громкость вывода 0.0–2.0, применяется сразу (атомарная запись
        float в CPython — без лока, читается в аудио-колбэке)."""
        self._output_volume = max(0.0, min(2.0, float(volume)))

    def get_output_volume(self) -> float:
        return self._output_volume

    def set_noise_suppression(self, enabled: bool) -> None:
        """Вкл/выкл шумоподавление (high-pass + soft gate)."""
        self._noise_suppression = bool(enabled)
        self._mic = MicChain() if enabled else None

    def is_noise_suppression(self) -> bool:
        return self._noise_suppression

    def restart_streams(self) -> bool:
        """Перезапускает аудио-потоки на живом соединении (смена
        устройств). Возвращает True если потоки подняты. Сетевая часть
        не трогается: собеседники услышат короткую паузу вместо полного
        дисконнекта. До запуска потоков (не подключён) — просто True:
        устройства применятся при connect()."""
        with self._audio_lock:
            self._stop_audio_streams_locked()
            if not self._running:
                return True
            return self._start_audio_streams_locked()

    def get_input_level(self) -> float:
        """Уровень микрофона 0..1 (для UI-метра)."""
        return self._input_level

    # ── Аудио-потоки ────────────────────────────────────────────────

    def _stop_audio_streams_locked(self) -> None:
        if self._stream_in is not None:
            try:
                self._stream_in.stop()
                self._stream_in.close()
            except OSError:
                pass
            self._stream_in = None
        if self._stream_out is not None:
            try:
                self._stream_out.stop()
                self._stream_out.close()
            except OSError:
                pass
            self._stream_out = None

    def _start_audio_streams(self) -> bool:
        """Запускает захват с микрофона и воспроизведение в наушники."""
        with self._audio_lock:
            return self._start_audio_streams_locked()

    def _start_audio_streams_locked(self) -> bool:
        """Открывает захват и воспроизведение с выбранными устройствами.
        sounddevice берётся через voice.devices — сейм для тестов
        (фейк-подмена voice.devices.sd видна движку)."""
        sd = _devices.sd
        if sd is None:
            return False
        in_dev = resolve_device(self._input_device, "input")
        try:
            kwargs: dict = {}
            if in_dev is not None:
                kwargs["device"] = in_dev
            self._stream_in = sd.InputStream(
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype="int16",
                blocksize=PCM_FRAME_SAMPLES,  # 20мс
                callback=self._audio_in_callback,
                **kwargs,
            )
            self._stream_in.start()
        except OSError as e:
            if self.on_error:
                dev_hint = self._input_device or "по умолчанию"
                self.on_error(
                    f"Не удалось открыть микрофон «{dev_hint}»: {e}\n"
                    "Проверь выбор устройства в настройках голоса."
                )
            self._running = False
            return False

        out_dev = resolve_device(self._output_device, "output")
        try:
            kwargs = {}
            if out_dev is not None:
                kwargs["device"] = out_dev
            self._stream_out = sd.OutputStream(
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype="int16",
                blocksize=PCM_FRAME_SAMPLES,  # 20мс
                # v3.5.7: «high» = больший аппаратный буфер. Вывод
                # перестаёт кликать при всплесках нагрузки (музыка,
                # браузер, антивирус) — цена +40-80мс к плейауту,
                # которые и так съедает джиттер-буфер.
                latency=CLIENT_OUTPUT_LATENCY,
                callback=self._audio_out_callback,
                **kwargs,
            )
            self._stream_out.start()
        except OSError as e:
            # Микрофон уже открыт — закрываем, чтобы не висел занятым
            self._stop_audio_streams_locked()
            if self.on_error:
                dev_hint = self._output_device or "по умолчанию"
                self.on_error(
                    f"Не удалось открыть динамик «{dev_hint}»: {e}\n"
                    "Проверь выбор устройства в настройках голоса."
                )
            self._running = False
            return False
        return True

    # ── Колбэк микрофона ────────────────────────────────────────────

    def _audio_in_callback(self, indata, frames, time_info, status) -> None:
        """Вызывается sounddevice'ом с новым блоком PCM с микрофона.
        Шумоподавление (high-pass + плавный гейт), VAD, свой индикатор
        «говорит», кодирование opus при согласовании — см. capture.py.
        """
        if not self._running or self._sock is None:
            return

        raw_pcm = indata.reshape(-1).tobytes()

        # ── Шумоподавление (high-pass + плавный гейт) ─────────────
        if self._noise_suppression and self._mic is not None:
            pcm, gate_gain = self._mic.process(raw_pcm)
        else:
            pcm, gate_gain = raw_pcm, 1.0

        level_rms = MicChain.rms_of(pcm)
        # VAD: с открытым гейтом тихий, но реальный звук идёт в сеть;
        # silence-флаг только когда гейт закрылся полностью.
        is_silence = level_rms < VAD_THRESHOLD

        # Уровень микрофона для UI-метра (гладкое затухание, чтобы бар
        # не зависал на пике между тиками UI)
        level = min(1.0, level_rms / (VAD_THRESHOLD * 6.0))
        self._input_level = max(level, self._input_level * 0.82)

        # ── Свой индикатор «говорит» (гистерезис ~260мс) ──────────
        self._update_local_speaking(
            not self._muted and not is_silence and gate_gain > 0.0)

        if self._muted:
            # Mute: отправляем silence frame с флагом muted
            flags = FLAG_MUTED | FLAG_SILENCE
            pcm_to_send = b"\x00" * PCM_FRAME_BYTES
        elif is_silence:
            flags = FLAG_SILENCE
            pcm_to_send = b"\x00" * PCM_FRAME_BYTES
        else:
            flags = 0x00
            pcm_to_send = pcm

        try:
            if flags == 0x00 and self._codec == CODEC_OPUS \
                    and self._opus_encoder is not None:
                payload = self._opus_encoder.encode(pcm_to_send)
            else:
                payload = pcm_to_send
            # v3.6.4: ТОЛЬКО ставим в очередь — sendall в потоке PortAudio
            # через ZeroTier/VPN замирал на сотни мс и вставлял захват.
            self._enqueue_frame(make_frame(flags, payload))
            self._frames_sent += 1
            if is_silence:
                self._silence_frames += 1
        except Exception:
            # encode/очередь не должны падать в принципе; колбэк обязан
            # вернуть управление sounddevice любой ценой.
            pass

    def _update_local_speaking(self, speaking_now: bool) -> None:
        """Гистерезис своего кольца: зажигается мгновенно, гаснет после
        CLIENT_SPEAK_HOLD_FRAMES кадров непрерывной тишины."""
        if speaking_now:
            self._silence_run = 0
        else:
            self._silence_run += 1
        if speaking_now and not self._local_speaking:
            self._local_speaking = True
            if self.on_local_speaking is not None:
                self.on_local_speaking(True)
        elif (
            not speaking_now
            and self._local_speaking
            and self._silence_run >= CLIENT_SPEAK_HOLD_FRAMES
        ):
            self._local_speaking = False
            if self.on_local_speaking is not None:
                self.on_local_speaking(False)

    # ── Колбэк воспроизведения ──────────────────────────────────────

    def _audio_out_callback(self, outdata, frames, time_info, status) -> None:
        """Вызывается sounddevice'ом для заполнения буфера
        воспроизведения. Кадр берётся из адаптивного джиттер-буфера.

        v3.5.7 АНТИ-ЩЕЛЧКИ: все стыки плавные —
          · тишина→звук: фейд-ин 2мс (было в v2.0);
          · звук→тишина (недобор): фейд-АУТ от последнего сыгравшего
            сэмпла — раньше был жёсткий прыжок амплитуды в ноль
            МЕЖДУ колбэками («тцк»);
          · рывок после catch-up (буфер выбросил кадры): фейд-ин на
            стыке — волна не прыгает;
          · громкость: линейный рамп от предыдущего значения —
            слайдер громкости больше не щёлкает.
        """
        import numpy as np
        needed = frames * _CHANNELS  # int16 = 2 байта на сэмпл
        fade_n = max(1, min(CLIENT_FADE_SAMPLES, frames))

        pcm = self._playback.pull()
        discontinuous = self._playback.discontinuous
        self._playback.discontinuous = False

        if pcm is None:
            # Недобор: тишина, НО начинается она с фейд-аута от
            # последнего сыгравшего сэмпла — без прыжка амплитуды.
            out = np.zeros((frames, _CHANNELS), dtype=np.int16)
            if abs(self._last_out_amp) >= 1.0:
                ramp = np.linspace(self._last_out_amp, 0.0,
                                   fade_n, dtype=np.float32)
                out[:fade_n, 0] = ramp.astype(np.int16)
            self._last_out_amp = 0.0
            self._out_gap = True
            outdata[:] = out
            return

        # Кадр 1920 байт (960 сэмплов) может не совпасть с requested
        # frames — работаем через сэмпл-аккумулятор: докладываем хвост
        # предыдущего кадра.
        data = self._tail + pcm if self._tail else pcm
        self._tail = b""
        avail = len(data) // 2
        if avail < needed:
            # Кадр меньше запрошенного — добираем из буфера или тишиной.
            while avail < needed:
                nxt = self._playback.pull()
                if nxt is None:
                    break
                data += nxt
                avail = len(data) // 2
        if avail > needed:
            cut = needed * 2
            self._tail = data[cut:]
            data = data[:cut]
        padded = avail < needed  # недобор внутри колбэка — хвост тишиной
        if padded:
            data += b"\x00" * ((needed - avail) * 2)

        # .copy(): frombuffer даёт read-only массив, а мы правим фейды
        arr = np.frombuffer(data, dtype=np.int16).reshape(
            -1, _CHANNELS).copy()

        if self._out_gap or discontinuous:
            # 2мс фейд-ин: стык тишина→речь ИЛИ рывок после catch-up —
            # волна поднимается с нуля/предыдущей точки плавно.
            n = min(fade_n, arr.shape[0])
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            arr[:n, 0] = (arr[:n, 0].astype(np.float32)
                          * ramp).astype(np.int16)
            self._out_gap = False
        if padded:
            # Фейд-АУТ хвоста реального звука перед тишиной: рвём волну
            # не жёстким прыжком, а 2мс спуском до нуля.
            b = avail  # граница реального звука (в сэмплах)
            a = max(0, b - fade_n)
            ramp = np.linspace(1.0, 0.0, b - a, dtype=np.float32) \
                if b > a else np.zeros(0, dtype=np.float32)
            if ramp.size:
                arr[a:b, 0] = (arr[a:b, 0].astype(np.float32)
                               * ramp).astype(np.int16)

        # Громкость вывода: 1.0 = без изменений. v3.5.7: рамп-гейн от
        # предыдущего применённого значения к целевому — движение
        # слайдера не даёт ступеньки амплитуды (щёлканья). Клиппим.
        vol = self._output_volume
        prev = self._applied_vol
        if vol != 1.0 or prev != vol:
            gains = np.linspace(prev, vol, arr.shape[0], dtype=np.float32)
            scaled = arr[:, 0].astype(np.float32) * gains
            np.clip(scaled, -32768, 32767, out=scaled)
            arr[:, 0] = scaled.astype(np.int16)
        self._applied_vol = vol

        outdata[:] = arr
        self._last_out_amp = float(arr[-1, 0])

    # ── Приём кадров от сервера ─────────────────────────────────────

    def _recv_loop(self) -> None:
        """Читает фреймы от сервера, складывает PCM в джиттер-буфер."""
        sock = self._sock
        while self._running and sock is not None:
            try:
                header = self._recv_exact(sock, FRAME_HEADER_LEN)
                if header is None:
                    break
                if header[:4] != MAGIC:
                    continue
                flags = header[4]
                length = struct.unpack("<H", header[5:7])[0]
                if length > MAX_PAYLOAD_BYTES:
                    continue
                payload = self._recv_exact(sock, length) if length else b""
                if length and payload is None:
                    break

                self._on_mixer_frame(flags, payload)
            except OSError:
                break

        self._running = False
        if self.on_error and not self._expected_close:
            self.on_error("Голосовое соединение разорвано")

    def _on_mixer_frame(self, flags: int, payload: bytes) -> None:
        """Один кадр от микшера → в джиттер-буфер плейаута.

        Вынесен из _recv_loop для тестируемости без сокета. Control-кадры
        уходят в _handle_control, silence — чистой тишиной, голос
        декодируется из opus если согласован.
        """
        # Control-фрейм: кто сейчас говорит / переговоры кодека (JSON)
        if flags & FLAG_CONTROL:
            self._handle_control(payload)
            return

        self._frames_recv += 1

        # Silence-кадр — ЧИСТАЯ тишина (payload игнорируем).
        if flags & FLAG_SILENCE:
            pcm = b"\x00" * PCM_FRAME_BYTES
        elif self._codec == CODEC_OPUS and self._opus_decoder is not None:
            try:
                pcm = self._opus_decoder.decode(payload)
            except Exception:
                # v3.5.7: битый кадр больше НЕ выбрасывается дырой в
                # поток (рывок волны = щелчок), а закрывается честным
                # PLC libopus — поток остаётся непрерывным.
                try:
                    pcm = self._opus_decoder.plc()
                except Exception:
                    pcm = b"\x00" * PCM_FRAME_BYTES
        else:
            pcm = payload

        self._playback.push(pcm)

    def _send_frame(self, frame: bytes) -> None:
        """Отправка control-кадра (переговоры кодека, ping) — через
        приоритетную lane очереди отправителя (v3.6.4)."""
        self._enqueue_frame(frame, control=True)

    def _handle_control(self, payload: bytes) -> None:
        """Разбирает служебный JSON о говорящих и переговорах кодека.
        {"t":"spk","name":N,"on":bool}      — переход одного участника
        {"t":"spk_all","states":{N:bool}}   — снапшот (при подключении)
        {"t":"codec","codec":"opus|pcm"}    — ответ на наш запрос кодека
        Плохой JSON молча игнорируем — это подсказка UI, не данные."""
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("t")
        if t == "codec":
            answer = str(msg.get("codec") or CODEC_PCM)
            self._codec = answer if answer in (CODEC_PCM, CODEC_OPUS) \
                else CODEC_PCM
            self._codec_ack.set()
        elif t == "pong":
            # v3.5.7: качество связи — RTT и потери из счётчиков микшера.
            # v3.6.4: «odd» — сколько СВОИХ кадров микшер отверг по
            # размеру (старая версия шлёт кадры не 20мс/48кГц).
            try:
                self._odd_out = max(0, int(msg.get("odd", 0)))
            except (TypeError, ValueError):
                pass
            self._on_pong(msg)
        elif t == "spk":
            name = msg.get("name")
            if isinstance(name, str) and self.on_speaking_change is not None:
                self.on_speaking_change({name: bool(msg.get("on"))})
        elif t == "spk_all":
            states = msg.get("states")
            if isinstance(states, dict) and self.on_speaking_change \
                    is not None:
                clean = {
                    n: bool(v)
                    for n, v in states.items()
                    if isinstance(n, str)
                }
                self.on_speaking_change(clean)
            # v3.6.4: рукопожатие версий. Микшер (хост) присылает свою
            # версию в каждом spk_all; при первом появлении/смене —
            # уведомляем UI. Разные версии приложения = риск «бурундука»
            # или тишины (аудио-формат менялся между релизами).
            host_v = str(msg.get("v") or "")
            if host_v and host_v != self._host_version:
                self._host_version = host_v
                if self.on_version_mismatch is not None:
                    my_v = app_version()
                    try:
                        self.on_version_mismatch(my_v, host_v)
                    except Exception:
                        pass  # UI-колбэк не имеет права ронять recv-поток

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)
