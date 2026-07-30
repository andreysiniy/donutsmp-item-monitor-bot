from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from donutsmp_bot.application.services import (
    AlertEvent,
    RuleProcessor,
    WatchService,
)
from donutsmp_bot.core.config import Settings
from donutsmp_bot.core.enums import Condition, PriceType
from donutsmp_bot.core.security import TokenCipher
from donutsmp_bot.infrastructure.donut_api import DonutTransientError
from donutsmp_bot.infrastructure.icons import IconService
from donutsmp_bot.persistence.models import WatchRule
from donutsmp_bot.persistence.repositories import UserRepository


class FailingApi:
    async def get_price(self, **kwargs):
        raise DonutTransientError("offline")


class NullSender:
    async def send_alert(self, event: AlertEvent) -> int:
        return 1

    async def send_invalid_token(self, discord_user_id: int) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_initial_check_does_not_leave_hidden_rule(database, fernet_key: str) -> None:
    root = Path(__file__).parents[1]
    icons = IconService(root / "manifest_detailed.json", root)
    icons.load()
    cipher = TokenCipher(fernet_key)
    async with database.session_factory.begin() as session:
        await UserRepository(session).save_valid_token(7, cipher.encrypt("private"), "abcdef123456")

    sender = NullSender()
    service = WatchService(
        session_factory=database.session_factory,
        api=FailingApi(),  # type: ignore[arg-type]
        cipher=cipher,
        icons=icons,
        processor=RuleProcessor(database.session_factory, sender),
        settings=Settings(),
    )
    with pytest.raises(DonutTransientError):
        await service.add(
            discord_user_id=7,
            item_id="minecraft:diamond",
            condition=Condition.PRICE_DOWN,
            threshold=Decimal("100"),
            price_type=PriceType.TOTAL,
        )

    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(WatchRule))
    assert count == 0
