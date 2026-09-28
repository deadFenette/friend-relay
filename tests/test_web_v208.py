#!/usr/bin/env python3
"""Живой тест веб-фич v2.0.8 + v3.5.9: Appearance Studio — панель
настраиваемых плагинов оформления (appearance.js + drawer + data-атрибуты
в style.css) и скины «Ночник»/«Крем-брюле» (nochnik.css/js).

Статические проверки: index.html содержит кнопку 🎨, разметку панели
(uxDrawer/uxOverlay/uxBody) и подключения appearance.js + nochnik.css/js;
appearance.js — реестр плагинов (8 + скин classic/nochnik/creme), пресеты,
применение через data-* и CSS-переменные; style.css — селекторы всех
плагинов и стили панели; nochnik.css — всё под html[data-skin]. Живые:
сервер отдаёт /static/appearance.js и /static/nochnik.* как статику,
старые элементы не потеряны, реестр /games цел (го на месте).
PySide6 этот слой не использует вовсе.
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read(), dict(r.headers)


def main() -> int:
    web = Path(__file__).resolve().parent.parent / "web_client"

    # ── 1. Статика: index.html ─────────────────────────────────────
    page = (web / "index.html").read_text(encoding="utf-8")
    check("index.html: кнопка Appearance Studio в шапке",
          'id="btnUx"' in page and "Оформление и плагины" in page)
    check("index.html: разметка панели (drawer + оверлей + тело)",
          'id="uxDrawer"' in page and 'id="uxOverlay"' in page
          and 'id="uxBody"' in page)
    check("index.html: пресеты и экспорт/импорт в футере",
          'id="uxPresetRich"' in page and 'id="uxPresetCalm"' in page
          and 'id="uxPresetReset"' in page and 'id="uxExport"' in page
          and 'id="uxImport"' in page)
    check("index.html: appearance.js подключён ДО main.js",
          page.index("static/appearance.js") < page.index("static/main.js"))
    check("index.html: скин «Ночник» подключён (css после style.css, "
          "js последним, после palette.js)",
          "static/nochnik.css" in page and "static/nochnik.js" in page
          and page.index("static/nochnik.css") > page.index("static/style.css")
          and page.index("static/nochnik.js") > page.index("static/palette.js"))
    check("index.html: toasts по-прежнему пустой контейнер",
          '<div id="toasts"></div>' in page)

    # ── 2. Статика: appearance.js ──────────────────────────────────
    js = (web / "appearance.js").read_text(encoding="utf-8")
    check("appearance.js: хранилище wr_ux + слияние с DEFAULTS",
          '"wr_ux"' in js and "Object.assign({}, DEFAULTS" in js)
    check("appearance.js: все 9 плагинов в реестре (8 + скин)",
          all(f'id: "{p}"' in js for p in
              ("skin", "accent", "type", "shape", "bg", "glass", "anim",
               "bubbles", "css")))
    check("appearance.js: скин — classic по умолчанию, сегмент "
          "classic/nochnik/creme, применение через data-skin",
          'skin: "classic"' in js and '{v: "nochnik", l: "Ночник"}' in js
          and '{v: "creme", l: "Крем-брюле"}' in js
          and "h.dataset.skin" in js)
    check("appearance.js: миграция studio4 → nochnik (старые сохранения)",
          'ux.skin === "studio4"' in js)
    check("appearance.js: пресеты не переключают скин (ка스кас — не косметика)",
          "vals, {skin: UX.skin" in js)
    check("appearance.js: палитра свотчей + свой цвет",
          "#6366f1" in js and "accentDerived" in js
          and 'type: "color"' in js)
    check("appearance.js: пресеты Дорого/Спокойно",
          "PRESET_RICH" in js and "PRESET_CALM" in js)
    check("appearance.js: применение через data-* на <html>",
          'h.dataset.density' in js and 'h.dataset.bgfx' in js
          and 'h.dataset.glass' in js and 'h.dataset.anim' in js
          and 'h.dataset.bubbles' in js and 'h.dataset.avatars' in js
          and 'h.dataset.font' in js)
    check("appearance.js: своя CSS живёт в <style#uxCustom>",
          'uxStyle.id = "uxCustom"' in js
          and "uxStyle.textContent" in js)
    check("appearance.js: сброс акцента возвращает цвета темы",
          "removeProperty" in js and "ACCENT_VARS" in js)

    # ── 3. Статика: style.css ──────────────────────────────────────
    css = (web / "style.css").read_text(encoding="utf-8")
    check("style.css: варианты плотности compact/roomy",
          'html[data-density="compact"]' in css
          and 'html[data-density="roomy"]' in css)
    check("style.css: фоны grid/dots/plain с интенсивностью",
          'html[data-bgfx="grid"]' in css and 'html[data-bgfx="dots"]' in css
          and 'html[data-bgfx="plain"]' in css and "calc(var(--bgfx-op)" in css)
    check("style.css: шрифтовые пакеты (rounded/serif/mono)",
          'html[data-font="rounded"]' in css
          and 'html[data-font="serif"]' in css
          and 'html[data-font="mono"]' in css)
    check("style.css: стекло off, анимации off, аватары off",
          'html[data-glass="off"]' in css and 'html[data-anim="off"]' in css
          and 'html[data-avatars="off"]' in css)
    check("style.css: стили баблов flat/outline",
          'html[data-bubbles="flat"]' in css
          and 'html[data-bubbles="outline"]' in css)
    check("style.css: панель, карточки, свитчи, сегменты, свотчи",
          ".ux-drawer" in css and ".uxcard" in css and ".uxswitch" in css
          and ".uxseg" in css and ".uxswatch" in css and ".uxcss" in css
          and ".ux-foot" in css)
    check("style.css: старые темы и компоненты на месте",
          'data-theme="lukewarm-ocean"' in css
          and 'data-theme="cherry-grove"' in css
          and ".gamecard" in css and ".pinpanel" in css)

    # ── 3б. Статика: nochnik.css / nochnik.js (скины v3.5.9) ──────
    nk_css = (web / "nochnik.css").read_text(encoding="utf-8")
    nk_js = (web / "nochnik.js").read_text(encoding="utf-8")
    check("nochnik.css: обе палитры по умолчанию + мост к темам",
          'html[data-skin="nochnik"]:not([data-theme])' in nk_css
          and 'html[data-skin="creme"]:not([data-theme])' in nk_css
          and "--nk-panel: var(--surface)" in nk_css)
    check("nochnik.css: каркас — комнаты/поток/сейчас, рейл скрыт",
          (".nk{" in nk_css.replace(" ", "")
           or (".nk{" in nk_css and "grid-template-areas" in nk_css)))
    check("nochnik.css: ключевые куски макета (бар, комнаты, карточки, "
          "уснул, палитра)",
          ".nk-medock" in nk_css and ".nk-card" in nk_css
          and ".nk-asleep" in nk_css and ".frpal-item" in nk_css
          and ".nk-day" in nk_css)
    check("nochnik.js: перенос панелей + возврат (home map, teardown)",
          "home = new Map()" in nk_js and "insertBefore(p, place.next)" in nk_js)
    check("nochnik.js: клик по человеку — сразу в переписку; скин "
          "только для nochnik/creme",
          "openDmWith" in nk_js and 's === "nochnik"' in nk_js
          and 's === "creme"' in nk_js)
    check("nochnik.js: честный экран «Свет погас» — только при затяжке",
          "LOST_OVERLAY_AFTER_MS" in nk_js and "nk-asleep" in nk_js)

    # ── 3в. v3.5.12: user-friendly боковых колонок «Ночника» ──────
    check("nochnik.css: карточки «Сейчас» сворачиваются РЕАЛЬНО "
          "(правило целилось в несуществующий .nk-body)",
          ".nk-card.closed .nk-cbody{display:none}" in nk_css
          and ".nk-card.closed .nk-body" not in nk_css)
    check("nochnik.css: секции колонки сворачиваются, раскрытая карточка "
          "не съедает колонку (свой скролл)",
          ".nk-sec.folded .nk-group{display:none}" in nk_css
          and ".nk-sec.folded .nk-lbl .nk-caret" in nk_css
          and "max-height:min(60vh" in nk_css.replace(" ", "")
          and "overscroll-behavior:contain" in nk_css)
    check("nochnik.css: ручки ширины колонок + сетка на переменных "
          "(шторки наследуют ширину)",
          ".nk-h-rooms" in nk_css and ".nk-h-now" in nk_css
          and "var(--nk-rooms-w," in nk_css and "var(--nk-now-w," in nk_css
          and "body.nk-drag" in nk_css)
    check("nochnik.js: секции с запоминанием + ручки ширины с запоминанием",
          "friendrelay.nkFold" in nk_js and "applyFold" in nk_js
          and "saveFold" in nk_js and "friendrelay.nkRoomsW" in nk_js
          and "friendrelay.nkNowW" in nk_js and "restoreColW" in nk_js
          and "setPointerCapture" in nk_js)
    check("nochnik.js: строка поиска — хоткей по платформе (Ctrl+K/⌘K), "
          "склеенная фраза убрана",
          "const KBD = " in nk_js and '"Ctrl+K"' in nk_js
          and "Куда перейти?…" not in nk_js
          and "Куда угодно: вкладка, канал, друг, тема…" in nk_js)
    check("palette.js: placeholder строки поиска чистый",
          "Куда перейти?…" not in (web / "palette.js")
          .read_text(encoding="utf-8"))
    check("nochnik.js: шторки закрываются Esc и кликом мимо",
          nk_js.count('classList.contains("peek")') >= 4
          and 'addEventListener("pointerdown"' in nk_js
          and "classList.remove(\"peek\")" in nk_js)

    # ── 4. Живой сервер ────────────────────────────────────────────
    tmp = Path(tempfile.mkdtemp(prefix="web_v208_"))
    relay = RelayServer(tmp, host_name="Host", access_key="",
                        max_file_size=10 * 1024 * 1024)
    port = 18553
    assert relay.start("127.0.0.1", port), "сервер не поднялся"
    base = f"http://127.0.0.1:{port}"
    try:
        code, body, _ = fetch(urllib.request.Request(base + "/ping",
            headers={"X-Relay-From": "Alice"}))
        check("/ping: сервер жив", code == 200
              and json.loads(body).get("ok") is True)

        code, body, hdrs = fetch(urllib.request.Request(base + "/"))
        page_http = body.decode("utf-8")
        check("GET /: страница содержит кнопку 🎨 и панель",
              code == 200 and 'id="btnUx"' in page_http
              and 'id="uxDrawer"' in page_http)

        code, body, hdrs = fetch(urllib.request.Request(
            base + "/static/appearance.js"))
        check("/static/appearance.js: отдаётся как javascript",
              code == 200
              and "javascript" in hdrs.get("Content-Type", "")
              and b"wr_ux" in body)

        code, body, hdrs = fetch(urllib.request.Request(
            base + "/static/nochnik.css"))
        check("/static/nochnik.css: отдаётся, правила под data-skin",
              code == 200 and "text/css" in hdrs.get("Content-Type", "")
              and b'html[data-skin="nochnik"]' in body
              and b'html[data-skin="creme"]' in body)
        code, body, hdrs = fetch(urllib.request.Request(
            base + "/static/nochnik.js"))
        check("/static/nochnik.js: отдаётся как javascript, каркас .nk",
              code == 200
              and "javascript" in hdrs.get("Content-Type", "")
              and b"nk-rooms" in body and b"nochnik" in body)

        code, body, _ = fetch(urllib.request.Request(
            base + "/static/style.css"))
        check("/static/style.css: отдаётся с новыми селекторами",
              code == 200 and b'.ux-drawer' in body
              and b'data-bubbles="outline"' in body)

        req = urllib.request.Request(base + "/games",
                                     headers={"X-Relay-From": "Alice"})
        code, body, _ = fetch(req)
        games = json.loads(body)
        ids = [g.get("id") for g in games.get("games", [])]
        check("/games: реестр цел, го по-прежнему первая",
              code == 200 and games.get("ok") is True
              and ids[:1] == ["go"] and "g2048" in ids
              and "minesweeper" in ids)
    finally:
        relay.stop()
        print(f"\nИтого: {PASS} OK / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
