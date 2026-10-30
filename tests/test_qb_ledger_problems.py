"""A Transaction Detail by Account file with problems is never refused
whole (#663).

The import checked the whole file and refused it whole when anything
failed: an account missing from the chart, a transaction that does not
balance, a section whose lines do not add up to QuickBooks' printed total.
One bad transaction, or 6,000 from a bad export, and nothing came in. Now a
file with problems posts nothing on its first upload and answers with the
count that is ready and the problems; the page lists them, and the same
file sent again with the person's answers imports what checks out with
what was accepted:

- a transaction that does not balance, accepted, posts with its difference
  on 3950 Import Differences (equity, made on first use); left out, it is
  left out, and a later upload can still bring it in;
- an account missing from the chart, accepted, is made with the type
  chosen; left out, every transaction that touches it is left out too;
- a file that looks cut short or edited, accepted, imports what is there;
  not accepted, nothing imports.

A second call posts nothing twice, and the adjusting-entries mode keeps its
refusal of a file that holds anything but journal entries. The fixtures are
variants of tests/fixtures/qb_desktop's invented books.
"""

import io
import json

import pytest

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine
from app.services.qb_ledger_import import SOURCE_TYPE
from tests.test_qb_desktop_ledger import ACCOUNTS, FIXTURE, _chart
from tests.test_qb_desktop_round_trip import GJ_ONLY
from tests.test_qb_ledger_other_sections import _report as _other_sections_report

URL = "/api/csv/import/qb-report"
CHECK_1002 = "Check 1002 · 2024-01-31 · Harbor Properties"


def _send(client, data: bytes, answers=None, adjusting=False):
    form = {}
    if answers is not None:
        form["answers"] = json.dumps(answers)
    if adjusting:
        form["adjusting_only"] = "true"
    return client.post(
        URL,
        files={
            "file": ("Transaction Detail by Account.csv", io.BytesIO(data), "text/csv")
        },
        data=form,
    )


def _upload(client, data: bytes, answers=None, adjusting=False):
    r = _send(client, data, answers, adjusting)
    assert r.status_code == 200, r.text
    return r.json()


def _variant(*swaps) -> bytes:
    text = FIXTURE.read_bytes().decode("cp1252")
    for old, new in swaps:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text.encode("cp1252")


# Check 1002's rent line 10.00 over its bank line, with the Rent section's
# own total, so the section adds up and the check does not balance
UNBALANCED = _variant(
    (
        'Operating Checking 4321,,"1,000.00",,,,',
        'Operating Checking 4321,,"1,010.00",,,,',
    ),
    (
        ',Total Rent,,,,,,,,,,,,,,,,,,,,"1,000.00"',
        ',Total Rent,,,,,,,,,,,,,,,,,,,,"1,010.00"',
    ),
)
# the Rent section's printed total off from its lines: edited, or cut
EDITED = _variant(
    (
        ',Total Rent,,,,,,,,,,,,,,,,,,,,"1,000.00"',
        ',Total Rent,,,,,,,,,,,,,,,,,,,,"1,100.00"',
    )
)


def _cut_after(marker: str) -> bytes:
    """The fixture stopped after the row holding `marker`, as Save as CSV
    stops a report at 32,768 rows: no Total line, no TOTAL."""
    text = FIXTURE.read_bytes().decode("cp1252")
    rows = text.split("\r\n")
    at = next(i for i, row in enumerate(rows) if marker in row)
    return ("\r\n".join(rows[: at + 1]) + "\r\n").encode("cp1252")


def _chart_without(db, *names):
    """The fixture's chart less `names` (the leaf names)."""
    have = {a.name.lower(): a for a in db.query(Account).all()}
    for name, kind, parent in ACCOUNTS:
        if name in names or parent in names:
            continue
        if parent is None and name.lower() in have:
            continue
        acct = Account(name=name, account_type=kind)
        if parent:
            acct.parent_id = have[parent.lower()].id
        db.add(acct)
        db.flush()
        if parent is None:
            have[name.lower()] = acct
    db.commit()


def _imported(db):
    return (
        db.query(Transaction)
        .filter(Transaction.source_type == SOURCE_TYPE)
        .order_by(Transaction.id)
        .all()
    )


