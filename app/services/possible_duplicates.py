"""Possible Duplicates on a bank or card account (#414).

A migration brings the old books across as they are, so a bank line the
old program's feed added on top of one someone had keyed, or a statement
entered twice, arrives as two real entries. This lists them for a person
to look at; it never voids or deletes anything by itself.

Two entries on the account are a possible duplicate when:

- they are on the same side (both money out, or both money in) for the
  same amount, to the cent;
- their dates are at most WINDOW_DAYS apart;
- their payees are the same or similar (``similar_payees``);
- they are not both from QuickBooks Desktop's Transaction Detail by
  Account import (#663): QuickBooks already keeps its transactions apart
  (two transfers with no name, of one amount, a day apart), and the import
  skips a second copy of the same file. An imported entry still pairs with
  a typed, bank-feed or other entry, and two imported entries pair when
  either is a partial entry (one posted from a file that did not hold all
  of the transaction, its difference on Import Differences): that is where
  a transaction can come in twice.

A group is anchored on its first (earliest) entry: every later entry
within WINDOW_DAYS of that anchor, with a payee similar to the anchor's,
joins it, so three copies a day apart are one group, but a long run of
like charges a few days apart does not chain into one. Voided entries and reversals are left out. Every row reads
from the register's own query (``bank_register.account_register``), so the
payee, number and memo are the ones the register shows.

A group kept as "not a duplicate" is stored by its account and transaction
ids (models/duplicate_dismissals.py) and stays hidden while every entry of
a group found later is in that set.
"""

from __future__ import annotations

import hashlib
import io
from datetime import date
from decimal import Decimal
from typing import Iterable, Iterator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.accounts import Account
from app.models.banking import BankTransaction
from app.models.duplicate_dismissals import DuplicateDismissal
from app.models.transactions import Transaction, TransactionLine
from app.services.account_label import account_label
from app.services.bank_register import account_register
from app.services.duplicate_detection import normalize_name
from app.services.entry_text import QB_LEDGER_SOURCE

# "Within a few days of each other": a feed line and the same payment keyed
# by hand land a day or two apart; a weekly or monthly bill never does.
WINDOW_DAYS = 3

# Postings that came across from another program. A Make Deposits deposit
# and an imported one share "deposit", so those read as keyed.
_MIGRATED_FIXED = {
    "qbo_ledger",
    "qbo_journal",
    "qb_ledger_import",
    "iif_import",
    "check",  # QuickBooks Desktop's check register report (qb_report_import)
}


def migrated_sources() -> set[str]:
    from app.routes.migration import SOURCES

    return _MIGRATED_FIXED | {source_type for _, _, source_type in SOURCES.values()}


# ── payees ───────────────────────────────────────────────────────────────


def _normalized_words(text: str | None) -> tuple[str, ...]:
    """A payee's words after normalize_name (case, punctuation and business
    suffixes dropped), before any further filtering."""
    return tuple(normalize_name(text or "").split())


def payee_words(text: str | None) -> tuple[str, ...]:
    """A payee as the words compared: case, punctuation and business
    suffixes dropped (the customer/vendor duplicate check's normalize_name),
    then every word holding a digit dropped, so a store or terminal number
    ("#0423", "STORE 1182") or a date in a feed description does not make
    two lines from one shop look different."""
    return tuple(w for w in _normalized_words(text) if not any(c.isdigit() for c in w))


def similar_payees(a: str | None, b: str | None) -> bool:
    """The same or a similar payee, conservatively.

    A blank payee is the same only as another blank payee; one blank and one
    named is not. A payee that is only a number ("76", a store or pump
    number) names no shop, so it is not treated as blank and matches nothing,
    including a blank payee.

    Otherwise the shorter name must be the *start* of the longer one, whole
    words in order, so "Home Depot" matches "The Home Depot #4410 Orlando"
    but a one-word "Deposit" does not match "ATM Deposit" (it is not the
    start). No fuzzy spelling: "Shell" and "Shellfish Co" are different."""
    raw_a, raw_b = _normalized_words(a), _normalized_words(b)
    if not raw_a and not raw_b:
        return True
    if not raw_a or not raw_b:
        return False
    wa, wb = payee_words(a), payee_words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return long_[: len(short)] == short


# ── the report ───────────────────────────────────────────────────────────


