"""Voxel Shooter — top-down multiplayer шутер с разрушаемым миром.

Пакет содержит (чистая логика, без Qt):
- entities: dataclasses для сущностей (Player, Enemy, Bullet, Wall, и т.д.)
- engine: VoxelEngine — серверная симуляция в реальном времени, держит
  состояние игры (стены, враги, пули, игроки). Один инстанс на сессию.

Qt-канвас живёт во View-слое: qt_app/widgets/games/voxel_canvas.py —
клиентский QWidget, рендерит snapshot с сервера, отправляет input.

Сам бот-класс живёт в lib/bots/voxel.py.
"""
