"""music_library.py — мост к ВНЕШНИМ музыкальным библиотекам (v3.3.0).

«Задел на API самой большой библиотеки музыки»: единый интерфейс
провайдеров + два готовых адаптера:

- itunes  — iTunes Search API (Apple). КЛЮЧ НЕ НУЖЕН, работает сразу.
            Отдаёт честные метаданные (название/артист/альбом/обложка/
            длительность) и 30-секундное превью m4a (previewUrl) — его
            можно слушать в браузере и импортировать в библиотеку хоста.
- jamendo — Jamendo (независимые артисты, лицензия CC). Требует
            бесплатный client_id (https://devportal.jamendo.com) —
            укажите его в конфиге, и поиск/полная загрузка заработают.

Добавить нового провайдера = один класс с search() и registered в
_PROVIDERS. Конфиг: <data_dir>/music_api.json (создаётся лениво):

    {
      "itunes":  {"enabled": true},
      "jamendo": {"enabled": false, "client_id": "ВАШ_CLIENT_ID"}
    }

Сеть: все запросы с таймаутом 6с и размерным лимитом; отсутствие
интернета на хосте даёт понятную ошибку, ничего не падает.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "music_api.json"
HTTP_TIMEOUT = 6.0
DOWNLOAD_TIMEOUT = 30.0
MAX_IMPORT_BYTES = 100 * 1024 * 1024  # тот же лимит, что у музыки вообще

_UA = "FriendRelay/3.3 (+local music bridge)"


@dataclass
class RemoteTrack:
    """Трек, найденный во внешней библиотеке."""
    provider: str
    track_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    artwork: str = ""
    stream_url: str = ""     # можно проиграть прямо в браузере (превью)
    download_url: str = ""   # можно скачать файлом (может = stream_url)
    page_url: str = ""
    ext: str = "mp3"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "id": self.track_id,
            "title": self.title, "artist": self.artist,
            "album": self.album, "duration": self.duration,
            "artwork": self.artwork, "stream_url": self.stream_url,
            "download_url": self.download_url, "page_url": self.page_url,
            "ext": self.ext,
        }


class MusicProvider(ABC):
    id: str = ""
    name: str = ""
    note: str = ""

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[RemoteTrack]:
        """Ищет треки; ошибки сети поднимает как RuntimeError с понятным текстом."""


def _http_get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class ITunesProvider(MusicProvider):
    """iTunes Search API — без ключа, метаданные + 30с превью."""
    id = "itunes"
    name = "iTunes Search"
    note = "Метаданные и 30-секундные превью m4a; ключ не нужен"

    def search(self, query: str, limit: int = 20) -> list[RemoteTrack]:
        limit = max(1, min(int(limit), 50))
        url = ("https://itunes.apple.com/search?"
               + urllib.parse.urlencode({
                   "term": query, "media": "music", "entity": "song",
                   "limit": limit}))
        try:
            data = _http_get_json(url)
        except Exception as e:
            raise RuntimeError(
                f"iTunes недоступен с хоста (интернет?): {e}") from e
        out: list[RemoteTrack] = []
        for r in data.get("results", []) or []:
            tid = str(r.get("trackId", "")).strip()
            if not tid:
                continue
            out.append(RemoteTrack(
                provider=self.id,
                track_id=tid,
                title=str(r.get("trackName", "")).strip() or "Без названия",
                artist=str(r.get("artistName", "")).strip(),
                album=str(r.get("collectionName", "")).strip(),
                duration=float(r.get("trackTimeMillis", 0) or 0) / 1000.0,
                artwork=str(r.get("artworkUrl100", "")).strip(),
                stream_url=str(r.get("previewUrl", "")).strip(),
                download_url=str(r.get("previewUrl", "")).strip(),
                page_url=str(r.get("trackViewUrl", "")).strip(),
                ext="m4a",
            ))
        return out


class JamendoProvider(MusicProvider):
    """Jamendo — нужен бесплатный client_id (см. докстринг модуля)."""
    id = "jamendo"
    name = "Jamendo"
    note = "Полные треки независимых артистов (CC); нужен client_id"

    def __init__(self, client_id: str = "", enabled: bool = False):
        self.client_id = (client_id or "").strip()
        self.enabled = bool(self.client_id)

    def search(self, query: str, limit: int = 20) -> list[RemoteTrack]:
        if not self.client_id:
            raise RuntimeError(
                "Jamendo: не задан client_id — впишите его в music_api.json "
                "(бесплатный ключ: devportal.jamendo.com)")
        limit = max(1, min(int(limit), 50))
        url = ("https://api.jamendo.com/v3.0/tracks/?"
               + urllib.parse.urlencode({
                   "client_id": self.client_id, "format": "json",
                   "search": query, "limit": limit,
                   "include": "musicinfo"}))
        try:
            data = _http_get_json(url)
        except Exception as e:
            raise RuntimeError(
                f"Jamendo недоступен с хоста (интернет?): {e}") from e
        out: list[RemoteTrack] = []
        for r in data.get("results", []) or []:
            tid = str(r.get("id", "")).strip()
            if not tid:
                continue
            audio = str(r.get("audio", "")).strip()
            out.append(RemoteTrack(
                provider=self.id,
                track_id=tid,
                title=str(r.get("name", "")).strip() or "Без названия",
                artist=str((r.get("artist") or {}).get("name", "")).strip(),
                album=str(r.get("album", {}).get("name", "")
                          if isinstance(r.get("album"), dict) else "").strip(),
                duration=float(r.get("duration", 0) or 0),
                artwork=str(r.get("image", "")).strip(),
                stream_url=audio,
                download_url=str(r.get("audiodownload", "") or audio).strip(),
                page_url=str(r.get("shorturl", "")).strip(),
                ext="mp3",
            ))
        return out


_PROVIDERS: dict[str, type[MusicProvider]] = {
    "itunes": ITunesProvider,
    "jamendo": JamendoProvider,
}

_SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_filename(name: str) -> str:
    name = _SAFE_NAME_RE.sub("_", name).strip(" ._")
    return (name or "track")[:120]


# ═══════════════════════ агрегатор ═══════════════════════

class MusicLibraryAPI:
    """Реестр провайдеров + кеш последних результатов + загрузка файлов."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / CONFIG_NAME
        self._lock = threading.Lock()
        self._cache: dict[str, list[RemoteTrack]] = {}
        self._providers: dict[str, MusicProvider] = {}
        self._reload_config()

    # ── конфиг ──
    def _reload_config(self) -> None:
        cfg: dict = {}
        if self.config_path.exists():
            try:
                cfg = json.loads(
                    self.config_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                cfg = {}
        self._providers = {}
        for pid, cls in _PROVIDERS.items():
            pcfg = cfg.get(pid, {}) or {}
            if pid == "jamendo":
                self._providers[pid] = JamendoProvider(
                    client_id=pcfg.get("client_id", ""),
                    enabled=pcfg.get("enabled", False))
            else:
                prov = cls()
                prov.enabled = bool(pcfg.get("enabled", True))
                self._providers[pid] = prov

    def save_config_patch(self, patch: dict) -> None:
        """Обновляет конфиг (jamendo client_id и т.п.) и перечитывает."""
        cfg: dict = {}
        if self.config_path.exists():
            try:
                cfg = json.loads(
                    self.config_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                cfg = {}
        for k, v in (patch or {}).items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._reload_config()

    # ── публичный API ──
    def status(self) -> list[dict]:
        out = []
        for pid, p in self._providers.items():
            out.append({"provider": pid, "name": p.name, "enabled":
                        getattr(p, "enabled", True), "note": p.note})
        return out

    def search(self, query: str, provider: str = "",
               limit: int = 20) -> dict:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "Пустой запрос"}
        if provider:
            provs = [self._providers.get(provider)]
            if provs[0] is None:
                return {"ok": False, "error": f"Нет провайдера «{provider}»"}
        else:
            provs = [p for p in self._providers.values()
                     if getattr(p, "enabled", True)]
        if not provs:
            return {"ok": False, "error":
                    "Внешние провайдеры отключены (music_api.json)"}
        results: list[RemoteTrack] = []
        errors: list[str] = []
        for p in provs:
            try:
                res = p.search(query, limit)
                with self._lock:
                    self._cache[p.id] = res
                results.extend(res)
            except RuntimeError as e:
                errors.append(str(e))
        return {
            "ok": bool(results) or not errors,
            "results": [t.to_dict() for t in results],
            "errors": errors,
        }

    def find(self, provider: str, track_id: str) -> RemoteTrack | None:
        """Трек из кеша последних поисков (RemoteTrack не сериализуем)."""
        with self._lock:
            for t in self._cache.get(provider, []):
                if t.track_id == track_id:
                    return t
        # Кеш пуст — пробуем живой поиск по точному ID (когда провайдер
        # умеет lookup; для itunes/jamendo поиск по id-строке тоже работает)
        p = self._providers.get(provider)
        if p is None:
            return None
        try:
            for t in p.search(track_id, 10):
                if t.track_id == track_id:
                    return t
        except RuntimeError:
            pass
        return None

    def download(self, track: RemoteTrack, dest_dir: Path) -> Path:
        """Скачивает download_url в dest_dir (атомарно, с лимитом размера)."""
        url = track.download_url or track.stream_url
        if not url:
            raise RuntimeError("У трека нет ссылки на файл "
                               "(у превью iTunes скачивание ограничено)")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = (track.artist + " - " + track.title) if track.artist \
            else track.title
        dest = dest_dir / f"{_safe_filename(base)}.{track.ext}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        received = 0
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_IMPORT_BYTES:
                        raise RuntimeError("Файл больше лимита импорта")
                    f.write(chunk)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)
        return dest


_INSTANCE: MusicLibraryAPI | None = None
_INSTANCE_LOCK = threading.Lock()


def get_library_api(data_dir: Path) -> MusicLibraryAPI:
    """Синглтон на процесс (конфиг один — data_dir один)."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = MusicLibraryAPI(data_dir)
        return _INSTANCE
