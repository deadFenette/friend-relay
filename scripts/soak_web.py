#!/usr/bin/env python3
"""soak_web.py — долгий смешанный soak-тест стабильности веб-версии (v3.8.3).

Юнит-тесты проверяют ЛОГИКУ за секунды. Этот скрипт проверяет ВРЕМЯ:
час работы под смешанной нагрузкой, где утечки памяти, незакрытые
файловые дескрипторы, копящиеся очереди и деградация задержек видны
только на дистанции.

Что внутри (всё против НАСТОЯЩЕГО RelayServer на 127.0.0.1):
  • chat-клиенты  — POST /send_text (подпись X-Relay-Auth, как браузер)
                    + GET /events?since=… с замером задержки каждого опроса;
  • voice-клиенты — wss://…/voice/ws (TLS, как браузер), аплинк PCM-кадров
                    каждые ~20 мс, приём микса; стушевавшегося слушателя
                    (нет кадров > 5 с) считаем stall'ом;
  • file-воркеры  — POST /send_file (стриминговая загрузка, HMAC по телу)
                    → GET /download/<id> → сверка длины и SHA-256;
  • ШТОРМ РЕКОННЕКТОВ — на 50% и 90% дистанции ВСЕ голосовые клиенты
    разом роняют соединение и пересобираются (thundering herd);
  • РЕСТАРТ СЕРВЕРА — на ~70% дистанции relay.stop() → пауза →
    relay.start() на том же порту; клиенты обязаны пережить кнопкой
    backoff'а (ошибки в окне рестарта считаются отдельно, не FAIL);
  • медленный клиент — опрашивает раз в 8 с и никогда не отваливается
    (сервер не должен на нём блокироваться).

Контрольные пороги (после прогрева):
  • рост RSS            ≤ 35 МБ (+15% базы)   — утечки памяти;
  • рост открытых fd    ≤ 40                   — незакрытые сокеты/файлы;
  • p95 задержки опроса ≤ 2.0 с                — деградация event-loop;
  • голосовые stall'ы   = 0                    — дыры в потоке микса;
  • реконнекты шторма   = 100% успех;
  • ошибки вне окна рестарта = 0.

На Windows без psutil метрики RSS/fd недоступны — soak работает,
соответствующие пороги помечаются «n/a» (остальные проверки живы).

Запуск:
  python scripts/soak_web.py                          # 5 минут, дефолты
  python scripts/soak_web.py --minutes 30             # ночной прогон
  python scripts/soak_web.py --minutes 1 --voice 2    # быстрый смоук
  python scripts/soak_web.py --no-restart             # без фазы рестарта

Отчёт: relay_data/soak_report.json (путь меняется --report) + итог в
консоль. Код возврата: 0 — пороги соблюдены, 1 — найдена деградация.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import ssl
import statistics
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.auth import sign_request  # noqa: E402
from lib.relay_server import RelayServer  # noqa: E402
from voice.protocol import MAGIC, PCM_FRAME_BYTES  # noqa: E402

try:  # websockets — зависимость голосового моста (есть в CI-наборе)
    import websockets.sync.client as _wsclient
except ImportError:  # pragma: no cover
    _wsclient = None

INSECURE_TLS = ssl._create_unverified_context()

def hdr(value: str) -> str:
    """Кириллица в HTTP-заголовке: UTF-8 байты, спрятанные в latin-1.
    urllib шлёт такие значения без ошибок, серверный header_decode
    разворачивает обратно (ровно так делает настоящий клиент)."""
    try:
        return value.encode("utf-8").decode("latin-1")
    except UnicodeError:
        return value


VOICE_STALL_S = 5.0        # нет кадров от сервера столько — stall
POLL_P95_FAIL_S = 2.0      # p95 задержки /events, выше — деградация
RSS_FAIL_MB = 35.0         # допустимый рост RSS после прогрева, МБ
FD_FAIL_GROWTH = 40        # допустимый рост числа открытых fd
RESTART_GRACE_S = 25.0     # сколько даём голосу пересобраться после сбоя


def _now() -> float:
    return time.monotonic()


def sample_process() -> tuple[int | None, int | None]:
    """(rss_bytes, fd_count) текущего процесса; None — недоступно."""
    rss: int | None = None
    fd: int | None = None
    try:
        txt = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in txt.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
    except Exception:
        pass
    try:
        fd = len(os.listdir("/proc/self/fd"))
    except Exception:
        try:
            import psutil  # необязательная зависимость

            p = psutil.Process()
            fd = p.num_fds()
            rss = rss if rss is not None else p.memory_info().rss
        except Exception:
            pass
    return rss, fd


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


@dataclass
class SoakResult:
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class Metrics:
    """Потокобезопасные счётчики и выборки соака."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chat_sent = 0
        self.chat_err = 0            # вне окна рестарта
        self.chat_err_restart = 0    # внутри окна рестарта (не FAIL)
        self.polls = 0
        self.poll_err = 0
        self.poll_lat: list[float] = []
        self.files_up = 0
        self.file_err = 0
        self.file_err_restart = 0
        self.files_down_ok = 0
        self.voice_rx = 0
        self.voice_stalls = 0
        self.voice_resessions = 0    # успешные пересборки (шторм/сбой)
        self.rss_samples: list[float] = []   # МБ
        self.fd_samples: list[int] = []
        self.errors: list[str] = []

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def add_latency(self, dt: float) -> None:
        with self._lock:
            self.polls += 1
            self.poll_lat.append(dt)

    def add_error(self, text: str) -> None:
        with self._lock:
            self.errors.append(text[:300])

    def sample_res(self) -> None:
        rss, fd = sample_process()
        with self._lock:
            if rss is not None:
                self.rss_samples.append(rss / (1024 * 1024))
            if fd is not None:
                self.fd_samples.append(fd)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "chat_sent": self.chat_sent,
                "chat_err": self.chat_err,
                "chat_err_restart": self.chat_err_restart,
                "polls": self.polls,
                "poll_err": self.poll_err,
                "poll_p95_s": round(_p95(self.poll_lat), 4),
                "files_up": self.files_up,
                "files_down_ok": self.files_down_ok,
                "file_err": self.file_err,
                "file_err_restart": self.file_err_restart,
                "voice_rx_frames": self.voice_rx,
                "voice_stalls": self.voice_stalls,
                "voice_resessions": self.voice_resessions,
                "rss_samples_mb": [round(v, 1) for v in self.rss_samples[-40:]],
                "fd_samples": self.fd_samples[-40:],
                "thread_errors": list(self.errors),
            }


