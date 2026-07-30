from decimal import Decimal

import pytest
from sqlalchemy import func, select

from donutsmp_bot.core.enums import Condition, PriceType, TokenStatus
from donutsmp_bot.persistence.models import User, WatchRule
from donutsmp_bot.persistence.repositories import UserRepository, WatchRuleRepository


@pytest.mark.asyncio
async def test_user_rules_survive_transactions_and_logout_cascades(database) -> None:
    async with database.session_factory.begin() as session:
        user = await UserRepository(session).save_valid_token(123, "encrypted", "abcdef123456")
        rule = await WatchRuleRepository(session).create(
            discord_user_id=user.discord_user_id,
            item_id="minecraft:diamond",
            display_name="Diamond",
            condition=Condition.PRICE_DOWN,
            threshold=Decimal("100000"),
            price_type=PriceType.TOTAL,
            hysteresis_percent=Decimal("2"),
            cooldown_seconds=60,
        )
        rule_id = rule.id

    async with database.session_factory() as session:
        stored = await UserRepository(session).get(123)
        stored_rule = await WatchRuleRepository(session).get_owned(123, rule_id)
        assert stored is not None
        assert stored.token_status is TokenStatus.VALID
        assert stored_rule is not None
        assert stored_rule.threshold == Decimal("100000")

    async with database.session_factory.begin() as session:
        assert await UserRepository(session).logout(123)

    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(WatchRule)) == 0
