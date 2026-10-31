"""The Possible Duplicates report (#414): entries on a bank or card account
with the same amount on the same side and the same or a similar payee a
few days apart are grouped; two equal monthly payments are not. A group
can be kept as "not a duplicate", for good; an entry is voided through its
own void route, so the reconciliation and closing-date rules hold. Reading
is every role's; dismissing is a write."""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.audit import AuditLog
from app.models.banking import BankAccount, BankTransaction, Reconciliation
from app.models.duplicate_dismissals import DuplicateDismissal
from app.models.users import (
    ROLE_ACCOUNTANT,
    ROLE_BOOKKEEPER,
    ROLE_READONLY,
    User,
)
from app.services import auth as auth_service
from app.services.accounting import create_journal_entry
from app.services.bank_posting import post_bank_entry
from app.services.possible_duplicates import WINDOW_DAYS, similar_payees

URL = "/api/reports/possible-duplicates"
MARCH = "start_date=2026-03-01&end_date=2026-04-30"
OTHER_PW = "other-password-123"


def _keyed(db, accounts, day, amount, payee, number="1000", category="6000"):
    txn = post_bank_entry(
        db,
        accounts[number],
        day,
        Decimal(amount),
        accounts[category],
        payee=payee,
        reference=None,
    )
    db.commit()
    return txn


def _migrated(db, accounts, day, amount, text, number="1000", category="6000"):
    """A journal the QuickBooks Online export import posted."""
    amt = Decimal(amount)
    bank, other = accounts[number].id, accounts[category].id
    lines = (
        [
            {"account_id": other, "debit": -amt, "credit": Decimal("0")},
            {"account_id": bank, "debit": Decimal("0"), "credit": -amt},
        ]
        if amt < 0
        else [
            {"account_id": bank, "debit": amt, "credit": Decimal("0")},
            {"account_id": other, "debit": Decimal("0"), "credit": amt},
        ]
    )
    txn = create_journal_entry(
        db, day, text, lines, source_type="qbo_export_import", reference="J-7"
    )
    db.commit()
    return txn


def _check(db, accounts, day, amount, text, number="1000", category="6000"):
    """A check the QuickBooks Desktop check-register report import posts: a
    plain journal with source_type "check" (CR bank, DR the expense)."""
    amt = Decimal(amount)
    bank, other = accounts[number].id, accounts[category].id
    lines = [
        {"account_id": other, "debit": -amt, "credit": Decimal("0")},
        {"account_id": bank, "debit": Decimal("0"), "credit": -amt},
    ]
    txn = create_journal_entry(
        db, day, text, lines, source_type="check", reference="1042"
    )
    db.commit()
    return txn


def _report(client, accounts, number="1000", extra=""):
    r = client.get(f"{URL}?account_id={accounts[number].id}&{MARCH}{extra}")
    assert r.status_code == 200, r.text
    return r.json()


def _user(db, username, role):
    db.add(
        User(
            username=username,
            display_name=username.title(),
            password_hash=auth_service.hash_password(OTHER_PW),
            role=role,
            is_active=True,
        )
    )
    db.commit()


def _login_as(client, username):
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login", json={"username": username, "password": OTHER_PW}
    )
    assert r.status_code == 200, r.text


# ── payees ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a, b",
    [
        ("Home Depot", "HOME DEPOT"),
        ("Home Depot #4410", "The Home Depot 4410 Orlando"),
        ("Shell Oil 57442", "SHELL OIL 12345"),
        ("Duke Energy, Inc.", "duke energy"),
        ("", ""),
    ],
)
def test_similar_payees(a, b):
    assert similar_payees(a, b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("Shell", "Shellfish Co"),
        ("Home Depot", "Lowe's"),
        ("Depot Home", "Home Depot"),
        ("", "Home Depot"),
        # a one-word payee must be the start of the longer name, not sit
        # anywhere inside it: a generic "Deposit" is not "ATM Deposit"
        ("Deposit", "ATM Deposit"),
        ("ATM Deposit", "Deposit"),
        # a payee that is only a number (a store/pump number) names no shop,
        # so it matches neither a blank payee nor a named one
        ("76", ""),
        ("76", "Shell"),
        ("Store 1182", ""),
    ],
)
def test_different_payees(a, b):
    assert not similar_payees(a, b)


