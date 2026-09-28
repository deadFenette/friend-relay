"""Orbital Defense — игра-бот «защити планету с орбиты».

Пакет содержит (чистая логика, без Qt):
- entities: Asteroid, Particle, Laser, PowerUp (dataclasses без логики),
  строковые константы клавиш KEY_* и QColor с заглушкой
- engine: OrbitalEngine — игровая логика (спавн, столкновения, волны, комбо)

Qt-канвас живёт во View-слое: qt_app/widgets/games/orbital_canvas.py.
Сам бот-класс живёт отдельно в lib/bots/orbital.py, как и snake.
"""
