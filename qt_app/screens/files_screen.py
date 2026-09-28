"""
Экран «Файлы» — аналог `_build_files_tab` из lib/tabs_ui.py.

Отличия от tkinter-версии:
- сетка через QGridLayout вместо Canvas+Frame+scrollregion-хаков;
- сеть и превью — через AsyncBridge (Qt-сигналы), а не ручную UI-очередь;
- сама эта версия ПОДКЛЮЧАЕТСЯ к настоящему lib/client.py, никаких заглушек.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.client_core.social import (
    can_delete_file,
    default_download_path,
    empty_files_text,
    files_count_text,
    filter_files,
)
from lib.constants import FILE_PREVIEW_MAX_BYTES
from lib.formatters import download_progress_text, upload_progress_text
from qt_app.async_bridge import AsyncBridge
from qt_app.preview import make_preview_pixmap
from qt_app.theme import PALETTE, RADIUS
from qt_app.widgets.drop_scroll_area import DropScrollArea
from qt_app.widgets.file_card import FileCard

FILTERS = [
    ("all", "📦 Все"),
    ("image", "🖼 Картинки"),
    ("archive", "🗜 Архивы"),
    ("mod", "🎮 Моды"),
]

COLUMNS = 3


class _ProgressSignals(QObject):
    """Мост прогресса из рабочего потока в GUI-поток.

    progress_cb из lib/client.download_file / send_file дёргается из РАБОЧЕГО
    потока — трогать QLabel оттуда нельзя. Сигнал, испущенный из чужого потока,
    Qt сам доставляет в поток виджета-получателя (queued connection) — тот же
    механизм, что у AsyncBridge, только с тремя аргументами."""

    progress = Signal(int, object, float)  # received, total|None, elapsed


class FilesScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = AsyncBridge()
        self._files: list[dict] = []
        self._filter = "all"
        self._filter_buttons: dict[str, QPushButton] = {}
        self._preview_inflight: set[str] = set()

        # -- контекст подключения (проставляется из MainWindow / SessionState) --
        self.base_url: str | None = None
        self.my_name: str = ""
        self.access_key: str = ""
        self.connected: bool = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        # -- header --
        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("📁 Файловая витрина")
        title.setStyleSheet(f"color: {PALETTE.text_primary}; font-size: 18px; font-weight: 700;")
        title_col.addWidget(title)
        self._count_label = QLabel("0 файлов")
        self._count_label.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
        title_col.addWidget(self._count_label)
        header.addLayout(title_col)
        header.addStretch(1)

        refresh_btn = QPushButton("🔄 Обновить")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._pill_qss(active=False))
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)

        share_btn = QPushButton("📤 Поделиться файлом")
        share_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        share_btn.setStyleSheet(self._pill_qss(active=False))
        share_btn.clicked.connect(self._on_share_file)
        header.addWidget(share_btn)

        root.addLayout(header)

        # -- filters --
        filters_row = QHBoxLayout()
        filters_row.setSpacing(8)
        for fid, label in FILTERS:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, f=fid: self._set_filter(f))
            filters_row.addWidget(btn)
            self._filter_buttons[fid] = btn
        filters_row.addStretch(1)
        root.addLayout(filters_row)
        self._restyle_filters()

        # -- scrollable grid --
        self._scroll = DropScrollArea(
            drop_text="Отпустите файл здесь, чтобы поделиться им со всеми"
        )
        self._scroll.setWidgetResizable(True)
        self._scroll.file_dropped.connect(self._upload_file)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(14)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._scroll.setWidget(self._grid_host)
        root.addWidget(self._scroll, 1)

        self._show_empty_state("Пока нет файлов\nОтправь что-нибудь в чат — появится здесь")

    # -- публичный API для MainWindow --
    def set_connection(
        self, base_url: str | None, my_name: str, access_key: str, connected: bool
    ) -> None:
        self.base_url = base_url
        self.my_name = my_name
        self.access_key = access_key
        self.connected = connected
        if connected:
            self.refresh()
        else:
            self._apply_files_list([])
            self._count_label.setText("Не подключено — файлы недоступны")

    def refresh(self) -> None:
        if not self.connected or not self.base_url:
            return
        self._bridge.run(
            lambda: client.list_files(self.base_url, self.my_name, self.access_key),
            on_success=self._apply_files_list,
            on_error=lambda e: self._count_label.setText(f"Ошибка: {e}"),
        )

    # -- внутреннее --
    def _pill_qss(self, active: bool) -> str:
        bg = PALETTE.accent if active else PALETTE.surface_3
        fg = PALETTE.text_on_accent if active else PALETTE.text_secondary
        hover = PALETTE.accent_hover if active else PALETTE.surface_2
        return f"""
            QPushButton {{
                background: {bg}; color: {fg}; border: none;
                border-radius: {RADIUS}px; padding: 6px 14px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {hover}; }}
        """

    def _restyle_filters(self) -> None:
        for fid, btn in self._filter_buttons.items():
            btn.setStyleSheet(self._pill_qss(active=(fid == self._filter)))

    def _set_filter(self, fid: str) -> None:
        if fid == self._filter:
            return
        self._filter = fid
        self._restyle_filters()
        self._apply_files_list(self._files)

    def _show_empty_state(self, text: str) -> None:
        # создаём лейбл заново на каждый показ, а не переиспользуем один
        # инстанс через все циклы очистки грида — переиспользование ловится
        # тестом как RuntimeError: C++ object already deleted (deleteLater()
        # в _apply_files_list рано или поздно убивает старый инстанс, а
        # Python-ссылка на него остаётся висеть)
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {PALETTE.text_secondary};")
        self._grid.addWidget(label, 0, 0, 1, COLUMNS)

    def _apply_files_list(self, files: list[dict]) -> None:
        self._files = files

        # чистим сетку
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._preview_inflight.clear()

        filtered = filter_files(files, self._filter)

        total = len(files)
        shown = len(filtered)
        self._count_label.setText(files_count_text(total, shown))

        if not filtered:
            self._show_empty_state(empty_files_text(self._filter))
            return

        for i, ev in enumerate(filtered):
            row, col = divmod(i, COLUMNS)
            can_delete = can_delete_file(ev, self.my_name, self.connected)
            card = FileCard(ev, can_delete=can_delete)
            card.download_clicked.connect(self._on_download_clicked)
            card.delete_clicked.connect(self._on_delete_clicked)
            # без alignment=AlignTop QGridLayout растягивает карточку на всю
            # высоту ряда (сама поймала на скриншоте — карточка "размазывалась"
            # на весь экран вместо компактной высоты по контенту)
            self._grid.addWidget(
                card, row, col, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            if card.is_previewable(FILE_PREVIEW_MAX_BYTES):
                self._fetch_preview(card)

    def _fetch_preview(self, card: FileCard) -> None:
        if not self.connected or not self.base_url or card.file_id in self._preview_inflight:
            return
        self._preview_inflight.add(card.file_id)

        def do_fetch():
            return client.fetch_file_bytes(
                self.base_url,
                card.file_id,
                self.access_key,
                max_bytes=FILE_PREVIEW_MAX_BYTES,
            )

        def on_ok(data: bytes):
            self._preview_inflight.discard(card.file_id)
            # v1.9.5: карточку могли уже удалить (кнопка «Обновить»/смена
            # фильтра сносит сетку deleteLater) — обращение к мёртвому
            # C++ объекту давало RuntimeError в консоль.
            try:
                pixmap = make_preview_pixmap(data, filename=card.name)
                card.set_preview_pixmap(pixmap)
            except RuntimeError:
                return  # карточка уже уничтожена — превью некуда ставить

        def on_err(_e):
            self._preview_inflight.discard(card.file_id)

        self._bridge.run(do_fetch, on_success=on_ok, on_error=on_err)

    def _on_download_clicked(self, file_id: str) -> None:
        ev = next((f for f in self._files if f.get("file_id") == file_id), None)
        if ev is None or not self.connected or not self.base_url:
            return
        name = ev.get("name", "файл")
        dest = default_download_path(name)

        # Сигнал-мост: progress_cb дёргается из рабочего потока bridge, а
        # обновлять _count_label можно только в GUI-потоке. Родитель = self,
        # чтобы объект жил, пока идёт скачивание.
        signals = _ProgressSignals(self)
        signals.progress.connect(
            lambda r, t, e: self._count_label.setText(download_progress_text(r, t, e))
        )

        def do_download():
            # name= -> после успеха клиент сообщит хосту "у меня есть файл"
            # (/file/have, реестр владельцев для multi-source раздачи)
            client.download_file(
                self.base_url, file_id, dest, self.access_key,
                progress_cb=signals.progress.emit, name=self.my_name,
            )
            return str(dest)

        self._bridge.run(
            do_download,
            on_success=lambda path: self._count_label.setText(f"✅ Скачано: {path}"),
            on_error=lambda e: self._count_label.setText(f"❌ Ошибка скачивания: {e}"),
        )

    def _on_delete_clicked(self, seq: int) -> None:
        """Удаляет свой файл с общей витрины (сервер также подчищает байты
        с диска - см. RelayServer._apply_deletions)."""
        if not self.connected or not self.base_url:
            return
        self._bridge.run(
            lambda: client.delete_event(self.base_url, self.my_name, seq, self.access_key),
            on_success=lambda _seq: self._on_file_deleted(),
            on_error=lambda e: self._count_label.setText(f"❌ Ошибка удаления: {e}"),
        )

    def _on_file_deleted(self) -> None:
        self._count_label.setText("✅ Файл удалён")
        self.refresh()

    def _on_share_file(self) -> None:
        """Открывает диалог выбора файла для раздачи."""
        if not self.connected or not self.base_url:
            self._count_label.setText("❌ Сначала подключись к серверу")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выбери файл для раздачи", str(Path.home()), "Все файлы (*)"
        )

        if not file_path:
            return

        self._upload_file(Path(file_path))

    def _upload_file(self, path: Path) -> None:
        """Заливает файл в общую витрину - общий путь и для кнопки "Поделиться
        файлом", и для drag-and-drop (см. DropScrollArea.file_dropped выше)."""
        if not self.connected or not self.base_url:
            self._count_label.setText("❌ Сначала подключись к серверу")
            return
        if not path.is_file():
            self._count_label.setText("❌ Не выбран файл")
            return

        # Скорость заливки — тот же сигнал-мост, что и у скачивания.
        up_signals = _ProgressSignals(self)
        up_signals.progress.connect(
            lambda s, t, e: self._count_label.setText(upload_progress_text(s, t, e))
        )

        def progress_callback(sent: int, total: int, elapsed: float) -> None:
            up_signals.progress.emit(sent, total, elapsed)

        def do_upload():
            return client.send_file(
                self.base_url, self.my_name, path, self.access_key, progress_callback
            )

        self._bridge.run(
            do_upload,
            on_success=lambda result: self._on_file_shared(path.name),
            on_error=lambda e: self._count_label.setText(f"❌ Ошибка загрузки: {e}"),
        )

    def _on_file_shared(self, filename: str) -> None:
        """Обрабатывает успешную загрузку файла."""
        self._count_label.setText(f"✅ Файл '{filename}' загружен")
        self.refresh()  # Обновляем список файлов
