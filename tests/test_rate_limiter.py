import pytest
from pydantic import ValidationError

from donutsmp_bot.core.config import Settings
from donutsmp_bot.infrastructure.rate_limiter import (
    PerTokenRateLimiter,
    calculate_poll_interval,
)


@pytest.mark.asyncio
async def test_per_token_budgets_are_isolated_and_reserve_is_kept() -> None:
    now = [0.0]
    limiter = PerTokenRateLimiter(
        monitoring_limit=2,
        hard_limit=3,
        window_seconds=60,
        clock=lambda: now[0],
    )
    assert await limiter.try_acquire("alice")
    assert await limiter.try_acquire("alice")
    assert not await limiter.try_acquire("alice")
    assert await limiter.try_acquire("alice", interactive=True)
    assert not await limiter.try_acquire("alice", interactive=True)
    assert await limiter.try_acquire("bob")

    now[0] = 61
    assert await limiter.try_acquire("alice")
    assert limiter.remaining("alice") == 1


@pytest.mark.asyncio
async def test_server_backoff_only_blocks_target_token() -> None:
    now = [10.0]
    limiter = PerTokenRateLimiter(
        monitoring_limit=2,
        hard_limit=3,
        clock=lambda: now[0],
    )
    limiter.block("alice", 30)
    assert not await limiter.try_acquire("alice")
    assert await limiter.try_acquire("bob")
    assert limiter.retry_after("alice") == pytest.approx(30)


def test_dynamic_poll_interval_accounts_for_worst_case_pages() -> None:
    assert calculate_poll_interval(1, 3, 220, 3) == 3
    assert calculate_poll_interval(40, 3, 220, 3) == pytest.approx(32.7272727)


def test_configuration_cannot_exceed_remote_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(safe_requests_per_minute=240, reserved_requests_per_minute=30)
