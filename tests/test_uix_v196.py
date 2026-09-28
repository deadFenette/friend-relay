"""UI/UX v1.9.6 — устройства голоса, профиль, аватар.

Проверяет:
  1. Настройки: новые ключи voice_*/sound_* пишутся и читаются
  2. VoiceClient: перечисление устройств, резолв имён, device= в потоках,
     громкость вывода, шумоподавление, рестрим на живом соединении
  3. Сервер: удаление аватара пустым POST (раньше всегда 400),
     has_avatar в GET /profile
  4. Настройки уведомлений реально читаются чатом
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ─────────────────────────────────────────────────────────────────────
print("1. storage: новые настройки голоса")

import lib.storage as storage


def _with_tmp_settings(tmp: Path):
    storage.SETTINGS_FILE = tmp / "settings.json"
    storage.DATA_DIR = tmp


import tempfile

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _with_tmp_settings(tmp)
    s = storage.load_settings()
    check("voice_input_device по умолчанию пуст", s.get("voice_input_device") == "")
    check("voice_output_volume по умолчанию 1.0",
          abs(s.get("voice_output_volume", 0) - 1.0) < 1e-9)
    check("voice_noise_suppression по умолчанию True",
          s.get("voice_noise_suppression") is True)
    s.update({
        "voice_input_device": "Yeti",
        "voice_output_device": "SHP9600",
        "voice_output_volume": 0.7,
        "voice_noise_suppression": False,
        "sound_on_message": False,
        "sound_volume": 0.3,
    })
    storage.save_settings(s)
    s2 = storage.load_settings()
    check("устройства сохраняются по именам",
          s2["voice_input_device"] == "Yeti" and s2["voice_output_device"] == "SHP9600")
    check("громкость/шумодав/уведомления сохраняются",
          abs(s2["voice_output_volume"] - 0.7) < 1e-9
          and s2["voice_noise_suppression"] is False
          and s2["sound_on_message"] is False
          and abs(s2["sound_volume"] - 0.3) < 1e-9)
    # звук уведомлений читается из настроек чатом
    check("ключи уведомлений видны чату",
          "sound_on_message" in storage._DEFAULTS and "sound_volume" in storage._DEFAULTS)

# ─────────────────────────────────────────────────────────────────────
print("\n2. VoiceClient: устройства (мок sounddevice)")

# Полностью фейковый sounddevice
fake_sd = types.ModuleType("sounddevice")


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        pass


FAKE_DEVICES = [
    {"name": "System Default", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Blue Yeti", "max_input_channels": 2, "max_output_channels": 2},
    {"name": "HD Webcam Mic", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "SHP9600 Output", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "No Channels", "max_input_channels": 0, "max_output_channels": 0},
]
fake_sd.query_devices = lambda: FAKE_DEVICES
fake_sd.InputStream = _FakeStream
fake_sd.OutputStream = _FakeStream

import numpy as real_numpy  # настоящий numpy остаётся

import voice.client.engine as vc  # v2.0: дом клиента — пакет voice/
import voice.devices as _vdev  # сейм sounddevice: патчим здесь

_vdev.HAS_AUDIO = True
_vdev.sd = fake_sd
_vdev.np = real_numpy

devices = _vdev.list_audio_devices()
check("list_audio_devices разделил входы/выходы",
      len(devices["inputs"]) == 2 and len(devices["outputs"]) == 3,
      f"in={len(devices['inputs'])} out={len(devices['outputs'])}")
check("входы без выходных каналов не попали в спикеры",
      all(d["name"] != "HD Webcam Mic" for d in devices["outputs"]))
check("устройства без каналов вообще отфильтрованы",
      all(d["name"] != "No Channels" for d in devices["inputs"] + devices["outputs"]))

check("_resolve_device: точное имя",
      _vdev.resolve_device("Blue Yeti", "input") == 1)
check("_resolve_device: пустое имя = системное (None)",
      _vdev.resolve_device("", "input") is None)
check("_resolve_device: несуществующее = None (не гадаем)",
      _vdev.resolve_device("Неизвестный девайс", "input") is None)

# VoiceClient с устройствами: потоки получают device=
client = vc.VoiceClient(input_device="Blue Yeti", output_device="SHP9600 Output",
                        output_volume=1.3, noise_suppression=False)
client.on_error = lambda msg: None
ok = client._start_audio_streams()
in_kwargs = client._stream_in.kwargs
out_kwargs = client._stream_out.kwargs
check("InputStream открылся на выбранном микрофоне (device=1)",
      ok and in_kwargs.get("device") == 1, str(in_kwargs))
check("OutputStream открылся на выбранном динамике (device=3)",
      ok and out_kwargs.get("device") == 3, str(out_kwargs))
check("шумоподавление выключено передаётся", client.is_noise_suppression() is False)
client.disconnect()

# Дефолтные устройства: без device=
client2 = vc.VoiceClient()
client2.on_error = lambda msg: None
ok2 = client2._start_audio_streams()
check("пустые имена = без device= (системные)",
      ok2 and "device" not in client2._stream_in.kwargs
      and "device" not in client2._stream_out.kwargs)
client2.disconnect()

# Громкость: колбэк плейаута масштабирует сэмплы (проверяем середину:
# первые 96 сэмплов уходит на фейд-ин после тишины — отключаем его)
client3 = vc.VoiceClient(output_volume=0.5)
client3.on_error = lambda msg: None
client3._start_audio_streams()
for _ in range(4):  # пребуфер джиттер-буфера (4 × 20мс)
    client3._play_queue.append(b"\x40\x7f" * 960)  # все сэмплы = 32576
import numpy as np

outdata = np.zeros((960, 1), dtype=np.int16)
client3._out_gap = False  # без фейда
client3._audio_out_callback(outdata, 960, None, None)
check("громкость 50% масштабирует вывод (32576 -> ~16288)",
      abs(int(outdata[500, 0]) - 16288) < 40, str(outdata[500, 0]))

client3._play_queue.clear()
for _ in range(4):
    client3._play_queue.append(b"\x40\x7f" * 960)
client3._out_gap = False  # без фейда
client3.set_output_volume(1.0)
outdata2 = np.zeros((960, 1), dtype=np.int16)
client3._audio_out_callback(outdata2, 960, None, None)
# v3.5.7: рамп громкости 0.5→1.0 едет ПО кадру: старт с прежней
# громкости (без ступеньки амплитуды = без щелчка), конец на целевой.
check("громкость 100%: старт рампа с прежней 0.5 (16288)",
      int(outdata2[0, 0]) == 16288, str(outdata2[0, 0]))
check("громкость 100%: конец рампа уже на 1.0 (32576)",
      int(outdata2[-1, 0]) == 32576, str(outdata2[-1, 0]))

# Громкость >1 клиппится
client3._play_queue.clear()
for _ in range(4):
    client3._play_queue.append(b"\x40\x7f" * 960)
client3.set_output_volume(2.0)
client3._out_gap = False
outdata3 = np.zeros((960, 1), dtype=np.int16)
client3._audio_out_callback(outdata3, 960, None, None)
# v3.5.7: рамп 1.0→2.0 — клиппинг зажимает сэмплы по мере роста гейна,
# к концу кадра всё зажато на 32767 (цифрового перегруза нет).
check("громкость 200%: старт рампа на 1.0 (32576)",
      int(outdata3[0, 0]) == 32576, str(outdata3[0, 0]))
check("громкость 200% клиппится к концу рампа (32767)",
      int(outdata3[-1, 0]) == 32767, str(outdata3[-1, 0]))

# Уровень микрофона обновляется входным колбэком (нужен фейковый сокет:
# колбэк при живом соединении шлёт фреймы в сеть)
class _FakeSock:
    def sendall(self, data):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


client3._running = True
client3._sock = _FakeSock()
fake_pcm = np.full(960, 400, dtype=np.int16).reshape(-1, 1)
client3._audio_in_callback(fake_pcm, 960, None, None)
check("уровень микрофона > 0 после входного колбэка", client3.get_input_level() > 0.0)
client3._running = False
client3.disconnect()

# ─────────────────────────────────────────────────────────────────────
print("\n3. restart_streams на живом соединении")

client4 = vc.VoiceClient(input_device="Blue Yeti")
client4.on_error = lambda msg: None
client4._start_audio_streams()
first_in = client4._stream_in
client4._running = True  # имитируем живое соединение
client4.set_devices("HD Webcam Mic", "")
ok = client4.restart_streams()
check("restart_streams перезапустил входной поток",
      ok and client4._stream_in is not first_in)
check("новый поток открылся на новом устройстве",
      client4._stream_in.kwargs.get("device") == 2,
      str(client4._stream_in.kwargs))
check("выходной поток перезапустился тоже", client4._stream_out is not None)
client4._running = False
client4.disconnect()

# рестрим до подключения — просто применит настройки
client5 = vc.VoiceClient()
client5.on_error = lambda msg: None
check("restart_streams без соединения = True (применится при connect)",
      client5.restart_streams() is True and client5._stream_in is None)

# ─────────────────────────────────────────────────────────────────────
print("\n4. Сервер: удаление аватара и has_avatar")

import threading
import urllib.request

from lib.relay_server import RelayServer

HOST = "127.0.0.1"
PORT = 18796

with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    relay = RelayServer(Path(td), host_name="Host", access_key="")
    assert relay.start(HOST, PORT), "сервер не стартовал"
    base = f"http://{HOST}:{PORT}"

    try:
        PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

        def post(path: str, data: bytes, sender: str = "alice"):
            req = urllib.request.Request(
                base + path, data=data, method="POST",
                headers={"X-Relay-From": sender,
                         "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        def get(path: str):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()

        # заливаем аватар напрямую через relay
        check("set_avatar PNG работает", relay.set_avatar("alice", PNG))
        code, body = get("/profile/alice")
        # (v2.0.2) json_fast отдаёт КОМПАКТНЫЙ JSON (без пробелов после
        # двоеточий, как orjson), поэтому проверяем поле, а не байты:
        # семантика ответа не менялась, формат разделителей — не контракт.
        check("GET /profile отдаёт has_avatar=true",
              json.loads(body)["profile"]["has_avatar"] is True, body[:200])

        # удаление пустым POST (раньше — 400 «неверный размер»)
        code, body = post("/avatar", b"")
        check("POST /avatar с пустым телом = 200 (удаление)", code == 200, body[:200])
        check("файл аватара удалён", relay.get_avatar_bytes("alice") is None)
        code, body = get("/profile/alice")
        check("GET /profile отдаёт has_avatar=false",
              json.loads(body)["profile"]["has_avatar"] is False, body[:200])

        # повторное удаление — идемпотентно
        code, body = post("/avatar", b"")
        check("повторное удаление тоже 200", code == 200)

        # битые данные отвергаются
        relay.set_avatar("alice", PNG)
        code, body = post("/avatar", b"NOT_PNG_GARBAGE")
        check("не-PNG данные отвергаются", code == 400, body[:200])
    finally:
        relay.stop()

# ─────────────────────────────────────────────────────────────────────
print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
