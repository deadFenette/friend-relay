#!/usr/bin/env python3
"""Визуальный аудит веб-клиента (полировка v3.6.9).

Поднимает реальный RelayServer, подключает двух Chromium-клиентов
(fake-mic), наполняет чат сообщениями (текст/файл/скриншот) и снимает
скриншоты ключевых состояний во вкладках чата и голоса, в двух темах.

Результат: /tmp/fr_visual/*.png — смотрим глазами и ищем, что полировать.
"""
from __future__ import annotations

import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.relay_server import RelayServer  # noqa: E402

OUT = Path("/tmp/fr_visual")
OUT.mkdir(exist_ok=True)

# валидный PNG 64x32 (сплошной оранжевый) — раньше тут был рукописный hex,
# который оказался битыми данными и вскрыл реальный edge case (см. v3.6.9)
def make_png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        raw = tag + data
        return (struct.pack(">I", len(data)) + raw +
                struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * w
    body = zlib.compress(row * h)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", body) + chunk(b"IEND", b""))


PNG = make_png(64, 32, (240, 120, 60))


def main() -> int:
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="fr_visual_"))
    base = 18540
    relay = RelayServer(tmp, host_name="Хост", access_key="k",
                        max_file_size=10 * 1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("сервер не стартовал")
        return 1

    def shot(page, name: str) -> None:
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
        print("shot:", name)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--force-color-profile=srgb",
                "--window-size=1180,760",
            ])
            ctx_a = browser.new_context(
                ignore_https_errors=True, viewport={"width": 1180, "height": 760},
                device_scale_factor=2)
            ctx_b = browser.new_context(
                ignore_https_errors=True, viewport={"width": 1180, "height": 760},
                device_scale_factor=2)
            a = ctx_a.new_page()
            b = ctx_b.new_page()

            def join(page, name):
                page.goto(f"https://127.0.0.1:{base}/", wait_until="domcontentloaded")
                page.fill("#name", name)
                # v3.7.1: ключи переехали в спойлер #advKeys
                page.click("#advKeys summary")
                page.fill("#key", "k")
                page.click("#btnConnect")
                page.wait_for_selector("#app:not(.hidden)", timeout=15000)

            join(a, "Алиса")
            join(b, "Боб")
            time.sleep(0.5)

            # --- чат: несколько сообщений с обеих сторон ---
            a.evaluate("""() => {
              const inp = document.querySelector('#msgText');
              const btn = document.querySelector('#btnSend');
              inp.value = 'Привет! Смотри какой закат поймал вчера 🌇';
              btn.click();
            }""")
            time.sleep(0.3)
            b.evaluate("""() => {
              const inp = document.querySelector('#msgText');
              const btn = document.querySelector('#btnSend');
              inp.value = 'Ого, огонь! А у нас сегодня дождь весь день **и туман**.';
              btn.click();
            }""")
            time.sleep(0.3)
            a.evaluate("""() => {
              const inp = document.querySelector('#msgText');
              const btn = document.querySelector('#btnSend');
              inp.value = 'Скинул скрин, глянь (`вложение` ниже):  1080→1440 масштаб норм?';
              btn.click();
            }""")
            time.sleep(0.3)

            # скриншот от Б через настоящий input (по кнопке-скрепке)
            b.evaluate("""() => { const i = document.querySelector('#imgInput');
                                 i && i.closest('.hidden')?.classList.remove('hidden'); }""")
            b.set_input_files("#imgInput", [{"name": "sunset.png",
                                            "mimeType": "image/png",
                                            "buffer": PNG}])
            time.sleep(1.2)

            shot(a, "01_chat_aurora")

            # --- v3.6.9: состояние скелетона (подвешиваем fetch в странице) ---
            a.evaluate("""() => {
              const of = window.fetch;
              window.__origFetch = of;
              window.__hang = true;
              window.fetch = (u, o) =>
                (window.__hang && String(u).includes('/download/'))
                  ? new Promise(() => {}) : of(u, o);
            }""")
            a.set_input_files("#imgInput", [{"name": "slow.png",
                                            "mimeType": "image/png",
                                            "buffer": PNG}])
            a.wait_for_selector("#feed .imgprev img.img-wait", timeout=15000)
            time.sleep(0.8)   # поймать фазу шиммера
            shot(a, "01b_imgwait_skeleton")
            a.evaluate("() => { window.__hang = false; }")

            # --- v3.6.9: аккуратный фейл ---
            a.evaluate("""() => {
              const of = window.__origFetch;
              window.__break = true;
              window.fetch = (u, o) =>
                (window.__break && String(u).includes('/download/'))
                  ? Promise.reject(new Error('net drop')) : of(u, o);
            }""")
            a.set_input_files("#imgInput", [{"name": "broken.png",
                                            "mimeType": "image/png",
                                            "buffer": PNG}])
            a.wait_for_selector("#feed .imgprev img.img-fail", timeout=15000)
            shot(a, "01c_imgfail")
            a.evaluate("() => { window.__break = false; "
                       "window.fetch = window.__origFetch; }")
            time.sleep(1.5)   # обе картинки успевают прогрузиться

            # --- тема Snow (светлая) ---
            a.evaluate("() => applyTheme('snow', false)")
            time.sleep(0.3)
            shot(a, "02_chat_snow")

            # --- лайтбокс поверх чата (ждём готовое превью) ---
            a.wait_for_selector("#feed .imgprev img.img-ready", timeout=15000)
            a.click("#feed .imgprev img.img-ready")
            a.wait_for_selector(".lightbox", timeout=5000)
            time.sleep(0.6)   # анимация появления
            shot(a, "03_lightbox_snow")
            a.evaluate("() => document.querySelector('.lb-x')?.click()")
            time.sleep(0.3)

            # --- вкладка голоса, не подключены ---
            a.evaluate("""() => document.querySelector('.tabs button[data-tab="voice"]').click()""")
            time.sleep(0.3)
            shot(a, "04_voice_disconnected")

            # --- вкладка голоса, подключены ---
            a.evaluate("""() => document.querySelector('.tabs button[data-tab="voice"]').click()""")
            a.click("#btnVoice")
            a.wait_for_function("() => VC.on === true && !!VC.playPort", timeout=15000)
            b.evaluate("""() => document.querySelector('.tabs button[data-tab="voice"]').click()""")
            b.click("#btnVoice")
            b.wait_for_function("() => VC.on === true && !!VC.playPort", timeout=15000)
            time.sleep(2.5)  # fake-mic говорит → «говорит» подсветка
            shot(a, "05_voice_connected")

            # --- профиль/панель ---
            a.evaluate("""() => document.querySelector('.tabs button[data-tab="profile"]').click()""")
            time.sleep(0.3)
            shot(a, "06_profile_snow")

            browser.close()
    finally:
        relay.stop()

    print("Готово:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
