"""Тест "умной раздачи v1": HTTP Range/206 на сервере + параллельная
докачка клиента (lib/client.download_file).

Запуск: python tests/test_smart_download.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import client
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="frelay_smartdl_") as td:
        tmp = Path(td)
        server = RelayServer(tmp / "data", host_name="Хост")
        assert server.start("127.0.0.1", 8793)
        time.sleep(0.2)
        base = "http://127.0.0.1:8793"

        payload = bytes(range(256)) * 4096  # 1 МБ детерминированных данных
        blob = tmp / "blob.bin"
        blob.write_bytes(payload)
        _, fid = client.send_file(base, "Вася", blob)
        url = f"{base}/download/{fid}"

        # ── сервер: Range-семантика ──────────────────────────────────────
        st, body, hdrs = raw_get(url)
        check("без Range -> 200 целиком", st == 200 and body == payload)
        check("Accept-Ranges анонсирован", hdrs.get("Accept-Ranges") == "bytes")

        st, body, hdrs = raw_get(url, {"Range": "bytes=0-0"})
        check("bytes=0-0 -> 206 один байт", st == 206 and body == payload[:1])
        check("Content-Range верный", hdrs.get("Content-Range") == f"bytes 0-0/{len(payload)}")

        st, body, _ = raw_get(url, {"Range": "bytes=100-199"})
        check("bytes=100-199 -> кусок", st == 206 and body == payload[100:200])

        st, body, _ = raw_get(url, {"Range": "bytes=-16"})
        check("суффикс bytes=-16 -> хвост", st == 206 and body == payload[-16:])

        st, body, _ = raw_get(url, {"Range": "bytes=999999999-" if False else "bytes=99999999-"})
        check("запредельный диапазон -> 416", st == 416)

        st, body, _ = raw_get(url, {"Range": "bytes=5-3"})
        check("end<start -> 200 целиком", st == 200 and body == payload)

        st, body, _ = raw_get(url, {"Range": "bytes=100-999999"})
        check("end за пределами обрезается", st == 206 and body == payload[100:1000000])

        # ── клиент: параллельная докачка ─────────────────────────────────
        big = os.urandom(client.PARALLEL_THRESHOLD_BYTES + 1024 * 1024)  # > 8 МБ
        big_src = tmp / "big.bin"
        big_src.write_bytes(big)
        _, big_fid = client.send_file(base, "Вася", big_src)

        dest = tmp / "out" / "big.bin"
        progress_seen = []
        client.download_file(
            base, big_fid, dest, progress_cb=lambda rec, tot, el: progress_seen.append(rec)
        )
        check("большой файл скачан", dest.exists() and dest.stat().st_size == len(big))
        check("байты совпадают", dest.read_bytes() == big)
        check("параллельная ветка реально работала", len(progress_seen) > 0)

        # малый файл идёт одиночным потоком (старое поведение)
        dest2 = tmp / "out" / "blob.bin"
        client.download_file(base, fid, dest2)
        check("малый файл через простую ветку", dest2.exists() and dest2.read_bytes() == payload)

        # DM-файл тоже качается
        dm_src = tmp / "priv_src.txt"
        dm_src.write_bytes(payload[:100])
        _, dm_fid = client.send_dm_file(base, "Вася", "Петя", dm_src)
        dest3 = tmp / "out" / "priv.txt"
        client.download_dm_file(base, "Петя", dm_fid, dest3)
        check("dm-файл качается движком", dest3.exists() and dest3.read_bytes() == payload[:100])

        server.stop()

    print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
