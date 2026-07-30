from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..core.enums import Condition, RuleState


@dataclass(frozen=True, slots=True)
class Evaluation:
    state: RuleState
    triggered: bool


def evaluate_threshold(
    *,
    condition: Condition,
    threshold: Decimal,
    hysteresis_percent: Decimal,
    previous_state: RuleState,
    current_price: Decimal | None,
    last_triggered_at: datetime | None,
    now: datetime,
    cooldown_seconds: int,
    notify_initial: bool = False,
) -> Evaluation:
    if current_price is None:
        return Evaluation(RuleState.NO_LISTINGS, False)

    cooldown_ready = _cooldown_ready(last_triggered_at, now, cooldown_seconds)
    hysteresis = hysteresis_percent / Decimal("100")

    if condition is Condition.PRICE_DOWN:
        rearm_price = threshold * (Decimal("1") + hysteresis)
        if previous_state is RuleState.UNKNOWN:
            matched = current_price <= threshold
            return Evaluation(
                RuleState.BELOW_THRESHOLD if matched else RuleState.ABOVE_THRESHOLD,
                matched and notify_initial and cooldown_ready,
            )
        if previous_state is RuleState.NO_LISTINGS:
            return Evaluation(
                RuleState.BELOW_THRESHOLD
                if current_price <= threshold
                else RuleState.ABOVE_THRESHOLD,
                False,
            )
        if previous_state is RuleState.ABOVE_THRESHOLD and current_price <= threshold:
            return Evaluation(RuleState.BELOW_THRESHOLD, cooldown_ready)
        if previous_state is RuleState.BELOW_THRESHOLD and current_price > rearm_price:
            return Evaluation(RuleState.ABOVE_THRESHOLD, False)
        return Evaluation(previous_state, False)

    rearm_price = threshold * (Decimal("1") - hysteresis)
    if previous_state is RuleState.UNKNOWN:
        matched = current_price >= threshold
        return Evaluation(
            RuleState.ABOVE_THRESHOLD if matched else RuleState.BELOW_THRESHOLD,
            matched and notify_initial and cooldown_ready,
        )
    if previous_state is RuleState.NO_LISTINGS:
        return Evaluation(
            RuleState.ABOVE_THRESHOLD if current_price >= threshold else RuleState.BELOW_THRESHOLD,
            False,
        )
    if previous_state is RuleState.BELOW_THRESHOLD and current_price >= threshold:
        return Evaluation(RuleState.ABOVE_THRESHOLD, cooldown_ready)
    if previous_state is RuleState.ABOVE_THRESHOLD and current_price < rearm_price:
        return Evaluation(RuleState.BELOW_THRESHOLD, False)
    return Evaluation(previous_state, False)


def _cooldown_ready(
    last_triggered_at: datetime | None, now: datetime, cooldown_seconds: int
) -> bool:
    if last_triggered_at is None:
        return True
    if last_triggered_at.tzinfo is None:
        last_triggered_at = last_triggered_at.replace(tzinfo=UTC)
    return now >= last_triggered_at + timedelta(seconds=cooldown_seconds)
