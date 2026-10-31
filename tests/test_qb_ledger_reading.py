"""What a Transaction Detail by Account file says of itself, read before
anything posts (#663).

- A file cut short says so. QuickBooks closes every section with its Total
  line and the report with a TOTAL row; Save as CSV stops at 32,768 rows
  without a word. The notice missed a file cut inside its first section
  (before any Total line was read: an "All dates" export of a busy checking
  account) and one cut right after a top-level "Total X". A file with
  transaction rows and no TOTAL row is cut, and one whose TOTAL is not the
  sum of its lines was cut or edited.
- A row that cannot be read refuses the file, the rows listed with what to
  do: it is the one refusal left, because a guess would post a wrong date
  or amount. A day-first export (20/03/2024) showed as "the file may be cut
  off", and ticked, 05/03 posted as May 3, 11/04 as November 4, and 20/03
  was dropped uncounted. A file that is plainly day-first (a first part
  over 12 and no second part over 12) is read day-first, and says so.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.transactions import Transaction
from app.services.qb_ledger_import import SOURCE_TYPE, _money
from tests.qb_ledger_files import build, grand, heading, row, send, total


def _imported(db):
    return (
        db.query(Transaction)
        .filter(Transaction.source_type == SOURCE_TYPE)
        .order_by(Transaction.id)
        .all()
    )


def _checks(n, start=1001):
    return [
        row(
            "Check",
            "01/%02d/2024" % (1 + i % 28),
            str(start + i),
            "Vendor %d" % i,
            "",
            "Rent or Lease",
            "",
            "10.00",
        )
        for i in range(n)
    ]


def _cut_off(data):
    return next((p for p in data.get("problems", []) if p["kind"] == "cut_off"), None)


# ── a file cut short ─────────────────────────────────────────────────────


def test_a_file_cut_inside_its_first_section_says_so(client, db_session, seed_accounts):
    """Save as CSV stopped inside the first section, before any Total line."""
    data = send(client, build([heading("Checking")] + _checks(5)))
    notice = _cut_off(data)
    assert notice is not None, data["problems"]
    assert notice["details"] == [
        "The file ends inside Checking, before QuickBooks' Total line for it."
    ]
    assert [p["kind"] for p in data["problems"]] == ["cut_off"] + ["unbalanced"] * 5
    assert _imported(db_session) == []


def test_a_file_cut_after_a_sections_total_says_so(client, db_session, seed_accounts):
    """Stopped right after "Total Checking": every section read is whole,
    and the report's TOTAL row is not there."""
    data = send(
        client,
        build(
            [heading("Checking")] + _checks(5) + [total("Checking", "0.00", "50.00")]
        ),
    )
    notice = _cut_off(data)
    assert notice is not None, data["problems"]
    assert notice["details"] == [
        "The file ends after QuickBooks' Total line for Checking, without the "
        "report's TOTAL row."
    ]
    # ticked, what is there imports; unticked, nothing does
    answered = send(
        client,
        build(
            [heading("Checking")] + _checks(5) + [total("Checking", "0.00", "50.00")]
        ),
        {"cut_off": False},
    )
    assert answered["journals"] == 0 and "Nothing was imported" in answered["errors"][0]


def test_a_total_row_that_is_not_the_sum_of_the_lines_says_so(
    client, db_session, seed_accounts
):
    """Each section adds up to its own Total line, and the report's TOTAL
    says more: a whole section was taken out (here, Rent or Lease's)."""
    rows = [
        heading("Checking"),
        row("Check", "01/05/2024", "101", "Acme", "", "Rent or Lease", "", "60.00"),
        total("Checking", "0.00", "60.00"),
        grand("60.00", "60.00"),
    ]
    data = send(client, build(rows))
    notice = _cut_off(data)
    assert notice is not None, data["problems"]
    assert notice["details"] == [
        "The file's lines come to 0.00 debit and 60.00 credit, and QuickBooks' "
        "TOTAL row says 60.00 and 60.00."
    ]


