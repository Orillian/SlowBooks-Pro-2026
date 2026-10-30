"""transactions.source_ref: the ledger import's key on its own (#663)

QuickBooks Desktop's Transaction Detail by Account import kept each journal's
duplicate key, "{Type} {Num} · {date} · {name}", as the reference. It now
writes QuickBooks' Num there and the key to source_ref, a column of its own,
which every other posting leaves empty. downgrade drops the column.

Revision ID: 4810bd59e481
Revises: d7b3f9a1c5e2
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4810bd59e481"
down_revision: Union[str, None] = "d7b3f9a1c5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "transactions" not in insp.get_table_names():
        return
    if "source_ref" not in {c["name"] for c in insp.get_columns("transactions")}:
        op.add_column(
            "transactions", sa.Column("source_ref", sa.String(100), nullable=True)
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "transactions" not in insp.get_table_names():
        return
    if "source_ref" not in {c["name"] for c in insp.get_columns("transactions")}:
        return
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("source_ref")