def test_the_window_is_a_few_days():
    assert WINDOW_DAYS == 3


# ── grouping ─────────────────────────────────────────────────────────────


def test_a_keyed_entry_and_its_migrated_copy_are_one_group(
    client, db_session, seed_accounts
):
    keyed = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-120.00", "Home Depot")
    copy = _migrated(
        db_session, seed_accounts, date(2026, 3, 4), "-120.00", "HOME DEPOT #4410"
    )
    data = _report(client, seed_accounts)
    assert data["count"] == 1 and data["window_days"] == 3
    (g,) = data["groups"]
    assert g["side"] == "credit" and g["side_label"] == "Payment"
    assert g["amount"] == 120.0 and g["key"] == f"{keyed.id},{copy.id}"
    by_id = {e["transaction_id"]: e for e in g["entries"]}
    assert by_id[keyed.id]["source"] == "keyed"
    assert by_id[keyed.id]["payee"] == "Home Depot"
    assert by_id[copy.id]["source"] == "migrated"
    assert by_id[copy.id]["reference"] == "J-7"
    assert by_id[keyed.id]["void_path"] == f"/banking/entries/{keyed.id}/void"
    assert by_id[copy.id]["void_path"] == f"/journal/{copy.id}/void"
    assert all(e["voidable"] and not e["reconciled"] for e in g["entries"])
    assert by_id[keyed.id]["link"]


def test_three_copies_a_day_apart_are_one_group(client, db_session, seed_accounts):
    for day in (10, 11, 13):
        _keyed(db_session, seed_accounts, date(2026, 3, day), "-45.00", "Cafe Bleu")
    (g,) = _report(client, seed_accounts)["groups"]
    assert len(g["entries"]) == 3


def test_a_run_three_days_apart_does_not_chain_into_one_group(
    client, db_session, seed_accounts
):
    """Ten like charges three days apart must not join into one 27-day group:
    the window is measured from each group's first entry, not neighbour to
    neighbour, so the run breaks into bounded groups of two."""
    for day in range(1, 29, 3):  # 1, 4, 7, ... 28 — ten entries
        _keyed(db_session, seed_accounts, date(2026, 3, day), "-5.00", "Parking")
    groups = _report(client, seed_accounts)["groups"]
    assert len(groups) == 5
    assert all(len(g["entries"]) == 2 for g in groups)


def test_a_daily_repeating_charge_does_not_make_a_giant_group(
    client, db_session, seed_accounts
):
    """A charge every day is bounded to the window from each group's first
    entry, so it never grows into one ever-larger group."""
    for day in range(1, 9):  # eight consecutive days
        _keyed(db_session, seed_accounts, date(2026, 3, day), "-9.00", "Toll")
    groups = _report(client, seed_accounts)["groups"]
    assert len(groups) == 2
    assert all(len(g["entries"]) == WINDOW_DAYS + 1 for g in groups)


def test_equal_monthly_payments_are_not_grouped(client, db_session, seed_accounts):
    _keyed(db_session, seed_accounts, date(2026, 3, 1), "-1500.00", "Oak Street Rent")
    _keyed(db_session, seed_accounts, date(2026, 3, 31), "-1500.00", "Oak Street Rent")
    assert _report(client, seed_accounts)["groups"] == []


def test_four_days_apart_is_outside_the_window(client, db_session, seed_accounts):
    _keyed(db_session, seed_accounts, date(2026, 3, 1), "-80.00", "Sparkle Cleaning")
    _keyed(db_session, seed_accounts, date(2026, 3, 5), "-80.00", "Sparkle Cleaning")
    assert _report(client, seed_accounts)["groups"] == []


