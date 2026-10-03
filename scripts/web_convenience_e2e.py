#!/usr/bin/env python3
"""E2E v3.8.0 «пак удобства» в настоящем Chromium (Playwright).

Три фичи против РЕАЛЬНОГО сервера и РЕАЛЬНОГО web_client:

  ПОИСК ПО ИСТОРИИ (GET /search + search.js):
    1. кнопка #btnHist открывает панель;
    2. короткий запрос вежливо отказывается (минимум 2 символа);
    3. поиск находит сообщение Боба в общем чате (регистр не важен);
    4. фильтр по автору работает (чужой автор -> пусто);
    5. чепуха -> честное «ничего не нашлось»;
    6. ГЛУБОКИЙ ПРЫЖОК: сообщение, уехавшее за пределы ленты (210
       филлеров после него), находится и открывается в ленте ДОКАЧКОЙ
       контекста (before=seq+1), бабл подсвечивается (.flash);
    7. сообщение из канала находится, у результата чип #канала, клик
       открывает канал и прыгает к баблу.

  DRAG&DROP ФАЙЛОВ (images.js):
    8. dragenter с файлами показывает оверлей #dropOverlay;
    9. drop .txt -> тост «Файл отправлен», file-бабл в ленте у обеих
       сторон (обычное file-событие /send_file);
   10. drop .png -> бабл с превью (.imgprev).

  ОС-УВЕДОМЛЕНИЯ (notify.js):
   11. включение звука колокольчиком при предвыданном permission
       включает wr_osnotify;
   12. скрытая вкладка + новое сообщение -> window.__frLastNotify
       (имя автора, сниппет);
   13. активная вкладка НЕ уведомляет (попапы только в скрытой);
   14. новое ЛС -> ОС-уведомление от собеседника (dmBadgeTick);
   15. ни одной JS-ошибки на обеих страницах.

Запуск: python scripts/web_convenience_e2e.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac as pyhmac
import json
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.relay_server import RelayServer  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# Headless Chromium ВСЕГДА держит Notification.permission = "denied"
# (grant_permissions не помогает) — стабим целиком: проверяем ЛОГИКУ
# notify.js (включение, гейт по document.hidden, содержимое попапа),
# а не браузерный permission-пайплайн.
NOTIFY_STUB = """
(() => {
  class FakeNotification {
    constructor(title, opts){
      window.__frNotifications = window.__frNotifications || [];
      window.__frNotifications.push({title, body: opts && opts.body,
        tag: opts && opts.tag});
    }
    close(){}
  }
  FakeNotification.permission = "granted";
  FakeNotification.requestPermission = () => Promise.resolve("granted");
  window.Notification = FakeNotification;
})();
"""

SEND = """async (text) => {
  const {json} = await apiPost("/send_text", {text});
  return json && json.ok;
}"""

_SSL = ssl._create_unverified_context()


def _req(port, path, method="GET", headers=None, body=None, timeout=10):
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}",
                                 method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = body.encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, data=data, timeout=timeout,
                                context=_SSL) as r:
        return json.loads(r.read().decode("utf-8"))


def phantom_fillers(port: int, total: int, key: str = "k") -> int:
    """Наполняет журнал обычными сообщениями от ТРЁХ фантомных сессий.

    Зачем не браузер: антифлуд (30 burst + 5/с на имя) сделал бы 210
    отправок через одного клиента полуминутной вознёй. Три легальные
    сессии (/ping -> token, подпись HMAC-SHA256(token, body) — та же
    схема, что у apiPost в core.js) дают ~15 сообщений/с и честно
    уважают token-bucket: при 429 просто ждём.

    Возвращает сколько реально ушло."""
    names = ["Phantom-1", "Phantom-2", "Phantom-3"]
    tokens = {}
    for n in names:
        j = _req(port, "/ping", headers={"X-Relay-From": n,
                                        "X-Relay-Key": key})
        if not j.get("session_token"):
            raise RuntimeError("phantom ping failed: " + str(j))
        tokens[n] = j["session_token"]
    tokens_left = {n: 30.0 for n in names}
    last = {n: time.monotonic() for n in names}
    sent = i = 0
    while sent < total:
        moved = False
        for n in names:
            now = time.monotonic()
            tokens_left[n] = min(30.0,
                                 tokens_left[n] + (now - last[n]) * 5.0)
            last[n] = now
            if tokens_left[n] < 1.0:
                continue
            body = json.dumps(
                {"text": f"филлер {i}: погода, игры и погода в играх"},
                ensure_ascii=False)
            sig = pyhmac.new(tokens[n].encode(), body.encode(),
                             hashlib.sha256).hexdigest()
            try:
                j = _req(port, "/send_text", method="POST", headers={
                    "X-Relay-From": n, "X-Relay-Key": key,
                    "X-Relay-Auth": f"{tokens[n]}:{sig}"}, body=body)
            except Exception:
                j = {}
            if j.get("ok"):
                sent += 1
                i += 1
                tokens_left[n] -= 1.0
                moved = True
            else:
                time.sleep(0.2)
        if not moved:
            time.sleep(0.3)
    return sent

DROP = """({name, mime, bytesB64}) => {
  const bytes = Uint8Array.from(atob(bytesB64), c => c.charCodeAt(0));
  const file = new File([bytes], name, {type: mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  const fire = (type) => {
    const ev = new DragEvent(type, {bubbles: true, cancelable: true});
    Object.defineProperty(ev, "dataTransfer", {value: dt});
    document.dispatchEvent(ev);
  };
  fire("dragenter");
  fire("dragover");
  const shown = !document.querySelector("#dropOverlay").classList
    .contains("hidden");
  fire("drop");
  const hiddenAfter = document.querySelector("#dropOverlay").classList
    .contains("hidden");
  return {shown, hiddenAfter};
}"""

HIDE = """(hidden) => {
  Object.defineProperty(document, "hidden",
    {configurable: true, get: () => hidden});
  return document.hidden;
}"""


def connect_page(page, name: str, port: int) -> None:
    page.goto(f"https://127.0.0.1:{port}/", wait_until="domcontentloaded")
    page.fill("#name", name)
    page.click("#advKeys summary")   # v3.7.1: ключи в спойлере
    page.fill("#key", "k")
    page.click("#btnConnect")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)


def main() -> int:
    print("== E2E v3.8.0: поиск по истории + drag&drop + ОС-уведомления ==\n")
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="wr_convenience_e2e_"))
    base = 18790
    relay = RelayServer(tmp, host_name="Хост", access_key="k",
                        max_file_size=1024 * 1024)
    if not relay.start("127.0.0.1", base):
        print("  [FAIL] сервер не стартовал (порт занят?)")
        return 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ])
            try:
                ctx_a = browser.new_context(ignore_https_errors=True)
                ctx_b = browser.new_context(ignore_https_errors=True)
                # ОС-уведомления: стаб вместо permission-пайплайна
                ctx_b.add_init_script(NOTIFY_STUB)
                a = ctx_a.new_page()
                b = ctx_b.new_page()
                errs: list[str] = []
                for pg in (a, b):
                    pg.on("pageerror", lambda e, _p=pg: errs.append(
                        f"{_p}: {e}"))

                print("  -- подключение Алисы и Боба --")
                connect_page(a, "Алиса", base)
                connect_page(b, "Боб", base)
                a.wait_for_function("() => !!S.token", timeout=10000)
                b.wait_for_function("() => !!S.token", timeout=10000)
                check("0. оба подключены", True)

                # ── ПОИСК: лёгкие проверки ────────────────────────────
                print("  -- поиск по истории --")
                ok1 = a.evaluate(SEND,
                    "встреча в пятницу в 19:00, не забудь")
                ok2 = b.evaluate(SEND,
                    "Созвон по проекту перенесли на четверг")
                a.wait_for_function(
                    "() => [...document.querySelectorAll('#feed .txt')]"
                    ".some(t => t.textContent.includes('Созвон'))",
                    timeout=8000)
                check("1. тестовые сообщения ушли и приехали в ленту",
                      ok1 and ok2)

                b.click("#btnHist")
                check("2. #btnHist открывает панель поиска",
                      b.evaluate("() => !document.querySelector('#histPanel')"
                                 ".classList.contains('hidden')"))
                b.fill("#histQ", "с")
                b.click("#btnHistRun")
                stat = b.text_content("#histStat") or ""
                check("3. короткий запрос вежливо отказан",
                      "2 символа" in stat, stat.strip())

                b.fill("#histQ", "созвон")
                b.click("#btnHistRun")
                b.wait_for_selector("#histList .histrow", timeout=8000)
                rows = b.evaluate(
                    """() => [...document.querySelectorAll('#histList .histrow')]
                         .map(r => r.querySelector('.pwho').textContent)""")
                check("4. «созвон» найден, регистр не важен",
                      len(rows) >= 1 and rows[0] == "Боб", str(rows))

                b.fill("#histQ", "созвон")
                b.fill("#histAuthor", "алиса")
                b.click("#btnHistRun")
                b.wait_for_function(
                    "() => (document.querySelector('#histStat')"
                    ".textContent || '').includes('ничего')", timeout=8000)
                check("5. фильтр по автору отсеял чужое", True)

                b.fill("#histAuthor", "")
                b.fill("#histQ", "щщщ_нет_такого_нигде")
                b.click("#btnHistRun")
                b.wait_for_function(
                    "() => (document.querySelector('#histStat')"
                    ".textContent || '').includes('ничего')", timeout=8000)
                check("6. чепуха -> честное «ничего не нашлось»", True)

                # ── ПОИСК: глубокий прыжок ────────────────────────────
                print("  -- глубокий прыжок в историю --")
                sent = phantom_fillers(base, 205)
                check("7. 205+ филлеров от трёх фантомов "
                      "(цель уехала из ленты)", sent >= 205,
                      f"отправлено {sent}")
                # Боб уходит в канал: лента общего чата сбрасывается
                a.evaluate(
                    """async () => {
                         await apiPost("/channel/create", {name: "деловой"});
                         return true;
                       }""")
                b.evaluate("() => openChannel('деловой')")
                b.wait_for_function(
                    "() => document.querySelector('#feed')"
                    ".textContent.includes('в канале пока пусто')",
                    timeout=8000)
                b.evaluate("() => toggleHistPanel(true)")
                b.fill("#histQ", "встреча в пятницу")
                b.click("#btnHistRun")
                b.wait_for_selector("#histList .histrow", timeout=8000)
                rows = b.evaluate(
                    """() => [...document.querySelectorAll('#histList .histrow')]
                         .filter(r => !r.querySelector('.hchan')).length""")
                check("8. цель найдена, у результата нет чипа канала",
                      rows >= 1, f"строк без канала: {rows}")
                target_before = b.evaluate(
                    "() => !!document.querySelector('#feed "
                    "[data-seq]')")
                b.click("#histList .histrow")
                flashed = b.wait_for_function(
                    """() => {
                         const el = document.querySelector('#feed .msg.flash');
                         return el ? el.dataset.seq : null;
                       }""", timeout=8000)
                seq = flashed.json_value() if hasattr(flashed, "json_value") \
                    else flashed
                b.wait_for_function(
                    "() => [...document.querySelectorAll('#feed .txt')]"
                    ".some(t => t.textContent.includes('встреча в пятницу'))",
                    timeout=5000)
                check("9. глубокий прыжок: докачали контекст, бабл с .flash",
                      bool(seq) and not target_before, f"seq={seq}")

                # ── ПОИСК: канал ──────────────────────────────────────
                print("  -- поиск по каналу --")
                a.evaluate(
                    """async () => {
                         await apiPost("/channel/send",
                           {channel: "деловой",
                            text: "квартальный отчёт обсуждаем в канале"});
                         return true;
                       }""")
                b.evaluate("() => openChannel('деловой')")
                b.wait_for_function(
                    "() => document.querySelector('#feed')"
                    ".textContent.includes('квартальный')", timeout=8000)
                b.evaluate("() => toggleHistPanel(true)")
                b.fill("#histQ", "квартальный")
                b.click("#btnHistRun")
                # ждём именно ЧИП: старые строки от автопоиска содержали
                # «встреча» без канала — селектор .histrow срабатывал бы
                # мгновенно, и чип оценивался бы по устаревшему ответу
                b.wait_for_function(
                    "() => !!document.querySelector('#histList .hchan')",
                    timeout=8000)
                chip = b.evaluate(
                    """() => {
                         const r = document.querySelector('#histList .histrow');
                         return r && r.querySelector('.hchan')
                           ? r.querySelector('.hchan').textContent : "";
                       }""")
                check("10. результат из канала помечен чипом #канала",
                      chip == "#деловой", chip)
                b.click("#histList .histrow")
                b.wait_for_function(
                    "() => !!document.querySelector('#feed .msg.flash')",
                    timeout=8000)
                check("11. клик по канальному результату: канал открыт, "
                      "бабл подсвечен", True)

                # ── DRAG&DROP ─────────────────────────────────────────
                print("  -- drag&drop файлов --")
                b.evaluate("() => openChannel('')")   # вернуться в общий чат
                b.wait_for_function(
                    "() => document.querySelectorAll('#feed [data-seq]').length > 100",
                    timeout=8000)
                r = b.evaluate(DROP, {
                    "name": "док.txt", "mime": "text/plain",
                    "bytesB64": base64.b64encode("привет".encode()).decode(),
                })
                check("12. dragenter показывает оверлей, drop прячет",
                      r["shown"] and r["hiddenAfter"])
                a.wait_for_function(
                    "() => [...document.querySelectorAll('#feed .msg.file')]"
                    ".some(m => m.textContent.includes('док.txt'))",
                    timeout=10000)
                b.wait_for_function(
                    "() => [...document.querySelectorAll('#feed .msg.file')]"
                    ".some(m => m.textContent.includes('док.txt'))",
                    timeout=10000)
                check("13. drop .txt -> file-бабл у ОБОИХ сторон", True)

                r = b.evaluate(DROP, {
                    "name": "картинка.png", "mime": "image/png",
                    "bytesB64": base64.b64encode(PNG_1PX).decode(),
                })
                a.wait_for_function(
                    """() => [...document.querySelectorAll('#feed .msg.file')]
                          .some(m => m.textContent.includes('картинка.png')
                                     && m.querySelector('.imgprev'))""",
                    timeout=10000)
                check("14. drop .png -> бабл с превью (.imgprev)", True)

                # ── ОС-УВЕДОМЛЕНИЯ ────────────────────────────────────
                print("  -- ОС-уведомления --")
                b.evaluate(
                    "() => localStorage.setItem('wr_notify', '0')")
                b.click("#btnNotify")       # включаем звук -> ask permission
                b.wait_for_function(
                    "() => localStorage.getItem('wr_osnotify') === '1'",
                    timeout=8000)
                check("15. колокольчик включил wr_osnotify "
                      "(permission предвыдан)", True)

                b.evaluate(HIDE, True)
                a.evaluate(SEND, "Боб, ты где? Уведомление проверяем")
                b.wait_for_function(
                    "() => window.__frLastNotify && "
                    "window.__frLastNotify.title === 'Алиса'", timeout=10000)
                body = b.evaluate("() => window.__frLastNotify.body")
                check("16. скрытая вкладка: ОС-уведомление с именем "
                      "автора и сниппетом",
                      "уведомление проверяем" in body.lower(), body[:60])

                b.evaluate(HIDE, False)
                a.evaluate(SEND, "а теперь ты видишь ленту - попапа не будет")
                b.wait_for_function(
                    "() => [...document.querySelectorAll('#feed .txt')]"
                    ".some(t => t.textContent.includes('попапа не будет'))",
                    timeout=10000)
                body2 = b.evaluate("() => window.__frLastNotify.body")
                check("17. активная вкладка НЕ уведомляет",
                      body2 == body, "last notify не перезаписался")

                b.evaluate(HIDE, True)
                a.evaluate(
                    """async () => {
                         await apiPost("/dm/send",
                           {to: "Боб", text: "личное: проверка уведомлений"});
                         return true;
                       }""")
                b.wait_for_function(
                    """() => window.__frLastNotify &&
                         window.__frLastNotify.tag !== undefined &&
                         window.__frLastNotify.title === 'Алиса' &&
                         document.querySelector('.tabs button[data-tab="dm"]'
                           + ' .badge')""",
                    timeout=20000)
                check("18. новое ЛС -> ОС-уведомление + бейдж на вкладке",
                      True)

                check("19. ни одной JS-ошибки на обеих страницах",
                      not errs, "; ".join(errs[:3]))
            finally:
                browser.close()
    finally:
        relay.stop()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