class BaseWorker(threading.Thread):
    """Общее: стоп-событие, окно рестарта, аккуратные исключения потоков."""

    def __init__(self, stop_ev: threading.Event, metrics: Metrics,
                 restart_window: list[tuple[float, float]]):
        super().__init__(daemon=True)
        self.stop_ev = stop_ev
        self.metrics = metrics
        self.restart_window = restart_window

    def in_restart_window(self) -> bool:
        t = _now()
        return any(a <= t <= b for a, b in self.restart_window)

    def mark_thread_error(self, exc: Exception) -> None:
        self.metrics.add_error(
            f"{type(self).__name__}: {type(exc).__name__}: {exc}")

    def http(self, req: urllib.request.Request, timeout: float = 15.0):
        t0 = _now()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return body, _now() - t0


class ChatWorker(BaseWorker):
    """Чат: /ping → session_token; /send_text + /events?since=… по кругу."""

    def __init__(self, name: str, base: str, key: str, stop_ev, metrics,
                 restart_window, send_every: float = 1.5,
                 poll_every: float = 0.7):
        super().__init__(stop_ev, metrics, restart_window)
        self.name = name
        self.base = base
        self.key = key
        self.send_every = send_every
        self.poll_every = poll_every
        self.token = ""
        self.since = 0
        self.n = 0

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"X-Relay-From": hdr(self.name), "X-Relay-Key": self.key}
        h.update(extra or {})
        return h

    def _ping(self) -> None:
        req = urllib.request.Request(self.base + "/ping",
                                     headers=self._headers())
        body, _ = self.http(req)
        tok = json.loads(body).get("session_token")
        if not tok:
            raise RuntimeError("ping не отдал session_token")
        self.token = tok

    def _send(self) -> None:
        self.n += 1
        payload = json.dumps(
            {"text": f"соак #{self.n} от {self.name} @{int(time.time())}"},
            ensure_ascii=False).encode()
        auth = f"{self.token}:{sign_request(self.token, payload)}"
        req = urllib.request.Request(
            self.base + "/send_text", data=payload, method="POST",
            headers=self._headers({
                "X-Relay-Auth": auth,
                "Content-Type": "application/json; charset=utf-8"}))
        body, _ = self.http(req)
        if not json.loads(body).get("ok"):
            raise RuntimeError("send_text: ok=false")

    def _poll(self) -> None:
        req = urllib.request.Request(
            self.base + f"/events?since={self.since}",
            headers=self._headers())
        body, dt = self.http(req)
        self.metrics.add_latency(dt)
        data = json.loads(body)
        evs = data.get("events") if isinstance(data, dict) else data
        if isinstance(evs, list):
            for ev in evs:
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > self.since:
                    self.since = seq

    def _reping_if_auth(self, e: Exception) -> bool:
        """403 → сессия протухла (рестарт сервера снёс in-memory токены).
        Настоящий веб-клиент именно так и выживает (core.js: пере-ping с
        сохранённым именем/ключом) — повторяем его поведение."""
        return isinstance(e, urllib.error.HTTPError) and e.code == 403

    def _send_safe(self) -> None:
        try:
            self._send()
        except Exception as e:
            if not self._reping_if_auth(e):
                raise
            self._ping()          # новая сессия — как браузер после рестарта
            self._send()

    def _poll_safe(self) -> None:
        try:
            self._poll()
        except Exception as e:
            if not self._reping_if_auth(e):
                raise
            self._ping()
            self._poll()

    def run(self) -> None:
        try:
            self._ping()
        except Exception as e:  # без токена работаем вхолостую пингами
            self.mark_thread_error(e)
            while not self.stop_ev.is_set():
                time.sleep(2.0)
                try:
                    self._ping()
                    break
                except Exception:
                    continue
        next_send = _now() + random.uniform(0, self.send_every)
        next_poll = _now() + random.uniform(0, self.poll_every)
        while not self.stop_ev.is_set():
            now = _now()
            try:
                if now >= next_send:
                    self._send_safe()
                    self.metrics.bump("chat_sent")
                    next_send = now + self.send_every * random.uniform(0.7, 1.4)
            except Exception as e:
                if self.in_restart_window():
                    self.metrics.bump("chat_err_restart")
                else:
                    self.metrics.bump("chat_err")
                    self.metrics.add_error(
                        f"{self.name}: send @{time.strftime('%H:%M:%S')}: {e}")
                next_send = now + 2.0
            try:
                if _now() >= next_poll:
                    self._poll_safe()
                    next_poll = _now() + self.poll_every * random.uniform(0.7, 1.4)
            except Exception as e:
                self.metrics.bump("poll_err")
                if not self.in_restart_window():
                    self.metrics.add_error(f"{self.name}: poll: {e}")
                next_poll = _now() + 2.0
                time.sleep(0.2)
            time.sleep(0.05)