# SQLite allows at most ~32k bound parameters in one statement; a single
# IN (...) over every entry on a busy account blows past that. 900 keeps each
# chunk well under the limit with room for the query's other parameters.
_IN_CHUNK = 900


def _chunks(seq: Iterable, size: int = _IN_CHUNK) -> Iterator[list]:
    items = list(seq)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _key(txn_ids) -> str:
    return ",".join(str(i) for i in sorted(set(txn_ids)))


def _ids(key: str) -> set[int]:
    return {int(p) for p in key.split(",") if p}


def _group_hash(txn_ids) -> str:
    """A fixed-length key for a dismissed group: sha256 hex of its sorted
    ids. Used for the per-account unique constraint so the key never
    overflows a column, however long the id list is. The ids themselves are
    kept (as text) for display and the subset match."""
    return hashlib.sha256(_key(txn_ids).encode()).hexdigest()


def _void_path(source_type: str, txn_id: int, source_id) -> str | None:
    """The route that voids this entry, under /api — the same one its own
    page uses, so the closing date, the reconciliation rule and the audit
    log apply as they always do. None: no void from here (open it)."""
    from app.routes.journal import _journal_sources

    if source_type == "bank_entry":
        return f"/banking/entries/{txn_id}/void"
    if source_type == "transfer":
        return f"/transfers/{txn_id}/void"
    if source_type == "cc_charge":
        return f"/cc-charges/{txn_id}/void"
    if source_type == "expense":
        return f"/expenses/{txn_id}/void"
    if source_type == "deposit":
        return f"/deposits/{txn_id}/void"
    if source_type == "payment" and source_id:
        return f"/payments/{source_id}/void"
    if source_type == "bill_payment" and source_id:
        return f"/bill-payments/{source_id}/void"
    # Journals in their own right, with no document behind them: the
    # QuickBooks Online ledger import, and the QuickBooks Desktop check
    # register report's checks (source_type "check", #414's own migration),
    # which post as a plain journal. /journal void reverses the lines and
    # keeps the reconciliation and closing-date rules.
    if source_type in set(_journal_sources()) | {"qbo_ledger", "check"}:
        return f"/journal/{txn_id}/void"
    return None


def require_register_account(db: Session, account_id: int | None) -> Account:
    """The bank or card account asked for; the first active bank account
    when none is named, as the register opens."""
    if account_id is None:
        acct = (
            db.query(Account)
            .filter(Account.bank_kind.isnot(None), Account.is_active)
            .order_by(Account.account_number, Account.name)
            .first()
        )
        if acct is None:
            raise HTTPException(
                status_code=404, detail="There is no bank or credit-card account"
            )
        return acct
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not acct.bank_kind:
        raise HTTPException(
            status_code=400,
            detail=f"{acct.name} is not a bank or credit-card account",
        )
    return acct


def _sides(account: Account) -> dict[str, str]:
    """The register's own column words for each side."""
    if account.bank_kind == "credit_card":
        return {"credit": "Charge", "debit": "Payment"}
    return {"credit": "Payment", "debit": "Deposit"}


def _voided_payments(db: Session, entries) -> set[tuple]:
    """(source_type, id) of the payments and bill payments among `entries`
    that are void. Their reversal points at the payment's id, not the
    posting's, so the register's own voided flag does not see them."""
    from app.models.bills import BillPayment
    from app.models.payments import Payment

    out = set()
    for kind, model in (("payment", Payment), ("bill_payment", BillPayment)):
        ids = {e["source_id"] for e in entries if e["source_type"] == kind}
        ids.discard(None)
        for chunk in _chunks(ids):
            out |= {
                (kind, r[0])
                for r in db.query(model.id).filter(
                    model.id.in_(chunk), model.is_voided.is_(True)
                )
            }
    return out


