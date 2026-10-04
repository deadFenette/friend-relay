#!/usr/bin/env python3
"""v3.6.8 — скриншоты и картинки в веб-чате: загрузка, превью, лайтбокс.

Проверяется в НАСТОЯЩЕМ Chromium (Playwright) против реального сервера:
  1. Кнопка «скриншот» в композере чата: выбор файла → POST /send_file →
     file-событие в ленте.
  2. Превью: бабл файла-картинки содержит .imgprev img (картинка докачана,
     класс img-ready) у ОБОИХ клиентов (отправителя и собеседника).
  3. Сервер отдал по /download ровно те же байты.
  4. Лайтбокс поверх чата: клик по превью открывает оверлей; картинка
     в нём; Esc закрывает; повторное открытие — крестик закрывает;
     клик по фону закрывает.
  5. Зум: клик по картинке — ~3x; колесо мыши — меняет масштаб; кнопка
     «−» возвращает к 100%.
  6. Ctrl+V: синтетический paste с картинкой в буфере уходит как файл.
  7. Темы: в светлой теме (snow) фон лайтбокса СВЕТЛЫЙ (свойство
     подхватывается из CSS-переменной --lb-backdrop), в тёмной Aurora —
     тёмный.
  8. Не-картинка (.txt) превью НЕ получает.

Запуск: python tests/test_web_images_v368.py
"""
from __future__ import annotations

import base64
import json
import ssl
import struct
import sys
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.relay_server import RelayServer  # noqa: E402
from lib.util import header_encode  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


