"""GoBot — бот «Го»: онлайн-PVP по сети (рефери) + рейтинг ELO.

Структура полностью аналогична chess.py (Шахматы):
  - хранит trigger (по умолчанию "go"), players (ELO) и matches в JSON;
  - matches_command ловит !go / !go <подкоманда> / !help go;
  - process_command возвращает dict с action="open_game" для открытия
    окна игры в Qt-клиенте (lib/bots/go.py — только серверная часть,
    без Qt-импортов, работает на безголовом хосте).

UI-часть (доска, ИИ, сетевой матч) живёт в qt_app/widgets/go_game_dialog.py
(ядро сети — lib/client_core/go_match.py), веб-версия — web_client/games/
go.html (одиночная: ИИ + hot-seat).

Онлайн-PVP (рефери на сервере, движок lib/games/go_engine.py):
  1. Alice: !go host        → match_id, Alice = чёрные (в Го начинают чёрные)
  2. Bob:   !go join ABCD   → Bob = белые, статус playing
  3. Alice: !go move D4     → движок валидирует (взятия/ко/суицид), ход в moves[]
  4. Bob:   !go move pass   → пас; два паса подряд → подсчёт по площади
  5. Финал: ELO обоих игроков обновляется сервером.

Ходы хранятся СПИСКОМ ВЕРШИН ("D4", "pass", …) — цвета чередуются,
чёрные первые. Каждое применение хода валидируется заново через GoEngine:
сервер — единственный арбитр правил, клиент не может «собой играет».
"""
from __future__ import annotations

import re
import string
import time
from pathlib import Path

from lib.bots.base import BaseBot
from lib.games.go_engine import (
    BLACK,
    WHITE,
    GoEngine,
    GoError,
    format_vertex,
    parse_vertex,
)

_TRIGGER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,30}$")

_DEFAULT_ELO = 1200
_K_FACTOR = 24

# Сколько секунд матч ждёт второго игрока, прежде чем истечь.
_MATCH_WAIT_TIMEOUT = 30 * 60
# Сколько секунд с последнего хода матч считается заброшенным.
_MATCH_ABANDON_TIMEOUT = 2 * 60 * 60


