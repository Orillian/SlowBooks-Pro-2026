"""transactions.source_ref; ledger imports read as register entries (#663)

QuickBooks Desktop's Transaction Detail by Account import wrote each journal's
description as "{Type} {Num} — {Name}: {Memo}" and kept its duplicate key,
"{Type} {Num} · {date} · {name}" (and " #2"... when a key repeats in a file),
as the reference. The register shows a journal's description as its payee,
so payees read "Check EFT — Acme Supply: memo". From here the import writes
QuickBooks' Name as the description (the payee; empty with no name), its Num
as the reference (Ref #), and the key to source_ref, a column of its own; each
line's memo stays on the line, where it always was.

The entries already imported are rewritten the same way, once: source_type
qb_ledger_import only, and only an entry whose description and key read back
one way. The key's "{Type} {Num}" splits after the longest of QuickBooks'
types it starts with (all of it is the type when it holds no space), and the
key holds the name when it was the journal's only one. The description gives
the name, then ": " and the first memo when there was one; a name or memo may
hold ":" or " — ", so a reading must also agree with the key and with the
lines, which carry each memo or, with none, the name. An entry with no such
reading, or more than one, is left as it was, and the import still finds its
key in the reference. Written out here so this migration never changes with
the importer.

downgrade puts each key back in the reference, where the import looked for
it before, and drops the column; the descriptions keep the name.

Revision ID: 4810bd59e481
Revises: d7b3f9a1c5e2
Create Date: 2026-10-30
"""

import re
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4810bd59e481"
down_revision: Union[str, None] = "d7b3f9a1c5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCE_TYPE = "qb_ledger_import"
KEY_LENGTH = 100  # transactions.reference, and so the key, held 100
DESCRIPTION_LENGTH = 200  # the importer cut the description there

# QuickBooks Desktop's transaction types as its reports print them, the
# Canadian spellings and the journal names the importer took among them
TYPES = sorted(
    (
        "Bill",
        "Bill CCard Payment",
        "Bill Credit",
        "Bill Pmt -CCard",
        "Bill Pmt -Check",
        "Bill Pmt -Cheque",
        "Build Assembly",
        "Check",
        "Cheque",
        "Credit",
        "Credit Card Charge",
        "Credit Card Credit",
        "Credit Card Payment",
        "Credit Card Refund",
        "Credit Memo",
        "Deposit",
        "Estimate",
        "General Journal",
        "Inventory Adjust",
        "Inventory Adjustment",
        "Inventory Transfer",
        "Invoice",
        "Item Receipt",
        "Journal",
        "Journal Entry",
        "Liability Adjustment",
        "Liability Check",
        "Liability Cheque",
        "Liability Refund",
        "Paycheck",
        "Paycheque",
        "Payment",
        "Purchase Order",
        "Sales Order",
        "Sales Receipt",
        "Sales Tax Adjustment",
        "Sales Tax Payment",
        "Statement Charge",
        "Transfer",
        "Transfer Inventory",
        "Vendor Credit",
        "YTD Adjustment",
    ),
    key=len,
    reverse=True,
)

_transactions = sa.table(
    "transactions",
    sa.column("id", sa.Integer),
    sa.column("date", sa.Date),
    sa.column("description", sa.Text),
    sa.column("reference", sa.String),
    sa.column("source_type", sa.String),
    sa.column("source_ref", sa.String),
)
_lines = sa.table(
    "transaction_lines",
    sa.column("id", sa.Integer),
    sa.column("transaction_id", sa.Integer),
    sa.column("description", sa.String),
)


def split_head(head: str):
    """(type, num) of "{Type} {Num}", or None when it can't be told."""
    for ttype in TYPES:
        if head == ttype:
            return ttype, ""
        if head.startswith(ttype + " "):
            return ttype, head[len(ttype) + 1 :]
    if " " not in head:
        return head, ""
    return None


