"""Тесты социального ядра (lib/client_core/social.py) — БЕЗ PySide6.

Фаза 3 отделения UI от логики: чистые функции профиля/файлов/DM переехали
из qt_app/screens/{profile_settings,files_screen,dm_screen}.py. Проверяем
рендер статистики из leaderboard, счётчики, фильтры витрины, группировку
ЛС, плейсхолдер дешифровки (общий с чатом) и политику отправки.

Запуск: python tests/test_social_core.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import client
from lib.client_core.chat_session import decrypt_text_of
from lib.client_core.social import (
    AVATAR_MAX_BYTES,
    DM_POLL_INTERVAL_MS,
    DM_PREVIEW_LIMIT,
    FRIENDS_POLL_INTERVAL_MS,
    STATS_POLL_INTERVAL_MS,
    avatar_size_error,
    can_delete_file,
    can_send_dm,
    counter_state,
    default_download_path,
    dm_preview_text,
    empty_files_text,
    files_count_text,
    filter_files,
    group_dm_flags,
    member_since_text,
    my_stats_from_leaderboard,
    status_bubble,
    validate_display_name,
)

PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


# ── my_stats_from_leaderboard ────────────────────────────────────────────────


def test_my_stats() -> None:
    print("== my_stats_from_leaderboard: поиск себя и дефолты ==")
    board = [
        {"name": "Alice", "score": 1350, "wins": 5, "losses": 2, "draws": 1},
        {"name": "Я", "score": 1207, "wins": 3, "losses": 1, "draws": 0},
        {"name": "Bob", "score": 990, "wins": 0, "losses": 4, "draws": 2},
    ]
    s = my_stats_from_leaderboard(board, "Я")
    check("себя нашли", s.found is True)
    check("elo — строка score", s.elo == "1207")
    check("место — второй", s.rank == "#2")
    check("победы", s.wins == "3")
    check("поражения", s.losses == "1")
    check("ничьи", s.draws == "0")

    first = my_stats_from_leaderboard(board, "Alice")
    check("первое место #1", first.rank == "#1")

    none = my_stats_from_leaderboard(board, "Карл")
    check("нет в списке — found False", none.found is False)
    check("дефолт elo 1200", none.elo == "1200")
    check("дефолт место —", none.rank == "—")
    check("дефолт wins 0", none.wins == "0")
    check("дефолт losses 0", none.losses == "0")
    check("дефолт draws 0", none.draws == "0")

    empty = my_stats_from_leaderboard([], "Я")
    check("пустой leaderboard — дефолты", empty.found is False and empty.elo == "1200")

    # неполные записи — дефолты внутри get()
    partial = my_stats_from_leaderboard([{"name": "Я"}], "Я")
    check("запись без score — elo 1200", partial.elo == "1200")
    check("запись без wins — 0", partial.wins == "0")


# ── counter_state ────────────────────────────────────────────────────────────


def test_counter_state() -> None:
    print("== counter_state: счётчик символов + warn-порог ==")
    label, warn = counter_state("", 200)
    check("пустой — 0 / MAX", label == "0 / 200")
    check("пустой — без warn", warn is False)

    label, warn = counter_state("привет", 200)
    check("обычный — N / MAX", label == "6 / 200")
    check("обычный — без warn", warn is False)

    label, warn = counter_state("x" * 200, 200)
    check("граница — N / MAX", label == "200 / 200")
    check("граница — warn", warn is True)

    label, warn = counter_state("x" * 201, 200)
    check("переполнение — warn", warn is True and label == "201 / 200")


# ── member_since_text / status_bubble / avatar_size_error ───────────────────


def test_profile_bits() -> None:
    print("== member_since_text / status_bubble / avatar_size_error ==")
    ts = time.mktime(time.strptime("15.03.2025", "%d.%m.%Y"))
    check("дата форматируется", member_since_text(ts) == "15.03.2025")
    check("0 — пустая строка", member_since_text(0) == "")
    check("нет поля — пустая строка", member_since_text(None) == "")

    check("статус есть — с эмодзи", status_bubble("В игре") == "💬  В игре")
    check("статус пуст — пусто", status_bubble("") == "")

    check("маленький файл — None", avatar_size_error(1024) is None)
    check("ровно 2МБ — None", avatar_size_error(AVATAR_MAX_BYTES) is None)
    err = avatar_size_error(AVATAR_MAX_BYTES + 1)
    check("больше 2МБ — ошибка", err is not None and "2 МБ" in err)
    check("константа = 2МБ", AVATAR_MAX_BYTES == 2 * 1024 * 1024)


# ── filter_files / files_count_text / empty_files_text ──────────────────────


def test_files() -> None:
    print("== filter_files / files_count_text / empty_files_text ==")
    files = [
        {"name": "cat.png", "from": "Я"},
        {"name": "mod.zip", "from": "Друг"},
        {"name": "map.pak", "from": "Друг"},   # mod
        {"name": "notes.txt", "from": "Я"},    # document
    ]
    check("all — все", len(filter_files(files, "all")) == 4)
    check("all — тот же список", filter_files(files, "all") is files)
    imgs = filter_files(files, "image")
    check("image — только картинки", [f["name"] for f in imgs] == ["cat.png"])
    mods = filter_files(files, "mod")
    check("mod — .pak, но не .zip (он archive)", [f["name"] for f in mods] == ["map.pak"])
    archs = filter_files(files, "archive")
    check("archive — .zip", [f["name"] for f in archs] == ["mod.zip"])
    check("пустой список — пусто", filter_files([], "image") == [])

    check("счётчик без фильтра", files_count_text(4, 4) == "4 файлов")
    check("счётчик с фильтром", files_count_text(4, 2) == "2 / 4 файлов")

    check(
        "empty all",
        empty_files_text("all") == "Пока нет файлов\nОтправь что-нибудь в чат — появится здесь",
    )
    check("empty image", empty_files_text("image") == "Картинок в этой категории пока нет")
    check("empty archive", empty_files_text("archive") == "Архивов в этой категории пока нет")
    check("empty mod", empty_files_text("mod") == "Модов в этой категории пока нет")
    check("empty unknown", empty_files_text("??") == "Файлов в этой категории пока нет")


# ── can_delete_file / default_download_path ─────────────────────────────────


def test_files_policy() -> None:
    print("== can_delete_file / default_download_path ==")
    ev = {"from": "Я", "name": "x.zip"}
    check("свой + подключён — да", can_delete_file(ev, "Я", True) is True)
    check("свой + не подключён — нет", can_delete_file(ev, "Я", False) is False)
    check("чужой — нет", can_delete_file(ev, "Друг", True) is False)
    check("нет from — нет", can_delete_file({}, "Я", True) is False)

    p = default_download_path("save.zip")
    check("путь в Downloads", p == Path.home() / "Downloads" / "save.zip")


# ── dm_preview_text / group_dm_flags / can_send_dm ──────────────────────────


def test_dm() -> None:
    print("== dm_preview_text / group_dm_flags / can_send_dm ==")
    check("короткий — как есть", dm_preview_text("привет") == "привет")
    long = "x" * 50
    check("длинный — обрезка с …", dm_preview_text(long) == "x" * DM_PREVIEW_LIMIT + "…")
    check("ровно limit — без …", dm_preview_text("x" * DM_PREVIEW_LIMIT) == "x" * DM_PREVIEW_LIMIT)
    check("пустой — пустой", dm_preview_text("") == "")

    msgs = [
        {"from": "Alice", "text": "1"},
        {"from": "Alice", "text": "2"},
        {"from": "Bob", "text": "3"},
        {"text": "4"},  # без from — «?»
        {"text": "5"},
    ]
    flags = group_dm_flags(msgs)
    check("первое не группируется", flags == [False, True, False, False, True])

    check("пустая лента — пустые флаги", group_dm_flags([]) == [])
    check("один отправитель подряд", group_dm_flags([{"from": "A"}] * 3) == [False, True, True])

    check("отправка ок", can_send_dm(True, "Bob", "текст") is True)
    check("не подключён — нет", can_send_dm(False, "Bob", "текст") is False)
    check("нет диалога — нет", can_send_dm(True, None, "текст") is False)
    check("пустой текст — нет", can_send_dm(True, "Bob", "") is False)
    check("текст из пробелов — нет", can_send_dm(True, "Bob", "  ") is False)


# ── decrypt_text_of (общая с чатом) ─────────────────────────────────────────


def test_decrypt_text_of() -> None:
    print("== decrypt_text_of: плейсхолдер и обычный текст ==")
    orig = client.decrypt_message

    class FakeDec:
        def __init__(self, ret: dict) -> None:
            self.ret = ret
            self.called_with: dict | None = None

        def __call__(self, msg: dict) -> dict:
            self.called_with = msg
            return self.ret

    try:
        fake = FakeDec({"_decrypt_failed": True})
        client.decrypt_message = fake
        check(
            "неудача — плейсхолдер",
            decrypt_text_of({"text": "сырье"}) == "🔒 [не удалось расшифровать — возможно, сменился ключ]",
        )
        check("в decrypt_message ушло само сообщение", fake.called_with == {"text": "сырье"})

        fake = FakeDec({"text": "привет"})
        client.decrypt_message = fake
        check("успех — текст", decrypt_text_of({"x": 1}) == "привет")

        fake = FakeDec({})
        client.decrypt_message = fake
        check("нет text — пустая строка", decrypt_text_of({"x": 1}) == "")
    finally:
        client.decrypt_message = orig
    check("клиент восстановлен", client.decrypt_message is orig)


# ── константы поллинга ───────────────────────────────────────────────────────


def test_constants() -> None:
    print("== константы поллинга ==")
    check("друзья 10с", FRIENDS_POLL_INTERVAL_MS == 10000)
    check("статистика 30с", STATS_POLL_INTERVAL_MS == 30000)
    check("DM 2с", DM_POLL_INTERVAL_MS == 2000)
    check("превью DM 40", DM_PREVIEW_LIMIT == 40)


def test_validate_display_name() -> None:
    print("== validate_display_name: нормализация отображаемого имени ==")
    check("пустая строка — None", validate_display_name("") is None)
    check("только пробелы — None", validate_display_name("   ") is None)
    check("None-вход — None", validate_display_name(None) is None)
    check("трим по краям", validate_display_name("  Вася ") == "Вася")
    check("внутренние пробелы сохранены", validate_display_name("В а с я") == "В а с я")
    check("юникод ок", validate_display_name("Ёжик_Туманный") == "Ёжик_Туманный")
    check("обрез до 32", len(validate_display_name("x" * 50)) == 32)
    check("граница 32 не режется", validate_display_name("а" * 32) == "а" * 32)
    check("свой лимит", validate_display_name("abcdef", max_len=3) == "abc")
    check("32 после трима", len(validate_display_name("  " + "б" * 40 + "  ")) == 32)


def main() -> int:
    test_my_stats()
    test_counter_state()
    test_validate_display_name()
    test_profile_bits()
    test_files()
    test_files_policy()
    test_dm()
    test_decrypt_text_of()
    test_constants()
    print(f"\nИТОГО: {PASS} OK, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