def _account(db, name):
    return db.query(Account).filter(Account.name == name).one_or_none()


def _kinds(data):
    return [p["kind"] for p in data["problems"]]


# ── a transaction that does not balance ──────────────────────────────────


def test_a_transaction_that_does_not_balance_waits_for_an_answer(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, UNBALANCED)
    assert data["journals"] == 0 and data["errors"] == []
    assert data["ready"] == 7 and data["duplicates_skipped"] == 0
    (p,) = data["problems"]
    assert p == {
        "kind": "unbalanced",
        "key": CHECK_1002,
        "date": "2024-01-31",
        "type": "Check",
        "num": "1002",
        "name": "Harbor Properties",
        "amount": "1010.00",
        "off": "10.00",
        "rows": [8, 38],
        "text": "Check 1002 of 01/31/2024 does not balance (10.00 over 2 lines, "
        "rows 8, 38)",
    }
    note = data["problem_notes"]["unbalanced"]
    assert "3950 Import Differences" in note and "equity" in note
    assert "account_types" not in data
    assert _imported(db_session) == []
    assert _account(db_session, "Import Differences") is None


def test_an_accepted_transaction_posts_its_difference_once(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, UNBALANCED, {"unbalanced": [CHECK_1002]})
    assert data["errors"] == [] and data["journals"] == 8
    assert data["accepted"] == {"unbalanced": 1, "accounts": [], "cut_off": False}
    assert data["left_out"] == {"unbalanced": 0, "missing_account": 0}
    assert data["differences_account"] == "3950 Import Differences"
    differences = _account(db_session, "Import Differences")
    assert differences.account_number == "3950"
    assert differences.account_type == AccountType.EQUITY
    check = next(t for t in _imported(db_session) if t.reference == "1002")
    assert check.source_ref == CHECK_1002
    assert sorted(
        (ln.account.name, ln.debit, ln.credit, ln.description) for ln in check.lines
    ) == [
        ("Import Differences", 0, 10, "Difference in the QuickBooks file"),
        ("Operating Checking 4321", 0, 1000, "January rent"),
        ("Rent", 1010, 0, "January rent"),
    ]
    # the same answers again, and the file alone: nothing twice
    again = _upload(client, UNBALANCED, {"unbalanced": [CHECK_1002]})
    assert again["journals"] == 0 and again["duplicates_skipped"] == 8
    assert "problems" not in again
    alone = _upload(client, UNBALANCED)
    assert alone["journals"] == 0 and alone["duplicates_skipped"] == 8
    assert "problems" not in alone
    assert len(_imported(db_session)) == 8
    assert (
        db_session.query(Account).filter(Account.name == "Import Differences").count()
        == 1
    )


def test_a_transaction_left_out_stays_out_until_a_later_upload(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, UNBALANCED, {})
    assert data["journals"] == 7
    assert data["accepted"] == {"unbalanced": 0, "accounts": [], "cut_off": False}
    assert data["left_out"] == {"unbalanced": 1, "missing_account": 0}
    assert "differences_account" not in data
    assert _account(db_session, "Import Differences") is None
    # the same file again asks about it alone
    again = _upload(client, UNBALANCED)
    assert again["ready"] == 0 and again["duplicates_skipped"] == 7
    assert [p["key"] for p in again["problems"]] == [CHECK_1002]
    # and a corrected file brings it in
    fixed = _upload(client, FIXTURE.read_bytes())
    assert fixed["journals"] == 1 and fixed["duplicates_skipped"] == 7
    assert len(_imported(db_session)) == 8


def test_a_numbered_differences_account_is_kept_when_renamed(
    client, db_session, seed_accounts
):
    _chart(db_session)
    db_session.add(
        Account(account_number="3950", name="Suspense", account_type=AccountType.EQUITY)
    )
    db_session.commit()
    first = _upload(client, UNBALANCED)
    assert "3950 Suspense" in first["problem_notes"]["unbalanced"]
    data = _upload(client, UNBALANCED, {"unbalanced": [CHECK_1002]})
    assert data["differences_account"] == "3950 Suspense"
    assert _account(db_session, "Import Differences") is None


# ── an account missing from the chart ───────────────────────────────────


