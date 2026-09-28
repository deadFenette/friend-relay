"""Диалог «Закреплённые сообщения» (вынесен из chat_screen.py в v1.9.9).

Чистая вьюха над состоянием экрана: список закрепов с кнопками «Показать»
(скролл к баблу, если оно сейчас в ленте) и «Открепить». Логика поиска
виджета/сетевого вызова остаётся на экране — здесь только сборка диалога.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.formatters import fmt_time
from qt_app.theme import PALETTE, RADIUS


def show_pinned_dialog(screen) -> None:
    """Показывает список всех закреплённых сообщений с возможностью
    перейти к сообщению (если оно сейчас загружено в ленте) или открепить."""
    dialog = QDialog(screen)
    dialog.setWindowTitle("Закреплённые сообщения")
    dialog.setMinimumWidth(380)
    dialog.setStyleSheet(f"background: {PALETTE.surface_1};")

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("border: none; background: transparent;")
    list_widget = QWidget()
    list_layout = QVBoxLayout(list_widget)
    list_layout.setSpacing(6)

    if not screen._pinned_messages:
        empty = QLabel("Нет закреплённых сообщений")
        empty.setStyleSheet(f"color: {PALETTE.text_secondary}; font-size: 12px;")
        list_layout.addWidget(empty)
    else:
        for ev in screen._pinned_messages:
            row = QWidget()
            row.setStyleSheet(f"""
                background: {PALETTE.surface_2};
                border-radius: {RADIUS}px;
            """)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(2)

            sender = ev.get("from", "?")
            # Превью в панеле пинов — по расшифрованному тексту
            text = screen._decrypt_message_text(ev)
            snippet = text[:120] + "…" if len(text) > 120 else text
            time_str = fmt_time(ev.get("ts", 0)) if ev.get("ts") else ""

            header_row = QHBoxLayout()
            meta_label = QLabel(f"{sender} · {time_str}")
            meta_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px;")
            header_row.addWidget(meta_label, 1)

            seq = ev.get("seq")

            goto_btn = QPushButton("Показать")
            goto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            goto_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {PALETTE.accent};
                    border: none; font-size: 12px; padding: 2px 6px; }}
                QPushButton:hover {{ text-decoration: underline; }}
            """)
            goto_btn.clicked.connect(
                lambda checked=False, s=seq, d=dialog: _goto_pinned(screen, s, d)
            )
            header_row.addWidget(goto_btn)

            unpin_btn = QPushButton("Открепить")
            unpin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            unpin_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {PALETTE.text_secondary};
                    border: none; font-size: 12px; padding: 2px 6px; }}
                QPushButton:hover {{ color: {PALETTE.text_primary}; text-decoration: underline; }}
            """)
            unpin_btn.clicked.connect(
                lambda checked=False, s=seq, d=dialog: _unpin_from_dialog(screen, s, d)
            )
            header_row.addWidget(unpin_btn)

            row_layout.addLayout(header_row)

            text_label = QLabel(snippet)
            text_label.setWordWrap(True)
            text_label.setStyleSheet(
                f"color: {PALETTE.text_primary}; font-size: 12px;")
            row_layout.addWidget(text_label)

            list_layout.addWidget(row)

    list_layout.addStretch(1)
    scroll.setWidget(list_widget)
    layout.addWidget(scroll)

    dialog.exec()


def _goto_pinned(screen, seq: int | None, dialog: QDialog) -> None:
    """Скроллит к сообщению, если оно сейчас есть в загруженной ленте
    (лента держит только последние ~50) - иначе просто сообщаем об этом,
    полноценная подгрузка старой истории к произвольному seq - отдельная
    задача (сейчас `before` в API это уже поддерживает, но здесь не
    подключено)."""
    widget = screen._message_widgets.get(seq) if seq is not None else None
    if widget is None:
        screen._set_status_text("Это сообщение сейчас не в видимой части ленты")
        dialog.accept()
        return
    dialog.accept()
    screen._scroll.ensureWidgetVisible(widget, ymargin=40)


def _unpin_from_dialog(screen, seq: int | None, dialog: QDialog) -> None:
    if seq is None or not screen._connected:
        return
    dialog.accept()
    screen._bridge.run(
        lambda: client.pin_message(
            screen._base_url, screen._my_name, seq, screen._access_key),
        on_success=lambda result: screen._after_message_action(),
        on_error=screen._on_message_error,
    )
