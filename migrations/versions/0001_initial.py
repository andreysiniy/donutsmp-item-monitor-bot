"""Create monitoring tables.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

condition = sa.Enum("price_down", "price_up", name="condition", native_enum=False)
price_type = sa.Enum("total", "per_item", name="pricetype", native_enum=False)
rule_state = sa.Enum(
    "unknown",
    "above_threshold",
    "below_threshold",
    "no_listings",
    name="rulestate",
    native_enum=False,
)
token_status = sa.Enum("valid", "invalid", name="tokenstatus", native_enum=False)
delivery_status = sa.Enum("pending", "sent", "failed", name="deliverystatus", native_enum=False)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("encrypted_donut_token", sa.Text(), nullable=False),
        sa.Column("token_fingerprint", sa.String(12), nullable=False),
        sa.Column("token_status", token_status, nullable=False),
        sa.Column("last_successful_request_at", sa.DateTime(timezone=True)),
        sa.Column("invalid_token_notified_at", sa.DateTime(timezone=True)),
        sa.Column("dm_error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_dm_error", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_users_token_fingerprint", "users", ["token_fingerprint"])

    op.create_table(
        "watch_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "discord_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.discord_user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("condition", condition, nullable=False),
        sa.Column("threshold", sa.Numeric(30, 8), nullable=False),
        sa.Column("price_type", price_type, nullable=False),
        sa.Column("hysteresis_percent", sa.Numeric(8, 4), server_default="2", nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("current_state", rule_state, server_default="unknown", nullable=False),
        sa.Column("last_observed_price", sa.Numeric(30, 8)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_watch_rules_discord_user_id", "watch_rules", ["discord_user_id"])
    op.create_index(
        "ix_watch_rules_active_request",
        "watch_rules",
        ["discord_user_id", "enabled", "item_id", "price_type"],
    )

    op.create_table(
        "price_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "discord_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.discord_user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("price", sa.Numeric(30, 8)),
        sa.Column("listing_price", sa.Numeric(30, 8)),
        sa.Column("item_count", sa.Integer()),
        sa.Column("seller_name", sa.String(255)),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_price_observations_item",
        "price_observations",
        ["discord_user_id", "item_id", "observed_at"],
    )
    op.create_index("ix_price_observations_retention", "price_observations", ["observed_at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "watch_rule_id",
            sa.Integer(),
            sa.ForeignKey("watch_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(30, 8), nullable=False),
        sa.Column("previous_price", sa.Numeric(30, 8)),
        sa.Column("delivery_status", delivery_status, nullable=False),
        sa.Column("discord_message_id", sa.BigInteger()),
        sa.Column("error_message", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_notifications_rule_created", "notifications", ["watch_rule_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("price_observations")
    op.drop_table("watch_rules")
    op.drop_table("users")
