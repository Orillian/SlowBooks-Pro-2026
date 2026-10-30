"""What a Transaction Detail by Account file says of itself, read before
anything posts (#663).

- A file cut short says so. QuickBooks closes every section with its Total
  line and the report with a TOTAL row; Save as CSV stops at 32,768 rows
  without a word. The notice missed a file cut inside its first section
  (before any Total line was read: an "All dates" export of a busy checking
  account) and one cut right after a top-level "Total X". A file with
  transaction rows and no TOTAL row is cut, and one whose TOTAL is not the
  sum of its lines was cut or edited.
"""

from app.models.transactions import Transaction
from app.services.qb_ledger_import import SOURCE_TYPE
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
