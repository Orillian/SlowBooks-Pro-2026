"""A partial entry is replaced, never posted twice (#663).

A transaction that does not balance in a Transaction Detail by Account file
can be accepted: it posts with its difference on Import Differences, a
partial entry. The import key holds the journal's one name, and the split by
name decides what is one journal, so in a cut or filtered file the lines
left out can carry the only other name or keep same-day entries from
splitting: the complete transaction in the corrected file had another key
and posted a second time. The reviewer saw it three ways, each doubling the
bank account: a general journal entry whose named A/R line was past the cut,
two same-day transfers merged into one accepted entry, and a same-payee
entry. And a voided partial entry kept its key, so the corrected file never
brought that transaction in (0 posted, 8 skipped).

Now an entry posted with a difference is marked partial
(transactions.source_difference). One rule tells that a journal of a file
and an entry in the books are one QuickBooks transaction (holds): the same
Type, Num and Date, and every line of the one, its account and amount,
among the other's. A later file's journals that hold a partial entry and
more are listed as replacing it, the partial entry's date, amount and
difference beside them: ticked, the partial entry is voided (its usual
reversal) and the file's post; unticked, both stay as they are and the
file's are left out. A copy still off posts as a partial entry again, and
the whole transaction replaces that in turn. A partial entry's key never
counts as imported, voided by hand or by a replacement; a cut copy that an
entry in the books holds is a duplicate. Possible Duplicates pairs two
imported entries again when either is partial.

The second review (#663) found what Type, Num and Date alone did: a
replaced partial entry's key, kept as imported, hid the whole transaction
from the next file (partial, then partial, then whole: the whole file
skipped; two same-day deposits merged by a cut: one lost); a 5.00 interest
deposit of the same day was offered as replacing a 100.00 walk-in's
partial entry, and held back while that was unticked; and the old cut file
uploaded after the corrected one posted its transaction a second time.
"""

from datetime import date
from decimal import Decimal

from app.models.accounts import Account
from app.models.banking import Reconciliation
from app.models.transactions import Transaction, TransactionLine
from app.services.qb_ledger_import import SOURCE_TYPE
from tests.qb_ledger_files import build, grand, heading, post, row, send, total
from tests.test_qb_desktop_ledger import FIXTURE, _chart
from tests.test_qb_ledger_problems import CHECK_1002, UNBALANCED


def _imported(db):
    return (
        db.query(Transaction)
        .filter(Transaction.source_type == SOURCE_TYPE)
        .order_by(Transaction.id)
        .all()
    )


def _voids(db):
    return db.query(Transaction).filter(Transaction.source_type == "manual_void").all()


def _balance(db, name) -> Decimal:
    acct = db.query(Account).filter(Account.name == name).one()
    return sum(
        (
            ln.debit - ln.credit
            for ln in db.query(TransactionLine).filter(
                TransactionLine.account_id == acct.id
            )
        ),
        Decimal("0"),
    )


def _kinds(data):
    return [p["kind"] for p in data.get("problems", [])]


def _of(data, kind):
    return [p for p in data.get("problems", []) if p["kind"] == kind]


# ── the reviewer's case 1: a journal whose named line was past the cut ───

GJ_BANK = row(
    "General Journal",
    "01/31/2024",
    "31",
    "",
    "write-off",
    "Accounts Receivable",
    "",
    "100.00",
)
GJ_AR = row(
    "General Journal", "01/31/2024", "31", "Smith", "write-off", "Checking", "100.00"
)
CHK = row("Check", "01/05/2024", "101", "Acme", "", "Rent or Lease", "", "60.00")
CHK_RENT = row("Check", "01/05/2024", "101", "Acme", "", "Checking", "60.00", "")
COMPLETE = build(
    [
        heading("Checking"),
        CHK,
        GJ_BANK,
        total("Checking", "0.00", "160.00"),
        heading("Accounts Receivable"),
        GJ_AR,
        total("Accounts Receivable", "100.00", "0.00"),
        heading("Rent or Lease"),
        CHK_RENT,
        total("Rent or Lease", "60.00", "0.00"),
        grand("160.00", "160.00"),
    ]
)
# Save as CSV stopped inside Accounts Receivable, before Smith's line
CUT = build(
    [
        heading("Checking"),
        CHK,
        GJ_BANK,
        total("Checking", "0.00", "160.00"),
        heading("Accounts Receivable"),
    ]
)


