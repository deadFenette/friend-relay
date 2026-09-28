"""screen — МОДУЛЬ ДЕМОНСТРАЦИИ ЭКРАНА Friend Relay v3.6.0.

САМОДОСТАТОЧНЫЙ пакет: ноль зависимостей от lib/ и qt_app/ — только
стандартная библиотека. По образцу пакета voice/: всё, что касается
стриминга экрана (сигналинг, реестр показов, конфиг), живёт здесь;
остальное приложение потребляет публичный API ниже.

КАК ЭТО РАБОТАЕТ (v3.6.0):
    Картинка и звук экрана идут ЧЕРЕЗ WebRTC P2P — напрямую от ведущего
    к зрителям, сервер НЕ прокачивает медиа. Библиотека не нужна:
    захват (getDisplayMedia с системным звуком) и кодирование (VP8/H264,
    аппаратное) делает сам браузер Chromium — это и есть самое
    открытое и бесплатное решение. Сервер только РЕЛЕИТ служебный
    сигналинг WebRTC (offer/answer SDP, пара служебных JSON) — тот же
    принцип, что у голосового чата (сигналинг через хост).

Поток данных:
    ведущий: getDisplayMedia → RTCPeerConnection на каждого зрителя
    зритель: «Смотреть» → hello → offer → answer → медиа P2P
    сервер:  POST /screen/signal (почта сообщений) + GET /screen/poll
             + GET /screen/info (кто сейчас показывает)

Публичный API:
    from screen import ScreenHub, STREAM_LIVE_S, MAX_SIGNAL_BYTES
    from screen import config

Структура:
    config.py   — ВСЕ константы (TTL, лимиты почты, имена)
    hub.py      — ScreenHub: реестр показов + сигналинг-почта
"""
from screen import config
from screen.config import (
    MAILBOX_MAX,
    MAX_SIGNAL_BYTES,
    SIGNAL_TTL_S,
    STREAM_LIVE_S,
)
from screen.hub import ScreenHub

__version__ = "1.0.0"

__all__ = [
    "MAILBOX_MAX",
    "MAX_SIGNAL_BYTES",
    "SIGNAL_TTL_S",
    "STREAM_LIVE_S",
    "ScreenHub",
    "__version__",
    "config",
]
