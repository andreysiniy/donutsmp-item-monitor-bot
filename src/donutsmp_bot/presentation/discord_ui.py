from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..application.monitoring import MonitoringCoordinator
from ..application.services import (
    AlertEvent,
    AuthService,
    InvalidItemError,
    InvalidThresholdError,
    NotAuthenticatedError,
    NotificationSender,
    PriceService,
    WatchService,
)
from ..core.enums import Condition, PriceType, TokenStatus
from ..domain.schemas import AuctionListing, PriceSnapshot
from ..infrastructure.donut_api import (
    DonutApiClient,
    DonutAuthenticationError,
    DonutRateLimitError,
    DonutTransientError,
    format_decimal_price,
)
from ..infrastructure.icons import IconService
from ..persistence.models import WatchRule
from ..persistence.repositories import UserRepository, WatchRuleRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppServices:
    session_factory: async_sessionmaker[AsyncSession]
    auth: AuthService
    watches: WatchService
    prices: PriceService
    api: DonutApiClient
    icons: IconService
    monitoring: MonitoringCoordinator


class AuthModal(discord.ui.Modal, title="Авторизация DonutSMP"):
    token: discord.ui.TextInput[AuthModal] = discord.ui.TextInput(
        label="Bearer-токен",
        placeholder="Вставьте токен DonutSMP",
        required=True,
        min_length=1,
        max_length=2000,
    )

    def __init__(self, auth_service: AuthService) -> None:
        super().__init__(timeout=300)
        self.auth_service = auth_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.auth_service.authenticate(interaction.user.id, str(self.token))
        except DonutAuthenticationError:
            await interaction.edit_original_response(
                content="Токен отклонён DonutSMP. Проверьте его и повторите `/auth`."
            )
        except DonutRateLimitError as exc:
            await interaction.edit_original_response(
                content=(
                    f"DonutSMP временно ограничил запросы. Повторите через {exc.retry_after:.0f} с."
                )
            )
        except DonutTransientError:
            await interaction.edit_original_response(
                content="DonutSMP сейчас недоступен. Токен не сохранён; попробуйте позже."
            )
        else:
            await interaction.edit_original_response(
                content="Авторизация успешна. Токен проверен и сохранён в зашифрованном виде."
            )


class LogoutView(discord.ui.View):
    def __init__(self, owner_id: int, auth_service: AuthService) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.auth_service = auth_service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
        return False

    @discord.ui.button(label="Удалить токен и правила", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        deleted = await self.auth_service.logout(self.owner_id)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        message = (
            "Токен и все правила мониторинга удалены."
            if deleted
            else "Сохранённого токена уже нет."
        )
        await interaction.response.edit_message(content=message, view=self)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="Удаление отменено.", view=self)