def _accept_the_cut_file(client):
    first = send(client, CUT)
    keys = [p["key"] for p in _of(first, "unbalanced")]
    assert keys == ["Check 101 · 2024-01-05 · Acme", "General Journal 31 · 2024-01-31"]
    data = send(client, CUT, {"cut_off": True, "unbalanced": keys})
    assert data["journals"] == 2 and data["accepted"]["unbalanced"] == 2


def test_an_accepted_entry_is_marked_partial(client, db_session, seed_accounts):
    _accept_the_cut_file(client)
    got = {t.source_ref: t.source_difference for t in _imported(db_session)}
    assert got == {
        "Check 101 · 2024-01-05 · Acme": Decimal("-60.00"),
        "General Journal 31 · 2024-01-31": Decimal("-100.00"),
    }
    # an entry that balances is not partial
    send(
        client,
        build(
            [
                heading("Checking"),
                row(
                    "Check",
                    "02/05/2024",
                    "102",
                    "Acme",
                    "",
                    "Rent or Lease",
                    "",
                    "5.00",
                ),
                total("Checking", "0.00", "5.00"),
                heading("Rent or Lease"),
                row("Check", "02/05/2024", "102", "Acme", "", "Checking", "5.00", ""),
                total("Rent or Lease", "5.00", "0.00"),
                grand("5.00", "5.00"),
            ]
        ),
    )
    whole = next(t for t in _imported(db_session) if t.reference == "102")
    assert whole.source_difference is None


def test_the_corrected_file_lists_what_it_replaces(client, db_session, seed_accounts):
    _accept_the_cut_file(client)
    data = send(client, COMPLETE)
    assert data["journals"] == 0  # nothing posts before the answers
    assert _kinds(data) == ["replaces", "replaces"]
    check, journal = _of(data, "replaces")
    (partial,) = journal["partial"]
    assert {k: journal[k] for k in ("key", "date", "type", "num", "name")} == {
        "key": "General Journal 31 · 2024-01-31",
        "date": "2024-01-31",
        "type": "General Journal",
        "num": "31",
        "name": "Smith",
    }
    assert (journal["amount"], journal["off"], journal["journals"]) == (
        "100.00",
        "0.00",
        1,
    )
    # the partial entry beside it: its date, amount and difference
    assert {k: partial[k] for k in ("date", "amount", "difference", "num")} == {
        "date": "2024-01-31",
        "amount": "100.00",
        "difference": "-100.00",
        "num": "31",
    }
    assert journal["voidable"] is True
    assert journal["text"] == (
        "General Journal 31 of 01/31/2024 is in the books as a partial entry of "
        "100.00, 100.00 of it on 3950 Import Differences; this file has the "
        "whole transaction, 100.00."
    )
    assert check["key"] == "Check 101 · 2024-01-05"
    assert "replaces" in data["problem_notes"]


def test_unticked_both_stay_and_the_file_s_copy_is_left_out(
    client, db_session, seed_accounts
):
    """The default: nothing is ticked. The books stay as they are (the two
    partial entries), and the corrected file's copies are left out."""
    _accept_the_cut_file(client)
    data = send(client, COMPLETE, {})
    assert data["journals"] == 0
    assert data["left_out"] == {
        "unbalanced": 0,
        "missing_account": 0,
        "replaces": 2,
        "closed_period": 0,
    }
    assert data["accepted"]["replaced"] == 0
    assert len(_imported(db_session)) == 2 and _voids(db_session) == []
    assert _balance(db_session, "Checking") == Decimal("-160.00")
    # and the next upload asks again
    assert _kinds(send(client, COMPLETE)) == ["replaces", "replaces"]


