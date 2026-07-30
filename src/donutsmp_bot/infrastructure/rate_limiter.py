import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass(slots=True)
class TokenLimitState:
    requests: deque[float] = field(default_factory=deque)
    blocked_until: float = 0
    consecutive_errors: int = 0


class PerTokenRateLimiter:
    """Sliding-window limiter with capacity reserved for interactive requests."""

    def __init__(
        self,
        *,
        monitoring_limit: int = 220,
        hard_limit: int = 250,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if monitoring_limit <= 0 or hard_limit < monitoring_limit:
            raise ValueError("Rate limits must satisfy 0 < monitoring_limit <= hard_limit")
        self.monitoring_limit = monitoring_limit
        self.hard_limit = hard_limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._states: defaultdict[str, TokenLimitState] = defaultdict(TokenLimitState)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _prune(self, state: TokenLimitState, now: float) -> None:
        cutoff = now - self.window_seconds
        while state.requests and state.requests[0] <= cutoff:
            state.requests.popleft()

    async def try_acquire(self, token_key: str, *, interactive: bool = False) -> bool:
        async with self._locks[token_key]:
            now = self._clock()
            state = self._states[token_key]
            self._prune(state, now)
            limit = self.hard_limit if interactive else self.monitoring_limit
            if now < state.blocked_until or len(state.requests) >= limit:
                return False
            state.requests.append(now)
            return True

    async def acquire(self, token_key: str, *, interactive: bool = False) -> None:
        while not await self.try_acquire(token_key, interactive=interactive):
            await asyncio.sleep(self.retry_after(token_key, interactive=interactive))

    def retry_after(self, token_key: str, *, interactive: bool = False) -> float:
        now = self._clock()
        state = self._states[token_key]
        self._prune(state, now)
        if now < state.blocked_until:
            return max(state.blocked_until - now, 0.01)
        limit = self.hard_limit if interactive else self.monitoring_limit
        if len(state.requests) >= limit:
            return max(state.requests[0] + self.window_seconds - now, 0.01)
        return 0

    def remaining(self, token_key: str, *, interactive: bool = False) -> int:
        now = self._clock()
        state = self._states[token_key]
        self._prune(state, now)
        limit = self.hard_limit if interactive else self.monitoring_limit
        return max(limit - len(state.requests), 0)

    def block(self, token_key: str, seconds: float) -> None:
        state = self._states[token_key]
        state.blocked_until = max(state.blocked_until, self._clock() + max(seconds, 0))

    def record_transient_error(self, token_key: str, *, maximum: float = 60) -> float:
        state = self._states[token_key]
        state.consecutive_errors += 1
        delay = min(float(2 ** (state.consecutive_errors - 1)), maximum)
        self.block(token_key, delay)
        return delay

    def record_success(self, token_key: str) -> None:
        state = self._states[token_key]
        state.consecutive_errors = 0
        state.blocked_until = 0

    def budget_reset_at(self, token_key: str) -> datetime | None:
        now = self._clock()
        state = self._states[token_key]
        self._prune(state, now)
        if not state.requests:
            return None
        seconds = max(state.requests[0] + self.window_seconds - now, 0)
        return datetime.now(UTC) + timedelta(seconds=seconds)


def calculate_poll_interval(
    unique_requests: int,
    pages_per_request: int,
    safe_requests_per_minute: int,
    default_seconds: float,
) -> float:
    if unique_requests <= 0:
        return default_seconds
    budget_interval = (
        unique_requests * max(pages_per_request, 1) * 60 / safe_requests_per_minute
    )
    return max(default_seconds, budget_interval)