def test_a_missing_account_is_one_row_with_its_transactions(
    client, db_session, seed_accounts
):
    _chart_without(db_session, "Card Fees")
    data = _upload(client, FIXTURE.read_bytes())
    assert data["journals"] == 0 and data["ready"] == 6
    assert data["problems"] == [
        {
            "kind": "missing_account",
            "key": "Card Fees",
            "path": "Card Fees",
            "transactions": 2,
            "text": "Card Fees is not in the chart (2 transactions)",
        }
    ]
    assert "Lists to IIF Files" in data["problem_notes"]["missing_account"]
    offered = {t["value"]: t for t in data["account_types"]}
    assert offered["expense"]["account_type"] == "expense"
    assert offered["bank"] == {
        "value": "bank",
        "label": "Bank",
        "account_type": "asset",
    }
    assert "accounts_receivable" not in offered and "accounts_payable" not in offered


def test_an_accepted_account_is_made_with_the_type_chosen(
    client, db_session, seed_accounts
):
    _chart_without(db_session, "Card Fees", "Operating Checking 4321")
    answers = {
        "accounts": {"Card Fees": "other_expense", "Operating Checking 4321": "bank"}
    }
    data = _upload(client, FIXTURE.read_bytes(), answers)
    assert data["journals"] == 8
    assert data["accepted"]["accounts"] == ["Operating Checking 4321", "Card Fees"]
    fees = _account(db_session, "Card Fees")
    assert (fees.account_type, fees.detail_type, fees.bank_kind) == (
        AccountType.EXPENSE,
        "other_expense",
        None,
    )
    bank = _account(db_session, "Operating Checking 4321")
    assert (bank.account_type, bank.detail_type, bank.bank_kind) == (
        AccountType.ASSET,
        "bank",
        "bank",
    )
    again = _upload(client, FIXTURE.read_bytes(), answers)
    assert again["journals"] == 0 and again["duplicates_skipped"] == 8
    assert db_session.query(Account).filter(Account.name == "Card Fees").count() == 1


def test_an_account_left_out_leaves_out_what_touches_it(
    client, db_session, seed_accounts
):
    _chart_without(db_session, "Card Fees")
    data = _upload(client, FIXTURE.read_bytes(), {"accounts": {}})
    assert data["journals"] == 6
    assert data["left_out"] == {"unbalanced": 0, "missing_account": 2}
    assert _account(db_session, "Card Fees") is None
    # made later, the next upload brings those two in
    db_session.add(Account(name="Card Fees", account_type=AccountType.EXPENSE))
    db_session.commit()
    later = _upload(client, FIXTURE.read_bytes())
    assert later["journals"] == 2 and later["duplicates_skipped"] == 6


def test_a_sub_account_and_its_parent_are_made_once(client, db_session, seed_accounts):
    _chart_without(db_session, "Office Expense")
    data = _upload(client, FIXTURE.read_bytes())
    assert [(p["path"], p["transactions"]) for p in data["problems"]] == [
        ("Office Expense:Software", 1),
        ("Office Expense:Supplies", 1),
    ]
    both = {"Office Expense:Software": "expense", "Office Expense:Supplies": "expense"}
    data = _upload(client, FIXTURE.read_bytes(), {"accounts": both})
    assert data["journals"] == 8
    (parent,) = db_session.query(Account).filter(Account.name == "Office Expense").all()
    subs = db_session.query(Account).filter(Account.parent_id == parent.id).all()
    assert sorted(a.name for a in subs) == ["Software", "Supplies"]
    check = next(t for t in _imported(db_session) if t.reference == "1001")
    assert {ln.account.name for ln in check.lines} == {
        "Operating Checking 4321",
        "Software",
        "Supplies",
    }