def test_ticked_the_partial_entry_is_voided_and_the_whole_one_posts(
    client, db_session, seed_accounts
):
    """The reviewer's file doubled Checking: -260.00 where the books say
    -160.00. Ticked, the partial entries are voided and the whole
    transactions post: Checking -160.00, nothing on Import Differences."""
    _accept_the_cut_file(client)
    answers = {
        "replaces": ["Check 101 · 2024-01-05", "General Journal 31 · 2024-01-31"]
    }
    data = send(client, COMPLETE, answers)
    assert data["errors"] == []
    assert data["journals"] == 2
    assert data["accepted"]["replaced"] == 2
    assert data["left_out"] == {
        "unbalanced": 0,
        "missing_account": 0,
        "replaces": 0,
        "closed_period": 0,
    }
    assert _balance(db_session, "Checking") == Decimal("-160.00")
    assert _balance(db_session, "Import Differences") == 0
    assert _balance(db_session, "Accounts Receivable") == Decimal("100.00")
    assert _balance(db_session, "Rent or Lease") == Decimal("60.00")
    # each partial entry's own reversal, as Void makes it
    voids = _voids(db_session)
    partial = {t.id for t in _imported(db_session) if t.source_difference is not None}
    assert {v.source_id for v in voids} == partial
    assert all(v.description.startswith("VOID: ") for v in voids)
    # the corrected file again, and the cut one: nothing more
    again = send(client, COMPLETE)
    assert again["journals"] == 0 and "problems" not in again
    old = send(client, CUT)
    assert old["journals"] == 0
    assert _kinds(old) == []
    assert _balance(db_session, "Checking") == Decimal("-160.00")


def test_the_same_cut_file_again_posts_nothing_and_asks_nothing(
    client, db_session, seed_accounts
):
    _accept_the_cut_file(client)
    again = send(client, CUT)
    assert again["journals"] == 0 and again["duplicates_skipped"] == 2
    assert _kinds(again) == []
    assert _balance(db_session, "Checking") == Decimal("-160.00")


# ── the reviewer's case 2: two same-day transfers merged by the filter ──


def test_two_transfers_merged_into_one_partial_entry_are_replaced_by_both(
    client, db_session, seed_accounts
):
    t1a = row("Transfer", "02/02/2024", "", "A", "", "Savings", "", "50.00")
    t2a = row("Transfer", "02/02/2024", "", "B", "", "Savings", "", "70.00")
    t1b = row("Transfer", "02/02/2024", "", "A", "", "Checking", "50.00", "")
    t2b = row("Transfer", "02/02/2024", "", "B", "", "Checking", "70.00", "")
    complete = build(
        [
            heading("Checking"),
            t1a,
            t2a,
            total("Checking", "0.00", "120.00"),
            heading("Savings"),
            t1b,
            t2b,
            total("Savings", "120.00", "0.00"),
            grand("120.00", "120.00"),
        ]
    )
    filtered = build(  # the report filtered to Checking
        [
            heading("Checking"),
            t1a,
            t2a,
            total("Checking", "0.00", "120.00"),
            grand("0.00", "120.00"),
        ]
    )
    first = send(client, filtered)
    (merged,) = _of(first, "unbalanced")
    assert (merged["key"], merged["off"]) == ("Transfer · 2024-02-02", "-120.00")
    send(client, filtered, {"unbalanced": [merged["key"]]})
    data = send(client, complete)
    (group,) = _of(data, "replaces")
    assert group["journals"] == 2 and group["names"] == ["A", "B"]
    assert group["amount"] == "120.00"
    assert [p["difference"] for p in group["partial"]] == ["-120.00"]
    data = send(client, complete, {"replaces": [group["key"]]})
    assert data["journals"] == 2 and data["accepted"]["replaced"] == 1
    assert _balance(db_session, "Checking") == Decimal("-120.00")
    assert _balance(db_session, "Savings") == Decimal("120.00")
    assert _balance(db_session, "Import Differences") == 0


# ── the reviewer's case 3: a same-payee entry ────────────────────────────


def test_a_same_payee_entry_is_replaced(client, db_session, seed_accounts):
    bank = row(
        "General Journal", "01/31/2024", "32", "Acme", "", "-SPLIT-", "", "100.00"
    )
    exp = row("General Journal", "01/31/2024", "32", "", "", "Checking", "60.00", "")
    ar = row(
        "General Journal", "01/31/2024", "32", "Smith", "", "Checking", "40.00", ""
    )
    complete = build(
        [
            heading("Checking"),
            bank,
            total("Checking", "0.00", "100.00"),
            heading("Accounts Receivable"),
            ar,
            total("Accounts Receivable", "40.00", "0.00"),
            heading("Rent or Lease"),
            exp,
            total("Rent or Lease", "60.00", "0.00"),
            grand("100.00", "100.00"),
        ]
    )
    filtered = build(  # filtered to Checking and Rent or Lease
        [
            heading("Checking"),
            bank,
            total("Checking", "0.00", "100.00"),
            heading("Rent or Lease"),
            exp,
            total("Rent or Lease", "60.00", "0.00"),
            grand("60.00", "100.00"),
        ]
    )
    first = send(client, filtered)
    keys = [p["key"] for p in _of(first, "unbalanced")]
    assert keys == ["General Journal 32 · 2024-01-31 · Acme"]
    send(client, filtered, {"unbalanced": keys})
    data = send(client, complete)
    (group,) = _of(data, "replaces")
    assert group["key"] == "General Journal 32 · 2024-01-31"
    send(client, complete, {"replaces": [group["key"]]})
    assert _balance(db_session, "Checking") == Decimal("-100.00")
    assert _balance(db_session, "Import Differences") == 0


