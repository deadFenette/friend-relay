#!/usr/bin/env python3
"""Релиз v3.6.5: бамп версии + README + сборка full-zip с проверками.

Фикс «веб-голосовой мост»: hb-echo и keepalive уезжали в браузер
БИНАРНЫМ кадром (json.dumps().encode()), браузер не мог разобрать
JSON; при активном аудио конкурентные send() из двух насос-тасок
кидали ConcurrencyError и echo молча терялся. Теперь: общий лок на
отправку + все контрольные кадры моста шлются str (текстовый кадр).

Урок v3.6.3/v3.6.4: assert'ы по исходнику проверяют ТОЛЬКО кодовые
строки (комментарии намеренно упоминают старые шаблоны).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD = ROOT.parent.parent / "download"
VERSION = "3.6.5"

NOTES = """v3.6.5 — веб-голос: фикс моста /voice/ws (hb-echo терялся).

- bridge.py отвечал на heartbeat браузера через ws.send(json.dumps(...).encode()).
  .encode делал БИНАРНЫЙ WS-кадр: браузер получал JSON как «аудио» и
  не мог распарсить контроль. Теперь hb-echo и keepalive шлются str —
  текстовым кадром, как и все control-сообщения микшера.
- При активном аудио два насоса шлют в один websocket одновременно;
  websockets 14+ запрещает конкурентные send() (ConcurrencyError) —
  hb-echo молча терялся ещё чаще. Отправка обеих насос-тасок идёт через
  общий asyncio.Lock — кадры не теряются и не «перемешиваются».
- Регрессия: tests/test_bridge_hb_v365.py — echo ТЕКСТОМ и без аудио,
  и при потоке PCM; mute/мусор/алиас ping; проверки по исходнику моста.
  Живая проверка веба 16/16: статика, /voice/info, рукопожатие версий
  spk_all «v», roster, mute, /server/stats, /events.
Менялись: voice/bridge.py; +tests/test_bridge_hb_v365.py.

"""

README_BLOCK = """>>> v3.6.5 — веб-голос: фикс моста /voice/ws.
>>>   • hb-echo и keepalive моста шлются ТЕКСТОМ (раньше .encode
>>>     делал бинарный кадр — браузер не мог разобрать JSON);
>>>   • общий лок на отправку в websocket: при активном аудио
>>>     конкурентные send() из двух насосов кидали ConcurrencyError
>>>     и hb-echo молча терялся;
>>>   • +tests/test_bridge_hb_v365.py (echo при аудио-потоке и без).
>>> Изменено: voice/bridge.py.

