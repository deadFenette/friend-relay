from pathlib import Path

APP_TITLE = "Своя раздача — чат и файлы"
APP_VERSION = "1.2.0"
PROTOCOL_VERSION = 1

DEFAULT_PORT = 8420
DATA_DIR = Path.home() / ".friend_relay"
SETTINGS_FILE = DATA_DIR / "settings.json"

# -- то, что живёт на стороне хоста (relay_server.py) -----------------------
HOST_DATA_DIR_NAME = "relay_data"  # <server_dir>/relay_data/
HISTORY_FILE_NAME = "history.jsonl"  # лог событий (текст+объявления о файлах)
FILES_DIR_NAME = "files"  # сами байты присланных файлов

EVENT_KIND_TEXT = "text"
EVENT_KIND_FILE = "file"
EVENT_KIND_DELETE = "delete"  # удаление сообщения/файла по target_seq
EVENT_KIND_SYSTEM = "system"  # "Вася зашёл в чат" и т.п. - на будущее
EVENT_KIND_EDIT = "edit"  # правка текста по target_seq
EVENT_KIND_REACTION = "reaction"  # эмодзи-реакция по target_seq
EVENT_KIND_PIN = "pin"  # закрепление/открепление по target_seq
EVENT_KIND_LINK_PREVIEW = (
    "link_preview"  # превью ссылки подъехало по target_seq (см. _fetch_and_attach_preview)
)

# Дельта-события, которые не самостоятельные сообщения, а правки уже
# существующих (по target_seq) - переигрываются поверх истории при загрузке.
DELTA_EVENT_KINDS = (EVENT_KIND_EDIT, EVENT_KIND_REACTION, EVENT_KIND_PIN, EVENT_KIND_LINK_PREVIEW)

# Не даём случайно залить и забить диск хоста одним файлом - можно поднять
# в настройках хоста при желании (теперь и прямо в UI, не только тут).
DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 ГБ

# -- антифлуд сообщений (v2.0.4) ---------------------------------------------
# Токен-ведро на отправителя (lib/util.TokenBucket, политика в
# RelayServer.allow_message) для /send_text и /dm/send. Числа с запасом на
# живого человека: 30 сообщений подряд мгновенно (всплеск) и 5 в секунду
# восстановления - переписка друзей не задевается никогда, а скрипт,
# шлющий сотни сообщений в секунду, начинает получать 429 и не жжёт CPU
# хоста, диск JSONL и сеть поллящих клиентов. Гуглится в /server/stats
# (поле flood_buckets).
FLOOD_MSG_RATE = 5.0   # сколько «сообщений» в секунду восстанавливается
FLOOD_MSG_BURST = 30   # размер ведра - мгновенный всплеск без отказа

# Кто считается "сейчас на связи": клиент помечает себя при каждом опросе
# /events (см. HEADER_FROM ниже), если за это время не было ни одного опроса
# от имени - считаем, что вышел/закрыл программу.
PRESENCE_TIMEOUT_SECONDS = 8
TYPING_TIMEOUT_SECONDS = (
    5  # сколько секунд после последнего сигнала "печатает" считаем, что человек ещё печатает
)

# -- аватарки -------------------------------------------------------------
AVATARS_DIR_NAME = "avatars"
AVATAR_SIZE = 96  # канонический размер, который все загружают
AVATAR_CHAT_SUBSAMPLE = 4  # 96/4 = 24px в строке чата - целое число, чтобы
# PhotoImage.subsample давал точный размер без блюра
AVATAR_PREVIEW_SUBSAMPLE = 1  # в настройках показываем как есть, крупно
MAX_AVATAR_UPLOAD_SIZE = 2 * 1024 * 1024  # 2МБ с запасом, это же просто фото профиля