# ── a voided partial entry, and one that cannot be voided ───────────────


def test_a_partial_entry_voided_by_hand_lets_the_corrected_file_in(
    client, db_session, seed_accounts
):
    """The reviewer's file: 0 posted, 8 skipped. A partial entry voided by
    hand no longer counts as here."""
    _chart(db_session)
    send(client, UNBALANCED, {"unbalanced": [CHECK_1002]})
    plugged = next(t for t in _imported(db_session) if t.source_ref == CHECK_1002)
    assert plugged.source_difference == Decimal("10.00")
    r = client.post(f"/api/journal/{plugged.id}/void")
    assert r.status_code == 200, r.text
    data = send(client, FIXTURE.read_bytes())
    assert data["journals"] == 1 and data["duplicates_skipped"] == 7
    assert "problems" not in data
    assert _balance(db_session, "Operating Checking 4321") == Decimal("145.00")
    assert _balance(db_session, "Import Differences") == 0


def test_a_partial_entry_in_a_reconciliation_is_not_replaced(
    client, db_session, seed_accounts
):
    _accept_the_cut_file(client)
    checking = db_session.query(Account).filter(Account.name == "Checking").one()
    rec = Reconciliation(
        account_id=checking.id,
        statement_date=date(2024, 1, 31),
        statement_balance=Decimal("-160.00"),
        status="completed",
    )
    db_session.add(rec)
    db_session.flush()
    partial = next(t for t in _imported(db_session) if t.reference == "31")
    for ln in partial.lines:
        if ln.account_id == checking.id:
            ln.reconciliation_id = rec.id
    db_session.commit()
    data = send(client, COMPLETE)
    journal = next(p for p in _of(data, "replaces") if p["num"] == "31")
    assert journal["voidable"] is False
    assert "completed reconciliation" in journal["text"]
    answers = {"replaces": [journal["key"]]}
    data = send(client, COMPLETE, answers)
    assert data["journals"] == 0
    # the reconciled one, and check 101's, which was not ticked
    assert data["left_out"]["replaces"] == 2
    assert any("completed reconciliation" in w for w in data["warnings"])
    assert _voids(db_session) == []


# ── Possible Duplicates: the backstop where it matters ──────────────────


def test_possible_duplicates_pairs_imported_entries_when_one_is_partial(
    client, db_session, seed_accounts
):
    """A complete transfer imported from one file, and the same transfer
    merged into a partial entry from another: Possible Duplicates pairs
    them, where two complete imported entries never pair."""
    from app.services.possible_duplicates import possible_duplicates

    t1a = row("Transfer", "02/02/2024", "", "A", "", "Savings", "", "50.00")
    t1b = row("Transfer", "02/02/2024", "", "A", "", "Checking", "50.00", "")
    t2a = row("Transfer", "02/02/2024", "", "B", "", "Savings", "", "70.00")
    first = build(
        [
            heading("Checking"),
            t1a,
            total("Checking", "0.00", "50.00"),
            heading("Savings"),
            t1b,
            total("Savings", "50.00", "0.00"),
            grand("50.00", "50.00"),
        ]
    )
    send(client, first)
    merged = build(
        [
            heading("Checking"),
            t1a,
            t2a,
            total("Checking", "0.00", "120.00"),
            grand("0.00", "120.00"),
        ]
    )
    (p,) = _of(send(client, merged), "unbalanced")
    send(client, merged, {"unbalanced": [p["key"]]})
    checking = db_session.query(Account).filter(Account.name == "Checking").one()
    found = possible_duplicates(
        db_session, checking, date(2024, 1, 1), date(2024, 12, 31)
    )
    (group,) = found["groups"]
    assert group["amount"] == 50.0
    assert {r["transaction_id"] for r in group["entries"]} == {
        t.id for t in _imported(db_session)
    }


# ── the second review: one rule, holds ───────────────────────────────────

