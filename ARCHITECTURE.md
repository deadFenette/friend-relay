# Friend Relay — структура проекта

## Источник правды

**Актуальное приложение: `main_qt.py` (PySide6).**

Именно его запускают:
- `RUN.bat`
- `scripts/build_exe.bat`
- `build_exe.bat` (только короткий wrapper)

Поэтому если меняется пользовательский интерфейс, рабочей точкой является `qt_app/`.

## Что общее для UI

`lib/` — общий прикладной слой, используемый Qt-версией:
- `client.py` — HTTP-клиент (включая параллельную докачку файлов)
- `relay_server.py` — фасад application-слоя сервера (юзкейсы, композиция сервисов)
- `domain/` — ДОМЕН: `event_store.py` (журнал событий/дельты/DM-лог), `log_io.py` (JSONL-персистентность журнала), `presence.py` (онлайн/печатает)
- `server/` — ТРАНСПОРТ: `http_api.py` (HTTP-маршруты, разбор запросов, коды ответов), `tls_mux.py` (TLS и plain HTTP на одном порту), `tls_util.py` (самоподписанный сертификат)
- `link_preview.py` — сетевой сервис превью ссылок (SSRF-защита внутри)
- **`voice/` (в КОРНЕ проекта, с v2.0.0) — ГОЛОСОВОЙ МОДУЛЬ, самодостаточный
  пакет: ноль импортов из lib/** — только stdlib + numpy/sounddevice/
  opuslib_next/websockets. Остальное приложение consumes публичный API
  (`from voice import VoiceClient, VoiceMixer, VoiceBridge`):
  - `protocol.py` — проводной протокол FRVC (магия/флаги/кадры, 48кГц/20мс) — единый источник правды
  - `config.py` — ВСЕ тюн-константы голоса (джиттер-буферы, битрейт, гейты) в одном месте
  - `codec.py` — Opus VOIP 48кбит/с + inband FEC + честный PLC libopus; фолбэк на raw PCM
  - `dsp.py` — RMS, high-pass, soft gate (атака/спад), PLC-хвост, микширование sqrt(n)
  - `devices.py` — аудиоустройства sounddevice; СЕЙМ для тестов (подмена voice.devices.sd)
  - `mixer.py` — VoiceMixer: абсолютный такт (perf_counter, без дрейфа sleep),
    джиттер-буферы, opus-PLC, переговоры кодека
  - `bridge.py` — VoiceBridge: WS-мост браузер ↔ микшер (транскодирование opus↔PCM)
  - `client/engine.py` — VoiceClient: сеть + потоки sounddevice + публичный API v1.9.x
  - `client/capture.py` — MicChain: high-pass + soft gate микрофона
  - `client/playback.py` — PlaybackBuffer: адаптивный джиттер-буфер (пребуфер/catch-up)
- **`screen/` (в КОРНЕ проекта, с v3.6.0) — МОДУЛЬ ДЕМОНСТРАЦИИ ЭКРАНА,
  самодостаточный пакет: только stdlib, ноль импортов из lib/**.
  Картинка и звук экрана идут через WebRTC P2P НАПРЯМУЮ между браузерами
  (getDisplayMedia + RTCPeerConnection — стек встроен в Chromium, никакой
  библиотеки не нужно); сервер только РЕЛЕИТ служебный сигналинг.
  Публичный API: `from screen import ScreenHub, config`.
  - `hub.py` — ScreenHub: реестр живых показов (publish/heartbeat/unpublish,
    watch — для /screen/info) + сигналинг-почта (offer/answer/bye с seq-
    курсорами, TTL 90с, 256 писем/получатель, 256КБ/письмо); один Lock,
    ленивая уборка без фоновых потоков
  - `config.py` — ВСЕ константы (ливность STREAM_LIVE_S=30с, TTL, лимиты)
  - точки врезки в фасад: `RelayServer._screen` (создаётся в __init__ —
    без портов и потоков, упасть нечему), методы get_screen_info /
    screen_publish (анонсы в общий чат) / screen_signal / screen_poll /
    screen_watch; маршруты — GET /screen/info, GET /screen/poll
    (AUTH_REQUIRED_GET), POST /screen/publish|signal|watch в http_api.py;
    клиент — web_client/screen.js (панель #tab-screen; в скинах — карточка
    AMBIENT «Экран»)
- `bots/` — ВСЕ БОТЫ в одном пакете (с v2.0.1): `base.py` (каркас BaseBot),
  `manager.py` (BotManager: реестр, /dispatch, списки), игровые боты
  `chess.py` / `orbital.py` / `voxel.py` / `snake.py`, сервисный `night_shift.py`.
  ИИ-боты отделены от игровых движков: чистая логика живёт в `games/`,
  боты — только сетевые обёртки над ними
- `bots/music.py` — MusicBot (v3.3.0): синхронный плеер хоста. Библиотека —
  папка `<data_dir>/music` (по умолчанию) или своя (api_settings);
  стабильные ID треков = sha1(путь)[:16]; очередь с авторами; режим
  open_dj (управляют все) / закрытый (хост+админы); синхронизация
  клиентов по server_time/state_version; авто-переход треков из
  фонового цикла (mutagen опционален — иначе по отчётам /music/ended).
  Управление: веб-эндпоинты /music/* (см. http_api.py) и команды !music.
- `lib/music_library.py` (v3.3.0) — мост к ВНЕШНИМ библиотекам (задел
  на большое API): интерфейс MusicProvider + iTunes Search (без ключа,
  превью 30с) + Jamendo (нужен client_id). Конфиг `<data_dir>/music_api.json`.
  Поиск/импорт: /music/api/search и /music/api/import.
- `games/` — ЧИСТЫЕ игровые движки (без сети и Qt): `chess_engine.py`
  (правила + AI, используется ботом И Qt-диалогом), `orbital/`, `voxel/`
- `channel_manager.py`, `profile_manager.py`, `voxel_server.py`, `auth.py` — сервисы
- `storage.py` — настройки и локальные данные приложения (журналы чата тут больше не живут — они в домене)
- `crypto.py` — шифрование
- `json_fast.py` (с v2.0.2) — быстрый JSON: orjson (опционально) с
  фолбэком на stdlib; транспорт и EventStore сериализуют только через него
- `constants.py`, `formatters.py`, `file_kind.py`, `avatar.py`, `updater.py` и т.д.
- `util.py` — общие утилиты: header_encode/decode, safe_name, parse_int,
  clamp, MtimeFileCache (кеш статики веб-клиента с инвалидацией по mtime);
  с v2.0.4 — служебные структуры ядра: TTLCache (кеш с временем жизни,
  LRU-вытеснение — кеш превью ссылок), TokenBucket + RateLimiter
  (антифлуд /send_text и /dm/send, политика — фасад), LatencyMeter
  (латентность запросов для /server/stats), fmt_uptime

**Правило слоёв сервера (с v1.4.0):**

```
transport (lib/server/http_api.py)
    ↓ вызывает только публичный API фасада
application (lib/relay_server.py + сервисы)
    ↓
domain (lib/domain/)  ← чистая логика, без HTTP/Qt/сети
```

Обратные зависимости запрещены. Проверка: в `lib/domain` нет импортов
`http.*`/`PySide6`/`qt_app`/`lib.storage` (домен везёт свою JSONL-персистентность
в `log_io.py`); в `lib/` нет ни одного Qt-импорта (Qt-клиенты
игр живут в `qt_app/`); фасад не разбирает HTTP-запросы. Добавляя новый
эндпоинт, пиши хендлер `_post_*`/`_get_*` в `http_api.py` + строку в таблице
маршрутов, а логику — методом фасада или домена.

Qt-слой:
- `main_qt.py` — единственная актуальная точка входа
- `qt_app/main_window.py` — главное окно
- `qt_app/screens/` — экраны; экран чата (v1.9.9) разобран на:
  - `chat_screen.py` — тонкий координатор: события, поллинг, экшены
  - `chat_ui_builder.py` — вся разметка экрана чата (build_chat_ui + glass_btn)
  - `feed_pages.py` — страницы ленты и их LRU-кэш (FeedPage/FeedPageManager)
  - `chat_message_renderer.py` — фабрика баблов (общий чат И каналы)
  - `widgets/pinned_dialog.py` — диалог закреплённых сообщений
- `qt_app/widgets/` — виджеты
- `qt_app/voxel_client.py` — Qt-клиент воксельного мультиплеера (QObject + сигналы)
- `qt_app/theme.py` — стили Qt
- `qt_app/async_bridge.py` — фоновые операции/сигналы

## Что старое

`legacy/tkinter/` — старая Tkinter-версия, сохранённая для истории и аварийного сравнения.

Она **не входит в актуальный запуск и не используется сборкой**.

Её файлы:
- `relay_app.py` — старый entry point
- `app.py`, `chat_ui.py`, `chat_widgets.py`, `settings_ui.py`, `tabs_ui.py`
- `animated_mixin.py`, `theme.py`

Если старую версию больше никогда не понадобится, этот каталог можно удалить отдельным коммитом.

## Важное различие

Qt и Tkinter **не работают одновременно и не являются двумя половинами одного приложения**.

Они являются двумя UI над общим backend:
- Qt → `main_qt.py` → `qt_app/*` → `lib/*`
- legacy Tkinter → `legacy/tkinter/relay_app.py` → `legacy/tkinter/*` → `lib/*`

## Что делать разработчику

1. Запускать только `RUN.bat` или `python main_qt.py`.
2. Новые UI-фичи писать только в `qt_app/`.
3. Общую сетевую/серверную логику менять в `lib/`.
4. `legacy/tkinter/` не редактировать без специальной причины.
5. Сборку делать через `build_exe.bat` или `scripts/build_exe.bat`; оба ведут в один и тот же PySide6 build.

## Проверка после реорганизации

- Старый entry point больше не находится в корне.
- Root `RUN.bat` больше не запускает Tkinter.
- Обе build-команды собирают `main_qt.py`.
- Qt-код не импортирует legacy Tkinter UI.
- Legacy Tkinter остаётся изолированным каталогом.
