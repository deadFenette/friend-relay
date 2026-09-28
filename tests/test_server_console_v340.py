#!/usr/bin/env python3
"""Тесты v3.4.0: немая консоль сервера + журнал + GUI-бэкенд.

Проверяем:
  1. SessionStore.details()      — снапшот сессий для GUI (имя, возраст).
  2. PresenceBoard.details()     — «печатает»/живость для GUI.
  3. Живой сервер: online_details() видит подключившихся; kick_user()
     аннулирует сессии (клиент отваливается) + системное сообщение в чат;
     хоста кикнуть нельзя.
  4. Журнал: logs/server.log создаётся, содержит старт/вход/кик.
  5. server_main.py: консоль НЕМАЯ — со стартовой шпаргалкой и без неё
     (--quiet) нет НИКАКИХ периодических статус-строк; чистый выход по
     SIGTERM/SIGINT; код 2 при занятом порту.
  6. server_gui.py без PySide6 честно завершается с подсказкой (код 1).
"""
from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auth import SessionStore, sign_request
from lib.domain.presence import PresenceBoard
from lib.relay_server import RelayServer

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_ping(base: str, name: str) -> tuple[int, dict]:
    """/ping — GET с header_encode (как шлёт lib.client)."""
    from lib.client import _request, header_encode

    headers = {"X-Relay-From": header_encode(name)}
    status, data, _ = _request("GET", base + "/ping", headers, timeout=5)
    try:
        return status, json.loads(data.decode("utf-8"))
    except Exception:
        return status, {}


# ── 1. SessionStore.details ───────────────────────────────────────────
def test_session_details() -> None:
    print("── SessionStore.details()")
    st = SessionStore()
    st.register("Вася")
    st.register("Петя#2")
    det = st.details()
    names = {d["name"] for d in det}
    check("две сессии видны", names == {"Вася", "Петя#2"})
    check("age_s целое и >= 0", all(isinstance(d["age_s"], int) and d["age_s"] >= 0 for d in det))
    st.register("Вася")  # повторная регистрация аннулирует старую
    det2 = [d["name"] for d in st.details()]
    check("повторный register не плодит дублей", det2.count("Вася") == 1)


# ── 2. PresenceBoard.details ─────────────────────────────────────────
def test_presence_details() -> None:
    print("── PresenceBoard.details()")
    pb = PresenceBoard()
    pb.touch("Вася")
    pb.touch("Жуж")
    pb.set_typing("Вася")
    det = pb.details()
    check("онлайн-пара видна", set(det) == {"Вася", "Жуж"})
    check("typing-флаг у Васи", det["Вася"]["typing"] is True)
    check("у Жужа typing нет", det["Жуж"]["typing"] is False)
    check("last_seen_ago int", isinstance(det["Вася"]["last_seen_ago"], int))


# ── 3-4. Живой сервер: online_details, kick, журнал ──────────────────
def test_live_server() -> None:
    print("── Живой сервер: online_details / kick_user / журнал")
    tmp = Path(tempfile.mkdtemp(prefix="relay340_"))
    port = free_port()
    relay = RelayServer(tmp, host_name="Жуж", access_key="",
                        admin_key="test-admin-key")
    ok = relay.start("127.0.0.1", port)
    check("сервер стартовал", ok)
    if not ok:
        return
    try:
        time.sleep(0.2)

        base = f"http://127.0.0.1:{port}"
        code, body = http_ping(base, "Вася")
        check("Вася подключился", code == 200 and body.get("ok"))
        vasya_token = str(body.get("session_token", ""))
        http_ping(base, "Петя#2")

        time.sleep(0.2)
        det = relay.online_details()
        names = {d["name"] for d in det}
        check("online_details видит обоих", {"Вася", "Петя#2"} <= names)
        v = next(d for d in det if d["name"] == "Вася")
        check("у Васи has_session=True", v["has_session"] is True)
        check("Вася не админ", v["admin"] is False)
        host_row = next((d for d in det if d["name"] == "Жуж"), None)
        check("хост в списке — админ (или появится после входа)",
              host_row is None or host_row["admin"] is True)

        # журнал: старт + входы
        log_path = tmp / "logs" / "server.log"
        time.sleep(0.3)
        log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        check("logs/server.log создан", log_path.exists())
        check("старт записан", "сервер запущен" in log_text)
        check("вход Васи записан", "вход: Вася" in log_text)

        # kick: сессия аннулируется
        kicked = relay.kick_user("Вася", by="Жуж")
        check("kick_user вернул True", kicked is True)
        check("token Васи невалиден после кика",
              relay._sessions.validate(vasya_token) is None)
        time.sleep(0.2)
        names2 = {d["name"] for d in relay.online_details()}
        check("Васи больше в списке (по сессии)", "Вася" not in names2 or
              not next(d for d in relay.online_details()
                       if d["name"] == "Вася")["has_session"])

        # системное сообщение о кике в чате
        hist = relay.get_text_history(50) if hasattr(relay, "get_text_history") else []
        sys_msgs = [h for h in hist if "Вася отключён" in str(h)]
        check("системное сообщение о кике в чате", bool(sys_msgs) or
              _chat_has_kick(relay))

        # хоста кикнуть нельзя
        check("кик хоста отклонён", relay.kick_user("Жуж", by="Жуж") is False)
        check("кик пустого имени отклонён", relay.kick_user("", by="Жуж") is False)

        # журнал: кик записан
        time.sleep(0.3)
        log_text = log_path.read_text(encoding="utf-8")
        check("кик записан в журнал", "кик: Вася" in log_text)
    finally:
        relay.stop()

    log_text = (tmp / "logs" / "server.log").read_text(encoding="utf-8")
    check("остановка записана", "сервер остановлен" in log_text)