ACME = row("Check", "01/05/2024", "101", "Acme", "", "-SPLIT-", "", "60.00")
ACME_RENT = row("Check", "01/05/2024", "101", "Acme", "", "Checking", "40.00", "")
ACME_UTIL = row("Check", "01/05/2024", "101", "Acme", "", "Checking", "20.00", "")
# filtered to Checking: the bank line only
ACME_BANK = build(
    [
        heading("Checking"),
        ACME,
        total("Checking", "0.00", "60.00"),
        grand("0.00", "60.00"),
    ]
)
# filtered to Checking and Rent or Lease: still 20.00 off
ACME_RENT_ONLY = build(
    [
        heading("Checking"),
        ACME,
        total("Checking", "0.00", "60.00"),
        heading("Rent or Lease"),
        ACME_RENT,
        total("Rent or Lease", "40.00", "0.00"),
        grand("40.00", "60.00"),
    ]
)
ACME_WHOLE = build(
    [
        heading("Checking"),
        ACME,
        total("Checking", "0.00", "60.00"),
        heading("Rent or Lease"),
        ACME_RENT,
        total("Rent or Lease", "40.00", "0.00"),
        heading("Utilities"),
        ACME_UTIL,
        total("Utilities", "20.00", "0.00"),
        grand("60.00", "60.00"),
    ]
)


def _accept(client, data, **more):
    keys = [p["key"] for p in _of(send(client, data), "unbalanced")]
    assert keys, "nothing to accept"
    return send(client, data, {"unbalanced": keys, **more})


def _replace(client, data, **more):
    keys = [p["key"] for p in _of(send(client, data), "replaces")]
    assert keys, "nothing replaces"
    return send(client, data, {"replaces": keys, **more})


def test_partial_then_partial_then_the_whole_transaction(
    client, db_session, seed_accounts
):
    """The reviewer's blocker: a copy still off replaced the partial entry
    and posted as a partial entry itself; the replaced entry's key stayed
    "already imported", so the whole file was skipped as a duplicate and
    3950 kept 20.00 for good."""
    _accept(client, ACME_BANK)
    copy = _replace(client, ACME_RENT_ONLY)
    assert copy["journals"] == 1 and copy["accepted"] == {
        "unbalanced": 1,
        "accounts": [],
        "cut_off": False,
        "replaced": 1,
    }
    assert [t.source_difference for t in _imported(db_session)] == [
        Decimal("-60.00"),
        Decimal("-20.00"),
    ]
    data = send(client, ACME_WHOLE)
    (group,) = _of(data, "replaces")
    assert [p["difference"] for p in group["partial"]] == ["-20.00"]
    assert (group["amount"], group["off"]) == ("60.00", "0.00")
    data = send(client, ACME_WHOLE, {"replaces": [group["key"]]})
    assert data["journals"] == 1 and data["accepted"]["replaced"] == 1
    assert _balance(db_session, "Checking") == Decimal("-60.00")
    assert _balance(db_session, "Rent or Lease") == Decimal("40.00")
    assert _balance(db_session, "Utilities") == Decimal("20.00")
    assert _balance(db_session, "Import Differences") == 0
    # each of the three files again: nothing more, nothing asked
    for again in (ACME_WHOLE, ACME_RENT_ONLY, ACME_BANK):
        data = send(client, again)
        assert data["journals"] == 0 and _kinds(data) == [], data.get("problems")
        assert data["duplicates_skipped"] == 1
    assert _balance(db_session, "Checking") == Decimal("-60.00")


def test_the_copy_voided_by_hand_lets_the_whole_transaction_in(
    client, db_session, seed_accounts
):
    """Voiding by hand did not free it either: the first partial entry's key
    stayed taken through its replacement, so the whole file posted nothing."""
    _accept(client, ACME_BANK)
    _replace(client, ACME_RENT_ONLY)
    copy = next(t for t in _imported(db_session) if t.source_difference == -20)
    assert client.post(f"/api/journal/{copy.id}/void").status_code == 200
    data = send(client, ACME_WHOLE)
    assert data["journals"] == 1 and "problems" not in data
    assert _balance(db_session, "Checking") == Decimal("-60.00")
    assert _balance(db_session, "Utilities") == Decimal("20.00")
    assert _balance(db_session, "Import Differences") == 0


