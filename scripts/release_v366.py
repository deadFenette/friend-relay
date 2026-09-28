#!/usr/bin/env python3
"""Релиз v3.6.6: бамп версии + README + сборка full-zip с проверками.

Два изменения:
1. «ГОЛОС НАСМЕРТЬ» — последний блокирующий sendall голосового стека:
   mixer._tick_once звал c.flush() → conn.sendall() ПРЯМО в потоке такта.
   У одного клиента с замиравшим TCP (ZeroTier-ретрансмиты) sendall вставал
   на сотни мс — такт микшера стоял ВМЕСТЕ с ним: тишина у ВСЕХ. Теперь у
   каждого клиента свой поток-отправитель; flush() только будит событие.
2. «R4» — пинг/потери в строке качества ВЕБ-клиента (в Qt есть с v3.5.7):
   мост проксирует {t:ping} микшеру, pong (in/out/odd) возвращается
   браузеру текстом; voice.js считает RTT и честные потери ↑↓, красит
   строку статуса вердиктом (пороги 80/200мс, 2/8% — как в Qt).

Урок v3.6.3/v3.6.4: assert'ы по исходнику проверяют ТОЛЬКО кодовые
строки (комментарии намеренно упоминают старые шаблоны).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD = ROOT.parent.parent / "download"
VERSION = "3.6.6"

NOTES = """v3.6.6 — голос насмерть (отправка микшера вне такта) + R4: пинг/потери в вебе.

- mixer._tick_once звал c.flush() → conn.sendall() ПРЯМО в потоке такта
  микшера. У одного клиента с замиравшим TCP (чих ZeroTier, ретрансмиты,
  нулевое окно) sendall вставал на сотни мс — такт стоял ВМЕСТЕ с ним:
  ТИШИНА У ВСЕХ УЧАСТНИКОВ, хотя сеть плохая только у одного. Теперь у
  каждого клиента СВОЙ поток-отправитель: такт только кладёт кадры в
  очередь и будит событием (flush() = event.set(), не блокируется
  никогда), сетевой I/O из микс-лупа исчез полностью. Переполнение
  очереди медленного клиента гасится дропом хвоста по границе кадров.
- R4 (веб): строка качества «пинг · потери ↑↓» в веб-клиенте. Мост
  раньше считал {t:ping} алиасом heartbeat'а — до микшера он не доходил.
  Теперь мост проксирует ping микшеру (FRVC-control), pong со счётчиками
  in/out/odd возвращается браузеру текстом; voice.js шлёт ping раз в 2с,
  считает RTT (EMA 0.7/0.3) и честные потери ↑ (послали−принял микшер) /
  ↓ (отправил микшер−приняли мы), красит строку вердиктом — пороги те
  же, что в Qt: good ≤80мс/≤2%, bad >200мс/>8%. Плюс ⚠ «твои кадры не
  принимаются» при odd>0. Сброс метрик при реконнекте/отключении.
- В Qt-клиенте строка пинг/потерь уже была (v3.5.7) — без изменений.
- Тесты: +tests/test_voice_r4_v366.py — 26 проверок: flush() мгновенен
  при полном буфере и молчащем пире; живой микшер с ТРЕМЯ клиентами —
  залипший НЕ тормозит такт (второй получает 57КБ за 0.6с), очередь
  доходит после разблокировки; мост: ping→pong end-to-end со счётчиками,
  hb-echo жив; node --check + маркеры voice.js.
- Обновлён tests/test_bridge_hb_v365.py (п.7): ping→pong вместо алиаса.
Менялись: voice/mixer.py, voice/bridge.py, web_client/voice.js.

"""

README_BLOCK = """>>> v3.6.6 — голос насмерть + R4: пинг/потери в вебе.
>>>   • микшер: отправка УБРАНА из потока такта — у каждого клиента свой
>>>     поток-отправитель; один залипший клиент (ZeroTier) больше НЕ
>>>     даёт тишину у всех;
>>>   • веб: в строке статуса голоса — пинг и потери ↑↓ (как в Qt),
>>>     цвет по качеству, ⚠ при несовместимых кадрах;
>>>   • мост проксирует ping браузера микшеру → честные счётчики.
>>> Изменено: voice/mixer.py, voice/bridge.py, web_client/voice.js.

