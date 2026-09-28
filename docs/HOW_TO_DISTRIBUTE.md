# Как собрать .exe и раздать друзьям

Гайд по сборке Friend Relay в один .exe (или .zip), публикации на GitHub
и настройке автообновления.

## TL;DR

```bash
# На твоей машине (Windows, Python 3.11+):
pip install -r requirements.txt
pip install pyinstaller
build_exe.bat
# → dist\FriendRelay\FriendRelay.exe

# Запакуй dist\FriendRelay\ в ZIP
# Кинь ZIP друзьям — они распаковывают и запускают FriendRelay.exe
```

---

## Пошаговая инструкция

### 1. Установка зависимостей у себя

У тебя должен быть Python 3.11+ на Windows. Скачай с python.org если нет.

```bash
cd friend_relay_v5
pip install -r requirements.txt
```

Это поставит:
- `PySide6` — GUI-фреймворк (Qt для Python)
- `cryptography` — AES-256-GCM шифрование
- `sounddevice` — голосовой канал
- `numpy` — аудио-миксер

### 2. Сборка .exe

Дважды кликни `build_exe.bat` (или запусти `scripts\build_exe.bat`).

Скрипт сам:
1. Поставит `pyinstaller` если его нет
2. Поставит все зависимости из `requirements.txt` если их нет
3. Почистит старый билд (`build/`, `dist/`, `FriendRelay.spec`)
4. Запустит PyInstaller со всеми нужными флагами:
   - `--collect-all PySide6` — все Qt-плагины + переводы
   - `--collect-all sounddevice` — PortAudio-библиотеки
   - `--collect-submodules cryptography` — rust-биндинги
   - `--hidden-import _cffi_backend` — нужен cryptography на некоторых сборках
   - `--add-data "qt_app;qt_app"` — ресурсы UI
   - `--add-data "lib;lib"` — сервер + клиент

Результат: `dist\FriendRelay\FriendRelay.exe` (папка ~250 МБ из-за Qt).

### 3. Запаковка в ZIP для друзей

Друзьям нужен не один .exe, а вся папка `dist\FriendRelay\` — там рядом
с .exe лежат Qt-плагины, PortAudio-библиотеки и т.п.

```bash
# В проводнике Windows:
# 1. Зайди в dist\
# 2. Правый клик на FriendRelay\
# 3. Send to → Compressed (zipped) folder
# 4. Получится FriendRelay.zip (~80-100 МБ после сжатия)
```

Или через 7-Zip для лучшего сжатия (~60 МБ).

### 4. Раздача друзьям

3 пути:

#### Путь A: Прямая передача

- Telegram (до 2 ГБ)
- Яндекс.Диск / Google Drive / Dropbox (публичная ссылка)
- ZeroTier/Tailscale (расшарь папку в локальной сети)

#### Путь B: GitHub Releases (рекомендую)

Загрузи ZIP как релиз на GitHub — тогда можно настроить автообновление
(см. раздел 5).

1. Создай репозиторий на GitHub (например, `friend-relay`)
2. Залей исходники: `git push origin main`
3. Создай Release: `https://github.com/yourname/friend-relay/releases/new`
   - Tag: `v1.2.0`
   - Title: `Friend Relay 1.2.0`
   - Description: что нового (из `version.json` → notes)
   - Attach files: загрузи `FriendRelay.zip`
4. Скопируй URL download-ссылки (правый клик на файл в Release → Copy link)

#### Путь C: GitHub Pages (для статического манифеста)

Если хочешь автообновление — нужен манифест. GitHub Pages даёт бесплатный
HTTPS-хостинг для одного JSON-файла:

1. В репозитории создай папку `docs/`
2. Положи туда `version.json` (см. раздел 6)
3. Settings → Pages → Source: `main` / `docs`
4. Через минуту получишь URL: `https://yourname.github.io/friend-relay/version.json`

---

## 5. Автообновление

После первого релиза друзья один раз качают .exe. Дальше при каждом запуске
приложение само проверяет манифест и предлагает обновиться.

### Как это работает

1. В Настройках → «URL манифеста обновлений» юзер вводит URL к `version.json`
2. При запуске (если `check_updates: true`) или по кнопке «Проверить» — приложение
   качает `version.json` по URL
3. Если `version` в манифесте больше чем `APP_VERSION` (из `lib/constants.py`) —
   показывает диалог «Доступна новая версия»
4. Юзер жмёт «Скачать» → качается .exe/.zip по `download_url` → приложение
   само перезапускается с новой версией (через helper .bat)

### Что нужно сделать тебе (один раз)

1. Поднять `version.json` на GitHub Pages (см. путь C выше)
2. В `version.json` вписать **реальные** URL к .exe/.zip на GitHub Releases
3. Друзьям прислать URL манифеста — они его впишут в Настройках один раз

