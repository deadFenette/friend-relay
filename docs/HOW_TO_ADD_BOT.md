# Как добавить своего бота-игру

Этот гайд объясняет, как добавить в чат нового бота с игрой — по образцу
`snake` (змейка) и `orbital` (космическая аркада). После этого игроки смогут
писать в чате `!mygame` — и откроется окно с игрой.

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│  Чат (chat_screen.py)                                       │
│    _on_send() перехватывает всё что начинается с "!"        │
│    и шлёт это на /dispatch                                  │
└────────────────────────┬────────────────────────────────────┘
                         │ POST /dispatch {text: "!mygame"}
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Сервер (relay_server.py)                                   │
│    /dispatch → bot_manager.dispatch_command(sender, text)   │
│    Перебирает всех ботов, вызывает matches_command(text)    │
│    Первый совпавший выполняет process_command(...)         │
└────────────────────────┬────────────────────────────────────┘
                         │ {"action": "open_game", "meta": {...}}
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Чат получает ответ с bot_action                            │
│    _on_dispatch_result() смотрит action="open_game"         │
│    и открывает нужный диалог (SnakeGameDialog /             │
│    OrbitalGameDialog / твой MyGameDialog)                   │
└─────────────────────────────────────────────────────────────┘
```

Бот состоит из 4 частей:

| Часть | Файл | Зачем |
|-------|------|-------|
| Логика бота | `lib/bots/<name>.py` | Команды, хранение trigger и scores |
| Игровой движок | `lib/games/<name>/` (опц.) | Чистая логика без Qt |
| Виджет игры | `qt_app/widgets/games/<name>_canvas.py` или прямо в диалоге | QPainter-отрисовка |
| Диалог-обёртка | `qt_app/widgets/<name>_game_dialog.py` | Окно поверх чата + таблица |

---

## Пошаговая инструкция

### 1. Создай файл `lib/bots/<name>.py`

Скопируй `lib/bots/snake.py` или `lib/bots/orbital.py` как образец.
Минимальный каркас:

```python
from __future__ import annotations

import re
import time
from pathlib import Path

from lib.bots.base import BaseBot


_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")


class MyGameBot(BaseBot):
    """Бот «My Game» — короткое описание."""

    def __init__(self, data_dir: Path):
        super().__init__("mygame", "My Game", data_dir)  # bot_id, name
        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "mygame")  # триггер по умолчанию
            self.set_data("scores", [])

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "mygame")

    # ── Dispatcher hooks ───────────────────────────────────────

    def matches_command(self, text: str) -> bool:
        if not text or not text.startswith("!"):
            return False
        text = text.strip()
        t = self.trigger
        return (
            text == f"!{t}"
            or text.startswith(f"!{t} ")
            or text.lower() in (f"!help {t}", "!help mygame")
        )

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        t = self.trigger
        low = text.lower()
        if low in (f"!help {t}", "!help mygame"):
            return ("help", [])
        rest = text[len(f"!{t}"):].strip()
        if not rest:
            return ("", [])
        parts = rest.split()
        return (parts[0], parts[1:])

    def get_meta(self) -> dict:
        return {
            "trigger": self.trigger,
            "name": self.name,
            "tick_ms": 16,           # частота обновления игры
            "game": "mygame",        # KEY: идентификатор для диалога
        }

    # ── Команды ─────────────────────────────────────────────────

    def process_command(self, sender: str, command: str, args: list[str]) -> str | dict:
        cmd = (command or "").lower()
        if cmd == "":
            return {"action": "open_game", "tab": "play", "meta": self.get_meta()}
        if cmd == "top":
            return {"action": "open_game", "tab": "leaderboard", "meta": self.get_meta()}
        if cmd == "help":
            return self.get_help()
        if cmd == "trigger":
            return self._cmd_trigger(args)
        if cmd == "savescore":
            return self._cmd_save_score(sender, args)
        if cmd == "getscores":
            return {"action": "scores", "scores": self._top_scores(50)}
        if cmd == "meta":
            return {"action": "meta", **self.get_meta()}
        return f"❌ Неизвестная подкоманда. Напиши !{self.trigger} help"

    def _cmd_trigger(self, args: list[str]) -> str:
        if not args:
            return f"Текущий триггер: !{self.trigger}"
        new = args[0].strip().lower()
        if not _TRIGGER_RE.match(new):
            return "❌ Недопустимый триггер."
        old = self.trigger
        self.set_data("trigger", new)
        return f"✅ Триггер: !{old} → !{new}"

    def _cmd_save_score(self, sender: str, args: list[str]) -> dict:
        # ... см. bots/snake.py / bots/orbital.py для полной реализации
        ...

    def _top_scores(self, limit: int = 10) -> list[dict]:
        return self.get_data("scores", [])[:limit]

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"🎮 My Game — описание\n"
            f"─────────────────────────────────────────────\n"
            f"!{t}         — начать игру\n"
            f"!{t} top     — таблица рекордов\n"
            f"!{t} trigger <name> — сменить триггер\n"
            f"!{t} help    — справка\n\n"
            f"Управление:\n"
            f"  ... (описание клавиш)\n"
        )
