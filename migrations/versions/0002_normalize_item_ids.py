"""Remove the Minecraft namespace from persisted item IDs.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAMES = ("watch_rules", "price_observations")
_PREFIX = "minecraft:"


def upgrade() -> None:
    for table_name in _TABLE_NAMES:
        table = _item_table(table_name)
        op.execute(
            table.update()
            .where(table.c.item_id.like(f"{_PREFIX}%"))
            .values(item_id=sa.func.substr(table.c.item_id, len(_PREFIX) + 1))
        )


def downgrade() -> None:
    for table_name in _TABLE_NAMES:
        table = _item_table(table_name)
        op.execute(
            table.update()
            .where(table.c.item_id.not_like("%:%"))
            .values(item_id=sa.literal(_PREFIX) + table.c.item_id)
        )


def _item_table(name: str) -> sa.TableClause:
    return sa.table(name, sa.column("item_id", sa.String()))
