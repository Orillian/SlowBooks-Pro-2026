"""The shared reversal refuses what the journal's Void refuses (#663).

reverse_journal_entry became the journal's Void and the ledger import's
replacement of a partial entry, but only the route refused an entry voided
already, a reversal, a pay run's payroll entry and an item's opening stock:
another caller could reverse any of them, a second time or behind the pay
run's back. The function refuses them itself now, with the route's own
answers, and the route answers as it did, in its own order (an
accountant's sign-in is told what an entry is before whose it is, and
whose before that it is voided already).
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.transactions import Transaction
from app.routes.journal import reverse_journal_entry
from tests.test_accountant_role import _as_cpa

REVERSAL = "Cannot void a reversal entry"
PAYROLL = (
    "This is a pay run's payroll entry. Void the pay run instead (Payroll, open "
    "the run, Void)."
)
OPENING_STOCK = (
    "An item's opening stock can't be voided here; correct it with an inventory "
    "adjustment on the item."
)
VOIDED = "This journal entry has already been voided."


def _entry(db, accounts, source_type):
    from app.services.accounting import create_journal_entry

    txn = create_journal_entry(
        db,
        date(2026, 2, 1),
        "An entry",
        [
            {
                "account_id": accounts["6300"].id,
                "debit": Decimal("10.00"),
                "credit": Decimal("0"),
            },
            {
                "account_id": accounts["2000"].id,
                "debit": Decimal("0"),
                "credit": Decimal("10.00"),
            },
        ],
        source_type=source_type,
    )
    db.commit()
    return txn


def _refused(db, txn) -> tuple:
    before = db.query(Transaction).count()
    with pytest.raises(HTTPException) as e:
        reverse_journal_entry(db, txn)
    db.rollback()
    assert db.query(Transaction).count() == before
    return e.value.status_code, e.value.detail


@pytest.mark.parametrize(
    "source_type, detail",
    [
        ("manual_void", REVERSAL),
        ("qbo_ledger_void", REVERSAL),
        ("payroll", PAYROLL),
        ("item_opening", OPENING_STOCK),
    ],
)
def test_it_refuses_what_an_entry_is(
    client, db_session, seed_accounts, source_type, detail
):
    txn = _entry(db_session, seed_accounts, source_type)
    assert _refused(db_session, txn) == (400, detail)
    # and the journal's Void answers as it did
    r = client.post(f"/api/journal/{txn.id}/void")
    assert (r.status_code, r.json()["detail"]) == (400, detail)


def test_it_refuses_an_entry_voided_already(client, db_session, seed_accounts):
    txn = _entry(db_session, seed_accounts, "manual")
    reverse_journal_entry(db_session, txn)
    db_session.commit()
    assert _refused(db_session, txn) == (400, VOIDED)
    reversals = db_session.query(Transaction).filter(Transaction.source_id == txn.id)
    assert reversals.count() == 1
    r = client.post(f"/api/journal/{txn.id}/void")
    assert (r.status_code, r.json()["detail"]) == (400, VOIDED)


def test_the_route_keeps_its_order(client, db_session, seed_accounts):
    """An accountant's sign-in is told a payroll entry is one before it is
    told the entry is not its own, and that an entry is not its own before
    that it is voided already: the order the route had."""
    payroll = _entry(db_session, seed_accounts, "payroll")
    owners = _entry(db_session, seed_accounts, "manual")
    reverse_journal_entry(db_session, owners)
    db_session.commit()
    _as_cpa(client, db_session)
    r = client.post(f"/api/journal/{payroll.id}/void")
    assert (r.status_code, r.json()["detail"]) == (400, PAYROLL)
    r = client.post(f"/api/journal/{owners.id}/void")
    assert r.status_code == 403, r.text