def test_the_other_side_is_not_a_duplicate(client, db_session, seed_accounts):
    # a refund the same day, for the same amount, from the same shop
    _keyed(db_session, seed_accounts, date(2026, 3, 9), "-60.00", "Staples")
    _keyed(
        db_session, seed_accounts, date(2026, 3, 9), "60.00", "Staples", category="4000"
    )
    assert _report(client, seed_accounts)["groups"] == []


def test_another_payee_or_amount_is_not_a_duplicate(client, db_session, seed_accounts):
    _keyed(db_session, seed_accounts, date(2026, 3, 9), "-60.00", "Staples")
    _keyed(db_session, seed_accounts, date(2026, 3, 9), "-60.00", "Office Depot")
    _keyed(db_session, seed_accounts, date(2026, 3, 10), "-60.01", "Staples")
    assert _report(client, seed_accounts)["groups"] == []


def test_a_voided_copy_drops_out(client, db_session, seed_accounts):
    _keyed(db_session, seed_accounts, date(2026, 3, 9), "-60.00", "Staples")
    second = _keyed(db_session, seed_accounts, date(2026, 3, 9), "-60.00", "Staples")
    assert client.post(f"/api/banking/entries/{second.id}/void").status_code == 200
    assert _report(client, seed_accounts)["groups"] == []


def test_a_voided_payment_drops_out(client, db_session, seed_accounts, seed_customer):
    """A customer payment's void points at the payment, not its posting,
    so the register's voided flag misses it; the report does not."""
    # postings elsewhere first, so a payment's id is not its posting's
    for n in range(5):
        _keyed(db_session, seed_accounts, date(2026, 1, 5), f"-{n + 1}.00", "x", "1010")
    ids = []
    for _ in range(2):
        r = client.post(
            "/api/payments",
            json={
                "customer_id": seed_customer.id,
                "amount": "300.00",
                "date": "2026-03-09",
                "method": "check",
                "deposit_to_account_id": seed_accounts["1000"].id,
                "allocations": [],
            },
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    (g,) = _report(client, seed_accounts)["groups"]
    assert g["side_label"] == "Deposit"
    paths = sorted(e["void_path"] for e in g["entries"])
    assert paths == sorted(f"/payments/{i}/void" for i in ids)
    assert client.post(f"/api{paths[0]}").status_code == 200
    assert _report(client, seed_accounts)["groups"] == []


def test_a_card_reads_charge_and_an_imported_line_says_so(
    client, db_session, seed_accounts
):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 9), "-30.00", "Netflix", "2100")
    _keyed(db_session, seed_accounts, date(2026, 3, 9), "-30.00", "NETFLIX.COM", "2100")
    feed = BankAccount(name="Card feed", account_id=seed_accounts["2100"].id)
    db_session.add(feed)
    db_session.flush()
    card_line = next(ln for ln in a.lines if ln.account_id == seed_accounts["2100"].id)
    db_session.add(
        BankTransaction(
            bank_account_id=feed.id,
            date=date(2026, 3, 9),
            amount=Decimal("-30.00"),
            payee="Netflix",
            transaction_id=a.id,
            transaction_line_id=card_line.id,
            match_status="added",
        )
    )
    db_session.commit()
    (g,) = _report(client, seed_accounts, "2100")["groups"]
    assert g["side_label"] == "Charge"
    sources = {e["transaction_id"]: e["source"] for e in g["entries"]}
    assert sources[a.id] == "imported"
    assert sorted(sources.values()) == ["imported", "keyed"]


def test_only_bank_and_card_accounts(client, seed_accounts):
    r = client.get(f"{URL}?account_id={seed_accounts['6000'].id}&{MARCH}")
    assert r.status_code == 400


# ── not a duplicate ──────────────────────────────────────────────────────


