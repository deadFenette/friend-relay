"""P3 smoke test: домен без storage, сеть без Qt-вьюх.

1. ast-аудит lib/domain: запрещены импорты lib.storage / http.* / PySide6 / qt_app.
2. lib/storage.py больше не содержит журнальных функций (только настройки).
3. Функциональный round-trip log_io: save_dm -> load_dm_history ->
   get_dm_conversations в тестовом каталоге.
4. EventStore round-trip: конструирование + send_dm + чтение истории.
5. lib/net_utils.get_local_ips(): возвращает список строк без 127.*.
"""
from __future__ import annotations

import ast
import sys
import tempfile
import traceback
from pathlib import Path

PROJ = str(Path(__file__).resolve().parent.parent)  # корень проекта (v1.9.9+: раньше был мёртвый захардкоженный путь старой распаковки)
sys.path.insert(0, PROJ)

failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        result = fn()
        print(f"  OK  {name}" + (f" -> {result}" if result is not None else ""))
    except Exception:
        failures.append(name)
        print(f" FAIL {name}")
        traceback.print_exc()


FORBIDDEN_IN_DOMAIN = ("lib.storage", "http", "PySide6", "qt_app")


def audit_domain() -> str:
    root = Path(PROJ, "lib", "domain")
    bad = []
    for p in root.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IN_DOMAIN):
                    bad.append(f"{p.name}:{node.lineno} imports {m}")
    assert not bad, "запрещённые импорты в lib/domain:\n" + "\n".join(bad)
    return "domain imports clean"


def storage_is_settings_only() -> str:
    import lib.storage

    public = {n for n in dir(lib.storage) if not n.startswith("_")}
    expected = {"annotations", "json", "Any", "DATA_DIR", "SETTINGS_FILE",
                "load_settings", "save_settings"}
    extra = public - expected
    assert not extra, f"в storage остались лишние символы: {extra}"
    return "storage = settings only"


def log_io_roundtrip() -> str:
    from lib.domain import log_io

    with tempfile.TemporaryDirectory() as td:
        dm_dir = Path(td)
        dm_path = dm_dir / "alice_bob.jsonl"
        log_io.save_dm(dm_path, "alice", "bob", "привет", seq=1)
        log_io.save_dm(dm_path, "bob", "alice", "привет-привет", seq=2,
                       encrypted=True, iv="aGVsbG8=")
        hist = log_io.load_dm_history(dm_path)
        assert [h["seq"] for h in hist] == [1, 2]
        assert hist[1].get("encrypted") is True and hist[1]["iv"] == "aGVsbG8="
        convs = log_io.get_dm_conversations(dm_dir, "alice")
        assert len(convs) == 1 and convs[0]["user"] == "bob"
        assert convs[0]["last_message"] == "привет-привет"
        old = log_io.load_history_before(dm_path, before_seq=2, count=10)
        assert [o["seq"] for o in old] == [1]
    return "save/load/conv/before all match"


def event_store_roundtrip() -> str:
    from lib.domain import EventStore

    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td))
        ev = store.send_dm("alice", "bob", "проверка связи")
        assert ev["kind"] == "dm" and ev["text"] == "проверка связи"
        assert ev["seq"] >= 1
        convs = store.get_dm_conversations("alice")
        assert len(convs) == 1 and convs[0]["user"] == "bob"
        hist = store.get_dm_history("alice", "bob")
        assert len(hist) == 1
    return "EventStore works on new log_io"


def local_ips_sane() -> str:
    from lib.net_utils import get_local_ips

    ips = get_local_ips()
    assert isinstance(ips, list) and all(isinstance(i, str) for i in ips)
    assert not [i for i in ips if i.startswith("127.")], "loopback не должен попадать"
    return f"{len(ips)} ip(s): {ips[:3]}"


check("lib/domain has no storage/http/Qt imports", audit_domain)
check("lib/storage.py is settings-only", storage_is_settings_only)
check("log_io round-trip", log_io_roundtrip)
check("EventStore round-trip", event_store_roundtrip)
check("get_local_ips sanity", local_ips_sane)

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL P3 SMOKE CHECKS PASSED")