class AlertActionsView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        rule_id: int,
        item_id: str,
        price_type: PriceType,
        watches: WatchService,
        prices: PriceService,
        icons: IconService,
    ) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.rule_id = rule_id
        self.item_id = item_id
        self.price_type = price_type
        self.watches = watches
        self.prices = prices
        self.icons = icons

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Это уведомление не для вас.", ephemeral=True)
        return False

    @discord.ui.button(
        label="Остановить наблюдение",
        style=discord.ButtonStyle.secondary,
        custom_id="donutsmp:pause",
    )
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        changed = await self.watches.set_enabled(self.owner_id, self.rule_id, enabled=False)
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Правило приостановлено." if changed else "Правило уже недоступно.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Удалить правило",
        style=discord.ButtonStyle.danger,
        custom_id="donutsmp:delete",
    )
    async def delete(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        deleted = await self.watches.delete(self.owner_id, self.rule_id)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Правило удалено." if deleted else "Правило уже удалено.", ephemeral=True
        )

    @discord.ui.button(
        label="Показать текущую цену",
        style=discord.ButtonStyle.primary,
        custom_id="donutsmp:price",
    )
    async def current_price(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await self.prices.get(self.owner_id, self.item_id, self.price_type)
        except Exception as exc:
            await interaction.followup.send(_friendly_error(exc), ephemeral=True)
            return
        await interaction.followup.send(
            embed=_price_embed(snapshot, self.icons.display_name(self.item_id)),
            ephemeral=True,
        )


class DiscordNotificationSender(NotificationSender):
    def __init__(self, bot: commands.Bot, icons: IconService) -> None:
        self.bot = bot
        self.icons = icons
        self._watches: WatchService | None = None
        self._prices: PriceService | None = None

    def bind_actions(self, watches: WatchService, prices: PriceService) -> None:
        self._watches = watches
        self._prices = prices

    async def send_alert(self, event: AlertEvent) -> int:
        if self._watches is None or self._prices is None:
            raise RuntimeError("Notification actions are not configured")
        user = self.bot.get_user(event.discord_user_id)
        if user is None:
            user = await self.bot.fetch_user(event.discord_user_id)
        icon_path = self.icons.icon_path(event.item_id)
        file = discord.File(icon_path, filename=icon_path.name)
        embed = _alert_embed(event)
        embed.set_thumbnail(url=f"attachment://{icon_path.name}")
        view = AlertActionsView(
            owner_id=event.discord_user_id,
            rule_id=event.rule_id,
            item_id=event.item_id,
            price_type=event.snapshot.price_type,
            watches=self._watches,
            prices=self._prices,
            icons=self.icons,
        )
        message = await user.send(embed=embed, file=file, view=view)
        return message.id

    async def send_invalid_token(self, discord_user_id: int) -> None:
        user = self.bot.get_user(discord_user_id)
        if user is None:
            user = await self.bot.fetch_user(discord_user_id)
        await user.send(
            "DonutSMP отклонил сохранённый токен. Все правила приостановлены; выполните `/auth`."
        )


class DonutCommands(commands.Cog):
    watch = app_commands.Group(name="watch", description="Управление наблюдениями")

    def __init__(self, services: AppServices) -> None:
        self.services = services

    @app_commands.command(name="start", description="Инструкция и состояние авторизации")
    async def start(self, interaction: discord.Interaction) -> None:
        user = await self.services.auth.get_user(interaction.user.id)
        authorized = user is not None and user.token_status is TokenStatus.VALID
        state = "авторизован" if authorized else "не авторизован"
        await interaction.response.send_message(
            "Бот следит за минимальной ценой лотов DonutSMP и пишет в личные сообщения "
            "только при пересечении порога.\n\n"
            f"Состояние: **{state}**\n"
            "1. `/auth` — сохранить токен.\n"
            "2. `/watch add` — создать правило.\n"
            "3. `/watch list` — посмотреть правила.\n"
            "4. `/price` — узнать цену без правила.",
            ephemeral=True,
        )

    @app_commands.command(name="auth", description="Сохранить Bearer-токен DonutSMP")
    async def auth(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AuthModal(self.services.auth))

    @app_commands.command(name="logout", description="Удалить токен и все правила")
    async def logout(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Удалить сохранённый токен и все связанные правила?",
            view=LogoutView(interaction.user.id, self.services.auth),
            ephemeral=True,
        )

    @watch.command(name="add", description="Добавить правило наблюдения")
    @app_commands.describe(
        item="ID предмета Minecraft",
        condition="Направление пересечения",
        threshold="Ценовой порог",
        price_type="Цена лота или одной единицы",
    )
    @app_commands.choices(
        condition=[
            app_commands.Choice(name="Цена не выше", value=Condition.PRICE_DOWN.value),
            app_commands.Choice(name="Цена не ниже", value=Condition.PRICE_UP.value),
        ],
        price_type=[
            app_commands.Choice(name="Цена всего лота", value=PriceType.TOTAL.value),
            app_commands.Choice(name="Цена за единицу", value=PriceType.PER_ITEM.value),
        ],
    )
    async def watch_add(
        self,
        interaction: discord.Interaction,
        item: str,
        condition: app_commands.Choice[str],
        threshold: app_commands.Range[int, 1],
        price_type: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            rule, snapshot = await self.services.watches.add(
                discord_user_id=interaction.user.id,
                item_id=item,
                condition=Condition(condition.value),
                threshold=threshold,
                price_type=PriceType(price_type.value),
            )
        except Exception as exc:
            await interaction.edit_original_response(content=_friendly_error(exc))
            return
        await interaction.edit_original_response(
            content=(
                f"Правило **#{rule.id}** создано: {rule.display_name}, "
                f"{_condition_symbol(rule.condition)} {format_decimal_price(rule.threshold)}.\n"
                f"Текущая цена: **{format_decimal_price(snapshot.selected_price)}**."
            )
        )

    @watch_add.autocomplete("item")
    async def watch_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._item_choices(current)

    @watch.command(name="list", description="Показать правила наблюдения")
    async def watch_list(self, interaction: discord.Interaction) -> None:
        try:
            rules = await self.services.watches.list(interaction.user.id)
        except Exception as exc:
            await interaction.response.send_message(_friendly_error(exc), ephemeral=True)
            return
        if not rules:
            text = "У вас пока нет правил."
        else:
            text = "\n\n".join(_format_rule(rule) for rule in rules)
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @watch.command(name="delete", description="Удалить правило")
    async def watch_delete(self, interaction: discord.Interaction, rule_id: int) -> None:
        await self._change_rule(interaction, rule_id, action="delete")

    @watch.command(name="pause", description="Приостановить правило")
    async def watch_pause(self, interaction: discord.Interaction, rule_id: int) -> None:
        await self._change_rule(interaction, rule_id, action="pause")

    @watch.command(name="resume", description="Возобновить правило")
    async def watch_resume(self, interaction: discord.Interaction, rule_id: int) -> None:
        await self._change_rule(interaction, rule_id, action="resume")

    @watch_delete.autocomplete("rule_id")
    @watch_pause.autocomplete("rule_id")
    @watch_resume.autocomplete("rule_id")
    async def rule_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        try:
            rules = await self.services.watches.list(interaction.user.id)
        except NotAuthenticatedError:
            return []
        needle = str(current).strip()
        return [
            app_commands.Choice(name=f"#{rule.id} {rule.display_name}"[:100], value=rule.id)
            for rule in rules
            if not needle
            or needle in str(rule.id)
            or needle.casefold() in rule.display_name.casefold()
        ][:25]

    @app_commands.command(name="price", description="Получить текущую цену предмета")
    @app_commands.choices(
        price_type=[
            app_commands.Choice(name="Цена всего лота", value=PriceType.TOTAL.value),
            app_commands.Choice(name="Цена за единицу", value=PriceType.PER_ITEM.value),
        ]
    )
    async def price(
        self,
        interaction: discord.Interaction,
        item: str,
        price_type: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await self.services.prices.get(
                interaction.user.id, item, PriceType(price_type.value)
            )
        except Exception as exc:
            await interaction.edit_original_response(content=_friendly_error(exc))
            return
        await interaction.edit_original_response(
            embed=_price_embed(snapshot, self.services.icons.display_name(item))
        )

    @price.autocomplete("item")
    async def price_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._item_choices(current)

    @app_commands.command(name="status", description="Показать состояние сервиса")
    async def status(self, interaction: discord.Interaction) -> None:
        async with self.services.session_factory() as session:
            user = await UserRepository(session).get(interaction.user.id)
            active_rules = await WatchRuleRepository(session).count_active(interaction.user.id)
        authorized = user is not None and user.token_status is TokenStatus.VALID
        fingerprint = user.token_fingerprint if user else None
        remaining = (
            self.services.api.limiter.remaining(fingerprint, interactive=False)
            if fingerprint
            else 0
        )
        reset_at = self.services.api.limiter.budget_reset_at(fingerprint) if fingerprint else None
        health = self.services.api.health
        embed = discord.Embed(title="Состояние DonutSMP Monitor", color=0x2ECC71)
        embed.add_field(name="Авторизация", value="активна" if authorized else "не настроена")
        embed.add_field(name="Активные правила", value=str(active_rules))
        embed.add_field(name="API-бюджет", value=f"≈ {remaining} запросов")
        embed.add_field(
            name="Последний успешный запрос",
            value=_discord_time(user.last_successful_request_at if user else None),
        )
        embed.add_field(
            name="Обновление бюджета",
            value=_discord_time(reset_at),
        )
        embed.add_field(
            name="DonutSMP API",
            value="доступен" if health.available else f"ошибка: {health.last_error}",
        )
        embed.add_field(
            name="Ошибки личных сообщений",
            value=str(user.dm_error_count if user else 0),
        )
        if user and user.last_dm_error:
            embed.add_field(name="Последняя ошибка DM", value=user.last_dm_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _change_rule(
        self, interaction: discord.Interaction, rule_id: int, *, action: str
    ) -> None:
        try:
            if action == "delete":
                changed = await self.services.watches.delete(interaction.user.id, rule_id)
                success = "Правило удалено."
            else:
                changed = await self.services.watches.set_enabled(
                    interaction.user.id, rule_id, enabled=action == "resume"
                )
                success = (
                    "Правило возобновлено." if action == "resume" else "Правило приостановлено."
                )
        except Exception as exc:
            await interaction.response.send_message(_friendly_error(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            success if changed else "Правило не найдено.", ephemeral=True
        )

    def _item_choices(self, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(
                name=f"{entry.display_name} — {entry.item_id}"[:100],
                value=entry.item_id,
            )
            for entry in self.services.icons.autocomplete(current)
        ]

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error(
            "Discord command failed user_id=%s error=%s",
            interaction.user.id,
            type(error).__name__,
        )
        message = "Команда не выполнена из-за внутренней ошибки."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _alert_embed(event: AlertEvent) -> discord.Embed:
    direction = "снизилась" if event.condition is Condition.PRICE_DOWN else "повысилась"
    emoji = "💰" if event.condition is Condition.PRICE_DOWN else "📈"
    embed = discord.Embed(
        title=f"{emoji} Цена {event.display_name} {direction}",
        color=0x2ECC71 if event.condition is Condition.PRICE_DOWN else 0x3498DB,
        timestamp=event.snapshot.checked_at,
    )
    embed.description = (
        f"**Текущая цена:** {format_decimal_price(event.current_price)}\n"
        f"**Заданный порог:** {format_decimal_price(event.threshold)}\n"
        f"**Предыдущая цена:** {format_decimal_price(event.previous_price)}"
    )
    _add_listing_fields(embed, event.snapshot.listing)
    embed.add_field(name="Предмет", value=event.item_id, inline=False)
    embed.add_field(name="Правило", value=f"#{event.rule_id}")
    return embed


def _price_embed(snapshot: PriceSnapshot, display_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Цена {display_name}",
        description=f"**{format_decimal_price(snapshot.selected_price)}**",
        color=0xF1C40F,
        timestamp=snapshot.checked_at,
    )
    _add_listing_fields(embed, snapshot.listing)
    embed.set_footer(text=f"{snapshot.item_id} • проверено страниц: {snapshot.pages_scanned}")
    return embed


def _add_listing_fields(embed: discord.Embed, listing: AuctionListing | None) -> None:
    if listing is None:
        embed.add_field(name="Лоты", value="Подходящих активных лотов нет.")
        return
    embed.add_field(name="Количество", value=str(listing.item.count))
    embed.add_field(name="Общая цена", value=format_decimal_price(listing.price))
    embed.add_field(
        name="Цена за единицу",
        value=format_decimal_price(listing.price / Decimal(listing.item.count)),
    )
    embed.add_field(name="Продавец", value=listing.seller.name)
    embed.add_field(name="Осталось", value=_format_duration(listing.time_left))
    if listing.item.enchants:
        embed.add_field(
            name="Зачарования",
            value=_safe_value(listing.item.enchants),
            inline=False,
        )
    if listing.item.trim:
        embed.add_field(name="Отделка", value=_safe_value(listing.item.trim), inline=False)
    if listing.item.lore:
        embed.add_field(name="Описание", value=_safe_value(listing.item.lore), inline=False)


def _safe_value(value: Any) -> str:
    if isinstance(value, dict):
        text = ", ".join(f"{key}: {item}" for key, item in value.items())
    elif isinstance(value, list):
        text = "\n".join(str(item) for item in value)
    else:
        text = str(value)
    return discord.utils.escape_markdown(text[:1024]) or "—"


def _format_rule(rule: WatchRule) -> str:
    checked = _discord_time(rule.last_checked_at)
    enabled = "активно" if rule.enabled else "приостановлено"
    return (
        f"**#{rule.id} {rule.display_name}**\n"
        f"Условие: цена {_condition_symbol(rule.condition)} "
        f"{format_decimal_price(rule.threshold)}\n"
        f"Текущая цена: {format_decimal_price(rule.last_observed_price)}\n"
        f"Статус: {enabled}\n"
        f"Последняя проверка: {checked}"
    )


def _condition_symbol(condition: Condition) -> str:
    return "≤" if condition is Condition.PRICE_DOWN else "≥"


def _discord_time(value: datetime | None) -> str:
    if value is None:
        return "ещё не было"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return discord.utils.format_dt(value, style="R")


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "неизвестно"
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def _friendly_error(error: Exception) -> str:
    if isinstance(error, NotAuthenticatedError):
        return "Сначала выполните `/auth`."
    if isinstance(error, InvalidItemError):
        return "Предмет отсутствует в Minecraft-манифесте."
    if isinstance(error, InvalidThresholdError):
        return "Порог должен быть положительным числом."
    if isinstance(error, DonutAuthenticationError):
        return "DonutSMP отклонил токен. Выполните `/auth` заново."
    if isinstance(error, DonutRateLimitError):
        return f"Лимит API исчерпан. Повторите через {error.retry_after:.0f} с."
    if isinstance(error, DonutTransientError):
        return "DonutSMP временно недоступен; попробуйте позже."
    if isinstance(error, discord.Forbidden):
        return "Discord запрещает отправку личных сообщений."
    return "Операция не выполнена из-за внутренней ошибки."
