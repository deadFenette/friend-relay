"""
Главное окно — ОДИН QMainWindow на всё приложение.

Собирает:
  - TitleBar (frameless-хром) — только на платформах, где сами
    тянем drag/resize; на macOS/wayland остаются нативные декорации;
  - Rail — вертикальный слева, а при окне < 800px
    перекладывается вниз как «bottom tab bar» (responsive);
  - SlidingStackedWidget (Canvas) — все экраны как страницы стека;
  - CommandPalette (Ctrl+K) — spotlight-оверлей с командами:
    переходы по разделам (Ctrl+1..6), каналы, действия.

Palette (контекстная правая панель) подключается экранами по мере
необходимости.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QMessageBox, QVBoxLayout, QWidget

from lib.constants import APP_TITLE
from qt_app.screens.bots_screen import BotsScreen
from qt_app.screens.chat_screen import ChatScreen
from qt_app.screens.dm_screen import DMScreen
from qt_app.screens.files_screen import FilesScreen
from qt_app.screens.profile_settings import ProfileSettingsScreen
from qt_app.screens.settings_screen import SettingsScreen
from qt_app.widgets.command_palette import CommandPalette
from qt_app.widgets.rail import Rail
from qt_app.widgets.sliding_stack import SlidingStackedWidget
from qt_app.widgets.title_bar import (
    EdgeResizeGrips,
    TitleBar,
    WindowsHitTester,
    apply_dwm_shadow,
    platform_supports_frameless,
)

RAIL_ITEMS = [
    ("chat", "chat", "Чат"),
    ("dm", "dm", "ЛС"),
    ("files", "files", "Файлы"),
    ("bots", "chess", "Боты"),  # временно chess, потом добавим отдельную иконку
    ("profile", "profile", "Профиль"),
    ("settings", "theme", "Настройки"),
]

# Responsive-макет: уже 800px — рейл становится нижней панелью
RAIL_BOTTOM_BREAKPOINT = 800


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 720)
        self.setMinimumSize(720, 480)

        # -- Frameless-хром: только где сами тянем drag/resize --
        self._frameless = platform_supports_frameless()
        if self._frameless:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._grips: EdgeResizeGrips | None = None
        self._shadow_applied = False

        root = QWidget()
        root.setObjectName("RootSurface")
        self.setCentralWidget(root)

        self._root_layout = QVBoxLayout(root)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        self._titlebar: TitleBar | None = None
        if self._frameless:
            self._titlebar = TitleBar(self)
            self._root_layout.addWidget(self._titlebar)
            self._titlebar.minimize_clicked.connect(self.showMinimized)
            self._titlebar.maximize_toggled.connect(self._toggle_maximized)
            self._titlebar.close_clicked.connect(self.close)

        # Контентная строка: Rail + стек. При <800px рейл перекладывается
        # из неё в корневой layout (вниз окна).
        self._content_host = QWidget()
        self._content_layout = QHBoxLayout(self._content_host)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self._root_layout.addWidget(self._content_host, 1)

        self.rail = Rail(RAIL_ITEMS)
        self._content_layout.addWidget(self.rail)

        self.stack = SlidingStackedWidget()
        self._content_layout.addWidget(self.stack, 1)

        self._screens = {
            "chat": ChatScreen(),
            "dm": DMScreen(),
            "files": FilesScreen(),
            "bots": BotsScreen(),
            "profile": ProfileSettingsScreen(),
            "settings": SettingsScreen(),
        }
        for screen in self._screens.values():
            self.stack.addWidget(screen)
        self.stack.setCurrentWidget(self._screens["chat"])

        self.rail.item_selected.connect(self._on_nav)
        self._screens["settings"].connected_changed.connect(self._on_connected_changed)
        self._screens["settings"].encryption_settings_changed.connect(
            self._on_encryption_settings_changed
        )

        # Bots screen → открытие игры из карточки бота
        self._screens["bots"].open_game_requested.connect(self._on_open_game_from_bots)

        # -- Command Palette (Ctrl+K) --
        self.palette = CommandPalette(self)
        self._register_static_commands()
        self._palette_shortcut = QShortcut("Ctrl+K", self)
        self._palette_shortcut.activated.connect(self._toggle_palette)

        # -- Шорткаты Ctrl+1..9 для пунктов Rail --
        for i, (key, _icon, _label) in enumerate(RAIL_ITEMS, start=1):
            QShortcut(f"Ctrl+{i}", self).activated.connect(
                lambda checked=False, k=key: self._navigate(k)
            )

        # Кромки-грипсы для frameless-ресайза вне Windows (на Windows
        # ресайз делает WM_NCHITTEST — нативно, с Aero Snap)
        if self._frameless and QGuiApplication.platformName() != "windows":
            self._grips = EdgeResizeGrips(self)

        # Авто-проверка обновлений при запуске (если включена в настройках).
        # Делаем через 2 секунды после старта, чтобы окно успело нарисоваться
        # и пользователь не смотрел на «проверяю…» пока грузится UI.
        self._auto_update_check_done = False
        QTimer.singleShot(2000, self._maybe_check_updates_on_startup)

    # ------------------------------------------------------------------
    # Окно: frameless-поведение
    # ------------------------------------------------------------------
    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._frameless and not self._shadow_applied:
            self._shadow_applied = True
            apply_dwm_shadow(self)
            if self._grips is not None:
                self._grips.reposition()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._grips is not None:
            self._grips.reposition()
        self._update_responsive_rail()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self._titlebar is not None:
                self._titlebar.update_max_icon()
            if self._grips is not None:
                self._grips.reposition()

    def nativeEvent(self, eventType, message):
        """WM_NCHITTEST на Windows: нативные drag/resize/Snap Layouts."""
        if self._frameless and eventType == b"windows_generic_MSG":
            try:
                code = WindowsHitTester.handle(self, self._titlebar, int(message))
                if code is not None:
                    return True, code
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    # ------------------------------------------------------------------
    # Responsive: <800px — рейл вниз, как tab bar
    # ------------------------------------------------------------------
    def _update_responsive_rail(self) -> None:
        want_bottom = self.width() < RAIL_BOTTOM_BREAKPOINT
        if want_bottom == self.rail.is_horizontal():
            return
        if want_bottom:
            self._content_layout.removeWidget(self.rail)
            self.rail.set_orientation(horizontal=True)
            self._root_layout.addWidget(self.rail)
        else:
            self._root_layout.removeWidget(self.rail)
            self.rail.set_orientation(horizontal=False)
            self._content_layout.insertWidget(0, self.rail)
        self.rail.setVisible(True)

    # ------------------------------------------------------------------
    # Command Palette
    # ------------------------------------------------------------------
    def _toggle_palette(self) -> None:
        # Каналы меняются на лету — перерегистрируем при каждом открытии
        self._register_channel_commands()
        self.palette.toggle()

    def _register_static_commands(self) -> None:
        for i, (key, icon, label) in enumerate(RAIL_ITEMS, start=1):
            self.palette.register(
                title=label,
                callback=lambda k=key: self._navigate(k),
                icon=icon,
                category="Разделы",
                shortcut=f"Ctrl+{i}",
            )
        self.palette.register(
            title="Найти в ленте чата",
            callback=self._screens["chat"]._focus_search,
            icon="🔍",
            category="Действия",
            keywords="поиск search",
            shortcut="Ctrl+Shift+F",
        )
        self.palette.register(
            title="Создать канал",
            callback=self._screens["chat"]._on_create_channel,
            icon="➕",
            category="Действия",
            keywords="канал channel create",
        )
        self.palette.register(
            title="Голосовой канал",
            callback=self._screens["chat"]._toggle_voice_dialog,
            icon="🎙",
            category="Действия",
            keywords="voice голос связь",
        )

    def _register_channel_commands(self) -> None:
        chat = self._screens["chat"]
        names = chat.channel_names()
        # Снимаем прежние канал-команды (реестр общий, префикс в category)
        for name in [c.title for c in self.palette._commands if c.category == "Каналы"]:
            self.palette.unregister(name)
        self.palette.register(
            title="Общий чат",
            callback=lambda: chat.jump_to_channel(None),
            icon="💬",
            category="Каналы",
            keywords="general канал общий",
        )
        for name in names:
            self.palette.register(
                title=name,
                callback=lambda n=name: chat.jump_to_channel(n),
                icon="📢",
                category="Каналы",
                keywords="канал",
            )

    def _navigate(self, key: str) -> None:
        """Навигация из Rail / палитры / шорткатов — единая точка."""
        self.rail._on_clicked(key)

    def _on_nav(self, key: str) -> None:
        # все пункты Rail равноправны -> кроссфейд.
        # Drill-down (Chat List -> Chat Room) использует "slide",
        # модалки/оверлеи — "push".
        self.stack.go_to(self._screens[key], transition="crossfade")

    # ------------------------------------------------------------------
    # Подключение / шифрование / боты (публичный API)
    # ------------------------------------------------------------------
    def _on_connected_changed(self, connected: bool) -> None:
        settings_screen = self._screens["settings"]
        self.set_connection(
            settings_screen.base_url if connected else None,
            settings_screen.my_name,
            settings_screen.access_key,
            connected,
        )
        self.set_bot_manager(settings_screen.get_bot_manager() if connected else None)

        # Передаём настройки шифрования + per-installation соль
        encryption_enabled, secret_key = settings_screen.get_encryption_settings()
        salt = settings_screen.settings.get("access_key_salt", "")
        self.set_encryption_settings(encryption_enabled, secret_key, salt)

        if connected:
            # успешное подключение -> сразу показываем чат, это первое, что
            # человек хочет увидеть, а не форму настроек
            self.rail._on_clicked("chat")

    def _on_encryption_settings_changed(self, enabled: bool, secret_key: str) -> None:
        """Обрабатывает изменение настроек шифрования.

        Соль берём из settings (она не меняется при редактировании ключа).
        """
        settings_screen = self._screens["settings"]
        salt = settings_screen.settings.get("access_key_salt", "")
        self.set_encryption_settings(enabled, secret_key, salt)

    # -- публичный API для подключения к реальной сети/хосту --
    def set_connection(
        self, base_url: str | None, my_name: str, access_key: str, connected: bool
    ) -> None:
        self._screens["files"].set_connection(base_url, my_name, access_key, connected)
        self._screens["dm"].set_connection(base_url or "", my_name, access_key, connected)
        self._screens["chat"].set_connection(base_url or "", my_name, access_key, connected)
        self._screens["profile"].set_connection(base_url or "", my_name, access_key, connected)
        self._screens["bots"].set_connection(base_url or "", my_name, access_key, connected)

    def _on_open_game_from_bots(self, bot_id: str) -> None:
        """Открывает игру из карточки бота на экране «Боты» — проксирует
        вызов в ChatScreen, который умеет подбирать нужный диалог по bot_id."""
        chat = self._screens["chat"]
        if not chat._base_url or not chat._my_name:
            QMessageBox.warning(self, "Боты", "Сначала подключись к серверу")
            return
        # Берём мета-инфу от бота и открываем диалог через тот же метод
        # что используется при !-команде из чата.
        bot_manager = self._screens["bots"]._bot_manager
        if bot_manager is None:
            return
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return
        meta = bot.get_meta() if hasattr(bot, "get_meta") else {}
        chat._open_game_dialog(bot_id, meta, "play")

    def _maybe_check_updates_on_startup(self) -> None:
        """Тихая авто-проверка обновлений при запуске.

        Срабатывает только если:
          - check_updates = True в настройках (по умолчанию включено)
          - задан URL манифеста
        Если есть обновление — показывает QMessageBox с кнопкой «Скачать».
        Если обновления нет — ничего не показывает (тихо).
        """
        if self._auto_update_check_done:
            return
        self._auto_update_check_done = True

        settings_screen = self._screens["settings"]
        settings = settings_screen.settings
        if not settings.get("check_updates", True):
            return
        manifest_url = settings.get("update_manifest_url", "").strip()
        if not manifest_url:
            return  # Не настроено — молча

        # Запускаем тихую проверку через тот же bridge, что и кнопка
        from lib.updater import check_for_updates

        def do_check():
            return check_for_updates(manifest_url)

        def on_ok(info):
            if info is None:
                return  # Тишина — у пользователя последняя версия
            # Есть обновление — показываем диалог
            settings_screen._pending_update = info
            msg = f"🌟 Доступна новая версия {info.version}!\n\n"
            if info.notes:
                # Только первые 300 символов — иначе диалог становится огромным
                notes = info.notes[:300]
                if len(info.notes) > 300:
                    notes += "…"
                msg += f"Что нового:\n{notes}\n\n"
            msg += "Скачать и установить сейчас?\n\n(Можно потом — в Настройках)"

            reply = QMessageBox.question(
                self,
                "Доступно обновление",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,  # Default — No, не навязчиво
            )
            if reply == QMessageBox.StandardButton.Yes:
                settings_screen._show_update_dialog(info)

        # Ошибки сети глушим — это фоновая проверка
        settings_screen._bridge.run(do_check, on_success=on_ok, on_error=lambda e: None)

    def set_bot_manager(self, bot_manager) -> None:
        self._screens["bots"].set_bot_manager(bot_manager)

    def set_encryption_settings(self, enabled: bool, secret_key: str,
                                 salt: str = "") -> None:
        """Передаёт настройки шифрования на экраны чата и ЛС."""
        self._screens["chat"].set_encryption_settings(enabled, secret_key, salt)
        self._screens["dm"].set_encryption_settings(enabled, secret_key, salt)

    def closeEvent(self, event) -> None:
        settings_screen = self._screens["settings"]
        if settings_screen.relay is not None:
            settings_screen.relay.stop()
        super().closeEvent(event)
