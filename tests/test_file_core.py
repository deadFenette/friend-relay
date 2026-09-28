"""Тест "файлового ядра v2": стриминг upload без RAM, sha256-конвейер,
ETag/If-Range, реестр владельцев /file/have.

Запуск: python tests/test_file_core.py"""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import client
from lib.constants import HEADER_FILENAME, HEADER_FROM, HEADER_SHA256
from lib.relay_server import RelayServer
from lib.util import header_encode

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def raw_get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "test", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def raw_post_file(base: str, port: int, path: str, payload: bytes,
                  sender: str, filename: str, sha: str = "") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {
        HEADER_FROM: header_encode(sender),
        HEADER_FILENAME: header_encode(filename),
        HEADER_SHA256: sha,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(payload)),
    }
    conn.putrequest("POST", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    conn.send(payload)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="frelay_filecore_") as td:
        tmp = Path(td)
        server = RelayServer(tmp / "data", host_name="Хост")
        assert server.start("127.0.0.1", 8795)
        time.sleep(0.2)
        base = "http://127.0.0.1:8795"
        port = 8795

        # ── 1. стриминг upload: хеш в ответе/событии, байты на диске ─────
        payload = bytes(range(256)) * 8192  # 2 МБ детерминированных данных
        blob = tmp / "blob.bin"
        blob.write_bytes(payload)
        expected_sha = hashlib.sha256(payload).hexdigest()

        seq, fid = client.send_file(base, "Вася", blob)
        ev = server.get_file_event(fid)
        check("upload вернул file_id", bool(fid))
        check("событие хранит sha256", ev.get("sha256") == expected_sha)
        check("байты на диске побайтово равны",
              server.file_bytes_path(fid).read_bytes() == payload)
        check("размер в событии верный", ev.get("size") == len(payload))

        # ── 2. битый хеш от клиента -> 400, файл не регистрируется ───────
        bogus_sha = "0" * 64
        st, body = raw_post_file(base, port, "/send_file", payload[:1024],
                                 "Вася", "evil.bin", sha=bogus_sha)
        check("несовпадение хеша -> 400", st == 400)
        st, body = raw_post_file(base, port, "/send_file", payload[:1024],
                                 "Вася", "ok.bin", sha=hashlib.sha256(payload[:1024]).hexdigest())
        check("совпадающий хеш -> 200", st == 200)
        check("в ответе есть file_id", bool(json.loads(body)["file_id"]))
        check("в temp-каталоге не осталось .part-мусора",
              not list((tmp / "data" / "files").glob(".incoming-*")))

        # ── 3. ETag на /download == sha256 ───────────────────────────────
        url = f"{base}/download/{fid}"
        st, body, hdrs = raw_get(url)
        check("200 целиком и байты равны", st == 200 and body == payload)
        check("ETag == sha256", hdrs.get("ETag") == f'"{expected_sha}"')

        # ── 4. If-Range: правильная ревизия -> 206, чужая -> 200 ─────────
        st, body, hdrs = raw_get(url, {"Range": "bytes=0-99",
                                       "If-Range": f'"{expected_sha}"'})
        check("If-Range совпал -> 206 кусок", st == 206 and body == payload[:100])
        st, body, hdrs = raw_get(url, {"Range": "bytes=0-99",
                                       "If-Range": '"deadbeef"'})
        check("If-Range чужой -> 200 целиком", st == 200 and body == payload)

        # ── 5. реестр владельцев /file/have ──────────────────────────────
        st, body = raw_post_file(base, port, "/send_file", b"tiny", "Вася", "t.bin")
        tiny_fid = json.loads(body)["file_id"]
        req = urllib.request.Request(
            f"{base}/file/have",
            data=json.dumps({"file_id": tiny_fid}).encode(),
            headers={"User-Agent": "test", "Content-Type": "application/json",
                     HEADER_FROM: header_encode("Петя")},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            have = json.loads(resp.read())
        check("report_have вернул holders", have.get("ok") and have["holders"] == ["Петя"])

        req = urllib.request.Request(
            f"{base}/file/have",
            data=json.dumps({"file_id": "no-such-file"}).encode(),
            headers={"User-Agent": "test", "Content-Type": "application/json",
                     HEADER_FROM: header_encode("Петя")},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            st = 200
        except urllib.error.HTTPError as e:
            st = e.code
        check("have для неизвестного файла -> 404", st == 404)

        files_list = client.list_files(base, "Вася")
        tiny_ev = next(f for f in files_list if f["file_id"] == tiny_fid)
        check("/files отдаёт holders", tiny_ev.get("holders") == ["Петя"])

        # клиент сам репортит после скачивания (name= в download_file)
        dest = tmp / "out" / "blob.bin"
        client.download_file(base, fid, dest, name="Оля")
        files_list = client.list_files(base, "Вася")
        blob_ev = next(f for f in files_list if f["file_id"] == fid)
        check("download_file сам зарегистрировал владельца",
              set(blob_ev.get("holders", [])) >= {"Оля"})

        # ── 6. параллельная докачка >8 МБ с verify хеша ──────────────────
        big = bytes((i * 7 + 13) % 256 for i in range(12 * 1024 * 1024))
        big_path = tmp / "big.bin"
        big_path.write_bytes(big)
        _, big_fid = client.send_file(base, "Вася", big_path)
        big_dest = tmp / "out" / "big.bin"
        client.download_file(base, big_fid, big_dest)
        check("большой файл скачан побайтово равным", big_dest.read_bytes() == big)

        # ── 7. DM-файл: sha256 в событии + права доступа ─────────────────
        _, dm_fid = client.send_dm_file(base, "Вася", "Петя", blob)
        dm_dest = tmp / "out" / "dm.bin"
        client.download_dm_file(base, "Петя", dm_fid, dm_dest)
        check("dm-файл скачан побайтово равным", dm_dest.read_bytes() == payload)
        dm_ev = server.get_dm_file_event(dm_fid)
        check("dm-событие хранит sha256", dm_ev.get("sha256") == expected_sha)

        server.stop()

        # ── 8. реестр владельцев переживает рестарт хоста ────────────────
        server2 = RelayServer(tmp / "data", host_name="Хост")
        server2.start("127.0.0.1", 8796)
        time.sleep(0.2)
        files_list = server2.list_all_files()
        tiny_ev2 = next((f for f in files_list if f["file_id"] == tiny_fid), None)
        check("holders пережили рестарт хоста",
              tiny_ev2 is not None and tiny_ev2.get("holders") == ["Петя"])
        server2.stop()

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
