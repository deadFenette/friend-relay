"""EventStore - доменная сущность "журнал событий чата".

Отвечает на вопрос "ЧТО такое сообщение/файл/дельта/tombstone" и "как они
хранятся": seq-нумерация, JSONL-персистентность, переигрывание дельта-событий
(edit/reaction/pin/link_preview) поверх оригиналов при загрузке, tombstone
удалений, индексы файлов (общих и личных).

Раньше вся эта логика была размазана по RelayServer (god-object, 56 методов).
Здесь НЕТ ничего транспортного: ни HTTP, ни заголовков, ни кодов ответов -
только сущности и правила их жизни. Транспорт (lib/server/http_api.py) и
application-фасад (RelayServer) вызывают эти методы.

Потокобезопасность: один лок на весь журнал (так же было и в RelayServer -
гораздо важнее простой и предсказуемый лок, чем тонкая гранулярность)."""
from __future__ import annotations

import bisect
import json
import threading
import time
import uuid
from pathlib import Path

from lib.constants import (
    DELTA_EVENT_KINDS,
    EVENT_CACHE_LIMIT,
    EVENT_KIND_DELETE,
    EVENT_KIND_EDIT,
    EVENT_KIND_FILE,
    EVENT_KIND_LINK_PREVIEW,
    EVENT_KIND_PIN,
    EVENT_KIND_REACTION,
    EVENT_KIND_TEXT,
    FILE_HOLDERS_MAX,
    FILES_DIR_NAME,
    HISTORY_FILE_NAME,
    HOLDERS_FILE_NAME,
)
from lib.domain.log_io import (
    get_dm_conversations,
    load_dm_history,
    load_history,
    save_dm,
    save_dm_event,
)
from lib.json_fast import dumps_str