def test_a_parents_own_section_asks_for_the_parent(client, db_session, seed_accounts):
    """QuickBooks prints a parent's own postings as "<Parent> - Other"; with
    the parent missing, the row asks for the parent itself."""
    data = _upload(client, _other_sections_report(2))
    assert [(p["path"], p["transactions"]) for p in data["problems"]] == [
        ("Office Expense", 1),
        ("Office Expense:Software", 1),
    ]
    data = _upload(
        client, _other_sections_report(2), {"accounts": {"Office Expense": "expense"}}
    )
    assert data["journals"] == 1
    assert data["left_out"] == {"unbalanced": 0, "missing_account": 1}
    office = _account(db_session, "Office Expense")
    (check,) = _imported(db_session)
    assert check.reference == "101"
    assert {ln.account_id for ln in check.lines} >= {office.id}
    # Software, made under it next time, brings the other check in
    data = _upload(
        client,
        _other_sections_report(2),
        {"accounts": {"Office Expense:Software": "expense"}},
    )
    assert data["journals"] == 1 and data["duplicates_skipped"] == 1
    software = _account(db_session, "Software")
    assert software.parent_id == office.id


@pytest.mark.parametrize(
    "answers, said",
    [
        ("not json", "could not be read"),
        ("[1, 2]", "could not be read"),
        ('{"unbalanced": "Check 1002"}', "could not be read"),
        ('{"accounts": ["Rent"]}', "could not be read"),
        ('{"accounts": {"Rent": "accounts_receivable"}}', "Rent"),
        ('{"accounts": {"Rent": "spending"}}', "spending"),
    ],
)
def test_answers_that_cannot_be_read_are_refused(
    client, db_session, seed_accounts, answers, said
):
    _chart(db_session)
    r = client.post(
        URL,
        files={"file": ("t.csv", io.BytesIO(FIXTURE.read_bytes()), "text/csv")},
        data={"answers": answers},
    )
    assert r.status_code == 400, r.text
    assert said in r.json()["detail"]
    assert _imported(db_session) == []


