"""Социальное ядро — профиль, друзья, файлы, DM (Фаза 3 выделения UI).

Переехало дословно, поведение 1:1, из трёх экранов:

  - qt_app/screens/profile_settings.py — рендер карточки «Статистика
    шахмат» из leaderboard (поиск себя + дефолты «ещё не играли»),
    счётчики символов bio/status с warn-порогом, формат «На сервере с»,
    лимит размера аватарки, интервалы поллинга друзей/статистики;
  - qt_app/screens/files_screen.py — фильтрация витрины по типу файла,
    текст счётчика «N / M файлов», тексты пустых состояний, политика
    «можно ли удалить файл», папка назначения для скачивания;
  - qt_app/screens/dm_screen.py — превью последнего сообщения в списке
    диалогов, группировка ленты DM по отправителю, политика «можно ли
    отправить», интервал поллинга.

Здесь нет сессионного состояния — поллинг друзей/статистики/DM остаётся
QTimer'ами экранов (там нет политики наложения запросов, как в чате),
поэтому модуль состоит из чистых функций и констант. Всё это
тестируется без PySide6 (tests/test_social_core.py).

Дешифровка DM-сообщений живёт в chat_session.decrypt_text_of — экран
ЛС использует ту же функцию, что и общий чат (раньше плейсхолдер
дублировался в двух местах).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from lib.constants import MAX_DISPLAY_NAME_LENGTH
from lib.file_kind import file_kind

# ── Константы (переехали из экранов) ────────────────────────────────────────

# profile_settings: QTimer обновления друзей (online-статус)
FRIENDS_POLL_INTERVAL_MS = 10000
# profile_settings: QTimer обновления статистики (ELO меняется после партий)
STATS_POLL_INTERVAL_MS = 30000
# dm_screen: поллинг диалогов и активного чата
DM_POLL_INTERVAL_MS = 2000
# profile_settings._upload_avatar: максимум для аватарки
AVATAR_MAX_BYTES = 2 * 1024 * 1024
# dm_screen.ConversationItem: обрезка последнего сообщения в списке диалогов
DM_PREVIEW_LIMIT = 40


# ── Профиль: статистика игр ─────────────────────────────────────────────────


@dataclass
class MyStats:
    """Строка «Статистика шахмат» — готовые к показу значения.

    Если своего имени в leaderboard нет (ещё не играли) — found=False и
    дефолты, ровно как раньше рисовал _render_stats: ELO «1200», место «—»,
    счётчики нулевые.
    """

    found: bool
    elo: str = "1200"
    rank: str = "—"
    wins: str = "0"
    losses: str = "0"
    draws: str = "0"


def my_stats_from_leaderboard(scores: list[dict], my_name: str) -> MyStats:
    """Ищет себя в leaderboard (первое совпадение по name) и собирает карточку.

    Порядок в списке = место в рейтинге, поэтому enumerate с 1 — это rank.
    """
    my_entry: dict | None = None
    my_rank: int | None = None
    for i, s in enumerate(scores, start=1):
        if s.get("name") == my_name:
            my_entry = s
            my_rank = i
            break

    if my_entry is None:
        # Не играли ещё — дефолты
        return MyStats(found=False)

    return MyStats(
        found=True,
        elo=str(my_entry.get("score", 1200)),
        rank=f"#{my_rank}" if my_rank else "—",
        wins=str(my_entry.get("wins", 0)),
        losses=str(my_entry.get("losses", 0)),
        draws=str(my_entry.get("draws", 0)),
    )


# ── Профиль: bio/status/аватар ──────────────────────────────────────────────


def counter_state(text: str, max_len: int) -> tuple[str, bool]:
    """Счётчик «N / MAX» под полем bio/status + warn-порог.

    warn=True, когда текст достиг лимита (у QLineEdit setMaxLength не даёт
    превысить, но границу подсвечиваем) — раньше так красился QLabel.
    """
    return f"{len(text)} / {max_len}", len(text) >= max_len


def validate_display_name(
    text: str, max_len: int = MAX_DISPLAY_NAME_LENGTH
) -> str | None:
    """Нормализует отображаемое имя (display_name) перед отправкой на сервер.

    Правила — зеркально lib/profile_manager.save_profile: трим по краям,
    обрез до max_len. None при пустой строке (отправлять нечего — экран
    показывает предупреждение, а не гоняет пустое имя по сети).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def member_since_text(created_at: float | int) -> str:
    """«На сервере с» — дата регистрации из profile.created_at.

    Пустая строка, если поля нет (экран в этом случае не трогает QLabel).
    """
    if not created_at:
        return ""
    try:
        return time.strftime("%d.%m.%Y", time.localtime(created_at))
    except Exception:
        return ""


