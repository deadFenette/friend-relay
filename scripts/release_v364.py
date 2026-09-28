#!/usr/bin/env python3
"""Релиз v3.6.4: бамп версии + README + сборка full-zip с проверками.

Фикс «голос через ZeroTier — бурундук/вообще не слышно»:
  1. sendall больше не в аудио-колбэке (очередь + поток-отправитель);
  2. джиттер-буферы под WAN (микшер 100/300/100мс, клиент — адаптив);
  3. диагностика несовпадения версий (spk_all «v», pong «odd»).

Урок v3.6.3: assert'ы по исходнику не должны падать на собственные
комментарии — проверяем ТОЛЬКО кодовые строки (комментарии намеренно
упоминают старые шаблоны).
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD = ROOT.parent.parent / "download"
VERSION = "3.6.4"

NOTES = """v3.6.4 — голос через ZeroTier/VPN: «бурундук» и «вообще не слышно».

- Отправка голоса больше НЕ в аудио-колбэке PortAudio: через ZeroTier
  буфер отправки TCP наполнялся и sendall замерял на сотни мс прямо в
  колбэке — захват вставал, у собеседника тишина. Теперь кадры кладутся
  в ограниченную очередь (переполнение теряет самые старые), отправку
  делает отдельный поток, control-кадры (ping/кодек) идут приоритетно.
- Джиттер-буферы выросли до масштаба TCP-затыков ZeroTier: у микшера
  пребуфер 100мс (было 60), лимит 300мс (было 160), PLC до 100мс; у
  Qt-клиента пребуфер плейаута стал АДАПТИВНЫМ: затык TCP + всплеск
  кадров поднимает запас до 300мс, стабильные 10с плавно возвращают к
  80мс. Веб-клиент: потолок адаптива 240→360мс.
- Диагностика несовпадения версий: хост присылает свою версию (spk_all
  «v») — клиент показывает предупреждение «у хоста vX, у тебя vY»;
  кадры не того размера (старая версия) считаются микшером и приходят
  клиенту в pong («odd») — строка качества пишет «твои кадры не
  принимаются». Раньше это была немая тишина или «бурундук».
- Веб-клиент показывает версию хоста в строке статуса голоса.
Менялись: voice/config.py, voice/client/engine.py,
voice/client/playback.py, voice/mixer.py, voice/__init__.py,
web_client/voice.js, qt_app/widgets/voice_channel_dialog.py;
+tests/test_voice_wan_v364.py.

"""

README_BLOCK = """>>> v3.6.4 — голос через ZeroTier/VPN: «бурундук»/тишина починены.
>>>   • отправка голоса больше не в аудио-колбэке: через ZeroTier
>>>     sendall замерял в колбэке (буфер TCP полон) — у собеседника
>>>     тишина; теперь очередь + отдельный поток-отправитель,
>>>     control-кадры приоритетно, переполнение теряет старые кадры;
>>>   • джиттер-буферы под WAN: у микшера пребуфер 100мс / лимит 300мс /
>>>     PLC 100мс; у Qt-клиента пребуфер АДАПТИВНЫЙ (80→300мс при
>>>     затыках, возврат при стабильности); веб-клиент: потолок 360мс;
>>>   • диагностика версий: хост шлёт свою версию (spk_all «v»), клиент
>>>     предупреждает о несовпадении («бурундук»/тишина между разными
>>>     версиями); кадры не того размера видны в строке качества.
>>>   Изменено: voice/, web_client/voice.js,
>>>   qt_app/widgets/voice_channel_dialog.py; +tests/test_voice_wan_v364.py.