class GoBot(BaseBot):
    """Бот «Го» — referee онлайн-матчей + рейтинг ELO."""

    def __init__(self, data_dir: Path):
        super().__init__("go", "Го", data_dir)

        self._tick_ms = 50

        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("trigger", "go")
            self.set_data("players", {})   # name -> {elo, wins, losses, draws}
            self.set_data("games_played", 0)
            self.set_data("matches", {})   # match_id -> состояние матча

    # ------------------------------------------------------------------
    # Dispatcher hooks (как в chess.py)
    # ------------------------------------------------------------------

    @property
    def trigger(self) -> str:
        return self.get_data("trigger", "go")

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
        if text.lower() == "!help go":
            return True
        return False

    def parse_command(self, text: str) -> tuple[str, list[str]] | None:
        if not self.matches_command(text):
            return None
        text = text.strip()
        trigger = self.trigger
        low = text.lower()
        if low == f"!help {trigger}" or low == "!help go":
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
            "game": "go",   # чат-скрин маршрутизирует окно по этому ключу
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
            # результат партии против локального ИИ (Qt: режим «vs ИИ»)
            return self._cmd_result(sender, args)

        if cmd == "getscores":
            # silent=True — поллинговая команда, не спамит чат
            return {"action": "scores", "scores": self._top_players(50), "silent": True}

        # ─── Онлайн-PVP ───────────────────────────────────────────────
        if cmd == "host":
            return self._cmd_host(sender, args)

        if cmd == "join":
            return self._cmd_join(sender, args)

        if cmd == "move":
            return self._cmd_move(sender, args)

        if cmd == "state":
            return self._cmd_state(sender)

        if cmd == "leave":
            return self._cmd_leave(sender)

        if cmd == "resign":
            return self._cmd_resign(sender)

        if cmd == "matchlist":
            return self._cmd_matchlist()

        if cmd == "meta":
            return {"action": "meta", **self.get_meta(), "silent": True}

        return f"❌ Неизвестная подкоманда. Напиши !{self.trigger} help"

    # ------------------------------------------------------------------
    # Прочее / триггер / статистика
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

    def _cmd_stats(self, sender: str) -> str:
        stats = self._get_player(sender)
        return (f"📊 Статистика {sender}:\n"
                f"ELO: {stats['elo']} | Побед: {stats['wins']} | "
                f"Поражений: {stats['losses']} | Ничьих: {stats['draws']}")

    def _cmd_result(self, sender: str, args: list[str]) -> dict:
        """Результат партии против локального ИИ (из окна игры) → ELO.
        ИИ считается соперником с фикс. рейтингом ~1200, как в шахматах."""
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
            "text": f"⚫ {sender}: Го, {label} против ИИ. Новый ELO: {stats['elo']}",
        }

    def _get_player(self, name: str) -> dict:
        players = self.get_data("players", {})
        return dict(players.get(name, {"elo": _DEFAULT_ELO, "wins": 0, "losses": 0, "draws": 0}))

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
    # Онлайн-PVP: матчи по сети (чёрные = хост, белые = присоединившийся)
    # ------------------------------------------------------------------

    @staticmethod
    def _gen_match_id() -> str:
        """4-значный код (секреты, не random — как в chess.py: код матча
        не должен быть предсказуемым, иначе в матч можно влететь чужим)."""
        import secrets

        return "".join(secrets.choice(string.ascii_uppercase + string.digits)
                       for _ in range(4))

    def _get_matches(self) -> dict:
        return self.get_data("matches", {}) or {}

    def _save_matches(self, matches: dict) -> None:
        self.set_data("matches", matches)

    def _find_match_by_player(self, player: str, include_finished: bool = False):
        matches = self._get_matches()
        for mid, m in matches.items():
            if m.get("status") == "finished" and not include_finished:
                continue
            if player in (m.get("black"), m.get("white")):
                return mid, m
        return None

    @staticmethod
    def _match_to_public(mid: str, m: dict) -> dict:
        """Публичный вид матча для клиентов (Qt-доска перегоняет moves[]
        через GoEngine сама; score — финальный подсчёт при его наличии)."""
        return {
            "match_id": mid,
            "black": m.get("black"),
            "white": m.get("white"),
            "size": m.get("size", 9),
            "status": m.get("status"),
            "moves": list(m.get("moves", [])),
            "result": m.get("result"),
            "score": m.get("score"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
        }

    def _cmd_host(self, sender: str, args: list[str]) -> dict:
        """Создать матч. Хост играет ЧЁРНЫМИ (в Го начинают чёрные).
        Аргумент — размер доски 9/13/19 (по умолчанию 9: быстрые партии).

        v3.1: если у игрока есть ЗАВЕРШЁННЫЙ матч — автоматически удаляем
        его (игрок уже увидел результат, нет смысла требовать ручной leave).
        Это чинит «вечный цикл выигрыша»: раньше после финала игрок не мог
        создать новую игру, потому что старый finished-матч блокировал host.
        """
        size = 9
        if args:
            raw = args[0].strip()
            if raw.isdigit() and int(raw) in (9, 13, 19):
                size = int(raw)

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
                    "text": (f"⚫ У тебя уже есть активный матч {mid} "
                             f"(статус: {m['status']}). "
                             f"Используй !{self.trigger} leave, чтобы выйти."),
                }
        self._cleanup_matches()

        matches = self._get_matches()
        for _ in range(100):
            mid = self._gen_match_id()
            if mid not in matches:
                break
        else:
            return {"action": "error",
                    "text": "❌ Не удалось сгенерировать ID матча. Попробуй ещё раз."}

        now = int(time.time())
        matches[mid] = {
            "black": sender,
            "white": None,
            "size": size,
            "moves": [],
            "status": "waiting",
            "result": None,
            "score": None,
            "created_at": now,
            "updated_at": now,
        }
        self._save_matches(matches)
        # v3: кликабельная ссылка-приглашение (как в chess.py).
        # Другие игроки кликают — открывается окно Го и джойн по коду.
        invite_link = f"[▶ Присоединиться к матчу Го {mid}](friendrelay://go/join/{mid})"
        return {
            "action": "match_created",
            "match": self._match_to_public(mid, matches[mid]),
            "text": (f"⚫ Матч Го создан! ID: {mid} (доска {size}×{size})\n"
                     f"Скинь другу ссылку — он присоединится одним кликом:\n"
                     f"{invite_link}\n"
                     f"Ты играешь чёрными. Ожидание соперника…"),
        }

    def _cmd_join(self, sender: str, args: list[str]) -> dict:
        if not args:
            return {"action": "error",
                    "text": f"❌ Укажи код матча: !{self.trigger} join ABCD"}
        mid = args[0].strip().upper()
        if len(mid) != 4:
            return {"action": "error", "text": "❌ Код матча — 4 символа (например, ABCD)"}

        existing = self._find_match_by_player(sender)
        if existing and existing[0] != mid:
            return {"action": "error",
                    "text": (f"❌ Ты уже в матче {existing[0]}. "
                             f"Сначала !{self.trigger} leave")}

        matches = self._get_matches()
        m = matches.get(mid)
        if not m:
            return {"action": "error", "text": f"❌ Матч {mid} не найден"}
        if m["status"] != "waiting":
            return {"action": "error", "text": f"❌ Матч {mid} уже не принимает игроков"}
        if m["black"] == sender:
            return {"action": "error", "text": "❌ Ты и так в этом матче (чёрные)"}

        m["white"] = sender
        m["status"] = "playing"
        m["updated_at"] = int(time.time())
        self._save_matches(matches)
        return {
            "action": "match_joined",
            "match": self._match_to_public(mid, m),
            "text": (f"⚫ Ты в матче {mid} против {m['black']}!\n"
                     f"Доска {m.get('size', 9)}×{m.get('size', 9)}. "
                     f"Ты — белые (коми {GoEngine(m.get('size', 9)).komi}). "
                     f"Жди первого хода чёрных."),
        }

    def _cmd_move(self, sender: str, args: list[str]) -> dict:
        """Ход игрока: !go move D4  или  !go move pass.

        Порядок строгий: чёрные/белые чередуются; каждое применение хода
        прогоняется через GoEngine — недопустимые (занято/ко/суицид)
        сервер отвергает с объяснением."""
        if not args:
            return {"action": "error",
                    "text": f"❌ Укажи ход: !{self.trigger} move D4 или pass"}

        found = self._find_match_by_player(sender)
        if not found:
            return {"action": "error", "text": "❌ У тебя нет активного матча. Создай: !go host"}
        mid, m = found
        if m["status"] != "playing":
            return {"action": "error", "text": "❌ Матч ещё не начался или уже завершён"}

        # чей ход: чётное число ходов в истории → чёрные
        moves: list[str] = m.get("moves", [])
        black_to_move = len(moves) % 2 == 0
        mover = m["black"] if black_to_move else m["white"]
        if sender != mover:
            waiting = "чёрных" if black_to_move else "белых"
            return {"action": "error", "text": f"❌ Сейчас ход {waiting}"}

        color = BLACK if black_to_move else WHITE
        raw = args[0].strip()

        engine = self._replay(m)
        try:
            if raw.lower() in ("pass", "пас"):
                engine.pass_turn(color)
                moves.append("pass")
                verb = "пас"
            else:
                vertex = parse_vertex(raw, engine.size)
                if vertex is None:
                    return {"action": "error",
                            "text": "❌ Не понял вершину. Формат: буква+цифра, "
                                    "например D4 (буква I пропускается)"}
                engine.play(vertex[0], vertex[1], color)
                moves.append(format_vertex(vertex[0], vertex[1], engine.size))
                verb = f"ход {moves[-1]}"
        except GoError as e:
            return {"action": "error", "text": f"❌ Ход отклонён: {e}"}

        m["moves"] = moves
        m["updated_at"] = int(time.time())

        # Два паса подряд → конец и подсчёт (сервер = арбитр счёта)
        if engine.game_over:
            sc = engine.area_score()
            result = sc["winner"]
            m["status"] = "finished"
            m["result"] = result
            m["score"] = (f"чёрные {sc['black']} : белые {sc['white']} "
                          f"(+коми {sc['komi']:g})")
            self._save_matches(self._get_matches())   # весь словарь матчей
            self._apply_elo_result(m)
            margin = sc["margin"]
            text = (f"🏁 Два паса — партия доиграна! Счёт: {m['score']}. "
                    f"Победа {self._result_label(result)} (+{margin:g})")
            return {"action": "match_finished", "match": self._match_to_public(mid, m),
                    "text": text}

        self._save_matches(self._get_matches())   # весь словарь матчей
        next_side = "белых" if black_to_move else "чёрных"
        caps = (f"взято: чёрными {engine.captured[BLACK]}, "
                f"белыми {engine.captured[WHITE]}")
        # v3: text возвращается запрашивающему (показывается в окне Го
        # как статус хода), но в общий чат НЕ идёт — action=move_accepted
        # автоматически silent на стороне сервера (relay_server.py).
        return {
            "action": "move_accepted",
            "match": self._match_to_public(mid, m),
            "text": f"⚫ {sender}: {verb}. Ход {next_side}. {caps}",
        }

    def _cmd_state(self, sender: str) -> dict:
        """Поллинговая команда: текущий матч игрока (silent)."""
        found = self._find_match_by_player(sender, include_finished=True)
        if not found:
            return {"action": "match_state", "match": None, "silent": True}
        mid, m = found
        return {"action": "match_state", "match": self._match_to_public(mid, m),
                "silent": True}

    def _cmd_leave(self, sender: str) -> dict:
        found = self._find_match_by_player(sender, include_finished=True)
        if not found:
            return {"action": "error", "text": "❌ У тебя нет матча"}
        mid, m = found
        if m.get("status") == "playing":
            # уход из идущей партии = сдача (соперник получает победу)
            return self._finish_by_resign(mid, m, quitter=sender)
        matches = self._get_matches()
        matches.pop(mid, None)
        self._save_matches(matches)
        # v3: silent — leave из ожидавшего матча касается только ушедшего.
        return {"action": "match_left", "text": "⚫ Матч покинут.",
                "silent": True}

    def _cmd_resign(self, sender: str) -> dict:
        found = self._find_match_by_player(sender)
        if not found:
            return {"action": "error", "text": "❌ У тебя нет активного матча"}
        mid, m = found
        if m["status"] != "playing":
            return {"action": "error", "text": "❌ Матч не идёт"}
        return self._finish_by_resign(mid, m, quitter=sender)

    def _finish_by_resign(self, mid: str, m: dict, quitter: str) -> dict:
        """Сдача/уход: победа сопернику + ELO (правила анти-«выйти и обнулить»)."""
        m["status"] = "finished"
        if quitter == m["black"]:
            m["result"] = "white"
        else:
            m["result"] = "black"
        m["score"] = "сдача"
        m["updated_at"] = int(time.time())
        self._save_matches(self._get_matches())   # весь словарь матчей
        self._apply_elo_result(m)
        return {
            "action": "match_finished",
            "match": self._match_to_public(mid, m),
            "text": (f"🏳 {quitter} сдался в матче {mid}. "
                     f"Победа {self._result_label(m['result'])}!"),
        }

    def _cmd_matchlist(self) -> dict:
        matches = self._get_matches()
        active = [
            f"{mid}: {m.get('black', '?')} vs {m.get('white', '…')} "
            f"({m.get('status')}, {m.get('size', 9)}×{m.get('size', 9)})"
            for mid, m in matches.items() if m.get("status") != "finished"
        ]
        text = "\n".join(active) if active else "Активных матчей нет."
        return {"action": "matchlist", "text": text, "silent": True}

    # ------------------------------------------------------------------
    # ELO
    # ------------------------------------------------------------------

    def _apply_elo_result(self, m: dict) -> None:
        """Обновляет ELO обеих сторон по завершении матча (K=24, как в
        шахматах; ничья — обмен по ожидаемости)."""
        result = m.get("result")
        players = self.get_data("players", {})

        def stats(name: str) -> dict:
            return players.get(name, {"elo": _DEFAULT_ELO, "wins": 0,
                                      "losses": 0, "draws": 0})

        black, white = stats(m["black"]), stats(m["white"])
        eb = 1 / (1 + 10 ** ((white["elo"] - black["elo"]) / 400))
        sb, sw = (0.5, 0.5) if result == "draw" else (
            (1.0, 0.0) if result == "black" else (0.0, 1.0))
        black["elo"] = round(black["elo"] + _K_FACTOR * (sb - eb))
        white["elo"] = round(white["elo"] + _K_FACTOR * (sw - (1 - eb)))
        for st, won, lost in ((black, sb, sw), (white, sw, sb)):
            if won == 1.0:
                st["wins"] = st.get("wins", 0) + 1
            elif lost == 1.0:
                st["losses"] = st.get("losses", 0) + 1
            else:
                st["draws"] = st.get("draws", 0) + 1
        players[m["black"]], players[m["white"]] = black, white
        self.set_data("players", players)
        self.set_data("games_played", self.get_data("games_played", 0) + 1)

    @staticmethod
    def _result_label(result: str | None) -> str:
        return {"black": "чёрных ⚫", "white": "белых ⚪"}.get(result or "", "—")

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def _replay(self, m: dict) -> GoEngine:
        """Восстановить позицию из истории ходов. Все ходы в истории уже
        были валидны при применении, но защита от битых данных нужна."""
        engine = GoEngine(m.get("size", 9))
        for i, v in enumerate(m.get("moves", [])):
            color = BLACK if i % 2 == 0 else WHITE
            try:
                if v == "pass":
                    engine.pass_turn(color)
                else:
                    vertex = parse_vertex(v, engine.size)
                    if vertex:
                        engine.play(vertex[0], vertex[1], color)
            except GoError:
                break   # повреждённая история — играем по валидному префиксу
        return engine

    def _cleanup_matches(self) -> None:
        """Протухшие матчи: ожидавшие > 30 мин удаляем, заброшенные в игре
        > 2 ч завершаем ничьей без изменения ELO."""
        now = int(time.time())
        matches = self._get_matches()
        dirty = False
        for mid in [k for k, v in matches.items() if v.get("status") == "waiting"]:
            if now - matches[mid].get("created_at", now) > _MATCH_WAIT_TIMEOUT:
                del matches[mid]
                dirty = True
        for mid in [k for k, v in matches.items() if v.get("status") == "playing"]:
            if now - matches[mid].get("updated_at", now) > _MATCH_ABANDON_TIMEOUT:
                matches[mid]["status"] = "finished"
                matches[mid]["result"] = "draw"
                matches[mid]["score"] = "матч заброшен"
                dirty = True
        if dirty:
            self._save_matches(matches)

    def get_help(self) -> str:
        t = self.trigger
        return (
            f"⚫ {self.name} — древняя игра в окружение (движок: взятия, ко, "
            f"подсчёт по площади, коми 6.5)\n"
            f"!{t} — открыть окно игры (ИИ / hot-seat / сеть)\n"
            f"!{t} host [9|13|19] — создать онлайн-матч (ты — чёрные)\n"
            f"!{t} join КОД — присоединиться к матчу (ты — белые)\n"
            f"!{t} move D4 — ход (буква I пропускается), "
            f"!{t} move pass — пас (два паса = подсчёт)\n"
            f"!{t} state / !{t} matchlist — состояние матчей\n"
            f"!{t} resign — сдаться, !{t} leave — выйти\n"
            f"!{t} stats / !{t} top — рейтинг ELO\n"
            f"!{t} trigger X — сменить триггер, !{t} help — эта справка"
        )
