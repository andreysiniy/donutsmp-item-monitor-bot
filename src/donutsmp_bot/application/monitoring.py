import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.enums import PriceType
from ..core.security import TokenCipher, TokenDecryptionError
from ..infrastructure.donut_api import (
    DonutApiClient,
    DonutApiError,
    DonutAuthenticationError,
)
from ..infrastructure.rate_limiter import calculate_poll_interval
from ..persistence.models import User, WatchRule
from ..persistence.repositories import ObservationRepository, UserRepository
from .services import NotificationSender, RuleProcessor

logger = logging.getLogger(__name__)


class MonitoringCoordinator:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        api: DonutApiClient,
        cipher: TokenCipher,
        processor: RuleProcessor,
        sender: NotificationSender,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.api = api
        self.cipher = cipher
        self.processor = processor
        self.sender = sender
        self.settings = settings
        self._clock = clock
        self._next_poll_at: dict[int, float] = {}
        self._last_prune_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.last_successful_cycle_at: datetime | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever(), name="donutsmp-monitoring")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Unexpected monitoring cycle error")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1)
            except TimeoutError:
                pass

    async def run_once(self) -> None:
        now = self._clock()
        if self._last_prune_at is None or now - self._last_prune_at >= 86400:
            async with self.session_factory.begin() as session:
                await ObservationRepository(session).prune(self.settings.observation_retention_days)
            self._last_prune_at = now

        async with self.session_factory() as session:
            users = await UserRepository(session).valid_users_with_rules()

        due_users = [
            user for user in users if self._next_poll_at.get(user.discord_user_id, 0) <= now
        ]
        results = await asyncio.gather(
            *(self._poll_user(user) for user in due_users),
            return_exceptions=True,
        )
        for user, result in zip(due_users, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "Token monitoring group failed user_id=%s error=%s",
                    user.discord_user_id,
                    type(result).__name__,
                )
        if due_users and not all(isinstance(result, Exception) for result in results):
            self.last_successful_cycle_at = datetime.now(UTC)

    async def _poll_user(self, user: User) -> None:
        rules = [rule for rule in user.watch_rules if rule.enabled]
        grouped: defaultdict[tuple[str, PriceType], list[WatchRule]] = defaultdict(list)
        for rule in rules:
            grouped[(rule.item_id, rule.price_type)].append(rule)
        interval = calculate_poll_interval(
            unique_requests=len(grouped),
            pages_per_request=self.settings.max_search_pages,
            safe_requests_per_minute=self.settings.safe_requests_per_minute,
            default_seconds=self.settings.default_poll_interval_seconds,
        )
        self._next_poll_at[user.discord_user_id] = self._clock() + interval

        try:
            token = self.cipher.decrypt(user.encrypted_donut_token)
        except TokenDecryptionError:
            await self._invalidate_user(user, notify=True)
            return

        any_success = False
        for (item_id, price_type), grouped_rules in grouped.items():
            try:
                snapshot = await self.api.get_price(
                    token=token,
                    token_key=user.token_fingerprint,
                    item_id=item_id,
                    price_type=price_type,
                )
            except DonutAuthenticationError:
                await self._invalidate_user(user, notify=True)
                return
            except DonutApiError as exc:
                logger.warning(
                    "Auction poll deferred user_id=%s token_fingerprint=%s item_id=%s error=%s",
                    user.discord_user_id,
                    user.token_fingerprint,
                    item_id,
                    type(exc).__name__,
                )
                continue

            any_success = True
            await self.processor.process(
                discord_user_id=user.discord_user_id,
                rules=grouped_rules,
                snapshot=snapshot,
            )

        if any_success:
            async with self.session_factory.begin() as session:
                await UserRepository(session).mark_api_success(
                    user.discord_user_id, datetime.now(UTC)
                )

    async def _invalidate_user(self, user: User, *, notify: bool) -> None:
        should_notify = notify and user.invalid_token_notified_at is None
        now = datetime.now(UTC)
        async with self.session_factory.begin() as session:
            repository = UserRepository(session)
            await repository.mark_invalid(user.discord_user_id)
            if should_notify:
                await repository.mark_invalid_notified(user.discord_user_id, now)
        if should_notify:
            try:
                await self.sender.send_invalid_token(user.discord_user_id)
            except Exception as exc:
                async with self.session_factory.begin() as session:
                    await UserRepository(session).record_dm_error(
                        user.discord_user_id, type(exc).__name__
                    )
