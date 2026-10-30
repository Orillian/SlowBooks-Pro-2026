"""Transaction Detail by Account entries read in the register as a typed
entry does (#663).

The import wrote each journal's description as "{Type} {Num} — {Name}:
{Memo}", and the register shows a journal's description as its payee, so a
payee read "Check 1001 — Office Depot: Scheduling app". QuickBooks' Name is
now the payee, its Num the reference (Ref #), and each line's Memo stays on
its line. The import's key, which was the reference, has a column of its own
(source_ref), so a second upload still posts nothing twice; an entry imported
before keeps its key in the reference until the upgrade rewrites it, and is
found there.
"""

import csv
import io
from decimal import Decimal

from app.models.accounts import Account
from app.models.transactions import Transaction
from app.services.qb_ledger_import import SOURCE_TYPE
from tests.test_qb_desktop_ledger import FIXTURE, _chart, _upload

YEAR = {"start_date": "2024-01-01", "end_date": "2024-12-31"}


def _report(txns) -> bytes:
    """A Transaction Detail by Account export of `txns`, [(type, date, num,
    [(account, name, memo, debit, credit)])]: one label column before Type,
    a section per account in the order met, each closed by QuickBooks'
    Total row, and the TOTAL row."""
    sections: dict = {}
    for ttype, day, num, parts in txns:
        for account, name, memo, debit, credit in parts:
            others = {p[0] for p in parts} - {account}
            split = others.pop() if len(others) == 1 else "-SPLIT-"
            sections.setdefault(account, []).append(
                ["", ttype, day, num, name, memo, split, debit, credit, ""]
            )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(
        [
            "",
            "Type",
            "Date",
            "Num",
            "Name",
            "Memo",
            "Split",
            "Debit",
            "Credit",
            "Balance",
        ]
    )
    grand = [Decimal("0"), Decimal("0")]
    for account, rows in sections.items():
        writer.writerow([account] + [""] * 9)
        writer.writerows(rows)
        debit = sum((Decimal(r[7] or "0") for r in rows), Decimal("0"))
        credit = sum((Decimal(r[8] or "0") for r in rows), Decimal("0"))
        writer.writerow([f"Total {account}"] + [""] * 6 + [f"{debit}", f"{credit}", ""])
        grand = [grand[0] + debit, grand[1] + credit]
    writer.writerow(["TOTAL"] + [""] * 6 + [f"{grand[0]}", f"{grand[1]}", ""])
    return out.getvalue().encode("utf-8")


def _account(db, name):
    return db.query(Account).filter(Account.name == name).one()


def _imported(db):
    return (
        db.query(Transaction)
        .filter(Transaction.source_type == SOURCE_TYPE)
        .order_by(Transaction.id)
        .all()
    )


def _as_2311(db, data: bytes) -> None:
    """Put the entries this report imported back as 2.31.1 wrote them: the
    description "{Type} {Num} — {Name}: {Memo}" and the key as the
    reference, with no source_ref."""
    from app.routes.csv import _decode_csv_upload
    from app.services.qb_ledger_import import (
        LEDGER_COLUMNS,
        _import_key,
        group_journals,
        parse_ledger,
    )
    from app.services.qb_report_import import _csv_rows, _find_header_for

    rows = _csv_rows(_decode_csv_upload(data))
    at, cols = _find_header_for(rows, LEDGER_COLUMNS)
    journals, _ = group_journals(parse_ledger(rows, at, cols)[0])
    seen, old = {}, {}
    for journal in journals:
        first = journal[0]
        names = [ln["name"] for ln in journal if ln["name"]]
        memos = [ln["memo"] for ln in journal if ln["memo"]]
        old[_import_key(journal, seen)] = (
            f"{first['type']} {first['num']}".strip()
            + (f" — {names[0]}" if names else "")
            + (f": {memos[0]}" if memos else "")
        )[:200]
    for txn in _imported(db):
        if txn.source_ref in old:
            txn.description = old[txn.source_ref]
            txn.reference, txn.source_ref = txn.source_ref, None
    db.commit()


def test_the_register_shows_the_name_the_num_and_the_memo(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, FIXTURE.read_bytes())
    assert data["errors"] == [] and data["journals"] == 8
    checking = _account(db_session, "Operating Checking 4321")
    reg = client.get(
        "/api/banking/check-register", params={"account_id": checking.id}
    ).json()
    assert [
        (e["date"], e["payee"], e["reference"], e["description"])
        for e in reg["entries"]
    ] == [
        ("2024-01-01", "", "", "Beginning balance for this acct on 1-1-24"),
        ("2024-01-05", "Card Processor", "", "Deposit"),
        ("2024-01-05", "Walk-in", "", "Deposit"),
        # no memo on the check's bank line: the Memo column shows the payee,
        # as it does for an entry typed with none
        ("2024-01-08", "Office Depot", "1001", "Office Depot"),
        ("2024-01-15", "", "", "Funds Transfer"),
        ("2024-01-31", "Harbor Properties", "1002", "January rent"),
    ]
    check = next(t for t in _imported(db_session) if t.reference == "1001")
    assert check.description == "Office Depot"
    assert check.source_ref == "Check 1001 · 2024-01-08 · Office Depot"
    # each split line keeps its own memo, in the drill-down too
    supplies = (
        db_session.query(Account)
        .filter(
            Account.name == "Supplies",
            Account.parent_id == _account(db_session, "Office Expense").id,
        )
        .one()
    )
    drill = client.get(
        "/api/reports/account-transactions",
        params={"account_id": supplies.id, **YEAR},
    ).json()
    assert [
        (e["payee"], e["reference"], e["description"]) for e in drill["entries"]
    ] == [("Office Depot", "1001", "Paper")]


