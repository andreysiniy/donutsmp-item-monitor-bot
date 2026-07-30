import logging

import discord
from discord.ext import commands

from ..infrastructure.donut_api import DonutApiClient
from ..persistence.database import Database
from .discord_ui import AppServices, DonutCommands

logger = logging.getLogger(__name__)


class DonutBot(commands.Bot):
    def __init__(self, *, database: Database, api: DonutApiClient) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.database = database
        self.api = api
        self.services: AppServices | None = None

    async def setup_hook(self) -> None:
        if self.services is None:
            raise RuntimeError("Application services have not been configured")
        await self.add_cog(DonutCommands(self.services))
        await self.tree.sync()
        self.services.monitoring.start()
        logger.info("Discord command tree synchronized")

    async def close(self) -> None:
        if self.services is not None:
            await self.services.monitoring.stop()
        await self.api.close()
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user is not None:
            logger.info("Discord bot ready user_id=%s", self.user.id)

