"""VoxelEngine — серверная симуляция шутера.

Один инстанс = одна сессия (room). Сервер (lib/voxel_server.py) держит
словарь sessions: session_id → VoxelEngine. Движок:
  - 60 FPS тик (через asyncio или Timer — зависит от транспорта)
  - Симулирует ВСЕ игроков, врагов, пули, стены
  - Принимает input от клиентов (update_player_input)
  - Выдаёт snapshot (to_snapshot) — клиентам рендерить и всё

Боты (NPC) тоже представлены как Player с is_bot=True, их поведение
симулируется прямо в тике (AI: двигаются к ближайшему врагу, стреляют).

Команды: PvP включается если есть ≥2 команд. Тогда игроки могут наносить
урон друг другу. PvE (атаки врагов) — всегда есть.
"""
from __future__ import annotations

import math
import random
import time

from lib.games.voxel.entities import (
    COLS,
    FIELD_H,
    FIELD_W,
    KEY_A,
    KEY_D,
    KEY_S,
    KEY_W,
    MOUSE_LEFT,
    ROWS,
    TEAM_BLUE,
    TEAM_NONE,
    TEAM_RED,
    Bullet,
    Enemy,
    Explosion,
    Grenade,
    Particle,
    Player,
    Wall,
)

# ── Режимы игры ────────────────────────────────────────────────────
MODE_PVE = "pve"       # волны врагов, PvE
MODE_PVP = "pvp"       # только PvP, без волн и врагов
MODE_MIXED = "mixed"   # и волны, и PvP


def rect_hits_wall(walls: list[Wall], x: float, y: float, size: float) -> bool:
    """Проверяет, пересекается ли квадрат (x, y, size) с любой живой стеной."""
    for w in walls:
        if not w.alive:
            continue
        if (x < w.x + w.tile_size and x + size > w.x
                and y < w.y + w.tile_size and y + size > w.y):
            return True
    return False


def find_spawn_point(walls: list[Wall], players: list[Player],
                     avoid_radius: float = 80) -> tuple[float, float]:
    """Находит случайную точку для спавна врага: не в стене, не у игроков."""
    for _ in range(50):
        x = random.randint(40, FIELD_W - 60)
        y = random.randint(40, FIELD_H - 60)
        if rect_hits_wall(walls, x, y, 30):
            continue
        ok = True
        for p in players:
            if p.alive and math.hypot(p.x - x, p.y - y) < avoid_radius:
                ok = False
                break
        if ok:
            return float(x), float(y)
    return 60.0, 60.0  # fallback


