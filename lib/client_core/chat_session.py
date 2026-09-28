"""ChatSession — ядро экрана чата БЕЗ Qt (Фаза 1 выделения UI из логики).

Переехало из qt_app/screens/chat_screen.py дословно, поведение 1:1:

  - политика live-поллинга: тик таймера, защита от наложения запросов
    (in-flight + kick_pending), мгновенный kick после действий;
  - курсор last_seq (откуда продолжать дельту /events?since=);
  - состояние каналов: какой открыт, хвосты сообщений, дифф хвоста
    (новые/изменившиеся/удалённые) вместо полной пересборки ленты;
  - состояние «печатает»: чей таймштамп, протухание 5с, троттлинг
    исходящего send_typing 2с;
  - расшифровка текста сообщения (плейсхолдер при неудаче);
  - разбор ответа /dispatch (бот сматчился? какую игру открыть? нужен ли
    дожим ленты?) — экрану остаётся только выполнить вердикт.

Ядро НЕ делает сетевых вызовов само по себе и не знает про потоки:
асинхронность внедряется через ``runner`` — функцию с сигнатурой
``AsyncBridge.run(fn, on_success, on_error)``. В приложении это мост в
GUI-поток, в тестах — синхронная фейковая заглушка. Экран подписывается
на колбэки (``on_poll_result`` и т.д.) и рисует.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lib import client
from lib.constants import CHAT_TAIL_ON_CONNECT

# runner(fn, on_success, on_error) — совместим с AsyncBridge.run
Runner = Callable[..., None]

SUCCESS_CB = Callable[[object], None]
ERROR_CB = Callable[[Exception], None]

# Протухание индикатора «печатает», сек (было в ChatScreen)
TYPING_TTL = 5.0
# Минимальная пауза между исходящими send_typing, сек (было 2.0 в ChatScreen)
TYPING_SEND_EVERY = 2.0


# ── Чистые функции (без состояния) ──────────────────────────────────────────


def merge_delta_into(data: dict, delta_ev: dict) -> dict:
    """Клиентское зеркало RelayServer._apply_delta - применяет одно
    дельта-событие (reaction/pin/edit/link_preview), полученное по
    инкрементальному поллингу, к уже отрисованным данным сообщения на месте,
    без похода на сервер за полным снапшотом."""
    kind = delta_ev.get("kind")
    if kind == "edit":
        new_text = delta_ev.get("new_text")
        if new_text is not None:
            data["text"] = new_text
            data["edited"] = True
            data["edited_ts"] = delta_ev.get("ts")
    elif kind == "reaction":
        sender = delta_ev.get("from")
        emoji = delta_ev.get("emoji")
        if sender and emoji:
            reactions = data.setdefault("reactions", {})
            users = reactions.setdefault(emoji, [])
            # v1.9.7: новые дельты несут ЯВНЫЙ флаг added (поставлена/снята) —
            # применение идемпотентно. Раньше дельта была слепым переключателем:
            # экран чата тогглил реакцию локально ДО ответа сервера
            # (_on_reaction_requested), дельта приезжала по поллингу и
            # переключала ещё раз — реакция «сама снималась» через секунду
            # после клика. Легаси-дельты без флага — переключение, как раньше
            # (для совместимости со старыми серверами/логами).
            if "added" in delta_ev:
                want = bool(delta_ev["added"])
                have = sender in users
                if want and not have:
                    users.append(sender)
                elif not want and have:
                    users.remove(sender)
            elif sender in users:
                users.remove(sender)
            else:
                users.append(sender)
            if not users:
                del reactions[emoji]
    elif kind == "pin":
        data["pinned"] = bool(delta_ev.get("pinned"))
    elif kind == "link_preview":
        preview = delta_ev.get("link_preview")
        if preview:
            data["link_preview"] = preview
    return data


def decrypt_text_of(msg: dict) -> str:
    """Расшифровывает текст сообщения (логика — в client.decrypt_message).

    Если расшифровка не удалась (сменилась соль, ключ, или старый формат) —
    плейсхолдер вместо сырого JSON, чтобы не пугать юзера. Используется и
    общим чатом (ChatSession.decrypt_text), и экраном ЛС (dm_screen) —
    раньше плейсхолдер дублировался в обоих экранах."""
    dec = client.decrypt_message(msg)
    if dec.get("_decrypt_failed"):
        return "🔒 [не удалось расшифровать — возможно, сменился ключ]"
    return dec.get("text", "")


@dataclass
class TailDiff:
    """Разница между прежним хвостом канала и свежескачанным.

    added   — новые сообщения (в порядке следования);
    changed — изменившиеся (правка/реакции/пин/превью);
    removed — seq'ы удалённых сообщений.
    """

    added: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff_tail(old: list[dict], new: list[dict]) -> TailDiff:
    """Дифф двух хвостов канала по seq.

    Сообщения без целочисленного seq пропускаются (как и раньше в экране —
    там их просто не было в _message_widgets). Сравнение — по содержимому
    словаря: тот же критерий, что раньше сравнивал bubble.message_data != msg.
    """
    old_by_seq: dict[int, dict] = {}
    for msg in old:
        seq = msg.get("seq")
        if isinstance(seq, int):
            old_by_seq[seq] = msg

    diff = TailDiff()
    seen: set[int] = set()
    for msg in new:
        seq = msg.get("seq")
        if not isinstance(seq, int):
            continue
        seen.add(seq)
        prev = old_by_seq.get(seq)
        if prev is None:
            diff.added.append(msg)
        elif prev != msg:
            diff.changed.append(msg)

    diff.removed = [seq for seq in old_by_seq if seq not in seen]
    return diff


@dataclass
class DispatchVerdict:
    """Что делать с ответом /dispatch — решает ядро, исполняет экран.

    kind="text"      — бот не сматчился: отправить исходный текст как сообщение;
    kind="open_game" — открыть диалог игры (bot_id/meta/tab);
    kind="noop"      — ничего не делать (бот уже всё сделал на сервере).
    refresh          — после исполнения дожать ленту (after_message_action).
    """

    kind: str = "noop"
    text: str = ""
    bot_id: str = ""
    meta: dict = field(default_factory=dict)
    tab: str = "play"
    refresh: bool = False


def interpret_dispatch(result: dict, original_text: str) -> DispatchVerdict:
    """Разбирает ответ /dispatch (дословный перенос _on_dispatch_result).

    Если бот не сматчил — исходный текст уходит в чат обычным сообщением.
    Если сматчил и вернул action=open_game — открыть игру; сопутствующий
    текст бот уже добавил на сервере, ленту дожимаем дельтой. Если бот
    просто что-то написал в чат — тоже дожимаем.
    """
    if not result.get("matched"):
        return DispatchVerdict(kind="text", text=original_text)

    bot_id = result.get("bot_id", "")
    bot_action = result.get("bot_action")
    bot_response = result.get("bot_response", "")

    if isinstance(bot_action, dict) and bot_action.get("action") == "open_game":
        return DispatchVerdict(
            kind="open_game",
            bot_id=bot_id,
            meta=bot_action.get("meta") or {},
            tab=bot_action.get("tab", "play"),
            refresh=bool(bot_response),
        )

    # v3: match_created / match_joined — тоже открывают диалог игры,
    # неся внутри meta сам матч (чтобы диалог не делал повторный запрос).
    # Раньше такие ответы падали в noop — пользователь набирал
    # !chess join ABCD, матч присоединялся, но диалог не открывался.
    if isinstance(bot_action, dict):
        action = bot_action.get("action")
        if action in ("match_created", "match_joined"):
            return DispatchVerdict(
                kind="open_game",
                bot_id=bot_id,
                meta=bot_action.get("match") or {},
                tab="play",
                refresh=bool(bot_response),
            )

    if bot_response:
        return DispatchVerdict(kind="noop", refresh=True)
    return DispatchVerdict(kind="noop", refresh=False)


# ── ChatSession: состояние + политика ───────────────────────────────────────


class ChatSession:
    """Ядро экрана чата: поллинг, каналы, типинг, дешифровка, dispatch.

    Один экземпляр на ChatScreen. Колбэки вызываются в потоке runner'а —
    в приложении AsyncBridge доставляет их в GUI-поток через сигналы,
    поэтому ядро про потоки не думает.
    """

    def __init__(
        self,
        base_url: str = "",
        my_name: str = "",
        access_key: str = "",
        runner: Runner | None = None,
    ) -> None:
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key
        self._runner: Runner = runner or (lambda fn, on_success=None, on_error=None: None)
        self._connected = False

        # Поллинг
        self._last_seq = 0
        self._polling = False
        self._poll_in_flight = False
        self._poll_kick_pending = False

        # Каналы
        self._current_channel: str | None = None  # None = общий чат
        self._channel_tails: dict[str, list[dict]] = {}
        # Кэш общего хвоста (последний полный снапшот initial) — вьюха
        # рисует его мгновенно при возврате в общий чат, сеть догоняет.
        self._general_tail: list[dict] = []

        # «Печатает»
        self._typing_users: dict[str, float] = {}  # username -> time.time()
        self._last_typing_sent = 0.0  # monotonic, троттлинг send_typing

        # Детектор обрыва связи (v1.9.5): подряд идущие ошибки поллинга.
        # Раньше _on_poll_error глотал всё бесконечно — хост умер, а UI
        # вечно показывал «подключён», онлайн-чипы и «печатает» замирали.
        self._poll_errors = 0
        self._poll_reported_down = False
        # (v3.5.0) Бэкофф: пока хост недоступен — пропускаем часть тиков
        # поллинга, чтобы не долбить мёртвый адрес на полной частоте.
        self._poll_backoff_skip = 0

        # Live-обновление открытого канала (v1.9.5): раньше сообщения канала
        # приезжали ТОЛЬКО после собственных действий (refresh_channel_tail
        # звался из after_message_action) — сидя в канале, чужие сообщения
        # не видно до переключения. Теперь тик догоняет и канал.
        self._channel_refresh_in_flight = False
        self._channel_refresh_pending = False

        # Колбэки (подписывается вьюха)
        self.on_poll_result: Callable[[dict], None] | None = None
        self.on_initial_result: Callable[[dict], None] | None = None
        self.on_initial_error: ERROR_CB | None = None
        self.on_channel_loaded: Callable[[str, list[dict]], None] | None = None
        self.on_channel_load_error: ERROR_CB | None = None
        self.on_channel_diff: Callable[[str, TailDiff], None] | None = None
        self.on_polling_stopped: Callable[[], None] | None = None
        # v1.9.5: связь пропала (N ошибок подряд) / восстановилась
        self.on_poll_failed: ERROR_CB | None = None
        self.on_poll_recovered: Callable[[], None] | None = None

    # -- Подключение -------------------------------------------------------

    def configure(self, base_url: str, my_name: str, access_key: str) -> None:
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key

    @property
    def my_name(self) -> str:
        return self._my_name

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_connected(self, connected: bool) -> None:
        self._connected = connected

    def reset(self) -> None:
        """Полная очистка при отключении (дословно _clear_all из экрана)."""
        self._last_seq = 0
        self._typing_users = {}
        self._last_typing_sent = 0.0
        self._current_channel = None
        self._channel_tails = {}
        self._general_tail = []
        self._poll_errors = 0
        self._poll_reported_down = False
        self._poll_backoff_skip = 0
        self._channel_refresh_in_flight = False
        self._channel_refresh_pending = False

    # -- Курсор ленты -------------------------------------------------------

    @property
    def last_seq(self) -> int:
        return self._last_seq

    # -- Live-поллинг -------------------------------------------------------

    def start_polling(self) -> None:
        self._polling = True

    def stop_polling(self) -> None:
        self._polling = False

    @property
    def is_polling(self) -> bool:
        return self._polling

    def tick(self) -> None:
        """Тик таймера поллинга (дословно _on_poll_tick: не подключены —
        останавливаемся, экран глушит свой QTimer по on_polling_stopped).

        v1.9.5: если открыт КАНАЛ — заодно догоняем его хвост тихим диффом
        (refresh_channel_tail). /events канальных сообщений не содержит
        (они живут в отдельном файле канала), поэтому без этого сидящий
        в канале не видел чужих сообщений, пока сам что-нибудь не отправит."""
        if not self._connected:
            self.stop_polling()
            if self.on_polling_stopped is not None:
                self.on_polling_stopped()
            return
        # (v3.5.0) Бэкофф: хост недоступен (>=5 ошибок подряд) — пропускаем
        # тик. Частота опроса падает от «каждый тик» до «каждый 15-й» и
        # мгновенно восстанавливается после первого же успешного ответа.
        if self._poll_backoff_skip > 0:
            self._poll_backoff_skip -= 1
            return
        self.do_poll()
        if self._current_channel is not None:
            self.refresh_channel_tail()

    def do_poll(self) -> None:
        """Один запрос дельты /events?since=. Если прошлый запрос ещё летит —
        помечаем «нужен ещё один» и добираем сразу после его завершения,
        не запуская параллельные запросы. Публичный = kick (мгновенная
        дельта-синхронизация после действия пользователя)."""
        if not self._connected:
            return
        if self._poll_in_flight:
            self._poll_kick_pending = True
            return
        self._poll_in_flight = True
        self._runner(
            lambda: client.poll_events(
                self._base_url, self._last_seq, self._access_key, self._my_name
            ),
            on_success=self._on_poll_success,
            on_error=self._on_poll_error,
        )

    # Синоним с прежним именем из экрана — чтобы чтение кода не ломалось.
    kick = do_poll

    def _on_poll_success(self, result: dict) -> None:
        self._poll_errors = 0
        self._poll_backoff_skip = 0  # (v3.5.0) связь жива — полная частота
        if self._poll_reported_down:
            # связь вернулась (после N ошибок подряд) — экран убирает «⚠ нет связи»
            self._poll_reported_down = False
            if self.on_poll_recovered is not None:
                self.on_poll_recovered()
        self._last_seq = result.get("next_since", self._last_seq)
        if self.on_poll_result is not None:
            self.on_poll_result(result)
        self._poll_done()

    def _on_poll_error(self, e: Exception) -> None:
        # Разовый сбой — тихо пропускаем тик. Но N ПОДРЯД ошибок = хост
        # умер: молчать нельзя (v1.9.5), сообщаем экрану один раз.
        self._poll_errors += 1
        if self._poll_errors >= 5:
            if not self._poll_reported_down:
                self._poll_reported_down = True
                if self.on_poll_failed is not None:
                    self.on_poll_failed(e)
            # (v3.5.0) бэкофф: 5..9 ошибок → каждый 2-й тик,
            # 10..19 → каждый 5-й, 20+ → каждый 15-й (при тике ~1.5с это ~22с)
            if self._poll_errors >= 20:
                self._poll_backoff_skip = 14
            elif self._poll_errors >= 10:
                self._poll_backoff_skip = 4
            else:
                self._poll_backoff_skip = 1
        self._poll_done()

    def _poll_done(self) -> None:
        self._poll_in_flight = False
        if self._poll_kick_pending:
            self._poll_kick_pending = False
            self.do_poll()

    # -- Первичная загрузка ленты -------------------------------------------

    def load_initial(self) -> None:
        """Полная перезагрузка ТЕКУЩЕЙ ленты (первое подключение, смена
        канала, возврат в общий чат). В режиме канала «перезагрузить ленту»
        = перезагрузить канал. Живые обновления после неё идут инкрементально
        через tick/do_poll."""
        if not self._connected:
            return
        if self._current_channel is not None:
            self.load_channel(self._current_channel)
            return

        self._runner(
            lambda: client.poll_events(
                self._base_url,
                0,
                self._access_key,
                self._my_name,
                tail=CHAT_TAIL_ON_CONNECT,
            ),
            on_success=self._on_initial_success,
            on_error=self.on_initial_error,
        )

    def _on_initial_success(self, result: dict) -> None:
        if not self._connected:
            # Пока initial летел, пользователь нажал «Отключиться» — всё
            # уже очищено reset(); поздний ответ не должен снова рисовать
            # ленту и онлайн-чипы при статусе «Не подключено» (v1.9.5).
            return
        if self._current_channel is not None:
            # Пока общий tail летел, открыли канал — не заменяем его ленту
            # общими сообщениями (ответ на устаревший запрос).
            return
        self._last_seq = result.get("next_since", self._last_seq)
        self._general_tail = list(result.get("events", []))
        if self.on_initial_result is not None:
            self.on_initial_result(result)

    # -- Каналы ---------------------------------------------------------------

    @property
    def current_channel(self) -> str | None:
        return self._current_channel

    def set_channel(self, channel_name: str | None) -> None:
        self._current_channel = channel_name

    def channel_tail(self, channel_name: str) -> list[dict]:
        """Последний загруженный хвост канала (для мгновенной отрисовки
        при переключении и для тестов)."""
        return self._channel_tails.get(channel_name, [])

    @property
    def general_tail(self) -> list[dict]:
        """Кэш общего хвоста: что последний раз вернул initial (сырые
        события, фильтрацию kind'ов делает вьюха). Пополняется ТОЛЬКО
        полными снапшотами — дельты поллинга его не трогают."""
        return self._general_tail

    def load_channel(self, channel_name: str) -> None:
        """Полная загрузка сообщений канала (при переключении)."""
        if not self._connected:
            return
        self._runner(
            lambda: client.get_channel_messages(
                self._base_url, channel_name, 50, self._my_name, self._access_key
            ),
            on_success=lambda msgs: self._on_channel_loaded(channel_name, msgs),
            on_error=self._on_channel_load_error,
        )

    def _on_channel_loaded(self, channel_name: str, messages: list[dict]) -> None:
        if channel_name != self._current_channel:
            # Пока запрос летел, канал переключили: старые сообщения не должны
            # затирать ленту нового канала. (Тот же guard, что давно стоит у
            # общего tail в _on_initial_success; у каналов раньше был пропущен —
            # при быстрой смене каналов старый ответ перерисовывал ленту.)
            return
        self._channel_tails[channel_name] = list(messages)
        if self.on_channel_loaded is not None:
            self.on_channel_loaded(channel_name, messages)

    def _on_channel_load_error(self, error: Exception) -> None:
        if self.on_channel_load_error is not None:
            self.on_channel_load_error(error)

    def refresh_channel_tail(self) -> None:
        """Тихая догрузка хвоста открытого канала (после send/delete И на
        каждом тике поллинга с v1.9.5) — разница применяется диффом, без
        пересборки. Тик дёргает каждые ~2с: без in-flight guard запросы
        накладывались, и УСТАРЕВШИЙ ответ перетирал свежий хвост диффом
        наоборот (мессадж «исчезал» до следующего тика)."""
        ch = self._current_channel
        if not ch or not self._connected:
            return
        if self._channel_refresh_in_flight:
            self._channel_refresh_pending = True
            return
        self._channel_refresh_in_flight = True
        self._runner(
            lambda: client.get_channel_messages(
                self._base_url, ch, 50, self._my_name, self._access_key
            ),
            on_success=lambda msgs: self._on_channel_tail_fresh(ch, msgs),
            on_error=lambda _e: self._channel_refresh_done(),
        )

    def _channel_refresh_done(self) -> None:
        self._channel_refresh_in_flight = False
        if self._channel_refresh_pending:
            self._channel_refresh_pending = False
            self.refresh_channel_tail()

    def _on_channel_tail_fresh(self, channel_name: str, messages: list[dict]) -> None:
        self._channel_refresh_done()
        if channel_name != self._current_channel:
            return  # пока запрос летел, канал переключили
        diff = diff_tail(self._channel_tails.get(channel_name, []), messages)
        self._channel_tails[channel_name] = list(messages)
        if diff.is_empty():
            return
        if self.on_channel_diff is not None:
            self.on_channel_diff(channel_name, diff)

    def after_message_action(self) -> None:
        """Обновление ленты после успешного действия (send/edit/delete/pin/файл).
        В общем чате — мгновенный дельта-poll; в канале — тихое дифф-обновление
        хвоста. (Дословно _after_message_action.)"""
        if self._current_channel is not None:
            self.refresh_channel_tail()
        else:
            self.do_poll()

    # -- «Печатает» -------------------------------------------------------------

    def note_typing(self, sender: str, now: float | None = None) -> None:
        """Чужой send_typing: запоминаем (своё игнорируем — дословно
        handle_typing_event)."""
        if sender == self._my_name:
            return
        self._typing_users[sender] = time.time() if now is None else now

    def active_typers(self, now: float | None = None) -> list[str]:
        """Активные «печатающие» (не протухшие за TYPING_TTL) с зачисткой."""
        current_time = time.time() if now is None else now
        self._typing_users = {
            name: ts for name, ts in self._typing_users.items() if current_time - ts < TYPING_TTL
        }
        return list(self._typing_users.keys())

    def should_send_typing(self, now: float | None = None) -> bool:
        """Не чаще раза в TYPING_SEND_EVERY сек, чтобы не долбить сервер на
        каждую нажатую клавишу (дословный троттлинг _on_input_text_edited)."""
        if not self._connected:
            return False
        now_monotonic = time.monotonic() if now is None else now
        if now_monotonic - self._last_typing_sent < TYPING_SEND_EVERY:
            return False
        self._last_typing_sent = now_monotonic
        return True

    # -- Онлайн -------------------------------------------------------------

    def sort_online(self, names: list[str]) -> list[str]:
        """Состав чипов онлайна: свои — первыми, далее по алфавиту без
        регистра (дословно sorted_online из _update_online_and_typing)."""
        return sorted(
            names,
            key=lambda n: (0 if n == self._my_name else 1, n.lower()),
        )

    # -- Дешифровка -------------------------------------------------------------

    def decrypt_text(self, msg: dict) -> str:
        """Расшифровка текста сообщения — см. decrypt_text_of (уровень модуля,
        ею же пользуется экран ЛС)."""
        return decrypt_text_of(msg)
