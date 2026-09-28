from __future__ import annotations

import time
from pathlib import Path

from lib.bots.base import BaseBot


class NightShiftBot(BaseBot):
    """Бот для игры 'Ночная смена' - вампиры vs охотники."""

    def __init__(self, data_dir: Path):
        super().__init__("night_shift", "Ночная Смена", data_dir)

        # Настройки игры
        self._hunt_cooldown = 5 * 60  # 5 минут
        self._duel_cooldown = 15 * 60  # 15 минут
        self._rank_threshold = 100  # ресурс для нового ранга
        self._max_rank = 5
        self._daily_interval = 24 * 60 * 60  # 24 часа

        # Инициализируем глобальные данные если нет
        if not self.get_data("initialized"):
            self.set_data("initialized", True)
            self.set_data("daily_reset", int(time.time()))
            self.set_data("faction_bonus", {})  # бонус к броскам для победившей фракции
            self.set_data("last_summary", "")  # последний текст итогов

    def process_command(self, sender: str, command: str, args: list[str]) -> str:
        """Обрабатывает команды игры."""
        command = command.lower()

        if command == "!join":
            return self._cmd_join(sender, args)
        elif command == "!hunt":
            return self._cmd_hunt(sender)
        elif command == "!duel":
            return self._cmd_duel(sender, args)
        elif command == "!top":
            return self._cmd_top()
        elif command == "!faction":
            return self._cmd_faction()
        elif command == "!summary":
            return self._cmd_summary()
        elif command == "!help":
            return self.get_help()
        else:
            return "❌ Неизвестная команда. Напиши !help для справки."

    def _cmd_join(self, sender: str, args: list[str]) -> str:
        """Выбор фракции: !join vampire или !join hunter."""
        if not args:
            return "❌ Укажи фракцию: !join vampire или !join hunter"

        faction = args[0].lower()
        if faction not in ("vampire", "hunter"):
            return "❌ Неверная фракция. Выбери: vampire или hunter"

        player = self.get_player_data(sender)

        if player.get("faction"):
            return f"❌ Ты уже в фракции {player['faction']}. Смена невозможна."

        player["faction"] = faction
        player["resource"] = 0
        player["rank"] = 1
        player["last_hunt"] = 0
        player["last_duel"] = 0
        player["wins"] = 0
        player["losses"] = 0

        self.set_player_data(sender, player)

        emoji = "🦇" if faction == "vampire" else "✝️"
        return f"{emoji} Добро пожаловать в {faction}! Твой ранг: 1. Напиши !hunt чтобы начать фармить."

    def _cmd_hunt(self, sender: str) -> str:
        """Фарм ресурсов: !hunt (кулдаун 5 мин)."""
        player = self.get_player_data(sender)

        if not player.get("faction"):
            return "❌ Сначала выбери фракцию: !join vampire или !join hunter"

        now = int(time.time())
        last_hunt = player.get("last_hunt", 0)

        if now - last_hunt < self._hunt_cooldown:
            remaining = self._hunt_cooldown - (now - last_hunt)
            mins = remaining // 60
            return f"⏳ Кулдаун! Подожди ещё {mins} мин."

        # Проверяем ежедневный сброс
        self._check_daily_reset()

        # Бросок d20 + бонус ранга + бонус фракции
        roll = self.roll_d20()
        rank_bonus = player.get("rank", 1) - 1
        faction_bonus = self._get_faction_bonus(player.get("faction"))
        total = roll + rank_bonus + faction_bonus

        faction = player["faction"]
        resource_name = "Кровь" if faction == "vampire" else "Репутация"
        emoji = "🦇" if faction == "vampire" else "✝️"

        result_text = f"{emoji} Бросок: {roll} + {rank_bonus}"
        if faction_bonus > 0:
            result_text += f" + {faction_bonus} (бонус фракции)"
        result_text += f" = {total}\n"

        if total <= 5:
            # Провал
            loss = 10
            player["resource"] = max(0, player["resource"] - loss)
            result_text += f"💀 Провал! Теряешь {loss} {resource_name}."
        elif total <= 15:
            # Успех
            gain = 15 + (rank_bonus * 5)
            player["resource"] += gain
            result_text += f"✨ Успех! +{gain} {resource_name}."
        else:
            # Крит
            gain = 30 + (rank_bonus * 10)
            player["resource"] += gain
            # Проверка на повышение ранга
            new_rank = self._check_rank_up(player)
            result_text += f"🌟 КРИТ! +{gain} {resource_name}!"
            if new_rank:
                result_text += f" Новый ранг: {new_rank}!"

        player["last_hunt"] = now
        self.set_player_data(sender, player)

        return result_text

    def _cmd_duel(self, sender: str, args: list[str]) -> str:
        """PvP дуэль: !duel @username."""
        if not args:
            return "❌ Укажи оппонента: !duel @username"

        opponent = args[0].lstrip("@")

        if opponent == sender:
            return "❌ Нельзя дуэлировать с собой!"

        player = self.get_player_data(sender)
        target = self.get_player_data(opponent)

        if not player.get("faction"):
            return "❌ Сначала выбери фракцию: !join vampire или !join hunter"

        if not target.get("faction"):
            return f"❌ {opponent} ещё не выбрал фракцию."

        now = int(time.time())
        last_duel = player.get("last_duel", 0)

        if now - last_duel < self._duel_cooldown:
            remaining = self._duel_cooldown - (now - last_duel)
            mins = remaining // 60
            return f"⏳ Кулдаун дуэли! Подожди ещё {mins} мин."

        # Броски обоих игроков
        player_roll = self.roll_d20() + (player.get("rank", 1) - 1)
        target_roll = self.roll_d20() + (target.get("rank", 1) - 1)

        result = f"⚔️ {sender} vs {opponent}\n"
        result += f"🎲 {sender}: {player_roll}\n"
        result += f"🎲 {opponent}: {target_roll}\n"

        if player_roll > target_roll:
            # Победа игрока
            steal = int(target["resource"] * 0.1)
            target["resource"] -= steal
            player["resource"] += steal
            player["wins"] = player.get("wins", 0) + 1
            target["losses"] = target.get("losses", 0) + 1

            result += f"🏆 {sender} победил! Украл {steal} ресурса."
        elif target_roll > player_roll:
            # Победа оппонента
            steal = int(player["resource"] * 0.1)
            player["resource"] -= steal
            target["resource"] += steal
            target["wins"] = target.get("wins", 0) + 1
            player["losses"] = player.get("losses", 0) + 1

            result += f"🏆 {opponent} победил! Украл {steal} ресурса."
        else:
            # Ничья
            result += "🤝 Ничья! Никто ничего не потерял."

        player["last_duel"] = now
        self.set_player_data(sender, player)
        self.set_player_data(opponent, target)

        return result

    def _cmd_top(self) -> str:
        """Лидерборд по ресурсу."""
        players_data = self.get_data("players", {})

        if not players_data:
            return "📊 Лидерборд пуст. Напиши !join чтобы начать!"

        # Сортируем по ресурсу
        sorted_players = sorted(
            players_data.items(), key=lambda x: x[1].get("resource", 0), reverse=True
        )[:10]  # Топ-10

        result = "📊 Топ-10 игроков:\n"
        for i, (name, data) in enumerate(sorted_players, 1):
            faction = data.get("faction", "?")
            emoji = "🦇" if faction == "vampire" else "✝️"
            resource = data.get("resource", 0)
            rank = data.get("rank", 1)
            result += f"{i}. {emoji} {name} - {resource} (ранг {rank})\n"

        return result

    def _cmd_faction(self) -> str:
        """Счёт фракций."""
        players_data = self.get_data("players", {})

        vampire_total = sum(
            p.get("resource", 0) for p in players_data.values() if p.get("faction") == "vampire"
        )
        hunter_total = sum(
            p.get("resource", 0) for p in players_data.values() if p.get("faction") == "hunter"
        )

        vampire_count = sum(1 for p in players_data.values() if p.get("faction") == "vampire")
        hunter_count = sum(1 for p in players_data.values() if p.get("faction") == "hunter")

        result = "⚔️ Счёт фракций:\n"
        result += f"🦇 Вампиры: {vampire_total} ({vampire_count} игроков)\n"
        result += f"✝️ Охотники: {hunter_total} ({hunter_count} игроков)\n"

        if vampire_total > hunter_total:
            result += "🌙 Вампиры лидируют!"
        elif hunter_total > vampire_total:
            result += "☀️ Охотники лидируют!"
        else:
            result += "⚖️ Паритет!"

        return result

    def _check_rank_up(self, player: dict) -> int | None:
        """Проверяет повышение ранга. Возвращает новый ранг или None."""
        current_rank = player.get("rank", 1)
        if current_rank >= self._max_rank:
            return None

        resource = player.get("resource", 0)
        required = current_rank * self._rank_threshold

        if resource >= required:
            player["rank"] = current_rank + 1
            return player["rank"]

        return None

    def get_help(self) -> str:
        """Справка по командам."""
        return """
🌙 Ночная Смена - команды:
!join vampire/hunter - Выбрать фракцию (разово)
!hunt - Фармить ресурс (кулдаун 5 мин)
!duel @user - Дуэль с игроком (кулдаун 15 мин)
!top - Лидерборд игроков
!faction - Счёт фракций
!summary - Итоги последней ночи
!help - Эта справка

🦇 Вампиры фармят Кровь
✝️ Охотники фармят Репутацию

Каждые 100 ресурса = новый ранг (+1 к броску)
Максимум 5 рангов.
Победившая фракция получает +2 к броскам на сутки.
        """.strip()

    def _check_daily_reset(self) -> None:
        """Проверяет и выполняет ежедневный сброс."""
        now = int(time.time())
        last_reset = self.get_data("daily_reset", 0)

        if now - last_reset >= self._daily_interval:
            # Выполняем ежедневный сброс
            self._perform_daily_reset()
            self.set_data("daily_reset", now)

    def _perform_daily_reset(self) -> None:
        """Выполняет ежедневный подсчёт итогов."""
        players_data = self.get_data("players", {})

        # Считаем ресурсы фракций
        vampire_total = sum(
            p.get("resource", 0) for p in players_data.values() if p.get("faction") == "vampire"
        )
        hunter_total = sum(
            p.get("resource", 0) for p in players_data.values() if p.get("faction") == "hunter"
        )

        # Определяем победителя
        if vampire_total > hunter_total:
            winner = "vampire"
            winner_emoji = "🦇"
            winner_name = "Вампиры"
        elif hunter_total > vampire_total:
            winner = "hunter"
            winner_emoji = "✝️"
            winner_name = "Охотники"
        else:
            winner = None
            winner_emoji = "⚖️"
            winner_name = "Никто"

        # Сохраняем бонус для победившей фракции
        if winner:
            self.set_data("faction_bonus", {"faction": winner, "bonus": 2})
        else:
            self.set_data("faction_bonus", {})

        # Формируем текст итогов
        summary = "🌙 Ночь окончена!\n"
        summary += f"🦇 Вампиры: {vampire_total}\n"
        summary += f"✝️ Охотники: {hunter_total}\n"

        if winner:
            summary += f"\n🏆 Победитель: {winner_emoji} {winner_name}!\n"
            summary += "Бонус: +2 к броскам на следующие 24 часа."
        else:
            summary += "\n⚖️ Ничья! Бонусов нет."

        self.set_data("last_summary", summary)

    def _get_faction_bonus(self, faction: str) -> int:
        """Возвращает бонус для фракции."""
        bonus_data = self.get_data("faction_bonus", {})
        if bonus_data.get("faction") == faction:
            return bonus_data.get("bonus", 0)
        return 0

    def _cmd_summary(self) -> str:
        """Показывает итоги последней ночи."""
        summary = self.get_data("last_summary", "")
        if not summary:
            return "📊 Ещё не было итогов. Первые итоги через 24 часа после начала игры."
        return summary
