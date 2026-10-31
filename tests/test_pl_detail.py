"""Profit & Loss Detail (#477): every transaction under each P&L account,
QuickBooks Desktop's columns (Type, Date, Num, Name, Memo, Split, Amount,
Balance), a running balance and total per account, sub-accounts beneath
their parents, and section totals that are the Profit & Loss's for the
same dates, class, job and adjusting-entries choice.
"""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.services.accounting import create_journal_entry
from app.services.ledger_exports import rows_of

D = Decimal
PERIOD = "start_date=2026-04-01&end_date=2026-04-30"
TOTAL_KEYS = (
    "total_income",
    "total_operating_income",
    "total_cogs",
    "gross_profit",
    "operating_gross_profit",
    "total_expenses",
    "total_operating_expenses",
    "net_operating_income",
    "total_other_income",
    "total_other_expense",
    "net_other_income",
    "net_income",
)


def _add(db, number, name, atype, parent=None, detail=None):
    acct = Account(
        account_number=number,
        name=name,
        account_type=atype,
        parent_id=parent.id if parent else None,
        detail_type=detail,
    )
    db.add(acct)
    db.flush()
    return acct


def _line(a, dr=0, cr=0, **kw):
    return {"account_id": a.id, "debit": D(str(dr)), "credit": D(str(cr)), **kw}


# Named, so another module can import it without shadowing the name its
# tests ask for.
@pytest.fixture(name="books")
def books_fixture(client, db_session, seed_accounts, seed_customer):
    """Travel > Hotel > Lodging Tax and Flight; an Other Income account;
    a class (Retail) and a job (Kitchen); an adjusting entry; a three-line
    entry (the Split column's -SPLIT-); and lines outside the period."""
    db = db_session
    a = dict(seed_accounts)
    E = AccountType.EXPENSE
    a["travel"] = _add(db, "7000", "Travel", E)
    a["hotel"] = _add(db, "7010", "Hotel", E, a["travel"])
    a["lodging"] = _add(db, "7011", "Lodging Tax", E, a["hotel"])
    a["flight"] = _add(db, "7020", "Flight", E, a["travel"])
    a["interest"] = _add(
        db, "7900", "Interest Earned", AccountType.INCOME, detail="other_income"
    )
    retail = client.post("/api/classes", json={"name": "Retail"}).json()["id"]
    kitchen = client.post(
        "/api/jobs", json={"customer_id": seed_customer.id, "name": "Kitchen"}
    ).json()["id"]
    cash, sales, ads, cogs = a["1000"], a["4000"], a["6000"], a["5000"]

    def je(day, what, lines, **kw):
        create_journal_entry(db, day, what, lines, **kw)

    d = date(2026, 4, 3)
    je(d, "sale", [_line(cash, 500), _line(sales, cr=500)], reference="R-1")
    je(
        date(2026, 4, 5),
        "retail sale",
        [_line(cash, 300), _line(sales, cr=300)],
        class_id=retail,
    )
    je(
        date(2026, 4, 6),
        "kitchen job",
        [_line(cogs, 120), _line(sales, cr=200), _line(cash, 80)],
        job_id=kitchen,
    )
    je(date(2026, 4, 7), "trip", [_line(a["travel"], 50), _line(cash, cr=50)])
    je(date(2026, 4, 8), "hotel", [_line(a["hotel"], 100), _line(cash, cr=100)])
    je(date(2026, 4, 8), "tax", [_line(a["lodging"], 10), _line(cash, cr=10)])
    je(
        date(2026, 4, 9),
        "flight and ads",
        [
            _line(a["flight"], 200, description="Seat 4A"),
            _line(ads, 40),
            _line(cash, cr=240),
        ],
    )
    je(date(2026, 4, 20), "interest", [_line(cash, 7), _line(a["interest"], cr=7)])
    je(
        date(2026, 4, 30),
        "accrual",
        [_line(ads, 60), _line(a["2000"], cr=60)],
        is_adjusting=True,
    )
    # outside the period: neither the detail nor its balances see them
    je(date(2026, 3, 31), "march", [_line(ads, 999), _line(cash, cr=999)])
    je(date(2026, 5, 1), "may", [_line(sales, cr=888), _line(cash, 888)])
    db.commit()
    return {**a, "retail": retail, "kitchen": kitchen}


