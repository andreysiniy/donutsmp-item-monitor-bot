from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from donutsmp_bot.core.enums import Condition, RuleState
from donutsmp_bot.domain.evaluator import evaluate_threshold

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("condition", "previous", "price", "expected_state", "triggered"),
    [
        (
            Condition.PRICE_DOWN,
            RuleState.ABOVE_THRESHOLD,
            Decimal("100"),
            RuleState.BELOW_THRESHOLD,
            True,
        ),
        (
            Condition.PRICE_UP,
            RuleState.BELOW_THRESHOLD,
            Decimal("100"),
            RuleState.ABOVE_THRESHOLD,
            True,
        ),
        (
            Condition.PRICE_DOWN,
            RuleState.BELOW_THRESHOLD,
            Decimal("101"),
            RuleState.BELOW_THRESHOLD,
            False,
        ),
        (
            Condition.PRICE_DOWN,
            RuleState.BELOW_THRESHOLD,
            Decimal("102.01"),
            RuleState.ABOVE_THRESHOLD,
            False,
        ),
        (
            Condition.PRICE_UP,
            RuleState.ABOVE_THRESHOLD,
            Decimal("98"),
            RuleState.ABOVE_THRESHOLD,
            False,
        ),
        (
            Condition.PRICE_UP,
            RuleState.ABOVE_THRESHOLD,
            Decimal("97.99"),
            RuleState.BELOW_THRESHOLD,
            False,
        ),
    ],
)
def test_crossing_and_hysteresis(
    condition: Condition,
    previous: RuleState,
    price: Decimal,
    expected_state: RuleState,
    triggered: bool,
) -> None:
    result = evaluate_threshold(
        condition=condition,
        threshold=Decimal("100"),
        hysteresis_percent=Decimal("2"),
        previous_state=previous,
        current_price=price,
        last_triggered_at=None,
        now=NOW,
        cooldown_seconds=60,
    )
    assert result.state is expected_state
    assert result.triggered is triggered


def test_initial_match_only_notifies_when_requested() -> None:
    silent = evaluate_threshold(
        condition=Condition.PRICE_DOWN,
        threshold=Decimal("100"),
        hysteresis_percent=Decimal("2"),
        previous_state=RuleState.UNKNOWN,
        current_price=Decimal("90"),
        last_triggered_at=None,
        now=NOW,
        cooldown_seconds=60,
    )
    notified = evaluate_threshold(
        condition=Condition.PRICE_DOWN,
        threshold=Decimal("100"),
        hysteresis_percent=Decimal("2"),
        previous_state=RuleState.UNKNOWN,
        current_price=Decimal("90"),
        last_triggered_at=None,
        now=NOW,
        cooldown_seconds=60,
        notify_initial=True,
    )
    assert not silent.triggered
    assert notified.triggered


def test_no_listings_never_trigger() -> None:
    result = evaluate_threshold(
        condition=Condition.PRICE_UP,
        threshold=Decimal("100"),
        hysteresis_percent=Decimal("2"),
        previous_state=RuleState.BELOW_THRESHOLD,
        current_price=None,
        last_triggered_at=None,
        now=NOW,
        cooldown_seconds=60,
    )
    assert result.state is RuleState.NO_LISTINGS
    assert not result.triggered


def test_cooldown_suppresses_crossing() -> None:
    result = evaluate_threshold(
        condition=Condition.PRICE_UP,
        threshold=Decimal("100"),
        hysteresis_percent=Decimal("2"),
        previous_state=RuleState.BELOW_THRESHOLD,
        current_price=Decimal("101"),
        last_triggered_at=NOW - timedelta(seconds=30),
        now=NOW,
        cooldown_seconds=60,
    )
    assert result.state is RuleState.ABOVE_THRESHOLD
    assert not result.triggered