# -- производительность чата ----------------------------------------------
# Не даём списку виджетов в чате расти бесконечно за недели переписки -
# старые сообщения просто убираются из ОТОБРАЖЕНИЯ (не с хоста), лента не
# резиновая на тысячи виджетов.
MAX_RENDERED_ROWS = 400
CHAT_TAIL_ON_CONNECT = 60  # при первом подключении не рендерим всю историю
# разом, только хвост - не подвешиваем окно

FILE_PREVIEW_MAX_BYTES = 8 * 1024 * 1024  # не тянем гигантские фото для превью
FILE_PREVIEW_MAX_WIDTH = 220

POLL_INTERVAL_MS = 2000  # как часто спрашивать хоста "что нового"
POLL_RETRY_BACKOFF_MS = (
    5000  # если хост не ответил (тоннель моргнул) - подождать и попробовать снова
)
REQUEST_TIMEOUT = 15
CHUNK_SIZE = 1024 * 256
PROGRESS_UPDATE_EVERY_BYTES = 512 * 1024  # не дёргать UI на каждый чанк, раз в ~0.5МБ достаточно

# -- производительность UI и сервера ----------------------------------------
# Не выгребаем всю очередь UI-событий за один проход after() - иначе при
# всплеске (история при подключении, несколько сообщений разом) окно перестаёт
# отвечать, пока все не обработаем. Батчим по несколько штук, между батчами
# даём Tkinter перерисовать окно через after(0).
MAX_UI_EVENTS_PER_TICK = 14
# Сколько последних событий держим в памяти на сервере, чтобы не перечитывать
# JSONL-файл целиком на каждый /events poll (в тысячах пользователей
# достаточно скромных чисел, для чата друзей - с запасом).
EVENT_CACHE_LIMIT = 5000

HEADER_FROM = "X-Relay-From"
HEADER_FILENAME = "X-Relay-Filename"
HEADER_TO = "X-Relay-To"  # получатель для приватной отправки файла в ЛС (см. /send_dm_file)
HEADER_KEY = "X-Relay-Key"  # необязательный общий "пароль" раздачи, см. README
HEADER_SHA256 = "X-Relay-SHA256"  # hex-дайджест содержимого файла; сервер сверяет и хранит в событии

HOLDERS_FILE_NAME = "holders.json"  # реестр "кто скачал какой файл" (фундамент multi-source)
FILE_HOLDERS_MAX = 200  # сколько имён максимум хранить на файл - хватит с запасом

# Параллельная докачка (клиент, lib/client.py)
PARALLEL_THRESHOLD_BYTES = 8 * 1024 * 1024  # меньше - не паримся, качаем одним потоком
PARALLEL_MIN_CHUNK = 4 * 1024 * 1024        # кусок мельче этого не имеет смысла
DEFAULT_CONNECTIONS = 4                     # одновременных Range-соединений
CHUNK_RETRIES = 2                           # перезапросов упавшего куска, прежде чем сдаться

# -- профили пользователей ------------------------------------------------
PROFILES_DIR_NAME = "profiles"  # <server_dir>/relay_data/profiles/{name}.json
MAX_BIO_LENGTH = 280  # как в Twitter, коротко и по делу
MAX_STATUS_LENGTH = 60  # одна строка "На связи" / "В игре" и т.п.
MAX_DISPLAY_NAME_LENGTH = 32  # отображаемое имя (может отличаться от логина)
MAX_FRIENDS_COUNT = 200  # верхний предел списка друзей на пользователя
GAME_HISTORY_LIMIT = 50  # сколько последних игр хранить на пользователя

# -- музыка (MusicBot) ----------------------------------------------------
MUSIC_DIR_NAME = "music"  # директория для музыки на сервере
MUSIC_SUPPORTED_FORMATS = {".mp3", ".flac", ".ogg", ".wav", ".m4a"}
MUSIC_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 МБ макс. размер трека
MUSIC_SYNC_INTERVAL_MS = 5000  # как часто рассылать sync-сигналы (5 сек)
