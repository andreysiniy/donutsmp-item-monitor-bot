import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.enums import Condition, PriceType, TokenStatus
from ..core.item_ids import normalize_item_id
from ..core.security import TokenCipher, token_fingerprint
from ..domain.evaluator import evaluate_threshold
from ..domain.schemas import PriceSnapshot
from ..infrastructure.donut_api import (
    DonutApiClient,
    DonutAuthenticationError,
)
from ..infrastructure.icons import IconService
from ..persistence.models import User, WatchRule
from ..persistence.repositories import (
    NotificationRepository,
    ObservationRepository,
    UserRepository,
    WatchRuleRepository,
)

logger = logging.getLogger(__name__)


class NotAuthenticatedError(ValueError):
    pass


class InvalidItemError(ValueError):
    pass


class InvalidThresholdError(ValueError):
    pass


class RuleNotFoundError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AlertEvent:
    discord_user_id: int
    rule_id: int
    item_id: str
    display_name: str
    condition: Condition
    threshold: Decimal
    previous_price: Decimal | None
    current_price: Decimal
    snapshot: PriceSnapshot


class NotificationSender(Protocol):
    async def send_alert(self, event: AlertEvent) -> int: ...

    async def send_invalid_token(self, discord_user_id: int) -> None: ...


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api: DonutApiClient,
        cipher: TokenCipher,
    ) -> None:
        self.session_factory = session_factory
        self.api = api
        self.cipher = cipher

    async def authenticate(self, discord_user_id: int, raw_token: str) -> str:
        token = raw_token.strip()
        if not token:
            raise DonutAuthenticationError("Token must not be empty")
        fingerprint = token_fingerprint(token)
        await self.api.validate_token(token, fingerprint)
        encrypted = self.cipher.encrypt(token)
        async with self.session_factory.begin() as session:
            await UserRepository(session).save_valid_token(discord_user_id, encrypted, fingerprint)
        return fingerprint

    async def logout(self, discord_user_id: int) -> bool:
        async with self.session_factory.begin() as session:
            return await UserRepository(session).logout(discord_user_id)

    async def get_user(self, discord_user_id: int) -> User | None:
        async with self.session_factory() as session:
            return await UserRepository(session).get(discord_user_id)


class RuleProcessor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sender: NotificationSender,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender

    async def process(
        self,
        *,
        discord_user_id: int,
        rules: Sequence[WatchRule],
        snapshot: PriceSnapshot,
        notify_initial_rule_id: int | None = None,
    ) -> list[AlertEvent]:
        listing = snapshot.listing
        events: list[tuple[AlertEvent, int]] = []
        rule_ids = {rule.id for rule in rules}
        if not rule_ids:
            return []

        async with self.session_factory.begin() as session:
            observation_repo = ObservationRepository(session)
            await observation_repo.add(
                discord_user_id=discord_user_id,
                item_id=snapshot.item_id,
                price=snapshot.selected_price,
                listing_price=listing.price if listing else None,
                item_count=listing.item.count if listing else None,
                seller_name=listing.seller.name if listing else None,
                observed_at=snapshot.checked_at,
            )
            repository = WatchRuleRepository(session)
            notification_repo = NotificationRepository(session)
            for rule_id in rule_ids:
                rule = await repository.get_owned(discord_user_id, rule_id, lock=True)
                if rule is None or not rule.enabled:
                    continue
                previous_price = rule.last_observed_price
                evaluation = evaluate_threshold(
                    condition=rule.condition,
                    threshold=rule.threshold,
                    hysteresis_percent=rule.hysteresis_percent,
                    previous_state=rule.current_state,
                    current_price=snapshot.selected_price,
                    last_triggered_at=rule.last_triggered_at,
                    now=snapshot.checked_at,
                    cooldown_seconds=rule.cooldown_seconds,
                    notify_initial=rule.id == notify_initial_rule_id,
                )
                rule.current_state = evaluation.state
                rule.last_observed_price = snapshot.selected_price
                rule.last_checked_at = snapshot.checked_at
                if evaluation.triggered and snapshot.selected_price is not None:
                    rule.last_triggered_at = snapshot.checked_at
                    notification = await notification_repo.create_pending(
                        rule.id, snapshot.selected_price, previous_price
                    )
                    events.append(
                        (
                            AlertEvent(
                                discord_user_id=discord_user_id,
                                rule_id=rule.id,
                                item_id=rule.item_id,
                                display_name=rule.display_name,
                                condition=rule.condition,
                                threshold=rule.threshold,
                                previous_price=previous_price,
                                current_price=snapshot.selected_price,
                                snapshot=snapshot,
                            ),
                            notification.id,
                        )
                    )

        for event, notification_id in events:
            await self._deliver(event, notification_id)
        return [event for event, _ in events]

    async def _deliver(self, event: AlertEvent, notification_id: int) -> None:
        try:
            message_id = await self.sender.send_alert(event)
        except Exception as exc:
            error_name = type(exc).__name__
            logger.warning(
                "Discord DM delivery failed user_id=%s rule_id=%s error=%s",
                event.discord_user_id,
                event.rule_id,
                error_name,
            )
            async with self.session_factory.begin() as session:
                await NotificationRepository(session).mark_failed(notification_id, error_name)
                await UserRepository(session).record_dm_error(event.discord_user_id, error_name)
        else:
            async with self.session_factory.begin() as session:
                await NotificationRepository(session).mark_sent(notification_id, message_id)
                await UserRepository(session).clear_dm_error(event.discord_user_id)


