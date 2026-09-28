"""
Экран «Боты» — управление ботами на хосте.

Структура:
  - Слева: список ботов как карточки (avatar + name + meta + действия)
  - Справа: панель с глобальными действиями (добавить, удалить, справка)

Каждая карточка показывает:
  - Иконку бота (🤖 или 🎮 для игр)
  - Имя + bot_id
  - Метаданные: trigger, тип (игра/командный), статус
  - Кнопки действий: «Открыть» (для ботов-игр), «Справка», «Удалить»

Боты — фича ХОСТА. Через set_bot_manager() передаётся ссылка на
BotManager когда хост запущен. Пока хоста нет — показываем empty state.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qt_app.theme import PALETTE, RADIUS, RADIUS_LG, RADIUS_SM

# Иконки по bot_id (для ботов-игр — аркадные, для остальных — стандартные)
_BOT_ICONS = {
    "snake": "🐍",
    "orbital": "🛰️",
    "voxel_shooter": "🔫",
    "night_shift": "🦇",
    "chess": "♟️",
    "go": "⚫",
}

# Боты-игры (для них показываем кнопку «Открыть»)
_GAME_BOTS = {"snake", "orbital", "voxel_shooter", "chess", "go"}


def _glass_btn_qss(primary: bool = False, danger: bool = False) -> str:
    if danger:
        bg = PALETTE.danger
        hover = "#e0475d"
    elif primary:
        bg = PALETTE.accent
        hover = PALETTE.accent_hover
    else:
        bg = PALETTE.glass_bg
        hover = PALETTE.glass_bg_hover
    return f"""
        QPushButton {{
            background: {bg};
            color: {PALETTE.text_on_accent if (primary or danger) else PALETTE.text_primary};
            border: 1px solid {PALETTE.border if not (primary or danger) else bg};
            border-radius: {RADIUS_SM}px;
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {hover};
            border-color: {PALETTE.border_strong if not (primary or danger) else hover};
        }}
        QPushButton:pressed {{
            background: {PALETTE.surface_3 if not (primary or danger) else PALETTE.accent_press};
        }}
    """


def _chip_qss(color: str = PALETTE.text_secondary) -> str:
    return f"""
        QLabel {{
            background: {PALETTE.glass_bg};
            color: {color};
            border: 1px solid {PALETTE.border_soft};
            border-radius: 10px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
        }}
    """


class BotCard(QFrame):
    """Карточка одного бота с действиями.

    Эмитит сигналы:
      - open_requested (для ботов-игр)
      - help_requested
      - remove_requested
      - trigger_change_requested (новый триггер как str)
    """

    open_requested = Signal(str)        # bot_id
    help_requested = Signal(str)        # bot_id
    remove_requested = Signal(str)      # bot_id
    trigger_change_requested = Signal(str, str)  # bot_id, new_trigger

    def __init__(self, bot_id: str, display_name: str, status: str,
                 meta: dict | None = None, parent=None):
        super().__init__(parent)
        self.bot_id = bot_id
        self.setObjectName("BotCard")
        self.setStyleSheet(f"""
            #BotCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_LG}px;
            }}
            #BotCard:hover {{
                border-color: {PALETTE.border};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        # ── Иконка ──
        icon_char = _BOT_ICONS.get(bot_id, "🤖")
        icon = QLabel(icon_char)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background: {PALETTE.surface_2}; border-radius: 22px; font-size: 22px;"
        )
        layout.addWidget(icon)

        # ── Имя + мета ──
        meta_col = QVBoxLayout()
        meta_col.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_lbl = QLabel(display_name)
        name_lbl.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-weight: 700; font-size: 14px;"
        )
        name_row.addWidget(name_lbl)

        # Чип «игра» если это бот-игра
        if bot_id in _GAME_BOTS:
            game_chip = QLabel("🎮 ИГРА")
            game_chip.setStyleSheet(_chip_qss(color=PALETTE.success))
            game_chip.setFixedHeight(20)
            name_row.addWidget(game_chip)

        name_row.addStretch(1)
        meta_col.addLayout(name_row)

        # Строка с метаданными: id + trigger + status
        details_row = QHBoxLayout()
        details_row.setSpacing(8)

        id_chip = QLabel(f"id: {bot_id}")
        id_chip.setStyleSheet(_chip_qss())
        id_chip.setFixedHeight(20)
        details_row.addWidget(id_chip)

        # Trigger chip (если есть)
        trigger = None
        if meta:
            trigger = meta.get("trigger")
        if trigger:
            trigger_chip = QLabel(f"⚡ !{trigger}")
            trigger_chip.setStyleSheet(_chip_qss(color=PALETTE.accent))
            trigger_chip.setFixedHeight(20)
            details_row.addWidget(trigger_chip)

        status_chip = QLabel(f"● {status}")
        status_chip.setStyleSheet(_chip_qss(color=PALETTE.success))
        status_chip.setFixedHeight(20)
        details_row.addWidget(status_chip)

        details_row.addStretch(1)
        meta_col.addLayout(details_row)

        layout.addLayout(meta_col, 1)

        # ── Кнопки действий ──
        actions_col = QVBoxLayout()
        actions_col.setSpacing(4)

        # Верхний ряд: Открыть (для игр) + Справка
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        if bot_id in _GAME_BOTS:
            open_btn = QPushButton("▶ Открыть")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.setStyleSheet(_glass_btn_qss(primary=True))
            open_btn.clicked.connect(lambda: self.open_requested.emit(self.bot_id))
            top_row.addWidget(open_btn)

        help_btn = QPushButton("? Справка")
        help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        help_btn.setStyleSheet(_glass_btn_qss())
        help_btn.clicked.connect(lambda: self.help_requested.emit(self.bot_id))
        top_row.addWidget(help_btn)

        actions_col.addLayout(top_row)

        # Нижний ряд: Сменить триггер + Удалить
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)

        if trigger:
            trigger_btn = QPushButton("⚡ Триггер")
            trigger_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            trigger_btn.setStyleSheet(_glass_btn_qss())
            trigger_btn.clicked.connect(self._on_trigger_click)
            bottom_row.addWidget(trigger_btn)

        remove_btn = QPushButton("🗑")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setFixedWidth(36)
        remove_btn.setStyleSheet(_glass_btn_qss(danger=True))
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.bot_id))
        bottom_row.addWidget(remove_btn)

        actions_col.addLayout(bottom_row)

        layout.addLayout(actions_col)

    def _on_trigger_click(self) -> None:
        """Открывает диалог ввода нового триггера."""
        from PySide6.QtWidgets import QInputDialog

        new_trigger, ok = QInputDialog.getText(
            self, "Сменить триггер",
            "Новый триггер (без !):",
        )
        if ok and new_trigger.strip():
            self.trigger_change_requested.emit(self.bot_id, new_trigger.strip())