После этого обновления будут прилетать автоматически.

---

## 6. Формат `version.json`

```json
{
  "version": "1.2.0",
  "download_url": "https://github.com/yourname/friend-relay/releases/download/v1.2.0/FriendRelay-1.2.0.zip",
  "download_urls": [
    "https://github.com/yourname/friend-relay/releases/download/v1.2.0/FriendRelay-1.2.0.zip",
    "https://yourname.github.io/friend-relay/FriendRelay-1.2.0.zip"
  ],
  "labels": [
    "GitHub Releases",
    "GitHub Pages"
  ],
  "relay_file_id": "",
  "notes": "Что нового в этой версии..."
}
```

### Поля

| Поле | Обязательно | Описание |
|------|-------------|----------|
| `version` | ✅ | Версия в формате `X.Y.Z`. Сравнивается с `APP_VERSION` из `lib/constants.py` |
| `download_url` | ⚠️ | Старый формат — один URL. Если есть `download_urls`, это поле игнорируется |
| `download_urls` | ✅ | Список зеркал, пробуются по порядку. Можно мешать GitHub, Яндекс.Диск, свой сервер |
| `labels` | опц. | Подписи к URL (по индексу). Если не указано — угадывается по домену |
| `relay_file_id` | опц. | Если у вас есть хост в приватной сети (ZeroTier) и вы залили ZIP в его файловую витрину — укажите `file_id` (берётся из URL `/download/<file_id>` на экране «Файлы»). Тогда друзья, подключённые к этому хосту, качают обновление через него (быстро, без интернета) |
| `notes` | опц. | Что нового. Показывается в диалоге обновления |

### Шаблон

В репозитории есть `version.json` с заглушками `yourname` — замени на свои
реальные URL перед публикацией.

### Пример с ZeroTier

Если у тебя в ZeroTier-сети есть постоянно работающий хост (например
`100.123.45.67:8420`):

1. Поднимает Friend Relay как хост на этом адресе
2. Через экран «Файлы» загружаешь `FriendRelay-1.2.0.zip` в файловую витрину
3. Получаешь `file_id` из URL скачивания
4. В `version.json` добавляешь `"relay_file_id": "<file_id>"`
5. Друзья, подключённые к твоему хосту, в первую очередь качают обновление
   через ZeroTier (быстро, без интернета), и только если не получилось —
   откатываются на GitHub Releases

---

## 7. Чеклист перед релизом

Перед тем как публиковать новую версию:

- [ ] Поднять `APP_VERSION` в `lib/constants.py` (например, `1.2.0` → `1.3.0`)
- [ ] Обновить `version.json`:
  - [ ] `version` → новая версия
  - [ ] `download_urls` → новые URL (после загрузки на GitHub Releases)
  - [ ] `notes` → что нового
- [ ] Запустить `build_exe.bat` — убедиться что собирается без ошибок
- [ ] Тест: запустить `dist\FriendRelay\FriendRelay.exe` — работает
- [ ] Запаковать `dist\FriendRelay\` в ZIP
- [ ] Создать Release на GitHub, загрузить ZIP
- [ ] Обновить `version.json` на GitHub Pages
- [ ] Проверить: запустить старую версию → должно предложить обновиться

---

## Частые проблемы

### «Сборка падает с ImportError: _cffi_backend»

Поставь `cffi` явно:
```bash
pip install cffi
```

### «EXE запускается, но голосовой канал не работает»

PyInstaller не подхватил PortAudio. Пересобери с явным флагом:
```bash
py -3 -m PyInstaller --noconfirm --windowed --name FriendRelay ^
  --collect-all PySide6 ^
  --collect-all sounddevice ^
  --collect-submodules cryptography ^
  --hidden-import _cffi_backend ^
  --add-data "qt_app;qt_app" ^
  --add-data "lib;lib" ^
  main_qt.py
```

### «Автообновление не находит новую версию»

Проверь:
1. URL манифеста в Настройках — правильный, HTTPS, возвращает JSON
2. `version` в манифесте **больше** чем `APP_VERSION` в `lib/constants.py`
   (например, манифест `1.3.0`, текущая `1.2.0` → обновление есть)
3. `download_urls` — реально работают (открой в браузере)

### «Слишком большой ZIP (~100 МБ)»

Это нормально — Qt весит ~200 МБ. Варианты:
- 7-Zip вместо стандартного ZIP-архиватора (~60 МБ)
- Не собирать voice-канал (убрать `--collect-all sounddevice` и `numpy`) —
  сэкономит ~10 МБ, но голоса не будет
- Использовать NSIS-инсталлер (но это сложнее)
