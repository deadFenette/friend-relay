# План: тесты синхронизации музыки + VPN/Zerotier + адресов

## Repository Research (выводы исследования кода)

### Музыкальная синхронизация (архитектура)
Единый источник правды — **MusicBot** в [lib/bots/music.py](file:///g:/Downloads/friend_relay_v3.4.1_servergui/lib/bots/music.py).
Состояние отдаётся через `api_state()` (и легаси `get_sync_state()`) как:
```
playing (bool) · track (dict|None) · position (сек, округл. до 0.01)
server_time (time.time()) · state_version (↑ при любом изменении)
queue (list) · open_dj (bool) · volume (0-100)
```
Клиенты получают это через HTTP `GET /music/sync` в [http_api.py:1763](file:///g:/Downloads/friend_relay_v3.4.1_servergui/lib/server/http_api.py#L1763-L1774).

- **Позиция** считается на сервере ЛАЙВО: `pause_pos + (time.time() - _started_at)` при `playing=True` (метод `_position_locked`).
- **state_version** растёт на каждое play/pause/seek/change (`_bump_locked`).
- Web-клиент [music.js](file:///g:/Downloads/friend_relay_v3.4.1_servergui/web_client/music.js) опрашивает `/music/sync` каждые 2.5с, вычисляет skew = clientTime − serverTime, экстраполирует позицию по age = `serverNow − st.server_time`. При смене version — перерисовывает немедленно.
- Qt GUI-сервера [server_gui.py](file:///g:/Downloads/friend_relay_v3.4.1_servergui/server_gui.py) вызывает `bot.api_state()` напрямую (в том же процессе).
- Qt-клиент (host-mode в qt_app/screens/settings_screen.py) стартует RelayServer локально.

### Сетевые адреса и VPN+ZT
Единый источник правды по интерфейсам — **`lib/net_utils.get_interfaces()`** (WinAPI + WSAIoctl на Windows), возвращает `list[InterfaceInfo(ip, kind, name)]` где kind:
`loopback | zerotier | tailscale | physical | vpn`.

Публикация адресов друзьям:
- **server_gui.py** [_fill_invite (L1371)](file:///g:/Downloads/friend_relay_v3.4.1_servergui/server_gui.py#L1371-L1437): для каждого non-loopback интерфейса → http:// + https:// + тег вида (ZT✨ TS✨ physical VPN❗) + copy-кнопка.
- **server_main.py** [print_hint (L125)](file:///g:/Downloads/friend_relay_v3.4.1_servergui/server_main.py#L125-L185): то же самое + теги в print (ZT✨ TS✨ LAN VPN❗).
- **relay_server.start (L1035)** [lib/relay_server.py](file:///g:/Downloads/friend_relay_v3.4.1_servergui/lib/relay_server.py#L1035-L1069): диагностика bind-ДО открытия сокета. Два предупреждения: конфликт ZT+VPN (fix bat), и «bind на конкретный IP при наличии ZT — друзья по overlay не зайдут».
- Порты: `HTTP=port`, `голос=port+1`, `voxel=port+2`. HTTPS = тот же port через TLS-мультиплексор.

**Выявленные инконсистентности/риски:**
1. **Qt-клиент settings_screen** (_host_ips_label) использует старый `get_local_ips()` (простой сокет-метод) вместо `get_interfaces()` — НЕТ пометок ZT/VPN.
2. **`/voice/info` endpoint** (relay_server.get_voice_info) возвращает `"host": self.host_name` (имя хоста, НЕ IP) — клиенты подменяют реальный host из base_url. Это документировано в докстринге, но хрупко.

## Files and Modules
- **НОВЫЙ ФАЙЛ**: `tests/test_music_sync_and_net_vpn_v341.py` — единый тест-файл, покрывающий оба блока (как просил пользователь, один файл в /tests).
- (Не планируется править код приложения в рамках этого плана — ТОЛЬКО тесты и документация несоответствий. Если тесты выявят реальные падения — после согласования исправляем.)

## Implementation Steps
1. **Блок А. Статические проверки консистентности всех клиентов/серверов**
   - А1: 5 entry-point (server_gui / server_main / main_qt / web_client/index.html / relay_server) подключают/используют music+net правильными функциями.
   - А2: Во всех entry-points используется порт-формула HTTP/HTTPS=port, voice=port+1, voxel=port+2.
   - А3: В server_gui._fill_invite и server_main.print_hint ОДИНАКОВЫЙ набор scheme (http+https) и теги видов интерфейсов.
   - А4: web_client/index.html содержит вкладку музыки (tab button, music.js, music/sync URL-константы).
   - А5: lib/relay_server.py содержит музыкальные фасад-методы (music_state, music_play, music_pause, music_seek, music_sync_state и т. д. — 12 методов).
   - А6: main_qt.py (Qt-клиент) корректно цепляет screens → base_url.

2. **Блок Б. Unit-тесты MusicBot (синхронизация)**
   - Б1: api_state() всегда возвращает нужные ключи (playing, track, position, server_time, state_version, queue, open_dj, volume).
   - Б2: state_version инкрементится на каждое действие (play, pause, seek, skip, add_queue).
   - Б3: Позиция растёт когда `playing=True` и стоит на месте когда `False` (проверка time.sleep(0.05) + допуск).
   - Б4: position округляется до сотых (API-инвариант — клиенты полагаются на формат).
   - Б5: `get_sync_state()` легаси возвращает None при отсутствии трека (а не пустое состояние).
   - Б6: Права open_dj=True → всем можно play/pause; open_dj=False → только host_name/админы.
   - Б7: Авто-переход: текущий трек закончился → бот переключает на голову очереди + version++; (моделируем конец трека вызовом api_ended).

3. **Блок В. Живой HTTP-сервер + консистентность адресов/VPN**
   - В1: `GET /music/sync` возвращает ok=True + структуру как api_state(); после play/pause version меняется.
   - В2: `GET /voice/info` возвращает порты: port (HTTP) → voice=port+1, https_port=port (TLS-мультиплексор). host-name поле заполнено.
   - В3: RelayServer.start("127.0.0.1", N) стартует на портах N, N+1 (voice), N+2 (voxel) — порты заняты (проверка через is_running + get_voice_port).
   - В4: Консистентность адресов server_gui._build_invite_card() формирует URL вида scheme://IP:port для каждого не-loopback интерфейса (импорт функции и статическая проверка текста функции).
   - В5: detect_vpn_zt_conflict() не падает, возвращает (bool, str) — даже без ZT (False, "") нормально.
   - В6: get_interfaces() возвращает только валидные InterfaceInfo: kind в разрешённом наборе, ip валидный IPv4/IPv6.
   - В7: Все клиенты (web music.js + http_api) используют ОДИНАКОВЫЙ путь /music/sync, один poll-механизм.

4. **Блок Г. Кросс-проверка «два клиента видят одно и то же»**
   - Г1: Два последовательных GET /music/sync (эмуляция двух пользователей) при старте = одинаковое состояние, одинаковые positions (с tiny-drift допуском).
   - Г2: После вызова play() на сервере → оба следующих GET видят playing=True и state_version+1.
   - Г3: При playing=True разница position между двумя GET через Δt ≈ Δt (± погрешность сна) — доказательство монотонной синхронности.

## Dependencies and Considerations
- Все тесты должны работать БЕЗ звукового оборудования и БЕЗ PySide6 в среде CI (каждое Qt-имя подменяем заглушкой при необходимости как в test_voice_api_v201).
- Для живого сервера порт выбираем случайный выше 18xxx (как в других тестах: 185xx).
- Задержки (sleep) делаем минимальными (0.05–0.1с) с допусками, чтобы тест не был флаки.
- `get_interfaces()` на разных машинах возвращает разный набор. Тестируем ИНВАРИАНТЫ (типы, форматы URL) + fallback на `local_ips()` когда пусто.

## Validation
1. `python tests\test_music_sync_and_net_vpn_v341.py` — exit code 0, FAIL == 0.
2. `ruff check tests\test_music_sync_and_net_vpn_v341.py` — All checks passed.
3. Прогон `ruff check .` ещё раз целиком — убедиться, что новые файлы зелёные.

## Risks
- **Флаки из-за time.sleep:** все численные проверки с щедрыми допусками (×2-×5 от сна). При необходимости прогон через статистическое усреднение.
- **Отсутствие mutagen:** MusicBot работает и без него; тесты НЕ проверяют метаданные треков, только state/синхронизацию.
- **Занятый порт:** используем retry-loop на выбор свободного порта или фиксируем 18541/18561 как другие тесты.
- **PySide6 импорты в settings_screen/main_qt:** статический анализ через grep/чтение текста файла без выполнения.