class BotsScreen(QWidget):
    """Главный экран «Боты»: список карточек + панель действий справа."""

    # Сигнал наверх, чтобы открыть игру (проксируется в ChatScreen)
    open_game_requested = Signal(str)  # bot_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bot_manager = None
        # Параметры подключения (прокидываются через set_connection)
        self._base_url = ""
        self._my_name = ""
        self._access_key = ""

        root = QHBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(20)

        # ── Левая колонка: список ботов ──
        left = QVBoxLayout()
        left.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("🤖  Боты")
        title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 20px; font-weight: 700;"
        )
        header.addWidget(title)
        header.addStretch(1)

        refresh_btn = QPushButton("🔄 Обновить")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.glass_bg};
                color: {PALETTE.text_secondary};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {PALETTE.glass_bg_hover};
                color: {PALETTE.text_primary};
                border-color: {PALETTE.border};
            }}
        """)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        left.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_host)
        left.addWidget(self._scroll, 1)
        root.addLayout(left, 3)

        # ── Правая колонка: добавить/удалить + справка ──
        right = QVBoxLayout()
        right.setSpacing(16)

        # Карточка добавления
        add_card = QFrame()
        add_card.setObjectName("AddCard")
        add_card.setStyleSheet(f"""
            #AddCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_LG}px;
            }}
        """)
        add_layout = QVBoxLayout(add_card)
        add_layout.setContentsMargins(16, 16, 16, 16)
        add_layout.setSpacing(8)

        add_title = QLabel("➕  Добавить бота")
        add_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )
        add_layout.addWidget(add_title)

        add_hint = QLabel("ID бота (например: night_shift)")
        add_hint.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        add_layout.addWidget(add_hint)

        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("bot_id…")
        self._add_input.setStyleSheet(f"""
            QLineEdit {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 8px 12px;
                color: {PALETTE.text_primary};
            }}
            QLineEdit:focus {{
                border: 1px solid {PALETTE.accent};
                background: {PALETTE.surface_4};
            }}
        """)
        self._add_input.returnPressed.connect(self._on_add)
        add_layout.addWidget(self._add_input)

        add_btn = QPushButton("Добавить")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(_glass_btn_qss(primary=True))
        add_btn.clicked.connect(self._on_add)
        add_layout.addWidget(add_btn)

        right.addWidget(add_card)

        # Карточка удаления
        remove_card = QFrame()
        remove_card.setObjectName("RemoveCard")
        remove_card.setStyleSheet(f"""
            #RemoveCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_LG}px;
            }}
        """)
        remove_layout = QVBoxLayout(remove_card)
        remove_layout.setContentsMargins(16, 16, 16, 16)
        remove_layout.setSpacing(8)

        remove_title = QLabel("🗑  Удалить бота")
        remove_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )
        remove_layout.addWidget(remove_title)

        remove_hint = QLabel("ID бота или кликни 🗑 на карточке")
        remove_hint.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        remove_layout.addWidget(remove_hint)

        self._remove_input = QLineEdit()
        self._remove_input.setPlaceholderText("bot_id…")
        self._remove_input.setStyleSheet(f"""
            QLineEdit {{
                background: {PALETTE.surface_3};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS}px;
                padding: 8px 12px;
                color: {PALETTE.text_primary};
            }}
            QLineEdit:focus {{
                border: 1px solid {PALETTE.danger};
                background: {PALETTE.surface_4};
            }}
        """)
        self._remove_input.returnPressed.connect(self._on_remove)
        remove_layout.addWidget(self._remove_input)

        remove_btn = QPushButton("Удалить")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setStyleSheet(_glass_btn_qss(danger=True))
        remove_btn.clicked.connect(self._on_remove)
        remove_layout.addWidget(remove_btn)

        right.addWidget(remove_card)

        # Карточка-подсказка
        help_card = QFrame()
        help_card.setObjectName("HelpCard")
        help_card.setStyleSheet(f"""
            #HelpCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_LG}px;
            }}
        """)
        help_layout = QVBoxLayout(help_card)
        help_layout.setContentsMargins(16, 16, 16, 16)
        help_layout.setSpacing(6)

        help_title = QLabel("📝  Как добавить своего бота")
        help_title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 13px; font-weight: 700;"
        )
        help_layout.addWidget(help_title)

        help_text = QLabel(
            "Код бота ищется в <code>lib/{id}_bot.py</code><br>"
            "с классом <code>{Id}Bot(data_dir)</code>.<br><br>"
            "<b>Например:</b> id=night_shift →<br>"
            "файл <code>lib/night_shift_bot.py</code>,<br>"
            "класс <code>NightShiftBot</code>.<br><br>"
            "Подробности — в <code>docs/HOW_TO_ADD_BOT.md</code>."
        )
        help_text.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
        )
        help_text.setTextFormat(Qt.TextFormat.RichText)
        help_text.setWordWrap(True)
        help_layout.addWidget(help_text)

        right.addWidget(help_card)
        right.addStretch(1)

        right_host = QWidget()
        right_host.setFixedWidth(280)
        right_host.setLayout(right)
        root.addWidget(right_host)

        self._show_empty_state(
            "Хост не запущен\n\nЗапусти хост в настройках,\nчтобы управлять ботами"
        )

    def _show_empty_state(self, text: str) -> None:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 13px; padding: 32px;"
        )
        self._list_layout.addWidget(label)
        self._list_layout.addStretch(1)

    # ── Публичный API ────────────────────────────────────────────────

    def set_bot_manager(self, bot_manager) -> None:
        """bot_manager: lib.bots.manager.BotManager | None (None = хост не запущен)."""
        self._bot_manager = bot_manager
        self.refresh()

    def set_connection(self, base_url: str, my_name: str, access_key: str,
                       connected: bool) -> None:
        """Прокидывается из MainWindow при подключении."""
        self._base_url = base_url
        self._my_name = my_name
        self._access_key = access_key

    def refresh(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()

        if self._bot_manager is None:
            self._show_empty_state(
                "Хост не запущен\n\nЗапусти хост в настройках,\nчтобы управлять ботами"
            )
            return

        bot_ids = self._bot_manager.list_bots()
        if not bot_ids:
            self._show_empty_state("Нет активных ботов\n\nДобавь бота справа →")
            return

        for bid in bot_ids:
            bot = self._bot_manager.get_bot(bid)
            display = getattr(bot, "name", bid) if bot else bid
            status = self._bot_status(bot)
            meta = bot.get_meta() if bot and hasattr(bot, "get_meta") else {}
            card = BotCard(bid, display, status, meta)
            card.open_requested.connect(self._on_open_game)
            card.help_requested.connect(self._on_show_help)
            card.remove_requested.connect(self._on_remove_by_id)
            card.trigger_change_requested.connect(self._on_change_trigger)
            self._list_layout.addWidget(card)
        self._list_layout.addStretch(1)

    def _bot_status(self, bot) -> str:
        if bot is None:
            return "Неизвестно"
        try:
            if hasattr(bot, "get_status"):
                return bot.get_status()
            if hasattr(bot, "enabled"):
                return "Активен" if bot.enabled else "Отключён"
        except Exception:
            return "Ошибка"
        return "Активен"

    # ── Обработчики действий ─────────────────────────────────────────

    def _on_open_game(self, bot_id: str) -> None:
        """Открывает игру бота (snake/orbital/voxel/chess). Эмитит сигнал наверх."""
        self.open_game_requested.emit(bot_id)

    def _on_show_help(self, bot_id: str) -> None:
        """Показывает справку по боту."""
        if self._bot_manager is None:
            return
        bot = self._bot_manager.get_bot(bot_id)
        if bot is None:
            QMessageBox.warning(self, "Боты", f"Бот '{bot_id}' не найден")
            return
        try:
            help_text = bot.get_help()
        except Exception as e:
            help_text = f"Ошибка получения справки: {e}"

        # Показываем в диалоге с моноширинным шрифтом
        from PySide6.QtWidgets import QDialog, QTextEdit

        dlg = QDialog(self)
        dlg.setWindowTitle(f"📖 Справка: {getattr(bot, 'name', bot_id)}")
        dlg.setMinimumSize(560, 420)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setPlainText(help_text)
        text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {PALETTE.surface_1};
                color: {PALETTE.text_primary};
                border: none;
                padding: 20px;
                font-family: 'Consolas, Menlo, monospace';
                font-size: 13px;
            }}
        """)
        layout.addWidget(text_edit)

        from PySide6.QtWidgets import QHBoxLayout as _QHBoxLayout

        btn_row = _QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.setStyleSheet(_glass_btn_qss(primary=True))
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        btn_row.addStretch(1)
        btn_row_widget = QWidget()
        btn_row_widget.setLayout(btn_row)
        btn_row_widget.setStyleSheet(f"background: {PALETTE.surface_1}; padding: 12px;")
        layout.addWidget(btn_row_widget)

        dlg.exec()

    def _on_remove_by_id(self, bot_id: str) -> None:
        """Удаление бота по клику на 🗑 в карточке."""
        if self._bot_manager is None:
            return
        bot = self._bot_manager.get_bot(bot_id)
        name = getattr(bot, "name", bot_id) if bot else bot_id
        reply = QMessageBox.question(
            self, "Боты",
            f"Удалить бота '{name}' (id: {bot_id})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._bot_manager.unregister_bot(bot_id):
            self.refresh()
        else:
            QMessageBox.warning(self, "Боты", f"Бот '{bot_id}' не найден")

    def _on_change_trigger(self, bot_id: str, new_trigger: str) -> None:
        """Смена триггера через /bot_command с подкомандой trigger."""
        if self._bot_manager is None:
            return
        bot = self._bot_manager.get_bot(bot_id)
        if bot is None:
            QMessageBox.warning(self, "Боты", f"Бот '{bot_id}' не найден")
            return

        try:
            response = bot.process_command(self._my_name or "admin", "trigger", [new_trigger])
            if isinstance(response, str):
                QMessageBox.information(self, "Боты", response)
                self.refresh()
            else:
                QMessageBox.warning(self, "Боты", "Не удалось сменить триггер")
        except Exception as e:
            QMessageBox.critical(self, "Боты", f"Ошибка: {e}")

    def _on_add(self) -> None:
        bid = self._add_input.text().strip()
        if not bid:
            QMessageBox.warning(self, "Боты", "Введи ID бота")
            return
        if self._bot_manager is None:
            QMessageBox.warning(self, "Боты", "Сначала запусти хост")
            return
        ok = self._bot_manager.register_bot_by_id(bid)
        if ok:
            self._add_input.clear()
            self.refresh()
        else:
            class_name = "".join(p.capitalize() for p in bid.split("_")) + "Bot"
            QMessageBox.critical(
                self,
                "Боты",
                f"Не удалось загрузить бота '{bid}'.\n"
                f"Убедись что файл lib/{bid}_bot.py существует\n"
                f"и в нём класс {class_name}.",
            )

    def _on_remove(self) -> None:
        bid = self._remove_input.text().strip()
        if not bid:
            QMessageBox.warning(self, "Боты", "Введи ID бота")
            return
        if self._bot_manager is None:
            QMessageBox.warning(self, "Боты", "Сначала запусти хост")
            return
        reply = QMessageBox.question(self, "Боты", f"Удалить бота '{bid}'?")
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._bot_manager.unregister_bot(bid):
            self._remove_input.clear()
            self.refresh()
        else:
            QMessageBox.warning(self, "Боты", f"Бот '{bid}' не найден")
