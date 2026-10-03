#!/usr/bin/env python3
"""v3.8.0 — «пак удобства»: поиск по всей истории, drag&drop файлов в чат,
ОС-уведомления.

Сервер (функционально, на временной папке):
  - EventStore.search_events: подстрока (регистр неважен), правки ищутся
    по АКТУАЛЬНОМУ тексту, удалённое не находится, зашифрованное честно
    пропускается, фильтр по автору, limit отдаёт ПОСЛЕДНИЕ совпадения;
  - ChannelManager.search_messages: поиск по каналам + фильтр;
  - RelayServer.search_history: слияние общий чат + каналы по глобальному seq.

Клиент (структурно):
  - GET /search зарегистрирован; панель #histPanel, автопоиск при
    открытии, подсветка .hl без innerHTML, чип канала .hchan;
  - ГЛУБОКИЙ прыжок: /events?before=seq+1 со СТРОГОЙ проверкой ответа
    (сервер /events не шлёт ok — заодно чиним loadOlderMessages, у
    которой !json.ok молча выбрасывал батч = подгрузка скроллом была
    сломана с v1.9.5);
  - drag&drop: #dropOverlay, document-хендлеры только для Files,
    uploadChatFile обобщён на любые файлы (картинки — прежний путь);
  - notify.js: permission только по жесту (включение звука), гейт
    document.hidden, dedup ЛС (osLastNotified), хук в addBubble.

Поведенческая проверка — браузерным E2E
(scripts/web_convenience_e2e.py, 20 проверок в Chromium).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail else ""))


def read(*parts) -> str:
    return (ROOT / Path(*parts)).read_text(encoding="utf-8")


def functional_block() -> None:
    print("\n-- функциональные: поиск на серверной стороне --\n")
    from lib.channel_manager import ChannelManager
    from lib.domain.event_store import EventStore

    tmp = Path(tempfile.mkdtemp(prefix="fr_test_v380_"))
    try:
        st = EventStore(tmp)
        e1 = st.add_text("Аня", "Привет, скинь отчёт по проекту")
        e2 = st.add_text("Боря", "отчёт уже готов, держи ОТЧЁТ.xlsx")
        e3 = st.add_file("Боря", "ОТЧЁТ.xlsx", b"x" * 32)
        e4 = st.add_text("Аня", "а погода как?")
        st.edit_text("Боря", e2["seq"], "отчёт переименован: итоги.xlsx")
        st.delete_event("Аня", e4["seq"])
        enc = st.add_text("Аня", "секретно", encrypted=True, iv="AAAA")

        r = st.search_events("отчёт")
        check("F1. регистронезависимый поиск по тексту", len(r) >= 2)
        check("F2. правка ищется по АКТУАЛЬНОМУ тексту",
              any(h["seq"] == e2["seq"] and "итоги" in h["text"] for h in r))
        check("F3. файл ищется по имени",
              any(h["kind"] == "file"
                  and h["file"]["name"] == "ОТЧЁТ.xlsx" for h in r))
        check("F4. удалённое не находится",
              not any(h["seq"] == e4["seq"] for h in r))
        check("F5. зашифрованное не находится (E2E — сервер не читает)",
              not any(h["seq"] == enc["seq"] for h in r))

        ra = st.search_events("отчёт", author="боря")
        check("F6. фильтр по автору (регистр автора неважен)",
              ra and all(h["from"] == "Боря" for h in ra))
        check("F7. запрос короче 2 символов — пусто",
              st.search_events("о") == [])
        lm = st.search_events("отчёт", limit=1)
        check("F8. limit отдаёт ПОСЛЕДНЕЕ совпадение по seq",
              len(lm) == 1 and lm[0]["seq"] == e3["seq"])

        ch = ChannelManager(tmp / "chans")
        ch.create_channel("тест-канал", "Аня")
        m1 = ch.send_channel_message("тест-канал", "Боря",
                                     "в канале тоже есть отчёт")
        ch.send_channel_message("тест-канал", "Аня", "пусто")
        rc = ch.search_messages("отчёт")
        check("F9. поиск по каналу с полем channel",
              len(rc) == 1 and rc[0]["channel"] == "тест-канал"
              and rc[0]["seq"] == m1["seq"])
        check("F10. фильтр автора в канале отсекает чужое",
              ch.search_messages("отчёт", author="аня") == [])

        # фасад через __new__ (без полной инициализации RelayServer)
        from lib.relay_server import RelayServer
        facade = RelayServer.__new__(RelayServer)
        facade._store = st
        facade._channels = ch
        out = facade.search_history("отчёт", limit=50)
        check("F11. фасад: слияние общего чата и каналов, сортировка по seq",
              out["total"] >= 3
              and out["results"] == sorted(out["results"],
                                           key=lambda x: x["seq"])
              and any("channel" in x for x in out["results"]))
        out2 = facade.search_history("отчёт", limit=2)
        check("F12. фасад: limit режет merged до последних",
              len(out2["results"]) == 2
              and out2["results"][-1]["seq"] == out["results"][-1]["seq"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def structural_block() -> None:
    print("\n-- структурные: клиент и транспорт --\n")
    http_api = read("lib", "server", "http_api.py")
    index = read("web_client", "index.html")
    search_js = read("web_client", "search.js")
    notify_js = read("web_client", "notify.js")
    chat = read("web_client", "chat.js")
    images = read("web_client", "images.js")
    dm = read("web_client", "dm.js")
    main_js = read("web_client", "main.js")
    style = read("web_client", "style.css")

    check("S1. GET /search зарегистрирован в маршрутах",
          '"/search": _get_search' in http_api)
    check("S2. /search: минимум 2 символа, автор, limit 10..100",
          "запрос короче 2 символов" in http_api
          and 'qs.get("author"' in http_api
          and "max(10, min(int(qs[\"limit\"][0]), 100))" in http_api)

    check("S3. index.html: кнопка #btnHist и панель #histPanel",
          'id="btnHist"' in index and 'id="histPanel"' in index)
    check("S4. панель: запрос+автор+кнопка, подсказка про E2E и ЛС",
          'id="histQ"' in index and 'id="histAuthor"' in index
          and 'id="btnHistRun"' in index and "E2E" in index
          and "приватны" in index)
    check("S5. search.js подключён ДО main.js (проводка кликов живёт там)",
          index.find("/static/search.js") < index.find("/static/main.js"))

    check("S6. панель закрывается, устаревший ответ отбрасывается",
          "histAbortInflight" in search_js and "HIST_REQ" in search_js)
    check("S7. автоповтор поиска при открытии панели",
          "runHistorySearch()" in search_js.split("function toggleHistPanel")[1]
          .split("}")[0])
    check("S8. данные результатов — только через textContent "
          "(innerHTML лишь на статических подсказках)",
          'className = "hl"' in search_js and "mark.textContent" in search_js
          and all("r." not in ln and "ev." not in ln
                  for ln in search_js.splitlines() if "innerHTML" in ln))
    check("S9. глубокий прыжок: before=seq+1 и СТРОГОЕ сравнение ok",
          "(r.seq + 1)" in search_js and "json.ok === false" in search_js)
    check("S10. канал: дождаться канального окна, не вставлять общий контекст",
          "await loadChannelMessages()" in search_js
          and "глубже канального окна" in search_js)

    _lom = chat.split("async function loadOlderMessages")[1][:700]
    check("S11. ФИКС loadOlderMessages: строгое сравнение ok",
          "json.ok === false" in _lom)
    check("S12. ОС-уведомление в addBubble: hidden + звук + сниппет",
          "osNotify(o.from" in chat and "notifyBeep()" in chat)
    check("S13. ЛС: dedup osLastNotified в dmBadgeTick",
          "osLastNotified" in dm and "osNotify(c.user" in dm)

    check("S14. drag&drop: document-хендлеры только для Files",
          all(x in images for x in ('"dragenter"', '"dragover"',
                                    '"dragleave"', '"drop"', '"Files"')))
    check("S15. drag&drop: guard видимости чата и сессии",
          "chatVisible" in images and "S.token" in images)
    check("S16. uploadChatFile обобщён, картинки — прежний путь",
          "async function uploadChatFile(" in images
          and "isImageName(file.name)" in images
          and "uploadChatImage(file)" in images
          and "uploadChatFiles(" in images)
    check("S17. оверлей #dropOverlay в разметке и CSS (pointer-events:none)",
          'id="dropOverlay"' in index and ".dropzone{" in style
          and "pointer-events:none" in style)
    check("S18. стили панели поиска на токенах тем",
          ".histrow" in style and ".hchan" in style and ".hl" in style
          and "var(--accent-soft)" in style)

    check("S19. notify.js: permission только по жесту, гейт hidden",
          "requestPermission" in notify_js and "document.hidden" in notify_js
          and "OSNOTIFY_KEY" in notify_js)
    check("S20. main.js: проводка панели + запрос permission на включении звука",
          'toggleHistPanel' in main_js and "osNotifyMaybeAsk" in main_js
          and 'wr_notify' in main_js)


def main() -> int:
    print("== v3.8.0: поиск по истории + drag&drop + ОС-уведомления ==\n")
    functional_block()
    structural_block()
    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
