"""GoMatch — ядро Го-диалога БЕЗ Qt (аналог ChessMatch для шахмат).

Что делает ядро (чистый Python, тестируется без PySide6):
  - сетевой матч (!go host/join/move/state/resign/leave): отправка команд
    боту через client.send_bot_command_full, разбор bot_action
    (match_created/match_joined/match_state/match_finished/
    move_accepted/match_left/error), политика опроса состояния
    (800 мс, только в NET-режиме и пока партия не завершена);
  - таблица рейтинга ELO (getscores): форматирование топа;
  - сохранение результата против локального ИИ (result win/loss/draw) —
    идемпотентно (result_saved: результат уходит на сервер ровно раз);
  - правило выхода при закрытии окна: активный сетевой матч — отправляем
    leave (соперник получит победу, правила честной игры).

Ядро НЕ знает ни про QWidget, ни про сигналы: сетевые вызовы уходят через
``runner`` (сигнатура AsyncBridge.run — в тестах синхронная заглушка),
результаты доставляются обычными колбэками, на которые подписывается
диалог. Про Го здесь почти ничего нет: правила считает GoEngine
(lib/games/go_engine.py), ядро — только сеть и политика результата.
"""

from __future__ import annotations

from collections.abc import Callable

from lib import client

# runner(fn, on_success, on_error) — совместим с AsyncBridge.run
Runner = Callable[..., None]

# Режимы игры — те же строки, что GoGameDialog.MODE_* (ядро не может
# импортировать qt_app, поэтому значения дублируем, а не импортируем).
MODE_AI = "ai"    # игрок против эвристического ИИ — результат идёт в ELO
MODE_PVP = "pvp"  # hot-seat за одним компьютером — ELO не сохраняется
MODE_NET = "net"  # онлайн-матч через relay — ELO обновляет сервер

# Периоды опроса (как в шахматном ядре), мс
NET_POLL_INTERVAL_MS = 800


def format_leaderboard(scores: list[dict], limit: int = 10) -> list[str]:
    """Строки таблицы рейтинга (формат как в шахматах — единая сетка)."""
    lines = []
    for i, s in enumerate(scores[:limit], start=1):
        marker = "👑" if i == 1 else f"{i}."
        lines.append(
            f"{marker} {s.get('name', '?')} — {s.get('score', 1200)} "
            f"({s.get('wins', 0)}W/{s.get('losses', 0)}L/{s.get('draws', 0)}D)"
        )
    return lines


def should_leave_on_close(mode: str, net_match_id: str | None, game_over: bool) -> bool:
    """Закрываем окно в активном сетевом матче → нужен корректный leave
    (соперник получит победу). Условие то же, что в шахматном ядре."""
    return mode == MODE_NET and bool(net_match_id) and not game_over


def result_for_player(score: dict, player_is_black: bool) -> str:
    """Счёт движка (area_score) → win/loss/draw для стороны игрока.

    score['winner'] — 'black'|'white'|'draw'; игрок в режиме ИИ всегда
    чёрные, в сети цвет приходит из матча."""
    w = score.get("winner")
    if w == "draw":
        return "draw"
    won = (w == "black") == player_is_black
    return "win" if won else "loss"