def test_not_a_duplicate_stays_dismissed(client, db_session, seed_accounts):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    b = _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "Pizza Barn")
    r = client.post(
        f"{URL}/dismiss",
        json={"account_id": seed_accounts["1000"].id, "transaction_ids": [b.id, a.id]},
    )
    assert r.status_code == 200, r.text
    kept = db_session.query(DuplicateDismissal).one()
    assert kept.transaction_ids == f"{a.id},{b.id}"
    # audited like any write
    assert (
        db_session.query(AuditLog)
        .filter_by(table_name="duplicate_dismissals", action="INSERT")
        .count()
        == 1
    )

    data = _report(client, seed_accounts)
    assert data["groups"] == [] and data["count"] == 0
    assert data["dismissed_count"] == 1
    shown = _report(client, seed_accounts, extra="&include_dismissed=true")
    (g,) = shown["groups"]
    assert g["dismissed"] and g["dismissal_id"] == kept.id

    # a third copy is news: the group comes back
    c = _keyed(db_session, seed_accounts, date(2026, 3, 4), "-25.00", "Pizza Barn")
    (g,) = _report(client, seed_accounts)["groups"]
    assert {e["transaction_id"] for e in g["entries"]} == {a.id, b.id, c.id}

    # and put back by hand
    r = client.delete(f"{URL}/dismissals/{kept.id}")
    assert r.status_code == 200
    assert db_session.query(DuplicateDismissal).count() == 0


def test_dismiss_takes_entries_on_the_account(client, db_session, seed_accounts):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    b = _keyed(
        db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn", "1010"
    )
    acct = seed_accounts["1000"].id
    r = client.post(
        f"{URL}/dismiss", json={"account_id": acct, "transaction_ids": [a.id, b.id]}
    )
    assert r.status_code == 400
    r = client.post(
        f"{URL}/dismiss", json={"account_id": acct, "transaction_ids": [a.id]}
    )
    assert r.status_code == 400


# ── voiding from the report ──────────────────────────────────────────────


def test_a_reconciled_entry_is_not_offered_and_is_refused(
    client, db_session, seed_accounts
):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-75.00", "Fuel Stop")
    _keyed(db_session, seed_accounts, date(2026, 3, 2), "-75.00", "Fuel Stop")
    recon = Reconciliation(
        account_id=seed_accounts["1000"].id,
        statement_date=date(2026, 3, 31),
        statement_balance=Decimal("0"),
    )
    db_session.add(recon)
    db_session.flush()
    for ln in a.lines:
        if ln.account_id == seed_accounts["1000"].id:
            ln.reconciliation_id = recon.id
            ln.cleared = True
    db_session.commit()

    (g,) = _report(client, seed_accounts)["groups"]
    assert g["any_reconciled"]
    entry = next(e for e in g["entries"] if e["transaction_id"] == a.id)
    assert entry["reconciled"] and not entry["voidable"]
    r = client.post(f"/api{entry['void_path']}")
    assert r.status_code == 400
    assert "reconciliation" in r.json()["detail"]


def test_a_void_in_a_closed_period_asks_for_the_password(
    client, db_session, seed_accounts
):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-75.00", "Fuel Stop")
    b = _migrated(db_session, seed_accounts, date(2026, 3, 2), "-75.00", "Fuel Stop")
    r = client.put(
        "/api/settings",
        json={"closing_date": "2026-03-31", "closing_date_password": "shut-tight"},
    )
    assert r.status_code == 200
    (g,) = _report(client, seed_accounts)["groups"]
    for e in g["entries"]:
        r = client.post(f"/api{e['void_path']}")
        assert r.status_code == 403, (e["source_type"], r.text)
        assert "closing date" in r.json()["detail"]
    # with the password, the keyed copy goes and the group with it
    r = client.post(
        f"/api/banking/entries/{a.id}/void",
        headers={"X-Closing-Date-Password": "shut-tight"},
    )
    assert r.status_code == 200, r.text
    assert _report(client, seed_accounts)["groups"] == []
    assert b.id  # the migrated one stays


