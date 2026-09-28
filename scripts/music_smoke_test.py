"""Music smoke test (v3.3.0): MusicBot + HTTP-роуты /music/* end-to-end.

1. Логика MusicBot: скан библиотеки, стабильные ID, очередь, права,
   пауза/возобновление/seek/скип, авто-переход по ended.
2. HTTP end-to-end: RelayServer + make_handler_class на обычном
   ThreadingHTTPServer (TLS-мультиплексор не участвует — маршруты те же).
3. Range-стриминг /music/stream/<id> (206 + Content-Range).
4. Внешний API /music/api/search — только «не упасть» (сеть в тестовой
   среде может отсутствовать; ответ обязан быть корректным JSON).

Запуск:  python scripts/music_smoke_test.py
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJ = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJ)

failures: list[str] = []


def check(name: str, fn):
    try:
        result = fn()
        print(f"  OK  {name}" + (f" -> {result}" if result is not None else ""))
        return result
    except Exception:
        failures.append(name)
        print(f" FAIL {name}")
        traceback.print_exc()
        return None


def make_wav(path: Path, seconds: float = 5.0, freq: int = 440) -> None:
    """Валидный WAV: моно 8кГц 16-бит, синус."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        frames = bytearray()
        n = int(seconds * 8000)
        for i in range(n):
            v = int(12000 * (0 if (i // 800) % 2 else 1)
                    * __import__("math").sin(2 * 3.14159 * freq * i / 8000))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


# ═══════════════ HTTP-окружение ═══════════════

class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "relay_data"
        from lib.relay_server import RelayServer

        self.relay = RelayServer(self.data_dir, "TestHost")
        handler = __import__(
            "lib.server.http_api", fromlist=["make_handler_class"]
        ).make_handler_class(self.relay)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path: str, sender: str = "Guest"):
        req = urllib.request.Request(self.base + path,
                                     headers={"X-Relay-From": sender})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def post(self, path: str, body: dict, sender: str = "Guest"):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"X-Relay-From": sender,
                     "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {"ok": False, "error": f"HTTP {e.code}"}
            return e.code, payload


def wait_sync(env: Env, seconds: float = 1.2) -> None:
    time.sleep(seconds)


# ═══════════════ тесты ═══════════════

def main() -> int:
    env = Env()
    try:
        print("== HTTP /music/* end-to-end ==")

        def t_library_empty():
            status, _, body = env.get("/music/library")
            j = json.loads(body)
            assert status == 200 and j["ok"] and j["tracks"] == []
            return "пусто, ok"

        check("GET /music/library пустая", t_library_empty)

        # Готовим файлы: 2 трека, в подпапке тоже (rglob)
        music_dir = env.data_dir / "music"
        make_wav(music_dir / "Song One.wav", 5.0)
        (music_dir / "Album").mkdir(parents=True, exist_ok=True)
        make_wav(music_dir / "Album" / "Song Two.wav", 5.0)

        def t_rescan():
            status, j = env.post("/music/rescan", {}, sender="TestHost")
            assert status == 200 and j["ok"] and j["count"] == 2, j
            return f"{j['count']} трека"

        check("POST /music/rescan находит 2 трека", t_rescan)

        tracks: dict = {}

        def t_ids():
            _, _, body = env.get("/music/library")
            j = json.loads(body)
            ids = [t["id"] for t in j["tracks"]]
            assert len(ids) == 2 and len(set(ids)) == 2, ids
            for t in j["tracks"]:
                tracks[t["filename"]] = t
            assert tracks["Song One.wav"]["title"] in ("Song One",
                                                        "Song One.wav")
            # стабильность ID при повторном скане
            _, _, body2 = env.get("/music/library")
            ids2 = [t["id"] for t in json.loads(body2)["tracks"]]
            assert ids2 == ids
            return "ID стабильны и уникальны"

        check("стабильные ID треков", t_ids)

        id1 = tracks["Song One.wav"]["id"]
        id2 = tracks["Song Two.wav"]["id"]

        def t_queue_add_autostart():
            # Ничего не играет: гость добавляет трек — стартует сразу
            status, j = env.post("/music/queue/add",
                                 {"track_id": id1}, sender="Guest")
            assert status == 200 and j["ok"] and j["started"], j
            return "автостарт"

        check("POST /music/queue/add автостарт (open_dj)", t_queue_add_autostart)

        def t_sync_playing():
            _, _, body = env.get("/music/sync")
            j = json.loads(body)
            assert j["ok"] and j["playing"] and j["track"]["id"] == id1, j
            assert "server_time" in j and "state_version" in j
            return f"pos={j['position']}"

        check("GET /music/sync: играет id1", t_sync_playing)

        def t_seek():
            status, j = env.post("/music/seek", {"position": 3.0},
                                 sender="Guest")
            assert status == 200 and j["ok"], j
            wait_sync(env, 0.8)
            _, _, body = env.get("/music/sync")
            j2 = json.loads(body)
            assert abs(j2["position"] - 3.8) < 1.0, j2["position"]
            return f"pos≈{j2['position']}"

        check("POST /music/seek + дрейф от server_time", t_seek)

        def t_pause_resume():
            _, j = env.post("/music/pause", {}, sender="TestHost")
            assert j["ok"]
            _, _, body = env.get("/music/sync")
            pos1 = json.loads(body)["position"]
            wait_sync(env, 0.7)
            _, _, body = env.get("/music/sync")
            j2 = json.loads(body)
            assert not j2["playing"] and abs(j2["position"] - pos1) < 0.2
            # возобновить без track_id — с той же позиции
            _, j3 = env.post("/music/play", {}, sender="TestHost")
            assert j3["ok"], j3
            _, _, body = env.get("/music/sync")
            j4 = json.loads(body)
            assert j4["playing"] and j4["position"] >= pos1 - 0.3
            return "пауза/резюм ок"

        check("POST /music/pause + /music/play (резюм)", t_pause_resume)

        def t_queue_second():
            status, j = env.post("/music/queue/add", {"track_id": id2},
                                 sender="Guest")
            assert status == 200 and j["ok"] and not j["started"], j
            assert j["position"] == 1
            _, _, body = env.get("/music/sync")
            q = json.loads(body)["queue"]
            assert len(q) == 1 and q[0]["by"] == "Guest"
            return "в очереди 1"

        check("очередь: второй трек ждёт", t_queue_second)

        def t_ended_next():
            env.post("/music/seek", {"position": 4.6}, sender="TestHost")
            status, j = env.post("/music/ended", {}, sender="Guest")
            assert status == 200 and j["ok"], j
            _, _, body = env.get("/music/sync")
            j2 = json.loads(body)
            assert j2["playing"] and j2["track"]["id"] == id2, j2
            return "переключило на id2"

        check("POST /music/ended -> следующий из очереди", t_ended_next)

        def t_settings_guard():
            # гость не может менять настройки
            status, j = env.post("/music/settings", {"open_dj": False},
                                 sender="Guest")
            assert status == 400 and not j["ok"], (status, j)
            # хост может
            status, j = env.post("/music/settings", {"open_dj": False},
                                 sender="TestHost")
            assert status == 200 and j["ok"] and j["open_dj"] is False, j
            # теперь гостю нельзя play, но можно в очередь
            status, j = env.post("/music/play", {}, sender="Guest")
            assert status == 400 and "DJ" in j["error"], (status, j)
            status, j = env.post("/music/queue/add", {"track_id": id1},
                                 sender="Guest")
            assert status == 200 and j["ok"], j
            # вернуть открытый режим
            status, j = env.post("/music/settings", {"open_dj": True},
                                 sender="TestHost")
            assert status == 200 and j["open_dj"] is True
            return "права работают"

        check("open_dj off: только хост управляет", t_settings_guard)

        def t_stream_range():
            _, _, body = env.get(f"/music/stream/{id1}")
            full_len = len(body)
            assert full_len > 1000
            # Range-запрос
            req = urllib.request.Request(
                env.base + f"/music/stream/{id1}",
                headers={"Range": "bytes=0-99", "X-Relay-From": "Guest"})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 206, r.status
                chunk = r.read()
                assert len(chunk) == 100
                cr = r.headers.get("Content-Range", "")
                assert cr.startswith(f"bytes 0-99/{full_len}"), cr
            # неизвестный трек — 404
            code404, _, _ = env.get("/music/stream/caffeee0000000000")
            assert code404 == 404, code404
            return f"{full_len} байт, 206 ок"

        check("GET /music/stream/<id> + Range 206", t_stream_range)

        def t_api_search_graceful():
            try:
                status, j = env.post("/music/api/search",
                                     {"query": "daft punk"},
                                     sender="TestHost")
                # Сеть есть и iTunes ответил — ок; сети нет — корректная
                # ошибка. Главное: ответ обязателен и валиден.
                assert isinstance(j.get("ok"), bool), j
                return "ok" if j["ok"] else f"мягко: {str(j.get('error'))[:60]}"
            except urllib.error.HTTPError as e:
                # 400 с понятной ошибкой — тоже допустимо (сети нет)
                assert e.code in (400, 503)
                return "мягкая ошибка сети"

        check("POST /music/api/search не падает без сети", t_api_search_graceful)

        def t_legacy_state():
            # после skip всех треков state -> playing False
            env.post("/music/skip", {}, sender="TestHost")
            env.post("/music/skip", {}, sender="TestHost")
            _, _, body = env.get("/music/sync")
            j = json.loads(body)
            assert j["ok"] and not j["playing"] and j["track"] is None, j
            assert env.relay.get_music_sync_state() is None
            return "легаси-метод ок"

        check("скип до конца очереди -> остановка", t_legacy_state)

        # ═══════════════ Unit: логика бота ═══════════════
        print("== MusicBot unit ==")

        def t_bot_position_math():
            from lib.bots.music import MusicBot

            bot = MusicBot(Path(env.tmp.name) / "unit_data", host_name="H")
            assert bot.music_dir.name == "music"
            bot.api_settings("H", open_dj=True)
            lib_ids = list(bot.library.keys())
            assert len(lib_ids) == 0  # другая data_dir — пусто
            return "папка по умолчанию = data/music"

        check("MusicBot: папка по умолчанию", t_bot_position_math)

        def t_bot_stable_id():
            from lib.bots.music import _stable_track_id

            a = _stable_track_id("Album\\Song.wav")
            b = _stable_track_id("Album/Song.wav")
            assert a == b and len(a) == 16
            return a

        check("MusicBot: ID не зависит от слэшей", t_bot_stable_id)

    finally:
        env.close()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}: {failures}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