class GoMatch:
    """Ядро Го-диалога: сеть + политика результата. Один экземпляр
    на GoGameDialog. Колбэки вызываются в потоке runner'а (AsyncBridge
    доставляет их в GUI-поток), подписывается диалог."""

    def __init__(
        self,
        base_url: str = "",
        player_name: str = "",
        access_key: str = "",
        bot_id: str = "go",
        runner: Runner | None = None,
    ) -> None:
        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._runner: Runner = runner or (lambda fn, on_success=None, on_error=None: None)

        # Идемпотентность сохранения результата (режим ИИ)
        self.result_saved = False

        # Колбэки (подписывается вьюха)
        self.on_status: Callable[[str], None] | None = None
        self.on_my_stats: Callable[[str], None] | None = None
        self.on_leaderboard: Callable[[list[str]], None] | None = None
        self.on_leaderboard_error: Callable[[], None] | None = None
        self.on_no_connection: Callable[[], None] | None = None
        # (мы_чёрные, match_id, имя соперника)
        self.on_match_ready: Callable[[bool, str, str], None] | None = None
        self.on_match_state: Callable[[dict], None] | None = None   # матч с сервера
        self.on_match_reset: Callable[[], None] | None = None
        self.on_move_rejected: Callable[[str], None] | None = None
        self.on_net_error: Callable[[str], None] | None = None

    # -- Подключение -------------------------------------------------------

    def configure(self, base_url: str, player_name: str, access_key: str) -> None:
        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key

    def reset_result_flag(self) -> None:
        """Новая партия / смена режима — снова можно сохранить."""
        self.result_saved = False

    def _emit(self, cb_name: str, *args) -> None:
        cb = getattr(self, cb_name, None)
        if cb is not None:
            cb(*args)

    # ------------------------------------------------------------------
    # Результат против ИИ
    # ------------------------------------------------------------------

    def save_ai_result(self, outcome: str) -> bool:
        """Отправить 'result win|loss|draw' боту (режим ИИ). False — уже
        сохранено (guards повторный вызов от двойного game over)."""
        if self.result_saved:
            return False
        self.result_saved = True
        if not self._base_url or not self._player_name:
            return True    # офлайн-партия: сохранять некуда, но флаг ставим
        self._runner(
            lambda: client.send_bot_command_full(
                self._base_url,
                self._bot_id,
                command="result",
                args=[outcome],
                name=self._player_name,
                access_key=self._access_key,
            ),
            on_success=self._on_result_saved,
            on_error=lambda e: None,
        )
        return True

    def _on_result_saved(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") == "score_saved":
            rank, best = action.get("rank"), action.get("best")
            if rank is not None:
                self._emit("on_my_stats", f"Твой ELO: {best}   ·   место: #{rank}")
        self.refresh_leaderboard()

    # ------------------------------------------------------------------
    # Онлайн-PVP: сетевые операции
    # ------------------------------------------------------------------

    def _net_command(self, command: str, args: list[str] | None = None,
                     on_success=None, on_error=None) -> None:
        """Отправляет команду боту (через /bot_command), не блокируя UI."""
        if not self._base_url or not self._player_name:
            if on_error:
                on_error("нет подключения к серверу")
            return
        self._runner(
            lambda: client.send_bot_command_full(
                self._base_url, self._bot_id,
                command=command, args=args or [],
                name=self._player_name, access_key=self._access_key,
            ),
            on_success=on_success,
            on_error=on_error or self._on_net_error,
        )

    def _on_net_error(self, err: str) -> None:
        self._emit("on_net_error", str(err))

    def host(self, size: int = 9) -> None:
        """«Создать матч» — !go host [size]; создавший играет чёрными."""
        self._emit("on_status", "Создание матча…")
        self._net_command("host", [str(int(size))], on_success=self._on_hosted)

    def _on_hosted(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") != "match_created":
            self._emit("on_status",
                       f"⚠ Не удалось создать матч: {action.get('text', '?')}")
            return
        match = action.get("match") or {}
        mid = match.get("match_id", "?")
        # Мы — чёрные (хост). Сначала доска, потом статус.
        self._emit("on_match_ready", True, mid, "Ожидание…")
        # (v2.0.6) и сразу применяем состояние: доска получит размер,
        # выбранный хостом (9/13/19), ещё до первого опроса
        self._emit("on_match_state", match)
        self.result_saved = False
        self._emit(
            "on_status",
            f"⚫ Матч создан!\n"
            f"Код: {mid}\n"
            f"Передай код другу — он нажмёт «Присоединиться».\n"
            f"Ты играешь ЧЁРНЫМИ. Ждём соперника…",
        )

    def join(self, code: str) -> bool:
        """«Присоединиться» — !go join с кодом. Код нормализуется; пустой —
        ничего не отправляем. Диалог ввода кода остаётся во вьюхе."""
        mid = (code or "").strip().upper()
        if not mid:
            return False
        self._emit("on_status", f"Присоединение к матчу {mid}…")
        self._net_command("join", [mid], on_success=self._on_joined)
        return True

    def _on_joined(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") != "match_joined":
            self._emit("on_status",
                       f"⚠ Не удалось присоединиться: {action.get('text', '?')}")
            return
        match = action.get("match") or {}
        mid = match.get("match_id", "?")
        black_name = match.get("black", "?")
        # Мы — белые (присоединившийся).
        self.result_saved = False
        self._emit("on_match_ready", False, mid, black_name)
        self._emit("on_match_state", match)   # размер/позиция сразу
        self._emit(
            "on_status",
            f"⚫ Ты в матче {mid}!\n"
            f"Чёрные: {black_name}\n"
            f"Ты играешь БЕЛЫМИ (+коми). Жди первого хода.",
        )

    def apply_initial_match(self, match: dict) -> bool:
        """v3: применить уже известное состояние матча без сетевого запроса.

        Используется, когда матч создан/присоединён через !go host/join
        в чате (ответ /dispatch несёт match прямо в bot_action). Диалог
        открывается с готовым матчем — не нужно повторно дёргать /bot_command.
        """
        if not isinstance(match, dict) or not match:
            return False
        black_name = match.get("black", "?")
        my_name = self._player_name
        # В Го хост = чёрные. Если мы black — мы хост, иначе — joiner.
        is_black = (black_name == my_name)
        mid = match.get("match_id", "?")
        self.result_saved = False
        self._emit("on_match_ready", is_black, mid, black_name)
        # Сразу применяем состояние — доска получит размер (9/13/19)
        # и историю ходов без 800мс поллинга.
        self._emit("on_match_state", match)
        status = match.get("status", "waiting")
        if status == "playing":
            if is_black:
                self._emit("on_status",
                    f"⚫ Матч Го {mid} создан. Ты играешь ЧЁРНЫМИ.\n"
                    f"Соперник: {match.get('white', '?')}")
            else:
                self._emit("on_status",
                    f"⚫ Ты в матче Го {mid}!\n"
                    f"Чёрные: {black_name}\n"
                    f"Ты играешь БЕЛЫМИ (+коми). Жди первого хода.")
        elif status == "waiting":
            self._emit("on_status",
                f"⚫ Матч Го {mid} создан. Код для друга: {mid}\n"
                f"Ты играешь ЧЁРНЫМИ. Ждём соперника…")
        return True

    def resign(self) -> None:
        self._net_command("resign", [], on_success=self._on_finished)

    def leave(self) -> None:
        self._net_command("leave", [], on_success=self._on_left)

    def _on_left(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        self.result_saved = False
        self._emit("on_match_reset")
        self._emit("on_status", action.get("text", "Матч покинут."))

    def _on_finished(self, response: dict) -> None:
        """Общий обработчик resign/финала: состояние → доске, статус."""
        action = response.get("bot_action") or {}
        match = action.get("match") or {}
        if match:
            self._emit("on_match_state", match)
        self._emit("on_status", action.get("text", "Матч завершён."))
        self.refresh_leaderboard()

    def send_move(self, vertex: str) -> None:
        """Ход игрока ("D4" или "pass") — сервер подтвердит или отклонит;
        фактическую синхронизацию делает poll_state через on_match_state."""
        self._net_command("move", [vertex], on_success=self._on_move_response)

    def _on_move_response(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") == "error":
            self._emit("on_move_rejected", action.get("text", "?"))
            return
        if action.get("action") == "match_finished":
            match = action.get("match") or {}
            if match:
                self._emit("on_match_state", match)
            self._emit("on_status", action.get("text", "Матч завершён."))
            self.refresh_leaderboard()
            return
        self._emit("on_status", "✓ Ход принят. Жди хода соперника…")

    def poll_state(self, *, is_net_mode: bool, game_over: bool,
                   net_color_black: bool) -> None:
        """Периодический опрос !go state — подтягивает ходы соперника.
        Политика: только при подключении, в NET-режиме, пока партия жива;
        ошибки сети тихие (поллинг всё равно повторится)."""
        if not self._base_url or not self._player_name:
            return
        if not is_net_mode or game_over:
            return

        def _on_ok(response: dict) -> None:
            action = response.get("bot_action") or {}
            match = action.get("match")
            if not match:
                return  # нет активного матча — молча (не спамим)
            self._emit("on_match_state", match)
            if match.get("status") == "finished":
                self.result_saved = True  # ELO уже обновил сервер
                self.refresh_leaderboard()
                res = match.get("result")
                if res:
                    my_won = ((res == "black") == net_color_black)
                    if res == "draw":
                        self._emit("on_status", "🤝 Ничья (дзиго)! Матч завершён.")
                    elif my_won:
                        self._emit("on_status", "🏆 Ты победил!")
                    else:
                        self._emit("on_status", "🏳 Поражение.")

        self._net_command("state", [], on_success=_on_ok, on_error=lambda e: None)

    # ------------------------------------------------------------------
    # Таблица рейтинга
    # ------------------------------------------------------------------

    def refresh_leaderboard(self) -> None:
        if not self._base_url or not self._player_name:
            self._emit("on_no_connection")
            return
        self._runner(
            lambda: client.send_bot_command_full(
                self._base_url,
                self._bot_id,
                command="getscores",
                args=[],
                name=self._player_name,
                access_key=self._access_key,
            ),
            on_success=self._on_scores_loaded,
            on_error=self._on_scores_error,
        )

    def _on_scores_loaded(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        scores = action.get("scores") if isinstance(action, dict) else None
        if not scores:
            self._emit("on_leaderboard", [])
            return
        self._emit("on_leaderboard", format_leaderboard(scores))

    def _on_scores_error(self, _err: str) -> None:
        self._emit("on_leaderboard_error")

    # ------------------------------------------------------------------
    # Закрытие окна
    # ------------------------------------------------------------------

    def leave_on_close(self) -> None:
        """Корректный выход из активного сетевого матча при закрытии окна
        (соперник получит победу). Блокирующий вызов — окно всё равно
        умирает, фон не нужен (как в шахматном ядре)."""
        try:
            client.send_bot_command_full(
                self._base_url, self._bot_id, command="leave", args=[],
                name=self._player_name, access_key=self._access_key,
            )
        except Exception:
            pass