WALK_IN_BANK = row(
    "Deposit", "03/01/2024", "", "Walk-in", "", "Service Income", "100.00"
)
WALK_IN_INCOME = row(
    "Deposit", "03/01/2024", "", "Walk-in", "", "Checking", "", "100.00"
)
BANK_BANK = row("Deposit", "03/01/2024", "", "Bank", "", "Other Income", "5.00")
BANK_INCOME = row("Deposit", "03/01/2024", "", "Bank", "", "Checking", "", "5.00")


def test_two_deposits_merged_by_a_cut_are_replaced_by_both(
    client, db_session, seed_accounts
):
    """The reviewer's second blocker: two same-day deposits with no Num. A
    file cut inside Checking had the walk-in's bank line; one cut inside
    Other Income had both bank lines and the walk-in's income line, merged
    into one entry 5.00 off, which replaced the first. The whole file has
    them as two journals: the walk-in's key was the replaced entry's, so it
    was skipped, and the bank's alone replaced the merged entry. Checking
    ended at 5.00 where the books say 105.00."""
    cut = build([heading("Checking"), WALK_IN_BANK])
    _accept(client, cut, cut_off=True)
    merged = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            BANK_BANK,
            total("Checking", "105.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            heading("Other Income"),
        ]
    )
    _replace(client, merged, cut_off=True)
    whole = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            BANK_BANK,
            total("Checking", "105.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            heading("Other Income"),
            BANK_INCOME,
            total("Other Income", "0.00", "5.00"),
            grand("105.00", "105.00"),
        ]
    )
    data = send(client, whole)
    (group,) = _of(data, "replaces")
    assert group["journals"] == 2 and group["names"] == ["Walk-in", "Bank"]
    assert [p["difference"] for p in group["partial"]] == ["5.00"]
    data = send(client, whole, {"replaces": [group["key"]]})
    assert data["journals"] == 2 and data["accepted"]["replaced"] == 1
    assert _balance(db_session, "Checking") == Decimal("105.00")
    assert _balance(db_session, "Service Income") == Decimal("-100.00")
    assert _balance(db_session, "Other Income") == Decimal("-5.00")
    assert _balance(db_session, "Import Differences") == 0


def _merged_after_the_walk_in(client):
    """The walk-in's deposit whole from a file of its own, then a cut file
    that merges it with the bank's bank line, accepted: the merged partial
    entry holds the walk-in's lines a second time."""
    walk_in = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            total("Checking", "100.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            grand("100.00", "100.00"),
        ]
    )
    assert send(client, walk_in)["journals"] == 1
    merged = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            BANK_BANK,
            total("Checking", "105.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            heading("Other Income"),
        ]
    )
    return walk_in, merged


WHOLE_DEPOSITS = build(
    [
        heading("Checking"),
        WALK_IN_BANK,
        BANK_BANK,
        total("Checking", "105.00", "0.00"),
        heading("Service Income"),
        WALK_IN_INCOME,
        total("Service Income", "0.00", "100.00"),
        heading("Other Income"),
        BANK_INCOME,
        total("Other Income", "0.00", "5.00"),
        grand("105.00", "105.00"),
    ]
)


def test_a_journal_here_only_by_a_voided_entrys_key_posts_in_the_replacement(
    client, db_session, seed_accounts
):
    """In a group being replaced, a journal that is here only by a key no
    entry that stands holds (its whole entry was voided by hand) posts with
    the rest: the partial entry it replaces held its lines."""
    walk_in, merged = _merged_after_the_walk_in(client)
    entry = _imported(db_session)[0]
    assert client.post(f"/api/journal/{entry.id}/void").status_code == 200
    # alone, the voided entry's file stays out: the void stands
    assert send(client, walk_in)["journals"] == 0
    _accept(client, merged, cut_off=True)
    data = send(client, WHOLE_DEPOSITS)
    (group,) = _of(data, "replaces")
    assert group["journals"] == 2
    data = send(client, WHOLE_DEPOSITS, {"replaces": [group["key"]]})
    assert data["journals"] == 2 and data["duplicates_skipped"] == 0
    assert _balance(db_session, "Checking") == Decimal("105.00")
    assert _balance(db_session, "Import Differences") == 0


