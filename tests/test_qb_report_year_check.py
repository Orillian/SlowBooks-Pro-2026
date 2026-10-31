"""A Transaction Detail by Account report that covers more than 12 months
gets a warning (#568), never a refusal.

For such a range QuickBooks prints the balance sheet accounts for the whole
range but the income and expense accounts for its last year only, so the
import could look half-complete and nobody knew why. The period is read from
the title block QuickBooks prints above the column headers (and the range
cell under them). A July-June fiscal year spans two calendar years but is 12
months, so it imports with nothing said; the adjusting-entries import never
warns; and when the file does not balance the explanation sits beside the
transactions that do not (#663).
"""

import io
from datetime import date

import pytest

from app.models.transactions import Transaction
from app.services.qb_ledger_import import SOURCE_TYPE
from app.services.qb_report_import import (
    _csv_rows,
    _period_range,
    long_period,
)
from tests.test_qb_desktop_ledger import FIXTURE, _chart
from tests.test_qb_desktop_round_trip import GJ_ONLY
from tests.test_import_errors_full_list import _ledger_with_one_sided_checks

WIDTH = 28  # the fixture's column count


def _titled(period: str) -> bytes:
    """The ledger fixture under the title block QuickBooks prints: company
    and time, report name and date, period and basis."""
    pad = "," * (WIDTH - 2)
    title = (
        f"Morden Bookkeeping{pad}8:16 PM\r\n"
        f"Transaction Detail by Account{pad}08/18/26\r\n"
        f'"{period}"{pad}Accrual Basis\r\n'  # quoted: the period has commas
    )
    return title.encode("cp1252") + FIXTURE.read_bytes()


def _upload(client, data: bytes):
    r = client.post(
        "/api/csv/import/qb-report",
        files={
            "file": ("Transaction Detail by Account.csv", io.BytesIO(data), "text/csv")
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _journals(db) -> int:
    return db.query(Transaction).filter(Transaction.source_type == SOURCE_TYPE).count()


@pytest.mark.parametrize(
    "text, span",
    [
        ("January through December 2024", (date(2024, 1, 1), date(2024, 12, 31))),
        ("July 2023 through June 2024", (date(2023, 7, 1), date(2024, 6, 30))),
        (
            "January 1, 2023 through December 31, 2024",
            (date(2023, 1, 1), date(2024, 12, 31)),
        ),
        ("April 9 through August 18, 2026", (date(2026, 4, 9), date(2026, 8, 18))),
        ("Apr 9 - Aug 18, '26", (date(2026, 4, 9), date(2026, 8, 18))),
        ("Jul 1, '23 - Jun 30, '24", (date(2023, 7, 1), date(2024, 6, 30))),
        ("01/01/2023 through 12/31/2024", (date(2023, 1, 1), date(2024, 12, 31))),
        ("December 1 through March 31, 2026", (date(2025, 12, 1), date(2026, 3, 31))),
        ("All Transactions", None),
        ("Accrual Basis", None),
        ("8:16 PM", None),
        ("08/18/26", None),
        ("Beginning balance for this acct on 1-1-24", None),
    ],
)
def test_the_period_text_is_read_as_a_date_range(text, span):
    assert _period_range(text) == span


def test_the_range_cell_under_the_headers_is_read_too():
    rows = _csv_rows(
        "Sunrise,,,8:16 PM\nTransaction Detail by Date,,,08/18/26\n"
        ",Date,Num,Name\n\"Jan 1, '25 - Dec 31, '26\",,,\n,01/02/2025,1,Walk-in\n"
    )
    assert long_period(rows, 2) == "Jan 1, '25 - Dec 31, '26"
    # a data row's date is never a period
    assert long_period(rows, 3) is None


@pytest.mark.parametrize(
    "period, long",
    [
        ("January through December 2024", False),
        ("July 1, 2023 through June 30, 2024", False),  # a fiscal year
        ("July 2023 through June 2024", False),
        ("January 1, 2023 through December 31, 2023", False),
        ("January 1, 2023 through January 1, 2024", True),  # a year and a day
        ("November 2023 through December 2024", True),  # 14 months
        ("January 1, 2023 through December 31, 2024", True),
    ],
)
def test_more_than_twelve_months_is_the_line(period, long):
    rows = [[period], ["Date", "Num"]]
    assert bool(long_period(rows, 1)) is long


def test_a_fiscal_year_over_two_calendar_years_imports_with_no_warning(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, _titled("July 1, 2023 through June 30, 2024"))
    assert data["errors"] == [], data["errors"]
    assert data["warnings"] == []
    assert data["journals"] == 8
    assert _journals(db_session) == 8


def test_a_fourteen_month_file_imports_with_a_warning(
    client, db_session, seed_accounts
):
    _chart(db_session)
    period = "November 1, 2023 through December 31, 2024"
    data = _upload(client, _titled(period))
    assert data["errors"] == [], data["errors"]
    assert data["journals"] == 8  # a warning, not a refusal
    assert _journals(db_session) == 8
    assert len(data["warnings"]) == 1
    note = data["warnings"][0]
    assert period in note and "more than 12 months" in note
    assert "one year per file" in note and "oldest year first" in note


def test_the_adjusting_entries_import_never_warns_on_a_long_period(
    client, db_session, seed_accounts
):
    pad = "," * 26
    title = (
        f"Morden Bookkeeping{pad}\n"
        f'"November 1, 2023 through December 31, 2024"{pad}\n'
    )
    r = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("aje.csv", io.BytesIO((title + GJ_ONLY).encode()), "text/csv")},
        data={"adjusting_only": "true"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["errors"] == [], data["errors"]
    assert not any("12 months" in w for w in data["warnings"])  # the A/R-A/P note stays
    assert data["journals"] == 1


def test_a_long_file_that_does_not_balance_carries_the_one_year_note(
    client, db_session, seed_accounts
):
    _chart(db_session)
    pad = "," * 26
    title = f'"November 1, 2023 through December 31, 2024"{pad}\n'
    data = _upload(client, title.encode() + _ledger_with_one_sided_checks(3))
    assert data["journals"] == 0
    assert [p["kind"] for p in data["problems"]] == ["unbalanced"] * 3
    note = data["problem_notes"]["unbalanced"]
    assert "do not balance" in note
    assert "more than 12 months" in note and "one year per file" in note
    assert data["warnings"] == []  # said once, beside them
    assert _journals(db_session) == 0


def test_a_short_file_that_does_not_balance_has_no_note(
    client, db_session, seed_accounts
):
    _chart(db_session)
    pad = "," * 26
    title = f'"January 1, 2024 through December 31, 2024"{pad}\n'
    data = _upload(client, title.encode() + _ledger_with_one_sided_checks(2))
    assert len(data["problems"]) == 2
    assert "12 months" not in data["problem_notes"]["unbalanced"]
    assert data["warnings"] == []


def test_a_single_year_report_imports_as_before(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(client, _titled("January through December 2024"))
    assert data["errors"] == [], data["errors"]
    assert data["warnings"] == []
    assert data["journals"] == 8


def test_a_report_with_no_period_imports_as_before(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(client, FIXTURE.read_bytes())
    assert data["errors"] == [], data["errors"]
    assert data["warnings"] == []
    assert data["journals"] == 8


def test_an_all_transactions_report_is_not_a_range(client, db_session, seed_accounts):
    _chart(db_session)
    data = _upload(client, _titled("All Transactions"))
    assert data["errors"] == [], data["errors"]
    assert data["journals"] == 8
