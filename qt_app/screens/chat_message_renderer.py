"""
Фабрика рендеринга сообщений — вынесена из ChatScreen для разгрузки.

Раньше ChatScreen._render_message() был 120-строчным методом внутри
god-object'а на 1900+ строк. Теперь вынесен сюда как чистая функция,
которая принимает chat_screen (для доступа к кэшу аватаров, callback'ам)
и событие — и возвращает готовый msg_container с пузырём внутри.

Это позволяет:
  - ChatScreen остаётся тонким координатором (обработчики сигналов)
  - Логику рендеринга можно тестировать отдельно
  - При добавлении нового типа сообщения (например, системное уведомление)
    меняется только тут
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from lib.formatters import fmt_time
from qt_app.theme import PALETTE
from qt_app.widgets.avatar_widget import AvatarWidget
from qt_app.widgets.message_bubble import MessageBubble


def build_message_container(
    chat_screen,
    ev: dict,
    my_name: str,
    base_url: str,
    access_key: str,
    avatar_cache,
    is_me: bool,
    grouped: bool,
) -> tuple[QWidget, MessageBubble]:
    """Создаёт контейнер с аватаркой + именем + временем + пузырём.

    Единая точка рендеринга для общего чата И каналов (раньше каналы
    дублировали эту логику ~90 строками в ChatScreen и не подключали
    сигналы бабла — из-за этого в каналах не работали реакции/ответ/правка).

    Args:
        chat_screen: экземпляр ChatScreen (для подключения сигналов bubble к его handlers)
        ev: событие (text или file)
        my_name: моё имя (для is_me и markdown)
        base_url, access_key: для загрузки аватаров и файлов
        avatar_cache: AvatarCache (для переиспользования)
        is_me: True если моё сообщение
        grouped: True если в группе с предыдущим от того же отправителя

    Returns:
        (container_widget, bubble) — контейнер уже готов вставить в feed_layout
    """
    sender = ev.get("from", "?")

    # Контейнер для сообщения
    msg_container = QWidget()
    msg_layout = QHBoxLayout(msg_container)
    msg_layout.setContentsMargins(0, 4, 0, 4)
    msg_layout.setSpacing(8)

    if not is_me:
        # Левая колонка для входящих
        if not grouped:
            # Аватарка + имя + время (только для первого сообщения в группе)
            header = _build_incoming_header(
                sender, ev, base_url, access_key, avatar_cache
            )
            msg_layout.addWidget(header)
        else:
            # Отступ для сгруппированных сообщений (под аватаркой предыдущего)
            spacer = QWidget()
            spacer.setFixedSize(28, 1)
            msg_layout.addWidget(spacer)

        # Пузырь с действиями
        bubble = _create_bubble(chat_screen, ev, is_me, my_name, base_url, access_key)
        msg_layout.addWidget(bubble, 1)
        msg_layout.addStretch()
    else:
        # Правая колонка для исходящих
        msg_layout.addStretch()

        if not grouped and ev.get("ts"):
            time_str = fmt_time(ev.get("ts", 0))
            time_label = QLabel(time_str)
            time_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px;"
            )
            time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            msg_layout.addWidget(time_label)

        bubble = _create_bubble(chat_screen, ev, is_me, my_name, base_url, access_key)
        msg_layout.addWidget(bubble)

    return msg_container, bubble


def _build_incoming_header(
    sender: str,
    ev: dict,
    base_url: str,
    access_key: str,
    avatar_cache,
) -> QWidget:
    """Создаёт блок аватарка + имя + время для входящего сообщения."""
    header = QWidget()
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(0, 0, 0, 0)
    header_layout.setSpacing(2)

    # Аватарка
    avatar = AvatarWidget(sender, size=24)
    cached = avatar_cache.get(sender)
    if cached:
        avatar.set_avatar_data(cached)
    else:
        avatar.load_avatar(base_url, access_key)

        def cache_avatar(data, username=sender):
            avatar_cache.set(username, data)

        avatar.avatar_loaded.connect(cache_avatar)

    header_layout.addWidget(avatar)

    # Имя + время
    meta = QWidget()
    meta_layout = QVBoxLayout(meta)
    meta_layout.setContentsMargins(0, 0, 0, 0)
    meta_layout.setSpacing(0)

    name_label = QLabel(sender)
    name_label.setStyleSheet(
        f"color: {PALETTE.text_primary}; font-weight: 600; font-size: 12px;"
    )
    meta_layout.addWidget(name_label)

    if ev.get("ts"):
        time_str = fmt_time(ev.get("ts", 0))
        time_label = QLabel(time_str)
        time_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        meta_layout.addWidget(time_label)

    header_layout.addWidget(meta)
    return header


def _create_bubble(
    chat_screen,
    ev: dict,
    is_me: bool,
    my_name: str,
    base_url: str,
    access_key: str,
) -> MessageBubble:
    """Создаёт MessageBubble и подключает его сигналы к handlers chat_screen'а."""
    decrypted_text = chat_screen._decrypt_message_text(ev)
    # v1.9.5: маркер правки — web показывает «(изменено)» давно, поле ev.edited
    # приходит всегда, но QT его выбрасывал (паритет фич).
    if ev.get("edited") and ev.get("kind") == "text":
        decrypted_text = f"{decrypted_text}  (изменено)"
    bubble = MessageBubble(
        decrypted_text, is_me, ev, base_url, access_key, my_name=my_name
    )
    # Подключаем все сигналы пузыря к обработчикам ChatScreen
    bubble.reply_requested.connect(chat_screen._on_reply_requested)
    bubble.edit_requested.connect(chat_screen._on_edit_requested)
    bubble.delete_requested.connect(chat_screen._on_delete_requested)
    bubble.copy_requested.connect(chat_screen._on_copy_requested)
    bubble.reaction_requested.connect(chat_screen._on_reaction_requested)
    bubble.pin_requested.connect(chat_screen._on_pin_requested)
    bubble.download_requested.connect(chat_screen._on_download_requested)
    # v3: клик по in-app ссылке (friendrelay://chess/join/CODE) —
    # ChatScreen открывает диалог шахмат и присоединяется к матчу.
    bubble.link_activated.connect(chat_screen._on_link_activated)
    # Поздний контент (превью догрузилось и бабл вырос) — экран подтянет
    # скролл, если пользователь у низа ленты.
    bubble.content_grew.connect(chat_screen._on_bubble_content_grew)
    return bubble