def possible_duplicates(
    db: Session,
    account: Account,
    start: date,
    end: date,
    include_dismissed: bool = False,
) -> dict:
    reg = account_register(db, account, start, end)
    dead = _voided_payments(db, reg["entries"])
    entries = [
        e
        for e in reg["entries"]
        if not e["voided"] and (e["source_type"], e["source_id"]) not in dead
    ]

    # imported: posted from a bank statement line (Add on the review list).
    # Chunked so a busy account's line ids never exceed SQLite's parameter
    # limit in one IN (...) (see _chunks).
    line_ids = [e["line_id"] for e in entries]
    imported: set[int] = set()
    for chunk in _chunks(line_ids):
        imported |= {
            r[0]
            for r in db.query(BankTransaction.transaction_line_id)
            .filter(BankTransaction.transaction_line_id.in_(chunk))
            .filter(BankTransaction.match_status == "added")
        }
    migrated = migrated_sources()
    # the ledger import's partial entries (#663): two imported entries pair
    # again when either is one
    partial: set[int] = set()
    for chunk in _chunks(
        {e["transaction_id"] for e in entries if e["source_type"] == QB_LEDGER_SOURCE}
    ):
        partial |= {
            r[0]
            for r in db.query(Transaction.id).filter(
                Transaction.id.in_(chunk), Transaction.source_difference.isnot(None)
            )
        }

    # same side, same amount; then pairs close enough in date and payee
    buckets: dict[tuple, list] = {}
    for e in entries:
        side = "debit" if e["debit"] > 0 else "credit"
        cents = int((Decimal(str(e["debit"] or e["credit"])) * 100).to_integral_value())
        if cents == 0:
            continue
        buckets.setdefault((side, cents), []).append(e)

    # A group is bounded by the window measured from its *first* (earliest)
    # entry, not pair-to-pair, so a long run of like charges a few days apart
    # cannot chain into one ever-growing group and one late copy cannot
    # resurrect the whole run. Sweep each (side, amount) bucket in date order:
    # the earliest ungrouped entry anchors a group, and every later entry
    # within WINDOW_DAYS of that anchor with a matching payee joins it.
    groups_raw = []
    for (side, cents), rows in buckets.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda e: (e["date"], e["transaction_id"], e["line_id"]))
        days = [date.fromisoformat(e["date"]) for e in rows]
        used = [False] * len(rows)
        for i in range(len(rows)):
            if used[i]:
                continue
            anchor = rows[i]
            members = [anchor]
            used[i] = True
            for j in range(i + 1, len(rows)):
                if (days[j] - days[i]).days > WINDOW_DAYS:
                    break  # rows are date-sorted: nothing later is nearer
                if used[j] or rows[j]["transaction_id"] == anchor["transaction_id"]:
                    continue
                if (
                    anchor["source_type"] == rows[j]["source_type"] == QB_LEDGER_SOURCE
                    and anchor["transaction_id"] not in partial
                    and rows[j]["transaction_id"] not in partial
                ):
                    continue
                if similar_payees(anchor["payee"], rows[j]["payee"]):
                    members.append(rows[j])
                    used[j] = True
            if len({m["transaction_id"] for m in members}) >= 2:
                groups_raw.append((side, cents, members))

    dismissals = (
        db.query(DuplicateDismissal)
        .filter(DuplicateDismissal.account_id == account.id)
        .all()
    )
    dismissed_sets = [(d, _ids(d.transaction_ids)) for d in dismissals]

    sides = _sides(account)
    groups = []
    hidden = 0
    for side, cents, members in sorted(
        groups_raw, key=lambda g: (g[2][0]["date"], g[2][0]["transaction_id"])
    ):
        txn_ids = {m["transaction_id"] for m in members}
        kept = next((d for d, ids in dismissed_sets if txn_ids <= ids), None)
        if kept is not None:
            hidden += 1
            if not include_dismissed:
                continue
        rows = []
        for m in members:
            reconciled = m["reconciliation_id"] is not None
            void_path = _void_path(
                m["source_type"] or "", m["transaction_id"], m["source_id"]
            )
            rows.append(
                {
                    "transaction_id": m["transaction_id"],
                    "line_id": m["line_id"],
                    "date": m["date"],
                    "reference": m["reference"],
                    "payee": m["payee"],
                    "memo": m["description"],
                    "source_type": m["source_type"],
                    "source_kind": m["source_kind"],
                    "source": (
                        "imported"
                        if m["line_id"] in imported
                        else (
                            "migrated"
                            if (m["source_type"] or "") in migrated
                            else "keyed"
                        )
                    ),
                    "link": m["source_link"] or f"/#/journal/{m['transaction_id']}",
                    "cleared": m["cleared"],
                    "reconciled": reconciled,
                    "void_path": void_path,
                    "voidable": bool(void_path) and not reconciled,
                }
            )
        groups.append(
            {
                "key": _key(txn_ids),
                "side": side,
                "side_label": sides[side],
                "amount": float(Decimal(cents) / 100),
                "any_reconciled": any(r["reconciled"] for r in rows),
                "dismissed": kept is not None,
                "dismissal_id": kept.id if kept is not None else None,
                "dismissed_by": kept.dismissed_by if kept is not None else None,
                "entries": rows,
            }
        )
    from app.services.settings_service import use_account_numbers

    return {
        "account": reg["account"],
        # whether the title prints the register's number (#567)
        "numbered": use_account_numbers(db),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "window_days": WINDOW_DAYS,
        "include_dismissed": include_dismissed,
        "count": sum(1 for g in groups if not g["dismissed"]),
        "dismissed_count": hidden,
        "groups": groups,
    }


