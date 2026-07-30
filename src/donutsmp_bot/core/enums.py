from enum import StrEnum


class Condition(StrEnum):
    PRICE_DOWN = "price_down"
    PRICE_UP = "price_up"


class PriceType(StrEnum):
    TOTAL = "total"
    PER_ITEM = "per_item"


class RuleState(StrEnum):
    UNKNOWN = "unknown"
    ABOVE_THRESHOLD = "above_threshold"
    BELOW_THRESHOLD = "below_threshold"
    NO_LISTINGS = "no_listings"


class TokenStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
