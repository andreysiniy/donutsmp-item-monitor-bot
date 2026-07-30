from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from donutsmp_bot.presentation.bot import DonutBot


@pytest.mark.asyncio
async def test_ready_sets_online_auction_presence() -> None:
    bot = SimpleNamespace(
        change_presence=AsyncMock(),
        user=SimpleNamespace(id=123),
    )

    await DonutBot.on_ready(bot)  # type: ignore[arg-type]

    bot.change_presence.assert_awaited_once()
    kwargs = bot.change_presence.await_args.kwargs
    assert kwargs["status"] is discord.Status.online
    activity = kwargs["activity"]
    assert isinstance(activity, discord.Activity)
    assert activity.type is discord.ActivityType.watching
    assert activity.name == "DonutSMP auction prices"