def test_a_journal_a_standing_entry_has_counts_in_the_replacement_but_never_posts(
    client, db_session, seed_accounts
):
    """The walk-in's whole entry stands, and the merged partial entry holds
    its lines too (Checking 205.00). The whole file's two journals hold the
    partial entry: ticked, it is voided, the bank's deposit posts, and the
    walk-in's, in the books already, is a duplicate."""
    _, merged = _merged_after_the_walk_in(client)
    _accept(client, merged, cut_off=True)
    assert _balance(db_session, "Checking") == Decimal("205.00")
    data = send(client, WHOLE_DEPOSITS)
    (group,) = _of(data, "replaces")
    assert group["journals"] == 2
    data = send(client, WHOLE_DEPOSITS, {"replaces": [group["key"]]})
    assert data["journals"] == 1 and data["duplicates_skipped"] == 1
    assert _balance(db_session, "Checking") == Decimal("105.00")
    assert _balance(db_session, "Service Income") == Decimal("-100.00")
    assert _balance(db_session, "Other Income") == Decimal("-5.00")
    assert _balance(db_session, "Import Differences") == 0


# the walk-in's deposit with only its bank line, and the bank's interest:
# one Type, Num and Date, two transactions
WALK_IN_CHECKING_ONLY = build(
    [
        heading("Checking"),
        WALK_IN_BANK,
        total("Checking", "100.00", "0.00"),
        grand("100.00", "0.00"),
    ]
)
INTEREST = row(
    "Deposit", "03/01/2024", "", "First Bank", "interest", "Other Income", "5.00"
)
INTEREST_INCOME = row(
    "Deposit", "03/01/2024", "", "First Bank", "interest", "Savings", "", "5.00"
)


def test_another_transaction_of_the_same_type_num_and_date_imports_on_its_own(
    client, db_session, seed_accounts
):
    """The reviewer's file: the 5.00 interest deposit on Savings was listed
    as replacing the walk-in's 100.00 partial entry (one Type, Num and Date),
    ticked it voided that entry, and unticked it was held back. It holds
    none of the walk-in's lines, so it imports at once."""
    _accept(client, WALK_IN_CHECKING_ONLY)
    savings = build(
        [
            heading("Savings"),
            INTEREST,
            total("Savings", "5.00", "0.00"),
            heading("Other Income"),
            INTEREST_INCOME,
            total("Other Income", "0.00", "5.00"),
            grand("5.00", "5.00"),
        ]
    )
    data = send(client, savings)
    assert "problems" not in data and data["journals"] == 1
    assert _balance(db_session, "Savings") == Decimal("5.00")
    assert _voids(db_session) == []
    # the walk-in's partial entry stands
    assert _balance(db_session, "Checking") == Decimal("100.00")
    assert _balance(db_session, "Import Differences") == Decimal("-100.00")


def test_the_complete_file_holds_back_only_what_replaces(
    client, db_session, seed_accounts
):
    """The complete file has both deposits whole: the walk-in's replaces its
    partial entry, and the interest, a transaction of its own, imports
    whatever the answer (it was held back with the replacement)."""
    _accept(client, WALK_IN_CHECKING_ONLY)
    complete = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            total("Checking", "100.00", "0.00"),
            heading("Savings"),
            INTEREST,
            total("Savings", "5.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            heading("Other Income"),
            INTEREST_INCOME,
            total("Other Income", "0.00", "5.00"),
            grand("105.00", "105.00"),
        ]
    )
    data = send(client, complete)
    assert data["ready"] == 1
    (group,) = _of(data, "replaces")
    assert (group["journals"], group["names"]) == (1, ["Walk-in"])
    assert group["amount"] == "100.00"
    data = send(client, complete, {})
    assert data["journals"] == 1 and data["left_out"]["replaces"] == 1
    assert _balance(db_session, "Savings") == Decimal("5.00")
    assert _balance(db_session, "Checking") == Decimal("100.00")
    data = send(client, complete, {"replaces": [group["key"]]})
    assert data["journals"] == 1 and data["duplicates_skipped"] == 1
    assert _balance(db_session, "Checking") == Decimal("100.00")
    assert _balance(db_session, "Service Income") == Decimal("-100.00")
    assert _balance(db_session, "Import Differences") == 0