# ── not a duplicate ──────────────────────────────────────────────────────


def dismiss(db: Session, account: Account, txn_ids, actor: str | None):
    """Keep a group: the entries must be two or more on this account."""
    ids = {int(i) for i in txn_ids}
    if len(ids) < 2:
        raise HTTPException(
            status_code=400, detail="A group is two or more entries on the account"
        )
    on_account: set[int] = set()
    for chunk in _chunks(ids):  # chunked for the parameter limit (see _chunks)
        on_account |= {
            r[0]
            for r in db.query(TransactionLine.transaction_id)
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .filter(TransactionLine.account_id == account.id)
            .filter(TransactionLine.transaction_id.in_(chunk))
        }
    missing = ids - on_account
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Not on {account.name}: entry {', '.join(str(i) for i in sorted(missing))}",
        )
    key = _key(ids)
    group_hash = _group_hash(ids)
    row = (
        db.query(DuplicateDismissal)
        .filter(
            DuplicateDismissal.account_id == account.id,
            DuplicateDismissal.group_hash == group_hash,
        )
        .first()
    )
    if row is None:
        row = DuplicateDismissal(
            account_id=account.id,
            transaction_ids=key,
            group_hash=group_hash,
            dismissed_by=actor,
        )
        db.add(row)
        db.flush()
    return row


# ── Save CSV / Save PDF ──────────────────────────────────────────────────


def _account_label(data: dict) -> str:
    a = data["account"]
    return account_label(a.get("number"), a["name"], data.get("numbered", True), " ")


def report_csv(data: dict, company: str) -> str:
    """One row per entry, the group's number, side and amount on each, so a
    spreadsheet sorts and filters by group."""
    from app.services.ledger_exports import _SafeWriter, _money, _preamble

    out = io.StringIO()
    w = _SafeWriter(out)
    _preamble(
        w,
        company,
        f"Possible Duplicates — {_account_label(data)}",
        f"{data['start_date']} to {data['end_date']}",
    )
    w.writerow(
        [
            "Group",
            "Side",
            "Amount",
            "Date",
            "Number",
            "Payee",
            "Memo",
            "Source",
            "Reconciled",
            "Dismissed",
            "Entry",
        ]
    )
    for n, g in enumerate(data["groups"], 1):
        for e in g["entries"]:
            w.writerow(
                [
                    n,
                    g["side_label"],
                    _money(g["amount"]),
                    e["date"],
                    e["reference"],
                    e["payee"],
                    e["memo"],
                    e["source"],
                    "yes" if e["reconciled"] else "",
                    "yes" if g["dismissed"] else "",
                    e["transaction_id"],
                ]
            )
    return out.getvalue()


def report_section(data: dict) -> dict:
    """The report as one section for generate_report_pdf."""
    from app.services.bank_register import money_text

    rows = []
    for n, g in enumerate(data["groups"], 1):
        head = (
            f"Group {n} · {g['side_label']} {money_text(g['amount'])}"
            f" · {len(g['entries'])} entries"
        )
        if g["any_reconciled"]:
            head += " · reconciled entry"
        if g["dismissed"]:
            head += " · not a duplicate"
        rows.append({"cells": [head, "", "", "", "", ""], "style": "subtotal"})
        for e in g["entries"]:
            rows.append(
                {
                    "cells": [
                        e["date"],
                        e["reference"],
                        e["payee"],
                        e["memo"],
                        e["source"],
                        "yes" if e["reconciled"] else "",
                    ]
                }
            )
    if not rows:
        rows.append(
            {"cells": ["No possible duplicates in this period", "", "", "", "", ""]}
        )
    return {
        "title": f"Possible Duplicates — {_account_label(data)}",
        "period": f"{data['start_date']} — {data['end_date']}",
        "columns": ["Date", "Number", "Payee", "Memo", "Source", "Reconciled"],
        "rows": rows,
    }