class WatchService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        api: DonutApiClient,
        cipher: TokenCipher,
        icons: IconService,
        processor: RuleProcessor,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.api = api
        self.cipher = cipher
        self.icons = icons
        self.processor = processor
        self.settings = settings

    async def add(
        self,
        *,
        discord_user_id: int,
        item_id: str,
        condition: Condition,
        threshold: str | int | Decimal,
        price_type: PriceType,
    ) -> tuple[WatchRule, PriceSnapshot]:
        normalized_item_id = normalize_item_id(item_id)
        if not self.icons.contains(normalized_item_id):
            raise InvalidItemError("Unknown Minecraft item ID")
        parsed_threshold = _positive_decimal(threshold)

        async with self.session_factory.begin() as session:
            user = await _require_valid_user(session, discord_user_id)
            rule = await WatchRuleRepository(session).create(
                discord_user_id=discord_user_id,
                item_id=normalized_item_id,
                display_name=self.icons.display_name(normalized_item_id),
                condition=condition,
                threshold=parsed_threshold,
                price_type=price_type,
                hysteresis_percent=Decimal(str(self.settings.default_hysteresis_percent)),
                cooldown_seconds=self.settings.default_notification_cooldown_seconds,
            )
            token = self.cipher.decrypt(user.encrypted_donut_token)
            fingerprint = user.token_fingerprint

        try:
            snapshot = await self.api.get_price(
                token=token,
                token_key=fingerprint,
                item_id=normalized_item_id,
                price_type=price_type,
                interactive=True,
            )
        except Exception:
            async with self.session_factory.begin() as session:
                await WatchRuleRepository(session).delete(discord_user_id, rule.id)
            raise
        await self.processor.process(
            discord_user_id=discord_user_id,
            rules=[rule],
            snapshot=snapshot,
            notify_initial_rule_id=rule.id,
        )
        return rule, snapshot

    async def list(self, discord_user_id: int) -> Sequence[WatchRule]:
        async with self.session_factory() as session:
            await _require_valid_user(session, discord_user_id)
            return await WatchRuleRepository(session).list_for_user(discord_user_id)

    async def delete(self, discord_user_id: int, rule_id: int) -> bool:
        async with self.session_factory.begin() as session:
            await _require_valid_user(session, discord_user_id)
            return await WatchRuleRepository(session).delete(discord_user_id, rule_id)

    async def set_enabled(self, discord_user_id: int, rule_id: int, *, enabled: bool) -> bool:
        async with self.session_factory.begin() as session:
            await _require_valid_user(session, discord_user_id)
            return await WatchRuleRepository(session).set_enabled(discord_user_id, rule_id, enabled)


class PriceService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        api: DonutApiClient,
        cipher: TokenCipher,
        icons: IconService,
    ) -> None:
        self.session_factory = session_factory
        self.api = api
        self.cipher = cipher
        self.icons = icons

    async def get(self, discord_user_id: int, item_id: str, price_type: PriceType) -> PriceSnapshot:
        normalized = normalize_item_id(item_id)
        if not self.icons.contains(normalized):
            raise InvalidItemError("Unknown Minecraft item ID")
        async with self.session_factory() as session:
            user = await _require_valid_user(session, discord_user_id)
            token = self.cipher.decrypt(user.encrypted_donut_token)
            fingerprint = user.token_fingerprint
        return await self.api.get_price(
            token=token,
            token_key=fingerprint,
            item_id=normalized,
            price_type=price_type,
            interactive=True,
        )


async def _require_valid_user(session: AsyncSession, discord_user_id: int) -> User:
    user = await UserRepository(session).get(discord_user_id)
    if user is None or user.token_status is not TokenStatus.VALID:
        raise NotAuthenticatedError("Authorize with /auth first")
    return user


def _positive_decimal(value: str | int | Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidThresholdError("Threshold must be a number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise InvalidThresholdError("Threshold must be positive")
    return parsed
