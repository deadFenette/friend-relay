"""VoxelCanvas — клиентский QWidget, рендерит snapshot с сервера.

View-слой (qt_app/widgets/games). Не симулирует игру — только отображает
состояние + отправляет input на сервер через callback. Симуляция идёт на
сервере (VoxelEngine в lib/games/voxel/engine.py), клиент только показывает.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from lib.games.voxel.entities import (
    FIELD_H,
    FIELD_W,
    TEAM_COLORS,
    TEAM_NONE,
    TILE_SIZE,
)


class VoxelCanvas(QWidget):
    """Виджет игры. Чистая отрисовка + сбор input."""

    # Эмитится когда input изменился (отправляется на сервер в диалоге)
    input_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(FIELD_W, FIELD_H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        # Snapshot с сервера — обновляется через set_snapshot
        self._snapshot: dict = {}
        self._my_player_id: str = ""

        # Локальный input (отправляем на сервер каждый тик)
        self._keys: set[int] = set()
        self._mouse_btn: set[Qt.MouseButton] = set()
        self._mouse_pos_x: int = 0
        self._mouse_pos_y: int = 0
        self._shop_open: bool = False
        self._pending_actions: dict = {}  # одноразовые действия (граната, покупка)

        # Таймер отправки input (20 раз в секунду — сервер тикает 60, но 20 ок)
        self._input_timer = QTimer(self)
        self._input_timer.timeout.connect(self._flush_input)
        self._input_timer.start(50)  # 50мс = 20 Гц

    def set_snapshot(self, snapshot: dict) -> None:
        """Получает snapshot с сервера и перерисовывает."""
        self._snapshot = snapshot
        self.update()

    def set_player_id(self, player_id: str) -> None:
        self._my_player_id = player_id

    # ── Ввод ────────────────────────────────────────────────────────

    def focusOutEvent(self, event) -> None:
        """v1.9.5: Alt-Tab с зажатым W — release до нас не доходит, без
        очистки игрок бежал вечно (сервер продолжал получать «w» 20р/с)."""
        self._keys.clear()
        self._mouse_btn = None
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_B:
            self._shop_open = not self._shop_open
            self._pending_actions["toggle_shop"] = True
            self.update()
            return
        # Покупки в магазине — проверяем ПЕРВЫМИ, чтобы 1/2/3/4 работали
        # для покупок а не для переключения оружия
        if self._shop_open:
            if key == Qt.Key.Key_1:
                self._pending_actions["shop_buy"] = "rifle"
                return
            if key == Qt.Key.Key_2:
                self._pending_actions["shop_buy"] = "grenade"
                return
            if key == Qt.Key.Key_3:
                self._pending_actions["shop_buy"] = "heal"
                return
            if key == Qt.Key.Key_4:
                self._shop_open = False
                self._pending_actions["toggle_shop"] = True
                return
            return  # в магазине другие клавиши не обрабатываем
        # Переключение оружия (только если магазин закрыт)
        if key == Qt.Key.Key_1:
            self._pending_actions["weapon_switch"] = "pistol"
            return
        if key == Qt.Key.Key_2:
            self._pending_actions["weapon_switch"] = "rifle"
            return
        if key == Qt.Key.Key_G:
            self._pending_actions["throw_grenade"] = True
            return
        self._keys.add(key)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self._keys.discard(event.key())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._mouse_btn.add(event.button())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._mouse_btn.discard(event.button())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._mouse_pos_x = event.position().x()
        self._mouse_pos_y = event.position().y()

    def _flush_input(self) -> None:
        """Отправляет накопленный input на сервер (через сигнал).

        Конвертирует Qt.Key_* и Qt.MouseButton в строковые идентификаторы,
        чтобы серверный движок (без Qt-зависимости) мог их обрабатывать.
        """
        # Конвертация клавиш: Qt.Key_W → "w" и т.д.
        KEY_MAP = {
            Qt.Key.Key_W: "w",
            Qt.Key.Key_A: "a",
            Qt.Key.Key_S: "s",
            Qt.Key.Key_D: "d",
        }
        keys_list = []
        for k in self._keys:
            mapped = KEY_MAP.get(k)
            if mapped:
                keys_list.append(mapped)

        # Конвертация кнопок мыши
        mouse_btn_list = []
        if Qt.MouseButton.LeftButton in self._mouse_btn:
            mouse_btn_list.append("left")

        data = {
            "keys": keys_list,
            "mouse_btn": mouse_btn_list,
            "mouse_x": self._mouse_pos_x,
            "mouse_y": self._mouse_pos_y,
        }
        # Добавляем одноразовые действия и чистим их
        for k, v in self._pending_actions.items():
            data[k] = v
        self._pending_actions = {}
        self.input_changed.emit(data)

    # ── Отрисовка ──────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Фон
        p.fillRect(self.rect(), QColor(45, 45, 45))

        # Сетка
        pen_color = QColor(55, 55, 55)
        p.setPen(pen_color)
        for x in range(0, FIELD_W, TILE_SIZE):
            p.drawLine(x, 0, x, FIELD_H)
        for y in range(0, FIELD_H, TILE_SIZE):
            p.drawLine(0, y, FIELD_W, y)

        # Стены — цвет зависит от типа блока
        for w in self._snapshot.get("walls", []):
            # v1.9.5: нулевой max_hp из снапшота = ZeroDivisionError в
            # paintEvent (краш всего окна игры)
            ratio = w["hp"] / w["max_hp"] if w.get("max_hp") else 1.0
            bt = w.get("block_type", "brick")
            # Базовый цвет по типу
            if bt == "wood":
                base = QColor(140, 100, 50)
            elif bt == "concrete":
                base = QColor(120, 120, 130)
            else:  # brick
                base = QColor(150, 80, 60)
            # Затемняем по мере повреждения
            col = QColor(
                int(base.red() * (0.5 + 0.5 * ratio)),
                int(base.green() * (0.5 + 0.5 * ratio)),
                int(base.blue() * (0.5 + 0.5 * ratio)),
            )
            p.fillRect(int(w["x"]) + 1, int(w["y"]) + 1,
                       w["tile_size"] - 2, w["tile_size"] - 2, col)
            # Бетон — более толстая обводка
            pen_w = 2 if bt == "concrete" else 1
            p.setPen(QPen(QColor(60, 60, 60), pen_w))
            p.drawRect(int(w["x"]), int(w["y"]), w["tile_size"], w["tile_size"])

        # Взрывы
        for ex in self._snapshot.get("explosions", []):
            r = ex["r"]
            # v1.9.5: тот же guard для взрывов
            max_r = ex["max_r"] or 1
            alpha = int(200 * (1 - r / max_r))
            col = QColor(255, 80 + int(100 * r / max_r), 0, alpha)
            p.setBrush(col)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(int(ex["x"] - r), int(ex["y"] - r), r * 2, r * 2)

        # Пули
        for b in self._snapshot.get("bullets", []):
            col = QColor(255, 220, 50) if b["is_player"] else QColor(255, 60, 60)
            p.fillRect(int(b["x"]) - 2, int(b["y"]) - 2, 5, 5, col)

        # Гранаты
        for g in self._snapshot.get("grenades", []):
            p.setBrush(QColor(255, 120, 0))
            p.setPen(QColor(180, 80, 0))
            p.drawEllipse(int(g["x"]) - 6, int(g["y"]) - 6, 12, 12)

        # Враги
        for e in self._snapshot.get("enemies", []):
            # Тень
            p.fillRect(int(e["x"]) + 3, int(e["y"]) + 3, e["size"], e["size"],
                       QColor(0, 0, 0, 90))
            # Тело
            p.fillRect(int(e["x"]), int(e["y"]), e["size"], e["size"],
                       QColor(200, 50, 50))
            p.setPen(QColor(150, 30, 30))
            p.drawRect(int(e["x"]), int(e["y"]), e["size"], e["size"])
            # Глаза
            p.fillRect(int(e["x"]) + 5, int(e["y"]) + 7, 7, 7, QColor(255, 255, 0))
            p.fillRect(int(e["x"]) + 18, int(e["y"]) + 7, 7, 7, QColor(255, 255, 0))
            # HP bar
            hp_r = e["hp"] / e["max_hp"] if e.get("max_hp") else 1.0
            p.fillRect(int(e["x"]), int(e["y"]) - 7, e["size"], 4, QColor(80, 0, 0))
            p.fillRect(int(e["x"]), int(e["y"]) - 7, int(e["size"] * hp_r), 4,
                       QColor(255, 40, 40))

        # Игроки
        for pl in self._snapshot.get("players", []):
            color = TEAM_COLORS.get(pl.get("team", TEAM_NONE), TEAM_COLORS[TEAM_NONE])
            if pl.get("is_bot"):
                # Боты — слегка прозрачнее
                color = QColor(color.red(), color.green(), color.blue(), 200)
            if not pl.get("alive", True):
                # Мёртвые — серые
                color = QColor(80, 80, 80, 100)
            x, y, size = int(pl["x"]), int(pl["y"]), pl["size"]
            # Тень
            p.fillRect(x + 3, y + 3, size, size, QColor(0, 0, 0, 90))
            p.fillRect(x, y, size, size, color)
            p.setPen(QColor(20, 20, 20))
            p.drawRect(x, y, size, size)
            # Глаза (для живых)
            if pl.get("alive", True):
                p.fillRect(x + 5, y + 7, 7, 7, QColor(255, 255, 255))
                p.fillRect(x + 18, y + 7, 7, 7, QColor(255, 255, 255))
            # Имя над головой
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Consolas", 9))
            p.drawText(x, y - 12, pl.get("name", "?"))
            # HP bar
            hp_r = pl["hp"] / pl["max_hp"]
            p.fillRect(x, y - 5, size, 4, QColor(40, 40, 40))
            p.fillRect(x, y - 5, int(size * hp_r), 4,
                       QColor(50, 220, 80) if hp_r > 0.3 else QColor(255, 60, 60))

        # Прицел (только для меня)
        if self._my_player_id:
            p.setPen(QPen(QColor(255, 255, 255, 180), 2))
            mx, my = self._mouse_pos_x, self._mouse_pos_y
            p.drawLine(mx - 12, my, mx + 12, my)
            p.drawLine(mx, my - 12, mx, my + 12)
            p.drawEllipse(mx - 4, my - 4, 8, 8)

        # UI — верхняя панель (данные моего игрока)
        my_data = next((p for p in self._snapshot.get("players", [])
                        if p["id"] == self._my_player_id), None)
        if my_data:
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
            weapon_name = "АВТОМАТ" if my_data["weapon"] == "rifle" else "ПИСТОЛЕТ"
            info = (f"❤ {int(my_data['hp'])}  |  💰 {my_data['money']}  |  "
                    f"🔫 {weapon_name}  |  🧨 {my_data['grenades']}  |  "
                    f"ВОЛНА {self._snapshot.get('wave', 1)}  |  "
                    f"☠ {my_data['kills']}")
            p.drawText(12, 24, info)

        # Подсказка снизу
        p.setFont(QFont("Consolas", 9))
        p.setPen(QColor(200, 200, 200))
        pvp_flag = " [PvP]" if self._snapshot.get("pvp") else ""
        hint = (f"WASD бегать | ЛКМ стрелять | G граната | 1/2 оружие | "
                f"B магазин{pvp_flag}")
        p.drawText(10, FIELD_H - 10, hint)

        # Магазин
        if self._shop_open and my_data:
            self._draw_shop(p, my_data)

        # Ожидание игроков если ещё не started
        game_state = self._snapshot.get("game_state", "playing")
        if game_state == "lobby":
            p.fillRect(self.rect(), QColor(0, 0, 0, 180))
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Consolas", 24, QFont.Weight.Bold))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "🟡 ЛОББИ — ждём старта\n\nНажмите «▶ Старт игры» когда готовы")
        elif not self._snapshot.get("players"):
            p.fillRect(self.rect(), QColor(0, 0, 0, 180))
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Consolas", 24, QFont.Weight.Bold))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Ожидание игроков…")

    def _draw_shop(self, p: QPainter, my_data: dict) -> None:
        p.fillRect(self.rect(), QColor(0, 0, 0, 190))
        x, y, w, h = 250, 140, 400, 360
        p.fillRect(x, y, w, h, QColor(50, 50, 55))
        p.setPen(QColor(255, 255, 255))
        p.drawRect(x, y, w, h)

        p.setFont(QFont("Consolas", 18, QFont.Weight.Bold))
        p.drawText(x + 20, y + 35, "🏪 МАГАЗИН")
        p.setFont(QFont("Consolas", 12))

        items = [
            ("1. АВТОМАТ  —  $500", my_data.get("has_rifle", False)),
            ("2. ГРАНАТА  —  $200", False),
            ("3. Хил +50  —  $300", False),
            ("", False),
            ("4. Закрыть", False),
        ]
        for i, (text, sold) in enumerate(items):
            if not text:
                continue
            color = QColor(120, 255, 120) if sold else QColor(255, 255, 255)
            p.setPen(color)
            p.drawText(x + 25, y + 75 + i * 32, text)

        p.setPen(QColor(180, 180, 180))
        p.setFont(QFont("Consolas", 10))
        p.drawText(x + 25, y + h - 15, f"Твои деньги: ${my_data['money']}")
