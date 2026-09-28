"""ChessMatch — ядро шахматного диалога БЕЗ Qt (Фаза 2 отделения UI от логики).

Переехало из qt_app/widgets/chess_game_dialog.py дословно, поведение 1:1:

  - политика сохранения результата партии (режим ИИ): идемпотентность
    (result_saved — результат уходит на сервер ровно один раз), маппинг
    исхода доски (win_white/win_black/draw) в win/loss/draw относительно
    стороны игрока;
  - страховочный опрос game over (в диалоге — QTimer каждые 400 мс):
    если колбэк доски не сработал, исход определяется по состоянию
    движка (мат/пат);
  - сетевой матч (!chess host/join/move/state/resign/leave): отправка
    команд боту, разбор bot_action (match_created/match_joined/
    match_state/match_finished/move_accepted/match_left/error),
    политика опроса состояния (800 мс, только в NET-режиме и только
    пока партия не завершена);
  - таблица рейтинга ELO (getscores): форматирование топа, показ
    «Твой ELO · место #N» после сохранения результата;
  - правило выхода при закрытии окна: активный сетевой матч —
    отправляем leave (соперник получит победу).

Ядро НЕ знает ни про QWidget, ни про сигналы: сетевые вызовы уходят
через ``runner`` (сигнатура AsyncBridge.run — в тестах синхронная
заглушка), результаты доставляются обычными колбэками (on_status,
on_match_ready и т.д.), на которые подписывается диалог.

Доска (ChessBoardWidget) остаётся владельцем состояния партии (движок,
цвета, применённые ходы) — ядро получает нужные ей факты параметрами
(mode, game_over, net_color, ...) и эмитит вердикты, а не трогает доску.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib import client

# runner(fn, on_success, on_error) — совместим с AsyncBridge.run
Runner = Callable[..., None]

# Режимы игры — те же строки, что ChessBoardWidget.MODE_* (ядро не может
# импортировать qt_app, поэтому дублируем значения, а не класс).
MODE_AI = "ai"    # игрок против ИИ — результат идёт в ELO
MODE_PVP = "pvp"  # hot-seat за одним компьютером — ELO не сохраняется
MODE_NET = "net"  # онлайн-матч через relay — ELO обновляет сервер

# Период опроса сетевого матча (было в ChessGameDialog), мс
NET_POLL_INTERVAL_MS = 800
# Период страховочного опроса game over (было в ChessGameDialog), мс
GAME_OVER_WATCH_MS = 400


# ── Чистые функции (без состояния) ──────────────────────────────────────────


def outcome_for_player(outcome: str, player_side: bool) -> str:
    """Исход доски (win_white/win_black) → win/loss для игрока.

    player_side=True — игрок белыми. draw сюда не попадает (обрабатывается
    отдельно, как и в оригинале)."""
    player_won = (outcome == "win_white" and player_side) or (
        outcome == "win_black" and not player_side
    )
    return "win" if player_won else "loss"


def format_leaderboard(scores: list[dict], limit: int = 10) -> list[str]:
    """Строки таблицы рейтинга (дословно цикл из _on_scores_loaded)."""
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
    (соперник получит победу). Дословно условие из closeEvent."""
    return mode == MODE_NET and bool(net_match_id) and not game_over


@dataclass
class GameOverVerdict:
    """Что решено сделать с завершившейся партией (для вьюхи и тестов).

    send    — что отправлено на сервер ("win"/"loss"/"draw") или None;
    refresh — запрошено ли обновление таблицы рейтинга;
    reason  — почему так: already_saved/pvp/net/ai/nothing_to_do.
    """

    send: str | None = None
    refresh: bool = False
    reason: str = "nothing_to_do"


# ── ChessMatch: политика результата + сетевой матч + рейтинг ────────────────