```

### 2. Зарегистрируй бота в сервере

В `lib/relay_server.py` найди метод `_register_default_bots()` и добавь блок:

```python
try:
    from lib.bots.mygame import MyGameBot

    bot_manager = self.get_bot_manager()
    mygame_bot = MyGameBot(self.data_dir)
    bot_manager.register_bot(mygame_bot)
except Exception:
    pass
```

### 3. Создай диалог игры `qt_app/widgets/mygame_game_dialog.py`

Скопируй `qt_app/widgets/snake_game_dialog.py` или `orbital_game_dialog.py`
как образец. Ключевые моменты:

- Класс наследуется от `QDialog`
- Конструктор принимает одинаковый набор аргументов:
  ```python
  def __init__(self, parent, base_url, player_name, access_key,
               bot_id, bot_meta, initial_tab):
  ```
- При game over вызывает `client.send_bot_command_full(...)` с
  `command="savescore"` и `args=[str(score), ...]`
- При открытии и после сохранения дёргает `client.send_bot_command_full(...)` с
  `command="getscores"` для обновления таблицы справа

### 4. Свяжи диалог с чатом

В `qt_app/screens/chat_screen.py` найди метод `_open_game_dialog()` и
добавь свой диалог в реестр:

```python
elif game == "mygame" or bot_id == "mygame":
    from qt_app.widgets.mygame_game_dialog import MyGameGameDialog
    dialog_cls = MyGameGameDialog
```

### 5. Готово!

Запусти проект, в чате пиши `!mygame` — откроется окно игры.

---

## Когда нужно разбить игру на несколько файлов?

Если вся игра помещается в 200-300 строк и логически простая — оставляй
в одном файле (`lib/games/<name>.py` + диалог).

Разбивай на модули если:

- **Сложная физика/логика** — выноси в `lib/games/<name>/`
  (чистый Python, без Qt). Так проще тестировать.
- **Много сущностей** (враги, пули, бонусы) — выноси в
  `lib/games/<name>/entities.py` как dataclasses
- **Сложная отрисовка** — выноси canvas в
  `qt_app/widgets/games/<name>_canvas.py` (Qt — только во View-слое),
  отдельно от диалога-обёртки

Структура пакета (как у `orbital`):

```
lib/
  games/
    orbital/
      __init__.py     # пакет
      entities.py     # Asteroid, Laser, Particle, PowerUp (dataclasses)
      engine.py       # OrbitalEngine — игровая логика, без Qt
qt_app/widgets/
  games/
    orbital_canvas.py # OrbitalGameWidget — QWidget с QPainter
  orbital_game_dialog.py  # QDialog обёртка: поле + таблица + кнопки
```

## Запуск тестов

```bash
# Серверные тесты (без GUI):
python scripts/orbital_e2e_test.py
python scripts/snake_e2e_test.py

# Линт:
ruff check .
```