def test_an_imported_qb_desktop_check_can_be_voided(client, db_session, seed_accounts):
    """A QuickBooks Desktop check-register check (source_type "check") posts
    as a journal of its own, so it had Open but no Void; it now routes to
    /journal void, which reverses it and keeps the same rules (#414)."""
    check = _check(db_session, seed_accounts, date(2026, 3, 2), "-200.00", "City Water")
    _keyed(db_session, seed_accounts, date(2026, 3, 3), "-200.00", "City Water")
    (g,) = _report(client, seed_accounts)["groups"]
    entry = next(e for e in g["entries"] if e["transaction_id"] == check.id)
    assert entry["source"] == "migrated"
    assert entry["void_path"] == f"/journal/{check.id}/void"
    assert entry["voidable"]
    # it really voids, and the group goes with it
    r = client.post(f"/api{entry['void_path']}")
    assert r.status_code == 200, r.text
    assert _report(client, seed_accounts)["groups"] == []


def test_an_imported_check_in_a_closed_period_keeps_the_rule(
    client, db_session, seed_accounts
):
    """Routing a check to /journal void keeps the closing-date rule."""
    _check(db_session, seed_accounts, date(2026, 3, 2), "-200.00", "City Water")
    other = _check(db_session, seed_accounts, date(2026, 3, 3), "-200.00", "City Water")
    assert (
        client.put(
            "/api/settings",
            json={"closing_date": "2026-03-31", "closing_date_password": "shut-tight"},
        ).status_code
        == 200
    )
    (g,) = _report(client, seed_accounts)["groups"]
    entry = next(e for e in g["entries"] if e["transaction_id"] == other.id)
    r = client.post(f"/api{entry['void_path']}")
    assert r.status_code == 403
    assert "closing date" in r.json()["detail"]


# ── QuickBooks Desktop's ledger import (#663) ───────────────────────────


def _ledger(db, accounts, day, amount, name, number="1000", category="3100"):
    """A journal QuickBooks Desktop's Transaction Detail by Account import
    posted: QuickBooks' Name on the header (none on a transfer)."""
    amt = Decimal(amount)
    bank, other = accounts[number].id, accounts[category].id
    lines = (
        [
            {"account_id": other, "debit": -amt, "credit": Decimal("0")},
            {"account_id": bank, "debit": Decimal("0"), "credit": -amt},
        ]
        if amt < 0
        else [
            {"account_id": bank, "debit": amt, "credit": Decimal("0")},
            {"account_id": other, "debit": Decimal("0"), "credit": amt},
        ]
    )
    txn = create_journal_entry(
        db, day, name, lines, source_type="qb_ledger_import", reference=None
    )
    db.commit()
    return txn


def test_two_ledger_import_entries_are_never_a_pair(client, db_session, seed_accounts):
    """QuickBooks keeps its transactions apart: two transfers with no name,
    of one amount, a day apart, are two transfers, and so are two checks to
    one payee."""
    _ledger(db_session, seed_accounts, date(2026, 3, 2), "-500.00", "")
    _ledger(db_session, seed_accounts, date(2026, 3, 3), "-500.00", "")
    _ledger(db_session, seed_accounts, date(2026, 3, 9), "-75.00", "Office Depot")
    _ledger(db_session, seed_accounts, date(2026, 3, 9), "-75.00", "Office Depot")
    assert _report(client, seed_accounts)["groups"] == []


def test_a_ledger_import_entry_still_pairs_with_a_keyed_one(
    client, db_session, seed_accounts
):
    imported = _ledger(db_session, seed_accounts, date(2026, 3, 2), "-75.00", "Staples")
    keyed = _keyed(db_session, seed_accounts, date(2026, 3, 3), "-75.00", "STAPLES")
    (g,) = _report(client, seed_accounts)["groups"]
    assert g["key"] == f"{imported.id},{keyed.id}"
    sources = {e["transaction_id"]: e["source"] for e in g["entries"]}
    assert sources == {imported.id: "migrated", keyed.id: "keyed"}


