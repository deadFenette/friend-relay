"""
VoiceChannelDialog — окно подключения к голосовому каналу.

Простое окно: кнопка «Подключиться/Отключиться», mute-переключатель,
список участников (периодически опрашивается через /voice/participants).

Звуком управляет voice/client/engine.py:VoiceClient. UI только
переключает состояние и показывает участников.

v3.5.6: ТИХИЙ АВТО-РЕКОННЕКТ (зеркало веб-клиента): разрыв связи больше
не выкидывает в ручной режим — голос сам переподключается по лестнице
задержек (1/2/4/8/16/30с), заметный статус — только со 2-й попытки.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from lib import client
from lib.storage import load_settings, save_settings
from qt_app.theme import PALETTE, RADIUS, RADIUS_SM
from qt_app.widgets.level_meter import LevelMeter
from qt_app.widgets.online_user_chip import color_for_name, initials
from voice import VoiceClient, list_audio_devices
from voice.config import (
    QUALITY_LOSS_BAD_PCT,
    QUALITY_LOSS_GOOD_PCT,
    QUALITY_RTT_BAD_MS,
    QUALITY_RTT_GOOD_MS,
    VOICE_RECONNECT_LADDER_S,
    VOICE_RECONNECT_SOFT_FROM,
)
from voice.devices import HAS_AUDIO as _HAS_AUDIO

# ── QSS-хелперы для контролов настроек (v1.9.6) ─────────────────────


def _combo_qss() -> str:
    """Тёмный QComboBox — дефолтный popup на тёмном фоне белый-на-белом."""
    return f"""
        QComboBox {{
            background: {PALETTE.surface_3};
            border: 1px solid {PALETTE.border_soft};
            border-radius: 6px;
            padding: 5px 10px;
            color: {PALETTE.text_primary};
            min-height: 18px;
            font-size: 12px;
        }}
        QComboBox:focus {{ border: 1px solid {PALETTE.accent}; }}
        QComboBox::drop-down {{ border: none; width: 20px; }}
        QComboBox::down-arrow {{
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {PALETTE.text_secondary};
            margin-right: 6px;
        }}
        QComboBox QAbstractItemView {{
            background: {PALETTE.surface_2};
            color: {PALETTE.text_primary};
            border: 1px solid {PALETTE.border};
            border-radius: 6px;
            padding: 4px;
            outline: 0;
            selection-background-color: {PALETTE.accent};
            selection-color: {PALETTE.text_on_accent};
        }}
        QComboBox QAbstractItemView::item {{
            background: transparent; color: {PALETTE.text_primary};
            padding: 5px 10px; min-height: 20px; border-radius: 4px;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background: {PALETTE.surface_3};
        }}
    """


def _slider_qss() -> str:
    """Горизонтальный QSlider в стиле приложения."""
    return f"""
        QSlider::groove:horizontal {{
            height: 4px; background: {PALETTE.surface_3};
            border-radius: 2px;
        }}
        QSlider::sub-page:horizontal {{
            background: {PALETTE.accent}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {PALETTE.text_secondary}; width: 12px;
            height: 12px; margin: -5px 0; border-radius: 6px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {PALETTE.text_primary};
        }}
    """


def _checkbox_qss() -> str:
    """QCheckBox с читаемым индикатором."""
    return f"""
        QCheckBox {{
            color: {PALETTE.text_secondary}; font-size: 11px;
            spacing: 8px; background: transparent;
        }}
        QCheckBox:hover {{ color: {PALETTE.text_primary}; }}
        QCheckBox::indicator {{
            width: 15px; height: 15px;
            border: 1px solid {PALETTE.border};
            border-radius: 4px; background: {PALETTE.surface_3};
        }}
        QCheckBox::indicator:checked {{
            background: {PALETTE.accent}; border-color: {PALETTE.accent};
        }}
        QCheckBox::indicator:hover {{
            border-color: {PALETTE.border_strong};
        }}
    """


class _VoiceUserChip(QWidget):
    """Карточка участника голосового канала (как OnlineUserChip, но без онлайн-точки
    и с индикатором mute + зелёное кольцо «говорит» как в Discord)."""

    def __init__(self, name: str, is_me: bool = False, muted: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._is_me = is_me
        self._muted = muted
        self._speaking = False
        self._color = color_for_name(name)
        self._initials = initials(name)

        self.setFixedHeight(36)
        self.setMinimumWidth(140)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self.update()

    def set_speaking(self, speaking: bool) -> None:
        """Вкл/выкл зелёное кольцо вокруг чипа."""
        if speaking != self._speaking:
            self._speaking = speaking
            self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(0, 0, -1, -1)
        radius = 18

        # Фон карточки (у говорящего чуть ярче)
        bg_alpha = (26 if self._speaking else 18) if self._is_me else (
            20 if self._speaking else 10
        )
        bg = QColor(255, 255, 255, bg_alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QColor(255, 255, 255, 30 if self._is_me else 20))
        p.drawRoundedRect(rect, radius, radius)

        # Зелёное кольцо «говорит» — фидбек как в Discord
        if self._speaking:
            ring = QPen(QColor(PALETTE.success))
            ring.setWidth(2)
            p.setPen(ring)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # Цветной кружок с инициалами
        circle_r = 12.0
        cx = 18.0
        cy = rect.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(cx, cy), circle_r, circle_r)

        # Иконка mute (🔇) или говорящего (🔊)
        icon_x = cx + circle_r - 4
        icon_y = cy + circle_r - 4
        if self._muted:
            p.setBrush(QColor(PALETTE.danger))
            p.drawEllipse(QPointF(icon_x, icon_y), 7, 7)
            p.setPen(QColor("white"))
            font = p.font()
            font.setPixelSize(8)
            p.setFont(font)
            p.drawText(
                int(icon_x - 5), int(icon_y - 5), 10, 10,
                Qt.AlignmentFlag.AlignCenter, "🔇",
            )
        else:
            p.setBrush(QColor(PALETTE.success))
            p.drawEllipse(QPointF(icon_x, icon_y), 7, 7)

        # Инициалы
        p.setPen(QColor("white"))
        font = p.font()
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.drawText(
            int(cx - circle_r), int(cy - circle_r),
            int(circle_r * 2), int(circle_r * 2),
            Qt.AlignmentFlag.AlignCenter, self._initials,
        )

        # Имя
        text_x = int(cx + circle_r + 12)
        text_color = QColor(PALETTE.text_primary if self._is_me else PALETTE.text_secondary)
        p.setPen(text_color)
        font2 = p.font()
        font2.setPixelSize(13)
        font2.setBold(self._is_me)
        p.setFont(font2)
        display = self._name if len(self._name) <= 18 else self._name[:17] + "…"
        p.drawText(
            text_x, 0, self.width() - text_x - 8, self.height(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            display,
        )

        p.end()


class VoiceChannelDialog(QDialog):
    """Окно голосового канала. Само окно маленькое, всегда поверх чата.

    v1.9.6: встроенные настройки голоса — выбор микрофона/динамика,
    громкость и шумоподавление. Меняются на живом соединении
    (VoiceClient.restart_streams), сохраняются в settings.json и
    переиспользуются экраном профиля."""

    closed = Signal()
    # Мост из аудио/сетевых потоков в GUI-поток: Qt сам маршалит queued-вызовы
    speaking_sig = Signal(dict)
    local_speaking_sig = Signal(bool)
    # Ошибки из recv-потока тоже нужно маршалить в GUI-поток — раньше
    # on_error дёргался напрямую из фонового потока и трогал виджеты.
    voice_error_sig = Signal(str)
    # v3.5.6: результат фонового connect() (авто-реконнект) → GUI-поток.
    voice_connect_result_sig = Signal(bool, str)
    # v3.6.4: несовпадение версии приложения с хостом (spk_all «v»).
    version_warn_sig = Signal(str)

    def __init__(self, parent: QWidget | None, base_url: str, voice_host: str,
                 voice_port: int, player_name: str, access_key: str = ""):
        super().__init__(parent)
        self.setWindowTitle("🎙  Голосовой канал")
        self.setModal(False)
        # v1.9.6: выше из-за встроенной карточки настроек устройств
        self.setMinimumSize(380, 560)
        self.resize(380, 620)
        self.setStyleSheet(f"background: {PALETTE.surface_0};")

        self._base_url = base_url
        self._voice_host = voice_host
        self._voice_port = voice_port
        self._player_name = player_name
        self._access_key = access_key

        # Настройки голоса (devices/громкость/шумодав) — общие с профилем
        self.settings = load_settings()
        self._voice = VoiceClient(
            input_device=self.settings.get("voice_input_device", ""),
            output_device=self.settings.get("voice_output_device", ""),
            output_volume=float(self.settings.get("voice_output_volume", 1.0)),
            noise_suppression=bool(self.settings.get("voice_noise_suppression", True)),
        )
        self._voice.on_error = self._emit_voice_error
        self._connected = False
        # v3.5.6: состояние тихого авто-реконнекта (зеркало веб-клиента)
        self._voice_reconnect_attempts = 0
        self._voice_reconnect_timer: QTimer | None = None
        self._voice_manual_disconnect = False   # юзер сам кликнул «Отключиться»
        self._ever_voice_connected = False      # связь уже была рабочей
        self._voice_connecting = False          # фоновая попытка в полёте

        # Индикатор «говорит»: имя -> on. Кольцо у себя — из локального VAD
        # (мгновенно), у остальных — из серверных control-фреймов.
        self._speaking: dict[str, bool] = {}
        self._chips: dict[str, _VoiceUserChip] = {}
        self._rendered_participants: list[str] | None = None
        self._voice.on_speaking_change = lambda states: self.speaking_sig.emit(states)
        self._voice.on_local_speaking = lambda on: self.local_speaking_sig.emit(on)
        self.speaking_sig.connect(self._apply_speaking)
        self.local_speaking_sig.connect(self._apply_local_speaking)
        self.voice_error_sig.connect(self._on_voice_error)
        self.voice_connect_result_sig.connect(self._on_voice_connect_result)
        # v3.6.4: рукопожатие версий — предупреждение из recv-потока
        # маршалим через сигнал (UI трогаем только в GUI-потоке).
        self._voice.on_version_mismatch = self._emit_version_warn
        self.version_warn_sig.connect(self._apply_version_warn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Заголовок
        title = QLabel("🎙  ГОЛОСОВОЙ КАНАЛ")
        title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-family: 'Consolas, Menlo, monospace';"
            f"font-size: 16px; font-weight: bold; letter-spacing: 3px;"
        )
        layout.addWidget(title)

        # Статус подключения
        self._status_label = QLabel("Не подключено")
        self._status_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        layout.addWidget(self._status_label)

        # v3.5.7: КАЧЕСТВО СВЯЗИ (пинг/потери) — обновляется раз в секунду
        # движком (ping/pong + счётчики кадров микшера). Пусто/скрыто,
        # пока не подключены.
        self._quality_label = QLabel("")
        self._quality_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
            f"font-family: 'Consolas, monospace';"
        )
        self._quality_label.setToolTip(
            "Качество связи с голосовым мостом.\n"
            f"Пинг: до {QUALITY_RTT_GOOD_MS:.0f} мс — отлично, "
            f"выше {QUALITY_RTT_BAD_MS:.0f} мс — плохо.\n"
            "↑ — потери НАШЕГО голоса на пути к микшеру,\n"
            "↓ — потери микса на пути к нам.\n"
            f"Потери до {QUALITY_LOSS_GOOD_PCT:.0f}% — отлично, "
            f"выше {QUALITY_LOSS_BAD_PCT:.0f}% — слышны провалы.\n"
            "Высокие потери при игре музыки = канал забит / машина загружена."
        )
        self._quality_label.setVisible(False)
        layout.addWidget(self._quality_label)

        # v3.6.4: предупреждение о несовпадении версии приложения с хостом
        # (spk_all «v»). Разные версии = риск «бурундука»/тишины: показываем
        # красную строку, пока версии не совпадут.
        self._version_label = QLabel("")
        self._version_label.setWordWrap(True)
        self._version_label.setVisible(False)
        layout.addWidget(self._version_label)

        # Кнопка подключения
        self._connect_btn = QPushButton("📞  Подключиться")
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._connect_btn.setFixedHeight(40)
        self._connect_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.success};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: {RADIUS}px;
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: #2bbd80; }}
            QPushButton:pressed {{ background: #2aa070; }}
        """)
        self._connect_btn.clicked.connect(self._toggle_connection)
        layout.addWidget(self._connect_btn)

        # Mute кнопка (скрыта пока не подключён)
        self._mute_btn = QPushButton("🎤  Mute")
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.setFixedHeight(36)
        self._mute_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.glass_bg};
                color: {PALETTE.text_primary};
                border: 1px solid {PALETTE.border};
                border-radius: {RADIUS}px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {PALETTE.glass_bg_hover}; }}
        """)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._mute_btn.setVisible(False)
        layout.addWidget(self._mute_btn)

        # ── Настройки голоса (v1.9.6) ───────────────────────────
        layout.addWidget(self._build_voice_settings())

        # Участники
        participants_title = QLabel("👥  В канале")
        participants_title.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px; font-weight: 600;"
            f"letter-spacing: 1px; padding-top: 8px;"
        )
        layout.addWidget(participants_title)

        self._participants_layout = QVBoxLayout()
        self._participants_layout.setSpacing(6)
        self._participants_layout.addStretch(1)
        layout.addLayout(self._participants_layout, 1)

        # Ошибка если sounddevice не установлен
        if not _HAS_AUDIO:
            err = QLabel(
                "⚠  Библиотека sounddevice не установлена.\n\n"
                "Установи: pip install sounddevice"
            )
            err.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 11px; padding: 12px;"
                f"background: {PALETTE.surface_2}; border-radius: {RADIUS_SM}px;"
            )
            err.setWordWrap(True)
            layout.addWidget(err)
            self._connect_btn.setEnabled(False)

        # Таймер опроса участников (через HTTP, не через voice TCP)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_participants)
        self._poll_timer.start(2000)
        # Таймер уровня микрофона (метр обновляется в GUI-потоке, уровень
        # пишет аудио-колбэк из потока PortAudio — читаем атомарный float)
        self._meter_timer = QTimer(self)
        self._meter_timer.timeout.connect(self._update_mic_meter)
        self._meter_timer.start(80)
        # v3.5.7: таймер строки качества (пинг/потери) — раз в секунду
        self._quality_timer = QTimer(self)
        self._quality_timer.timeout.connect(self._update_quality)
        self._quality_timer.start(1000)
        # v1.9.5: in-flight guard поллинга (запрос ушёл в фон — не дублируем)
        self._poll_in_flight = False
        # v1.9.5: мост для сетевых запросов в фон (как на всех экранах)
        from qt_app.async_bridge import AsyncBridge

        self._bridge = AsyncBridge(self)

        # Первичная загрузка участников
        self._poll_participants()

    # ── Жизненный цикл ──────────────────────────────────────────────

    def _teardown(self) -> None:
        """Общая очистка (v1.9.5): вызывается из closeEvent И из reject —
        идемпотентна (guard). Раньше очистка жила только в closeEvent, а
        Esc у QDialog зовёт reject()→done()→hide() БЕЗ closeEvent: микрофон
        оставался захваченным, TCP-стрим шёл, таймер опрашивал участников,
        повторное открытие создавало ВТОРОЙ VoiceClient. Отдельно: reject()
        → close() на QDialog зацикливается (closeEvent→reject→close) и
        окно вообще не прячется — поэтому зовём super().reject()."""
        if getattr(self, "_teardown_done", False):
            return
        self._teardown_done = True
        # v3.5.6: гасим авто-реконнект ДО всего прочего — иначе запланированная
        # попытка могла выстрелить уже после закрытия окна.
        self._voice_manual_disconnect = True
        self._cancel_voice_reconnect()
        # disconnect() идемпотентен и None-безопасен: зовём ВСЕГДА, даже если
        # в этот момент в фоне идёт попытка реконнекта (поток увидит
        # _running=False и корректно остановится / результат погасит хендлер).
        self._voice.disconnect()
        self._connected = False
        self._poll_timer.stop()
        self.closed.emit()

    def reject(self) -> None:
        """Esc = тот же путь очистки, что и крестик окна (см. _teardown)."""
        self._teardown()
        super().reject()

    def closeEvent(self, event) -> None:
        self._teardown()
        super().closeEvent(event)

    # ── Настройки голоса (v1.9.6) ─────────────────────────────────

    def _build_voice_settings(self) -> QWidget:
        """Карточка настроек: микрофон, динамик, громкость, шумоподавление."""
        card = QWidget()
        card.setStyleSheet(f"""
            QWidget#VoiceSettingsCard {{
                background: {PALETTE.surface_1};
                border: 1px solid {PALETTE.border_soft};
                border-radius: {RADIUS_SM + 2}px;
            }}
        """)
        card.setObjectName("VoiceSettingsCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        header_row = QHBoxLayout()
        header = QLabel("⚙  Устройства")
        header.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 11px;"
            f"font-weight: 600; letter-spacing: 1px;"
        )
        header_row.addWidget(header)
        header_row.addStretch(1)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Пересканировать аудиоустройства (воткнул наушники?)")
        refresh_btn.setFixedSize(26, 22)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.glass_bg}; color: {PALETTE.text_secondary};
                border: 1px solid {PALETTE.border}; border-radius: 6px;
                font-size: 12px;
            }}
            QPushButton:hover {{ background: {PALETTE.glass_bg_hover};
                color: {PALETTE.text_primary}; }}
        """)
        refresh_btn.clicked.connect(lambda: self._fill_device_combos())
        header_row.addWidget(refresh_btn)
        lay.addLayout(header_row)

        # Микрофон
        mic_lbl = QLabel("🎤  Микрофон")
        mic_lbl.setStyleSheet(f"color: {PALETTE.text_dim}; font-size: 10px;")
        lay.addWidget(mic_lbl)
        self._mic_combo = QComboBox()
        self._mic_combo.setToolTip("Устройство записи (микрофон)")
        self._mic_combo.setStyleSheet(_combo_qss())
        self._mic_combo.activated.connect(self._on_device_changed)
        lay.addWidget(self._mic_combo)

        # Динамик
        spk_lbl = QLabel("🔊  Динамик")
        spk_lbl.setStyleSheet(f"color: {PALETTE.text_dim}; font-size: 10px;")
        lay.addWidget(spk_lbl)
        self._spk_combo = QComboBox()
        self._spk_combo.setToolTip("Устройство воспроизведения (наушники/динамики)")
        self._spk_combo.setStyleSheet(_combo_qss())
        self._spk_combo.activated.connect(self._on_device_changed)
        lay.addWidget(self._spk_combo)

        # Громкость + значение рядом
        vol_row = QHBoxLayout()
        vol_lbl = QLabel("📈  Громкость")
        vol_lbl.setStyleSheet(f"color: {PALETTE.text_dim}; font-size: 10px;")
        vol_row.addWidget(vol_lbl)
        self._vol_value = QLabel("100%")
        self._vol_value.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 10px;"
            f"font-family: 'Consolas, monospace';"
        )
        vol_row.addStretch(1)
        vol_row.addWidget(self._vol_value)
        lay.addLayout(vol_row)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 200)
        self._vol_slider.setValue(int(float(self.settings.get("voice_output_volume", 1.0)) * 100))
        self._vol_slider.setStyleSheet(_slider_qss())
        self._vol_slider.setToolTip("Громкость собеседников")
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        lay.addWidget(self._vol_slider)

        # Шумоподавление
        self._ns_check = QCheckBox("Шумоподавление микрофона")
        self._ns_check.setChecked(bool(self.settings.get("voice_noise_suppression", True)))
        self._ns_check.setToolTip(
            "Фильтр гула/шума и гейт тишины. Выключи, если USB-микрофон\n"
            "уже чистит звук своим DSP — двойная обработка портит голос."
        )
        self._ns_check.setStyleSheet(_checkbox_qss())
        self._ns_check.toggled.connect(self._on_ns_toggled)
        lay.addWidget(self._ns_check)

        # Метр уровня микрофона — живой отклик «тебя слышно»
        meter_row = QHBoxLayout()
        meter_lbl = QLabel("Уровень")
        meter_lbl.setStyleSheet(f"color: {PALETTE.text_dim}; font-size: 10px;")
        meter_row.addWidget(meter_lbl)
        self._meter_bar = LevelMeter()
        meter_row.addWidget(self._meter_bar, 1)
        lay.addLayout(meter_row)

        self._fill_device_combos()
        return card

    def _fill_device_combos(self) -> None:
        """Заполняет комбобоксы устройствами из PortAudio.

        «По умолчанию» (пустое имя) — системное устройство. Выбор
        сохраняется по ИМЕНИ (индексы PortAudio плывут между сессиями)."""
        devices = list_audio_devices()
        cur_in = self.settings.get("voice_input_device", "")
        cur_out = self.settings.get("voice_output_device", "")

        self._mic_combo.blockSignals(True)
        self._mic_combo.clear()
        self._mic_combo.addItem("🎤  По умолчанию (системное)", "")
        for d in devices["inputs"]:
            label = d["name"] if len(d["name"]) <= 34 else d["name"][:33] + "…"
            self._mic_combo.addItem("🎤  " + label, d["name"])
        idx = self._mic_combo.findData(cur_in)
        self._mic_combo.setCurrentIndex(max(0, idx))
        self._mic_combo.blockSignals(False)

        self._spk_combo.blockSignals(True)
        self._spk_combo.clear()
        self._spk_combo.addItem("🔊  По умолчанию (системное)", "")
        for d in devices["outputs"]:
            label = d["name"] if len(d["name"]) <= 34 else d["name"][:33] + "…"
            self._spk_combo.addItem("🔊  " + label, d["name"])
        idx = self._spk_combo.findData(cur_out)
        self._spk_combo.setCurrentIndex(max(0, idx))
        self._spk_combo.blockSignals(False)

    def _on_device_changed(self) -> None:
        """Смена микрофона/динамика: применяем К ЖИВОМУ соединению
        (короткая пауза вместо переподключения) и сохраняем в настройки."""
        in_dev = self._mic_combo.currentData() or ""
        out_dev = self._spk_combo.currentData() or ""
        self.settings["voice_input_device"] = in_dev
        self.settings["voice_output_device"] = out_dev
        save_settings(self.settings)
        self._voice.set_devices(in_dev, out_dev)
        if self._connected:
            ok = self._voice.restart_streams()
            if ok:
                self._status_label.setText("●  Устройство переключено")
                self._status_label.setStyleSheet(
                    f"color: {PALETTE.success}; font-size: 12px; font-weight: 600;"
                )
                # Вернём обычный статус через 1.5с, не мешая ошибкам
                QTimer.singleShot(1500, self._restore_status)

    def _restore_status(self) -> None:
        if self._connected:
            self._status_label.setText(
                f"●  В канале ({self._voice_host}:{self._voice_port})"
            )
            self._status_label.setStyleSheet(
                f"color: {PALETTE.success}; font-size: 12px; font-weight: 600;"
            )

    def _on_volume_changed(self, value: int) -> None:
        """Громкость применяется мгновенно (в аудио-колбэке) + в настройки."""
        vol = value / 100.0
        self._vol_value.setText(f"{value}%")
        self._voice.set_output_volume(vol)
        self.settings["voice_output_volume"] = vol
        save_settings(self.settings)

    def _on_ns_toggled(self, checked: bool) -> None:
        self._voice.set_noise_suppression(checked)
        self.settings["voice_noise_suppression"] = checked
        save_settings(self.settings)

    def _update_mic_meter(self) -> None:
        """Тянет уровень микрофона в метр (вызывается из GUI-таймера)."""
        if self._connected and self._voice:
            self._meter_bar.set_level(self._voice.get_input_level())
        else:
            self._meter_bar.set_level(0.0)

    def _emit_version_warn(self, my_v: str, host_v: str) -> None:
        """Колбэк движка (recv-поток): несовпадение версий → в GUI."""
        if my_v == "?" or my_v == host_v:
            self.version_warn_sig.emit("")
        else:
            self.version_warn_sig.emit(
                f"⚠ У хоста v{host_v}, у тебя v{my_v} — разные версии дают "
                f"«бурундука» или тишину. Обнови ОБЕ стороны")

    def _apply_version_warn(self, text: str) -> None:
        """Показ/скрытие предупреждения о версиях (GUI-поток)."""
        if text:
            self._version_label.setText(text)
            self._version_label.setStyleSheet(
                f"color: {PALETTE.danger}; font-size: 11px; "
                f"font-weight: 600;"
            )
            self._version_label.setVisible(True)
        else:
            self._version_label.setVisible(False)

    def _update_quality(self) -> None:
        """v3.5.7: строка «КАЧЕСТВО связи» — пинг и потери ↑↓ с цветной
        индикацией. Данные даёт движок (ping/pong раз в 2с, EMA);
        мы только рисуем. Пока первый pong не пришёл — «измеряю…»."""
        if not self._connected or not self._voice:
            self._quality_label.setVisible(False)
            return
        q = self._voice.get_quality()
        if not q.get("connected"):
            self._quality_label.setVisible(False)
            return
        verdict = q.get("verdict") or "ok"
        color = {
            "good": PALETTE.success,
            "ok": PALETTE.warning,
            "bad": PALETTE.danger,
        }.get(verdict, PALETTE.warning)
        if not q.get("rtt_ms") and q.get("uplink_pct", 0) == 0 \
                and q.get("downlink_pct", 0) == 0:
            text = "📶  качество связи: измеряю…"
        else:
            text = (f"📶  {q['rtt_ms']:.0f} мс   ·   "
                    f"потери ↑{q['uplink_pct']:.0f}% "
                    f"↓{q['downlink_pct']:.0f}%")
        # v3.6.4: микшер отвергает НАШИ кадры по размеру — у одной из
        # сторон старая версия (аудио-формат отличается). Без этого
        # подсказки человек видел просто «не слышно».
        if q.get("odd_out", 0) > 0:
            text += "  ·  ⚠ твои кадры не принимаются — обнови программу"
        self._quality_label.setText(text)
        self._quality_label.setStyleSheet(
            f"color: {color}; font-size: 11px;"
            f"font-family: 'Consolas, monospace'; font-weight: 600;"
        )
        self._quality_label.setVisible(True)

    # ── Подключение ─────────────────────────────────────────────────

    def _toggle_connection(self) -> None:
        if self._connected:
            self._do_disconnect(manual=True)   # v3.5.6: ручное отключение
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        # Голосовой хост: берём из base_url (только host, без http://)
        host = self._voice_host
        port = self._voice_port
        if not host or port == 0:
            self._on_voice_error("Голосовой сервер не найден — хост не запущен?")
            return
        # v3.5.6: ручное подключение отменяет всё, что планировал автореконнект
        self._voice_manual_disconnect = False
        self._cancel_voice_reconnect()
        if self._voice_connecting:
            return  # фоновая попытка ещё в полёте — ждём её результат

        ok = self._voice.connect(host, port, self._player_name, self._access_key)
        if not ok:
            return  # детали уже ушли в _on_voice_error от самого движка
        self._ever_voice_connected = True
        self._voice_reconnect_attempts = 0
        self._apply_connected_ui(host, port)

    def _apply_connected_ui(self, host: str, port: int) -> None:
        """UI-состояние «в канале» (общее для ручного подключения и
        успешного авто-реконнекта — v3.5.6)."""
        self._connected = True
        self._status_label.setText(f"●  В канале ({host}:{port})")
        self._status_label.setStyleSheet(
            f"color: {PALETTE.success}; font-size: 12px; font-weight: 600;"
        )
        self._connect_btn.setText("📵  Отключиться")
        self._connect_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.danger};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: {RADIUS}px;
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: #e0475d; }}
        """)
        self._mute_btn.setVisible(True)
        self._quality_label.setVisible(True)  # v3.5.7
        self._poll_participants()

    def _do_disconnect(self, manual: bool = False) -> None:
        """v3.5.6: manual=True — юзер кликнул «Отключиться» (гасим
        авто-реконнект); manual=False — сброс состояния после разрыва
        (решение о реконнекте принимает _on_voice_error)."""
        if manual:
            self._voice_manual_disconnect = True
            self._cancel_voice_reconnect()
        self._voice.disconnect()
        self._connected = False
        self._speaking.clear()
        self._status_label.setText("Не подключено")
        self._status_label.setStyleSheet(
            f"color: {PALETTE.text_secondary}; font-size: 12px;"
        )
        self._connect_btn.setText("📞  Подключиться")
        self._connect_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PALETTE.success};
                color: {PALETTE.text_on_accent};
                border: none;
                border-radius: {RADIUS}px;
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: #2bbd80; }}
        """)
        self._mute_btn.setVisible(False)
        self._quality_label.setVisible(False)  # v3.5.7
        self._poll_participants()

    def _toggle_mute(self) -> None:
        new_muted = not self._voice.is_muted()
        self._voice.set_muted(new_muted)
        if new_muted:
            self._mute_btn.setText("🔇  Unmute")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {PALETTE.danger};
                    color: {PALETTE.text_on_accent};
                    border: none;
                    border-radius: {RADIUS}px;
                    font-size: 13px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background: #e0475d; }}
            """)
        else:
            self._mute_btn.setText("🎤  Mute")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {PALETTE.glass_bg};
                    color: {PALETTE.text_primary};
                    border: 1px solid {PALETTE.border};
                    border-radius: {RADIUS}px;
                    font-size: 13px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background: {PALETTE.glass_bg_hover}; }}
            """)

    def _emit_voice_error(self, msg: str) -> None:
        """Вызывается из фоновых потоков VoiceClient — только сигнал."""
        self.voice_error_sig.emit(msg)

    def _on_voice_error(self, msg: str) -> None:
        """Приходит уже в GUI-потоке (через voice_error_sig)."""
        # Если соединение потеряно (recv-поток умер) — сбрасываем состояние
        # диалога и освобождаем аудио-устройства. Раньше после разрыва
        # (хост перезапустился/сеть мигнула) кнопка навсегда висела в
        # «Отключиться», а микрофон оставался захваченным до ручного клика.
        had_connection = self._connected and not self._voice.is_connected()
        if had_connection:
            self._do_disconnect(manual=False)
        # v3.5.6: связь УЖЕ была рабочей и разрыв не по вине пользователя —
        # тихий авто-реконнект (как в вебе): статус рисует планировщик.
        if had_connection and self._ever_voice_connected \
                and not self._voice_manual_disconnect:
            self._schedule_voice_reconnect()
            return
        self._status_label.setText(f"⚠  {msg}")
        self._status_label.setStyleSheet(
            f"color: {PALETTE.warning}; font-size: 12px;"
        )

    # ── Тихий авто-реконнект (v3.5.6, зеркало веб-клиента) ─────────

    def _cancel_voice_reconnect(self) -> None:
        """Гасит запланированную попытку и счётчик (ручные действия)."""
        if self._voice_reconnect_timer is not None:
            self._voice_reconnect_timer.stop()
            self._voice_reconnect_timer.deleteLater()
            self._voice_reconnect_timer = None
        self._voice_reconnect_attempts = 0

    def _schedule_voice_reconnect(self) -> None:
        """Планирует следующую попытку по лестнице 1/2/4/8/16/30с.
        Первая попытка обычно успевает до того, как глаза заметят
        пропажу звука, — заметный статус только со 2-й (SOFT_FROM).
        Идемпотентна: повторный вызов, пока попытка запланирована/идёт,
        ничего не делает (ошибки connect и результат попытки приходят
        двумя разными сигналами — без guard получилось бы двойное
        планирование)."""
        if self._voice_manual_disconnect or not self._ever_voice_connected:
            return
        if self._voice_reconnect_timer is not None or self._voice_connecting:
            return
        if self._connected:
            return  # связь уже восстановилась (например, вручную)
        self._voice_reconnect_attempts += 1
        a = self._voice_reconnect_attempts
        ladder = VOICE_RECONNECT_LADDER_S
        delay = ladder[min(a - 1, len(ladder) - 1)]
        if a >= VOICE_RECONNECT_SOFT_FROM:
            self._status_label.setText(
                f"⚠  связь рвётся — переподключаюсь сам (попытка {a})…")
            self._status_label.setStyleSheet(
                f"color: {PALETTE.warning}; font-size: 12px;"
            )
        else:
            self._status_label.setText("…связь дрогнула — переподключаюсь")
            self._status_label.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px;"
            )
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._voice_reconnect_fire)
        self._voice_reconnect_timer = timer
        timer.start(int(delay * 1000))

    def _voice_reconnect_fire(self) -> None:
        """Таймер попытки сработал: запускаем connect() в фоне — к мёртвому
        хосту connect висит до 5с (settimeout) и из GUI-потока морозил бы
        интерфейс на каждую попытку."""
        self._voice_reconnect_timer = None
        if self._voice_manual_disconnect or self._connected:
            return
        if self._voice_connecting:
            return
        host, port = self._voice_host, self._voice_port
        if not host or port == 0:
            return  # хост исчез — переподключаться некуда
        self._voice_connecting = True
        threading.Thread(target=self._reconnect_worker,
                         args=(host, port), daemon=True,
                         name="voice-reconnect").start()

    def _reconnect_worker(self, host: str, port: int) -> None:
        """connect() вне GUI-потока. Ошибки подключения движок сам
        публикует через on_error → voice_error_sig; сюда наружу идёт
        только итог (ok) — чтобы GUI-поток знал, планиров ли следующую
        попытку."""
        ok = False
        try:
            ok = self._voice.connect(host, port,
                                     self._player_name, self._access_key)
        except Exception:
            ok = False  # поток не имеет права падать молча-навсегда
        self.voice_connect_result_sig.emit(bool(ok), "")

    def _on_voice_connect_result(self, ok: bool, _err: str) -> None:
        """Итог фоновой попытки — в GUI-потоке. Успех: чистим счётчики и
        рисуем «в канале». Неудача: движок уже доложил ошибку через
        on_error — просто планируем следующую попытку лестницы."""
        self._voice_connecting = False
        if getattr(self, "_teardown_done", False):
            # Окно закрыли, пока попытка была в полёте: если connect успел
            # подключиться — разъединяем, чтобы не остался живым микрофон.
            self._voice.disconnect()
            return
        if ok:
            self._ever_voice_connected = True
            self._voice_reconnect_attempts = 0
            self._apply_connected_ui(self._voice_host, self._voice_port)
            return
        self._schedule_voice_reconnect()

    # ── Опрос участников ────────────────────────────────────────────

    def _poll_participants(self) -> None:
        """Опрашивает /voice/participants на HTTP-сервере (не на голосовом TCP).

        v1.9.5: запрос уходит через AsyncBridge (фоновый поток), как весь
        остальной клиент. Раньше таймер дёргал его СИНХРОННО в GUI-потоке:
        недоступный хост (firewall drop) морозил всё приложение до 3с
        каждые 2с — окно «висело» 60% времени."""
        if not self._base_url:
            return
        if self._poll_in_flight:
            return  # не накапливаем параллельные запросы
        self._poll_in_flight = True
        self._bridge.run(
            lambda: client.get_voice_participants(
                self._base_url, self._player_name, self._access_key
            ),
            on_success=self._on_participants_loaded,
            on_error=self._on_participants_error,
        )

    def _on_participants_loaded(self, participants: list) -> None:
        """Ответ поллинга — уже в GUI-потоке (через мост)."""
        self._poll_in_flight = False
        self._render_participants(participants or [])

    def _on_participants_error(self, _e) -> None:
        """Сетевой сбой поллинга — молча снимаем in-flight (голосовой
        может быть ещё не поднят), следующий тик повторит."""
        self._poll_in_flight = False

    # ── Индикатор «говорит» ─────────────────────────────────────────

    def _apply_speaking(self, states: dict) -> None:
        """Серверные переходы {имя: on}. Своё кольцо ведёт локальный VAD —
        сетевые события о себе игнорируем."""
        for name, on in states.items():
            if name == self._player_name:
                continue
            self._speaking[name] = bool(on)
            chip = self._chips.get(name)
            if chip is not None:
                chip.set_speaking(bool(on))

    def _apply_local_speaking(self, on: bool) -> None:
        """Мой локальный VAD: мгновенный отклик без круга через сервер."""
        self._speaking[self._player_name] = bool(on)
        chip = self._chips.get(self._player_name)
        if chip is not None:
            chip.set_speaking(bool(on))

    def _render_participants(self, participants: list[str]) -> None:
        # Пересобираем чипы ТОЛЬКО если состав изменился: раньше каждые 2с
        # всё уничтожалось и создавалось заново — мерцание и сброс колец.
        if participants == self._rendered_participants:
            # Состав тот же — обновим mute у себя и КОЛЬЦА «говорит»
            # (v1.9.5: раньше после disconnect/тишины чужие кольца
            # оставались зелёными навсегда — _speaking чистился, но чипы
            # в этой ветке не обновлялись).
            me_chip = self._chips.get(self._player_name)
            if me_chip is not None:
                me_chip.set_muted(self._voice.is_muted())
            for name, chip in self._chips.items():
                if name != self._player_name:
                    chip.set_speaking(self._speaking.get(name, False))
            return
        self._rendered_participants = list(participants)
        self._chips.clear()
        # Чистим старых
        while self._participants_layout.count() > 1:
            item = self._participants_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        if not participants:
            empty = QLabel("Никого в канале")
            empty.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 12px; padding: 12px;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._participants_layout.insertWidget(0, empty)
            return
        for name in participants:
            is_me = name == self._player_name
            muted = is_me and self._voice.is_muted()
            chip = _VoiceUserChip(name, is_me=is_me, muted=muted)
            chip.set_speaking(self._speaking.get(name, False))
            self._chips[name] = chip
            self._participants_layout.insertWidget(
                self._participants_layout.count() - 1, chip
            )
