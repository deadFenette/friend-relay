"""MusicBot — синхронная музыка для всех: хост стримит, гости выбирают.

v3.3.0 — полная переработка под веб-клиент (вкладка «Музыка»):

- Папка библиотеки по умолчанию: <data_dir>/music (lib/constants.py:
  MUSIC_DIR_NAME) — хосту достаточно закинуть туда файлы, ничего
  настраивать не нужно. Сменить можно через веб (POST /music/settings)
  или командой !music setdir <путь>.
- Стабильные ID треков: sha1(относительный путь)[:16] — URL-безопасные,
  не ломаются при одинаковых именах в разных папках (раньше id = имя
  файла, два «track01.mp3» в разных альбомах конфликтовали).
- Метаданные через mutagen (ОПЦИОНАЛЬНО): длительность/артист/название.
  Без mutagen всё работает: длительность = None, переход трека — по
  отчётам клиентов «трек закончился» (POST /music/ended).
- Очередь с авторами: любой гость может выбрать трек (add в очередь);
  если ничего не играет — выбранный трек стартует сразу.
- Режим open_dj (по умолчанию ВКЛ): play/pause/skip/seek доступны всем.
  Хост может выключить (POST /music/settings или !music opendj off) —
  тогда управление только у хоста и админов музыки, у гостей — очередь.
- Синхронизация клиентов: get_sync_state() отдаёт position, вычисленный
  от серверных часов (time.time()), плюс server_time и state_version —
  клиент корректирует дрейф и мгновенно видит смену состояния.
- Авто-переход: фоновый поток (BaseBot.start() зовёт _background_loop)
  следит за концом трека и включает следующий из очереди.

Команды чата (!music ...) сохранены и ведут к тем же api_*-методам,
что и HTTP-эндпоинты: веб и Qt управляются одинаково.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

from lib.bots.base import BaseBot
from lib.constants import (
    MUSIC_DIR_NAME,
    MUSIC_MAX_FILE_SIZE,
    MUSIC_SUPPORTED_FORMATS,
)

log = logging.getLogger("friend_relay.music")

# Позиция, раньше которой отчёт «трек закончился» игнорируется (защита
# от мгновенного скипа спам-кликом по /music/ended).
ENDED_MIN_POSITION = 3.0
# Насколько далеко за конец трека авто-переход срабатывает (запас на
# неточность длительности у VBR-файлов).
ENDED_GRACE_SEC = 0.75
# Максимальная длина очереди — защита от переполнения состояния.
QUEUE_LIMIT = 100


def _stable_track_id(rel_path: str) -> str:
    """Стабильный URL-безопасный ID: sha1 нормализованного пути.

    Нормализация: слэши всегда '/', регистр сохранён (файлы «A.mp3» и
    «a.mp3» на Windows — одно и то же, но б-library кейс редкий; главное —
    ID не меняется между рестартами сервера на той же ОС)."""
    norm = rel_path.replace("\\", "/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _probe_meta(path: Path) -> dict:
    """Длительность/теги через mutagen, если он установлен.

    Возвращает {"duration": float|None, "artist": str, "title": str}.
    Любая ошибка — тихий фолбэк (длительность неизвестна, тегов нет):
    библиотека важнее метаданных."""
    out: dict[str, Any] = {"duration": None, "artist": "", "title": ""}
    try:
        import mutagen  # noqa: F401  (проверка наличия пакета)
        from mutagen import File as MutagenFile

        m = MutagenFile(str(path), easy=True)
        if m is None or m.info is None:
            return out
        try:
            out["duration"] = float(m.info.length)
        except (AttributeError, ValueError, TypeError):
            pass
        def _tag(key: str) -> str:
            try:
                v = (m.get(key) or [""])[0]
                return str(v).strip()
            except Exception:
                return ""
        out["artist"] = _tag("artist")
        t = _tag("title")
        out["title"] = t if t else path.stem
    except ImportError:
        pass
    except Exception as e:  # битый файл / экзотический контейнер
        log.debug("mutagen не смог прочитать %s: %s", path.name, e)
    return out


def _fmt_sec(s: float | None) -> str:
    if not s or s <= 0:
        return "?:??"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


class MusicBot(BaseBot):
    """Синхронный музыкальный плеер хоста (стрим + очередь + права)."""

    def __init__(self, data_dir: Path, host_name: str = ""):
        super().__init__("music", "🎵 DJ Бот", data_dir)

        self.host_name = host_name

        # ── папка библиотеки: по умолчанию <data_dir>/music ─────────────
        stored = str(self.get_data("music_dir", "") or "")
        if stored:
            self.music_dir = Path(stored)
        else:
            self.music_dir = data_dir / MUSIC_DIR_NAME
            self.set_data("music_dir", str(self.music_dir))
        try:
            self.music_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # ── настройки/права ─────────────────────────────────────────────
        self.volume = int(self.get_data("volume", 70))  # 0-100 (легаси)
        self.open_dj = bool(self.get_data("open_dj", True))
        self.admins: set[str] = set(self.get_data("admins", []))

        # ── очередь: [{track_id, by, at}] ───────────────────────────────
        self.queue: list[dict] = list(self.get_data("queue", []))

        # ── воспроизведение (не персистим «играет» — после рестарта хоста
        #    плеер всегда на паузе на сохранённой позиции) ────────────────
        self._lock = threading.RLock()
        self._playing = False
        self._started_at = 0.0      # time.time() старта текущего отрезка
        self._pause_pos = float(self.get_data("current_position", 0.0) or 0.0)
        self._current_id: str | None = self.get_data("current_track_id") or None
        self._state_version = int(self.get_data("state_version", 0) or 0)
        self._ended_reports: set[str] = set()
        # (v3.5.0) Авто-переход по длительности трека. False — только ручные
        # /music/ended отчёты (нужно тестам: у тестовых mp3 длительность
        # 0.078с — цикл успевал перескочить трек прямо между операциями).
        self.auto_advance = True

        # ── библиотека ──────────────────────────────────────────────────
        self.library: dict[str, dict] = {}
        self._scan_library()

    # ═════════════════════════ библиотека ═════════════════════════

    def _scan_library(self) -> None:
        """Сканирует music_dir и строит библиотеку с метаданными.

        Кеш метаданных живёт в данных бота (music_meta_cache): ключ
        «отн.путь|mtime|size», значение — длительность/теги. Повторный
        старт сервера на большой библиотеке не перекодирует все файлы."""
        with self._lock:
            self.library = {}
            if not self.music_dir or not self.music_dir.exists():
                return
            cache: dict[str, dict] = dict(self.get_data("music_meta_cache", {}) or {})
            dirty = False
            try:
                files = [p for p in sorted(self.music_dir.rglob("*"))
                         if p.is_file()
                         and p.suffix.lower() in MUSIC_SUPPORTED_FORMATS]
            except OSError as e:
                log.warning("MusicBot: не удалось просканировать %s: %s",
                            self.music_dir, e)
                return
            for file_path in files:
                try:
                    size = file_path.stat().st_size
                except OSError:
                    continue
                if size > MUSIC_MAX_FILE_SIZE:
                    log.info("MusicBot: пропущен (больше лимита) %s",
                             file_path.name)
                    continue
                rel = file_path.relative_to(self.music_dir)
                track_id = _stable_track_id(str(rel))
                cache_key = f"{rel}|{int(file_path.stat().st_mtime)}|{size}"
                meta = cache.get(cache_key)
                if meta is None:
                    meta = _probe_meta(file_path)
                    cache[cache_key] = meta
                    dirty = True
                self.library[track_id] = {
                    "id": track_id,
                    "path": str(file_path),
                    "filename": file_path.name,
                    "folder": str(rel.parent) if str(rel.parent) != "." else "",
                    "title": meta.get("title") or file_path.stem,
                    "artist": meta.get("artist") or "",
                    "duration": meta.get("duration"),
                    "size": size,
                }
            if dirty:
                # Кеш метаданных может быть большим — чистим устаревшие ключи
                live_keys = set()
                for file_path in files:
                    try:
                        rel = file_path.relative_to(self.music_dir)
                        live_keys.add(
                            f"{rel}|{int(file_path.stat().st_mtime)}"
                            f"|{file_path.stat().st_size}")
                    except OSError:
                        continue
                cache = {k: v for k, v in cache.items() if k in live_keys}
                self.set_data("music_meta_cache", cache)

    def _track(self, track_id: str | None) -> dict | None:
        if not track_id:
            return None
        return self.library.get(track_id)

    @staticmethod
    def _track_public(t: dict) -> dict:
        """Трек без локального пути — клиенту он не нужен."""
        return {
            "id": t["id"],
            "filename": t["filename"],
            "folder": t.get("folder", ""),
            "title": t.get("title") or t["filename"],
            "artist": t.get("artist", ""),
            "duration": t.get("duration"),
            "size": t.get("size", 0),
        }

    def get_library(self) -> list[dict]:
        """Публичная библиотека (для GET /music/library)."""
        with self._lock:
            tracks = [self._track_public(t) for t in self.library.values()]
        tracks.sort(key=lambda t: ((t.get("artist") or "").lower(),
                                   (t.get("title") or "").lower()))
        return tracks

    # ═════════════════════════ состояние ═════════════════════════

    def _position_locked(self) -> float:
        """Текущая позиция (сек) — вызывать под локом."""
        if self._playing and self._started_at > 0:
            return self._pause_pos + (time.time() - self._started_at)
        return self._pause_pos

    def _bump_locked(self) -> None:
        self._state_version += 1
        self.set_data("state_version", self._state_version)

    def _persist_playback_locked(self) -> None:
        self.set_data("current_track_id", self._current_id)
        self.set_data("current_position", round(self._pause_pos, 3))

    def _persist_queue_locked(self) -> None:
        self.set_data("queue", self.queue)

    def api_state(self) -> dict:
        """Полное состояние для GET /music/sync (и для фасада)."""
        with self._lock:
            cur = self._track(self._current_id)
            pos = self._position_locked()
            dur = cur.get("duration") if cur else None
            if cur and dur and pos > dur:
                pos = float(dur)
            state = {
                "playing": self._playing,
                "track": self._track_public(cur) if cur else None,
                "position": round(max(0.0, pos), 2),
                "server_time": time.time(),
                "state_version": self._state_version,
                "queue": [
                    {
                        "track": self._track_public(q["track"])
                        if isinstance(q.get("track"), dict) else
                        (self._track_public(self.library[q["track_id"]])
                         if q.get("track_id") in self.library else
                         {"id": q.get("track_id", ""),
                          "title": q.get("title", "?"),
                          "filename": q.get("title", "?"),
                          "artist": "", "folder": "", "duration": None,
                          "size": 0}),
                        "by": q.get("by", ""),
                        "at": q.get("at", 0),
                    }
                    for q in self.queue
                ],
                "open_dj": self.open_dj,
                "volume": self.volume,
                "listeners_hint": "",
            }
            # Легаси-поля для старых клиентов (music.js v1 читал их напрямую)
            if cur:
                state["track_id"] = cur["id"]
                state["filename"] = cur["filename"]
            return state

    # совместимость со старым фасадом
    def get_sync_state(self) -> dict | None:
        st = self.api_state()
        return st if st.get("track") else None

    # ═════════════════════════ права ═════════════════════════

    def _is_admin(self, sender: str) -> bool:
        if sender and sender == self.host_name:
            return True
        return sender in self.admins

    def _can_control(self, sender: str) -> bool:
        """open_dj включён — управляют все; выключен — хост/админы."""
        return self.open_dj or self._is_admin(sender)

    # ═════════════════════════ api_* (HTTP + команды) ═════════════════════════
    # Все методы возвращают dict с ok:True/False — транспорт отдаёт его
    # как есть, клиент показывает error при ok:False.

    def api_play(self, sender: str, track_id: str | None = None,
                 position: float | None = None) -> dict:
        """Играть трек (или возобновить текущий, если track_id нет)."""
        with self._lock:
            if not self._can_control(sender):
                return {"ok": False, "error":
                        "DJ-режим закрыт: play/pause только у хоста и админов "
                        "(гости добавляют треки в очередь)"}
            track = self._track(track_id) if track_id else self._track(self._current_id)
            if track is None and not track_id and self.queue:
                # Возобновлять нечего — берём голову очереди
                head = self.queue.pop(0)
                self._persist_queue_locked()
                track = self._track(head.get("track_id"))
            if track is None:
                return {"ok": False, "error":
                        "Трек не найден (библиотека обновлялась?)"}
            if position is None:
                # Новая трека — с нуля; возобновление — с сохранённой позиции
                position = 0.0 if track_id or track["id"] != self._current_id \
                    else self._pause_pos
            self._current_id = track["id"]
            self._pause_pos = max(0.0, float(position))
            self._started_at = time.time()
            self._playing = True
            self._ended_reports.clear()
            self._bump_locked()
            self._persist_playback_locked()
            return {"ok": True, "track": self._track_public(track)}

    def api_pause(self, sender: str) -> dict:
        with self._lock:
            if not self._can_control(sender):
                return {"ok": False, "error":
                        "DJ-режим закрыт: play/pause только у хоста и админов"}
            if not self._playing:
                self._pause_pos = self._position_locked()
                return {"ok": True, "playing": False}
            self._pause_pos = self._position_locked()
            self._playing = False
            self._bump_locked()
            self._persist_playback_locked()
            return {"ok": True, "playing": False}

    def api_seek(self, sender: str, position: float) -> dict:
        with self._lock:
            if not self._can_control(sender):
                return {"ok": False, "error":
                        "DJ-режим закрыт: перемотка только у хоста и админов"}
            cur = self._track(self._current_id)
            if cur is None:
                return {"ok": False, "error": "Ничего не играет"}
            dur = cur.get("duration")
            pos = max(0.0, float(position))
            if dur:
                pos = min(pos, float(dur) + ENDED_GRACE_SEC)
            self._pause_pos = pos
            self._started_at = time.time()
            self._ended_reports.clear()
            self._bump_locked()
            self._persist_playback_locked()
            return {"ok": True, "position": round(pos, 2)}

    def _next_locked(self) -> dict:
        """Взять следующий из очереди или остановиться (под локом)."""
        self.queue = [q for q in self.queue if self._track(q.get("track_id"))]
        if self.queue:
            head = self.queue.pop(0)
            self._persist_queue_locked()
            track = self._track(head["track_id"])
            self._current_id = track["id"]
            self._pause_pos = 0.0
            self._started_at = time.time()
            self._playing = True
            self._ended_reports.clear()
            self._bump_locked()
            self._persist_playback_locked()
            return {"ok": True, "playing": True,
                    "track": self._track_public(track),
                    "by": head.get("by", "")}
        self._current_id = None
        self._playing = False
        self._pause_pos = 0.0
        self._bump_locked()
        self._persist_playback_locked()
        return {"ok": True, "playing": False, "track": None}

    def api_skip(self, sender: str) -> dict:
        with self._lock:
            if not self._can_control(sender):
                return {"ok": False, "error":
                        "DJ-режим закрыт: skip только у хоста и админов"}
            return self._next_locked()

    def api_queue_add(self, sender: str, track_id: str) -> dict:
        with self._lock:
            track = self._track(track_id)
            if track is None:
                return {"ok": False, "error":
                        "Трек не найден в библиотеке хоста"}
            if len(self.queue) >= QUEUE_LIMIT:
                return {"ok": False, "error": "Очередь переполнена"}
            # Уже в очереди? Ставим второй раз бессмысленно
            if any(q.get("track_id") == track_id for q in self.queue):
                return {"ok": False, "error": "Трек уже в очереди"}
            # Ничего не играет и не выбрано — стартуем сразу (главный
            # юзкейс «выбрал трек — играет всем»)
            if self._current_id is None and not self._playing:
                self._current_id = track_id
                self._pause_pos = 0.0
                self._started_at = time.time()
                self._playing = True
                self._ended_reports.clear()
                self._bump_locked()
                self._persist_playback_locked()
                return {"ok": True, "started": True, "position": 0,
                        "track": self._track_public(track)}
            self.queue.append({"track_id": track_id, "by": sender,
                               "at": time.time()})
            self._persist_queue_locked()
            self._bump_locked()
            return {"ok": True, "started": False, "position": len(self.queue),
                    "track": self._track_public(track)}

    def api_queue_remove(self, sender: str, index: int) -> dict:
        with self._lock:
            if index < 1 or index > len(self.queue):
                return {"ok": False, "error": "Нет такой позиции в очереди"}
            entry = self.queue[index - 1]
            # Свою заявку убрать может автор; чужую — только хост/админ
            if entry.get("by") != sender and not self._can_control(sender):
                return {"ok": False, "error":
                        "Чужую заявку может убрать только хост/админ"}
            self.queue.pop(index - 1)
            self._persist_queue_locked()
            self._bump_locked()
            return {"ok": True}

    def api_ended(self, sender: str) -> dict:
        """Клиент сообщает: аудио-элемент дошёл до конца трека.

        Если длительность известна — за концом следит фоновый цикл, отчёт
        подтверждает только при позиции > 80% длительности (докатился).
        Если НЕ известна (нет mutagen) — первый отчёт переводит на
        следующий трек."""
        with self._lock:
            if not self._playing or self._current_id is None:
                return {"ok": True, "ignored": True}
            pos = self._position_locked()
            if pos < ENDED_MIN_POSITION:
                return {"ok": True, "ignored": True}
            cur = self._track(self._current_id)
            dur = cur.get("duration") if cur else None
            if dur and pos < float(dur) * 0.8:
                return {"ok": True, "ignored": True}
            return self._next_locked()

    def api_rescan(self, sender: str = "") -> dict:
        self._scan_library()
        with self._lock:
            n = len(self.library)
        return {"ok": True, "count": n}

    def api_volume(self, sender: str, volume: int) -> dict:
        """Легаси: серверная громкость (сохранили для совместимости команд).

        Веб-клиент громкость крутит локально — это поле теперь просто
        подсказка по умолчанию для новых слушателей."""
        with self._lock:
            if not self._is_admin(sender):
                return {"ok": False, "error":
                        "Громкость по умолчанию меняет только хост/админ"}
            v = max(0, min(100, int(volume)))
            self.volume = v
            self.set_data("volume", v)
            return {"ok": True, "volume": v}

    def api_settings(self, sender: str, music_dir: str | None = None,
                     open_dj: bool | None = None) -> dict:
        with self._lock:
            if not self._is_admin(sender):
                return {"ok": False, "error":
                        "Настройки музыки меняет только хост"}
            changed = []
            if music_dir is not None and str(music_dir).strip():
                p = Path(str(music_dir).strip())
                if not p.exists() or not p.is_dir():
                    return {"ok": False, "error":
                            f"Папка не найдена: {music_dir}"}
                self.music_dir = p
                self.set_data("music_dir", str(p))
                changed.append("папка")
            if open_dj is not None:
                self.open_dj = bool(open_dj)
                self.set_data("open_dj", self.open_dj)
                changed.append("open_dj=" + ("вкл" if self.open_dj else "выкл"))
            if "папка" in changed:
                self._scan_library()
            return {"ok": True, "changed": changed,
                    "open_dj": self.open_dj,
                    "music_dir": str(self.music_dir),
                    "count": len(self.library)}

    def api_search(self, sender: str, query: str,
                   provider: str = "") -> dict:
        """Поиск во ВНЕШНИХ библиотеках (задел на большое API).

        Реализовано в lib/music_library.py; отсутствие интернета на хосте
        возвращает понятную ошибку, ничего не падает."""
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "Пустой поисковый запрос"}
        try:
            from lib.music_library import get_library_api

            return get_library_api(self.data_dir).search(q, provider)
        except Exception as e:
            return {"ok": False, "error": f"Поиск недоступен: {e}"}

    def api_import(self, sender: str, provider: str, remote_id: str) -> dict:
        """Скачать трек из внешнего провайдера в библиотеку хоста (фоном)."""
        if not (provider and remote_id):
            return {"ok": False, "error": "Нужны provider и track_id"}
        if not self._is_admin(sender) and not self.open_dj:
            return {"ok": False, "error":
                    "Импорт доступен гостям только в open-DJ режиме"}
        try:
            from lib.music_library import get_library_api

            api = get_library_api(self.data_dir)
            track = api.find(provider, remote_id)
            if track is None:
                return {"ok": False, "error":
                        "Трек не найден у провайдера (истёк кеш?)"}

            def _worker():
                try:
                    dest = api.download(track, self.music_dir)
                    log.info("MusicBot: импортирован %s", dest.name)
                    self._scan_library()
                    with self._lock:
                        self._bump_locked()
                except Exception as e:
                    log.warning("MusicBot: импорт не удался: %s", e)

            threading.Thread(target=_worker, daemon=True,
                             name="music-import").start()
            return {"ok": True, "started": True,
                    "hint": "Скачивание на хосте идёт фоном — трек появится "
                            "в библиотеке через несколько секунд"}
        except Exception as e:
            return {"ok": False, "error": f"Импорт недоступен: {e}"}

    # ═════════════════════════ фоновый цикл ═════════════════════════

    def _background_loop(self) -> None:
        """Следит за концом трека (если длительность известна)."""
        while self._running:
            time.sleep(1.0)
            try:
                with self._lock:
                    if not self._running:
                        break
                    if not (self._playing and self._current_id):
                        continue
                    if not self.auto_advance:  # (v3.5.0) см. __init__
                        continue
                    cur = self._track(self._current_id)
                    dur = cur.get("duration") if cur else None
                    if not dur:
                        continue
                    if self._position_locked() >= float(dur) + ENDED_GRACE_SEC:
                        self._next_locked()
            except Exception as e:
                log.warning("MusicBot: фоновый цикл: %s", e)

    # ═════════════════════════ команды чата ═════════════════════════

    def set_host_name(self, host_name: str) -> None:
        self.host_name = host_name

    def process_command(self, sender: str, command: str,
                        args: list[str]) -> dict:
        if not args:
            return {"text": self.get_help(), "silent": True}
        action = args[0].lower()

        def _err(res: dict) -> dict:
            return {"text": "❌ " + res.get("error", "ошибка"), "silent": True}

        if action == "play" and len(args) >= 2:
            res = self.api_play(sender, args[1])
            if not res.get("ok"):
                return _err(res)
            t = res.get("track") or {}
            return {"text": f"🎵 Играет: {t.get('title', t.get('filename', ''))}",
                    "silent": True}
        if action == "pause":
            res = self.api_pause(sender)
            return ({"text": "⏸ Пауза.", "silent": True} if res.get("ok")
                    else _err(res))
        if action == "skip":
            res = self.api_skip(sender)
            if not res.get("ok"):
                return _err(res)
            t = res.get("track")
            return {"text": f"⏭ Дальше: {t['title']}" if t
                    else "⏹ Очередь пуста — остановлено.", "silent": True}
        if action == "add" and len(args) >= 2:
            res = self.api_queue_add(sender, args[1])
            if not res.get("ok"):
                return _err(res)
            if res.get("started"):
                return {"text": "🎵 Ничего не играло — стартую сразу: "
                                + (res.get("track") or {}).get("title", ""),
                        "silent": True}
            return {"text": f"📝 В очередь (позиция {res.get('position')}): "
                            + (res.get("track") or {}).get("title", ""),
                    "silent": True}
        if action == "queue":
            with self._lock:
                if not self.queue:
                    return {"text": "📭 Очередь пуста.", "silent": True}
                lines = ["📋 Очередь:"]
                for i, q in enumerate(self.queue, 1):
                    t = self._track(q.get("track_id")) or {}
                    lines.append(f"  {i}. {t.get('title', q.get('track_id'))}"
                                 f" — от {q.get('by', '?')}")
                return {"text": "\n".join(lines), "silent": True}
        if action == "list":
            lib = self.get_library()
            if not lib:
                return {"text": "📭 Библиотека пуста. Хост: закиньте файлы в "
                                + str(self.music_dir)
                                + " или !music setdir <путь>",
                        "silent": True}
            lines = [f"🎵 Библиотека ({len(lib)}):"]
            for t in lib[:20]:
                lines.append(f"  {t['title']}"
                             + (f" — {t['artist']}" if t.get("artist") else ""))
            if len(lib) > 20:
                lines.append(f"… и ещё {len(lib) - 20}")
            return {"text": "\n".join(lines), "silent": True}
        if action == "setdir" and len(args) >= 2:
            res = self.api_settings(sender, music_dir=args[1])
            if not res.get("ok"):
                return _err(res)
            return {"text": f"📁 Папка: {res['music_dir']} · "
                            f"треков: {res['count']}", "silent": True}
        if action == "opendj" and len(args) >= 2:
            on = args[1].lower() in ("on", "вкл", "1", "true", "да")
            res = self.api_settings(sender, open_dj=on)
            if not res.get("ok"):
                return _err(res)
            return {"text": "🎛 Open-DJ " + ("включён: управляют все" if on
                    else "выключен: управление у хоста/админов"),
                    "silent": True}
        if action == "volume" and len(args) >= 2:
            try:
                res = self.api_volume(sender, int(args[1]))
            except ValueError:
                return {"text": "❌ Громкость — число 0..100", "silent": True}
            if not res.get("ok"):
                return _err(res)
            return {"text": f"🔊 Громкость по умолчанию: {res['volume']}%",
                    "silent": True}
        if action == "admin" and len(args) >= 3:
            sub, name = args[1].lower(), args[2]
            if not self._is_admin(sender):
                return {"text": "Только хост может назначать админов.",
                        "silent": True}
            if sub == "add":
                self.admins.add(name)
            elif sub == "remove":
                self.admins.discard(name)
            else:
                return {"text": "Использование: !music admin add|remove <имя>",
                        "silent": True}
            self.set_data("admins", list(self.admins))
            return {"text": f"✅ {name}: админ музыки "
                            f"{'добавлен' if sub == 'add' else 'убран'}.",
                    "silent": True}
        if action == "rescan":
            res = self.api_rescan(sender)
            return {"text": f"↻ Найдено треков: {res.get('count', 0)}",
                    "silent": True}
        if action == "search" and len(args) >= 2:
            res = self.api_search(sender, " ".join(args[1:]))
            if not res.get("ok"):
                return _err(res)
            items = res.get("results") or []
            if not items:
                return {"text": "Ничего не найдено.", "silent": True}
            lines = ["🌐 Найдено (импорт — во вкладке «Музыка»):"]
            for t in items[:10]:
                lines.append(f"  {t.get('artist', '')} — {t.get('title', '')}"
                             f" [{t.get('provider', '')}]")
            return {"text": "\n".join(lines), "silent": True}
        return {"text": self.get_help(), "silent": True}

    def get_help(self) -> str:
        return """🎵 DJ Бот — музыка синхронно для всех (вкладка «Музыка» в вебе):
  !music list                — библиотека хоста
  !music play <id>           — играть (управление см. open_dj)
  !music pause / skip        — пауза / следующий
  !music add <id>            — себе в очередь (все гости)
  !music queue               — очередь с авторами
  !music search <запрос>     — поиск во внешней библиотеке (API)
  !music setdir <путь>       — папка с музыкой (хост)
  !music opendj on|off       — управляют все или только хост (хост)
  !music volume <0-100>      — громкость по умолчанию (хост)
  !music admin add <имя>     — админ музыки (хост)"""
