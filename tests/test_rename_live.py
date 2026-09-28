#!/usr/bin/env python3
"""Живой тест смены display_name: настоящий RelayServer + client API.

Путь, который нажимает кнопка ✏ в Профиле:
  1. client.update_profile(display_name=...) -> POST /profile/update
  2. client.get_profile -> имя сохранилось и читается обратно
  3. client.get_friends -> друзья видят display_name
Плюс поведение ProfileManager на граничные случаи (пустое/длинное имя).
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import client
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rename_live_"))
    relay = RelayServer(tmp, host_name="Host", access_key="", max_file_size=10 * 1024 * 1024)
    port = 18499
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.2)

        # 1. Смена имени как её делает кнопка (через client.update_profile)
        updated = client.update_profile(base, "Host", display_name="Быстрый Ёжик", access_key="")
        check("update_profile вернул профиль", bool(updated))
        check("сервер сохранил новое имя", (updated or {}).get("display_name") == "Быстрый Ёжик")

        # 2. Читается обратно другим запросом (как _refresh_profile)
        profile = client.get_profile(base, "Host", "")
        check("get_profile видит новое имя", (profile or {}).get("display_name") == "Быстрый Ёжик")
        check("логин не изменился", (profile or {}).get("name") == "Host")

        # 3. Друзья видят display_name (get_friends): сначала добавляем
        #    Guest в друзья Host, затем меняем Guest отображаемое имя
        client.add_friend(base, "Host", "Guest", "")
        client.update_profile(base, "Guest", display_name="Гость Гостевич", access_key="")
        friends = client.get_friends(base, "Host", "")
        me = [f for f in friends if f.get("name") == "Guest"]
        check("в списке друзей display_name", bool(me) and me[0].get("display_name") == "Гость Гостевич")

        # 4. Пустое имя сервер не принимает как пустоту — профиль остаётся прежним
        #    (клиентский validate_display_name отсекает None, сервер страхует: трим+обрез)
        updated2 = client.update_profile(base, "Host", display_name="   ", access_key="")
        # сервер: dn = ("   " or name).strip() -> "" -> dn or name -> "Host"
        check("пустое имя -> откат к логину", (updated2 or {}).get("display_name") == "Host")

        # 5. Длинное имя обрезается до 32
        updated3 = client.update_profile(base, "Host", display_name="Ж" * 50, access_key="")
        check("длинное имя обрезано до 32", len((updated3 or {}).get("display_name", "")) == 32)

        # 6. Возврат к логину: пустой display_name -> имя = логину
        profile2 = client.get_profile(base, "Host", "")
        check("после обрезки читается 32", len((profile2 or {}).get("display_name", "")) == 32)

    finally:
        relay.stop()
        client.clear_session()

    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