def _get(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def _detail(client, qs=""):
    return _get(client, f"/api/reports/profit-loss-detail?{PERIOD}{qs}")


def _pl(client, qs=""):
    return _get(client, f"/api/reports/profit-loss?{PERIOD}{qs}")


def _block(d, number):
    for blocks in d["sections"].values():
        for b in blocks:
            if b["account_number"] == number:
                return b
    raise AssertionError(number)


@pytest.mark.parametrize(
    "qs",
    ["", "&exclude_adjusting=true", "&sort=name", "&class_id={retail}"],
    ids=["all", "no-adjusting", "by-name", "one-class"],
)
def test_the_section_totals_are_the_profit_and_loss(client, books, qs):
    qs = qs.format(retail=books["retail"])
    d, pl = _detail(client, qs), _pl(client, qs)
    for key in TOTAL_KEYS:
        assert d[key] == pl[key], key
    # account by account too, and each account's lines add to its total
    for section in ("income", "cogs", "expenses", "other_income", "other_expense"):
        flat = {i["account_id"]: i["amount"] for i in pl[section] if i["amount"]}
        mine = {b["account_id"]: b["amount"] for b in d["sections"][section]}
        assert mine == flat, section
        for b in d["sections"][section]:
            assert sum(D(str(e["amount"])) for e in b["entries"]) == D(str(b["amount"]))
            assert b["entries"][-1]["balance"] == b["amount"]
    assert d["has_other"] == pl["has_other"]


def test_the_net_income_without_the_filters(client, books):
    d = _detail(client)
    # 500 + 300 + 200 sales, 120 COGS, 50+100+10+200+40+60 expenses, 7 interest
    assert d["total_operating_income"] == 1000
    assert d["total_cogs"] == 120
    assert d["total_operating_expenses"] == 460
    assert d["total_other_income"] == 7
    assert d["net_income"] == 1000 - 120 - 460 + 7
    assert d["has_other_income"] and not d["has_other_expense"]


def test_a_job_is_that_jobs_lines_and_ties_to_p_and_l_by_job(client, books):
    job = books["kitchen"]
    d = _detail(client, f"&job_id={job}")
    assert d["job_name"] == "Kitchen"
    grid = _get(client, f"/api/reports/profit-loss-by-job?{PERIOD}&job_ids={job}")
    col = grid["jobs"][0]
    assert d["total_operating_income"] == col["operating_income"] == 200
    assert d["total_cogs"] == col["cogs"] == 120
    assert d["net_income"] == col["net_income"] == 80
    # 0 is the lines with no job: the two add up to the company
    none = _detail(client, "&job_id=0")
    assert none["net_income"] + d["net_income"] == _pl(client)["net_income"]
    assert (
        client.get(f"/api/reports/profit-loss-detail?{PERIOD}&job_id=99999").status_code
        == 404
    )


def test_the_columns_are_quickbooks_and_the_lines_run_in_date_order(client, books):
    d = _detail(client)
    sales = _block(d, "4000")
    assert [e["date"] for e in sales["entries"]] == [
        "2026-04-03",
        "2026-04-05",
        "2026-04-06",
    ]
    first = sales["entries"][0]
    assert first["type"] == "Journal entry"
    assert first["num"] == "R-1"
    assert first["memo"] == "sale"
    assert first["split"] == "1000 - Checking"
    assert first["source_link"].startswith("/#/journal/")
    # a running balance from zero: the period's, not the account's to date
    assert [e["balance"] for e in sales["entries"]] == [500, 800, 1000]
    # the kitchen entry has two other accounts: -SPLIT-, as QuickBooks writes it
    assert sales["entries"][2]["split"] == "-SPLIT-"
    # the line's own memo wins over the entry's
    flight = _block(d, "7020")
    assert flight["entries"][0]["memo"] == "Seat 4A"
    assert flight["entries"][0]["split"] == "-SPLIT-"


def test_sub_accounts_print_beneath_their_parents(client, books):
    d = _detail(client)
    shape = [
        (r["kind"], r["depth"], r["label"], r.get("amount"))
        for r in d["tree"]["expenses"]
    ]
    assert shape == [
        ("account", 0, "6000 - Advertising & Marketing", 100.0),
        ("heading", 0, "7000 - Travel", None),
        ("heading", 1, "7010 - Hotel", None),
        ("account", 2, "7011 - Lodging Tax", 10.0),
        ("direct", 2, "Direct postings to Hotel", 100.0),
        ("total", 1, "Total 7010 - Hotel", 110.0),
        ("account", 1, "7020 - Flight", 200.0),
        ("direct", 1, "Direct postings to Travel", 50.0),
        ("total", 0, "Total 7000 - Travel", 360.0),
    ]
    # the account rows carry their lines into the tree
    lodging = next(
        r for r in d["tree"]["expenses"] if r["label"] == "7011 - Lodging Tax"
    )
    assert [e["amount"] for e in lodging["entries"]] == [10.0]


def test_the_csv_adds_up_the_way_the_screen_does(client, books):
    r = client.get(f"/api/reports/profit-loss-detail/csv?{PERIOD}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].endswith(
        "profit-loss-detail_2026-04-01_2026-04-30.csv"
    )
    text = r.text.lstrip("\ufeff")
    assert text.splitlines()[1] == "Report,Profit & Loss Detail"
    rows = rows_of(text)
    head = rows[0]
    assert head == [
        "Section", "Level", "Account number", "Account", "Type", "Date",
        "Num", "Name", "Memo", "Split", "Amount", "Balance",
    ]  # fmt: skip
    body = rows[1:]
    lines = [x for x in body if x[5]]
    d = _detail(client)
    assert len(lines) == d["line_count"]
    by_label = {x[3]: x for x in body if not x[5]}
    for section, total in (
        ("Income", "Total Income"),
        ("Expenses", "Total Expenses"),
        ("Other Income", "Total Other Income"),
    ):
        got = sum(D(x[10]) for x in lines if x[0] == section)
        assert got == D(by_label[total][10]), section
    assert by_label["Total 7000 - Travel"][10] == "360.00"
    assert by_label["Total 7000 - Travel"][1] == "0"
    assert by_label["Net Income"][10] == f"{D(str(d['net_income'])):.2f}"
    hotel_tax = next(x for x in lines if x[3] == "Travel: Hotel: Lodging Tax")
    assert hotel_tax[1] == "2"
    # the product's own -SPLIT- is text in a spreadsheet as it stands: no
    # formula guard's apostrophe (Excel showed '-SPLIT-)
    splits = [x[9] for x in lines if "SPLIT" in x[9]]
    assert splits and set(splits) == {"-SPLIT-"}, splits
    assert "'-SPLIT-" not in text


def test_the_split_word_is_unguarded_but_user_text_still_is():
    from app.services.csv_export import _csv_safe, _Verbatim

    assert _csv_safe(_Verbatim("-SPLIT-")) == "-SPLIT-"
    assert _csv_safe("-SUM(A1)") == "'-SUM(A1)"
    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"


def test_the_pdf_is_landscape_with_the_class_named(client, books):
    r = client.get(
        f"/api/reports/profit-loss-detail/pdf?{PERIOD}&class_id={books['retail']}"
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert (
        "profit-loss-detail_retail_2026-04-01_2026-04-30.pdf"
        in r.headers["content-disposition"]
    )
    assert r.content[:4] == b"%PDF"


def test_the_pdf_section_reads_left_to_right(db_session, books, client):
    from app.routes.reports.pl_detail import _detail_section
    from app.services.terminology import Terms

    d = _detail(client)
    s = _detail_section(d, Terms())
    assert s["columns"] == [
        "Type",
        "Date",
        "Num",
        "Name",
        "Memo",
        "Split",
        "Amount",
        "Balance",
    ]
    assert s["text_columns"] == 6
    cells = [r["cells"][0] for r in s["rows"]]
    assert cells[0] == "Income"
    assert "Total Income" in cells and "Gross Profit" in cells
    assert cells[-1] == "Net Income"
    assert s["rows"][-1]["cells"][6] == "$427.00"


def test_a_nonprofit_reads_its_own_words(client, books, db_session):
    r = client.put("/api/settings", json={"company_type": "nonprofit"})
    assert r.status_code == 200, r.text
    text = client.get(f"/api/reports/profit-loss-detail/csv?{PERIOD}").text
    text = text.lstrip("\ufeff")
    assert text.splitlines()[1] == "Report,Statement of Activities Detail"
    assert any(x[0] == "Revenue & Support" for x in rows_of(text))


def test_an_empty_period_has_no_lines_and_zero_totals(client, books):
    d = _get(
        client,
        "/api/reports/profit-loss-detail?start_date=2020-01-01&end_date=2020-01-31",
    )
    assert d["line_count"] == 0 and d["net_income"] == 0
    assert all(not v for v in d["sections"].values())
    r = client.get(
        "/api/reports/profit-loss-detail?start_date=2026-05-01&end_date=2026-04-01"
    )
    assert r.status_code == 400


def test_it_is_a_saved_report_type_and_a_report_center_view():
    from app.routes.saved_reports import _ALLOWED_TYPES

    assert "profit_loss_detail" in _ALLOWED_TYPES
    js = Path("app/static/js/reports.js").read_text(encoding="utf-8")
    assert "'profit-loss-detail':" in js
    assert "card('profit-loss-detail'" in js


def test_the_type_words_are_the_screens():
    """The PDF and CSV's Type column (bank_register.source_word) and the
    screen's (utils.js sourceWord) name every posting alike."""
    from app.services.bank_register import _WORDS, source_word

    js = Path("app/static/js/utils.js").read_text(encoding="utf-8")
    body = re.search(
        r"function sourceWord\(type, kind\) \{\s*const words = \{(.*?)\};", js, re.S
    )
    assert body, "sourceWord's map"
    keys = set(re.findall(r"(\w+):\s*(?:'|\(\))", body.group(1)))
    assert keys == set(_WORDS)
    for key, word in re.findall(r"(\w+):\s*'([^']*)'", body.group(1)):
        assert _WORDS[key] == word, key
    assert source_word("bill_payment_void") == "Bill payment void"
    assert source_word("some_new_kind") == "Some new kind"
    assert source_word(None) == "Journal entry"
    # an import's own word for the row (#663)
    assert source_word("qb_ledger_import", kind="Check") == "Check (imported)"
    assert "if (kind) return `${kind} (imported)`;" in js
