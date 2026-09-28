"""ScreenHub — реестр показов + сигналинг-почта модуля «Экран».

ТОНКИЙ узел: всё состояние в RAM, без потоков и сокетов (медиа идёт
P2P через WebRTC, сервер только пересылает служебные JSON). Потоковая
безопасность — один Lock, как у RateLimiter/SessionStore: HTTP-слой
многопоточный (ThreadingHTTPServer).

Ответственность:
  - реестр живых показов: кто ведёт (publish/heartbeat/unpublish),
    кто смотрит (watch/unwatch) — для GET /screen/info;
  - сигналинг-почта: send_signal(от→кому) кладёт сообщение, poll(name)
    выдаёт адресату новые по seq (курсор на клиенте) — WebRTC-хендшейк;
  - ленивая уборка: просроченные heartbeat и письма чистятся при
    обращениях, отдельного потока нет.

Честные лимиты (screen/config.py): MAILBOX_MAX на получателя,
MAX_SIGNAL_BYTES на письмо, SIGNAL_TTL_S на письмо, STREAM_LIVE_S на
показ. Всё, что сверх, молча выбрасывается со счётчиком — сигналинг
не должен стать каналом флуда (антифлуд-ведро чата сюда не смотрит).

Имена участников — уже проверенные (HTTP-слой аутентифицирует
отправителя до вызова; см. _do_POST_impl в lib/server/http_api.py).
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Any

from screen.config import (
    MAILBOX_MAX,
    MAX_SIGNAL_BYTES,
    SIGNAL_TTL_S,
    STREAM_LIVE_S,
    WATCHER_STALE_S,
)


class _Stream:
    """Один живой показ: ведущий + метаданные + зрители."""

    __slots__ = ("beat", "sharer", "started", "title", "viewers")

    def __init__(self, sharer: str, title: str, now: float) -> None:
        self.sharer = sharer
        self.title = title or ""
        self.started = now
        self.beat = now          # последний heartbeat ведущего
        # (v3.6.3) зритель → время последнего пульса watch(on=true).
        # Раньше это был set: вкладка без «bye» вечно висела в счётчике.
        self.viewers: dict[str, float] = {}


class ScreenHub:
    """Реестр показов + почта сигналинга. Один Lock на всё."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        # имя показа → _Stream
        self._streams: dict[str, _Stream] = {}
        # получатель → [(seq, ts, frm, data)]
        self._mail: dict[str, list[tuple[int, float, str, dict]]] = {}
        # служебные счётчики (для /screen/info — видны хосту)
        self.dropped_signals = 0

    # ── уборка ───────────────────────────────────────────────────────────
    def _sweep(self, now: float) -> None:
        """Ленивая чистка (вызывать под локом): мёртвые показы, зрители-
        молчуны и старые письма. Показ мёртв, если ведущий молчит дольше
        STREAM_LIVE_S; зритель исчез из счётчика, если его пульс
        watch(on=true) был позднее WATCHER_STALE_S назад (v3.6.3 —
        вкладка умерла без «bye» и счётчик перестал врать)."""
        dead = [n for n, s in self._streams.items() if now - s.beat > STREAM_LIVE_S]
        for n in dead:
            del self._streams[n]
        for s in self._streams.values():
            stale = [v for v, t in s.viewers.items() if now - t > WATCHER_STALE_S]
            for v in stale:
                del s.viewers[v]
        if self._mail:
            fresh: dict[str, list[tuple[int, float, str, dict]]] = {}
            for who, box in self._mail.items():
                keep = [m for m in box if now - m[1] <= SIGNAL_TTL_S]
                if keep:
                    fresh[who] = keep
            self._mail = fresh

    # ── реестр показов ───────────────────────────────────────────────────
    def publish(self, sharer: str, title: str, now: float | None = None) -> bool:
        """Ведущий объявил показ / прислал heartbeat. True — это НАЧАЛО
        (показа не было), False — продление существующего."""
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            existed = sharer in self._streams
            if existed:
                st = self._streams[sharer]
                st.beat = now
                st.title = title or st.title
            else:
                self._streams[sharer] = _Stream(sharer, title, now)
            return not existed

    def unpublish(self, sharer: str) -> bool:
        """Показ окончен (кнопка или закрытие вкладки с явным стопом).
        True — показ был и снят."""
        with self._lock:
            return self._streams.pop(sharer, None) is not None

    def watch(self, viewer: str, sharer: str, on: bool) -> bool:
        """Зритель сообщил о фактическом подключении (on) или уходе.
        (v3.6.3) on=true — это ещё и пульс живости: клиент повторяет его
        каждые ~12с (SCREEN_WATCH_BEAT_MS), хаб ставит отметку времени и
        лениво чистит молчунов старше WATCHER_STALE_S."""
        now = time.time()
        with self._lock:
            self._sweep(now)
            st = self._streams.get(sharer)
            if st is None:
                return False
            if on:
                st.viewers[viewer] = now
            else:
                st.viewers.pop(viewer, None)
            return True

    def snapshot(self) -> list[dict]:
        """Список живых показов для GET /screen/info."""
        now = time.time()
        with self._lock:
            self._sweep(now)
            out = []
            for s in self._streams.values():
                out.append({
                    "from": s.sharer,
                    "title": s.title,
                    "started": round(now - s.started, 1),
                    "viewers": sorted(s.viewers),
                })
            # свежие показы — сверху
            out.sort(key=lambda x: x["from"])
            return out

    # ── сигналинг-почта ──────────────────────────────────────────────────
    def send_signal(self, frm: str, to: str, data: dict) -> int | None:
        """Положить сигнал адресату. Возвращает seq письма или None,
        если письмо отброшено (переполнение/слишком большое)."""
        now = time.time()
        with self._lock:
            self._sweep(now)
            box = self._mail.setdefault(to, [])
            if len(box) >= MAILBOX_MAX:
                # выбрасываем САМОЕ СТАРОЕ письмо — свежий хендшейк важнее
                box.pop(0)
                self.dropped_signals += 1
            try:
                size = len(json.dumps(data, ensure_ascii=False))
            except (TypeError, ValueError):
                self.dropped_signals += 1
                return None
            if size > MAX_SIGNAL_BYTES:
                self.dropped_signals += 1
                return None
            seq = next(self._seq)
            box.append((seq, now, frm, data))
            return seq

    def poll(self, who: str, since: int) -> tuple[list[dict], int]:
        """Новые письма получателя с seq > since. Возвращает (список,
        последний seq). Список пуст — почта всё равно чистится."""
        now = time.time()
        with self._lock:
            self._sweep(now)
            box = self._mail.get(who)
            if not box:
                return [], since
            msgs = [m for m in box if m[0] > since]
            last = box[-1][0]
            return (
                [{"seq": m[0], "frm": m[2], "data": m[3]} for m in msgs],
                last,
            )

    def drop_mailbox(self, who: str) -> None:
        """Забыть почту участника (уход из чата/кик — гигиена RAM)."""
        with self._lock:
            self._mail.pop(who, None)

    # ── диагностика ──────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """Мелкая витрина для /server/stats (только счётчики)."""
        with self._lock:
            return {
                "streams": len(self._streams),
                "mailboxes": len(self._mail),
                "dropped_signals": self.dropped_signals,
            }
