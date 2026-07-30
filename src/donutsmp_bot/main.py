import asyncio

from .application.monitoring import MonitoringCoordinator
from .application.services import AuthService, PriceService, RuleProcessor, WatchService
from .core.config import get_settings
from .core.logging import configure_logging
from .core.security import TokenCipher
from .infrastructure.donut_api import DonutApiClient
from .infrastructure.icons import IconService
from .infrastructure.rate_limiter import PerTokenRateLimiter
from .persistence.database import Database
from .presentation.bot import DonutBot
from .presentation.discord_ui import AppServices, DiscordNotificationSender


async def async_main() -> None:
    settings = get_settings()
    settings.validate_runtime_secrets()
    configure_logging(settings.log_level)

    icons = IconService(settings.manifest_path, settings.assets_dir)
    icons.load()
    database = Database(settings.database_url)
    limiter = PerTokenRateLimiter(
        monitoring_limit=settings.safe_requests_per_minute,
        hard_limit=settings.safe_requests_per_minute + settings.reserved_requests_per_minute,
    )
    api = DonutApiClient(
        base_url=settings.donut_api_base_url,
        limiter=limiter,
        timeout_seconds=settings.request_timeout_seconds,
        max_search_pages=settings.max_search_pages,
    )
    cipher = TokenCipher(settings.token_encryption_key)
    bot = DonutBot(database=database, api=api)
    sender = DiscordNotificationSender(bot, icons)
    processor = RuleProcessor(database.session_factory, sender)
    auth = AuthService(database.session_factory, api, cipher)
    watches = WatchService(
        session_factory=database.session_factory,
        api=api,
        cipher=cipher,
        icons=icons,
        processor=processor,
        settings=settings,
    )
    prices = PriceService(
        session_factory=database.session_factory,
        api=api,
        cipher=cipher,
        icons=icons,
    )
    sender.bind_actions(watches, prices)
    monitoring = MonitoringCoordinator(
        session_factory=database.session_factory,
        api=api,
        cipher=cipher,
        processor=processor,
        sender=sender,
        settings=settings,
    )
    bot.services = AppServices(
        session_factory=database.session_factory,
        auth=auth,
        watches=watches,
        prices=prices,
        api=api,
        icons=icons,
        monitoring=monitoring,
    )
    await bot.start(settings.discord_bot_token)


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
