from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.enums import (
    Condition,
    DeliveryStatus,
    PriceType,
    RuleState,
    TokenStatus,
)
from .models import Notification, PriceObservation, User, WatchRule


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, discord_user_id: int) -> User | None:
        return await self.session.get(User, discord_user_id)

    async def save_valid_token(
        self, discord_user_id: int, encrypted_token: str, fingerprint: str
    ) -> User:
        user = await self.get(discord_user_id)
        if user is None:
            user = User(
                discord_user_id=discord_user_id,
                encrypted_donut_token=encrypted_token,
                token_fingerprint=fingerprint,
                token_status=TokenStatus.VALID,
            )
            self.session.add(user)
        else:
            user.encrypted_donut_token = encrypted_token
            user.token_fingerprint = fingerprint
            user.token_status = TokenStatus.VALID
            user.invalid_token_notified_at = None
        await self.session.flush()
        return user

    async def logout(self, discord_user_id: int) -> bool:
        result = await self.session.execute(
            delete(User).where(User.discord_user_id == discord_user_id)
        )
        return bool(result.rowcount)

    async def mark_invalid(self, discord_user_id: int) -> None:
        await self.session.execute(
            update(User)
            .where(User.discord_user_id == discord_user_id)
            .values(token_status=TokenStatus.INVALID)
        )
        await self.session.execute(
            update(WatchRule)
            .where(WatchRule.discord_user_id == discord_user_id)
            .values(enabled=False)
        )

    async def mark_invalid_notified(self, discord_user_id: int, at: datetime) -> None:
        await self.session.execute(
            update(User)
            .where(User.discord_user_id == discord_user_id)
            .values(invalid_token_notified_at=at)
        )

    async def mark_api_success(self, discord_user_id: int, at: datetime) -> None:
        await self.session.execute(
            update(User)
            .where(User.discord_user_id == discord_user_id)
            .values(last_successful_request_at=at)
        )

    async def record_dm_error(self, discord_user_id: int, message: str) -> None:
        await self.session.execute(
            update(User)
            .where(User.discord_user_id == discord_user_id)
            .values(
                dm_error_count=User.dm_error_count + 1,
                last_dm_error=message[:255],
            )
        )

    async def clear_dm_error(self, discord_user_id: int) -> None:
        await self.session.execute(
            update(User)
            .where(User.discord_user_id == discord_user_id)
            .values(dm_error_count=0, last_dm_error=None)
        )

    async def valid_users_with_rules(self) -> Sequence[User]:
        result = await self.session.scalars(
            select(User)
            .where(
                User.token_status == TokenStatus.VALID,
                User.watch_rules.any(WatchRule.enabled.is_(True)),
            )
            .options(selectinload(User.watch_rules))
        )
        return result.all()


class WatchRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        discord_user_id: int,
        item_id: str,
        display_name: str,
        condition: Condition,
        threshold: Decimal,
        price_type: PriceType,
        hysteresis_percent: Decimal,
        cooldown_seconds: int,
    ) -> WatchRule:
        rule = WatchRule(
            discord_user_id=discord_user_id,
            item_id=item_id,
            display_name=display_name,
            condition=condition,
            threshold=threshold,
            price_type=price_type,
            hysteresis_percent=hysteresis_percent,
            cooldown_seconds=cooldown_seconds,
        )
        self.session.add(rule)
        await self.session.flush()
        return rule

    async def get_owned(
        self, discord_user_id: int, rule_id: int, *, lock: bool = False
    ) -> WatchRule | None:
        query = select(WatchRule).where(
            WatchRule.id == rule_id,
            WatchRule.discord_user_id == discord_user_id,
        )
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def list_for_user(self, discord_user_id: int) -> Sequence[WatchRule]:
        result = await self.session.scalars(
            select(WatchRule)
            .where(WatchRule.discord_user_id == discord_user_id)
            .order_by(WatchRule.id)
        )
        return result.all()

    async def active_for_user(self, discord_user_id: int) -> Sequence[WatchRule]:
        result = await self.session.scalars(
            select(WatchRule).where(
                WatchRule.discord_user_id == discord_user_id,
                WatchRule.enabled.is_(True),
            )
        )
        return result.all()

    async def set_enabled(self, discord_user_id: int, rule_id: int, enabled: bool) -> bool:
        values: dict[str, object] = {"enabled": enabled}
        if enabled:
            values["current_state"] = RuleState.UNKNOWN
        result = await self.session.execute(
            update(WatchRule)
            .where(
                WatchRule.id == rule_id,
                WatchRule.discord_user_id == discord_user_id,
            )
            .values(**values)
        )
        return bool(result.rowcount)

    async def delete(self, discord_user_id: int, rule_id: int) -> bool:
        result = await self.session.execute(
            delete(WatchRule).where(
                WatchRule.id == rule_id,
                WatchRule.discord_user_id == discord_user_id,
            )
        )
        return bool(result.rowcount)

    async def count_active(self, discord_user_id: int | None = None) -> int:
        query = select(func.count()).select_from(WatchRule).where(WatchRule.enabled.is_(True))
        if discord_user_id is not None:
            query = query.where(WatchRule.discord_user_id == discord_user_id)
        return int(await self.session.scalar(query) or 0)


class ObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        discord_user_id: int,
        item_id: str,
        price: Decimal | None,
        listing_price: Decimal | None,
        item_count: int | None,
        seller_name: str | None,
        observed_at: datetime,
    ) -> PriceObservation:
        observation = PriceObservation(
            discord_user_id=discord_user_id,
            item_id=item_id,
            price=price,
            listing_price=listing_price,
            item_count=item_count,
            seller_name=seller_name,
            observed_at=observed_at,
        )
        self.session.add(observation)
        await self.session.flush()
        return observation

    async def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        result = await self.session.execute(
            delete(PriceObservation).where(PriceObservation.observed_at < cutoff)
        )
        return int(result.rowcount or 0)


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_pending(
        self, rule_id: int, price: Decimal, previous_price: Decimal | None
    ) -> Notification:
        notification = Notification(
            watch_rule_id=rule_id,
            price=price,
            previous_price=previous_price,
            delivery_status=DeliveryStatus.PENDING,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def mark_sent(self, notification_id: int, discord_message_id: int) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(
                delivery_status=DeliveryStatus.SENT,
                discord_message_id=discord_message_id,
                error_message=None,
            )
        )

    async def mark_failed(self, notification_id: int, error: str) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(delivery_status=DeliveryStatus.FAILED, error_message=error[:255])
        )