def key_fits(key: str, base: str) -> bool:
    """Whether `key` is `base` as the importer stored it: cut to the
    column, with " #2", " #3"... on a key that repeated in the file."""
    if key == base[:KEY_LENGTH]:
        return True
    if len(base) >= KEY_LENGTH or not key.startswith(base):
        return False
    tail = key[len(base) :]
    if re.fullmatch(r" #([2-9]|[1-9]\d+)", tail):
        return True
    # the repeat number cut short by the column
    return len(key) == KEY_LENGTH and re.fullmatch(r" (#\d*)?", tail) is not None


def read_back(description, reference, day, line_texts):
    """(name, num) of an entry as the importer wrote it, or None when its
    description and key do not read back in exactly one way."""
    key = reference or ""
    stamp = f" · {str(day)[:10]}"
    at = key.find(stamp)
    if at < 1:
        return None
    head, base = key[:at], key[: at + len(stamp)]
    split = split_head(head)
    desc = description or ""
    if split is None or not desc.startswith(head):
        return None
    rest = desc[len(head) :]
    cut = len(desc) >= DESCRIPTION_LENGTH
    # (name, memo) as the description may read: no name, then a memo or
    # nothing; or " — " and the first name, then ": " and the first memo
    if rest == "" or rest.startswith(": "):
        readings = [("", rest[2:])]
    elif rest.startswith(" — "):
        after = rest[3:]
        readings = [(after, "")] + [
            (after[:i], after[i + 2 :])
            for i in range(1, len(after))
            if after.startswith(": ", i)
        ]
    else:
        return None
    texts = set(line_texts)

    def fits(name, memo):
        if not (key_fits(key, base) or (name and key_fits(key, f"{base} · {name}"))):
            return False
        if memo:
            # the first memo is on its line, whole (a line held 300)
            return memo in texts or (cut and any(t.startswith(memo) for t in texts))
        # no memo anywhere: the first named line carries the name
        return not name or name in texts

    found = [name for name, memo in readings if fits(name, memo)]
    if len(found) != 1:
        return None
    return found[0], split[1]


def rewrite(bind) -> int:
    """Rewrite the entries the importer wrote before #663; how many."""
    t, ln = _transactions, _lines
    old = (t.c.source_type == SOURCE_TYPE) & t.c.source_ref.is_(None)
    rows = bind.execute(
        sa.select(t.c.id, t.c.date, t.c.description, t.c.reference).where(old)
    ).fetchall()
    if not rows:
        return 0
    texts = defaultdict(list)
    for txn_id, text in bind.execute(
        sa.select(ln.c.transaction_id, ln.c.description)
        .select_from(ln.join(t, t.c.id == ln.c.transaction_id))
        .where(old)
    ):
        texts[txn_id].append(text or "")
    changes = []
    for row in rows:
        found = read_back(row.description, row.reference, row.date, texts[row.id])
        if found is None:
            continue
        name, num = found
        changes.append(
            {"_id": row.id, "_name": name, "_num": num or None, "_key": row.reference}
        )
    if changes:
        bind.execute(
            t.update()
            .where(t.c.id == sa.bindparam("_id"))
            .values(
                description=sa.bindparam("_name"),
                reference=sa.bindparam("_num"),
                source_ref=sa.bindparam("_key"),
            ),
            changes,
        )
    return len(changes)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "transactions" not in insp.get_table_names():
        return
    if "source_ref" not in {c["name"] for c in insp.get_columns("transactions")}:
        op.add_column(
            "transactions", sa.Column("source_ref", sa.String(100), nullable=True)
        )
    rewrite(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "transactions" not in insp.get_table_names():
        return
    if "source_ref" not in {c["name"] for c in insp.get_columns("transactions")}:
        return
    t = _transactions
    bind.execute(
        t.update()
        .where((t.c.source_type == SOURCE_TYPE) & t.c.source_ref.isnot(None))
        .values(reference=t.c.source_ref)
    )
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("source_ref")
