#!/usr/bin/env python3
"""Тесты v3.6.4 — голос через ZeroTier/VPN (WAN-устойчивость).

Баг от друга: «голос как бурундук или вообще не слышно через ZeroTier».
Три корня:
  1. sendall ПРЯМО из аудио-колбэка PortAudio: через ZeroTier буфер
     отправки TCP наполнялся (ретрансмиты/релей) и sendall замирал на
     сотни мс ВНУТРИ колбэка — захват вставал, у собеседника тишина.
  2. Фиксированные джиттер-буферы (80мс клиент / 60мс микшер) меньше
     масштаба TCP-затыков ZeroTier (100-300мс) — вечные недоборы,
     PLC-робот и «прожёвывание».
  3. Старая версия собеседника шлёт кадры не того размера — микшер молча
     их выкидывал: «вообще не слышно» без единой подсказки почему.

Что покрываем:
  1. Аудио-колбэк НЕ блокируется при замирающем sendall (очередь +
     поток-отправитель).
  2. Очередь отправки: control-lane приоритетна и не режется, аудио
     при переполнении теряет самые старые, drain шлёт control первым.
  3. Ошибка sendall в отправителе = штатный обрыв (_running=False).
  4. Адаптивный пребуфер: рост при «голод+всплеск», потолок, сжатие в
     стабильности, БЕЗ роста на catch-up и на ровном возобновлении.
  5. Микшер: odd-кадры считаются, в pong уходят, соединение живо.
  6. Рукопожатие версий: spk_all «v» (микшер), on_version_mismatch
     (клиент), «odd» в get_quality.
  7. Санити-константы.

Запуск: python tests/test_voice_wan_v364.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import voice.client.engine as _engine_mod
from voice import app_version
from voice.client.engine import VoiceClient
from voice.client.playback import PlaybackBuffer
from voice.config import (
    CLIENT_PREBUF_FRAMES,
    CLIENT_PREBUF_MAX_FRAMES,
    MIXER_JITTER_LIMIT,
    MIXER_PLC_MAX_FRAMES,
    MIXER_PREBUF_FRAMES,
    SEND_QUEUE_DROP_KEEP,
    SEND_QUEUE_LIMIT_FRAMES,
)
from voice.mixer import VoiceMixer
from voice.protocol import (
    FLAG_CONTROL,
    FLAG_SILENCE,
    FRAME_HEADER_LEN,
    MAGIC,
    PCM_FRAME_BYTES,
    make_frame,
)

PASS = FAIL = 0
ROOT = Path(__file__).resolve().parent.parent


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def frame() -> bytes:
    return b"\x01\x02" * (PCM_FRAME_BYTES // 2)


# ══════════════════════════════════════════════════════════════════
# 1. Аудио-колбэк не блокируется при замирающем sendall
# ══════════════════════════════════════════════════════════════════

class SlowSocket:
    """Фейковый сокет: sendall «замерзает» как через ZeroTier-релей."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.chunks: list[bytes] = []
        self.shutdown_called = False

    def sendall(self, data: bytes) -> None:
        time.sleep(self.delay)
        self.chunks.append(data)

    def shutdown(self, *a) -> None:
        self.shutdown_called = True


def make_engine(sock, noise_suppression: bool = False) -> VoiceClient:
    """Движок без реального connect(): живой сокет подставлен руками
    (как это делает connect() после AUTH_OK)."""
    vc = VoiceClient(noise_suppression=noise_suppression)
    vc._running = True
    vc._sock = sock
    vc._sender_alive = True
    vc._sender_thread = threading.Thread(
        target=vc._sender_loop, daemon=True, name="voice-send-test")
    vc._sender_thread.start()
    return vc