def test_a_journal_entry_with_no_name_reads_as_its_memo(
    client, db_session, seed_accounts
):
    """A general journal entry has no name: no payee, and the journal page,
    the drill-down, the general ledger and its void read its memo."""
    _chart(db_session)
    _upload(client, FIXTURE.read_bytes())
    listed = {
        e["reference"]: e
        for e in client.get("/api/journal").json()
        if e["source_type"] == SOURCE_TYPE
    }
    assert listed["7"]["description"] == "Card sales, week 2"
    assert listed["8"]["description"] == "Chargeback fee"
    assert listed["1002"]["description"] == "Harbor Properties"
    fees = _account(db_session, "Card Fees")
    drill = client.get(
        "/api/reports/account-transactions", params={"account_id": fees.id, **YEAR}
    ).json()
    assert [
        (e["payee"], e["reference"], e["description"]) for e in drill["entries"]
    ] == [
        ("", "7", "Card sales, week 2"),
        ("", "8", "Chargeback fee"),
    ]
    gl = client.get(
        "/api/reports/general-ledger", params={"account_id": fees.id, **YEAR}
    ).json()
    assert [
        (e["reference"], e["description"]) for e in gl["accounts"][0]["entries"]
    ] == [("7", "Card sales, week 2"), ("8", "Chargeback fee")]
    void = client.post(f"/api/journal/{listed['7']['id']}/void")
    assert void.status_code == 200, void.text
    assert void.json()["description"] == "VOID: Card sales, week 2"
    assert void.json()["reference"] == "7"


def test_adjusting_entries_with_no_name_list_with_their_memo(
    client, db_session, seed_accounts
):
    _chart(db_session)
    report = _report(
        [
            (
                "General Journal",
                "12/31/2024",
                "AJE-3",
                [
                    ("Card Fees", "", "Year-end fees", "40.00", ""),
                    ("Card Clearing", "", "Year-end fees", "", "40.00"),
                ],
            )
        ]
    )
    body = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("aje.csv", io.BytesIO(report), "text/csv")},
        data={"adjusting_only": "true"},
    ).json()
    assert body["errors"] == [] and body["journals"] == 1, body
    aje = client.get("/api/reports/adjusting-entries", params=YEAR).json()
    assert [(e["reference"], e["description"]) for e in aje["entries"]] == [
        ("AJE-3", "Year-end fees")
    ]


NEXT_MONTH = [
    # in the fixture already
    (
        "Deposit",
        "01/05/2024",
        "",
        [
            ("Operating Checking 4321", "Walk-in", "Deposit", "120.00", ""),
            ("Service Income", "Walk-in", "Deposit", "", "120.00"),
        ],
    ),
    (
        "Check",
        "01/31/2024",
        "1002",
        [
            ("Rent", "Harbor Properties", "January rent", "1000.00", ""),
            (
                "Operating Checking 4321",
                "Harbor Properties",
                "January rent",
                "",
                "1000.00",
            ),
        ],
    ),
    # new
    (
        "Check",
        "02/29/2024",
        "1003",
        [
            ("Rent", "Harbor Properties", "February rent", "1000.00", ""),
            (
                "Operating Checking 4321",
                "Harbor Properties",
                "February rent",
                "",
                "1000.00",
            ),
        ],
    ),
]


def test_an_overlapping_file_posts_only_what_is_new(client, db_session, seed_accounts):
    _chart(db_session)
    _upload(client, FIXTURE.read_bytes())
    again = _upload(client, _report(NEXT_MONTH))
    assert again["errors"] == [], again["errors"]
    assert again["journals"] == 1 and again["duplicates_skipped"] == 2
    new = next(t for t in _imported(db_session) if t.reference == "1003")
    assert new.description == "Harbor Properties"
    assert new.source_ref == "Check 1003 · 2024-02-29 · Harbor Properties"
    assert len(_imported(db_session)) == 9


def test_an_entry_imported_before_is_found_by_the_key_in_its_reference(
    client, db_session, seed_accounts
):
    """Books imported by 2.31.1 that the upgrade could not read back keep
    their key in the reference: a second upload still skips them."""
    _chart(db_session)
    _upload(client, FIXTURE.read_bytes())
    _as_2311(db_session, FIXTURE.read_bytes())
    check = next(
        t for t in _imported(db_session) if t.reference.startswith("Check 1002")
    )
    assert check.description == "Check 1002 — Harbor Properties: January rent"
    assert check.source_ref is None
    again = _upload(client, FIXTURE.read_bytes())
    assert again["journals"] == 0 and again["duplicates_skipped"] == 8
    overlapping = _upload(client, _report(NEXT_MONTH))
    assert overlapping["journals"] == 1 and overlapping["duplicates_skipped"] == 2
    assert len(_imported(db_session)) == 9
