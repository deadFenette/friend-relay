from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from legacy.tkinter.app import RelayApp


class AnimatedMixin:
    """Миксин для анимаций и плавных переходов.

    Отвечает за:
    - Плавные переходы между видами (slide left/right)
    - Пульсация индикаторов
    - Появление элементов с fade-in
    - Все анимации через after() без потоков
    """

    def _init_animation_state(self: RelayApp) -> None:
        """Инициализирует состояние анимаций."""
        self._current_view: tk.Frame | None = None
        self._target_view: tk.Frame | None = None
        self._animation_step: int = 0
        self._animation_total_steps: int = 10
        self._animation_delay: int = 20  # мс между шагами
        self._animation_in_progress: bool = False
        self._animation_callback: Callable[[], None] | None = None

    def _switch_view_animated(self: RelayApp, new_view: tk.Frame,
                              direction: str = "left",
                              callback: Callable[[], None] | None = None) -> None:
        """Плавный переход к новому виду.

        Args:
            new_view: Новый фрейм для показа
            direction: "left" (уходит влево) или "right" (уходит вправо)
            callback: Функция вызывается после завершения анимации
        """
        if self._animation_in_progress:
            return  # Не прерываем текущую анимацию

        self._target_view = new_view
        self._animation_callback = callback
        self._animation_step = 0
        self._animation_in_progress = True

        # Подготавливаем анимацию
        if self._current_view:
            # Текущий вид уходит
            self._current_view.place_forget()

        # Новый вид появляется справа
        self._target_view.place(
            relx=1.0, rely=0.0,
            relwidth=1.0, relheight=1.0,
            anchor="ne"
        )

        # Запускаем анимацию
        self._animate_slide(direction)

    def _animate_slide(self: RelayApp, direction: str) -> None:
        """Выполняет один шаг анимации слайда."""
        if not self._animation_in_progress:
            return

        if self._animation_step >= self._animation_total_steps:
            # Анимация завершена
            self._finish_animation()
            return

        # Вычисляем прогресс (0.0 → 1.0)
        progress = self._animation_step / self._animation_total_steps

        if direction == "left":
            # Текущий уходит влево (x: 0 → -1), новый приходит справа (x: 1 → 0)
            # Упрощённая версия - только новый вид
            start_x = 1.0
            end_x = 0.0
        else:
            # Направо
            start_x = -1.0
            end_x = 0.0

        # Линейная интерполяция
        current_x = start_x + (end_x - start_x) * progress

        if self._target_view:
            self._target_view.place(
                relx=current_x, rely=0.0,
                relwidth=1.0, relheight=1.0,
                anchor="nw"
            )

        self._animation_step += 1
        self.root.after(self._animation_delay, lambda: self._animate_slide(direction))

    def _finish_animation(self: RelayApp) -> None:
        """Завершает анимацию и показывает финальное состояние."""
        self._animation_in_progress = False

        if self._target_view:
            # Финальная позиция
            self._target_view.place(
                relx=0.0, rely=0.0,
                relwidth=1.0, relheight=1.0,
                anchor="nw"
            )

        # Обновляем текущий вид
        if self._current_view:
            self._current_view.place_forget()
        self._current_view = self._target_view

        # Вызываем callback если есть
        if self._animation_callback:
            callback = self._animation_callback
            self._animation_callback = None
            callback()

    def _pulse_animation(self: RelayApp, canvas: tk.Canvas,
                         color: str, duration: int = 1000) -> None:
        """Создаёт пульсирующую анимацию на канвасе.

        Args:
            canvas: Канвас для анимации
            color: Цвет для пульсации
            duration: Длительность одного цикла в мс
        """
        if not hasattr(self, '_pulse_state'):
            self._pulse_state = {}

        canvas_id = id(canvas)
        if canvas_id not in self._pulse_state:
            self._pulse_state[canvas_id] = {
                'step': 0,
                'total_steps': 20,
                'direction': 1,
                'active': True
            }

        state = self._pulse_state[canvas_id]
        if not state['active']:
            return

        # Вычисляем размер (4px → 8px → 4px)
        base_size = 4
        max_size = 8
        size_range = max_size - base_size

        progress = state['step'] / state['total_steps']
        if state['direction'] == 1:
            current_size = base_size + size_range * progress
        else:
            current_size = max_size - size_range * progress

        # Рисуем пульсирующую точку
        canvas.delete("pulse")
        canvas.create_oval(
            (10 - current_size, 10 - current_size,
             10 + current_size, 10 + current_size),
            fill=color, outline="", tags="pulse"
        )

        # Обновляем состояние
        state['step'] += 1
        if state['step'] >= state['total_steps']:
            state['step'] = 0
            state['direction'] *= -1  # Меняем направление

        # Продолжаем анимацию
        delay = duration // state['total_steps']
        self.root.after(delay, lambda: self._pulse_animation(canvas, color, duration))

    def _stop_pulse_animation(self: RelayApp, canvas: tk.Canvas) -> None:
        """Останавливает пульсацию на канвасе."""
        canvas_id = id(canvas)
        if canvas_id in self._pulse_state:
            self._pulse_state[canvas_id]['active'] = False
            canvas.delete("pulse")

    def _fade_in_widget(self: RelayApp, widget: tk.Widget,
                        duration: int = 300) -> None:
        """Плавное появление виджета (fade-in).

        Примечание: Tkinter ограничен в прозрачности, это упрощённая версия.
        """
        # Tkinter не поддерживает alpha для обычных виджетов
        # Используем альтернативный подход - gradual show
        widget.place_forget()
        widget.place(relx=0.5, rely=0.5, anchor="center")
        widget.lift()  # Поднимаем на передний план

    def _bounce_animation(self: RelayApp, widget: tk.Widget,
                          amplitude: int = 10, duration: int = 200) -> None:
        """Анимация подпрыгивания (bounce)."""
        original_y = widget.winfo_y()

        def bounce_step(step: int):
            if step > 4:  # 4 шага: up, down, up, down, original
                widget.place(y=original_y)
                return

            # Sine wave для плавности
            offset = amplitude * (1 - step / 4) * (1 if step % 2 == 0 else -1)
            widget.place(y=original_y + offset)

            self.root.after(duration // 4, lambda: bounce_step(step + 1))

        bounce_step(0)