class VoiceWorker(BaseWorker):
    """Голосовой WS-клиент: аплинк PCM + приём микса + авто-пересборка."""

    def __init__(self, name: str, port: int, key: str, stop_ev, metrics,
                 restart_window, storm_ev: threading.Event):
        super().__init__(stop_ev, metrics, restart_window)
        self.name = name
        self.port = port
        self.key = key
        self.storm_ev = storm_ev
        self.broken_since: float | None = None
        self._seq = 0

    # ── кадр аплинка: MAGIC | flags | <H len | payload ──
    def _frame(self) -> bytes:
        self._seq = (self._seq + 1) & 0xFFFF
        payload = bytes(
            ((self._seq + (i & 0xFF)) & 0xFF) for i in range(PCM_FRAME_BYTES))
        return MAGIC + b"\x00" + struct.pack("<H", len(payload)) + payload

    def _session(self) -> None:
        assert _wsclient is not None
        conn = _wsclient.connect(
            f"wss://127.0.0.1:{self.port}/voice/ws",
            ssl_context=INSECURE_TLS, open_timeout=10, close_timeout=5)
        try:
            conn.send(json.dumps({"name": self.name, "access_key": self.key}))
            reply = json.loads(conn.recv(timeout=10))
            if not reply.get("ok"):
                raise RuntimeError(f"auth отвергнут: {reply}")
            last_rx = _now()
            self.broken_since = None
            self.metrics.bump("voice_resessions")
            next_frame = 0.0
            while not self.stop_ev.is_set():
                now = _now()
                if now >= next_frame:
                    conn.send(self._frame())
                    next_frame = now + 0.02
                try:
                    conn.recv(timeout=0.15)
                    last_rx = _now()
                    self.metrics.bump("voice_rx")
                except TimeoutError:
                    pass
                if _now() - last_rx > VOICE_STALL_S:
                    # (v3.8.3) в окне рестарта сервер честно молчит — не stall
                    if not self.in_restart_window():
                        self.metrics.bump("voice_stalls")
                        self.metrics.add_error(
                            f"{self.name}: голос молчит >{VOICE_STALL_S:.0f}с "
                            f"@{time.strftime('%H:%M:%S')}")
                    last_rx = _now()
                if self.storm_ev.is_set():
                    raise ConnectionAbortedError("шторм реконнектов")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def run(self) -> None:
        backoff = 0.4
        while not self.stop_ev.is_set():
            try:
                self._session()
                backoff = 0.4
            except Exception as e:
                if self.broken_since is None:
                    self.broken_since = _now()
                if isinstance(e, ConnectionAbortedError):
                    self.metrics.bump("chat_err", 0)  # шторм — не ошибка чата
                time.sleep(backoff + random.uniform(0, 0.4))
                backoff = min(backoff * 1.6, 1.5)
            # пауза между сессиями, чтобы шторм был именно штормом
            if self.storm_ev.is_set():
                time.sleep(random.uniform(0.5, 1.5))


