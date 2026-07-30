from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..core.enums import Condition, DeliveryStatus, PriceType, RuleState, TokenStatus


def enum_column(enum_type: type[Condition] | type[DeliveryStatus] | type[PriceType] | type[RuleState] | type[TokenStatus]) -> Enum:  # type: ignore[type-arg]
    return Enum(
        enum_type,
        values_callable=lambda members: [member.value for member in members],
        native_enum=False,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    encrypted_donut_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_fingerprint: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    token_status: Mapped[TokenStatus] = mapped_column(
        enum_column(TokenStatus), default=TokenStatus.VALID, nullable=False
    )
    last_successful_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalid_token_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dm_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_dm_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    watch_rules: Mapped[list["WatchRule"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    observations: Mapped[list["PriceObservation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class WatchRule(TimestampMixin, Base):
    __tablename__ = "watch_rules"
    __table_args__ = (
        Index(
            "ix_watch_rules_active_request",
            "discord_user_id",
            "enabled",
            "item_id",
            "price_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.discord_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    condition: Mapped[Condition] = mapped_column(enum_column(Condition), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    price_type: Mapped[PriceType] = mapped_column(enum_column(PriceType), nullable=False)
    hysteresis_percent: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("2"), nullable=False
    )
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_state: Mapped[RuleState] = mapped_column(
        enum_column(RuleState), default=RuleState.UNKNOWN, nullable=False
    )
    last_observed_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="watch_rules")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="watch_rule", cascade="all, delete-orphan", passive_deletes=True
    )


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        Index("ix_price_observations_retention", "observed_at"),
        Index("ix_price_observations_item", "discord_user_id", "item_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.discord_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    listing_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    item_count: Mapped[int | None] = mapped_column(Integer)
    seller_name: Mapped[str | None] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="observations")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_rule_created", "watch_rule_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_rule_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("watch_rules.id", ondelete="CASCADE"), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus), default=DeliveryStatus.PENDING, nullable=False
    )
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    watch_rule: Mapped[WatchRule] = relationship(back_populates="notifications")

