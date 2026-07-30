from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from donutsmp_bot.application.monitoring import MonitoringCoordinator
from donutsmp_bot.application.services import AlertEvent, RuleProcessor
from donutsmp_bot.core.config import Settings
from donutsmp_bot.core.enums import Condition, PriceType
from donutsmp_bot.core.security import TokenCipher
from donutsmp_bot.domain.schemas import AuctionItem, AuctionListing, PriceSnapshot
from donutsmp_bot.persistence.models import PriceObservation
from donutsmp_bot.persistence.repositories import UserRepository, WatchRuleRepository


class FakeSender:
    def __init__(self) -> None:
        self.alerts: list[AlertEvent] = []
        self.invalid_users: list[int] = []

    async def send_alert(self, event: AlertEvent) -> int:
        self.alerts.append(event)
        return len(self.alerts)

    async def send_invalid_token(self, discord_user_id: int) -> None:
        self.invalid_users.append(discord_user_id)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, PriceType]] = []
        self.prices = [Decimal("150"), Decimal("80")]

    async def get_price(
        self,
        *,
        token: str,
        token_key: str,
        item_id: str,
        price_type: PriceType,
        interactive: bool = False,
    ) -> PriceSnapshot:
        self.calls.append((item_id, price_type))
        price = self.prices[len(self.calls) - 1]
        listing = AuctionListing(
            item=AuctionItem(id=item_id, count=1),
            price=price,
        )
        return PriceSnapshot(
            item_id=item_id,
            price_type=price_type,
            selected_price=price,
            listing=listing,
            checked_at=datetime.now(UTC),
            pages_scanned=1,
        )


@pytest.mark.asyncio
async def test_monitoring_deduplicates_requests_and_triggers_each_rule(database) -> None:
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    sender = FakeSender()
    api = FakeApi()
    now = [0.0]

    async with database.session_factory.begin() as session:
        await UserRepository(session).save_valid_token(
            42, cipher.encrypt("private-token"), "abcdef123456"
        )
        repository = WatchRuleRepository(session)
        for threshold in (Decimal("100"), Decimal("90")):
            await repository.create(
                discord_user_id=42,
                item_id="diamond",
                display_name="Diamond",
                condition=Condition.PRICE_DOWN,
                threshold=threshold,
                price_type=PriceType.TOTAL,
                hysteresis_percent=Decimal("2"),
                cooldown_seconds=60,
            )

    processor = RuleProcessor(database.session_factory, sender)
    coordinator = MonitoringCoordinator(
        session_factory=database.session_factory,
        api=api,  # type: ignore[arg-type]
        cipher=cipher,
        processor=processor,
        sender=sender,
        settings=Settings(),
        clock=lambda: now[0],
    )

    await coordinator.run_once()
    assert len(api.calls) == 1
    assert not sender.alerts

    now[0] = 4
    await coordinator.run_once()
    assert len(api.calls) == 2
    assert len(sender.alerts) == 2
    assert {event.database_rule_id for event in sender.alerts} == {1, 2}
    assert {event.display_rule_id for event in sender.alerts} == {1, 2}

    async with database.session_factory() as session:
        observations = await session.scalar(select(func.count()).select_from(PriceObservation))
    assert observations == 2