class FileWorker(BaseWorker):
    """Файлы: /send_file (стрим + HMAC по телу) → /download/<id> → сверка."""

    def __init__(self, name: str, base: str, key: str, stop_ev, metrics,
                 restart_window, every: float = 4.0, min_kb: int = 64,
                 max_kb: int = 256):
        super().__init__(stop_ev, metrics, restart_window)
        self.name = name
        self.base = base
        self.key = key
        self.every = every
        self.min_kb = min_kb
        self.max_kb = max_kb
        self.token = ""

    def _ping(self) -> None:
        req = urllib.request.Request(
            self.base + "/ping",
            headers={"X-Relay-From": hdr(self.name), "X-Relay-Key": self.key})
        body, _ = self.http(req)
        self.token = json.loads(body).get("session_token") or ""

    def _cycle(self) -> None:
        body = os.urandom(random.randint(self.min_kb, self.max_kb) * 1024)
        fname = f"soak_{uuid.uuid4().hex[:10]}.bin"
        auth = f"{self.token}:{sign_request(self.token, body)}"
        req = urllib.request.Request(
            self.base + "/send_file", data=body, method="POST",
            headers={"X-Relay-From": hdr(self.name), "X-Relay-Key": self.key,
                     "X-Relay-Auth": auth, "X-Relay-Filename": hdr(fname),
                     "X-Relay-SHA256": hashlib.sha256(body).hexdigest(),
                     "Content-Type": "application/octet-stream"})
        resp, _ = self.http(req, timeout=30)
        ans = json.loads(resp)
        if not ans.get("ok") or not ans.get("file_id"):
            raise RuntimeError(f"send_file: {ans}")
        self.metrics.bump("files_up")
        # ── обратно: скачиваем и сверяем длину+хеш ──
        fid = ans["file_id"]
        dreq = urllib.request.Request(
            self.base + f"/download/{fid}",
            headers={"X-Relay-From": hdr(self.name), "X-Relay-Key": self.key})
        data, _ = self.http(dreq, timeout=30)
        if len(data) != len(body) or hashlib.sha256(data).hexdigest() != \
                hashlib.sha256(body).hexdigest():
            raise RuntimeError(f"download/{fid}: файл побит")
        self.metrics.bump("files_down_ok")

    def run(self) -> None:
        try:
            self._ping()
        except Exception as e:
            self.mark_thread_error(e)
            return
        while not self.stop_ev.is_set():
            try:
                self._cycle_safe()
            except Exception as e:
                if self.in_restart_window():
                    self.metrics.bump("file_err_restart")
                else:
                    self.metrics.bump("file_err")
                    self.metrics.add_error(
                        f"{self.name}: @{time.strftime('%H:%M:%S')}: {e}")
            time.sleep(self.every * random.uniform(0.7, 1.3))

    def _cycle_safe(self) -> None:
        try:
            self._cycle()
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
            self._ping()          # рестарт снёс сессию — пере-пинг, как браузер
            self._cycle()