"""

CHECKS: list[tuple[str, Path, list[str]]] = [
    ("mixer: у клиента есть поток-отправитель",
     ROOT / "voice/mixer.py",
     ["def start_sender", "def _sender_loop", "def stop_sender"]),
    ("mixer: flush() только будит событие — sendall из такта исчез",
     ROOT / "voice/mixer.py",
     ["def flush(self) -> None:",
      "self._out_event.set()"]),
    ("mixer: sendall существует РОВНО в потоке-отправителе",
     ROOT / "voice/mixer.py", ["__SENDALL_ONLY_IN_SENDER__"]),
    ("mixer: enqueue будит отправителя",
     ROOT / "voice/mixer.py",
     ["self._trim_send_buf()\n        self._out_event.set()"]),
    ("mixer: recv-луп гасит отправителя при уходе клиента",
     ROOT / "voice/mixer.py",
     ["client.stop_sender()"]),
    ("mixer: stop() гасит отправители всех клиентов",
     ROOT / "voice/mixer.py",
     ["c.stop_sender()"]),
    ("регресс v3.6.4: WAN-константы конфига",
     ROOT / "voice/config.py",
     ["MIXER_PREBUF_FRAMES = 5", "CLIENT_PREBUF_MAX_FRAMES = 15"]),
    ("регресс v3.6.5: hb-echo текстом и лок",
     ROOT / "voice/bridge.py",
     ['{"t": "hb", "ts": int(time.time())}))', '"ok": True}))',
      "send_lock = asyncio.Lock()"]),
    ("bridge: ping проксируется микшеру FRVC-control кадром",
     ROOT / "voice/bridge.py",
     ['cmd.get("t") == "ping"',
      'FLAG_CONTROL, msg.encode("utf-8"))']),
    ("voice.js: ping-таймер 2с и обработчик pong",
     ROOT / "web_client/voice.js",
     ['}, 2000);', 't:"ping", id:id', 'if (j.t === "pong"){ onPong(j);']),
    ("voice.js: потери ↑↓ и пороги 80/200/2/8",
     ROOT / "web_client/voice.js",
     ["потери ↑", "QUALITY_RTT_BAD_MS = 200", "QUALITY_LOSS_BAD_PCT = 8"]),
    ("voice.js: счётчики кадров и сброс качества",
     ROOT / "web_client/voice.js",
     ["VC.sentFrames++", "VC.recvFrames += Math.floor(i16.length / 960)",
      "resetQuality();"]),
    ("voice.js: регресс v3.6.4 — потолок адаптива 18",
     ROOT / "web_client/voice.js",
     ["Math.min(18, VC.jitterTarget + 1)", "hostVersion"]),
]


def bump() -> None:
    vj = ROOT / "version.json"
    data = json.loads(vj.read_text(encoding="utf-8"))
    if data.get("version") == VERSION \
            and (data.get("notes") or "").startswith(NOTES[:40]):
        pass  # уже забамплено (перезапуск после падения verify)
    else:
        assert data.get("version") == "3.6.5", \
            f"ожидали 3.6.5, найдено {data.get('version')}"
        data["version"] = VERSION
        data["notes"] = NOTES + (data.get("notes") or "")
        vj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if ">>> v3.6.6" not in text:
        marker = ">>> v3.6.5"
        idx = text.index(marker)
        text = text[:idx] + README_BLOCK + text[idx:]
        readme.write_text(text, encoding="utf-8")
    print("version.json + README обновлены")


def verify() -> None:
    import ast
    mixer_path = ROOT / "voice/mixer.py"
    mixer = mixer_path.read_text(encoding="utf-8")
    # sendall в mixer.py допустим только: (1) внутри _sender_loop,
    # (2) auth-ответы в потоке регистрации (AUTH_OK/FULL/AUTH_FAIL —
    # свой поток на подключение, не такт). Проверяем по AST — строки и
    # комментарии старые шаблоны не считают вызовами.
    tree = ast.parse(mixer)
    lines = mixer.splitlines()
    allowed_auth = ('AUTH_OK', 'FULL', 'AUTH_FAIL')
    sender_span = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_sender_loop":
            sender_span = (node.lineno, node.end_lineno)
    assert sender_span, "_sender_loop не найден"
    sender_src = "\n".join(lines[sender_span[0] - 1:sender_span[1]])
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "sendall":
            ln = node.lineno
            in_sender = sender_span[0] <= ln <= sender_span[1]
            line_text = lines[ln - 1]
            is_auth = any(k in line_text for k in allowed_auth)
            assert in_sender or is_auth, \
                f"sendall вне потока-отправителя, строка {ln}: {line_text.strip()}"
    for name, path, needles in CHECKS:
        body = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle == "__SENDALL_ONLY_IN_SENDER__":
                assert "self.conn.sendall(data)" in sender_src, \
                    "sendall микса должен жить в _sender_loop"
            else:
                assert needle in body, f"{name}: не найдено «{needle}»"
    assert json.loads((ROOT / "version.json").read_text(
        encoding="utf-8"))["version"] == VERSION
    assert ">>> v3.6.6" in (ROOT / "README.md").read_text(encoding="utf-8")
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
        assert f"friend_relay_v{VERSION}/tests/test_voice_r4_v366.py" in names
        assert f"friend_relay_v{VERSION}/voice/mixer.py" in names
        assert f"friend_relay_v{VERSION}/web_client/voice.js" in names
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
