from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.enums import PriceType


class AuctionItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    count: int = 1
    display_name: str | None = None
    lore: list[Any] = Field(default_factory=list)
    enchants: dict[str, Any] | list[Any] = Field(default_factory=dict)
    trim: Any | None = None

    @field_validator("count", mode="before")
    @classmethod
    def normalize_count(cls, value: Any) -> int:
        try:
            count = int(value or 1)
        except (TypeError, ValueError):
            return 1
        return max(count, 1)


class AuctionSeller(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "Unknown"
    uuid: str | None = None


class AuctionListing(BaseModel):
    model_config = ConfigDict(extra="allow")

    item: AuctionItem
    price: Decimal
    seller: AuctionSeller = Field(default_factory=AuctionSeller)
    time_left: int | None = None

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Decimal:
        try:
            price = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("price must be numeric") from exc
        if not price.is_finite() or price < 0:
            raise ValueError("price must be a finite non-negative value")
        return price

    def selected_price(self, price_type: PriceType) -> Decimal:
        if price_type is PriceType.PER_ITEM:
            return self.price / Decimal(max(self.item.count, 1))
        return self.price


class PriceSnapshot(BaseModel):
    item_id: str
    price_type: PriceType
    selected_price: Decimal | None
    listing: AuctionListing | None
    checked_at: datetime
    pages_scanned: int


class ApiHealth(BaseModel):
    available: bool = True
    last_error: str | None = None
    last_success_at: datetime | None = None