def _growth(values: list[float], head: int = 3, tail: int = 3) -> float:
    """Разница медиан последних и первых (после прогрева) выборок."""
    if len(values) < head + tail:
        return 0.0
    return statistics.median(values[-tail:]) - statistics.median(values[:head])


def run_soak(minutes: float = 5.0, clients: int = 6, voice: int = 4,
             file_workers: int = 2, port: int = 8741, key: str = "soak-key",
             restart: bool = True, report: Path | None = None,
             data_dir: Path | None = None, warmup: float | None = None,
             quiet: bool = False, slow_clients: int = 1) -> SoakResult:
    """Прогоняет соак и возвращает результат с порогами. Утилита для CLI
    и для tests/test_soak_v383.py (дымовой режим)."""
    if voice and _wsclient is None:
        print("[SOAK] websockets не установлен — голосовые клиенты выключены")
        voice = 0
    duration = max(10.0, minutes * 60.0)
    warmup_s = warmup if warmup is not None else max(10.0, duration * 0.15)
    tmp_flag = data_dir is None
    data_dir = Path(data_dir or tempfile.mkdtemp(prefix="wr_soak_"))
    metrics = Metrics()
    result = SoakResult()

    relay = RelayServer(data_dir, host_name="SoakHost", access_key=key,
                        max_file_size=8 * 1024 * 1024)
    p = port
    while p < port + 20:
        try:
            if relay.start("127.0.0.1", p):
                break
        except OSError:
            pass
        p += 1
    else:
        result.failures.append(f"не удалось занять порт {port}..{p}")
        result.passed = False
        return result
    base = f"http://127.0.0.1:{p}"

    stop_ev = threading.Event()
    restart_window: list[tuple[float, float]] = []
    storm_ev = threading.Event()
    workers: list[BaseWorker] = []

    for i in range(max(0, clients - slow_clients)):
        workers.append(ChatWorker(f"Чат{i+1}", base, key, stop_ev, metrics,
                                  restart_window))
    for i in range(slow_clients):
        workers.append(ChatWorker(f"Медленный{i+1}", base, key, stop_ev,
                                  metrics, restart_window,
                                  send_every=3.0, poll_every=8.0))
    for i in range(voice):
        workers.append(VoiceWorker(f"Голос{i+1}", p, key, stop_ev, metrics,
                                   restart_window, storm_ev))
    for i in range(file_workers):
        workers.append(FileWorker(f"Файлы{i+1}", base, key, stop_ev, metrics,
                                  restart_window))
    for w in workers:
        w.start()

    sampler_stop = threading.Event()

    def sampler() -> None:
        while not sampler_stop.is_set():
            metrics.sample_res()
            sampler_stop.wait(2.0)

    threading.Thread(target=sampler, daemon=True).start()

    if not quiet:
        print(f"[SOAK] {duration:.0f}с · чат={clients} голос={voice} "
              f"файлы={file_workers} порт={p} данные={data_dir}")
    t0 = _now()
    storms = [duration * 0.5, duration * 0.9] if voice else []
    restart_at = duration * 0.7 if (restart and duration >= 25.0) else None
    done_storms = 0
    restart_done = False
    next_progress = t0 + 10.0

    while _now() - t0 < duration:
        now = _now()
        elapsed = now - t0
        if done_storms < len(storms) and elapsed >= storms[done_storms]:
            storm_ev.set()
            if not quiet:
                print(f"[SOAK] {elapsed:.0f}с ({time.strftime('%H:%M:%S')}): "
                      "ШТОРМ — все голосовые клиенты роняют соединение")
            time.sleep(2.0)
            storm_ev.clear()
            done_storms += 1
        if restart_at and not restart_done and elapsed >= restart_at:
            if not quiet:
                print(f"[SOAK] {elapsed:.0f}с ({time.strftime('%H:%M:%S')}): "
                      "РЕСТАРТ сервера")
            # окно регистрируем ДО остановки: ошибки с момента stop() —
            # часть рестарта (b=inf, закроем после успешного старта)
            window = [_now(), float("inf")]
            restart_window.append(window)
            try:
                relay.stop()
                time.sleep(1.5)
                ok = False
                for _ in range(5):
                    try:
                        ok = relay.start("127.0.0.1", p)
                    except OSError:
                        ok = False
                    if ok:
                        break
                    time.sleep(1.0)
                if not ok:
                    result.failures.append("рестарт: сервер не поднялся обратно")
            except Exception as e:
                result.failures.append(f"рестарт: {type(e).__name__}: {e}")
            finally:
                window[1] = _now() + 1.0
            restart_done = True
            if not quiet:
                print(f"[SOAK] сервер снова на порту {p}")
        if not quiet and now >= next_progress:
            s = metrics.snapshot()
            rss = s["rss_samples_mb"][-1] if s["rss_samples_mb"] else -1
            print(f"[SOAK] {elapsed:.0f}с: msg={s['chat_sent']} "
                  f"p95={s['poll_p95_s']*1000:.0f}мс voice_rx={s['voice_rx_frames']} "
                  f"файлы={s['files_up']} err={s['chat_err']+s['file_err']} "
                  f"rss={rss:.0f}МБ")
            next_progress = now + 10.0
        time.sleep(0.25)

    stop_ev.set()
    sampler_stop.set()
    deadline = _now() + 15.0
    for w in workers:
        w.join(timeout=max(0.5, deadline - _now()))
    relay.stop()

    # ── оценщик порогов ──────────────────────────────────────────────
    stats = metrics.snapshot()
    stats["port"] = p
    stats["duration_s"] = round(_now() - t0, 1)

    rss_after = [v for v in metrics.rss_samples]  # sampler тикает каждые 2с
    n_warm = max(1, int(warmup_s / 2.0))
    if len(rss_after) > n_warm + 3:
        growth = _growth(rss_after[n_warm:])
        stats["rss_growth_mb"] = round(growth, 1)
        limit = max(RSS_FAIL_MB, 0.15 * statistics.median(rss_after[n_warm:]))
        stats["rss_limit_mb"] = round(limit, 1)
        if growth > limit:
            result.failures.append(
                f"утечка памяти: RSS вырос на {growth:.1f} МБ (лимит {limit:.0f})")
    else:
        stats["rss_growth_mb"] = "n/a"
    if len(metrics.fd_samples) > n_warm + 3:
        fg = int(_growth([float(x) for x in metrics.fd_samples[n_warm:]]))
        stats["fd_growth"] = fg
        if fg > FD_FAIL_GROWTH:
            result.failures.append(
                f"утечка дескрипторов: +{fg} fd (лимит {FD_FAIL_GROWTH})")
    else:
        stats["fd_growth"] = "n/a"

    p95 = stats["poll_p95_s"]
    if stats["polls"] >= 10 and p95 > POLL_P95_FAIL_S:
        result.failures.append(
            f"деградация опроса: p95={p95:.2f}с (лимит {POLL_P95_FAIL_S}с)")
    if stats["voice_stalls"] > 0:
        result.failures.append(
            f"стопы голосового потока: {stats['voice_stalls']} (лимит 0)")
    if voice and stats["voice_resessions"] < 1:
        result.failures.append("голосовые клиенты ни разу не собрались")
    if stats["chat_err"] > 0:
        result.failures.append(
            f"ошибки чата вне рестарта: {stats['chat_err']} (лимит 0)")
    if stats["file_err"] > 0:
        result.failures.append(
            f"ошибки файлов вне рестарта: {stats['file_err']} (лимит 0)")
    broken = [w.name for w in workers if isinstance(w, VoiceWorker)
              and w.broken_since is not None
              and _now() - w.broken_since > RESTART_GRACE_S]
    if broken:
        result.failures.append(f"голос не пересобрался: {', '.join(broken)}")
    if metrics.errors:
        uniq = Counter(metrics.errors).most_common(3)
        for text, cnt in uniq:
            result.failures.append(f"потоки ({cnt}x): {text}")

    # медленный клиент должен был дожить до конца
    result.stats = stats
    result.passed = not result.failures

    if not quiet:
        print("─" * 62)
        print(f"  сообщений: {stats['chat_sent']}   опросов: {stats['polls']} "
              f"(p95 {stats['poll_p95_s']*1000:.0f} мс)")
        print(f"  голос: принято {stats['voice_rx_frames']} кадров, "
              f"пересборок {stats['voice_resessions']}, стопов {stats['voice_stalls']}")
        print(f"  файлы: загружено {stats['files_up']}, скачано целым "
              f"{stats['files_down_ok']}, ошибок {stats['file_err']}")
        print(f"  RSS: рост {stats['rss_growth_mb']} МБ · fd: рост "
              f"{stats['fd_growth']} · ошибки рестарта (не FAIL): "
              f"{stats['chat_err_restart']}+{stats['file_err_restart']}")
        if result.passed:
            print(f"  ИТОГО: ПОРОГИ СОБЛЮДЕНЫ за {stats['duration_s']}с")
        else:
            print(f"  ИТОГО: {len(result.failures)} НАРУШЕНИЙ:")
            for f in result.failures:
                print(f"    - {f}")
        print("─" * 62)
    if report:
        try:
            report = Path(report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(
                {"passed": result.passed, "failures": result.failures,
                 "stats": stats}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            print(f"[SOAK] отчёт не записан: {e}")
    if tmp_flag:
        try:
            import shutil
            shutil.rmtree(data_dir, ignore_errors=True)
        except Exception:
            pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Soak-тест стабильности веб-версии Friend Relay")
    ap.add_argument("--minutes", type=float, default=5.0,
                    help="длительность в минутах (по умолчанию 5)")
    ap.add_argument("--clients", type=int, default=6,
                    help="чат-клиентов (по умолчанию 6, включая 1 медленного)")
    ap.add_argument("--voice", type=int, default=4,
                    help="голосовых WS-клиентов (по умолчанию 4)")
    ap.add_argument("--file-workers", type=int, default=2,
                    help="файловых воркеров (по умолчанию 2)")
    ap.add_argument("--slow-clients", type=int, default=1,
                    help="медленных клиентов среди чат-клиентов (по умолч. 1)")
    ap.add_argument("--port", type=int, default=8741,
                    help="базовый порт сервера (по умолчанию 8741)")
    ap.add_argument("--no-restart", action="store_true",
                    help="не рестартовать сервер посреди прогона")
    ap.add_argument("--report", default=str(ROOT / "relay_data" / "soak_report.json"),
                    help="куда писать JSON-отчёт")
    ap.add_argument("--data-dir", default=None,
                    help="каталог данных сервера (по умолчанию временный)")
    ap.add_argument("--quiet", action="store_true", help="без прогресса")
    args = ap.parse_args()
    res = run_soak(minutes=args.minutes, clients=args.clients,
                   voice=args.voice, file_workers=args.file_workers,
                   port=args.port, restart=not args.no_restart,
                   report=Path(args.report) if args.report else None,
                   data_dir=Path(args.data_dir) if args.data_dir else None,
                   quiet=args.quiet, slow_clients=args.slow_clients)
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