def make_png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """Минимальный валидный PNG без внешних библиотек (Truecolor RGB)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        raw = tag + data
        return (struct.pack(">I", len(data)) + raw +
                struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * w
    body = zlib.compress(row * h)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", body) + chunk(b"IEND", b""))


PASTE_HELPER = """
async () => {
  const b64 = arguments_0 && null;
  return null;
}
"""


def connect_page(page, name: str, port: int) -> None:
    page.goto(f"https://127.0.0.1:{port}/", wait_until="domcontentloaded")
    page.fill("#name", name)
    # v3.7.1: ключи переехали в спойлер #advKeys (дружелюбный экран входа)
    page.click("#advKeys summary")
    page.fill("#key", "k")
    page.click("#btnConnect")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)


def wait_img_ready(page, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = page.evaluate(
            "() => document.querySelectorAll('#feed .imgprev img.img-ready').length")
        if n > 0:
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    print("== v3.6.8: скриншоты в веб-чате — превью + лайтбокс ==\n")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # (CI-fix) В «безголовом» наборе CI playwright не ставится: CI должен
        # быть быстрым и детерминированным, браузер там не качается. Браузерные
        # E2E гоняются локально/на релизе. Раннер (run_all_tests.py) помечает
        # этот файл SKIP по TEST_NEEDS, а прямой запуск без playwright
        # честно завершается нулём вместо ModuleNotFoundError.
        print("  [SKIP] playwright не установлен — браузерный E2E пропущен "
              "(локально: pip install playwright && playwright install chromium)")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="wr_img_"))
    base = 18550
    png_small = make_png(24, 18, (220, 70, 40))
    (tmp / "скрин тест.png").write_bytes(png_small)
    relay = RelayServer(tmp, host_name="Хост", access_key="k",
                        max_file_size=1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("  [FAIL] сервер не стартовал (порт занят?)")
        return 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx_a = browser.new_context(ignore_https_errors=True)
                ctx_b = browser.new_context(ignore_https_errors=True)
                a = ctx_a.new_page()
                b = ctx_b.new_page()
                errs: list[str] = []
                for pg in (a, b):
                    pg.on("pageerror", lambda e: errs.append(str(e)))

                connect_page(a, "Алиса", base)
                connect_page(b, "Боб", base)

                # ── 1. загрузка через кнопку композера ────────────────
                check("1. кнопка и input в композере есть",
                      a.evaluate("() => !!document.getElementById('btnImg') && "
                                 "!!document.getElementById('imgInput')"))
                a.set_input_files("#imgInput", str(tmp / "скрин тест.png"))
                # ждём file-событие у обоих
                ok_a = wait_img_ready(a)
                ok_b = wait_img_ready(b)
                check("2. превью появилось в ленте отправителя", ok_a)
                check("3. превью появилось в ленте собеседника", ok_b)

                # ── 2. байты целы на сервере ──────────────────────────
                uctx = ssl._create_unverified_context()
                req = urllib.request.Request(
                    f"https://127.0.0.1:{base}/events?since=0&tail=50",
                    headers={"X-Relay-From": header_encode("Хост"),
                             "X-Relay-Key": "k"})
                events = json.loads(urllib.request.urlopen(
                    req, context=uctx, timeout=5).read()).get("events", [])
                file_ev = next((e for e in events if e.get("kind") == "file"),
                               None)
                got_bytes_ok = False
                if file_ev:
                    req2 = urllib.request.Request(
                        f"https://127.0.0.1:{base}/download/{file_ev['file_id']}",
                        headers={"X-Relay-From": header_encode("Хост"),
                                 "X-Relay-Key": "k"})
                    got_bytes_ok = (urllib.request.urlopen(
                        req2, context=uctx, timeout=5).read() == png_small)
                check("4. /download отдаёт ровно те же байты PNG", got_bytes_ok)

                # ── 3. лайтбокс: Esc, крестик, фон ────────────────────
                b.click("#feed .imgprev img")
                b.wait_for_selector(".lightbox", timeout=5000)
                lb_img_src = b.evaluate(
                    "() => document.querySelector('.lightbox .lb-stage img').src")
                check("5. лайтбокс открыт поверх чата (оверх, картинка внутри)",
                      lb_img_src.startswith("blob:"))
                check("6. имя файла в шапке лайтбокса",
                      "скрин тест" in b.evaluate(
                          "() => document.querySelector('.lb-name').textContent"))
                b.keyboard.press("Escape")
                check("7. Esc закрывает лайтбокс",
                      b.evaluate("() => !document.querySelector('.lightbox')"))

                b.click("#feed .imgprev img")
                b.wait_for_selector(".lightbox", timeout=5000)
                b.click(".lb-x")
                check("8. крестик закрывает лайтбокс",
                      b.evaluate("() => !document.querySelector('.lightbox')"))

                b.click("#feed .imgprev img")
                b.wait_for_selector(".lightbox", timeout=5000)
                b.mouse.click(400, 450)   # фон вокруг картинки
                check("9. клик по фону закрывает лайтбокс",
                      b.evaluate("() => !document.querySelector('.lightbox')"))

                # ── 4. зум ─────────────────────────────────────────────
                b.click("#feed .imgprev img")
                b.wait_for_selector(".lightbox", timeout=5000)
                b.click(".lb-stage img")
                s1 = b.evaluate(
                    "() => document.querySelector('.lb-stage img').style.transform")
                b.hover(".lb-stage img")
                b.mouse.wheel(0, -240)
                s2 = b.evaluate(
                    "() => document.querySelector('.lb-stage img').style.transform")
                b.click("button[title*='Уменьшить']")
                z = b.evaluate("() => document.querySelector('.lb-zoom').textContent")
                check("10. клик по картинке увеличивает (~3x)",
                      "scale(3" in s1, s1)
                check("11. колесо мыши меняет масштаб", s1 != s2)
                check("12. индикатор масштаба живёт и уменьшается",
                      z.endswith("%") and z != "300%" and z != "100%",
                      z)
                b.keyboard.press("Escape")

                # ── 5. Ctrl+V — синтетический paste с картинкой ───────
                before = a.evaluate(
                    "() => document.querySelectorAll('#feed .imgprev').length")
                a.evaluate("""() => {
                  const bytes = Uint8Array.from(atob(
                    'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAF0lEQVR4nGNg' +
                    'YGBgYGRgYGBgYGBgYGBgAAMKAAW/4P0AAAAAAElFTkSuQmCC'), c => c.charCodeAt(0));
                  const f = new File([bytes], 'paste shot.png', {type: 'image/png'});
                  const dt = new DataTransfer();
                  dt.items.add(f);
                  const ev = new ClipboardEvent('paste', {clipboardData: dt,
                                                          bubbles: true});
                  document.dispatchEvent(ev);
                }""")
                pasted = False
                deadline = time.time() + 10
                while time.time() < deadline:
                    now = a.evaluate(
                        "() => document.querySelectorAll('#feed .imgprev').length")
                    if now > before:
                        pasted = True
                        break
                    time.sleep(0.3)
                check("13. Ctrl+V со скриншотом в буфере уходит как файл", pasted)

                # ── 6. не-картинка без превью ──────────────────────────
                (tmp / "note.txt").write_text("это не картинка", encoding="utf-8")
                b.set_input_files("#imgInput", str(tmp / "note.txt"))
                time.sleep(1.5)
                warns = b.evaluate("() => document.getElementById('chatWarn').textContent")
                check("14. .txt отвергается с понятным предупреждением",
                      "картинк" in warns, warns[:60])

                # ── 7. темы лайтбокса ─────────────────────────────────
                def lb_bg(page) -> str:
                    page.click("#feed .imgprev img")
                    page.wait_for_selector(".lightbox", timeout=5000)
                    col = page.evaluate(
                        "() => getComputedStyle("
                        "document.querySelector('.lightbox')).backgroundColor")
                    page.keyboard.press("Escape")
                    page.evaluate("() => !document.querySelector('.lightbox')")
                    return col

                dark = lb_bg(b)
                b.evaluate("() => applyTheme('snow', false)")
                light = lb_bg(b)
                check("15. фон лайтбокса в Aurora тёмный",
                      dark.startswith("rgba(6, 7, 10") or dark.startswith("rgba(6,7,10"),
                      dark)
                check("16. фон лайтбокса в Snow светлый",
                      light.startswith("rgba(240, 242, 246") or light.startswith("rgba(240,242,246"),
                      light)

                # ── 8. ЛС: превью приватной картинки (/dm_download) ──────
                # дружим клиентов, открываем ЛС, шлём картинку в диалог
                a.evaluate("() => apiPost('/friends/add', {name: 'Боб'})")
                b.evaluate("() => apiPost('/friends/add', {name: 'Алиса'})")
                a.evaluate("() => activateTab('dm')")
                a.wait_for_function(
                    "() => [...document.querySelectorAll('#dmPeer option')]"
                    ".some(o => o.value === 'Боб')", timeout=10000)
                a.select_option("#dmPeer", "Боб")
                a.set_input_files("#dmFileInput", str(tmp / "скрин тест.png"))
                dm_ok = False
                deadline = time.time() + 12
                while time.time() < deadline:
                    n = a.evaluate(
                        "() => document.querySelectorAll"
                        "('#dmFeed .imgprev img.img-ready').length")
                    if n > 0:
                        dm_ok = True
                        break
                    time.sleep(0.3)
                check("17. ЛС: превью приватной картинки докачано (dm_download)",
                      dm_ok)
                a.click("#dmFeed .imgprev img")
                a.wait_for_selector(".lightbox", timeout=5000)
                a.keyboard.press("Escape")
                check("18. ЛС: лайтбокс открывается и закрывается по Esc",
                      a.evaluate("() => !document.querySelector('.lightbox')"))

                # ── 9. v3.6.9: скелетон при загрузке и аккуратный фейл ───
                # подвешиваем /download у A ВНУТРИ страницы (висящий
                # Promise — без playwright-routes, не оставляет хвостов):
                # превью должно остаться img-wait (скелетон), а НЕ
                # «сломанной картинкой»
                (tmp / "медленный.png").write_bytes(make_png(30, 20, (40, 160, 90)))
                a.evaluate("""() => {
                  const of = window.fetch;
                  window.__origFetch = of;
                  window.__hang = true;
                  window.fetch = (u, o) =>
                    (window.__hang && String(u).includes('/download/'))
                      ? new Promise(() => {})          // висим
                      : of(u, o);
                }""")
                a.set_input_files("#imgInput", str(tmp / "медленный.png"))
                slow_seen = False
                deadline = time.time() + 10
                while time.time() < deadline:
                    st = a.evaluate("""() => {
                      const im = document.querySelector(
                        '#feed .imgprev img.img-wait');
                      return im ? {src: im.src.slice(0, 15), n: 1}
                                : {n: 0};
                    }""")
                    if st["n"]:
                        slow_seen = st["src"].startswith("data:image/gif")
                        break
                    time.sleep(0.2)
                check("19. пока превью качается — скелетон (прозрачная "
                      "заглушка, не «сломанная картинка»)", slow_seen)
                a.evaluate("() => { window.__hang = false; }")   # отпускаем новые

                # «обрываем» /download у A — аккуратный фейл с честным тултипом
                (tmp / "битый.png").write_bytes(make_png(30, 20, (90, 60, 200)))
                a.evaluate("""() => {
                  const of = window.__origFetch;
                  window.__break = true;
                  window.fetch = (u, o) =>
                    (window.__break && String(u).includes('/download/'))
                      ? Promise.reject(new Error('net drop'))
                      : of(u, o);
                }""")
                a.set_input_files("#imgInput", str(tmp / "битый.png"))
                fail_state = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    fail_state = a.evaluate("""() => {
                      const im = document.querySelector(
                        '#feed .imgprev img.img-fail');
                      return im ? {alt: im.alt, title: im.title,
                                   src: im.src.slice(0, 15), n: 1} : {n: 0};
                    }""")
                    if fail_state["n"]:
                        break
                    time.sleep(0.2)
                check("20. ошибка сети — аккуратный фейл: пустой alt, "
                      "тултип «не загрузилось», заглушка вместо иконки",
                      bool(fail_state and fail_state.get("n")
                           and fail_state["alt"] == ""
                           and "не загрузилось" in fail_state["title"]
                           and fail_state["src"].startswith("data:image/gif")),
                      str(fail_state))
                # восстанавливаем сеть
                a.evaluate("""() => {
                  window.__break = false;
                  window.fetch = window.__origFetch;
                }""")

                # тот же файл у B (без перехвата) грузится нормально:
                # состояния независимы по file_id. Меряем прирост готовых
                # превью: до этой секции у B уже были img-ready (п.2 и п.13)
                base_ready = b.evaluate(
                    "() => document.querySelectorAll"
                    "('#feed .imgprev img.img-ready').length")
                b_ok = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    now_ready = b.evaluate(
                        "() => document.querySelectorAll"
                        "('#feed .imgprev img.img-ready').length")
                    if now_ready >= base_ready + 2:   # медленный + битый
                        b_ok = True
                        break
                    time.sleep(0.2)
                check("21. у собеседника те же картинки грузятся нормально "
                      "(состояния независимы)", b_ok,
                      f"было {base_ready}")

                # битые данные с именем .png — сервер честно отдаёт байты,
                # но декодировать их нельзя: должны получить аккуратный
                # фейл (onerror), а НЕ «сломанную картинку» браузера
                fail_before = b.evaluate(
                    "() => document.querySelectorAll"
                    "('#feed .imgprev img.img-fail').length")
                (tmp / "мусор.png").write_bytes(b"\x00\x01\x02 not a png")
                b.set_input_files("#imgInput", str(tmp / "мусор.png"))
                junk_fail = False
                deadline = time.time() + 10
                while time.time() < deadline:
                    n = b.evaluate(
                        "() => document.querySelectorAll"
                        "('#feed .imgprev img.img-fail').length")
                    if n > fail_before:
                        junk_fail = True
                        break
                    time.sleep(0.2)
                check("22. битые данные с именем .png — аккуратный фейл, "
                      "не «сломанная картинка»", junk_fail)

                # клик по битому превью: лайтбокс открывается-не-удалось —
                # сам закрывается и объясняет тостом (файл остаётся доступен)
                b.locator("#feed .imgprev img.img-fail").last.click()
                toast_seen = False
                deadline = time.time() + 6
                while time.time() < deadline:
                    t = b.evaluate(
                        "() => [...document.querySelectorAll('.toast')]"
                        ".map(x => x.textContent).join(' | ')")
                    if "не удалось" in t:
                        toast_seen = True
                        break
                    time.sleep(0.15)
                lb_gone = b.evaluate("() => !document.querySelector('.lightbox')")
                check("23. клик по битому превью — лайтбокс не зависает: "
                      "закрылся сам с тостом-объяснением",
                      toast_seen and lb_gone, t[:60])

                check("24. на страницах нет JS-ошибок", not errs,
                      "; ".join(errs)[:140])
            finally:
                browser.close()
    finally:
        relay.stop()

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
