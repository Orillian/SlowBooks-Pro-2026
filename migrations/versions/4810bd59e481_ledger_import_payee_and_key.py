"""transactions.source_ref, source_kind and source_difference; ledger
imports read as register entries (#663)

QuickBooks Desktop's Transaction Detail by Account import wrote each journal's
description as "{Type} {Num} — {Name}: {Memo}" and kept its duplicate key,
"{Type} {Num} · {date} · {name}" (and " #2"... when a key repeats in a file),
as the reference. The register shows a journal's description as its payee,
so payees read "Check EFT — Acme Supply: memo". From here the import writes
QuickBooks' Name as the description (the payee; empty with no name), its Num
as the reference (Ref #), the key to source_ref and QuickBooks' Type to
source_kind, columns of their own; each line's memo stays on the line, where
it always was. The register's Type column shows the type ("Check
(imported)").

The entries already imported are rewritten the same way, once: source_type
qb_ledger_import only, and only an entry whose description and key read back
one way. The key's "{Type} {Num}" splits after the longest of QuickBooks'
types it starts with (all of it is the type when it holds no space), and the
key holds the name when it was the journal's only one. The description gives
the name, then ": " and the first memo when there was one; a name or memo may
hold ":" or " — ", so a reading must also agree with the key and with the
lines, which carry each memo or, with none, the name. An entry with no such
reading, or more than one, is left as it was, and the import still finds its
key in the reference. An entry already in the new shape whose key a
downgrade put back in the reference (its description the key's one name, or
empty with a key that holds no name) gets its Num and key back the same
way. An entry rewritten gets the type its key starts with as its
source_kind; one left as it was has none. Written out here so this
migration never changes with the importer.

source_difference marks a partial entry: one the import posted from a file
that did not hold all of the transaction, the file's debits less its
credits, put on Import Differences on a line of its own memo ("Difference in
the QuickBooks file"). A later file that holds the whole transaction offers
to replace it. The upgrade fills it from that line on the entries that have
one (after a downgrade, which drops it); every other entry leaves it NULL.

downgrade puts each key back in the reference, where the import looked for
it before, and drops the three columns; the descriptions keep the name, and
an upgrade again reads those entries back as above.

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
    sa.column("source_kind", sa.String),
    sa.column("source_difference", sa.Numeric(15, 2)),
)
_lines = sa.table(
    "transaction_lines",
    sa.column("id", sa.Integer),
    sa.column("transaction_id", sa.Integer),
    sa.column("description", sa.String),
    sa.column("debit", sa.Numeric(15, 2)),
    sa.column("credit", sa.Numeric(15, 2)),
)
# the memo the import gives the line it puts a partial entry's difference on
DIFFERENCE_MEMO = "Difference in the QuickBooks file"


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


def read_back_downgraded(description, reference, day):
    """(name, num) of an entry the importer wrote in the new shape whose key
    a downgrade put back in the reference: its description is the key's one
    name, or empty with a key that holds none. None for anything else."""
    key = reference or ""
    stamp = f" · {str(day)[:10]}"
    at = key.find(stamp)
    split = split_head(key[:at]) if at > 0 else None
    if split is None:
        return None
    base, name = key[: at + len(stamp)], description or ""
    if key_fits(key, f"{base} · {name}" if name else base):
        return name, split[1]
    return None


def type_of(reference, day):
    """QuickBooks' Type at the head of an entry's key, or None."""
    key = reference or ""
    at = key.find(f" · {str(day)[:10]}")
    split = split_head(key[:at]) if at > 0 else None
    return split[0] if split else None


def rewrite(bind) -> int:
    """Rewrite the entries the importer wrote before #663, and those a
    downgrade put back; how many."""
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
        found = read_back(
            row.description, row.reference, row.date, texts[row.id]
        ) or read_back_downgraded(row.description, row.reference, row.date)
        if found is None:
            continue
        name, num = found
        changes.append(
            {
                "_id": row.id,
                "_name": name,
                "_num": num or None,
                "_key": row.reference,
                "_kind": (type_of(row.reference, row.date) or "")[:50] or None,
            }
        )
    if changes:
        bind.execute(
            t.update()
            .where(t.c.id == sa.bindparam("_id"))
            .values(
                description=sa.bindparam("_name"),
                reference=sa.bindparam("_num"),
                source_ref=sa.bindparam("_key"),
                source_kind=sa.bindparam("_kind"),
            ),
            changes,
        )
    return len(changes)


def mark_partial(bind) -> int:
    """Fill source_difference on the entries that carry an Import
    Differences line: the line's credit less its debit (the file's debits
    less its credits). How many."""
    t, ln = _transactions, _lines
    found = defaultdict(lambda: 0)
    for txn_id, debit, credit in bind.execute(
        sa.select(ln.c.transaction_id, ln.c.debit, ln.c.credit)
        .select_from(ln.join(t, t.c.id == ln.c.transaction_id))
        .where(
            (t.c.source_type == SOURCE_TYPE)
            & t.c.source_difference.is_(None)
            & (ln.c.description == DIFFERENCE_MEMO)
        )
    ):
        found[txn_id] += (credit or 0) - (debit or 0)
    if found:
        bind.execute(
            t.update()
            .where(t.c.id == sa.bindparam("_id"))
            .values(source_difference=sa.bindparam("_off")),
            [{"_id": i, "_off": off} for i, off in found.items()],
        )
    return len(found)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "transactions" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("transactions")}
    if "source_ref" not in have:
        op.add_column(
            "transactions", sa.Column("source_ref", sa.String(100), nullable=True)
        )
    if "source_kind" not in have:
        op.add_column(
            "transactions", sa.Column("source_kind", sa.String(50), nullable=True)
        )
    if "source_difference" not in have:
        op.add_column(
            "transactions",
            sa.Column("source_difference", sa.Numeric(15, 2), nullable=True),
        )
    rewrite(bind)
    mark_partial(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "transactions" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("transactions")}
    if "source_ref" not in have:
        return
    t = _transactions
    bind.execute(
        t.update()
        .where((t.c.source_type == SOURCE_TYPE) & t.c.source_ref.isnot(None))
        .values(reference=t.c.source_ref)
    )
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("source_ref")
        if "source_kind" in have:
            batch.drop_column("source_kind")
        if "source_difference" in have:
            batch.drop_column("source_difference")
