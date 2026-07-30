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
    DonutResponseError,
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


class AuthModal(discord.ui.Modal, title="DonutSMP Authorization"):
    token: discord.ui.TextInput[AuthModal] = discord.ui.TextInput(
        label="Bearer token",
        placeholder="Paste your DonutSMP token",
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
                content="DonutSMP rejected the token. Check it and run `/auth` again."
            )
        except DonutRateLimitError as exc:
            await interaction.edit_original_response(
                content=(
                    "DonutSMP temporarily rate-limited requests. "
                    f"Try again in {exc.retry_after:.0f} seconds."
                )
            )
        except DonutTransientError:
            await interaction.edit_original_response(
                content="DonutSMP is unavailable. The token was not saved; try again later."
            )
        except DonutResponseError:
            await interaction.edit_original_response(
                content=(
                    "DonutSMP returned an unexpected response. "
                    "The token was not saved; try again later."
                )
            )
        except Exception as exc:
            logger.error(
                "Authorization failed user_id=%s error=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await interaction.edit_original_response(
                content="Authorization failed because of an internal error. Try again later."
            )
        else:
            await interaction.edit_original_response(
                content="Authorization succeeded. The verified token was stored encrypted."
            )


class LogoutView(discord.ui.View):
    def __init__(self, owner_id: int, auth_service: AuthService) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.auth_service = auth_service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This confirmation does not belong to you.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Delete token and rules", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        deleted = await self.auth_service.logout(self.owner_id)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        message = (
            "The token and all monitoring rules were deleted."
            if deleted
            else "There is no stored token."
        )
        await interaction.response.edit_message(content=message, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="Deletion cancelled.", view=self)


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
        await interaction.response.send_message(
            "This notification does not belong to you.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Pause monitoring",
        style=discord.ButtonStyle.secondary,
        custom_id="donutsmp:pause",
    )
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        changed = await self.watches.set_enabled(self.owner_id, self.rule_id, enabled=False)
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Rule paused." if changed else "The rule is no longer available.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Delete rule",
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
            "Rule deleted." if deleted else "The rule has already been deleted.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Show current price",
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
            "DonutSMP rejected the stored token. All rules were paused; run `/auth` again."
        )