"""

CHECKS: list[tuple[str, Path, list[str]]] = [
    ("bridge: общий лок создаётся в _handler",
     ROOT / "voice/bridge.py",
     ["send_lock = asyncio.Lock()"]),
    ("bridge: обе насос-таски принимают send_lock",
     ROOT / "voice/bridge.py",
     ["self._pump_mixer_to_ws(reader, ws, transcode, send_lock)",
      "self._pump_ws_to_mixer(ws, writer, state, transcode, send_lock)"]),
    ("bridge: ws_send под локом в обоих насосах",
     ROOT / "voice/bridge.py", ["__WS_SEND_UNDER_LOCK__"]),
    ("bridge: hb-echo и keepalive шлются str (без .encode в ws)",
     ROOT / "voice/bridge.py", ["__NO_ENCODE_IN_WS__"]),
    ("bridge: keepalive-hb текстом",
     ROOT / "voice/bridge.py",
     ['{"t": "hb", "ts": int(time.time())}))']),
    ("bridge: hb-echo текстом",
     ROOT / "voice/bridge.py",
     ['"ok": True}))']),
    ("регресс v3.6.4: потолок адаптива веба 18",
     ROOT / "web_client/voice.js",
     ["Math.min(18, VC.jitterTarget + 1)", "hostVersion"]),
    ("регресс v3.6.4: WAN-константы конфига",
     ROOT / "voice/config.py",
     ["MIXER_PREBUF_FRAMES = 5", "CLIENT_PREBUF_MAX_FRAMES = 15"]),
]


def bump() -> None:
    vj = ROOT / "version.json"
    data = json.loads(vj.read_text(encoding="utf-8"))
    if data.get("version") == VERSION \
            and (data.get("notes") or "").startswith(NOTES[:40]):
        pass  # уже забамплено (перезапуск после падения verify)
    else:
        assert data.get("version") == "3.6.4", \
            f"ожидали 3.6.4, найдено {data.get('version')}"
        data["version"] = VERSION
        data["notes"] = NOTES + (data.get("notes") or "")
        vj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if ">>> v3.6.5" not in text:
        marker = ">>> v3.6.4"
        idx = text.index(marker)
        text = text[:idx] + README_BLOCK + text[idx:]
        readme.write_text(text, encoding="utf-8")
    print("version.json + README обновлены")


def verify() -> None:
    bridge = (ROOT / "voice/bridge.py").read_text(encoding="utf-8")
    n_lock = bridge.count("async with send_lock:")
    n_def = bridge.count("async def ws_send(data):")
    bad_encode = [ln for ln in bridge.splitlines()
                  if ("ws.send" in ln or "ws_send(" in ln)
                  and '.encode("utf-8")' in ln]
    for name, path, needles in CHECKS:
        body = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle == "__WS_SEND_UNDER_LOCK__":
                assert n_def == 2 and n_lock >= 2, (
                    f"ws_send определён {n_def} раз, async with {n_lock} раз — "
                    "лок должен использоваться в ОБОИХ насосах")
            elif needle == "__NO_ENCODE_IN_WS__":
                assert not bad_encode, (
                    "ws.send с .encode внутри: " + "; ".join(bad_encode[:2]))
            else:
                assert needle in body, f"{name}: не найдено «{needle}»"
    assert json.loads((ROOT / "version.json").read_text(
        encoding="utf-8"))["version"] == VERSION
    assert ">>> v3.6.5" in (ROOT / "README.md").read_text(encoding="utf-8")
    for bat in ("RUN.bat", "RUN_SERVER.bat", "build_exe.bat"):
        raw = (ROOT / bat).read_bytes()
        assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), \
            f"{bat}: не CRLF"
    print(f"Проверки исходников: OK ({len(CHECKS)} групп)")


def build_zip() -> None:
    INCLUDE_DIRS = ("lib", "voice", "qt_app", "web_client", "screen", "tests",
                    "scripts", "docs", "legacy")
    INCLUDE_FILES = (
        "README.md", "ARCHITECTURE.md",
        "RUN.bat", "RUN_LEGACY.bat", "RUN_TELEMETRY.bat",
        "RUN_SERVER.bat", "RUN_SERVER_GUI.bat",
        "build_exe.bat", "build_legacy_exe.bat",
        "main_qt.py", "server_gui.py", "server_main.py",
        "telemetry_viewer.py",
        "pyproject.toml", "requirements.txt",
        "version.json", ".gitignore",
    )
    EXCLUDE_PARTS = {"__pycache__", ".git", "tool-results", "download",
                     "upload", ".venv", "build", "dist", "node_modules",
                     "skills", "relay_data"}
    EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log")

    def wanted(p: Path) -> bool:
        return not (set(p.parts) & EXCLUDE_PARTS or p.suffix in EXCLUDE_SUFFIX)

    files: list[Path] = []
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if base.is_dir():
            files.extend(p for p in base.rglob("*")
                         if p.is_file() and wanted(p))
    files.extend(ROOT / f for f in INCLUDE_FILES if (ROOT / f).is_file())
    files = sorted(set(files))

    out = DOWNLOAD / f"friend_relay_v{VERSION}_full.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for p in files:
            z.write(p, f"friend_relay_v{VERSION}/"
                    + p.relative_to(ROOT).as_posix())
    # Контроль содержимого
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert f"friend_relay_v{VERSION}/version.json" in names
        assert f"friend_relay_v{VERSION}/tests/test_bridge_hb_v365.py" in names
        assert f"friend_relay_v{VERSION}/voice/bridge.py" in names
        vj = json.loads(z.read(f"friend_relay_v{VERSION}/version.json")
                        .decode("utf-8"))
        assert vj["version"] == VERSION
    print(f"ZIP: {out} ({len(files)} файлов, {out.stat().st_size // 1024} КиБ)")


def main() -> None:
    bump()
    verify()
    build_zip()


if __name__ == "__main__":
    main()