class EventStore:
    def __init__(self, data_dir: Path, cache_limit: int = EVENT_CACHE_LIMIT):
        self.data_dir = data_dir
        self.files_dir = data_dir / FILES_DIR_NAME
        self.history_path = data_dir / HISTORY_FILE_NAME

        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.dm_dir = data_dir / "dm"
        self.dm_dir.mkdir(parents=True, exist_ok=True)
        # Файлы, отправленные приватно через ЛС - физически ОТДЕЛЬНАЯ папка
        # от files_dir (общей витрины), чтобы их не было видно в /files и на
        # экране "Файлы" - раньше приватной отправки не существовало вовсе, и
        # файл, кинутый в ЛС, на самом деле утекал в общую витрину, видную
        # всем на сервере (см. add_dm_file/download-проверку прав в транспорте).
        self.dm_files_dir = data_dir / "dm_files"
        self.dm_files_dir.mkdir(parents=True, exist_ok=True)

        self.holders_path = data_dir / HOLDERS_FILE_NAME
        self._holders: dict[str, set[str]] = {}  # file_id -> кто скачал (multi-source v1)

        self._lock = threading.Lock()
        self._events: list[dict] = []
        # (v2.0.2) Индекс seq -> событие: edit/reaction/pin/delete/preview
        # ищут цель по seq на КАЖДЫЙ клик пользователя. Раньше это был
        # линейный скан next(e for e in self._events if e["seq"] == ...) —
        # O(n) на каждый запрос; на кеше 5000 событий лишняя работа была
        # незаметна, но и пользы от неё нет. Зеркален _events (см.
        # _cache_append/_trim_cache/_apply_deletions — все три точки,
        # где меняется состав кеша).
        self._by_seq: dict[int, dict] = {}
        self._deleted_seqs: set = set()  # seq, помеченные удалёнными
        self._file_index: dict[str, dict] = {}  # file_id -> event dict
        self._next_seq = 1
        self._history_fp = open(
            self.history_path, "a", encoding="utf-8", buffering=1
        )  # line-buffered
        self._cache_limit = cache_limit
        self._load_history()

        # Индекс приватных DM-файлов: file_id -> событие (with "from"/"to" -
        # проверяем при скачивании, что просит один из двух участников).
        # Восстанавливается из dm/*.jsonl при старте, чтобы старые файлы можно
        # было скачать и после перезапуска хоста (тот же баг, что мы уже
        # чинили для реакций/пинов - события на диске есть, в памяти их не было).
        self._dm_file_index: dict[str, dict] = {}
        self._load_dm_file_index()
        self._load_holders()

    # -- seq ----------------------------------------------------------------
    def next_seq(self) -> int:
        """Выдаёт следующий глобальный seq (для событий, файлов, channel-сообщений).
        Используется ChannelManager'ом через seq-provider: seq единый на всё
        приложение, чтобы не пересекался с seq событий основного чата."""
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
        return seq

    # -- личные сообщения ----------------------------------------------------
    def _dm_path(self, user1: str, user2: str) -> Path | None:
        """Путь файла диалога из двух имён. Имена прогоняются через
        safe_name (v1.9.5): раньше X-Relay-From: '../../evil' в POST /dm/send
        собирал dm_dir/'../../evil_bob.jsonl' — запись JSONL НА ДВА УРОВНЯ
        выше каталога диалогов (path traversal), а GET /dm/history читал
        произвольные файлы по тому же принципу. Пустое sanitized-имя
        (= мусорный заголовок) → None, вызов отвергается."""
        from lib.util import safe_name

        a, b = safe_name(user1 or ""), safe_name(user2 or "")
        if not a or not b:
            return None
        participants = sorted([a, b])
        return self.dm_dir / f"{'_'.join(participants)}.jsonl"

    def send_dm(self, sender: str, recipient: str, text: str,
                encrypted: bool = False, iv: str = "") -> dict:
        """Отправляет личное сообщение. encrypted+iv — метка E2E-шифрования
        (текст в "text" уже зашифрован отправителем); домен только хранит
        эти поля, расшифровка — дело клиента."""
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1

        # Имя файла диалога: участники в алфавитном порядке, безопасные
        dm_path = self._dm_path(sender, recipient)
        if dm_path is None:
            raise ValueError("недопустимое имя участника диалога")

        save_dm(dm_path, sender, recipient, text, seq, encrypted=encrypted, iv=iv)

        ev = {
            "kind": "dm",
            "from": sender,
            "to": recipient,
            "text": text,
            "seq": seq,
            "ts": int(time.time()),
        }
        if encrypted:
            ev["encrypted"] = True
            ev["iv"] = iv
        return ev

    def get_dm_history(self, user1: str, user2: str, limit: int | None = None) -> list[dict]:
        """Получает историю личных сообщений между двумя пользователями."""
        dm_path = self._dm_path(user1, user2)
        if dm_path is None:
            return []
        return load_dm_history(dm_path, limit)

    def get_dm_conversations(self, username: str) -> list[dict]:
        """Получает список диалогов пользователя."""
        return get_dm_conversations(self.dm_dir, username)

    def _load_dm_file_index(self) -> None:
        for dm_file in self.dm_dir.glob("*.jsonl"):
            for ev in load_dm_history(dm_file):
                if ev.get("kind") == "dm_file" and ev.get("file_id"):
                    self._dm_file_index[ev["file_id"]] = ev

    def add_dm_file(self, sender: str, recipient: str, filename: str, data: bytes,
                    sha256: str = "") -> dict:
        """Приватно отправляет файл собеседнику в ЛС (байты в памяти - старый
        путь). Приватная версия add_file_from_path - add_dm_file_from_path."""
        file_id = uuid.uuid4().hex
        dest = self.dm_files_dir / file_id
        dest.write_bytes(data)
        return self._register_dm_file(file_id, sender, recipient, filename,
                                      dest.stat().st_size, sha256)

    def add_dm_file_from_path(self, sender: str, recipient: str, filename: str,
                              source: Path, sha256: str = "") -> dict:
        """Приватная отправка файла из temp-файла на диске (стриминг без RAM).
        Физически лежит в dm_files_dir (НЕ в общей files_dir), в общую витрину
        /files не попадает, скачать может только sender или recipient (проверка
        прав - в HTTP-транспорте, /dm_download/<file_id>)."""
        file_id = uuid.uuid4().hex
        dest = self.dm_files_dir / file_id
        source.replace(dest)
        return self._register_dm_file(file_id, sender, recipient, filename,
                                      dest.stat().st_size, sha256)

    def _register_dm_file(self, file_id: str, sender: str, recipient: str,
                          filename: str, size: int, sha256: str) -> dict:
        with self._lock:
            ev = {
                "kind": "dm_file",
                "from": sender,
                "to": recipient,
                "file_id": file_id,
                "name": filename,
                "size": size,
                "seq": self._next_seq,
                "ts": int(time.time()),
            }
            if sha256:
                ev["sha256"] = sha256
            self._next_seq += 1

        dm_path = self._dm_path(sender, recipient)
        if dm_path is None:
            raise ValueError("недопустимое имя участника диалога")
        save_dm_event(dm_path, ev)

        with self._lock:
            self._dm_file_index[file_id] = ev
        return ev

    def get_dm_file_event(self, file_id: str) -> dict | None:
        with self._lock:
            return self._dm_file_index.get(file_id)

    def dm_file_bytes_path(self, file_id: str) -> Path:
        return self.dm_files_dir / file_id

    # -- персистентность на диске -------------------------------------------
    def _load_history(self) -> None:
        events = load_history(self.history_path, limit=self._cache_limit)
        # (v2.0.2) Инвариант «_events отсортирован по seq» — фундамент
        # bisect-поиска в events_since/events_before. Журнал пишется строго
        # по возрастанию seq, поэтому сортировка обычно no-op; защищает
        # только от ручной починки history.jsonl на диске (склейка двух
        # файлов и т.п.) — дешевле один раз отсортировать при старте, чем
        # объяснять bisect, почему тот нашёл не то.
        events.sort(key=lambda e: e.get("seq", 0))
        for ev in events:
            if ev.get("kind") == EVENT_KIND_DELETE:
                target = ev.get("target_seq")
                if isinstance(target, int):
                    self._deleted_seqs.add(target)
                self._cache_append(ev)
            else:
                self._cache_append(ev)
                if ev.get("kind") == EVENT_KIND_FILE and "file_id" in ev:
                    self._file_index[ev["file_id"]] = ev
            self._next_seq = max(self._next_seq, ev.get("seq", 0) + 1)

        # Переигрываем дельта-события (edit/reaction/pin/link_preview) поверх
        # оригинальных сообщений - без этого правки/реакции/пины и подъехавшие
        # превью ссылок пропадали бы с экрана при каждом перезапуске хоста,
        # хотя в history.jsonl всё честно записано.
        self._replay_deltas(self._events)

        self._apply_deletions()

    @staticmethod
    def build_delta_map(events: list[dict]) -> dict[int, list[dict]]:
        """target_seq -> дельта-события (edit/reaction/pin/link_preview) в
        хронологическом порядке (порядок в events должен быть по возрастанию seq)."""
        deltas: dict[int, list[dict]] = {}
        for ev in events:
            if ev.get("kind") in DELTA_EVENT_KINDS:
                target = ev.get("target_seq")
                if isinstance(target, int):
                    deltas.setdefault(target, []).append(ev)
        return deltas

    # Синоним для внутренней совместимости (в коде рефакторинга так короче)
    _build_delta_map = build_delta_map

    @staticmethod
    def apply_delta(target: dict, delta: dict) -> None:
        """Применяет одно дельта-событие к его оригинальному сообщению."""
        kind = delta.get("kind")
        if kind == EVENT_KIND_EDIT:
            new_text = delta.get("new_text")
            if new_text is not None:
                target["text"] = new_text
                target["edited"] = True
                target["edited_ts"] = delta.get("ts")
        elif kind == EVENT_KIND_REACTION:
            sender = delta.get("from")
            emoji = delta.get("emoji")
            if not sender or not emoji:
                return
            # Реакции исторически хранились как «переключение» (add_reaction
            # тоглит присутствие sender в списке) - переигрываем в том же
            # порядке, чтобы получить тот же итоговый набор.
            # v1.9.7: у новых дельт есть ЯВНЫЙ флаг added — применяем его
            # (идемпотентно: повторная доставка дельты не отматывает
            # реакцию). Легаси-события без флага — переключение, как раньше.
            reactions = target.setdefault("reactions", {})
            users = reactions.setdefault(emoji, [])
            if "added" in delta:
                if delta["added"]:
                    if sender not in users:
                        users.append(sender)
                else:
                    if sender in users:
                        users.remove(sender)
            elif sender in users:
                users.remove(sender)
            else:
                users.append(sender)
            if not users:
                del reactions[emoji]
        elif kind == EVENT_KIND_PIN:
            target["pinned"] = bool(delta.get("pinned"))
        elif kind == EVENT_KIND_LINK_PREVIEW:
            preview = delta.get("link_preview")
            if preview:
                target["link_preview"] = preview

    # Синоним для внутренней совместимости
    _apply_delta = apply_delta

    def _replay_deltas(self, events: list[dict]) -> None:
        """Мутирует оригинальные события в `events` на месте, применяя все
        дельта-события (edit/reaction/pin/link_preview) из того же списка."""
        deltas = self.build_delta_map(events)
        if not deltas:
            return
        for ev in events:
            seq = ev.get("seq")
            if seq in deltas:
                for delta in deltas[seq]:
                    self.apply_delta(ev, delta)

    def _append_event(self, ev: dict) -> None:
        # (v2.0.2) json_fast: тот же формат строки (ensure_ascii=False),
        # но сериализация в 5-10 раз быстрее при orjson. Каждое сообщение
        # проходит здесь синхронно под локом — каждый мс задержки держит
        # очередь поллингов.
        self._history_fp.write(dumps_str(ev) + "\n")
        self._history_fp.flush()

    def _cache_append(self, ev: dict) -> None:
        """(v2.0.2) Единственная точка добавления события в in-memory кеш.
        Раньше append'ов в _events было восемь разрозненных — каждый новый
        индекс (сейчас _by_seq, завтра что-то ещё) требовал не забыть все
        восемь мест. Синтаксис: seq обязан быть, иначе индекс бессмыслен."""
        self._events.append(ev)
        self._by_seq[ev["seq"]] = ev

    def _trim_cache(self) -> None:
        """Держим в памяти только последние _cache_limit событий, чтобы на
        месяцах работы релея не росла до бесконечности. Tombstone и файловые
        события из _file_index так же подчищаем: file_index мы больше не
        трогаем (скачивание удалённых файлов редко нужно и оно всё равно
        проверяет путь на диске)."""
        if len(self._events) <= self._cache_limit:
            return
        drop = len(self._events) - self._cache_limit
        dropped = self._events[:drop]
        self._events = self._events[drop:]
        # (v2.0.2) Вытесненные уходят и из seq-индекса: иначе _by_seq рос
        # вечно, а edit/delete «находили» сообщения, которых уже нет в ленте.
        for ev in dropped:
            self._by_seq.pop(ev.get("seq"), None)

    def _apply_deletions(self) -> None:
        """Убирает из памяти удалённый контент, tombstone-события остаются для poll.
        Если удалённое сообщение было файлом - подчищаем и сами байты с диска
        (files_dir/<file_id>), иначе они бы висели там вечно: раньше
        _apply_deletions трогал только индекс в памяти, а физический файл
        оставался занимать место на диске хоста навсегда, даже после удаления
        сообщения из чата."""
        if not self._deleted_seqs:
            return
        self._events = [e for e in self._events if e.get("seq") not in self._deleted_seqs]
        # (v2.0.2) Индекс держим строго зеркальным кешу: удалённые seq
        # больше не «находятся» для правок/реакций (поведение как раньше —
        # линейный скан тоже их не видел).
        for seq in list(self._by_seq):
            if seq in self._deleted_seqs:
                del self._by_seq[seq]
        for fid in list(self._file_index):
            ev = self._file_index[fid]
            if ev.get("seq") in self._deleted_seqs:
                del self._file_index[fid]
                path = self.files_dir / fid
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass  # диск мог быть занят/недоступен - не роняем сервер из-за уборки мусора
                # Файла больше нет - из реестра владельцев его запись тоже долой
                self._holders.pop(fid, None)

    def close(self) -> None:
        try:
            if self._history_fp is not None:
                self._history_fp.flush()
                self._history_fp.close()
                self._history_fp = None
        except OSError:
            pass

    # -- события ---------------------------------------------------------
    def add_text(self, sender: str, text: str, encrypted: bool = False, iv: str = "") -> dict:
        with self._lock:
            ev = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_TEXT,
                "from": sender,
                "ts": time.time(),
                "text": text,
            }
            if encrypted:
                ev["encrypted"] = True
                ev["iv"] = iv
            self._next_seq += 1
            self._cache_append(ev)
            self._append_event(ev)
            self._trim_cache()

        # Фоновое превью ссылок здесь НЕ запускается: это политика
        # application-слоя (RelayServer.add_text), домен только хранит.

        return ev

    def attach_link_preview(self, target_seq: int, preview: dict, host_name: str) -> None:
        """Прикрепляет превью ссылки к событию: патчит оригинал в памяти и
        пишет отдельное delta-событие (link_preview) в журнал. Вызывается из
        фонового потока фасада - здесь только доменные правила."""
        if not preview:
            return
        with self._lock:
            if target_seq in self._deleted_seqs:
                return
            target = self._by_seq.get(target_seq)  # (v2.0.2) O(1) вместо скана
            if target is not None and target.get("kind") == EVENT_KIND_TEXT:
                target["link_preview"] = preview

            preview_event = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_LINK_PREVIEW,
                "from": host_name,
                "ts": time.time(),
                "target_seq": target_seq,
                "link_preview": preview,
            }
            self._next_seq += 1
            self._cache_append(preview_event)
            self._append_event(preview_event)
            self._trim_cache()

    def add_file(self, sender: str, filename: str, data: bytes,
                 sha256: str = "") -> dict:
        """Регистрирует файл из байтов в памяти (старый путь - мелкие файлы
        и тесты). Для больших файлов используй add_file_from_path - он не
        держит файл в RAM."""
        file_id = uuid.uuid4().hex
        dest = self.files_dir / file_id
        dest.write_bytes(data)
        return self._register_file(file_id, sender, filename, len(data), sha256)

    def add_file_from_path(self, sender: str, filename: str, source: Path,
                           sha256: str = "") -> dict:
        """Регистрирует файл, УЖЕ лежащий на диске (транспорт стримит тело
        запроса сразу в temp-файл в files_dir, без буферизации в RAM).
        Делает атомарный rename на постоянное место и создаёт событие.
        Возвращает событие файла."""
        file_id = uuid.uuid4().hex
        dest = self.files_dir / file_id
        source.replace(dest)
        return self._register_file(file_id, sender, filename, dest.stat().st_size, sha256)

    def _register_file(self, file_id: str, sender: str, filename: str,
                       size: int, sha256: str) -> dict:
        """Общая часть add_file/add_file_from_path: событие + индексы."""
        with self._lock:
            ev = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_FILE,
                "from": sender,
                "ts": time.time(),
                "file_id": file_id,
                "name": filename,
                "size": size,
            }
            if sha256:
                ev["sha256"] = sha256
            self._next_seq += 1
            self._cache_append(ev)
            self._file_index[file_id] = ev
            self._append_event(ev)
            self._trim_cache()
            return ev

    def edit_text(self, sender: str, target_seq: int, new_text: str,
                  encrypted: bool = False, iv: str = "") -> dict | None:
        """Редактирует своё текстовое сообщение. Создаёт событие редактирования.

        encrypted+iv (v1.9.5): правка зашифрованного сообщения приходит уже
        зашифрованной — флаг переносится на оригинал. Раньше plaintext-правка
        события с encrypted=True ломала расшифровку у всех получателей
        навсегда (v2-блоб невалиден) и открытый текст хранился на сервере."""
        with self._lock:
            # (v2.0.2) O(1) поиск цели по seq (был линейный скан)
            target = self._by_seq.get(target_seq)
            if target is None:
                return None
            if target.get("from") != sender:
                return None
            if target.get("kind") != EVENT_KIND_TEXT:
                return None
            if target_seq in self._deleted_seqs:
                return None

            # Обновляем текст в оригинальном событии
            target["text"] = new_text
            target["edited"] = True
            target["edited_ts"] = time.time()
            if encrypted:
                target["encrypted"] = True
                if iv:
                    target["iv"] = iv
            else:
                # правка сняла шифрование (клиент прислал открытый текст)
                target.pop("encrypted", None)
                target.pop("iv", None)

            # Создаём событие редактирования для синхронизации
            edit_event = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_EDIT,
                "from": sender,
                "ts": time.time(),
                "target_seq": target_seq,
                "new_text": new_text,
            }
            self._next_seq += 1
            self._cache_append(edit_event)
            self._append_event(edit_event)
            self._trim_cache()
            return edit_event

    def add_reaction(self, sender: str, target_seq: int, emoji: str) -> dict | None:
        """Добавляет реакцию (эмодзи) к сообщению.

        v1.9.7: дельта-событие несёт ЯВНЫЙ флаг "added" (поставлена/снята),
        применение идемпотентно. Раньше дельта была «слепым переключателем»:
        Qt-клиент тогглил бабл локально до ответа сервера, потом дельта
        приезжала по поллингу и переключала ЕЩЁ РАЗ — реакция «сама
        снималась» через секунду после клика. А веб при переподключении
        (since=0) получал text-события с уже вшитыми реакциями плюс дельты
        поверх — и реакции «отматывались» до пустых."""
        with self._lock:
            # (v2.0.2) O(1) поиск цели по seq (был линейный скан)
            target = self._by_seq.get(target_seq)
            if target is None:
                return None
            if target.get("kind") != EVENT_KIND_TEXT:
                return None
            if target_seq in self._deleted_seqs:
                return None

            # Добавляем или убираем реакцию
            reactions = target.setdefault("reactions", {})
            users = reactions.setdefault(emoji, [])
            added = sender not in users
            if added:
                users.append(sender)
            else:
                users.remove(sender)
                if not users:
                    del reactions[emoji]

            # Создаём событие реакции для синхронизации (с явным флагом)
            reaction_event = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_REACTION,
                "from": sender,
                "ts": time.time(),
                "target_seq": target_seq,
                "emoji": emoji,
                "added": added,
            }
            self._next_seq += 1
            self._cache_append(reaction_event)
            self._append_event(reaction_event)
            self._trim_cache()
            return reaction_event

    def pin_message(self, sender: str, target_seq: int) -> dict | None:
        """Закрепляет сообщение."""
        with self._lock:
            # (v2.0.2) O(1) поиск цели по seq (был линейный скан)
            target = self._by_seq.get(target_seq)
            if target is None:
                return None
            if target.get("kind") != EVENT_KIND_TEXT:
                return None
            if target_seq in self._deleted_seqs:
                return None

            # Переключаем пин
            is_pinned = target.get("pinned", False)
            target["pinned"] = not is_pinned

            # Создаём событие пина для синхронизации
            pin_event = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_PIN,
                "from": sender,
                "ts": time.time(),
                "target_seq": target_seq,
                "pinned": not is_pinned,
            }
            self._next_seq += 1
            self._cache_append(pin_event)
            self._append_event(pin_event)
            self._trim_cache()
            return pin_event

    def delete_event(self, sender: str, target_seq: int) -> dict | None:
        """Удаляет своё сообщение/файл. Пишет tombstone в историю для синхронизации."""
        with self._lock:
            # (v2.0.2) O(1) поиск цели по seq (был линейный скан)
            target = self._by_seq.get(target_seq)
            if target is None:
                return None
            if target.get("from") != sender:
                return None
            if target.get("kind") not in (EVENT_KIND_TEXT, EVENT_KIND_FILE):
                return None
            if target_seq in self._deleted_seqs:
                return None

            self._deleted_seqs.add(target_seq)
            tombstone = {
                "seq": self._next_seq,
                "kind": EVENT_KIND_DELETE,
                "from": sender,
                "ts": time.time(),
                "target_seq": target_seq,
            }
            self._next_seq += 1
            self._cache_append(tombstone)
            self._append_event(tombstone)
            self._apply_deletions()
            self._trim_cache()
            return tombstone

    def events_since(self, since: int, tail: int | None = None) -> list[dict]:
        """tail - если задан И since==0 (свежее подключение), отдаём только
        последние N событий вместо всей истории разом - чтобы у только что
        подключившегося друга не подвисало окно на рендере тысяч старых
        сообщений. Дальше поллинг идёт как обычно, по honest since.

        (v2.0.2) Самая горячая точка хоста (docs/PERFORMANCE.md): каждый
        клиент дёргает /events каждые 1.5-2с, и до сих пор каждый такой
        опрос линейно сканировал ВЕСЬ кеш [e for e in self._events if ...]
        — 5000 сравнений словарей на каждый тик на каждого клиента.
        Кеш отсортирован по seq (инвариант из _load_history/_cache_append),
        поэтому скан заменён на bisect: O(log n) поиск позиции + срез.
        Результат байт-в-байт тот же, что у старого list-comprehension —
        покрытие регресс-тестами не изменилось."""
        with self._lock:
            if tail is not None and since == 0 and len(self._events) > tail:
                return self._events[-tail:]
            pos = bisect.bisect_right(self._events, since, key=lambda e: e["seq"])
            return self._events[pos:]

    def events_before(self, before_seq: int, count: int = 50) -> list[dict]:
        """Возвращает события с seq меньше before_seq (старые сообщения), от
        старых к новым - так же, как ветка "в памяти" (транспорт и клиент
        берут events[0] как минимальный seq в батче).
        Используется для подгрузки истории при скролле вверх.
        Читает из диска, потому что старые события могут быть не в памяти
        (они вытеснены из кеша _trim_cache)."""
        # Сначала проверяем в памяти - если там есть нужные события, берём
        # оттуда. ФИЛЬТР КАК НА ДИСКЕ (v1.9.5): ветка памяти раньше отдавала
        # и дельты (edit/reaction/pin), и tombstone'ы — а дисковая их
        # исключала; на границе кеша страницы подгрузки меняли состав
        # (tombstone «пропадал» при переходе в диск, дельты приезжали
        # дважды: уже применённые в памяти + переигранные с диска).
        with self._lock:
            # (v2.0.2) Сначала отрезаем bisect'ом префикс seq < before_seq
            # (список отсортирован), потом фильтруем по видам события —
            # вместо скана всего кеша на каждый подгруз истории.
            head = self._events[:bisect.bisect_left(
                self._events, before_seq, key=lambda e: e["seq"])]
            in_memory = [
                e for e in head
                if e.get("kind") not in DELTA_EVENT_KINDS
                and e.get("kind") != EVENT_KIND_DELETE
            ]
            if len(in_memory) >= count:
                return in_memory[-count:]

        # В памяти недостаточно - читаем с диска. Старая версия отдавала
        # events "как есть", без переигрывания edit/reaction/pin/link_preview
        # поверх них (сообщения из-за пределов кеша показывали правки/реакции/
        # пины неактуальными) и в обратном порядке (новые->старые), из-за чего
        # next_since на клиенте считался неверно и подгрузка совсем старой
        # истории могла зависнуть на месте. Читаем файл целиком раз на такой
        # запрос и прогоняем дельты как в _load_history.
        raw_events = load_history(self.history_path)
        deltas = self.build_delta_map(raw_events)
        deleted: set[int] = set()
        for ev in raw_events:
            if ev.get("kind") == EVENT_KIND_DELETE:
                target = ev.get("target_seq")
                if isinstance(target, int):
                    deleted.add(target)

        window = []
        for ev in raw_events:
            seq = ev.get("seq", 0)
            if seq >= before_seq or seq in deleted or ev.get("kind") in DELTA_EVENT_KINDS:
                continue
            if seq in deltas:
                ev = dict(ev)
                for delta in deltas[seq]:
                    self.apply_delta(ev, delta)
            window.append(ev)

        window.sort(key=lambda e: e.get("seq", 0))
        return window[-count:]

    def get_pinned_messages(self) -> list[dict]:
        """Возвращает все закреплённые сообщения, от старых к новым - сканирует
        весь лог на диске, а не только кеш в памяти, потому что пин мог стоять
        на сообщении, давно вытесненном из кеша."""
        raw_events = load_history(self.history_path)
        deltas = self.build_delta_map(raw_events)
        deleted: set[int] = set()
        for ev in raw_events:
            if ev.get("kind") == EVENT_KIND_DELETE:
                target = ev.get("target_seq")
                if isinstance(target, int):
                    deleted.add(target)

        pinned = []
        for ev in raw_events:
            if ev.get("kind") != EVENT_KIND_TEXT:
                continue
            seq = ev.get("seq")
            if seq in deleted:
                continue
            if seq in deltas:
                ev = dict(ev)
                for delta in deltas[seq]:
                    self.apply_delta(ev, delta)
            if ev.get("pinned"):
                pinned.append(ev)

        pinned.sort(key=lambda e: e.get("ts", 0))
        return pinned

    def search_events(self, query: str, limit: int = 50,
                      author: str = "") -> list[dict]:
        """Поиск по журналу общего чата (v3.8.0): регистронезависимая
        подстрока в тексте сообщений и именах файлов. Сканируется ВЕСЬ лог
        на диске - включая события, вытесненные из кеша (тот же шаблон
        чтения, что у пинов). Правки учитываются: ищем по АКТУАЛЬНОМУ
        тексту (дельты edit переигрываются, как в _load_history).
        Зашифрованные сообщения не ищутся честно: сервер не может их
        прочитать (E2E), осмысленной подстроки в шифротексте не бывает.
        Удалённые сообщения (tombstone) в результаты не попадают.
        Возвращает последние limit совпадений, от старых к новым."""
        q = (query or "").strip().lower()
        if len(q) < 2:
            return []
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 50
        author_l = (author or "").strip().lower()

        raw_events = load_history(self.history_path)
        deltas = self.build_delta_map(raw_events)
        deleted: set[int] = set()
        for ev in raw_events:
            if ev.get("kind") == EVENT_KIND_DELETE:
                target = ev.get("target_seq")
                if isinstance(target, int):
                    deleted.add(target)

        hits: list[dict] = []
        for ev in raw_events:
            kind = ev.get("kind")
            if kind not in (EVENT_KIND_TEXT, EVENT_KIND_FILE):
                continue
            seq = ev.get("seq")
            if seq in deleted:
                continue
            if author_l and str(ev.get("from", "")).lower() != author_l:
                continue
            if kind == EVENT_KIND_TEXT:
                if ev.get("encrypted"):
                    continue
                text = ev.get("text")
                if not isinstance(text, str) or not text:
                    continue
                if seq in deltas:
                    ev = dict(ev)
                    for delta in deltas[seq]:
                        self.apply_delta(ev, delta)
                    text = ev.get("text") or ""
                if q in text.lower():
                    hits.append({
                        "seq": seq, "kind": "text",
                        "from": ev.get("from", ""), "ts": ev.get("ts", 0),
                        "text": text, "edited": bool(ev.get("edited")),
                    })
            else:   # EVENT_KIND_FILE - ищем по имени файла
                name = str(ev.get("name", ""))
                if q in name.lower():
                    hits.append({
                        "seq": seq, "kind": "file",
                        "from": ev.get("from", ""), "ts": ev.get("ts", 0),
                        "text": "",
                        "file": {"file_id": ev.get("file_id", ""),
                                 "name": name, "size": ev.get("size", 0),
                                 "sha256": ev.get("sha256", "")},
                    })

        hits.sort(key=lambda e: e.get("seq", 0))
        return hits[-limit:]

    # -- файловая витрина ---------------------------------------------------
    def get_file_event(self, file_id: str) -> dict | None:
        with self._lock:
            return self._file_index.get(file_id)

    def list_all_files(self) -> list[dict]:
        """Возвращает список всех файлов в индексе, от новых к старым.
        К каждому файлу добавляется holders - кто уже скачал (реестр
        владельцев, фундамент будущей multi-source раздачи)."""
        with self._lock:
            items = [dict(ev) for ev in self._file_index.values()]
            holders = {fid: sorted(users) for fid, users in self._holders.items()
                       if fid in self._file_index}
        for ev in items:
            ev["holders"] = holders.get(ev["file_id"], [])
        items.sort(key=lambda ev: ev.get("ts", 0), reverse=True)
        return items

    # -- реестр владельцев файлов (multi-source v1) -------------------------
    def register_file_holder(self, file_id: str, user: str) -> list[str] | None:
        """Отмечает, что user скачал файл и может им раздавать.
        Возвращает актуальный список владельцев или None, если файл неизвестен.
        Реестр переживает рестарт хоста (holders.json) - у формы это просто
        подсказка "у кого уже есть копия"."""
        with self._lock:
            if file_id not in self._file_index and file_id not in self._dm_file_index:
                return None
            users = self._holders.setdefault(file_id, set())
            users.add(user)
            if len(users) > FILE_HOLDERS_MAX:
                # Не даём реестру разрастаться: старые записи вытесняются
                # (list не сохраняет порядок вставки - просто обрезаем)
                for name in sorted(users)[:-FILE_HOLDERS_MAX]:
                    users.discard(name)
            result = sorted(users)
            snapshot = {fid: sorted(us) for fid, us in self._holders.items()}
        self._save_holders(snapshot)
        return result

    def _load_holders(self) -> None:
        try:
            raw = json.loads(self.holders_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for fid, users in raw.items():
                    if isinstance(users, list):
                        self._holders[fid] = {u for u in users if isinstance(u, str)}
        except (OSError, ValueError):
            pass  # нет файла/битый json - начинаем с пустого реестра

    def _save_holders(self, snapshot: dict[str, list[str]]) -> None:
        try:
            tmp = self.holders_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.holders_path)
        except OSError:
            pass  # реестр - подсказка, не критичные данные

    def file_bytes_path(self, file_id: str) -> Path:
        return self.files_dir / file_id

    def stats(self) -> dict:
        """(v2.0.2) Служебная статистика журнала для GET /server/stats:
        живое самочувствие кеша без раскрытия содержимого (ни одного
        текста/имени наружу — только счётчики). Вызывается транспортом
        через фасад; домен не знает про HTTP."""
        with self._lock:
            return {
                "events_cached": len(self._events),
                "next_seq": self._next_seq,
                "deleted_cached": len(self._deleted_seqs),
                "files_tracked": len(self._file_index),
                "dm_files_tracked": len(self._dm_file_index),
            }