class ChessMatch:
    """Ядро шахматного диалога. Один экземпляр на ChessGameDialog.

    Колбэки вызываются в потоке runner'а — в приложении AsyncBridge
    доставляет их в GUI-поток через сигналы, поэтому ядро про потоки
    не думает. Исключение — leave_on_close(): блокирующий вызов, как и
    в прежнем closeEvent.
    """

    def __init__(
        self,
        base_url: str = "",
        player_name: str = "",
        access_key: str = "",
        bot_id: str = "chess",
        runner: Runner | None = None,
    ) -> None:
        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key
        self._bot_id = bot_id
        self._runner: Runner = runner or (lambda fn, on_success=None, on_error=None: None)

        # Идемпотентность сохранения результата (было self._result_saved
        # в диалоге): guards повторный game over при одном game over.
        self.result_saved = False

        # Колбэки (подписывается вьюха)
        self.on_status: Callable[[str], None] | None = None          # текст сетевого статуса
        self.on_my_stats: Callable[[str], None] | None = None        # «Твой ELO: … место: #N»
        self.on_leaderboard: Callable[[list[str]], None] | None = None  # строки топа ([] = пусто)
        self.on_leaderboard_error: Callable[[], None] | None = None
        self.on_no_connection: Callable[[], None] | None = None      # заглушка рейтинга без сервера
        self.on_match_ready: Callable[[bool, str, str], None] | None = None  # (мой_цвет, id, соперник)
        self.on_match_state: Callable[[dict], None] | None = None    # матч с сервера → доске
        self.on_match_reset: Callable[[], None] | None = None        # сброс сетевого состояния доски
        self.on_move_rejected: Callable[[str], None] | None = None   # ход отклонён сервером
        self.on_net_error: Callable[[str], None] | None = None

    # -- Подключение -------------------------------------------------------

    def configure(self, base_url: str, player_name: str, access_key: str) -> None:
        self._base_url = base_url
        self._player_name = player_name
        self._access_key = access_key

    def reset_result_flag(self) -> None:
        """Новая партия / смена режима / новый матч — снова можно сохранить."""
        self.result_saved = False

    def _emit(self, cb_name: str, *args) -> None:
        cb = getattr(self, cb_name, None)
        if cb is not None:
            cb(*args)

    # ------------------------------------------------------------------
    # Game over → сохраняем результат (только в режиме ИИ)
    # ------------------------------------------------------------------

    def on_board_game_over(
        self, outcome: str | None, winner_color: str | None, *, mode: str, player_side: bool
    ) -> GameOverVerdict:
        """Колбэк из доски (дословно ChessGameDialog._on_board_game_over).

        В PVP/NET ничего не сохраняем на сервере — в NET ELO уже обновил
        сам сервер при завершении матча. В режиме ИИ приходят исходы
        win_white / win_black / draw. result_saved ставится ДО отправки,
        иначе страховочный таймер увидит game_over и сохранит ПОВТОРНО.
        """
        if self.result_saved:
            # Защита от повторного game over (доска страхуется сама через
            # идемпотентный _check_state, но пусть будет и здесь).
            return GameOverVerdict(reason="already_saved")
        if mode == MODE_PVP:
            # Дружеская партия — ELO не трогаем.
            self.result_saved = True
            return GameOverVerdict(reason="pvp")
        if mode == MODE_NET:
            # Сервер уже обновил ELO обоих игроков при завершении матча.
            self.result_saved = True
            self.refresh_leaderboard()
            return GameOverVerdict(refresh=True, reason="net")
        self.result_saved = True
        if outcome == "draw":
            self._save_result("draw")
            return GameOverVerdict(send="draw", reason="ai")
        send = outcome_for_player(outcome or "", player_side)
        self._save_result(send)
        return GameOverVerdict(send=send, reason="ai")

    def check_game_over_fallback(
        self,
        *,
        game_over: bool,
        mode: str,
        player_side: bool,
        is_checkmate: bool,
        white_to_move: bool,
    ) -> GameOverVerdict:
        """Страховка: если колбэк не сработал (например, доска переключалась
        в режим после game over), всё равно ловим завершение партии.
        Дословно ChessGameDialog._check_game_over; состояние движка
        (мат? чей ход?) передаёт вьюха."""
        if not game_over or self.result_saved:
            return GameOverVerdict(reason="nothing_to_do")
        if mode == MODE_PVP:
            self.result_saved = True
            return GameOverVerdict(reason="pvp")
        if mode == MODE_NET:
            self.result_saved = True
            self.refresh_leaderboard()
            return GameOverVerdict(refresh=True, reason="net")
        self.result_saved = True
        send = ("win" if white_to_move != player_side else "loss") if is_checkmate else "draw"
        self._save_result(send)
        return GameOverVerdict(send=send, reason="ai")

    def _save_result(self, outcome: str) -> None:
        if not self._base_url or not self._player_name:
            return
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

    def _on_result_saved(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") == "score_saved":
            rank = action.get("rank")
            best = action.get("best")
            if rank is not None:
                self._emit("on_my_stats", f"Твой ELO: {best}   ·   место: #{rank}")
        self.refresh_leaderboard()

    # ------------------------------------------------------------------
    # Онлайн-PVP: сетевые операции
    # ------------------------------------------------------------------

    def _net_command(self, command: str, args: list[str] | None = None,
                     on_success=None, on_error=None) -> None:
        """Отправляет команду боту на сервере (через /bot_command).
        Использует runner, чтобы не блокировать UI (дословно
        ChessGameDialog._net_command)."""
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
        self._emit("on_net_error", err)

    def host(self) -> None:
        """«Создать матч» — вызывает !chess host (дословно _on_net_host)."""
        self._emit("on_status", "Создание матча…")
        self._net_command("host", [], on_success=self._on_hosted)

    def _on_hosted(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") != "match_created":
            self._emit("on_status", f"⚠ Не удалось создать матч: {action.get('text', '?')}")
            return
        match = action.get("match") or {}
        mid = match.get("match_id", "?")
        # Мы — белые (хост). Сначала доска, потом флаг, потом статус.
        self._emit("on_match_ready", True, mid, "Ожидание…")
        self.result_saved = False
        self._emit(
            "on_status",
            f"🌐 Матч создан!\n"
            f"Код: {mid}\n"
            f"Передай код другу — он нажмёт «Присоединиться».\n"
            f"Ты играешь белыми. Ждём соперника…",
        )

    def join(self, code: str) -> bool:
        """«Присоединиться» — !chess join с кодом матча. Код нормализуется
        (trim + верхний регистр); пустой — ничего не отправляем.
        Вопрос-диалог ввода кода остаётся во вьюхе."""
        mid = (code or "").strip().upper()
        if not mid:
            return False
        self._emit("on_status", f"Присоединение к матчу {mid}…")
        self._net_command("join", [mid], on_success=self._on_joined)
        return True

    def _on_joined(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") != "match_joined":
            self._emit("on_status", f"⚠ Не удалось присоединиться: {action.get('text', '?')}")
            return
        match = action.get("match") or {}
        mid = match.get("match_id", "?")
        white_name = match.get("white", "?")
        # Мы — чёрные (присоединившийся).
        self.result_saved = False
        self._emit("on_match_ready", False, mid, white_name)
        self._emit(
            "on_status",
            f"🌐 Ты присоединился к матчу {mid}!\n"
            f"Белые: {white_name}\n"
            f"Ты играешь чёрными. Жди хода белых.",
        )

    def apply_initial_match(self, match: dict) -> bool:
        """v3: применить уже известное состояние матча без сетевого запроса.

        Используется, когда матч создан/присоединён через !chess host/join
        в чате (ответ /dispatch несёт match прямо в bot_action). Диалог
        открывается с готовым матчем — не нужно повторно дёргать /bot_command.

        Возвращает True, если match применён (диалог можно показывать сразу).
        """
        if not isinstance(match, dict) or not match:
            return False
        white_name = match.get("white", "?")
        my_name = self._player_name
        # Если мы white — значит, мы хост (создали матч). Иначе — joiner.
        is_white = (white_name == my_name)
        mid = match.get("match_id", "?")
        self.result_saved = False
        self._emit("on_match_ready", is_white, mid, white_name)
        status = match.get("status", "waiting")
        if status == "playing":
            if is_white:
                self._emit("on_status",
                    f"🌐 Матч {mid} создан. Ты играешь белыми.\n"
                    f"Соперник: {match.get('black', '?')}")
            else:
                self._emit("on_status",
                    f"🌐 Ты в матче {mid}!\n"
                    f"Белые: {white_name}\n"
                    f"Ты играешь чёрными. Жди хода белых.")
        elif status == "waiting":
            self._emit("on_status",
                f"🌐 Матч {mid} создан. Код для друга: {mid}\n"
                f"Ты играешь белыми. Ждём соперника…")
        # Сразу отрисуем доску из истории ходов — без 800мс поллинга.
        moves = match.get("moves") or []
        if moves:
            self._emit("on_match_state", match)
        return True

    def resign(self) -> None:
        """«Сдаться» — вызывает !chess resign (подтверждение QMessageBox —
        во вьюхе; проверка наличия матча — тоже, она читает доску)."""
        self._net_command("resign", [], on_success=self._on_finished)

    def leave(self) -> None:
        """«Выйти из матча» — вызывает !chess leave."""
        self._net_command("leave", [], on_success=self._on_left)

    def _on_left(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        self.result_saved = False
        self._emit("on_match_reset")
        self._emit("on_status", action.get("text", "Матч покинут."))

    def _on_finished(self, response: dict) -> None:
        """Общий обработчик для resign/финального хода — доске применяется
        состояние матча, статус обновляется (дословно _on_net_finished)."""
        action = response.get("bot_action") or {}
        match = action.get("match") or {}
        if match:
            self._emit("on_match_state", match)
        self._emit("on_status", action.get("text", "Матч завершён."))
        self.refresh_leaderboard()

    def send_move(self, uci: str) -> None:
        """Колбэк от доски: игрок сделал ход, отправляем его на сервер.
        Сервер подтвердит или отклонит; фактическое обновление доски при
        расхождении делает poll_state через on_match_state."""
        self._net_command("move", [uci], on_success=self._on_move_response)

    def _on_move_response(self, response: dict) -> None:
        action = response.get("bot_action") or {}
        if action.get("action") == "error":
            # Сервер отклонил ход — вьюха синхронизирует доску немедленным
            # опросом (дословно: setText + _net_poll()).
            self._emit("on_move_rejected", action.get("text", "?"))
            return
        if action.get("action") == "match_finished":
            # Ход привёл к мату/пату.
            match = action.get("match") or {}
            if match:
                self._emit("on_match_state", match)
            self._emit("on_status", action.get("text", "Матч завершён."))
            self.refresh_leaderboard()
            return
        # Обычный ход принят — обновим статус.
        self._emit("on_status", "✓ Ход отправлен. Жди хода соперника…")

    def poll_state(self, *, is_net_mode: bool, game_over: bool, net_color: bool) -> None:
        """Периодический опрос !chess state — подтягивает ходы соперника
        (дословно _net_poll). Политика: только при подключении, только
        в NET-режиме и пока партия не завершена; ошибки сети тихие."""
        if not self._base_url or not self._player_name:
            return
        if not is_net_mode:
            return
        # Если уже game_over — опрос не нужен (состояние финальное).
        if game_over:
            return

        def _on_ok(response: dict) -> None:
            action = response.get("bot_action") or {}
            match = action.get("match")
            if match is None:
                return  # нет активного матча
            # Применяем состояние к доске.
            self._emit("on_match_state", match)
            # Если матч завершён — обновим статус и таблицу рейтинга.
            if match.get("status") == "finished":
                self.result_saved = True  # ELO уже обновил сервер
                self.refresh_leaderboard()
                if match.get("result"):
                    res = match["result"]
                    my_won = (
                        (res == "white" and net_color) or
                        (res == "black" and not net_color)
                    )
                    if res == "draw":
                        self._emit("on_status", "½ Ничья! Матч завершён.")
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
        (соперник получит победу). Блокирующий вызов — 1:1 с прежним
        closeEvent: окно всё равно умирает, фон не нужен."""
        try:
            client.send_bot_command_full(
                self._base_url, self._bot_id, command="leave", args=[],
                name=self._player_name, access_key=self._access_key,
            )
        except Exception:
            pass
