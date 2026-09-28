#!/usr/bin/env python3
"""Регрессионные тесты багфиксов v1.9.5 (QT + ядро).

1. ChatSession: live-обновление открытого канала на тике (раньше чужие
   сообщения в канале не приезжали до собственных действий).
2. ChatSession: детектор обрыва связи (5 ошибок подряд → on_poll_failed,
   успех → on_poll_recovered; раньше обрыв не детектировался вообще).
3. ChatSession: поздний initial после disconnect не рисует ленту.
4. Голосовой диалог: Esc (reject) вызывает closeEvent — микрофон и таймер
   больше не остаются живыми после закрытия окна клавишей.
5. DmScreen: инкрементальный дифф (тихие тики не пересобирают ленту;
   позиция скролла при чтении истории не сбрасывается).
6. Правка ЗАШИФРОВАННОГО сообщения: полный серверный круг — зашифрованная
   правка хранится зашифрованной и расшифровывается у получателя.
7. DM: участники диалога с '_' в имени (фантомные диалоги из split('_')).

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/test_v195_qt.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra and not cond else ""))
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1


# ══ 1-3: ядро ChatSession (без Qt) ════════════════════════════════════
from lib.client_core.chat_session import ChatSession


class FakeRunner:
    """Синхронный «мост»: fn выполняется сразу, колбэки — сразу."""

    def __init__(self):
        self.calls: list = []

    def run(self, fn, on_success=None, on_error=None):
        self.calls.append(fn)
        try:
            result = fn()
        except Exception as e:
            if on_error:
                on_error(e)
            return
        if on_success:
            on_success(result)


def new_session(runner_run=None):
    s = ChatSession(runner=runner_run or FakeRunner().run)
    s.configure("http://x", "me", "")
    s.set_connected(True)
    return s


# -- 1. Live-обновление канала на тике ------------------------------------
fr = FakeRunner()
s2 = new_session(fr.run)
s2.set_channel("music")
s2.tick()
# после тика должно быть два запроса: poll_events И get_channel_messages
check("1. тик при открытом канале дёргает и poll, и хвост канала",
      len(fr.calls) == 2, f"calls={len(fr.calls)}")

# повторный tick при in-flight канальном запросе не плодит параллельные
s2._channel_refresh_in_flight = True
n_before = len(fr.calls)
s2.tick()
check("1a. in-flight guard: параллельного запроса канала нет",
      len(fr.calls) == n_before + 1, f"+{len(fr.calls) - n_before}")  # только poll

# -- 2. Детектор обрыва связи ----------------------------------------------
fr3 = FakeRunner()
s3 = new_session(fr3.run)
events = {"failed": [], "recovered": []}
s3.on_poll_failed = lambda e: events["failed"].append(str(e))
s3.on_poll_recovered = lambda: events["recovered"].append(1)

class Boom(Exception):
    pass

def _fail_poll():
    raise Boom("host dead")

fr3.run = lambda fn, on_success=None, on_error=None: on_error(Boom("x")) if on_error else None
s3._runner = fr3.run
for _ in range(5):
    s3.do_poll()
check("2. 5 ошибок подряд → on_poll_failed (один раз)",
      len(events["failed"]) == 1, f"got={len(events['failed'])}")
for _ in range(3):
    s3.do_poll()
check("2a. дальше ошибки не спамят колбэк", len(events["failed"]) == 1)

fr4 = FakeRunner()
s3._runner = fr4.run
# успешный poll: прямой вызов внутреннего хендлера
s3._on_poll_success({"events": [], "next_since": 10})
check("2b. успех после обрыва → on_poll_recovered",
      len(events["recovered"]) == 1, f"got={len(events['recovered'])}")
check("2c. счётчик ошибок сброшен", s3._poll_errors == 0)

# -- 3. Поздний initial после disconnect -----------------------------------
s5 = new_session()
s5._connected = False  # «отключились», пока ответ летит
drawn = []
s5.on_initial_result = lambda r: drawn.append(r)
s5._on_initial_success({"events": [{"seq": 1, "kind": "text"}], "next_since": 1})
check("3. initial после disconnect не рисует ленту", not drawn)

# ══ 4: голосовой диалог — Esc ═════════════════════════════════════════
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from qt_app.widgets.voice_channel_dialog import VoiceChannelDialog

dlg = VoiceChannelDialog(
    parent=None,
    base_url="http://127.0.0.1:1",
    voice_host="127.0.0.1",
    voice_port=1,
    player_name="tester",
    access_key="",
)
closed_fired = []
dlg.closed.connect(lambda: closed_fired.append(1))
dlg.show()
timer_was_running = dlg._poll_timer.isActive()
# Esc = reject() у QDialog: должен пройти через close() → closeEvent
dlg.reject()
app.processEvents()  # offscreen: даём событию закрытия обработаться
check("4. Esc (reject) вызывает closeEvent: closed.emit",
      bool(closed_fired), f"fired={closed_fired}")
check("4a. таймер опроса участников остановлен",
      not dlg._poll_timer.isActive())
check("4b. окно скрыто", not dlg.isVisible())

# ══ 5: DM-экран — инкрементальный дифф ════════════════════════════════
from qt_app.screens.dm_screen import DMScreen as DmScreen

dm = DmScreen()
dm._connected = True
dm._base_url = "http://127.0.0.1:1"
dm._my_name = "me"
dm._active_user = "bob"

msgs1 = [
    {"seq": 1, "kind": "dm", "from": "bob", "to": "me", "text": "привет", "ts": 1},
    {"seq": 2, "kind": "dm", "from": "me", "to": "bob", "text": "приветик", "ts": 2},
]
dm._on_chat_loaded(list(msgs1), "bob")
built = list(dm._dm_widgets.values())
check("5. первая загрузка построила сообщения", len(built) == 2, f"n={len(built)}")

# тихий тик: тот же список — виджеты не пересоздаются
widgets_before = list(dm._dm_widgets.values())
dm._on_chat_loaded(list(msgs1), "bob")
check("5a. тихий тик не пересобирает ленту (те же объекты)",
      list(dm._dm_widgets.values()) == widgets_before)

# приращение: одно новое сообщение — старые виджеты живы, добавился один
msgs2 = [
    *msgs1,
    {"seq": 3, "kind": "dm", "from": "bob", "to": "me", "text": "новое", "ts": 3},
]
dm._on_chat_loaded(list(msgs2), "bob")
check("5b. инкремент: старые виджеты на месте",
      list(dm._dm_widgets.values())[:2] == widgets_before)
check("5c. инкремент: новое сообщение дописано", len(dm._dm_widgets) == 3)

# устаревший ответ (другой юзер) не рисуется
dm._active_user = "alice"
dm._on_chat_loaded(list(msgs2), "bob")
check("5d. поздний ответ чужого диалога не рисуется",
      len(dm._dm_widgets) == 3)

# ══ 6: правка зашифрованного сообщения (серверный круг) ═══════════════
from lib.crypto import CryptoManager, decrypt_text
from lib.relay_server import RelayServer

tmp = Path(tempfile.mkdtemp(prefix="fr_v195_"))
relay = RelayServer(tmp, host_name="H", access_key="k")
port = 18890 + os.getpid() % 500
assert relay.start("127.0.0.1", port), "сервер не поднялся"

try:
    from lib import client

    # клиент с шифрованием
    salt = relay.get_crypto_salt()
    cm = CryptoManager(ENC_PASS := "pass195", salt)
    client.set_crypto_salt(salt)
    client.set_crypto_secret_key(ENC_PASS)
    tok = client.ping(f"http://127.0.0.1:{port}", name="Alice")["session_token"]
    client.set_session_token(tok)

    seq = client.send_text(f"http://127.0.0.1:{port}", "Alice", "секрет",
                           access_key="k", encrypt=True)
    evs = relay.events_since(0)
    orig = next(e for e in evs if e.get("seq") == seq)
    check("6. оригинал зашифрован (v2:)",
          orig.get("encrypted") and orig["text"].startswith("v2:"))

    # ПРАВКА с перешифрованием (новое поведение)
    client.edit_text(f"http://127.0.0.1:{port}", "Alice", seq,
                     "секрет (правка)", access_key="k", encrypt=True)
    evs2 = relay.events_since(0)
    edited_ev = next(e for e in evs2 if e.get("seq") == seq)
    check("6a. правка хранится зашифрованной",
          edited_ev.get("encrypted") and edited_ev["text"].startswith("v2:"))
    pt = decrypt_text(edited_ev["text"], ENC_PASS, salt, edited_ev.get("iv", ""))
    check("6b. правка расшифровывается у получателя",
          pt == "секрет (правка)", f"pt={pt!r}")
    check("6c. флаг edited выставлен", bool(edited_ev.get("edited")))
finally:
    relay.stop()

# ══ 7: DM-файл с подчёркиванием в имени ════════════════════════════════
from lib.domain.log_io import get_dm_conversations

tmp2 = Path(tempfile.mkdtemp(prefix="fr_v195_dm_"))
dm_dir = tmp2 / "dm"
dm_dir.mkdir()
# диалог anna ↔ bob_marley: имя файла через sorted + '_'
(dm_dir / "anna_bob_marley.jsonl").write_text(
    '{"kind": "dm", "from": "anna", "to": "bob_marley", "text": "hi", "seq": 1, "ts": 5}\n',
    encoding="utf-8",
)
convs = get_dm_conversations(dm_dir, "anna")
check("7. anna видит собеседника bob_marley (не фантомного bob)",
      len(convs) == 1 and convs[0]["user"] == "bob_marley", f"c={convs}")
convs2 = get_dm_conversations(dm_dir, "bob_marley")
check("7a. bob_marley видит anna",
      len(convs2) == 1 and convs2[0]["user"] == "anna", f"c={convs2}")

print(f"\nИтог: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
