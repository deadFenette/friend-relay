#!/usr/bin/env python3
"""Живой тест веб-прототипа: сервер отдаёт / и /web, а весь HTTP-путь,
который делает браузерный JS, работает (ping -> send_text с подписью ->
events -> send_file -> download -> profile/update)."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auth import sign_request
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


def fetch(req: urllib.request.Request) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def hdr_encode(s: str) -> str:
    """header_encode из lib/util.py: UTF-8 байты как latin-1 строка
    (urllib требует latin-1-совместимые значения заголовков)."""
    return s.encode("utf-8").decode("latin-1")


def _ping(base: str, name: str, admin_key: str = "") -> dict:
    """GET /ping как его делает веб-клиент (v3.5.1): X-Relay-From всегда,
    X-Relay-Admin — если введён ключ админа. Ответ с _status для проверок."""
    headers = {"X-Relay-From": hdr_encode(name)}
    if admin_key:
        headers["X-Relay-Admin"] = hdr_encode(admin_key)
    code, body, _ = fetch(urllib.request.Request(base + "/ping", headers=headers))
    data = json.loads(body)
    data["_status"] = code
    return data


def test_admin_key() -> None:
    """(v3.5.1) Ключ админа: loopback-вход без ключа, вход по верному ключу
    (в т.ч. с КИРИЛЛИЦЕЙ — как реальные ключи вида ВАСИЛИСА_2014), а на
    не-loopback адресе — дискриминатор «Host#2» и явная пометка
    admin_key_rejected при неверном ключе (раньше молчал)."""
    print("── Админ-ключ: /ping с X-Relay-Admin (v3.5.1)")
    admin = "ВАСИЛИСА_2014_ХХХ_КАРА_НЕБЕСНАЯ"   # кириллица намеренно
    tmp = Path(tempfile.mkdtemp(prefix="web_admin_"))
    relay = RelayServer(tmp, host_name="Host", access_key="", admin_key=admin)
    assert relay.start("127.0.0.1", 18510), "сервер 18510 не поднялся"
    base = "http://127.0.0.1:18510"
    try:
        time.sleep(0.2)
        d = _ping(base, "Host")
        check("loopback: имя хоста без ключа — пускает (правило loopback)",
              d["_status"] == 200 and d.get("ok") and not d.get("reserved_name"))
        d = _ping(base, "Host", admin_key=admin)
        check("верный ключ (кириллица) принимается",
              d["_status"] == 200 and d.get("ok") and not d.get("reserved_name"))
    finally:
        relay.stop()

    # Дискриминатор и отклонение ключа видны только с НЕ-loopback адреса:
    # соединяемся с собственным внешним IP. Если такого нет (глухая
    # песочница) — честно пропускаем, не роняя тест.
    ext_ip = ""
    try:
        from lib.net_utils import get_local_ips
        for ip in get_local_ips():
            if ip not in ("127.0.0.1", "::1"):
                ext_ip = ip
                break
    except Exception:
        pass
    if not ext_ip:
        print("  [SKIP] нет не-loopback интерфейса — дискриминатор не проверяем")
        return
    tmp2 = Path(tempfile.mkdtemp(prefix="web_admin2_"))
    relay2 = RelayServer(tmp2, host_name="Host", access_key="", admin_key=admin)
    if not relay2.start(ext_ip, 18511):
        print(f"  [SKIP] не смог подняться на {ext_ip} — дискриминатор не проверяем")
        return
    base2 = f"http://{ext_ip}:18511"
    try:
        time.sleep(0.2)
        d = _ping(base2, "Гость")
        if d.get("_status") != 200:
            print("  [SKIP] внешний адрес недоступен — дискриминатор не проверяем")
            return
        check("гость с чужого адреса входит свободно",
              d.get("ok") and not d.get("reserved_name"))

        d = _ping(base2, "Host")
        if not d.get("reserved_name"):
            print("  [SKIP] не-loopback адрес выглядит как loopback — дальше не проверяем")
            return
        check("чужой адрес без ключа -> «Host#2», без пометки неверного ключа",
              d.get("reserved_name") is True
              and str(d.get("name_assigned", "")).startswith("Host#")
              and not d.get("admin_key_rejected"))

        d = _ping(base2, "Host", admin_key="НЕВЕРНЫЙ_КЛЮЧ")
        check("неверный ключ помечается admin_key_rejected (раньше молчал)",
              d.get("reserved_name") is True and d.get("admin_key_rejected") is True)

        d = _ping(base2, "Host", admin_key=admin)
        check("верный ключ с чужого адреса = админ (имя хоста удержано)",
              d["_status"] == 200 and d.get("ok")
              and not d.get("reserved_name") and not d.get("name_assigned"))
    finally:
        relay2.stop()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="web_live_"))
    relay = RelayServer(tmp, host_name="Host", access_key="", max_file_size=10 * 1024 * 1024)
    port = 18500
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.2)

        # 1. Статика: / и /web отдают index.html
        for path in ("/", "/web", "/web/"):
            code, body, headers = fetch(urllib.request.Request(base + path))
            check(f"GET {path} -> 200", code == 200)
            check(f"GET {path} — text/html", "text/html" in headers.get("Content-Type", ""))
            check(f"GET {path} — это наш клиент", b"Friend Relay" in body)

        # 1b. (v3.5.1) на странице есть поле «Ключ админа», а main.js шлёт
        #     X-Relay-Admin на /ping
        code, body, _ = fetch(urllib.request.Request(base + "/"))
        check("index.html содержит поле id=adminkey",
              code == 200 and b'id="adminkey"' in body)
        req = urllib.request.Request(base + "/static/main.js")
        code, body, _ = fetch(req)
        check("main.js шлёт X-Relay-Admin на /ping",
              code == 200 and b"X-Relay-Admin" in body)

        # 1c. (v3.5.5) голос: мгновенная лестница реконнекта + watchdog
        #     стыла сокета; кнопки чата и пикер реакций оформлены в CSS
        req = urllib.request.Request(base + "/static/voice.js")
        code, body, _ = fetch(req)
        check("voice.js: быстрая лестница реконнекта (v3.5.5)",
              code == 200 and b"RECONNECT_LADDER_MS" in body)
        check("voice.js: watchdog стыла сокета (45с без входа)",
              b"45000" in body and b"lastInboundAt" in body)
        check("voice.js: тревожная надпись только при долгом разрыве",
              b"a >= 3 || goneS >= 5" in body)
        # 1c-bis. (v3.5.6) голос: реконнект доводит дело до конца
        check("voice.js: mute+счётчики синхронизируются и при реконнекте",
              b"function voiceReconnected" in body
              and b"voiceReconnected();" in body
              and b"if (j.ok){" in body)
        check("voice.js: джиттер-буфер сбрасывается к стартовым 80мс",
              b"VC.jitterTarget = 4" in body and b"VC.jitterTarget = 8" not in body)
        req = urllib.request.Request(base + "/static/style.css")
        code, body, _ = fetch(req)
        check("style.css: пилюля панели действий (v3.5.5)",
              code == 200 and b".msg .acts button:focus-visible" in body
              and b"top:-16px" in body)
        check("style.css: пикер реакций переехал в CSS (v3.5.5)",
              code == 200 and b".rpicker span:hover" in body)

        # 2. Путь браузера: ping -> session_token
        req = urllib.request.Request(base + "/ping", headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        data = json.loads(body)
        check("ping отдал session_token", code == 200 and data.get("session_token"))
        token = data["session_token"]

        # 2b. /crypto/info: соль PBKDF2 для веб-клиента (нужна для вывода
        #     AES-ключа той же, что у Qt-клиента хоста)
        req = urllib.request.Request(base + "/crypto/info",
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        crypto_info = json.loads(body)
        check("crypto/info отдаёт соль", code == 200
              and crypto_info.get("ok") and crypto_info.get("salt"))
        salt = crypto_info.get("salt", "")

        # 2c. /static/*: статика веб-клиента (v1.8.2 — клиент разбит на
        #     модули: forge + core/chat/files/profile/chess/voice/main.js
        #     + style.css + voice-worklet.js)
        req = urllib.request.Request(base + "/static/forge.min.js")
        code, body, headers = fetch(req)
        check("static forge.min.js отдаётся", code == 200
              and b"forge" in body[:400]
              and "javascript" in headers.get("Content-Type", ""))
        req = urllib.request.Request(base + "/static/style.css")
        code, body, headers = fetch(req)
        check("static style.css отдаётся (text/css)", code == 200
              and b"--bg" in body
              and "text/css" in headers.get("Content-Type", ""))
        for mod in ("core.js", "chat.js", "files.js", "profile.js",
                    "chess.js", "voice.js", "voice-worklet.js", "main.js"):
            req = urllib.request.Request(base + "/static/" + mod)
            code, body, headers = fetch(req)
            check(f"static {mod} отдаётся", code == 200 and len(body) > 300
                  and "javascript" in headers.get("Content-Type", ""))
        check("static защищён от traversal",
              fetch(urllib.request.Request(base + "/static/..%2Findex.html"))[0] == 404)
        check("static не отдаёт не-стилики",
              fetch(urllib.request.Request(base + "/static/index.html"))[0] == 404)
        check("static не отдаёт .py",
              fetch(urllib.request.Request(base + "/static/x.py"))[0] == 404)

        # 3. send_text с подписью X-Relay-Auth (как в JS hmacHex)
        payload = json.dumps({"text": "привет из браузера"}, ensure_ascii=False).encode()
        auth = token + ":" + sign_request(token, payload)
        req = urllib.request.Request(
            base + "/send_text", data=payload, method="POST",
            headers={"X-Relay-From": "WebUser", "X-Relay-Auth": auth,
                     "Content-Type": "application/json; charset=utf-8"},
        )
        code, body, _ = fetch(req)
        check("send_text с подписью принят", code == 200 and json.loads(body).get("ok"))

        # 4. events: сообщение видно
        req = urllib.request.Request(base + "/events?since=0",
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        events = json.loads(body).get("events", [])
        check("events видит сообщение", any(
            e.get("kind") == "text" and e.get("text") == "привет из браузера"
            for e in events))

        # 4b. РЕГРЕССИЯ v1.8.2 (typing + keep-alive): POST /typing раньше
        #     не вычитывал тело — байты "{}" склеивались со следующим
        #     запросом на том же сокете: "501 Unsupported method ('{}POST')".
        #     Веб-клиент начал слать /typing и вскрыл это. Проверяем
        #     последовательность на ОДНОМ keep-alive соединении.
        import http.client
        hc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            hc.request("POST", "/typing", body=b"{}",
                       headers={"X-Relay-From": "WebUser",
                                "X-Relay-Key": "",
                                "Content-Type": "application/json"})
            r1 = hc.getresponse()
            r1.read()
            typing_code = r1.status
            hc.request("POST", "/send_text",
                       body=json.dumps({"text": "после typing"}).encode("utf-8"),
                       headers={"X-Relay-From": "WebUser",
                                "Content-Type": "application/json"})
            r2 = hc.getresponse()
            r2.read()
            after_code = r2.status
        finally:
            hc.close()
        check("typing на keep-alive соединении принят", typing_code == 200)
        check("запрос ПОСЛЕ typing не побился (раньше 501 '{}POST')",
              after_code == 200)

        # 4c. typing виден другим участникам через /events
        req = urllib.request.Request(base + "/typing", data=b"{}",
                                     method="POST",
                                     headers={"X-Relay-From": "Other"})
        code, body, _ = fetch(req)
        req = urllib.request.Request(base + "/events?since=0",
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        check("typing виден другим через /events",
              "Other" in (json.loads(body).get("typing") or []))

        # 5. Загрузка файла (как uploadFile: сырые байты + X-Relay-Filename/SHA256;
        #    кириллица в заголовке — через header_encode: UTF-8 байты как latin-1,
        #    ровно как делает headerEncode() в JS)
        blob = b"WEB-BLOB-" + bytes(range(64)) * 4
        sha = hashlib.sha256(blob).hexdigest()
        fn = "прототип.bin".encode().decode("latin-1")
        req = urllib.request.Request(
            base + "/send_file", data=blob, method="POST",
            headers={"X-Relay-From": "WebUser",
                     "X-Relay-Filename": fn,
                     "X-Relay-SHA256": sha},
        )
        code, body, _ = fetch(req)
        data = json.loads(body)
        check("send_file принял байты", code == 200 and data.get("ok"))
        file_id = data.get("file_id", "")

        # 6. Скачивание по /download/{id} и сверка sha256
        req = urllib.request.Request(base + "/download/" + file_id,
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        check("download отдал ровно те же байты",
              code == 200 and hashlib.sha256(body).hexdigest() == sha)

        # 7. Профиль: GET + rename через /profile/update (кнопка в веб-профиле)
        req = urllib.request.Request(base + "/profile/WebUser",
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        prof = json.loads(body).get("profile", {})
        check("GET /profile отдаёт имя входа", prof.get("name") == "WebUser")

        payload = json.dumps({"display_name": "Веб-Вася"}, ensure_ascii=False).encode()
        req = urllib.request.Request(
            base + "/profile/update", data=payload, method="POST",
            headers={"X-Relay-From": "WebUser",
                     "Content-Type": "application/json; charset=utf-8"},
        )
        code, body, _ = fetch(req)
        data = json.loads(body)
        check("profile/update сохранил display_name",
              code == 200 and (data.get("profile") or {}).get("display_name") == "Веб-Вася")

        # 8. Подмена подписи отбивается (безопасность пути send_text)
        payload = json.dumps({"text": "фейк"}).encode()
        req = urllib.request.Request(
            base + "/send_text", data=payload, method="POST",
            headers={"X-Relay-From": "WebUser", "X-Relay-Auth": token + ":" + "0" * 64,
                     "Content-Type": "application/json"},
        )
        code, _, _ = fetch(req)
        check("битая подпись -> 403", code == 403)

        # 8b. Раунд-трип шифрования сообщений: ровно то, что делает веб-клиент
        #     после ввода ключа (AES-256-GCM по соли из /crypto/info)
        from lib.crypto import decrypt_text, encrypt_text

        secret = "общий-секретный-ключ"
        enc_text, iv = encrypt_text("тайна за шифром", secret, salt)
        check("encrypt_text даёт v2-формат", enc_text.startswith("v2:"))
        payload = json.dumps(
            {"text": enc_text, "encrypted": True, "iv": iv},
            ensure_ascii=False).encode()
        auth = token + ":" + sign_request(token, payload)
        req = urllib.request.Request(
            base + "/send_text", data=payload, method="POST",
            headers={"X-Relay-From": "WebUser", "X-Relay-Auth": auth,
                     "Content-Type": "application/json; charset=utf-8"},
        )
        code, body, _ = fetch(req)
        check("зашифрованный send_text принят", code == 200
              and json.loads(body).get("ok"))

        req = urllib.request.Request(base + "/events?since=0",
                                     headers={"X-Relay-From": "WebUser"})
        code, body, _ = fetch(req)
        enc_ev = next((e for e in json.loads(body).get("events", [])
                       if e.get("kind") == "text" and e.get("encrypted")), None)
        check("events помечает сообщение encrypted", enc_ev is not None)
        if enc_ev:
            check("расшифровка тем же ключом даёт исходник",
                  decrypt_text(enc_ev["text"], secret, salt,
                               enc_ev.get("iv", "")) == "тайна за шифром")
            check("чужой ключ не расшифровывает",
                  decrypt_text(enc_ev["text"], "другой-ключ", salt) == "")

        # 9. Шахматы PVP через /bot_command — ровно так их играет веб-клиент
        #    (вкладка «Шахматы»): host -> join -> move/move -> ошибка хода ->
        #    state -> resign. Судья — ChessBot на сервере, UI ничего не решает.
        def bot_cmd(name: str, tok: str, command: str, args: list) -> dict:
            payload = json.dumps(
                {"bot_id": "chess", "command": command, "args": args},
                ensure_ascii=False).encode()
            auth = tok + ":" + sign_request(tok, payload)
            req = urllib.request.Request(
                base + "/bot_command", data=payload, method="POST",
                headers={"X-Relay-From": name, "X-Relay-Auth": auth,
                         "Content-Type": "application/json; charset=utf-8"},
            )
            code, body, _ = fetch(req)
            return {"code": code, "data": json.loads(body)}

        req = urllib.request.Request(base + "/ping", headers={"X-Relay-From": "Friend"})
        code, body, _ = fetch(req)
        data = json.loads(body)
        check("ping второго игрока", code == 200 and data.get("session_token"))
        token2 = data["session_token"]

        r = bot_cmd("WebUser", token, "host", [])
        act = r["data"].get("bot_action") or {}
        check("host -> match_created", act.get("action") == "match_created")
        mid = (act.get("match") or {}).get("match_id", "")
        check("матч получил код", bool(mid))
        check("хост играет белыми", (act.get("match") or {}).get("white") == "WebUser")

        r = bot_cmd("Friend", token2, "join", [mid])
        act = r["data"].get("bot_action") or {}
        check("join -> match_joined", act.get("action") == "match_joined")
        check("друг играет чёрными", (act.get("match") or {}).get("black") == "Friend")

        r = bot_cmd("WebUser", token, "move", ["e2e4"])
        act = r["data"].get("bot_action") or {}
        check("ход белых принят", act.get("action") == "move_accepted"
              and (act.get("match") or {}).get("moves") == ["e2e4"])

        # после e2e4 ход ЧЁРНЫХ: попытка белой пешкой от чёрных -> «не твой ход»
        r = bot_cmd("Friend", token2, "move", ["d2d4"])
        act = r["data"].get("bot_action") or {}
        check("не та очередь -> error", act.get("action") == "error")

        r = bot_cmd("Friend", token2, "move", ["d7d5"])
        act = r["data"].get("bot_action") or {}
        check("ход чёрных принят", act.get("action") == "move_accepted"
              and len((act.get("match") or {}).get("moves") or []) == 2)

        r = bot_cmd("WebUser", token, "move", ["e2e5"])
        act = r["data"].get("bot_action") or {}
        check("нелегальный ход отклонён", act.get("action") == "error")

        r = bot_cmd("Friend", token2, "state", [])
        act = r["data"].get("bot_action") or {}
        check("state отдаёт позицию (поллинг как в UI)",
              act.get("action") == "match_state"
              and (act.get("match") or {}).get("moves") == ["e2e4", "d7d5"])

        r = bot_cmd("WebUser", token, "resign", [])
        act = r["data"].get("bot_action") or {}
        check("resign -> match_finished", act.get("action") == "match_finished")
        check("победа присуждена сопернику",
              (act.get("match") or {}).get("result") == "black")

        r = bot_cmd("Friend", token2, "state", [])
        act = r["data"].get("bot_action") or {}
        m = act.get("match") or {}
        check("финальное состояние видно обоим (вкл. завершённые)",
              m.get("status") == "finished" and m.get("result") == "black")

    finally:
        relay.stop()

    test_admin_key()

    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