def test_answers_longer_than_a_text_field_come_as_a_part(
    client, db_session, seed_accounts
):
    """A form's text field holds 1 MB, and thousands of ticked keys can be
    more: the page sends the answers as a part of their own."""
    _chart(db_session)
    keys = [CHECK_1002] + [
        f"Check {n} · 2024-02-01 · Northwind Property Management Group"
        for n in range(30000)
    ]
    body = json.dumps({"unbalanced": keys}).encode()
    assert len(body) > 1024 * 1024
    r = client.post(
        URL,
        files={
            "file": ("t.csv", io.BytesIO(UNBALANCED), "text/csv"),
            "answers": ("answers.json", io.BytesIO(body), "application/json"),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["journals"] == 8
    assert r.json()["accepted"]["unbalanced"] == 1


# ── a file that looks cut short or edited ────────────────────────────────


def test_a_section_off_its_total_is_a_notice_for_the_file(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, EDITED)
    assert data["ready"] == 8
    (notice,) = data["problems"]
    assert notice["kind"] == "cut_off" and notice["key"] == "file"
    assert notice["details"] == [
        "The lines under Rent come to 1,000.00 debit and 0.00 credit, and "
        "QuickBooks' total says 1,100.00 and 0.00."
    ]
    assert notice["sections"] == [
        {
            "path": "Rent",
            "debit": "1000.00",
            "credit": "0.00",
            "total_debit": "1100.00",
            "total_credit": "0.00",
        }
    ]
    note = data["problem_notes"]["cut_off"]
    assert "32,768 rows" in note and "one year per file" in note


def test_a_notice_accepted_imports_what_is_there(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(client, EDITED, {"cut_off": True})
    assert data["journals"] == 8 and data["errors"] == []
    assert data["accepted"] == {"unbalanced": 0, "accounts": [], "cut_off": True}


def test_a_notice_not_accepted_imports_nothing(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(client, EDITED, {"cut_off": False, "unbalanced": []})
    assert data["journals"] == 0
    assert data["errors"] and "Nothing was imported" in data["errors"][0]
    assert _imported(db_session) == []


def test_a_file_cut_off_mid_section_says_so(client, db_session, seed_accounts):
    """Cut inside Office Expense:Software, the way Save as CSV stops at its
    row limit: the checks lose their other lines and do not balance, and
    the notice says where the file ends."""
    _chart(db_session)
    data = _upload(client, _cut_after("Scheduling app"))
    assert _kinds(data) == ["cut_off", "unbalanced", "unbalanced"]
    assert data["problems"][0]["details"] == [
        "The file ends inside Office Expense:Software, before QuickBooks' Total "
        "line for it."
    ]
    assert [p["key"] for p in data["problems"][1:]] == [
        "Check 1001 · 2024-01-08 · Office Depot",
        CHECK_1002,
    ]
    assert data["ready"] == 6
    # what is there, without the two checks
    data = _upload(client, _cut_after("Scheduling app"), {"cut_off": True})
    assert data["journals"] == 6
    assert data["left_out"] == {"unbalanced": 2, "missing_account": 0}


def test_a_row_that_cannot_be_read_is_in_the_notice(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(
        client,
        _variant(
            (
                ",,,,,Check,,01/31/2024,,1002,,,,Harbor Properties,,January rent,,Ö,,Operating",
                ",,,,,Check,,31/01/2024,,1002,,,,Harbor Properties,,January rent,,Ö,,Operating",
            )
        ),
    )
    notice = data["problems"][0]
    assert notice["kind"] == "cut_off"
    assert any("could not be read" in d for d in notice["details"]), notice
    assert any(d.startswith("The lines under Rent") for d in notice["details"])
    assert [p["key"] for p in data["problems"] if p["kind"] == "unbalanced"] == [
        CHECK_1002
    ]


# ── all three, and the adjusting-entries import ─────────────────────────


def test_every_kind_together_in_one_answer(client, db_session, seed_accounts):
    _chart_without(db_session, "Card Fees")
    text = UNBALANCED.decode("cp1252").replace(
        ",Total Service Income,,,,,,,,,,,,,,,,,,,,0.00,,620.00",
        ",Total Service Income,,,,,,,,,,,,,,,,,,,,0.00,,630.00",
    )
    data = _upload(client, text.encode("cp1252"))
    assert _kinds(data) == ["cut_off", "missing_account", "unbalanced"]
    assert data["ready"] == 5
    answers = {
        "cut_off": True,
        "accounts": {"Card Fees": "expense"},
        "unbalanced": [CHECK_1002],
    }
    data = _upload(client, text.encode("cp1252"), answers)
    assert data["journals"] == 8
    assert data["accepted"] == {
        "unbalanced": 1,
        "accounts": ["Card Fees"],
        "cut_off": True,
    }
    assert data["left_out"] == {"unbalanced": 0, "missing_account": 0}


def test_an_unbalanced_transaction_on_a_missing_account_needs_both(
    client, db_session, seed_accounts
):
    _chart_without(db_session, "Rent")
    data = _upload(client, UNBALANCED)
    assert _kinds(data) == ["missing_account", "unbalanced"]
    # the account alone: the check still waits on its own answer
    data = _upload(client, UNBALANCED, {"accounts": {"Rent": "expense"}})
    assert data["journals"] == 7
    assert data["left_out"] == {"unbalanced": 1, "missing_account": 0}
    data = _upload(client, UNBALANCED, {"unbalanced": [CHECK_1002]})
    assert data["journals"] == 1 and data["duplicates_skipped"] == 7


def test_the_adjusting_entries_import_still_refuses_a_whole_ledger(
    client, db_session, seed_accounts
):
    _chart_without(db_session, "Card Fees")
    for answers in (None, {"cut_off": True, "accounts": {"Card Fees": "expense"}}):
        data = _upload(client, FIXTURE.read_bytes(), answers, adjusting=True)
        assert data["journals"] == 0 and "problems" not in data
        (refusal,) = data["errors"]
        assert refusal.startswith(
            "The adjusting-entries import takes General Journal rows only"
        )
        assert "Nothing was posted" in refusal
    assert _imported(db_session) == []
    assert _account(db_session, "Card Fees") is None


def test_an_adjusting_file_imports_as_before(client, db_session, seed_accounts):
    r = _send(client, GJ_ONLY.encode(), adjusting=True)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["errors"] == [] and data["journals"] == 1 and data["adjusting"]
    (entry,) = _imported(db_session)
    assert entry.is_adjusting


def test_a_clean_file_imports_with_answers_or_without(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, FIXTURE.read_bytes(), {"unbalanced": ["anything"]})
    assert data["journals"] == 8 and "accepted" not in data and "problems" not in data
    lines = db_session.query(TransactionLine).count()
    assert lines == sum(len(t.lines) for t in _imported(db_session))