def test_a_replacement_voids_only_the_partial_entries_it_holds(
    client, db_session, seed_accounts
):
    """Two partial entries of one Type, Num and Date: the walk-in's bank line
    and the interest's Savings line, each from a file of its own. A file
    with the walk-in's deposit whole replaces the walk-in's alone."""
    _accept(client, WALK_IN_CHECKING_ONLY)
    _accept(
        client,
        build(
            [
                heading("Savings"),
                INTEREST,
                total("Savings", "5.00", "0.00"),
                grand("5.00", "0.00"),
            ]
        ),
    )
    walk_in, interest = _imported(db_session)
    walk_in_whole = build(
        [
            heading("Checking"),
            WALK_IN_BANK,
            total("Checking", "100.00", "0.00"),
            heading("Service Income"),
            WALK_IN_INCOME,
            total("Service Income", "0.00", "100.00"),
            grand("100.00", "100.00"),
        ]
    )
    data = send(client, walk_in_whole)
    (group,) = _of(data, "replaces")
    assert [p["id"] for p in group["partial"]] == [walk_in.id]
    send(client, walk_in_whole, {"replaces": [group["key"]]})
    assert {v.source_id for v in _voids(db_session)} == {walk_in.id}
    assert _balance(db_session, "Checking") == Decimal("100.00")
    assert _balance(db_session, "Savings") == Decimal("5.00")
    assert _balance(db_session, "Import Differences") == Decimal("-5.00")


def test_a_replacement_whose_copy_cannot_post_keeps_the_partial_entry(
    client, db_session, seed_accounts
):
    """The void and the post are one: a copy the books refuse (a line with
    both a debit and a credit) leaves the partial entry as it was."""
    _accept(client, ACME_BANK)
    bad = build(
        [
            heading("Checking"),
            ACME,
            total("Checking", "0.00", "60.00"),
            heading("Rent or Lease"),
            row("Check", "01/05/2024", "101", "Acme", "", "Checking", "40.00", "3.00"),
            total("Rent or Lease", "40.00", "3.00"),
            heading("Utilities"),
            ACME_UTIL,
            total("Utilities", "20.00", "0.00"),
            grand("60.00", "63.00"),
        ]
    )
    (group,) = _of(send(client, bad), "replaces")
    r = post(client, bad, {"replaces": [group["key"]]})
    assert r.status_code == 400, r.text
    db_session.expire_all()
    assert _voids(db_session) == []
    (entry,) = _imported(db_session)
    assert entry.source_difference == Decimal("-60.00")
    assert _balance(db_session, "Checking") == Decimal("-60.00")


# ── a cut copy of an entry in the books ──────────────────────────────────


def test_the_old_cut_file_after_a_hand_void_and_the_corrected_file(
    client, db_session, seed_accounts
):
    """The reviewer's file: the partial entry voided by hand, the corrected
    file imported, then the old cut file again. It was listed as cut off
    and unbalanced, and ticked, Checking went to -200.00. The corrected
    file's entry holds what the cut file has of it: a duplicate."""
    gj = build(
        [
            heading("Checking"),
            GJ_BANK,
            total("Checking", "0.00", "100.00"),
            heading("Accounts Receivable"),
        ]
    )
    _accept(client, gj, cut_off=True)
    (partial,) = _imported(db_session)
    assert client.post(f"/api/journal/{partial.id}/void").status_code == 200
    whole = build(
        [
            heading("Checking"),
            GJ_BANK,
            total("Checking", "0.00", "100.00"),
            heading("Accounts Receivable"),
            GJ_AR,
            total("Accounts Receivable", "100.00", "0.00"),
            grand("100.00", "100.00"),
        ]
    )
    assert send(client, whole)["journals"] == 1
    again = send(client, gj)
    assert again["journals"] == 0 and again["duplicates_skipped"] == 1
    assert "problems" not in again
    # the notice for the cut file is a warning, with nothing to answer
    assert any("ends inside Accounts Receivable" in w for w in again["warnings"])
    assert _balance(db_session, "Checking") == Decimal("-100.00")


def test_the_cut_file_after_the_complete_one_is_a_duplicate(
    client, db_session, seed_accounts
):
    data = send(client, COMPLETE)
    assert data["journals"] == 2
    again = send(client, CUT)
    assert again["journals"] == 0 and again["duplicates_skipped"] == 2
    assert "problems" not in again
    # the answers of a list made before change nothing either
    again = send(
        client,
        CUT,
        {
            "cut_off": True,
            "unbalanced": [
                "Check 101 · 2024-01-05 · Acme",
                "General Journal 31 · 2024-01-31",
            ],
        },
    )
    assert again["journals"] == 0 and again["duplicates_skipped"] == 2
    assert _balance(db_session, "Checking") == Decimal("-160.00")
    # nothing was ever off
    assert not db_session.query(Account).filter_by(name="Import Differences").count()