def test_a_ledger_import_entry_still_pairs_with_a_feed_or_other_import(
    client, db_session, seed_accounts
):
    imported = _ledger(db_session, seed_accounts, date(2026, 3, 2), "-30.00", "")
    added = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-30.00", "")
    feed = BankAccount(name="Checking feed", account_id=seed_accounts["1000"].id)
    db_session.add(feed)
    db_session.flush()
    line = next(ln for ln in added.lines if ln.account_id == seed_accounts["1000"].id)
    db_session.add(
        BankTransaction(
            bank_account_id=feed.id,
            date=date(2026, 3, 2),
            amount=Decimal("-30.00"),
            payee="",
            transaction_id=added.id,
            transaction_line_id=line.id,
            match_status="added",
        )
    )
    db_session.commit()
    other = _ledger(db_session, seed_accounts, date(2026, 3, 20), "-90.00", "Acme")
    online = _migrated(db_session, seed_accounts, date(2026, 3, 21), "-90.00", "Acme")
    groups = _report(client, seed_accounts)["groups"]
    assert [g["key"] for g in groups] == [
        f"{imported.id},{added.id}",
        f"{other.id},{online.id}",
    ]
    assert {e["source"] for e in groups[0]["entries"]} == {"migrated", "imported"}


def test_an_imported_anchor_leaves_the_next_imported_entry_out(
    client, db_session, seed_accounts
):
    """A group is its first entry and the entries that match it. Two of the
    import's never match, so a typed entry between two imported ones pairs
    with the first; the second stands alone. A typed entry first matches
    both."""
    first = _ledger(db_session, seed_accounts, date(2026, 3, 2), "-40.00", "Shell")
    typed = _keyed(db_session, seed_accounts, date(2026, 3, 3), "-40.00", "Shell")
    _ledger(db_session, seed_accounts, date(2026, 3, 4), "-40.00", "Shell")
    (g,) = _report(client, seed_accounts)["groups"]
    assert g["key"] == f"{first.id},{typed.id}"

    keyed = _keyed(db_session, seed_accounts, date(2026, 4, 1), "-60.00", "Cafe Bleu")
    a = _ledger(db_session, seed_accounts, date(2026, 4, 2), "-60.00", "Cafe Bleu")
    b = _ledger(db_session, seed_accounts, date(2026, 4, 3), "-60.00", "Cafe Bleu")
    groups = _report(client, seed_accounts)["groups"]
    assert [g["key"] for g in groups][-1] == f"{keyed.id},{a.id},{b.id}"


# ── roles ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", [ROLE_READONLY, ROLE_ACCOUNTANT])
def test_read_only_roles_read_but_do_not_dismiss(
    client, db_session, seed_accounts, role
):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    b = _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "Pizza Barn")
    _user(db_session, "someone", role)
    _login_as(client, "someone")
    data = _report(client, seed_accounts)
    assert data["count"] == 1
    assert (
        client.get(
            f"{URL}/csv?account_id={seed_accounts['1000'].id}&{MARCH}"
        ).status_code
        == 200
    )
    r = client.post(
        f"{URL}/dismiss",
        json={"account_id": seed_accounts["1000"].id, "transaction_ids": [a.id, b.id]},
    )
    assert r.status_code == 403
    assert db_session.query(DuplicateDismissal).count() == 0