"""

CHECKS: list[tuple[str, Path, list[str]]] = [
    ("engine: колбэк шлёт только через очередь",
     ROOT / "voice/client/engine.py",
     ["self._enqueue_frame(make_frame(flags, payload))",
      "def _sender_loop", "def _drain_send_queue",
      "self._sender_alive = True"]),
    ("engine: в аудио-колбэке нет ни одного sendall",
     ROOT / "voice/client/engine.py", ["__NO_SENDALL_IN_CB__"]),
    ("engine: sendall только в connect(auth) и drain(ctl+аудио) — 3 раза",
     ROOT / "voice/client/engine.py", ["__SENDALL_COUNT__"]),
    ("playback: адаптив (starved/потолок/сжатие)",
     ROOT / "voice/client/playback.py",
     ["self._starved = False", "self._max_prebuf",
      "and len(self.q) > self._target + 3"]),
    ("config: WAN-константы",
     ROOT / "voice/config.py",
     ["MIXER_PREBUF_FRAMES = 5", "MIXER_JITTER_LIMIT = 15",
      "MIXER_PLC_MAX_FRAMES = 5",
      "CLIENT_PREBUF_MAX_FRAMES = 15",
      "SEND_QUEUE_LIMIT_FRAMES = 150"]),
    ("mixer: odd-кадры + версия хоста",
     ROOT / "voice/mixer.py",
     ['"odd": client.odd_frames', '"v": self._app_version',
      "client.odd_frames += 1"]),
    ("voice/__init__: app_version ДО под-импортов (без circular import)",
     ROOT / "voice/__init__.py", ["__APPVER_POS__"]),
    ("web voice.js: потолок адаптива 18 + версия хоста",
     ROOT / "web_client/voice.js",
     ["Math.min(18, VC.jitterTarget + 1)", "hostVersion"]),
    ("диалог: предупреждение о версиях",
     ROOT / "qt_app/widgets/voice_channel_dialog.py",
     ["version_warn_sig", "self._version_label",
      "on_version_mismatch"]),
]


def bump() -> None:
    vj = ROOT / "version.json"
    data = json.loads(vj.read_text(encoding="utf-8"))
    if data.get("version") == "3.6.4" \
            and (data.get("notes") or "").startswith(NOTES[:40]):
        pass  # уже забамплено (перезапуск после падения verify)
    else:
        assert data.get("version") == "3.6.3", \
            f"ожидали 3.6.3, найдено {data.get('version')}"
        data["version"] = VERSION
        data["notes"] = NOTES + (data.get("notes") or "")
        vj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if ">>> v3.6.4" not in text:
        marker = ">>> v3.6.3"
        idx = text.index(marker)
        text = text[:idx] + README_BLOCK + text[idx:]
        readme.write_text(text, encoding="utf-8")
    print("version.json + README обновлены")


def verify() -> None:
    engine = (ROOT / "voice/client/engine.py").read_text(encoding="utf-8")
    n_sendall = engine.count(".sendall(")
    # Тело аудио-колбэка: от его def до следующего def того же уровня
    cb_start = engine.index("def _audio_in_callback")
    cb_end = engine.index("def _update_local_speaking")
    callback_body = engine[cb_start:cb_end]
    init = (ROOT / "voice/__init__.py").read_text(encoding="utf-8")
    pos_def = init.index("def app_version")
    pos_import = init.index("from voice.client import VoiceClient")
    for name, path, needles in CHECKS:
        body = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle == "__SENDALL_COUNT__":
                assert n_sendall == 3, (
                    f"sendall в engine.py {n_sendall} раз, ожидалось 3 "
                    "(auth в connect + ctl-батч + аудио-батч в drain)")
            elif needle == "__NO_SENDALL_IN_CB__":
                assert ".sendall(" not in callback_body, (
                    "аудио-колбэк по-прежнему зовёт sendall напрямую!")
            elif needle == "__APPVER_POS__":
                assert pos_def < pos_import, "app_version должен быть ДО импортов"
            else:
                assert needle in body, f"{name}: не найдено «{needle}»"
    assert json.loads((ROOT / "version.json").read_text(
        encoding="utf-8"))["version"] == VERSION
    assert ">>> v3.6.4" in (ROOT / "README.md").read_text(encoding="utf-8")
    # .bat — CRLF (после наших правок .bat не трогали, но архив проверяем)
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
        assert f"friend_relay_v{VERSION}/tests/test_voice_wan_v364.py" in names
        assert f"friend_relay_v{VERSION}/voice/client/engine.py" in names
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
