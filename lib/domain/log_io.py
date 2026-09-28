"""JSONL-персистентность журнала событий (domain-слой).

Хелперы чтения/записи JSONL-логов: общий журнал чата (history), логи
личных диалогов (dm/*.jsonl). Раньше жили в lib/storage.py, но их единственным
потребителем был EventStore — формат на диске (seq, kind, tombstone) и есть
часть доменной сущности «журнал событий». Теперь домен самовывозит свою
персистентность: lib/domain больше не зависит от прикладного storage.

Только файловый I/O по переданным путям: никаких настроек, HTTP, Qt, сети.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def load_history(history_path: Path, limit: int | None = None) -> list[dict]:
    """Загружает события из JSONL файла. Если limit задан, возвращает только последние N событий."""
    if not history_path.exists():
        return []

    events = []
    with open(history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                events.append(ev)
            except json.JSONDecodeError:
                continue

    if limit is not None and len(events) > limit:
        return events[-limit:]
    return events


def load_history_before(history_path: Path, before_seq: int, count: int = 50) -> list[dict]:
    """Загружает события из JSONL файла с seq меньше before_seq.
    Возвращает не более count событий в обратном порядке (от новых к старым).

    Используется для подгрузки старых сообщений при скролле вверх в чате.
    Читает файл с конца для эффективности при больших историях.
    """
    if not history_path.exists():
        return []

    # Читаем файл с конца для эффективности при больших историях
    events = []
    with open(history_path, encoding="utf-8") as f:
        # Получаем все строки
        lines = f.readlines()

        # Идём с конца файла
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                seq = ev.get("seq", 0)
                if seq < before_seq:
                    events.append(ev)
                    if len(events) >= count:
                        break
            except json.JSONDecodeError:
                continue

    # events уже в обратном порядке (от новых к старым) из-за reversed
    return events


def save_dm(dm_path: Path, sender: str, recipient: str, text: str, seq: int,
            encrypted: bool = False, iv: str = "") -> None:
    """Сохраняет личное текстовое сообщение в JSONL файл.

    encrypted+iv — метка E2E-шифрования (текст уже зашифрован отправителем);
    поля пишутся в событие только когда зашифровано, чтобы старые записи
    на диске оставались ровно в прежнем формате."""
    event = {
        "kind": "dm",
        "from": sender,
        "to": recipient,
        "text": text,
        "seq": seq,
        "ts": int(time.time()),
    }
    if encrypted:
        event["encrypted"] = True
        event["iv"] = iv
    save_dm_event(dm_path, event)


def save_dm_event(dm_path: Path, event: dict) -> None:
    """Дописывает произвольное DM-событие (текст или файл, см. kind) в JSONL
    файл диалога - используется и текстовыми, и файловыми сообщениями, чтобы
    формат на диске был единым и историю можно было читать одним и тем же
    load_dm_history."""
    dm_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dm_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_dm_history(dm_path: Path, limit: int | None = None) -> list[dict]:
    """Загружает историю личных сообщений из JSONL файла."""
    if not dm_path.exists():
        return []

    events = []
    with open(dm_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                events.append(ev)
            except json.JSONDecodeError:
                continue

    if limit is not None and len(events) > limit:
        return events[-limit:]
    return events


def get_dm_conversations(dm_dir: Path, username: str) -> list[dict]:
    """Возвращает список диалогов пользователя с последними сообщениями.

    v1.9.5: участники берутся из ПОСЛЕДНЕГО события файла (поля from/to),
    а не из split('_') по имени файла. Имя «anna_bob_marley.jsonl» могло
    быть диалогом anna↔bob_marley — split давал трёх фантомных участников
    («bob» у anna, «anna» у несуществующего «bob»). Прошиваем все строки
    с конца до первого валидного (последняя может быть битой при обрыве
    записи)."""
    if not dm_dir.exists():
        return []

    from lib.util import safe_name

    me = safe_name(username or "")
    if not me:
        return []

    conversations = []
    for dm_file in dm_dir.glob("*.jsonl"):
        # Быстрый префильтр по имени файла (участники в алфавитном порядке
        # через '_'). Имя с '_' само разбивается на несколько токенов
        # (bob_marley), поэтому точного вхождения в split может не быть —
        # допускаем и префикс/суффикс формы sorted-конкатенации. Точные
        # имена всё равно берём из событий ниже.
        stem = dm_file.stem
        if not (me in stem.split("_")
                or stem.startswith(me + "_")
                or stem.endswith("_" + me)):
            continue

        # Ищем последнее событие с валидными from/to
        history = load_dm_history(dm_file, limit=1)
        last_msg = history[-1] if history else None
        other_user = ""
        if last_msg:
            f, t = last_msg.get("from", ""), last_msg.get("to", "")
            if me == safe_name(f):
                other_user = safe_name(t)
            elif me == safe_name(t):
                other_user = safe_name(f)
        if not other_user:
            continue

        conversations.append(
            {
                "user": other_user,
                "last_message": last_msg.get("text", ""),
                "last_time": last_msg.get("ts", 0),
                "unread": False,  # считается на слое присутствия/клиента
            }
        )

    # Сортируем по времени последнего сообщения
    conversations.sort(key=lambda x: x["last_time"], reverse=True)
    return conversations