def test_a_bookkeeper_dismisses(client, db_session, seed_accounts):
    a = _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    b = _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "Pizza Barn")
    _user(db_session, "keeper", ROLE_BOOKKEEPER)
    _login_as(client, "keeper")
    r = client.post(
        f"{URL}/dismiss",
        json={"account_id": seed_accounts["1000"].id, "transaction_ids": [a.id, b.id]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["dismissed_by"] == "keeper"


# ── the dismissal key on PostgreSQL ──────────────────────────────────────


@pytest.fixture
def postgres_url():
    """A scratch PostgreSQL database for one test, created and dropped here.
    Same convention as test_atomic_account_balance.py: SLOWBOOKS_TEST_POSTGRES_URL
    is an admin URL the test can CREATE/DROP DATABASE against; skips when unset
    or unreachable."""
    import sqlalchemy as sa

    admin_url = os.environ.get("SLOWBOOKS_TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("SLOWBOOKS_TEST_POSTGRES_URL is not set")
    pytest.importorskip("psycopg2")
    name = "slowbooks_dup414_" + uuid.uuid4().hex[:12]
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on the machine
        admin.dispose()
        pytest.skip(f"cannot create a scratch PostgreSQL database: {exc}")
    url = sa.engine.make_url(admin_url).set(database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_dismissing_a_large_group_does_not_overflow_on_postgres(postgres_url):
    """A long group's id list is thousands of characters — more than the old
    String(2000) key held, which 500'd on PostgreSQL. The key is now a
    fixed-length hash, so dismissing it succeeds and reads back whole."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models.accounts import Account, AccountType
    from app.models.transactions import Transaction, TransactionLine
    from app.services import possible_duplicates as pd

    engine = create_engine(postgres_url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        bank = Account(
            account_number="1000",
            name="Checking",
            account_type=AccountType("asset"),
            bank_kind="checking",
            balance=Decimal("0"),
        )
        db.add(bank)
        db.flush()
        ids = []
        for i in range(600):  # id list well past 2000 characters
            txn = Transaction(date=date(2026, 3, 1), description=f"dup {i}")
            db.add(txn)
            db.flush()
            db.add(
                TransactionLine(
                    transaction_id=txn.id,
                    account_id=bank.id,
                    debit=Decimal("5.00"),
                    credit=Decimal("0"),
                )
            )
            ids.append(txn.id)
        db.commit()

        assert len(",".join(str(i) for i in ids)) > 2000
        row = pd.dismiss(db, bank, ids, actor="tester")
        db.commit()

        assert len(row.group_hash) == 64
        assert {int(p) for p in row.transaction_ids.split(",")} == set(ids)
    finally:
        db.close()
        engine.dispose()


# ── Save CSV / Save PDF ──────────────────────────────────────────────────


def test_csv_is_guarded_and_marked(client, db_session, seed_accounts):
    _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "=HYPERLINK(1)")
    _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "=HYPERLINK(1)")
    r = client.get(f"{URL}/csv?account_id={seed_accounts['1000'].id}&{MARCH}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert (
        "possible-duplicates_1000_2026-03-01_2026-04-30.csv"
        in r.headers["content-disposition"]
    )
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    assert "Possible Duplicates — 1000 Checking" in text
    assert "'=HYPERLINK(1)" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("1,")]
    assert len(rows) == 2 and all(",Payment,25.00," in ln for ln in rows)


def test_pdf(client, db_session, seed_accounts):
    pytest.importorskip("weasyprint")
    _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "Pizza Barn")
    r = client.get(f"{URL}/pdf?account_id={seed_accounts['1000'].id}&{MARCH}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_a_saved_report_can_carry_it(client, seed_accounts):
    r = client.post(
        "/api/saved-reports",
        json={
            "name": "Checking duplicates",
            "report_type": "possible_duplicates",
            "parameters": {"account_id": seed_accounts["1000"].id},
        },
    )
    assert r.status_code in (200, 201), r.text


def test_the_pdf_section_lists_each_group(client, db_session, seed_accounts):
    """The section the PDF renders, checked where WeasyPrint is missing."""
    from app.services.possible_duplicates import (
        possible_duplicates,
        report_section,
    )

    _keyed(db_session, seed_accounts, date(2026, 3, 2), "-25.00", "Pizza Barn")
    _keyed(db_session, seed_accounts, date(2026, 3, 3), "-25.00", "Pizza Barn")
    data = possible_duplicates(
        db_session, seed_accounts["1000"], date(2026, 3, 1), date(2026, 4, 30)
    )
    section = report_section(data)
    assert section["title"] == "Possible Duplicates — 1000 Checking"
    assert section["columns"][:3] == ["Date", "Number", "Payee"]
    head, *entries = section["rows"]
    assert head["style"] == "subtotal"
    assert head["cells"][0] == "Group 1 · Payment $25.00 · 2 entries"
    assert [r["cells"][2] for r in entries] == ["Pizza Barn", "Pizza Barn"]