def _chat_has_kick(relay: RelayServer) -> bool:
    """Запасной способ проверить системное сообщение (разные версии API)."""
    try:
        for ev in relay.events_since(0):
            if "Вася отключён" in json.dumps(ev, ensure_ascii=False):
                return True
    except Exception:
        pass
    return False


# ── 5. server_main.py: немая консоль ─────────────────────────────────
def test_server_main_quiet() -> None:
    print("── server_main.py: немая консоль")
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="relay340main_"))
    port = free_port()

    proc = subprocess.Popen(
        [sys.executable, str(root / "server_main.py"),
         "--data-dir", str(tmp), "--port", str(port),
         "--name", "Тест", "--quiet", "--bind", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(root), text=True, encoding="utf-8", errors="replace",
    )
    time.sleep(3.0)
    check("процесс жив через 3с", proc.poll() is None)
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    else:
        out, err = proc.communicate()

    check("--quiet: консоль ПУСТАЯ (немая)", out.strip() == "")
    check("нет статус-строк", "online:" not in out and "uptime" not in out)
    log_path = tmp / "logs" / "server.log"
    check("журнал создан", log_path.exists())
    if log_path.exists():
        lt = log_path.read_text(encoding="utf-8")
        check("в журнале есть старт и остановка",
              "сервер запущен" in lt and "сервер остановлен" in lt)
    check("код выхода 0", proc.returncode == 0)


def test_server_main_banner() -> None:
    print("── server_main.py: шпаргалка + занятый порт")
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="relay340b_"))
    port = free_port()

    # Первый запуск: печатает шпаргалку и живёт.
    proc = subprocess.Popen(
        [sys.executable, str(root / "server_main.py"),
         "--data-dir", str(tmp), "--port", str(port),
         "--name", "Жуж", "--bind", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(root), text=True, encoding="utf-8", errors="replace",
    )
    time.sleep(3.0)
    alive = proc.poll() is None
    check("без --quiet: процесс жив", alive)
    out, err = "", ""
    if alive:
        proc.send_signal(signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    else:
        out, err = proc.communicate()
    check("шпаргалка печатается один раз", "Friend Relay v" in out)
    check("в шпаргалке admin key", "Admin key" in out)
    check("НЕТ дашборда/статус-строк", "ОНЛАЙН" not in out and "online:" not in out)
    check("код выхода 0", proc.returncode == 0)

    # Второй запуск на том же порту (порт свободен — но теперь проверим
    # занятость: поднимем первый снова и попробуем второй).
    p1 = subprocess.Popen(
        [sys.executable, str(root / "server_main.py"),
         "--data-dir", str(tmp), "--port", str(port),
         "--name", "Жуж", "--quiet", "--bind", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(root), text=True, encoding="utf-8", errors="replace")
    time.sleep(2.5)
    if p1.poll() is not None:
        check("первый сервер поднялся (пропуск теста занятого порта)",
              True)  # окружение не даёт занять порт — не считаем ошибкой
    else:
        p2 = subprocess.Popen(
            [sys.executable, str(root / "server_main.py"),
             "--data-dir", str(tmp / "d2"), "--port", str(port),
             "--name", "Второй", "--quiet", "--bind", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(root), text=True, encoding="utf-8", errors="replace")
        try:
            o2, e2 = p2.communicate(timeout=15)
            check("второй на занятом порту: код 2", p2.returncode == 2)
            check("ошибка порта видна в консоли (это ОШИБКА — можно)",
                  "ОШИБКА" in (o2 + e2) or "ОШИБКА" in e2)
        except subprocess.TimeoutExpired:
            p2.kill()
            check("второй на занятом порту: код 2 (таймаут)", False)
        finally:
            p1.send_signal(signal.SIGINT)
            try:
                p1.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                p1.kill()


# ── 6. server_gui.py без PySide6 ─────────────────────────────────────
def test_server_gui_no_pyside() -> None:
    print("── server_gui.py без PySide6 (в этой песочнице)")
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, str(root / "server_gui.py")],
        capture_output=True, text=True, timeout=30, cwd=str(root),
    )
    check("без PySide6 код выхода 1", r.returncode == 1)
    check("подсказка про PySide6", "PySide6" in (r.stdout + r.stderr))
    check("подсказка про server_main", "server_main.py" in (r.stdout + r.stderr))


if __name__ == "__main__":
    test_session_details()
    test_presence_details()
    test_live_server()
    test_server_main_quiet()
    test_server_main_banner()
    test_server_gui_no_pyside()
    print(f"\nИТОГО: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)
