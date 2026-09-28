"""Qt-виджеты игр (View-слой).

Здесь живут только QWidget-канвасы — отрисовка и сбор ввода. Игровая
логика (engine) и сущности (entities) остаются в ``lib/games/<name>/``
и не зависят от Qt:
- orbital_canvas.OrbitalGameWidget  → lib/games/orbital/engine.py
- voxel_canvas.VoxelCanvas          → lib/games/voxel (симуляция на сервере)

Правило слоёв: qt_app → lib разрешено, lib → qt_app — запрещено
(см. ARCHITECTURE.md).
"""