def drain_wait(vc: VoiceClient, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with vc._send_q_lock:
            empty = not vc._ctl_q and not vc._audio_q
        if empty and not vc._send_activity.is_set():
            return True
        time.sleep(0.01)
    return False


def wait_sent(sock, total: int, timeout: float = 8.0) -> bool:
    """Ждём, пока отправитель реально дотолкает байты (sendall медленный).
    Пустая очередь ≠ отправлено: снапшот может ещё уезжать."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(len(c) for c in sock.chunks) >= total:
            return True
        time.sleep(0.02)
    return False


def test_nonblocking_callback() -> None:
    print("\n== 1. Аудио-колбэк НЕ блокируется (sendall замирал на 300мс) ==")
    sock = SlowSocket(delay=0.3)
    vc = make_engine(sock)
    try:
        silent = np.zeros((960, 1), dtype=np.int16)
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            vc._audio_in_callback(silent, 960, None, None)
            times.append(time.perf_counter() - t0)
        worst = max(times)
        check("каждый колбэк вернулся < 50мс (раньше — 300мс+)",
              worst < 0.05, f"worst={worst * 1000:.1f}мс")
        check("кадры зачтены отправленными", vc._frames_sent == 5,
              f"sent={vc._frames_sent}")
        check("всё ушло в сокет после отладки очереди",
              wait_sent(sock, 5 * (FRAME_HEADER_LEN + PCM_FRAME_BYTES))
              and sum(len(c) for c in sock.chunks) == 5 * (FRAME_HEADER_LEN
                                                           + PCM_FRAME_BYTES),
              f"bytes={sum(len(c) for c in sock.chunks)}")
        check("в колбэке ушёл SILENCE-флаг (нули с мики)",
              all(f[4] & FLAG_SILENCE for c in sock.chunks for f in [c]))
    finally:
        vc._sender_alive = False
        vc._send_activity.set()
        vc._sender_thread.join(timeout=1.0)


# ══════════════════════════════════════════════════════════════════
# 2. Очередь отправки: приоритет control, переполнение, порядок
# ══════════════════════════════════════════════════════════════════

class RecordSocket:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.shutdown_called = False

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def shutdown(self, *a) -> None:
        self.shutdown_called = True


def test_queue_overflow_and_priority() -> None:
    print("\n== 2. Очередь: аудио теряет хвост, control не режется ==")
    vc = VoiceClient(noise_suppression=False)
    vc._running = True
    for i in range(SEND_QUEUE_LIMIT_FRAMES + 50):
        vc._enqueue_frame(str(i).zfill(4).encode())
    check("аудио-очередь в пределах LIMIT",
          len(vc._audio_q) <= SEND_QUEUE_LIMIT_FRAMES,
          f"len={len(vc._audio_q)}")
    check("дропы посчитаны",
          vc._send_dropped == SEND_QUEUE_LIMIT_FRAMES + 50
          - len(vc._audio_q),
          f"dropped={vc._send_dropped}")
    first_kept = bytes(next(iter(vc._audio_q)))
    check("потеряны САМЫЕ СТАРЫЕ кадры (свежие остались)",
          first_kept == str(SEND_QUEUE_LIMIT_FRAMES + 50
                            - len(vc._audio_q)).zfill(4).encode(),
          f"first={first_kept}")
    dropped_after_audio = vc._send_dropped
    queued_audio = len(vc._audio_q)
    for i in range(20):
        vc._enqueue_frame(b"C", control=True)
    check("control-очередь не режется", len(vc._ctl_q) == 20)
    check("дропы не выросли от control",
          vc._send_dropped == dropped_after_audio)

    sock = RecordSocket()
    vc._sock = sock
    ok = vc._drain_send_queue()
    check("drain успешен", ok is True)
    check("control ушёл ПЕРВЫМ (ping/кодек не ждут за аудио)",
          sock.chunks and sock.chunks[0] == b"C" * 20,
          f"first={sock.chunks[0][:10] if sock.chunks else None}")
    audio_bytes = b"".join(sock.chunks[1:])
    check("вся аудио-очередь доставлена",
          len(audio_bytes) == 4 * queued_audio,
          f"{len(audio_bytes)} == 4*{queued_audio}")


def test_sender_oserror_marks_dead() -> None:
    print("\n== 3. Ошибка sendall в отправителе = штатный обрыв ==")

    class DeadSocket(RecordSocket):
        def sendall(self, data: bytes) -> None:
            raise OSError("zedt link down")

    vc = VoiceClient(noise_suppression=False)
    vc._running = True
    vc._sock = DeadSocket()
    vc._sender_alive = True
    vc._sender_thread = threading.Thread(target=vc._sender_loop, daemon=True)
    vc._sender_thread.start()
    vc._enqueue_frame(b"\x00" * PCM_FRAME_BYTES)
    deadline = time.time() + 3.0
    while time.time() < deadline and vc._running:
        time.sleep(0.01)
    check("_running=False после ошибки сокета", vc._running is False)
    vc._sender_alive = False
    vc._send_activity.set()
    vc._sender_thread.join(timeout=1.0)


# ══════════════════════════════════════════════════════════════════
# 4. Адаптивный джиттер-буфер
# ══════════════════════════════════════════════════════════════════

def test_adaptive_playback() -> None:
    print("\n== 4. Адаптивный пребуфер: рост при голоде+всплеске ==")
    f = frame()
    buf = PlaybackBuffer(PCM_FRAME_BYTES)
    check("старт с базовой цели", buf.target_frames() == CLIENT_PREBUF_FRAMES,
          f"target={buf.target_frames()}")

    # Первый старт: цель = prebuf
    for _ in range(CLIENT_PREBUF_FRAMES):
        buf.push(f)
    check("первый старт по цели", buf.pull() == f)

    # Голод + всплеск → цель растёт
    while buf.pull() is not None:
        pass  # высосали всё — недобор
    check("недобор посчитан", buf.underruns >= 1)
    for _ in range(8):
        buf.push(f)  # всплеск 8 кадров между колбэками
    check("голод+всплеск поднял цель +2",
          buf.target_frames() == CLIENT_PREBUF_FRAMES + 2,
          f"target={buf.target_frames()}")
    check("после всплеска плейаут возобновился", buf.pull() == f)

    # Ровное возобновление после паузы (без всплеска) цель НЕ растит
    while buf.pull() is not None:
        pass
    target_before = buf.target_frames()
    buf.push(f)
    buf.push(f)  # reprebuf-возобновление, по 1 кадру
    check("ровное возобновление не растит цель",
          buf.target_frames() == target_before,
          f"target={buf.target_frames()}")
    # Возобновление требует reprebuf + весь накопленный рост: цель 6 →
    # ждём 2 + (6-4) = 4 кадра — в этом и смысл адаптива после затыка.
    need_now = 2 + (target_before - CLIENT_PREBUF_FRAMES)
    while len(buf.q) < need_now:
        buf.push(f)
    check("возобновление по reprebuf+росту", buf.pull() == f,
          f"need={need_now}")

    # Потолок адаптива
    for _ in range(20):
        while buf.pull() is not None:
            pass
        for _ in range(30):
            buf.push(f)
    check("цель не выше потолка", buf.target_frames()
          == CLIENT_PREBUF_MAX_FRAMES, f"target={buf.target_frames()}")

    # Catch-up переполнения сам по себе цель не растит (свежесть!)
    buf2 = PlaybackBuffer(PCM_FRAME_BYTES)
    for _ in range(CLIENT_PREBUF_FRAMES):
        buf2.push(f)
    buf2.pull()
    for _ in range(100):
        buf2.push(f)  # гигантский всплеск → catch-up, голода НЕ было
    check("catch-up без голода не растит цель",
          buf2.target_frames() == CLIENT_PREBUF_FRAMES,
          f"target={buf2.target_frames()}")
    check("после catch-up плейаут сразу возобновляется (свежесть)",
          buf2.pull() == f)

    # Сжатие в стабильности
    buf3 = PlaybackBuffer(PCM_FRAME_BYTES, shrink_after_s=1.0)
    for _ in range(4):
        buf3.push(f)
    buf3.pull()
    while buf3.pull() is not None:
        pass
    for _ in range(8):
        buf3.push(f)
    grown = buf3.target_frames()
    check("выросли перед проверкой сжатия", grown > CLIENT_PREBUF_FRAMES)
    time.sleep(1.05)
    buf3.push(f)  # тихий кадр: сжатие на 1
    check("после стабильной секунды цель сжалась на 1",
          buf3.target_frames() == grown - 1,
          f"target={buf3.target_frames()}")

    # clear() — с чистого листа
    buf3.clear()
    check("clear() сбросил цель к базе",
          buf3.target_frames() == CLIENT_PREBUF_FRAMES)


# ══════════════════════════════════════════════════════════════════
# 5/6. Микшер: odd-кадры + версия хоста
# ══════════════════════════════════════════════════════════════════

class RawClient:
    """Сырой TCP-клиент микшера с точным чтением кадров."""

    def __init__(self, port: int, name: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(10)
        self.sock.sendall(f"{name}|\n".encode())
        resp = b""
        while len(resp) < 8:
            chunk = self.sock.recv(8 - len(resp))
            if not chunk:
                raise ConnectionError("closed during auth")
            resp += chunk
        assert resp == b"AUTH_OK\n", resp
        self.buf = b""
        self.lock = threading.Lock()

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_control(self, obj: dict) -> None:
        self.sock.sendall(make_frame(
            FLAG_CONTROL, json.dumps(obj).encode("utf-8")))

    def read_frame(self, timeout: float = 2.0):
        with self.lock:
            deadline = time.time() + timeout
            while True:
                while len(self.buf) >= FRAME_HEADER_LEN:
                    if self.buf[:4] != MAGIC:
                        self.buf = self.buf[1:]
                        continue
                    flags = self.buf[4]
                    (length,) = struct.unpack("<H", self.buf[5:7])
                    if len(self.buf) < FRAME_HEADER_LEN + length:
                        break
                    payload = self.buf[FRAME_HEADER_LEN:
                                       FRAME_HEADER_LEN + length]
                    self.buf = self.buf[FRAME_HEADER_LEN + length:]
                    return flags, payload
                if time.time() > deadline:
                    return None
                self.sock.settimeout(max(0.02, deadline - time.time()))
                try:
                    chunk = self.sock.recv(65536)
                except (TimeoutError, OSError):
                    return None
                if not chunk:
                    return None
                self.buf += chunk

    def read_control(self, timeout: float = 2.0, want: str | None = None):
        skip = {"spk_all", "roster", "spk"}
        if want is None:
            skip = set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            fr = self.read_frame(max(0.05, deadline - time.time()))
            if fr is None:
                return None
            flags, payload = fr
            if not flags & FLAG_CONTROL:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if want is None or msg.get("t") == want or msg.get("t") not in skip:
                return msg
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_mixer_odd_frames_and_version() -> None:
    print("\n== 5. Микшер: odd-кадры считаются, pong несёт «odd» ==")
    mixer = VoiceMixer(host="127.0.0.1", port=0, access_key="")
    if not mixer.start():
        check("микшер стартовал", False)
        return
    port = mixer._sock.getsockname()[1]
    try:
        cl = RawClient(port, "СтарыйКлиент")
        # Регистрационные control-кадры: первый — spk_all с версией хоста
        reg = cl.read_control(timeout=3.0)
        check("spk_all содержит версию хоста",
              isinstance(reg, dict) and reg.get("t") == "spk_all"
              and reg.get("v") == app_version(),
              f"v={reg.get('v') if isinstance(reg, dict) else None}")

        # Кадр НЕ того размера (старая версия: 16кГц/10мс = 321 байт с
        # запасом) — должен быть посчитан, а соединение остаться живым
        cl.send_raw(make_frame(0, b"\x00" * 321))
        cl.send_raw(make_frame(0, frame()))  # нормальный кадр после него
        cl.send_control({"t": "ping", "id": 11})
        pong = cl.read_control(timeout=4.0, want="pong")
        check("pong пришёл после odd-кадра (поток не рассинхронизирован)",
              isinstance(pong, dict) and pong.get("t") == "pong")
        check("odd посчитан", isinstance(pong, dict) and pong.get("odd") == 1,
              f"odd={pong.get('odd') if isinstance(pong, dict) else None}")
        check("frames_in считает все голосовые кадры",
              isinstance(pong, dict) and pong.get("in", 0) >= 2,
              f"in={pong.get('in') if isinstance(pong, dict) else None}")
        cl.close()
    finally:
        mixer.stop()

    print("\n== 6. Клиент: рукопожатие версий + odd из pong ==")
    vc = VoiceClient(noise_suppression=False)
    fired: list[tuple[str, str]] = []
    vc.on_version_mismatch = lambda my, host: fired.append((my, host))
    vc._handle_control(json.dumps(
        {"t": "spk_all", "states": {}, "v": "9.9.9"}).encode())
    check("несовпадение версий дошло до колбэка",
          fired == [(app_version(), "9.9.9")], f"fired={fired}")
    vc._handle_control(json.dumps(
        {"t": "spk_all", "states": {}, "v": "9.9.9"}).encode())
    check("та же версия повторно не срабатывает", len(fired) == 1)
    vc._handle_control(json.dumps(
        {"t": "pong", "id": 1, "in": 10, "out": 10, "odd": 7}).encode())
    check("odd из pong учтён", vc._odd_out == 7, f"odd={vc._odd_out}")
    q = vc.get_quality()
    check("get_quality отдаёт odd_out", q.get("odd_out") == 7)

    vc2 = VoiceClient(noise_suppression=False)
    fired2: list[tuple[str, str]] = []
    vc2.on_version_mismatch = lambda my, host: fired2.append((my, host))
    vc2._handle_control(json.dumps(
        {"t": "spk_all", "states": {}}).encode())
    check("старый микшер (без «v») не дёргает предупреждение",
          fired2 == [])


def test_config_sanity() -> None:
    print("\n== 7. Санити WAN-констант ==")
    check("микшер: prebuf < limit",
          MIXER_PREBUF_FRAMES < MIXER_JITTER_LIMIT,
          f"{MIXER_PREBUF_FRAMES} < {MIXER_JITTER_LIMIT}")
    check("микшер: PLC не длиннее limit",
          MIXER_PLC_MAX_FRAMES <= MIXER_JITTER_LIMIT)
    check("клиент: база пребуфера < потолка адаптива",
          CLIENT_PREBUF_FRAMES < CLIENT_PREBUF_MAX_FRAMES)
    check("очередь: KEEP < LIMIT",
          SEND_QUEUE_DROP_KEEP < SEND_QUEUE_LIMIT_FRAMES)
    st = VoiceClient(noise_suppression=False).get_stats()
    check("get_stats содержит новые поля",
          {"send_dropped", "send_queued", "playback_target"} <= set(st))


def test_dialog_version_warn_logic() -> None:
    """Диалог Qt нельзя импортировать в песочнице (нет libEGL), но новую
    логику предупреждения о версиях можно выполнить в изоляции: вытаскиваем
    методы AST'ом и гоняем на стабах (сигнал/лейбл)."""
    print("\n== 8. Диалог: логика предупреждения о версиях (без Qt) ==")
    import ast as _ast
    import textwrap
    src = (ROOT / "qt_app/widgets/voice_channel_dialog.py").read_text(
        encoding="utf-8")
    tree = _ast.parse(src)
    methods = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name in (
                "_emit_version_warn", "_apply_version_warn"):
            methods[node.name] = textwrap.dedent(
                _ast.get_source_segment(src, node))
    check("методы предупреждения найдены в диалоге",
          len(methods) == 2, f"{sorted(methods)}")
    check("сигнал version_warn_sig объявлен",
          "version_warn_sig = Signal(str)" in src)
    check("колбэк движка подключён к маршалингу",
          "self._voice.on_version_mismatch = self._emit_version_warn" in src
          and "self.version_warn_sig.connect(self._apply_version_warn)" in src)

    class StubSignal:
        def __init__(self):
            self.emitted = None

        def emit(self, t):
            self.emitted = t

    class StubLabel:
        def __init__(self):
            self.text = ""
            self.visible = None

        def setText(self, t):
            self.text = t

        def setStyleSheet(self, t):
            pass

        def setWordWrap(self, b):
            pass

        def setVisible(self, b):
            self.visible = b

    class Stub:
        pass

    s = Stub()
    s.version_warn_sig = StubSignal()
    s._version_label = StubLabel()

    class Palette:
        danger = "#e0475d"

    env = {"PALETTE": Palette}
    for code in methods.values():
        exec(compile(code, "<dialog-method>", "exec"), env)
    emit = env["_emit_version_warn"]
    apply_w = env["_apply_version_warn"]

    emit(s, app_version(), "9.9.9")
    check("несовпадение → текст с обеими версиями",
          "9.9.9" in (s.version_warn_sig.emitted or "")
          and app_version() in s.version_warn_sig.emitted,
          f"emitted={s.version_warn_sig.emitted!r}")
    apply_w(s, s.version_warn_sig.emitted)
    check("лейбл показан", s._version_label.visible is True)
    emit(s, app_version(), app_version())
    check("совпадение версий → пустой текст (предупреждение снято)",
          s.version_warn_sig.emitted == "")
    emit(s, "?", "9.9.9")
    check("неизвестная своя версия («?») → без паники",
          s.version_warn_sig.emitted == "")
    apply_w(s, "")
    check("лейбл скрыт", s._version_label.visible is False)


def main() -> None:
    print("═" * 62)
    print("  v3.6.4 — голос через ZeroTier/VPN: WAN-устойчивость")
    print("═" * 62)
    test_nonblocking_callback()
    test_queue_overflow_and_priority()
    test_sender_oserror_marks_dead()
    test_adaptive_playback()
    test_mixer_odd_frames_and_version()
    test_config_sanity()
    test_dialog_version_warn_logic()
    print("\n" + "═" * 62)
    print(f"ИТОГО: {PASS} OK, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