def test_a_whole_file_has_no_notice(client, db_session, seed_accounts):
    rows = [
        heading("Checking"),
        row("Check", "01/05/2024", "101", "Acme", "", "Rent or Lease", "", "60.00"),
        total("Checking", "0.00", "60.00"),
        heading("Rent or Lease"),
        row("Check", "01/05/2024", "101", "Acme", "", "Checking", "60.00", ""),
        total("Rent or Lease", "60.00", "0.00"),
        grand("60.00", "60.00"),
    ]
    data = send(client, build(rows))
    assert data["errors"] == [] and data["journals"] == 1 and "problems" not in data


# ── rows that cannot be read ─────────────────────────────────────────────


def _day_first(days):
    """Three checks on the dates given, each with both of its lines."""
    bank, rent = [heading("Checking")], [heading("Rent or Lease")]
    for i, day in enumerate(days):
        num = str(101 + i)
        bank.append(row("Check", day, num, "Acme", "", "Rent or Lease", "", "10.00"))
        rent.append(row("Check", day, num, "Acme", "", "Checking", "10.00", ""))
    n = f"{10 * len(days):.2f}"
    return build(
        bank
        + [total("Checking", "0.00", n)]
        + rent
        + [total("Rent or Lease", n, "0.00"), grand(n, n)]
    )


def test_a_day_first_file_is_read_day_first_and_says_so(
    client, db_session, seed_accounts
):
    """The reviewer's file: 05/03 posted as May 3, 11/04 as November 4, and
    20/03 dropped. One date's first part is over 12 and none's second is,
    so every date is day/month/year."""
    data = send(client, _day_first(["05/03/2024", "20/03/2024", "11/04/2024"]))
    assert data["errors"] == [], data["errors"]
    assert "problems" not in data
    assert data["journals"] == 3
    assert sorted((t.reference, t.date) for t in _imported(db_session)) == [
        ("101", date(2024, 3, 5)),
        ("102", date(2024, 3, 20)),
        ("103", date(2024, 4, 11)),
    ]
    assert any("day/month/year" in w for w in data["warnings"]), data["warnings"]


def test_a_file_whose_dates_read_neither_way_is_refused(
    client, db_session, seed_accounts
):
    """20/03 is day-first and 03/25 month-first: no reading takes both, so
    the file is refused with the rows that cannot be read, and what to do."""
    data = send(client, _day_first(["05/03/2024", "20/03/2024", "03/25/2024"]))
    assert data["journals"] == 0 and "problems" not in data
    assert _imported(db_session) == []
    errors = data["errors"]
    assert errors[0].startswith("Nothing was imported: 2 rows could not be read")
    assert "month/day/year" in errors[0]
    assert errors[1:] == [
        "Row 4: the date ('20/03/2024') or an amount ('' / '10.00') could not be read",
        "Row 9: the date ('20/03/2024') or an amount ('10.00' / '') could not be "
        "read",
    ]


@pytest.mark.parametrize(
    "cell",
    ["1.000,00", "1,00", "12,34.56", "ten", "1.2.3"],
)
def test_an_amount_that_is_not_one_is_refused(client, db_session, seed_accounts, cell):
    """An amount must read the way QuickBooks writes one (1,234.56): a
    decimal comma read as a thousands separator made 1.000,00 one dollar."""
    rows = [
        heading("Checking"),
        row("Check", "01/05/2024", "101", "Acme", "", "Rent or Lease", "", cell),
        total("Checking", "0.00", cell),
        heading("Rent or Lease"),
        row("Check", "01/05/2024", "101", "Acme", "", "Checking", cell, ""),
        total("Rent or Lease", cell, "0.00"),
        grand(cell, cell),
    ]
    data = send(client, build(rows))
    assert data["journals"] == 0 and _imported(db_session) == []
    assert data["errors"][0].startswith("Nothing was imported: 2 rows could not")
    assert len(data["errors"]) == 3


@pytest.mark.parametrize(
    "cell, value",
    [
        ("1,234.56", "1234.56"),
        ("$1,234.56", "1234.56"),
        ("-75.00", "-75.00"),
        ("(75.00)", "-75.00"),
        ("75.00-", "-75.00"),
        ("1000", "1000"),
        (".50", "0.50"),
        ("", "0"),
    ],
)
def test_the_amounts_quickbooks_writes_are_read(cell, value):
    assert _money(cell) == Decimal(value)