class DonutCommands(commands.Cog):
    watch = app_commands.Group(name="watch", description="Manage price monitoring rules")

    def __init__(self, services: AppServices) -> None:
        self.services = services

    @app_commands.command(name="start", description="Show instructions and authorization status")
    async def start(self, interaction: discord.Interaction) -> None:
        user = await self.services.auth.get_user(interaction.user.id)
        authorized = user is not None and user.token_status is TokenStatus.VALID
        state = "authorized" if authorized else "not authorized"
        await interaction.response.send_message(
            "The bot monitors the lowest DonutSMP listing price and sends a direct message "
            "only when a configured threshold is crossed.\n\n"
            f"Status: **{state}**\n"
            "1. `/auth` — store a token.\n"
            "2. `/watch add` — create a rule.\n"
            "3. `/watch list` — view your rules.\n"
            "4. `/price` — check a price without creating a rule.",
            ephemeral=True,
        )

    @app_commands.command(name="auth", description="Store a DonutSMP Bearer token")
    async def auth(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AuthModal(self.services.auth))

    @app_commands.command(name="logout", description="Delete the token and all rules")
    async def logout(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Delete the stored token and all associated monitoring rules?",
            view=LogoutView(interaction.user.id, self.services.auth),
            ephemeral=True,
        )

    @watch.command(name="add", description="Add a monitoring rule")
    @app_commands.describe(
        item="Minecraft item ID",
        condition="Threshold crossing direction",
        threshold="Price threshold",
        price_type="Whole listing price or per-item price",
    )
    @app_commands.choices(
        condition=[
            app_commands.Choice(name="Price at or below", value=Condition.PRICE_DOWN.value),
            app_commands.Choice(name="Price at or above", value=Condition.PRICE_UP.value),
        ],
        price_type=[
            app_commands.Choice(name="Whole listing price", value=PriceType.TOTAL.value),
            app_commands.Choice(name="Price per item", value=PriceType.PER_ITEM.value),
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
                f"Rule **#{rule.id}** created: {rule.display_name}, "
                f"{_condition_symbol(rule.condition)} {format_decimal_price(rule.threshold)}.\n"
                f"Current price: **{format_decimal_price(snapshot.selected_price)}**."
            )
        )

    @watch_add.autocomplete("item")
    async def watch_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._item_choices(current)

    @watch.command(name="list", description="Show monitoring rules")
    async def watch_list(self, interaction: discord.Interaction) -> None:
        try:
            rules = await self.services.watches.list(interaction.user.id)
        except Exception as exc:
            await interaction.response.send_message(_friendly_error(exc), ephemeral=True)
            return
        if not rules:
            text = "You do not have any monitoring rules yet."
        else:
            text = "\n\n".join(_format_rule(rule) for rule in rules)
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @watch.command(name="delete", description="Delete a rule")
    async def watch_delete(self, interaction: discord.Interaction, rule_id: int) -> None:
        await self._change_rule(interaction, rule_id, action="delete")

    @watch.command(name="pause", description="Pause a rule")
    async def watch_pause(self, interaction: discord.Interaction, rule_id: int) -> None:
        await self._change_rule(interaction, rule_id, action="pause")

    @watch.command(name="resume", description="Resume a rule")
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

    @app_commands.command(name="price", description="Get the current item price")
    @app_commands.choices(
        price_type=[
            app_commands.Choice(name="Whole listing price", value=PriceType.TOTAL.value),
            app_commands.Choice(name="Price per item", value=PriceType.PER_ITEM.value),
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

    @app_commands.command(name="status", description="Show service status")
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
        embed = discord.Embed(title="DonutSMP Monitor Status", color=0x2ECC71)
        embed.add_field(name="Authorization", value="active" if authorized else "not configured")
        embed.add_field(name="Active rules", value=str(active_rules))
        embed.add_field(name="API budget", value=f"≈ {remaining} requests")
        embed.add_field(
            name="Last successful request",
            value=_discord_time(user.last_successful_request_at if user else None),
        )
        embed.add_field(
            name="Budget reset",
            value=_discord_time(reset_at),
        )
        embed.add_field(
            name="DonutSMP API",
            value="available" if health.available else f"error: {health.last_error}",
        )
        embed.add_field(
            name="Direct message errors",
            value=str(user.dm_error_count if user else 0),
        )
        if user and user.last_dm_error:
            embed.add_field(name="Last DM error", value=user.last_dm_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _change_rule(
        self, interaction: discord.Interaction, rule_id: int, *, action: str
    ) -> None:
        try:
            if action == "delete":
                changed = await self.services.watches.delete(interaction.user.id, rule_id)
                success = "Rule deleted."
            else:
                changed = await self.services.watches.set_enabled(
                    interaction.user.id, rule_id, enabled=action == "resume"
                )
                success = "Rule resumed." if action == "resume" else "Rule paused."
        except Exception as exc:
            await interaction.response.send_message(_friendly_error(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            success if changed else "Rule not found.", ephemeral=True
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
        message = "The command failed because of an internal error."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _alert_embed(event: AlertEvent) -> discord.Embed:
    direction = "dropped" if event.condition is Condition.PRICE_DOWN else "increased"
    emoji = "💰" if event.condition is Condition.PRICE_DOWN else "📈"
    embed = discord.Embed(
        title=f"{emoji} {event.display_name} price {direction}",
        color=0x2ECC71 if event.condition is Condition.PRICE_DOWN else 0x3498DB,
        timestamp=event.snapshot.checked_at,
    )
    embed.description = (
        f"**Current price:** {format_decimal_price(event.current_price)}\n"
        f"**Configured threshold:** {format_decimal_price(event.threshold)}\n"
        f"**Previous price:** {format_decimal_price(event.previous_price)}"
    )
    _add_listing_fields(embed, event.snapshot.listing)
    embed.add_field(name="Item", value=event.item_id, inline=False)
    embed.add_field(name="Rule", value=f"#{event.rule_id}")
    return embed


def _price_embed(snapshot: PriceSnapshot, display_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{display_name} Price",
        description=f"**{format_decimal_price(snapshot.selected_price)}**",
        color=0xF1C40F,
        timestamp=snapshot.checked_at,
    )
    _add_listing_fields(embed, snapshot.listing)
    embed.set_footer(text=f"{snapshot.item_id} • pages checked: {snapshot.pages_scanned}")
    return embed


def _add_listing_fields(embed: discord.Embed, listing: AuctionListing | None) -> None:
    if listing is None:
        embed.add_field(name="Listings", value="No matching active listings.")
        return
    embed.add_field(name="Quantity", value=str(listing.item.count))
    embed.add_field(name="Total price", value=format_decimal_price(listing.price))
    embed.add_field(
        name="Price per item",
        value=format_decimal_price(listing.price / Decimal(listing.item.count)),
    )
    embed.add_field(name="Seller", value=listing.seller.name)
    embed.add_field(name="Time remaining", value=_format_duration(listing.time_left))
    if listing.item.enchants:
        embed.add_field(
            name="Enchantments",
            value=_safe_value(listing.item.enchants),
            inline=False,
        )
    if listing.item.trim:
        embed.add_field(name="Trim", value=_safe_value(listing.item.trim), inline=False)
    if listing.item.lore:
        embed.add_field(name="Lore", value=_safe_value(listing.item.lore), inline=False)


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
    enabled = "active" if rule.enabled else "paused"
    return (
        f"**#{rule.id} {rule.display_name}**\n"
        f"Condition: price {_condition_symbol(rule.condition)} "
        f"{format_decimal_price(rule.threshold)}\n"
        f"Current price: {format_decimal_price(rule.last_observed_price)}\n"
        f"Status: {enabled}\n"
        f"Last check: {checked}"
    )


def _condition_symbol(condition: Condition) -> str:
    return "≤" if condition is Condition.PRICE_DOWN else "≥"


def _discord_time(value: datetime | None) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return discord.utils.format_dt(value, style="R")


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} hr {seconds % 3600 // 60} min"


def _friendly_error(error: Exception) -> str:
    if isinstance(error, NotAuthenticatedError):
        return "Run `/auth` first."
    if isinstance(error, InvalidItemError):
        return "The item is not present in the Minecraft manifest."
    if isinstance(error, InvalidThresholdError):
        return "The threshold must be a positive number."
    if isinstance(error, DonutAuthenticationError):
        return "DonutSMP rejected the token. Run `/auth` again."
    if isinstance(error, DonutRateLimitError):
        return f"The API budget is exhausted. Try again in {error.retry_after:.0f} seconds."
    if isinstance(error, DonutTransientError):
        return "DonutSMP is temporarily unavailable; try again later."
    if isinstance(error, discord.Forbidden):
        return "Discord blocked the direct message."
    return "The operation failed because of an internal error."
