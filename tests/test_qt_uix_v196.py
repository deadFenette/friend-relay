"""Qt UI v1.9.6 — экраны и виджеты новых настроек (offscreen).

Проверяет:
  1. ProfileSettingsScreen: карточки «Звук и голос» и «Уведомления»
     строятся, контролы читают и пишут настройки
  2. VoiceChannelDialog: комбобоксы устройств/громкость/шумодав,
     живое переключение устройства на мокнутом VoiceClient
  3. LevelMeter: set_level красит без исключений
  4. qt_app.sound: «динь» собирается, гейт громкости работает
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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


import tempfile

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import lib.storage as storage

TMP = Path(tempfile.mkdtemp())
storage.SETTINGS_FILE = TMP / "settings.json"
storage.DATA_DIR = TMP

# ── мокаем sounddevice ДО импорта voice-модулей ──────────────────────
fake_sd = types.ModuleType("sounddevice")


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


FAKE_DEVICES = [
    {"name": "Default", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Blue Yeti", "max_input_channels": 2, "max_output_channels": 2},
    {"name": "Webcam Mic", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
]
fake_sd.query_devices = lambda: FAKE_DEVICES
fake_sd.InputStream = _FakeStream
fake_sd.OutputStream = _FakeStream
sys.modules["sounddevice"] = fake_sd

import voice.devices as _vdev  # v2.0: сейм sounddevice для всего voice-стека

assert _vdev.HAS_AUDIO, \
    "фейковый sounddevice должен подхватиться из sys.modules"

# ─────────────────────────────────────────────────────────────────────
print("1. Экран профиля: карточки настроек")

from qt_app.screens.profile_settings import ProfileSettingsScreen

scr = ProfileSettingsScreen()
check("карточка «Звук и голос» построена",
      hasattr(scr, "_mic_combo") and hasattr(scr, "_spk_combo"))
check("комбобоксы заполнены устройствами",
      scr._mic_combo.count() == 3 and scr._spk_combo.count() == 4,
      f"mic={scr._mic_combo.count()} spk={scr._spk_combo.count()}")
check("пункт «По умолчанию» первый в обоих",
      scr._mic_combo.itemText(0).startswith("По умолчанию")
      and scr._spk_combo.itemText(0).startswith("По умолчанию"))
check("громкость/шумодав/метр/проверка есть",
      hasattr(scr, "_vol_slider") and hasattr(scr, "_ns_check")
      and hasattr(scr, "_mic_test_btn") and hasattr(scr, "_mic_meter"))
check("карточка «Уведомления» построена",
      hasattr(scr, "_sound_check") and hasattr(scr, "_notif_vol_slider"))

# выбор устройств -> настройки
scr._mic_combo.setCurrentIndex(scr._mic_combo.findData("Blue Yeti"))
scr._spk_combo.setCurrentIndex(scr._spk_combo.findData("Speakers"))
scr._on_voice_device_changed()
s = storage.load_settings()
check("выбранный микрофон сохранён", s["voice_input_device"] == "Blue Yeti")
check("выбранный динамик сохранён", s["voice_output_device"] == "Speakers")

scr._vol_slider.setValue(140)
s = storage.load_settings()
check("громкость 140% сохранена", abs(s["voice_output_volume"] - 1.4) < 1e-9)
check("лейбл громкости обновился", scr._vol_value_label.text() == "140%")

scr._ns_check.setChecked(False)
s = storage.load_settings()
check("выключение шумодава сохранено", s["voice_noise_suppression"] is False)

scr._sound_check.setChecked(False)
scr._notif_vol_slider.setValue(80)
s = storage.load_settings()
check("уведомления: вкл/громкость сохранены",
      s["sound_on_message"] is False and abs(s["sound_volume"] - 0.8) < 1e-9)

# refresh кнопка пересканирует устройства
FAKE_DEVICES.append({"name": "New Headset", "max_input_channels": 1,
                      "max_output_channels": 2})
scr._fill_voice_combos()
check("↻ подхватывает новые устройства",
      scr._mic_combo.count() == 4 and scr._spk_combo.count() == 5,
      f"mic={scr._mic_combo.count()} spk={scr._spk_combo.count()}")

# ─────────────────────────────────────────────────────────────────────
print("\n2. Диалог голосового канала: настройки устройств")

from qt_app.widgets.voice_channel_dialog import VoiceChannelDialog

dlg = VoiceChannelDialog(None, "http://127.0.0.1:8420", "127.0.0.1", 8421,
                         "tester", "")
check("комбобоксы в диалоге заполнены",
      dlg._mic_combo.count() == 4 and dlg._spk_combo.count() == 5,
      f"mic={dlg._mic_combo.count()} spk={dlg._spk_combo.count()}")
s = storage.load_settings()
check("VoiceClient создан с сохранёнными устройствами",
      dlg._voice.get_devices() == (s["voice_input_device"], s["voice_output_device"]),
      str(dlg._voice.get_devices()))
check("VoiceClient получил громкость",
      abs(dlg._voice.get_output_volume() - 1.4) < 1e-9)

# живое переключение: мокаем рестрим
restarted = []
dlg._connected = True
orig_restart = dlg._voice.restart_streams
dlg._voice.restart_streams = lambda: restarted.append(1) or True
dlg._mic_combo.setCurrentIndex(dlg._mic_combo.findData("New Headset"))
dlg._on_device_changed()
check("смена микрофона на живом соединении зовёт restart_streams",
      len(restarted) == 1)
s = storage.load_settings()
check("новый микрофон сохранился", s["voice_input_device"] == "New Headset")
dlg._voice.restart_streams = orig_restart

# громкость в диалоге применяется мгновенно
dlg._vol_slider.setValue(70)
check("громкость применилась к VoiceClient сразу",
      abs(dlg._voice.get_output_volume() - 0.7) < 1e-9)

# шумоподавление через диалог
dlg._ns_check.setChecked(True)
check("шумодав через диалог применился", dlg._voice.is_noise_suppression() is True)

dlg._teardown()
dlg.deleteLater()

# ─────────────────────────────────────────────────────────────────────
print("\n3. LevelMeter")

from qt_app.widgets.level_meter import LevelMeter

meter = LevelMeter()
meter.resize(120, 8)
meter.show()
meter.set_level(0.5)
meter.set_level(0.5)   # дубль — не должен дёргать repaint
check("метр принимает уровень без исключений", meter._level == 0.5)
meter.set_level(5.0)
check("уровень клампится к 1.0", meter._level == 1.0)
meter.hide()

# ─────────────────────────────────────────────────────────────────────
print("\n4. Звук уведомления")

import numpy as np

from qt_app import sound as qsound

data = qsound._build_ding(0.5)
check("динь собирается (int16, ~0.36с)", data is not None and data.size > 0
      and data.dtype == np.int16)
check("громкость 0 = тишина", int(np.max(np.abs(qsound._build_ding(0.0)))) == 0)
# пик = 55% шкалы × громкость (0.5): 0.55*32767*0.5 ≈ 9011
check("пик = 55% шкалы × громкость",
      abs(int(np.max(np.abs(data))) - int(0.55 * 32767 * 0.5)) <= 2,
      str(int(np.max(np.abs(data)))))
data_full = qsound._build_ding(1.0)
check("пик на полной громкости ≈ 55% шкалы",
      abs(int(np.max(np.abs(data_full))) - int(0.55 * 32767)) <= 2,
      str(int(np.max(np.abs(data_full)))))

# …и основной джингл не падает без устройств (PortAudio в тестах пуст)
played = qsound.play_message_sound(0.5)
check("play_message_sound не падает (нет устройств -> False/фолбэк)",
      isinstance(played, bool))

print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
