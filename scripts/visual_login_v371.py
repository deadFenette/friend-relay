#!/usr/bin/env python3
"""Визуальная проверка дружелюбного экрана входа (v3.7.1).

Поднимает реальный RelayServer (с ключом доступа) и снимает скриншоты
ключевых состояний окна входа в браузере (Chromium):

  1. пустой вход (aurora, тёмная)      — что видит новичок
  2. имя введено                       — живой аватар с инициалами
  3. спойлер ключей раскрыт            — advKeys
  4. ошибка подключения                — пилюля «!» в connWarn
  5. состояние «Подключаемся…»         — вертушка на кнопке
  6. светлая тема Snow + скин Крем-брюле
  7. мобильный вьюпорт 390x844

Результат: /tmp/fr_login/*.png — смотрим глазами.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.relay_server import RelayServer  # noqa: E402

OUT = Path("/tmp/fr_login")
OUT.mkdir(exist_ok=True)


def main() -> int:
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="fr_login_"))
    base = 18560
    relay = RelayServer(tmp, host_name="Хост", access_key="k",
                        max_file_size=10 * 1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("сервер не стартовал")
        return 1

    url = f"http://127.0.0.1:{base}/"
    time.sleep(0.2)

    ok = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def new_page(viewport=None):
                ctx = browser.new_context(
                    viewport=viewport or {"width": 1280, "height": 900},
                    locale="ru-RU")
                page = ctx.new_page()
                page.goto(url)
                page.wait_for_selector("#connectPanel")
                return ctx, page

            def shot(page, name):
                page.screenshot(path=str(OUT / f"{name}.png"))
                print("shot:", name)

            # 1. пустой вход, тёмная тема по умолчанию
            ctx1, pg = new_page()
            shot(pg, "1_login_dark_empty")

            # 2. имя введено — аватар с инициалами + цвет из avatarColor
            pg.fill("#name", "Жужа Пример")
            shot(pg, "2_login_name_typed")

            # 3. спойлер ключей раскрыт
            pg.click("#advKeys summary")
            shot(pg, "3_login_advkeys_open")

            # 4. ошибка подключения: /ping открыт даже с access_key
            #     (регистрация сессии), поэтому честный путь до warn-пилюли
            #     на экране входа — пустое имя: «Введи имя»
            pg.fill("#name", "")
            pg.click("#btnConnect")
            pg.wait_for_selector("#connWarn:not(:empty)")
            shot(pg, "4_login_error_pill")

            # 5. состояние «Подключаемся…» — вертушка на кнопке.
            #     Живой прогон (класс .loading ставится/снимается main.js)
            #     проверен отдельно; для СКРИНШОТА играем детерминированно:
            #     sync-playwright блокирует поток в route-хендлере, поэтому
            #     честную гонку «снимок vs ответ /ping» не выиграть —
            #     вешаем ровно тот же класс и надпись, что ставит main.js
            #     (см. connect() в main.js), снимаем, возвращаем как было.
            ctx1.close()
            ctx5 = browser.new_context(viewport={"width": 1280, "height": 900})
            pg5 = ctx5.new_page()
            pg5.goto(url)
            pg5.wait_for_selector("#connectPanel")
            pg5.fill("#name", "Жужа")
            pg5.evaluate("""() => {
              document.getElementById('btnConnect').classList.add('loading');
              document.getElementById('btnConnectTxt').textContent =
                'Подключаемся…';
            }""")
            shot(pg5, "5_login_connecting")
            pg5.evaluate("""() => {
              document.getElementById('btnConnect').classList.remove('loading');
              document.getElementById('btnConnectTxt').textContent =
                'Войти в чат';
            }""")
            ctx5.close()
            ok += 1

            # 6. светлая тема Snow и скин «Крем-брюле»
            ctx6, pg6 = new_page()
            pg6.evaluate("document.documentElement.setAttribute('data-theme','snow');"
                         "document.documentElement.style.colorScheme='light'")
            shot(pg6, "6_login_snow")
            pg6.evaluate("document.documentElement.removeAttribute('data-theme');"
                         "document.documentElement.setAttribute('data-skin','creme');"
                         "document.documentElement.style.colorScheme='light'")
            shot(pg6, "7_login_creme_skin")
            pg6.fill("#name", "Жужа")
            shot(pg6, "8_login_creme_name")
            ctx6.close()

            # 7. мобильный вьюпорт
            ctx7, pg7 = new_page(viewport={"width": 390, "height": 844})
            pg7.fill("#name", "Жужа")
            pg7.click("#advKeys summary")
            shot(pg7, "9_login_mobile")
            ctx7.close()

            browser.close()
        print("ИТОГО: скриншоты в", OUT)
        return 0
    except Exception as e:
        print("FAIL:", e)
        return 1
    finally:
        relay.stop()


if __name__ == "__main__":
    sys.exit(main())
