"""
ChannelManager — управление каналами (вынесено из RelayServer для разгрузки).

Каналы — это групповые чаты внутри основного чата. Каждый канал — отдельный
.jsonl-файл в channels_dir, где первая строка — метаданные (name, creator,
members), а остальные — сообщения.

Все методы потокобезопасны (через лок). RelayServer делегирует сюда вызовы
create_channel / get_channels / send_channel_message / get_channel_messages /
add_channel_member / delete_channel.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Импортируем функцию экранирования имени файла из утилит домена (раньше она
# жила в relay_server.py, и сервис зависел от фасада - перевёрнутая
# иерархия). Чтобы не плодить копии, импортируем лениво.
try:
    from lib.util import safe_name as _safe_name
except ImportError:
    # Fallback: простая реализация если импорт не сработал (например, при
    # тестировании модуля отдельно)
    import re
    _SAFE_NAME_RE = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9_\-. ]+")

    def _safe_name(name: str) -> str:
        return _SAFE_NAME_RE.sub("", name).strip()[:100]


class ChannelManager:
    """Менеджер каналов: создание, удаление, список, сообщения, участники.

    Один экземпляр на RelayServer. Хранит channels_dir, не имеет своего
    состояния между вызовами (кроме локи на _next_seq).
    """

    def __init__(self, channels_dir: Path):
        self.channels_dir = channels_dir
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        # _next_seq передаётся снаружи (RelayServer владеет глобальным seq),
        # мы его инкрементируем под своей блокировкой.
        self._seq_provider = None  # callable → int, устанавливается снаружи
        # v1.9.5: лок на ПЕРЕЗАПИСЬ файлов каналов. add_channel_member делает
        # read-all → truncate → write всего файла: параллельный
        # send_channel_message (append) между read и truncate ТЕРЯЛ
        # сообщение навсегда, а два одновременных /channel/join могли
        # интерливингом побить файл канала. Docstring класса обещал
        # «все методы потокобезопасны» — теперь это правда.
        import threading

        self._file_lock = threading.Lock()

    def set_seq_provider(self, provider) -> None:
        """Устанавливает функцию выдачи seq (RelayServer.next_seq()).
        Без этого send_channel_message вернёт seq=0.
        """
        self._seq_provider = provider

    # ── CRUD каналов ────────────────────────────────────────────────

    def create_channel(self, name: str, creator: str) -> dict:
        """Создаёт новый канал. Возвращает результат с kind=channel_created
        или {"error": ...}."""
        safe_name = _safe_name(name)
        if not safe_name:
            return {"error": "invalid_name"}

        channel_path = self.channels_dir / f"{safe_name}.jsonl"
        if channel_path.exists():
            return {"error": "channel_exists"}

        channel_data = {
            "name": name,
            "creator": creator,
            "created": int(time.time()),
            "members": [creator],
        }

        try:
            with open(channel_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(channel_data, ensure_ascii=False) + "\n")
        except OSError:
            return {"error": "io_error"}

        return {
            "kind": "channel_created",
            "name": name,
            "creator": creator,
            "safe_name": safe_name,
            "ts": int(time.time()),
        }

    def get_channels(self) -> list[dict]:
        """Возвращает список всех каналов (читает метаданные из каждого файла)."""
        channels = []
        for channel_file in self.channels_dir.glob("*.jsonl"):
            try:
                with open(channel_file, encoding="utf-8") as f:
                    first_line = f.readline()
                    if first_line:
                        channel_data = json.loads(first_line)
                        channels.append(channel_data)
            except (json.JSONDecodeError, OSError):
                continue
        return channels

    def delete_channel(self, channel_name: str, requester: str) -> dict:
        """Удаляет канал. Удалить может только создатель."""
        safe_name = _safe_name(channel_name)
        if not safe_name:
            return {"error": "invalid_name"}

        channel_path = self.channels_dir / f"{safe_name}.jsonl"
        if not channel_path.exists():
            return {"error": "channel_not_found"}

        try:
            with open(channel_path, encoding="utf-8") as f:
                first_line = f.readline()
                if not first_line:
                    return {"error": "invalid_channel"}
                channel_data = json.loads(first_line)
                creator = channel_data.get("creator", "")
        except (json.JSONDecodeError, OSError):
            return {"error": "read_failed"}

        if creator != requester:
            return {"error": "not_creator", "creator": creator}

        try:
            channel_path.unlink()
        except OSError:
            return {"error": "delete_failed"}

        return {"ok": True, "name": channel_name, "ts": int(time.time())}

    # ── Сообщения канала ────────────────────────────────────────────

    def send_channel_message(self, channel_name: str, sender: str, text: str,
                             encrypted: bool = False, iv: str = "") -> dict:
        """Записывает сообщение в файл канала. Возвращает сам message dict
        с присвоенным seq, или {"error": ...}.

        encrypted+iv — метка E2E-шифрования: текст в "text" уже зашифрован
        отправителем, получатель расшифровывает по этим полям."""
        safe_name = _safe_name(channel_name)
        channel_path = self.channels_dir / f"{safe_name}.jsonl"

        if not channel_path.exists():
            return {"error": "channel_not_found"}

        # seq выдаёт RelayServer (глобальный, чтобы он не пересекался с
        # событиями основного чата)
        seq = self._seq_provider() if self._seq_provider is not None else int(time.time() * 1000)

        message = {
            "kind": "channel_message",
            "channel": channel_name,
            "from": sender,
            "text": text,
            "seq": seq,
            "ts": int(time.time()),
        }
        if encrypted:
            message["encrypted"] = True
            message["iv"] = iv

        try:
            with open(channel_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
        except OSError:
            return {"error": "io_error"}

        return message

    def get_channel_messages(self, channel_name: str, limit: int = 50) -> list[dict]:
        """Читает сообщения из канала (последние N, по умолчанию 50)."""
        safe_name = _safe_name(channel_name)
        channel_path = self.channels_dir / f"{safe_name}.jsonl"

        if not channel_path.exists():
            return []

        messages = []
        try:
            with open(channel_path, encoding="utf-8") as f:
                lines = f.readlines()
                # Пропускаем первую строку (метаданные канала)
                for line in lines[1:]:
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            if msg.get("kind") == "channel_message":
                                messages.append(msg)
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass

        return messages[-limit:] if limit else messages

    # ── Участники канала ───────────────────────────────────────────

    def add_channel_member(self, channel_name: str, username: str) -> dict:
        """Добавляет участника в канал (записывает в метаданные).

        v1.9.5: под локом и АТОМАРНО (tmp + os.replace): раньше падение в
        середине truncate+write уничтожало канал целиком (метаданные + вся
        история), а гонка с append теряла чужие сообщения."""
        import os

        safe_name = _safe_name(channel_name)
        channel_path = self.channels_dir / f"{safe_name}.jsonl"

        if not channel_path.exists():
            return {"error": "channel_not_found"}

        with self._file_lock:
            try:
                lines = channel_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if not lines:
                    return {"error": "invalid_channel"}

                channel_data = json.loads(lines[0])
                if username in channel_data.get("members", []):
                    return {"error": "already_member"}
                channel_data["members"].append(username)

                # Атомарная перезапись: метаданные + все старые сообщения
                tmp_path = channel_path.with_suffix(".jsonl.tmp")
                tmp_path.write_text(
                    json.dumps(channel_data, ensure_ascii=False) + "\n" + "".join(lines[1:]),
                    encoding="utf-8",
                )
                os.replace(tmp_path, channel_path)

                return {
                    "kind": "channel_member_added",
                    "channel": channel_name,
                    "username": username,
                    "ts": int(time.time()),
                }
            except (json.JSONDecodeError, OSError):
                channel_path.with_suffix(".jsonl.tmp").unlink(missing_ok=True)
                return {"error": "update_failed"}
