"""ChessBot — бот «Красивые Шахматы»: ИИ, локальный PVP и онлайн-PVP по сети.

Структура полностью аналогична snake_bot.py / orbital_bot.py:
  - хранит trigger (по умолчанию "chess") и leaderboard (ELO) в JSON
  - matches_command ловит !chess, !chess top, !chess trigger X, !chess help, !help chess
  - process_command возвращает dict с action="open_game" для открытия окна игры

UI-часть (движок, доска, ИИ, диалог) живёт в qt_app/widgets/chess_game_dialog.py —
этот файл содержит ТОЛЬКО серверную часть бота (без Qt-импортов), чтобы
lib/relay_server.py мог зарегистрировать бота даже на безголовом хосте без GUI.

Поддерживается ТРИ режима игры (переключается кнопкой в диалоге):
  • vs ИИ      — игрок (белые) против встроенного ChessAI (чёрные).
                 Результат сохраняется на сервере и учитывается в ELO.
  • PVP local  — два игрока за одним компьютером (hot-seat). ELO не сохраняется.
  • PVP online — два игрока через relay-сервер, каждый у себя. Хост создаёт
                 матч (!chess host), второй присоединяется (!chess join <id>).
                 Ходы передаются через !chess move <uci>. Результат матча
                 учитывается в ELO обоих игроков.

Онлайн-PVP хранится в self._data["matches"] — словарь match_id → {
    "white": str, "black": str | None, "moves": [uci_str, ...],
    "status": "waiting" | "playing" | "finished",
    "result": "white" | "black" | "draw" | None,
    "created_at": int, "updated_at": int,
}
"""
from __future__ import annotations

import re
import string
import time
from pathlib import Path

from lib.bots.base import BaseBot

_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")

_DEFAULT_ELO = 1200
_K_FACTOR = 24  # шаг изменения ELO за партию

# Сколько секунд матч ждёт второго игрока, прежде чем истечь.
_MATCH_WAIT_TIMEOUT = 30 * 60
# Сколько секунд с последнего обновления матч считается «брошенным».
_MATCH_ABANDON_TIMEOUT = 60 * 60