class VoxelEngine:
    """Игровая сессия: состояние + методы обновления.

    Не зависит от транспорта (TCP/WebSocket/loopback) — только чистая
    логика. Сервер (VoxelSessionServer) тикает её с частотой 60 FPS и
    раз в ~50мс шлёт snapshot клиентам.
    """

    def __init__(self, session_id: str, max_players: int = 8,
                 game_mode: str = MODE_PVE, wall_hp_override: int = 0,
                 team_red_size: int = 5, team_blue_size: int = 5):
        self.session_id = session_id
        self.game_mode = game_mode  # pve | pvp | mixed
        self.wall_hp_override = wall_hp_override
        self.team_red_size = team_red_size   # сколько слотов в красной команде
        self.team_blue_size = team_blue_size  # сколько слотов в синей
        # БАГФИКС: max_players раньше могло быть меньше, чем team_red_size +
        # team_blue_size (например max_players=8 по умолчанию, а команды 5v5 =
        # 10 слотов). Тогда fill_bots()/start_game() пытались добавить ботов
        # сверх max_players, add_bot() поднимал RuntimeError("session full"),
        # исключение никто не ловил — и кнопки "Заполнить ботами"/"Старт игры"
        # просто переставали отвечать. Гарантируем, что слотов всегда хватает
        # на обе команды.
        self.max_players = max(max_players, team_red_size + team_blue_size)
        self.created_ts = time.time()
        self.last_activity = time.time()

        self.players: dict[str, Player] = {}  # id → Player
        self.walls: list[Wall] = []
        self.enemies: list[Enemy] = []
        self.bullets: list[Bullet] = []
        self.grenades: list[Grenade] = []
        self.explosions: list[Explosion] = []
        self.particles: list[Particle] = []

        self.wave = 1
        self.game_started = False  # True когда первый игрок готов
        self.game_state = "lobby"   # lobby | playing — в lobby нет урона/врагов
        self.tick_count = 0
        self.next_bot_id = 1

        self._generate_level()

    # ── Игроки ──────────────────────────────────────────────────────

    def add_player(self, player_id: str, name: str, team: str = TEAM_NONE) -> Player:
        """Добавляет игрока в сессию. Если достигнут лимит — поднимает."""
        if player_id in self.players:
            return self.players[player_id]
        if len(self.players) >= self.max_players:
            raise RuntimeError(f"session full (max {self.max_players})")

        # Спавним в стороне от других
        x, y = find_spawn_point(self.walls, list(self.players.values()))
        player = Player(id=player_id, name=name, team=team, x=x, y=y)
        self.players[player_id] = player
        self.last_activity = time.time()
        self.game_started = True
        return player

    def remove_player(self, player_id: str) -> None:
        self.players.pop(player_id, None)
        self.last_activity = time.time()

    def update_player_input(self, player_id: str, input_data: dict) -> None:
        """Обновляет ввод игрока (от клиента)."""
        p = self.players.get(player_id)
        if p is None or not p.alive:
            return
        # input_data от клиента: keys = ["w","a",...], mouse_btn = ["left"]
        # Конвертируем списки в множества строк (engine не зависит от Qt).
        p.keys = set(input_data.get("keys", []))
        p.mouse_btn = set(input_data.get("mouse_btn", []))
        p.mouse_x = input_data.get("mouse_x", p.mouse_x)
        p.mouse_y = input_data.get("mouse_y", p.mouse_y)

        if input_data.get("toggle_shop"):
            p.shop_open = not p.shop_open

        if input_data.get("weapon_switch"):
            w = input_data["weapon_switch"]
            if w == "pistol":
                p.weapon = "pistol"
            elif w == "rifle" and p.has_rifle:
                p.weapon = "rifle"

        if input_data.get("throw_grenade"):
            self._throw_grenade(p)

        if input_data.get("shop_buy") and p.shop_open:
            self._shop_buy(p, input_data["shop_buy"])

    def add_bot(self, team: str = TEAM_NONE) -> Player:
        """Добавляет NPC-бота в сессию со случайной стратегией."""
        bot_id = f"bot_{self.next_bot_id}"
        self.next_bot_id += 1
        bot = self.add_player(bot_id, f"Bot-{self.next_bot_id - 1}", team)
        bot.is_bot = True
        # Случайная стратегия — разные боты играют по-разному
        import random as _r
        bot.ai_strategy = _r.choice(["aggressive", "sniper", "flanker", "defensive"])
        bot.ai_strafe_dir = _r.choice([1, -1])
        return bot

    # ── Тик симуляции ──────────────────────────────────────────────

    def tick(self) -> None:
        """Один кадр симуляции (1/60 сек). Вызывается сервером.

        В 'lobby' состоянии — игроки могут двигаться, но нет урона и врагов.
        В 'playing' — полная симуляция.
        """
        self.tick_count += 1
        if not self.game_started:
            return

        # Обработка игроков (движение разрешено в lobby, урон — нет)
        for p in list(self.players.values()):
            if p.alive:
                if p.is_bot and self.game_state == "playing":
                    self._ai_player_tick(p)
                if self.game_state == "playing":
                    self._handle_player_movement(p)
                    self._handle_player_shoot(p)
                else:
                    # В lobby можно ходить, но не стрелять
                    self._handle_player_movement(p)
                if p.shoot_cooldown > 0:
                    p.shoot_cooldown -= 1
                if p.grenade_cooldown > 0:
                    p.grenade_cooldown -= 1

        # Только в playing-состоянии — симуляция урона/врагов/пуль
        if self.game_state != "playing":
            return

        # Спавн волны если ещё нет врагов (только в PvE и mixed режимах)
        if self.game_mode != MODE_PVP and not self.enemies and self.tick_count > 60:
            self._spawn_wave()

        self._update_bullets()
        self._update_grenades()
        self._update_enemies()
        self._update_explosions()
        self._update_particles()
        self._check_wave_complete()
        self._respawn_dead_players()

    def _handle_player_movement(self, p: Player) -> None:
        if p.shop_open:
            return
        dx, dy = 0, 0
        if KEY_W in p.keys:
            dy -= 1
        if KEY_S in p.keys:
            dy += 1
        if KEY_A in p.keys:
            dx -= 1
        if KEY_D in p.keys:
            dx += 1
        if dx == 0 and dy == 0:
            return
        length = math.hypot(dx, dy)
        dx /= length
        dy /= length
        nx = p.x + dx * p.speed
        ny = p.y + dy * p.speed
        if not rect_hits_wall(self.walls, nx, p.y, p.size):
            p.x = nx
        if not rect_hits_wall(self.walls, p.x, ny, p.size):
            p.y = ny
        # Не выходим за границы поля
        p.x = max(0, min(FIELD_W - p.size, p.x))
        p.y = max(0, min(FIELD_H - p.size, p.y))

    def _handle_player_shoot(self, p: Player) -> None:
        if p.shop_open:
            return
        if MOUSE_LEFT in p.mouse_btn and p.shoot_cooldown <= 0:
            self._shoot(p)

    def _shoot(self, p: Player) -> None:
        cx = p.x + p.size / 2
        cy = p.y + p.size / 2
        angle = math.atan2(p.mouse_y - cy, p.mouse_x - cx)
        if p.weapon == "pistol":
            self.bullets.append(Bullet.from_angle(cx, cy, angle, 10, 25,
                                                  is_player=True, owner_id=p.id))
            p.shoot_cooldown = 18
        elif p.weapon == "rifle" and p.has_rifle:
            spread = random.uniform(-0.12, 0.12)
            self.bullets.append(Bullet.from_angle(cx, cy, angle + spread, 13, 15,
                                                  is_player=True, owner_id=p.id))
            p.shoot_cooldown = 5

    def _throw_grenade(self, p: Player) -> None:
        if p.grenades <= 0 or p.grenade_cooldown > 0:
            return
        p.grenades -= 1
        p.grenade_cooldown = 40
        cx = p.x + p.size / 2
        cy = p.y + p.size / 2
        self.grenades.append(Grenade.from_target(cx, cy, p.mouse_x, p.mouse_y,
                                                 owner_id=p.id))

    # ── AI для ботов ────────────────────────────────────────────────

    def _ai_player_tick(self, p: Player) -> None:
        """Умный AI с 4 стратегиями + сепарация (расхождение от других ботов).

        Стратегии (выбираются случайно при создании бота):
          - aggressive: бежит в упор, стреляет вплотную, использует гранаты
          - sniper: держит дистанцию 300px, стреляет аккуратно, экономит гранаты
          - flanker: пытается зайти сбоку/сзади цели, стрейфит агрессивно
          - defensive: держит дистанцию, отступает при контратаке, лечится в магазине

        Адаптивность: при HP < 30% все стратегии переходят в «отступление».
        Сепарация: бот избегает других ботов — не сливаются в куб.
        """
        if not p.alive:
            return

        # Сепарация — отходим от ближайшего бота если слишком близко
        separation_x, separation_y = self._compute_separation(p)

        # Если HP < 30% — отступаем независимо от стратегии
        if p.hp < p.max_hp * 0.3:
            target = self._find_nearest_target(p, include_players=False)
            self._ai_retreat(p, target, separation_x, separation_y)
            return

        # Defensive: если есть деньги и HP < 60% — покупаем хил
        if p.ai_strategy == "defensive" and p.hp < p.max_hp * 0.6 and p.money >= 300:
            p.shop_open = True
            p.keys = set()
            p.mouse_btn = set()
            # Эмуляция покупки — делаем это через update_player_input
            # (нельзя напрямую т.к. _shop_buy приватный, но мы на сервере)
            self._shop_buy(p, "heal")
            p.shop_open = False
            return

        # Ищем цель
        target = self._find_nearest_target(p, include_players=True)
        if target is None:
            p.keys = set()
            p.mouse_btn = set()
            return

        dx = target.x - p.x
        dy = target.y - p.y
        dist = math.hypot(dx, dy)

        # Стрейф таймер — меняем направление каждые ~30-60 тиков
        p.ai_strafe_timer -= 1
        if p.ai_strafe_timer <= 0:
            p.ai_strafe_dir *= -1
            p.ai_strafe_timer = 30 + (hash(p.id) % 30)

        # Применяем стратегию
        if p.ai_strategy == "aggressive":
            self._ai_aggressive(p, target, dx, dy, dist, separation_x, separation_y)
        elif p.ai_strategy == "sniper":
            self._ai_sniper(p, target, dx, dy, dist, separation_x, separation_y)
        elif p.ai_strategy == "flanker":
            self._ai_flanker(p, target, dx, dy, dist, separation_x, separation_y)
        else:  # defensive
            self._ai_defensive(p, target, dx, dy, dist, separation_x, separation_y)

    def _compute_separation(self, p: Player) -> tuple[float, float]:
        """Вектор сепарации — отходит от ближайших ботов чтобы не сливаться.
        Возвращает (dx, dy) — направление куда нужно сместиться.
        """
        sep_x, sep_y = 0.0, 0.0
        min_dist = 60  # если бот ближе 60px — отходим
        for other in self.players.values():
            if other is p or not other.alive or not other.is_bot:
                continue
            d = math.hypot(other.x - p.x, other.y - p.y)
            if 0 < d < min_dist:
                # Отходим от этого бота
                sep_x -= (other.x - p.x) / d
                sep_y -= (other.y - p.y) / d
        return sep_x, sep_y

    def _apply_separation(self, p: Player, sep_x: float, sep_y: float) -> None:
        """Добавляет сепарацию к текущим клавишам."""
        if abs(sep_x) > 0.1:
            p.keys.add(KEY_D if sep_x > 0 else KEY_A)
        if abs(sep_y) > 0.1:
            p.keys.add(KEY_S if sep_y > 0 else KEY_W)

    def _ai_retreat(self, p: Player, target, sep_x: float, sep_y: float) -> None:
        """Отступление при низком HP."""
        p.keys = set()
        if target:
            dx = p.x - target.x
            dy = p.y - target.y
            if abs(dx) > 10:
                p.keys.add(KEY_D if dx > 0 else KEY_A)
            if abs(dy) > 10:
                p.keys.add(KEY_S if dy > 0 else KEY_W)
            # Стреляем на бегу
            p.mouse_x = int(target.x)
            p.mouse_y = int(target.y)
            p.mouse_btn = {MOUSE_LEFT}
        else:
            p.mouse_btn = set()
        self._apply_separation(p, sep_x, sep_y)

    def _ai_aggressive(self, p: Player, target, dx: float, dy: float,
                       dist: float, sep_x: float, sep_y: float) -> None:
        """Aggressive: бежит в упор, стреляет, кидает гранаты."""
        p.keys = set()
        # Бежим к цели
        if abs(dx) > 10:
            p.keys.add(KEY_D if dx > 0 else KEY_A)
        if abs(dy) > 10:
            p.keys.add(KEY_S if dy > 0 else KEY_W)
        # Стрейф для уклонения
        if p.ai_strafe_dir > 0:
            p.keys.add(KEY_A if dy >= 0 else KEY_D)
        else:
            p.keys.add(KEY_D if dy >= 0 else KEY_A)
        # Стрельба
        p.mouse_x = int(target.x)
        p.mouse_y = int(target.y)
        p.mouse_btn = {MOUSE_LEFT} if dist < 400 else set()
        # Гранаты если близко и есть
        if dist < 150 and p.grenades > 0 and p.grenade_cooldown <= 0:
            p.mouse_x = int(target.x)
            p.mouse_y = int(target.y)
            self._throw_grenade(p)
        self._apply_separation(p, sep_x, sep_y)

    def _ai_sniper(self, p: Player, target, dx: float, dy: float,
                   dist: float, sep_x: float, sep_y: float) -> None:
        """Sniper: держит дистанцию 300px, стреляет аккуратно, экономит гранаты."""
        p.keys = set()
        ideal_dist = 300
        if dist > ideal_dist + 50:
            # Подходим
            if abs(dx) > 10:
                p.keys.add(KEY_D if dx > 0 else KEY_A)
            if abs(dy) > 10:
                p.keys.add(KEY_S if dy > 0 else KEY_W)
        elif dist < ideal_dist - 50:
            # Отходим
            if abs(dx) > 10:
                p.keys.add(KEY_A if dx > 0 else KEY_D)
            if abs(dy) > 10:
                p.keys.add(KEY_W if dy > 0 else KEY_S)
        else:
            # Стрейф на месте
            if p.ai_strafe_dir > 0:
                p.keys.add(KEY_A)
            else:
                p.keys.add(KEY_D)
        # Стрельба с упреждением
        lead = min(dist / 200, 1.5)
        p.mouse_x = int(target.x + getattr(target, "vx", 0) * lead * 5)
        p.mouse_y = int(target.y + getattr(target, "vy", 0) * lead * 5)
        p.mouse_btn = {MOUSE_LEFT} if dist < 450 else set()
        self._apply_separation(p, sep_x, sep_y)

    def _ai_flanker(self, p: Player, target, dx: float, dy: float,
                   dist: float, sep_x: float, sep_y: float) -> None:
        """Flanker: пытается зайти сбоку/сзади цели, агрессивный стрейф."""
        p.keys = set()
        # Движение перпендикулярно к цели (боковое)
        perp_x = -dy
        perp_y = dx
        perp_len = math.hypot(perp_x, perp_y)
        if perp_len > 0:
            perp_x /= perp_len
            perp_y /= perp_len
        # Меняем сторону каждые ~50 тиков
        if p.ai_strafe_dir > 0:
            p.keys.add(KEY_D if perp_x > 0 else KEY_A)
            p.keys.add(KEY_S if perp_y > 0 else KEY_W)
        else:
            p.keys.add(KEY_A if perp_x > 0 else KEY_D)
            p.keys.add(KEY_W if perp_y > 0 else KEY_S)
        # Если далеко — подходим
        if dist > 200:
            p.keys.add(KEY_D if dx > 0 else KEY_A)
            p.keys.add(KEY_S if dy > 0 else KEY_W)
        # Стрельба
        p.mouse_x = int(target.x)
        p.mouse_y = int(target.y)
        p.mouse_btn = {MOUSE_LEFT} if dist < 350 else set()
        self._apply_separation(p, sep_x, sep_y)

    def _ai_defensive(self, p: Player, target, dx: float, dy: float,
                      dist: float, sep_x: float, sep_y: float) -> None:
        """Defensive: держит дистанцию 200px, отступает при контратаке."""
        p.keys = set()
        ideal_dist = 200
        if dist > ideal_dist + 50:
            if abs(dx) > 10:
                p.keys.add(KEY_D if dx > 0 else KEY_A)
            if abs(dy) > 10:
                p.keys.add(KEY_S if dy > 0 else KEY_W)
        elif dist < ideal_dist - 30:
            # Отходим
            if abs(dx) > 10:
                p.keys.add(KEY_A if dx > 0 else KEY_D)
            if abs(dy) > 10:
                p.keys.add(KEY_W if dy > 0 else KEY_S)
        # Стрейф
        if p.ai_strafe_dir > 0:
            p.keys.add(KEY_A)
        else:
            p.keys.add(KEY_D)
        # Стрельба
        p.mouse_x = int(target.x)
        p.mouse_y = int(target.y)
        p.mouse_btn = {MOUSE_LEFT} if dist < 400 else set()
        self._apply_separation(p, sep_x, sep_y)

    def _find_nearest_target(self, p: Player, include_players: bool = True) -> object | None:
        """Находит ближайшую цель для AI.
        Приоритет: враги (PvE) > игроки чужой команды (PvP).
        """
        target = None
        target_dist = 999999

        # Сначала ищем врагов (PvE)
        for e in self.enemies:
            if not e.alive:
                continue
            d = math.hypot(e.x - p.x, e.y - p.y)
            if d < target_dist:
                target_dist = d
                target = e

        # Если врагов нет и включён PvP — ищем игроков чужой команды
        if target is None and include_players and self.game_mode != MODE_PVE:
            for other in self.players.values():
                if other is p or not other.alive:
                    continue
                if p.team != TEAM_NONE and other.team == p.team:
                    continue  # тиммейт
                d = math.hypot(other.x - p.x, other.y - p.y)
                if d < target_dist:
                    target_dist = d
                    target = other

        return target

    # ── Обновление сущностей ────────────────────────────────────────

    def _update_bullets(self) -> None:
        for b in self.bullets:
            b.x += b.vx
            b.y += b.vy

            # Стены
            for w in self.walls:
                if not w.alive:
                    continue
                if (w.x <= b.x <= w.x + w.tile_size
                        and w.y <= b.y <= w.y + w.tile_size):
                    # Пуленепробиваемые стены (бетон) — пуля просто исчезает,
                    # урон не наносится
                    if not w.bulletproof:
                        w.hp -= b.damage
                        if w.hp <= 0:
                            w.alive = False
                            self.particles.extend(Particle.spawn(
                                w.x + w.tile_size / 2, w.y + w.tile_size / 2,
                                w_color(), 8
                            ))
                    b.alive = False
                    break

            # Враги (пули игроков)
            if b.is_player and b.alive:
                for e in self.enemies:
                    if not e.alive:
                        continue
                    if (e.x <= b.x <= e.x + e.size
                            and e.y <= b.y <= e.y + e.size):
                        e.hp -= b.damage
                        b.alive = False
                        if e.hp <= 0:
                            e.alive = False
                            owner = self.players.get(b.owner_id)
                            if owner:
                                owner.money += 100
                                owner.kills += 1
                            self.particles.extend(Particle.spawn(
                                e.x + e.size / 2, e.y + e.size / 2,
                                e_color(), 10
                            ))
                        break

            # Игроки (пули врагов)
            if not b.is_player and b.alive:
                for p in self.players.values():
                    if not p.alive:
                        continue
                    if (p.x <= b.x <= p.x + p.size
                            and p.y <= b.y <= p.y + p.size):
                        p.hp -= b.damage
                        b.alive = False
                        if p.hp <= 0:
                            p.alive = False
                            p.deaths += 1
                        break

            # PvP: пули игроков могут попасть в игроков другой команды
            if b.is_player and b.alive and self._has_pvp():
                owner = self.players.get(b.owner_id)
                for p in self.players.values():
                    if p is owner or not p.alive:
                        continue
                    if owner and p.team != TEAM_NONE and p.team == owner.team:
                        continue  # тиммейт
                    if (p.x <= b.x <= p.x + p.size
                            and p.y <= b.y <= p.y + p.size):
                        p.hp -= b.damage
                        b.alive = False
                        if p.hp <= 0:
                            p.alive = False
                            p.deaths += 1
                            if owner:
                                owner.kills += 1
                                owner.money += 50
                        break

            if not (0 <= b.x <= FIELD_W and 0 <= b.y <= FIELD_H):
                b.alive = False

        self.bullets = [b for b in self.bullets if b.alive]

    def _update_grenades(self) -> None:
        for g in self.grenades:
            g.x += g.vx
            g.y += g.vy
            g.timer -= 1
            # Отскок от стен
            for w in self.walls:
                if not w.alive:
                    continue
                if (w.x <= g.x <= w.x + w.tile_size
                        and w.y <= g.y <= w.y + w.tile_size):
                    g.vx *= -0.6
                    g.vy *= -0.6
            if g.timer <= 0:
                self._explode(g.x, g.y, owner_id=g.owner_id)
                g.alive = False
        self.grenades = [g for g in self.grenades if g.alive]

    def _explode(self, x: float, y: float, owner_id: str = "") -> None:
        self.explosions.append(Explosion(x=x, y=y))
        # Стены
        for w in self.walls:
            if not w.alive:
                continue
            wc = w.x + w.tile_size / 2
            wh = w.y + w.tile_size / 2
            dist = math.hypot(wc - x, wh - y)
            if dist < 100:
                w.hp -= 100 * (1 - dist / 100)
                if w.hp <= 0:
                    w.alive = False
                    self.particles.extend(Particle.spawn(wc, wh, w_color(), 6))
        # Враги
        for e in self.enemies:
            if not e.alive:
                continue
            ec = e.x + e.size / 2
            ey = e.y + e.size / 2
            dist = math.hypot(ec - x, ey - y)
            if dist < 100:
                e.hp -= 120 * (1 - dist / 100)
                if e.hp <= 0:
                    e.alive = False
                    owner = self.players.get(owner_id)
                    if owner:
                        owner.money += 100
                        owner.kills += 1
                    self.particles.extend(Particle.spawn(ec, ey, e_color(), 10))
        # Игроки (половина урона)
        for p in self.players.values():
            if not p.alive:
                continue
            pc = p.x + p.size / 2
            py = p.y + p.size / 2
            dist = math.hypot(pc - x, py - y)
            if dist < 100:
                p.hp -= 40 * (1 - dist / 100)
                if p.hp <= 0:
                    p.alive = False
                    p.deaths += 1

    def _update_enemies(self) -> None:
        """ИИ врагов: преследуют ближайшего игрока, стреляют."""
        for e in self.enemies:
            if not e.alive:
                continue
            # Ищем ближайшего живого игрока
            target = None
            target_dist = 999999
            for p in self.players.values():
                if not p.alive:
                    continue
                d = math.hypot(p.x - e.x, p.y - e.y)
                if d < target_dist:
                    target_dist = d
                    target = p
            if target is None:
                continue

            dx = (target.x + target.size / 2) - (e.x + e.size / 2)
            dy = (target.y + target.size / 2) - (e.y + e.size / 2)
            dist = math.hypot(dx, dy)
            if dist > 10:
                dx /= dist
                dy /= dist
                nx = e.x + dx * e.speed
                ny = e.y + dy * e.speed
                if not rect_hits_wall(self.walls, nx, e.y, e.size):
                    e.x = nx
                if not rect_hits_wall(self.walls, e.x, ny, e.size):
                    e.y = ny

            if dist < 350 and e.shoot_cooldown <= 0:
                angle = math.atan2(
                    target.y + target.size / 2 - (e.y + e.size / 2),
                    target.x + target.size / 2 - (e.x + e.size / 2),
                )
                self.bullets.append(Bullet.from_angle(
                    e.x + e.size / 2, e.y + e.size / 2,
                    angle, 6, 12, is_player=False
                ))
                e.shoot_cooldown = max(30, 100 - self.wave * 3)
            if e.shoot_cooldown > 0:
                e.shoot_cooldown -= 1

    def _update_explosions(self) -> None:
        for ex in self.explosions:
            ex.r += 10
            if ex.r >= ex.max_r:
                ex.alive = False
        self.explosions = [ex for ex in self.explosions if ex.alive]

    def _update_particles(self) -> None:
        for pt in self.particles:
            pt.x += pt.vx
            pt.y += pt.vy
            pt.life -= 1
        self.particles = [pt for pt in self.particles if pt.life > 0]

    # ── Спавн и волны ──────────────────────────────────────────────

    def _generate_level(self) -> None:
        """Границы + случайные внутренние стены с разными типами блоков.

        Типы: wood (слабый), brick (обычный), concrete (пуленепробиваемый).
        Границы всегда brick. Внутренние — случайно с весами.
        """
        # Границы карты
        for x in range(COLS):
            self.walls.append(Wall(gx=x, gy=0, block_type="brick"))
            self.walls.append(Wall(gx=x, gy=ROWS - 1, block_type="brick"))
        for y in range(1, ROWS - 1):
            self.walls.append(Wall(gx=0, gy=y, block_type="brick"))
            self.walls.append(Wall(gx=COLS - 1, gy=y, block_type="brick"))

        # Случайные внутренние стены с разными типами
        for _ in range(90):
            gx = random.randint(2, COLS - 3)
            gy = random.randint(2, ROWS - 3)
            if not any(w.gx == gx and w.gy == gy for w in self.walls):
                # Выбор типа блока: 50% brick, 30% wood, 20% concrete
                roll = random.random()
                if roll < 0.5:
                    bt = "brick"
                elif roll < 0.8:
                    bt = "wood"
                else:
                    bt = "concrete"
                wall = Wall(gx=gx, gy=gy, block_type=bt)
                # Override HP если задан
                if self.wall_hp_override > 0:
                    wall.hp = self.wall_hp_override
                    wall.max_hp = self.wall_hp_override
                self.walls.append(wall)

    def _spawn_wave(self) -> None:
        count = 2 + self.wave
        players_list = list(self.players.values())
        for _ in range(count):
            x, y = find_spawn_point(self.walls, players_list, avoid_radius=200)
            self.enemies.append(Enemy(x=x, y=y, wave=self.wave))

    def _check_wave_complete(self) -> None:
        # В PvP-режиме нет волн — пропускаем
        if self.game_mode == MODE_PVP:
            return
        if not any(e.alive for e in self.enemies):
            self.wave += 1
            for p in self.players.values():
                if p.alive:
                    p.hp = min(p.hp + 25, p.max_hp)
                    p.money += 50
            self._spawn_wave()

    def _respawn_dead_players(self) -> None:
        """Респавн мёртвых игроков через 5 секунд (300 тиков)."""
        for p in self.players.values():
            if not p.alive and not p.is_bot:
                # Считаем через tick_count; упрощённо — респавним сразу
                # если прошло достаточно. Полная реализация: хранить death_ts.
                # Пока: респавн через 3 секунды (180 тиков)
                if not hasattr(p, "_death_tick"):
                    p._death_tick = self.tick_count
                if self.tick_count - p._death_tick > 180:
                    p.alive = True
                    p.hp = p.max_hp
                    p.x, p.y = find_spawn_point(self.walls,
                                                 list(self.players.values()))
                    delattr(p, "_death_tick")
            elif p.alive and hasattr(p, "_death_tick"):
                delattr(p, "_death_tick")

    # ── Магазин ────────────────────────────────────────────────────

    def _shop_buy(self, p: Player, item: str) -> None:
        if item == "rifle" and not p.has_rifle and p.money >= 500:
            p.money -= 500
            p.has_rifle = True
            p.weapon = "rifle"
        elif item == "grenade" and p.money >= 200:
            p.money -= 200
            p.grenades += 1
        elif item == "heal" and p.money >= 300:
            p.money -= 300
            p.hp = min(p.hp + 50, p.max_hp)

    # ── Утилиты ────────────────────────────────────────────────────

    def _has_pvp(self) -> bool:
        """True если в сессии ≥2 разных команд."""
        teams = {p.team for p in self.players.values() if p.alive and p.team != TEAM_NONE}
        return len(teams) >= 2

    def team_count(self, team: str) -> int:
        """Сколько игроков (включая ботов) в команде."""
        return sum(1 for p in self.players.values() if p.team == team)

    def team_has_space(self, team: str) -> bool:
        """Есть ли свободный слот в команде."""
        if team == TEAM_RED:
            return self.team_count(TEAM_RED) < self.team_red_size
        if team == TEAM_BLUE:
            return self.team_count(TEAM_BLUE) < self.team_blue_size
        return True  # TEAM_NONE — всегда можно

    def start_game(self) -> bool:
        """Переводит сессию из lobby в playing. Заполняет пустые слоты ботами."""
        # Заполняем пустые слоты ботами. max_players теперь всегда >=
        # team_red_size + team_blue_size (см. __init__), но на всякий случай
        # ловим RuntimeError, чтобы одна проблемная сессия не роняла обработку
        # HTTP-запроса (иначе кнопка "Старт игры" молча зависает).
        try:
            while self.team_count(TEAM_RED) < self.team_red_size:
                self.add_bot(team=TEAM_RED)
            while self.team_count(TEAM_BLUE) < self.team_blue_size:
                self.add_bot(team=TEAM_BLUE)
        except RuntimeError:
            pass
        self.game_state = "playing"
        self.game_started = True
        # Респавним всех на стартовых позициях
        self._respawn_all()
        return True

    def fill_bots(self) -> int:
        """Заполняет все пустые слоты ботами. Возвращает сколько добавил."""
        added = 0
        try:
            while self.team_count(TEAM_RED) < self.team_red_size:
                self.add_bot(team=TEAM_RED)
                added += 1
            while self.team_count(TEAM_BLUE) < self.team_blue_size:
                self.add_bot(team=TEAM_BLUE)
                added += 1
        except RuntimeError:
            pass
        return added

    def _respawn_all(self) -> None:
        """Респавнит всех игроков на стартовые позиции (Red — слева, Blue — справа)."""
        red_idx = 0
        blue_idx = 0
        for p in self.players.values():
            if p.team == TEAM_RED:
                # Red — левый край карты
                p.x = 60.0 + (red_idx % 3) * 50
                p.y = 60.0 + (red_idx // 3) * 50
                red_idx += 1
            elif p.team == TEAM_BLUE:
                # Blue — правый край карты
                p.x = FIELD_W - 90.0 - (blue_idx % 3) * 50
                p.y = FIELD_H - 90.0 - (blue_idx // 3) * 50
                blue_idx += 1
            p.hp = p.max_hp
            p.alive = True

    def to_snapshot(self) -> dict:
        """Сериализует состояние для отправки клиентам."""
        return {
            "session_id": self.session_id,
            "tick": self.tick_count,
            "wave": self.wave,
            "game_state": self.game_state,
            "game_mode": self.game_mode,
            "team_red_size": self.team_red_size,
            "team_blue_size": self.team_blue_size,
            "players": [p.to_dict() for p in self.players.values()],
            "enemies": [e.to_dict() for e in self.enemies if e.alive],
            "bullets": [b.to_dict() for b in self.bullets],
            "grenades": [g.to_dict() for g in self.grenades],
            "explosions": [ex.to_dict() for ex in self.explosions],
            "walls": [w.to_dict() for w in self.walls if w.alive],
            "particles": [pt.to_dict() for pt in self.particles],
            "pvp": self._has_pvp(),
        }


def w_color():
    """Цвет частиц разрушаемой стены."""
    from lib.games.voxel.entities import QColor
    return QColor(140, 140, 140)


def e_color():
    """Цвет частиц убитого врага."""
    from lib.games.voxel.entities import QColor
    return QColor(200, 50, 50)