def status_bubble(status: str) -> str:
    """Статус-сообщение в карточке профиля: «💬  X» или пусто."""
    return f"💬  {status}" if status else ""


def avatar_size_error(size_bytes: int) -> str | None:
    """Проверка размера аватарки: None — можно грузить, иначе текст ошибки."""
    if size_bytes > AVATAR_MAX_BYTES:
        return f"Размер файла превышает {AVATAR_MAX_BYTES // (1024 * 1024)} МБ"
    return None


# ── Файловая витрина ────────────────────────────────────────────────────────


def filter_files(files: list[dict], filter_id: str) -> list[dict]:
    """Фильтр витрины: «all» пропускает всё, иначе сравнение file_kind."""
    if filter_id == "all":
        return files
    return [f for f in files if file_kind(f.get("name", "")) == filter_id]


def files_count_text(total: int, shown: int) -> str:
    """Заголовок счётчика витрины: без фильтра — «N файлов», иначе «N / M»."""
    return f"{total} файлов" if shown == total else f"{shown} / {total} файлов"


def empty_files_text(filter_id: str) -> str:
    """Текст пустого состояния витрины (для «all» и для категорий)."""
    names = {"image": "Картинок", "archive": "Архивов", "mod": "Модов"}
    if filter_id == "all":
        return "Пока нет файлов\nОтправь что-нибудь в чат — появится здесь"
    return f"{names.get(filter_id, 'Файлов')} в этой категории пока нет"


def can_delete_file(ev: dict, my_name: str, connected: bool) -> bool:
    """Удалять с витрины можно только свой файл и только при подключении."""
    return bool(connected) and ev.get("from") == my_name


def default_download_path(name: str) -> Path:
    """Куда падает скачанный файл, если юзер не выбрал путь вручную."""
    return Path.home() / "Downloads" / name


# ── Личные сообщения (DM) ───────────────────────────────────────────────────


def dm_preview_text(last_message: str, limit: int = DM_PREVIEW_LIMIT) -> str:
    """Превью диалога в списке: обрезка с многоточием после limit символов."""
    if len(last_message) > limit:
        return last_message[:limit] + "…"
    return last_message


def group_dm_flags(msgs: list[dict]) -> list[bool]:
    """Флаги группировки ленты DM: True — тот же отправитель, что и выше.

    Первое сообщение никогда не сгруппировано (last_sender = None в
    исходном цикле _on_chat_loaded). Сообщения без «from» считаются
    отправителем «?» — как и раньше, два таких подряд сгруппируются.
    """
    flags: list[bool] = []
    last_sender: str | None = None
    for msg in msgs:
        sender = msg.get("from", "?")
        flags.append(sender == last_sender)
        last_sender = sender
    return flags


def can_send_dm(connected: bool, active_user: str | None, text: str) -> bool:
    """Политика кнопки «Отправить»: подключены, диалог открыт, текст непуст.

    Текст тримается здесь же — экран передаёт сырой input, но дублирующий
    strip ничего не ломает (экран стрипает до вызова).
    """
    return bool(connected) and bool(active_user) and bool(text.strip())