class ChessBot(BaseBot):
    """Бот 'Красивые Шахматы' — полноценная шахматная игра с ИИ."""

    def __init__(self, data_dir: Path):
        super().__init__("chess", "Красивые Шахматы", data_dir)

        self._tick_ms = 50

        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "chess")
            self.set_data("players", {})  # name -> {elo, wins, losses, draws}
            self.set_data("games_played", 0)
            self.set_data("matches", {})   # match_id -> online PVP match state

    # ------------------------------------------------------------------
    # Dispatcher hooks
    # ------------------------------------------------------------------

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "chess")

    def matches_command(self, text: str) -> bool:
        if not text:
            return False
        text = text.strip()
        if not text.startswith("!"):
            return False
        trigger = self.trigger
        if text == f"!{trigger}":
            return True
        if text.startswith(f"!{trigger} "):
            return True
        if text.lower() == f"!help {trigger}":
            return True
        if text.lower() == "!help chess":
            return True
        return False

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        trigger = self.trigger
        low = text.lower()
        if low == f"!help {trigger}" or low == "!help chess":
            return ("help", [])
        rest = text[len(f"!{trigger}"):].strip()
        if not rest:
            return ("", [])
        parts = rest.split()
        return (parts[0], parts[1:])

    def get_meta(self) -> dict:
        return {
            "trigger": self.trigger,
            "name": self.name,
            "tick_ms": self._tick_ms,
        }

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

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

        if cmd == "stats":
            return self._cmd_stats(sender)

        if cmd == "result":
            # args[0] in {"win", "loss", "draw"} — результат партии против ИИ
            return self._cmd_result(sender, args)

        if cmd == "getscores":
            # silent=True — поллинговая команда, не должна спамить чат
            return {"action": "scores", "scores": self._top_players(50), "silent": True}

        # ─── Онлайн-PVP команды ──────────────────────────────────────
        if cmd == "host":
            return self._cmd_host(sender)

        if cmd == "join":
            return self._cmd_join(sender, args)

        if cmd == "move":
            return self._cmd_move(sender, args)

        if cmd == "state":
            return self._cmd_state(sender, args)

        if cmd == "leave":
            return self._cmd_leave(sender)

        if cmd == "resign":
            return self._cmd_resign(sender)

        if cmd == "matchlist":
            return self._cmd_matchlist(sender)

        if cmd == "meta":
            # silent=True — поллинговая команда
            return {"action": "meta", **self.get_meta(), "silent": True}

        return f"❌ Неизвестная подкоманда. Напиши !{self.trigger} help"

    # ------------------------------------------------------------------
    # Подкоманды
    # ------------------------------------------------------------------

    def _cmd_trigger(self, args: list[str]) -> str:
        if not args:
            return f"Текущий триггер: !{self.trigger}\nЧтобы сменить: !{self.trigger} trigger <новый>"

        new_trigger = args[0].strip().lower()
        if not _TRIGGER_RE.match(new_trigger):
            return (f"❌ Триггер '{new_trigger}' недопустим. "
                    "Буквы/цифры/_, 2-31 символ, начинается с буквы.")

        old = self.trigger
        self.set_data("trigger", new_trigger)
        return (f"✅ Триггер сменён: !{old} → !{new_trigger}\n"
                f"Теперь игра открывается по команде !{new_trigger}")

    def _get_player(self, name: str) -> dict:
        players = self.get_data("players", {})
        return dict(players.get(name, {"elo": _DEFAULT_ELO, "wins": 0, "losses": 0, "draws": 0}))

    def _cmd_stats(self, sender: str) -> str:
        stats = self._get_player(sender)
        return (f"📊 Статистика {sender}:\n"
                f"ELO: {stats['elo']} | Побед: {stats['wins']} | "
                f"Поражений: {stats['losses']} | Ничьих: {stats['draws']}")

    def _cmd_result(self, sender: str, args: list[str]) -> dict:
        """Сохраняет результат партии игрока против встроенного ИИ и
        обновляет ELO простым правилом (ИИ считается соперником с фикс.
        рейтингом ~1200, чтобы шкала была осмысленной и без второго
        сетевого игрока)."""
        outcome = (args[0].lower() if args else "").strip()
        if outcome not in ("win", "loss", "draw"):
            return {"action": "error", "text": "❌ Неверный результат (win/loss/draw)"}

        players = self.get_data("players", {})
        stats = players.get(sender, {"elo": _DEFAULT_ELO, "wins": 0, "losses": 0, "draws": 0})

        opponent_elo = _DEFAULT_ELO
        expected = 1 / (1 + 10 ** ((opponent_elo - stats["elo"]) / 400))
        score = {"win": 1.0, "loss": 0.0, "draw": 0.5}[outcome]
        stats["elo"] = round(stats["elo"] + _K_FACTOR * (score - expected))
        if outcome == "win":
            stats["wins"] = stats.get("wins", 0) + 1
        elif outcome == "loss":
            stats["losses"] = stats.get("losses", 0) + 1
        else:
            stats["draws"] = stats.get("draws", 0) + 1

        players[sender] = stats
        self.set_data("players", players)
        self.set_data("games_played", self.get_data("games_played", 0) + 1)

        label = {"win": "победа 🏆", "loss": "поражение", "draw": "ничья"}[outcome]
        return {
            "action": "score_saved",
            "rank": self._rank_of(sender),
            "best": stats["elo"],
            "text": f"♟ {sender}: {label}. Новый ELO: {stats['elo']}",
        }

    # ------------------------------------------------------------------
    # Хелперы для таблицы
    # ------------------------------------------------------------------

    def _top_players(self, limit: int = 10) -> list[dict]:
        players: dict = self.get_data("players", {})
        rows = [
            {"name": name, "score": s.get("elo", _DEFAULT_ELO),
             "wins": s.get("wins", 0), "losses": s.get("losses", 0),
             "draws": s.get("draws", 0)}
            for name, s in players.items()
        ]
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit]

    def _rank_of(self, name: str) -> int | None:
        rows = self._top_players(10_000)
        for i, r in enumerate(rows):
            if r["name"] == name:
                return i + 1
        return None

    # ------------------------------------------------------------------
    # Онлайн-PVP: матчи по сети между двумя игроками
    # ------------------------------------------------------------------
    #
    # Поток:
    #   1. Alice: !chess host         → сервер создаёт match_id, Alice = white
    #   2. Bob:   !chess join ABCD    → сервер добавляет Bob = black, status=playing
    #   3. Alice: !chess move e2e4   → сервер валидирует ход, добавляет в moves[]
    #   4. Bob:   !chess state        → получает новый state с ходом Alice
    #   5. ... ходы по очереди ...
    #   6. При мате/пате/ничьей сервер сам обновляет ELO обоих игроков.
    #
    # Ходы в UCI-нотации: "e2e4", "g1f3", "e7e8q" (с превращением).
    # Валидация и применение ходов выполняются через ChessEngine (тот же
    # движок, что и в UI) — мы импортируем его лениво внутри методов, чтобы
    # не тащить Qt-зависимости в lib/.

    @staticmethod
    def _gen_match_id() -> str:
        """4-значный код в верхнем регистре (легко продиктовать другу).
        secrets, а не random (v1.9.5): Mersenne Twister предсказуем —
        перебор/подсчёт состояния позволял угадывать чужие коды матчей
        и влетать в чужую партию через `chess join`."""
        import secrets

        return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))

    def _get_matches(self) -> dict:
        return self.get_data("matches", {}) or {}

    def _save_matches(self, matches: dict) -> None:
        self.set_data("matches", matches)

    def _find_match_by_player(self, player: str, include_finished: bool = False) -> tuple[str, dict] | None:
        """Возвращает (match_id, match) для матча игрока или None.

        По умолчанию ищет только активные (waiting/playing) матчи. Если
        include_finished=True — возвращает и завершённые (нужно для
        команды state, чтобы клиент мог увидеть финальный результат)."""
        matches = self._get_matches()
        for mid, m in matches.items():
            if m.get("status") == "finished" and not include_finished:
                continue
            if player in (m.get("white"), m.get("black")):
                return mid, m
        return None

    @staticmethod
    def _match_to_public(mid: str, m: dict) -> dict:
        """Превращает внутренний объект матча в публичный (для отправки клиентам).
        Внутреннее поле 'white'/'black' = имена игроков; клиенту они тоже нужны,
        чтобы понять, кто за какой цвет играет."""
        return {
            "match_id": mid,
            "white": m.get("white"),
            "black": m.get("black"),
            "status": m.get("status"),
            "moves": list(m.get("moves", [])),
            "result": m.get("result"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
        }

    def _cmd_host(self, sender: str) -> dict:
        """Создать новый онлайн-матч. Хост автоматически играет белыми.

        v3.1: если у игрока есть ЗАВЕРШЁННЫЙ матч — автоматически удаляем
        его (игрок уже увидел результат, нет смысла требовать ручной leave).
        Это чинит «вечный цикл выигрыша»: раньше после финала игрок не мог
        создать новую игру, потому что старый finished-матч блокировал host.
        Активный (waiting/playing) матч по-прежнему блокирует host.
        """
        existing = self._find_match_by_player(sender, include_finished=True)
        if existing:
            mid, m = existing
            if m.get("status") == "finished":
                # v3.1: авто-очистка завершённого матча — даём создать новый.
                matches = self._get_matches()
                matches.pop(mid, None)
                self._save_matches(matches)
            else:
                # Активный (waiting/playing) матч — не даём создать второй.
                return {
                    "action": "match_state",
                    "match": self._match_to_public(mid, m),
                    "text": (f"♟ У тебя уже есть активный матч {mid} "
                             f"(статус: {m['status']}). "
                             f"Используй !{self.trigger} leave, чтобы выйти."),
                }
        # Чистим протухшие матчи (ждавшие > 30 мин или заброшенные > 1 ч).
        self._cleanup_matches()

        matches = self._get_matches()
        # Генерируем уникальный 4-значный ID.
        for _ in range(100):
            mid = self._gen_match_id()
            if mid not in matches:
                break
        else:
            return {"action": "error", "text": "❌ Не удалось сгенерировать ID матча. Попробуй ещё раз."}

        now = int(time.time())
        matches[mid] = {
            "white": sender,
            "black": None,
            "moves": [],
            "status": "waiting",
            "result": None,
            "created_at": now,
            "updated_at": now,
        }
        self._save_matches(matches)
        # v3: ссылка-приглашение кликабельна в чате (markdown).
        # Другие игроки видят её и могут присоединиться одним кликом —
        # без ручного ввода кода. Схема friendrelay://chess/join/<mid>
        # обрабатывается в PySide6 (message_bubble) и в веб-клиенте (chat.js).
        invite_link = f"[▶ Присоединиться к матчу {mid}](friendrelay://chess/join/{mid})"
        return {
            "action": "match_created",
            "match": self._match_to_public(mid, matches[mid]),
            "text": (f"🌐 Матч создан! ID: {mid}\n"
                     f"Скинь другу ссылку — он присоединится одним кликом:\n"
                     f"{invite_link}\n"
                     f"Ты играешь белыми. Ожидание соперника…"),
        }

    def _cmd_join(self, sender: str, args: list[str]) -> dict:
        """Присоединиться к матчу по его ID. Присоединившийся играет чёрными."""
        if not args:
            return {"action": "error",
                    "text": f"❌ Укажи код матча: !{self.trigger} join ABCD"}
        mid = args[0].strip().upper()
        if len(mid) != 4:
            return {"action": "error", "text": "❌ Код матча — 4 символа (например, ABCD)"}

        # Если уже в каком-то матче — сначала выходим.
        existing = self._find_match_by_player(sender)
        if existing and existing[0] != mid:
            return {
                "action": "error",
                "text": (f"❌ Ты уже в матче {existing[0]}. "
                         f"Сначала !{self.trigger} leave"),
            }

        matches = self._get_matches()
        m = matches.get(mid)
        if not m:
            return {"action": "error", "text": f"❌ Матч {mid} не найден"}
        if m["status"] == "finished":
            return {"action": "error", "text": f"❌ Матч {mid} уже завершён"}
        if m["status"] == "playing" and m.get("black") and m["black"] != sender:
            return {"action": "error",
                    "text": f"❌ Матч {mid} уже начат другим игроком"}
        if m["white"] == sender:
            return {"action": "error",
                    "text": "❌ Нельзя присоединиться к своему же матчу"}

        m["black"] = sender
        m["status"] = "playing"
        m["updated_at"] = int(time.time())
        matches[mid] = m
        self._save_matches(matches)

        # v3: silent=True — подтверждение присоединения видит ТОЛЬКО
        # присоединившийся (в его диалоге/вкладке). В общий чат не идёт —
        # нет спама. Соперник узнаёт о присоединении через поллинг state.
        return {
            "action": "match_joined",
            "match": self._match_to_public(mid, m),
            "text": (f"🌐 Ты присоединился к матчу {mid}!\n"
                     f"Белые: {m['white']}  ·  Чёрные: {m['black']}\n"
                     f"Ты играешь чёрными. Жди хода белых."),
            "silent": True,
        }

    def _cmd_move(self, sender: str, args: list[str]) -> dict:
        """Сделать ход в онлайн-матче. UCI-формат: e2e4, g1f3, e7e8q."""
        if not args:
            return {"action": "error", "text": f"❌ Укажи ход: !{self.trigger} move e2e4"}

        uci = args[0].strip().lower()
        existing = self._find_match_by_player(sender)
        if not existing:
            return {"action": "error",
                    "text": "❌ Ты не в активном матче. Создай: !chess host"}
        mid, m = existing
        if m["status"] != "playing":
            return {"action": "error",
                    "text": "❌ Матч ещё не начат (ждёт второго игрока)"}

        # Чей ход?
        is_white_turn = (len(m["moves"]) % 2 == 0)
        my_color = "white" if m["white"] == sender else "black"
        if (my_color == "white") != is_white_turn:
            return {"action": "error", "text": "❌ Сейчас не твой ход"}

        # Валидация хода через движок.
        ok, err, new_state = self._apply_move_to_match(m, uci)
        if not ok:
            return {"action": "error", "text": f"❌ Неверный ход: {err}"}

        matches = self._get_matches()
        matches[mid] = new_state
        self._save_matches(matches)

        # Если игра закончилась — обновляем ELO обоих.
        if new_state["status"] == "finished":
            self._finalize_match_elo(new_state)
            # v3: silent=True — финал матча видят оба игрока в их диалогах
            # через поллинг state. В общий чат не идёт — нет спама.
            return {
                "action": "match_finished",
                "match": self._match_to_public(mid, new_state),
                "text": (f"🏁 Матч {mid} завершён. "
                         f"Результат: {new_state['result']}"),
                "silent": True,
            }

        return {
            "action": "move_accepted",
            "match": self._match_to_public(mid, new_state),
        }

    def _apply_move_to_match(self, m: dict, uci: str) -> tuple[bool, str, dict | None]:
        """Восстанавливает движок из m['moves'], применяет uci, проверяет мат/пат.
        Возвращает (ok, error, new_match_state)."""
        # Движок живёт в lib/chess_engine.py (без Qt) — сервер валидирует
        # ходы на безголовом хосте без PySide6.
        from lib.games.chess_engine import ChessEngine

        eng = ChessEngine()
        for prev_uci in m.get("moves", []):
            mv = self._uci_to_move(eng, prev_uci)
            if mv is None:
                return False, "внутренняя ошибка (невосстановимая история)", None
            eng.make_move(mv)

        mv = self._uci_to_move(eng, uci)
        if mv is None:
            return False, f"не удалось разобрать '{uci}'", None
        if mv not in eng.generate_moves():
            return False, f"ход {uci} нелегален", None

        eng.make_move(mv)
        new_moves = [*m.get("moves", []), uci]
        new_state = dict(m)
        new_state["moves"] = new_moves
        new_state["updated_at"] = int(time.time())

        if eng.is_checkmate():
            new_state["status"] = "finished"
            # Чей сейчас ход = тот и проиграл (ему поставили мат).
            new_state["result"] = "black" if eng.white_to_move else "white"
        elif eng.is_stalemate():
            new_state["status"] = "finished"
            new_state["result"] = "draw"
        # Ничья по правилу 50 ходов / недостаточному материалу — здесь не
        # проверяем для простоты, можно добавить отдельно.
        return True, "", new_state

    @staticmethod
    def _uci_to_move(eng, uci: str) -> tuple[int, int, str | None] | None:
        """Конвертирует UCI-строку (e2e4 / e7e8q) в tuple (fr, to, promo)
        для ChessEngine.make_move. None если строка невалидного формата."""
        if not isinstance(uci, str) or len(uci) not in (4, 5):
            return None
        uci = uci.strip().lower()
        if len(uci) not in (4, 5):
            return None
        files = "abcdefgh"
        try:
            f1 = files.index(uci[0])
            r1 = 8 - int(uci[1])
            f2 = files.index(uci[2])
            r2 = 8 - int(uci[3])
        except (ValueError, IndexError):
            return None
        if not (0 <= f1 < 8 and 0 <= r1 < 8 and 0 <= f2 < 8 and 0 <= r2 < 8):
            return None
        fr = eng.idx(r1, f1)
        to = eng.idx(r2, f2)
        promo = None
        if len(uci) == 5:
            p = uci[4].upper()
            if p not in "QRBN":
                return None
            promo = p
        return (fr, to, promo)

    def _cmd_state(self, sender: str, args: list[str]) -> dict:
        """Получить текущее состояние матча (поллинг со стороны клиента).
        Можно указать match_id явно, иначе ищется активный матч игрока
        (включая завершённые — чтобы клиент увидел финальный результат).

        ВСЕ ответы помечены silent=True — это поллинговая команда, вызывается
        каждые 800мс. Без silent чат заспамливался бы сообщениями «У тебя нет
        активного матча» и подобными.
        """
        matches = self._get_matches()
        mid = None
        if args:
            mid = args[0].strip().upper()
        if mid is None:
            existing = self._find_match_by_player(sender, include_finished=True)
            if not existing:
                return {"action": "match_state", "match": None,
                        "text": "У тебя нет активного матча", "silent": True}
            mid, m = existing
        else:
            m = matches.get(mid)
            if not m:
                return {"action": "error", "text": f"❌ Матч {mid} не найден",
                        "silent": True}
        return {"action": "match_state", "match": self._match_to_public(mid, m),
                "silent": True}

    def _cmd_leave(self, sender: str) -> dict:
        """Выйти из активного матча. Если игра ещё шла — соперник получает
        победу (как при сдаче). Если матч уже завершён — просто удаляет его
        из списка (освобождает игрока)."""
        existing = self._find_match_by_player(sender, include_finished=True)
        if not existing:
            return {"action": "error", "text": "У тебя нет активного матча"}
        mid, m = existing
        matches = self._get_matches()

        if m["status"] == "playing":
            # Активная игра — фиксируем победу соперника.
            winner = "black" if m["white"] == sender else "white"
            m["status"] = "finished"
            m["result"] = winner
            m["updated_at"] = int(time.time())
            matches[mid] = m
            self._save_matches(matches)
            self._finalize_match_elo(m)
            # v3: silent=True — выход из матча видит только ушедший
            # (в его диалоге). Соперник узнаёт через поллинг state.
            return {
                "action": "match_finished",
                "match": self._match_to_public(mid, m),
                "text": f"🌐 Ты вышел из матча {mid}. Победа присуждена сопернику.",
                "silent": True,
            }
        # Матч ещё не начат (waiting) или уже завершён (finished) — просто удаляем.
        del matches[mid]
        self._save_matches(matches)
        return {
            "action": "match_left",
            "match_id": mid,
            "text": f"🌐 Матч {mid} отменён.",
            "silent": True,
        }

    def _cmd_resign(self, sender: str) -> dict:
        """Сдаться в активной игре. Соперник получает победу."""
        existing = self._find_match_by_player(sender)
        if not existing:
            return {"action": "error", "text": "У тебя нет активного матча"}
        mid, m = existing
        if m["status"] != "playing":
            return {"action": "error",
                    "text": "❌ Матч ещё не начат. Используй !chess leave"}
        matches = self._get_matches()
        winner = "black" if m["white"] == sender else "white"
        m["status"] = "finished"
        m["result"] = winner
        m["updated_at"] = int(time.time())
        matches[mid] = m
        self._save_matches(matches)
        self._finalize_match_elo(m)
        # v3: silent=True — сдача видит только сдавшийся. Соперник узнаёт
        # через поллинг state (матч завершится, результат обновится).
        return {
            "action": "match_finished",
            "match": self._match_to_public(mid, m),
            "text": f"🏳 Ты сдался в матче {mid}. Победа присуждена сопернику.",
            "silent": True,
        }

    def _cmd_matchlist(self, sender: str) -> dict:
        """Список матчей, ожидающих второго игрока (status=waiting).
        Нужен, чтобы клиент мог показать «открытые лобби»."""
        matches = self._get_matches()
        now = int(time.time())
        open_matches = []
        for mid, m in matches.items():
            if m["status"] != "waiting":
                continue
            if now - m.get("created_at", 0) > _MATCH_WAIT_TIMEOUT:
                continue
            open_matches.append({
                "match_id": mid,
                "white": m.get("white"),
                "created_at": m.get("created_at"),
            })
        return {"action": "match_list", "matches": open_matches, "silent": True}

    def _cleanup_matches(self) -> None:
        """Удаляет протухшие матчи (ждавшие > 30 мин или заброшенные > 1 ч).
        Сохраняет результат «висящих» playing-матчей как ничью по таймауту."""
        matches = self._get_matches()
        if not matches:
            return
        now = int(time.time())
        changed = False
        for mid in list(matches.keys()):
            m = matches[mid]
            age = now - m.get("updated_at", m.get("created_at", now))
            if m["status"] == "waiting" and age > _MATCH_WAIT_TIMEOUT:
                del matches[mid]
                changed = True
            elif m["status"] == "playing" and age > _MATCH_ABANDON_TIMEOUT:
                m["status"] = "finished"
                m["result"] = "draw"
                m["updated_at"] = now
                matches[mid] = m
                changed = True
                self._finalize_match_elo(m)
        if changed:
            self._save_matches(matches)

    def _finalize_match_elo(self, m: dict) -> None:
        """Применяет результат онлайн-матча к ELO обоих игроков.
       winner → win, loser → loss, draw → draw для обоих."""
        result = m.get("result")
        white = m.get("white")
        black = m.get("black")
        if not white or not black or not result:
            return
        if result == "white":
            self._record_outcome(white, "win")
            self._record_outcome(black, "loss")
        elif result == "black":
            self._record_outcome(white, "loss")
            self._record_outcome(black, "win")
        elif result == "draw":
            self._record_outcome(white, "draw")
            self._record_outcome(black, "draw")

    def _record_outcome(self, name: str, outcome: str) -> None:
        """Записывает результат в ELO игрока (без изменения ELO соперника —
        для онлайн-PVP оба вызова делаются отдельно в _finalize_match_elo)."""
        if outcome not in ("win", "loss", "draw"):
            return
        players = self.get_data("players", {})
        stats = players.get(name, {"elo": _DEFAULT_ELO, "wins": 0, "losses": 0, "draws": 0})
        # ELO меняем относительно среднего соперника (т.е. ~1200), как и в vs ИИ.
        opponent_elo = _DEFAULT_ELO
        expected = 1 / (1 + 10 ** ((opponent_elo - stats["elo"]) / 400))
        score = {"win": 1.0, "loss": 0.0, "draw": 0.5}[outcome]
        stats["elo"] = round(stats["elo"] + _K_FACTOR * (score - expected))
        if outcome == "win":
            stats["wins"] = stats.get("wins", 0) + 1
        elif outcome == "loss":
            stats["losses"] = stats.get("losses", 0) + 1
        else:
            stats["draws"] = stats.get("draws", 0) + 1
        players[name] = stats
        self.set_data("players", players)
        self.set_data("games_played", self.get_data("games_played", 0) + 1)

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"♔ Красивые Шахматы — ИИ / PVP локал / PVP по сети\n"
            f"─────────────────────────────────────\n"
            f"!{t}         — открыть игру\n"
            f"!{t} top     — таблица рейтинга (ELO)\n"
            f"!{t} stats   — твоя статистика\n"
            f"!{t} trigger <новый>  — сменить триггер\n"
            f"!{t} help    — эта справка\n\n"
            f"Режимы игры (переключаются в окне):\n"
            f"  🤖 vs ИИ      — ты (белые) против движка (чёрные).\n"
            f"                  Результат сохраняется в ELO.\n"
            f"  👥 PVP local  — два игрока за одним компьютером,\n"
            f"                  ходят по очереди. ELO не сохраняется.\n"
            f"  🌐 PVP сеть   — игра с другом по сети через relay.\n"
            f"                  Хост создаёт лобби, друг присоединяется\n"
            f"                  по 4-значному коду. ELO учитывается у обоих.\n\n"
            f"Сетевые команды (также доступны из диалога):\n"
            f"  !{t} host       — создать матч (ты белые), получить код\n"
            f"  !{t} join <код> — присоединиться к матчу (ты чёрные)\n"
            f"  !{t} move <uci> — сделать ход (напр. e2e4, g1f3, e7e8q)\n"
            f"  !{t} state      — текущее состояние матча\n"
            f"  !{t} resign     — сдаться\n"
            f"  !{t} leave      — выйти из матча (соперник победит)\n"
            f"  !{t} matchlist  — список открытых лобби\n\n"
            f"Скины фигур (3 стиля, выбор сохраняется):\n"
            f"  ♔ Стандарт  — классические Unicode-силуэты Staunton\n"
            f"  ⛧ Готика    — острые угловатые «резные» фигуры\n"
            f"  ▣ Квадрат   — блочные 8-битные силуэты\n\n"
            f"Управление в игре:\n"
            f"  ЛКМ — выбрать фигуру и ходить\n"
            f"  F   — перевернуть доску\n"
            f"  R   — новая партия\n"
            f"  P   — сменить режим\n"
            f"  1/2/3 — сложность ИИ (лёгкая/средняя/сложная)\n\n"
            f"Все правила: рокировка, взятие на проходе,\n"
            f"превращение пешки (с выбором фигуры), шах/мат/пат.\n"
        )
