"""QuickBooks Desktop's transaction warnings (#481): a date far from
today, a duplicate check number, and editing a transaction already saved.
Company settings, off until a company turns them on.

- The settings: defaults (off; 90 days back, 30 ahead), "true"/"false" only,
  day counts whole numbers from 1 to 3650; Settings draws them as named
  boxes sent as "true"/"false".
- A duplicate check number is a 409 (code duplicate_check_number) naming
  where the number is used, across register entries, expenses (checks) and
  bill payments on the same bank account; the request sent again with
  allow_duplicate_check_number=true posts. A deposit's reference, a number
  on another account, "ACH" or "rcpt 1187", a voided entry's number and an entry's own
  number on an edit are not duplicates; with the setting off nothing is
  refused.
- Every transaction form asks TxnWarn.ok before it saves (the date, and on
  an edit form, the edit), the register's quick row too, and the check
  number's 409 is answered on the register, an expense and a bill payment.

The questions on the page are tests/test_browser_transaction_warnings.py.
"""

from pathlib import Path

import pytest

from app.models.settings import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "static" / "js"
OVERRIDE = "?allow_duplicate_check_number=true"


def _js(name):
    return (JS / name).read_text(encoding="utf-8")


# ---- the settings -----------------------------------------------------------


def test_the_warnings_default_off_until_turned_on(client):
    got = client.get("/api/settings").json()
    for key, want in {
        "warn_date_past": "false",
        "warn_date_past_days": "90",
        "warn_date_future": "false",
        "warn_date_future_days": "30",
        "warn_duplicate_check_number": "false",
        "warn_edit_transaction": "false",
    }.items():
        assert DEFAULT_SETTINGS[key] == want and got[key] == want, key


@pytest.mark.parametrize(
    "body",
    [
        {"warn_edit_transaction": "maybe"},
        {"warn_date_past_days": "0"},
        {"warn_date_future_days": "-5"},
        {"warn_date_future_days": "ten"},
        {"warn_date_past_days": "4000"},
    ],
)
def test_a_bad_value_is_refused(client, body):
    assert client.put("/api/settings", json=body).status_code == 422


def test_the_settings_save(client):
    r = client.put(
        "/api/settings",
        json={"warn_edit_transaction": "false", "warn_date_past_days": "365"},
    )
    assert r.status_code == 200, r.text
    got = client.get("/api/settings").json()
    assert got["warn_edit_transaction"] == "false"
    assert got["warn_date_past_days"] == "365"


# ---- a duplicate check number ----------------------------------------------


def _turn_on(client):
    r = client.put("/api/settings", json={"warn_duplicate_check_number": "true"})
    assert r.status_code == 200, r.text


def _entry(client, accounts, ref, amount="-25.00", bank="1000", query=""):
    return client.post(
        "/api/banking/transactions" + query,
        json={
            "account_id": accounts[bank].id,
            "date": "2026-10-01",
            "amount": amount,
            "category_account_id": accounts["6100"].id,
            "payee": "Gas Co",
            "check_number": ref,
        },
    )


def test_a_second_register_payment_with_the_number_is_a_409(client, seed_accounts):
    _turn_on(client)
    assert _entry(client, seed_accounts, "5001").status_code == 201
    r = _entry(client, seed_accounts, " 5001 ")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_check_number"
    assert detail["number"] == "5001"
    assert detail["message"] == (
        "Check number 5001 is already used on 1000 Checking (Gas Co, 10/1/2026, $25.00)."
    )
    assert detail["uses"] == [
        {"date": "2026-10-01", "payee": "Gas Co", "amount": "25.00"}
    ]
    # keep the number
    assert _entry(client, seed_accounts, "5001", query=OVERRIDE).status_code == 201


def test_what_is_not_a_duplicate(client, seed_accounts):
    _turn_on(client)
    assert _entry(client, seed_accounts, "5001").status_code == 201
    # money in: a deposit slip's number is not a check
    assert _entry(client, seed_accounts, "5001", amount="40.00").status_code == 201
    # another bank account
    assert _entry(client, seed_accounts, "5001", bank="1010").status_code == 201
    # not a number
    for word in ("ACH", "ACH", "rcpt 1187", "rcpt 1187"):
        assert _entry(client, seed_accounts, word).status_code == 201
    # a void frees its number
    first = _entry(client, seed_accounts, "7001").json()
    assert client.post(f"/api/banking/entries/{first['id']}/void").status_code == 200
    assert _entry(client, seed_accounts, "7001").status_code == 201


def test_an_edit_keeping_its_number_is_not_asked_one_taking_another_is(
    client, seed_accounts
):
    _turn_on(client)
    a = _entry(client, seed_accounts, "5001").json()
    b = _entry(client, seed_accounts, "5002").json()
    path = f"/api/banking/entries/{b['id']}"
    assert client.put(path, json={"description": "fuel"}).status_code == 200
    assert client.put(path, json={"check_number": "5002"}).status_code == 200
    r = client.put(path, json={"check_number": "5001"})
    assert (
        r.status_code == 409 and r.json()["detail"]["code"] == "duplicate_check_number"
    )
    assert client.put(path + OVERRIDE, json={"check_number": "5001"}).status_code == 200
    assert a["id"] != b["id"]


def _vendor_bill(client, accounts, amount="100.00"):
    v = client.post("/api/vendors", json={"name": "Paper Mill"}).json()
    bill = client.post(
        "/api/bills",
        json={
            "vendor_id": v["id"],
            "bill_number": "PM-1",
            "date": "2026-10-01",
            "lines": [
                {
                    "account_id": accounts["6000"].id,
                    "description": "Flyers",
                    "quantity": 1,
                    "rate": amount,
                }
            ],
        },
    ).json()
    return v, bill


def _pay(client, vendor, bill, number, query="", headers=None):
    return client.post(
        "/api/bill-payments" + query,
        json={
            "vendor_id": vendor["id"],
            "date": "2026-10-02",
            "amount": "50.00",
            "method": "check",
            "check_number": number,
            "allocations": [{"bill_id": bill["id"], "amount": "50.00"}],
        },
        headers=headers or {},
    )


def test_bill_payments_checks_and_register_share_the_numbers(client, seed_accounts):
    _turn_on(client)
    vendor, bill = _vendor_bill(client, seed_accounts)
    # a bill payment from 1000 Checking (the default) takes 6001
    assert _pay(client, vendor, bill, "6001").status_code == 201
    # the register sees it
    r = _entry(client, seed_accounts, "6001")
    assert r.status_code == 409
    assert "Paper Mill" in r.json()["detail"]["message"]
    # a second bill payment with it
    r = _pay(client, vendor, bill, "6001")
    assert r.status_code == 409
    assert _pay(client, vendor, bill, "6001", query=OVERRIDE).status_code == 201
    # an expense (a check) on Checking
    expense = {
        "date": "2026-10-03",
        "payee": "Office Depot",
        "expense_account_id": seed_accounts["6000"].id,
        "paid_from_account_id": seed_accounts["1000"].id,
        "amount": "12.00",
        "reference": "6001",
    }
    r = client.post("/api/expenses", json=expense)
    assert r.status_code == 409, r.text
    assert client.post("/api/expenses" + OVERRIDE, json=expense).status_code == 201
    # and a new number goes straight through, then counts
    assert (
        client.post("/api/expenses", json={**expense, "reference": "6100"}).status_code
        == 201
    )
    assert _entry(client, seed_accounts, "6100").status_code == 409


def test_a_payment_sent_again_with_its_key_answers_with_itself(client, seed_accounts):
    _turn_on(client)
    vendor, bill = _vendor_bill(client, seed_accounts)
    key = {"Idempotency-Key": "pay-6001-once"}
    first = _pay(client, vendor, bill, "6001", headers=key)
    assert first.status_code == 201
    again = _pay(client, vendor, bill, "6001", headers=key)
    assert again.status_code in (200, 201) and again.json()["id"] == first.json()["id"]


def test_off_nothing_is_refused(client, seed_accounts):
    # off by default: nothing to turn off
    assert _entry(client, seed_accounts, "5001").status_code == 201
    assert _entry(client, seed_accounts, "5001").status_code == 201


# ---- the forms --------------------------------------------------------------

# file -> the save functions that must ask TxnWarn.ok, and whether they edit
FORMS = {
    "invoices.js": ["TxnWarn.ok(form, { editing: !!id })"],
    "estimates.js": ["TxnWarn.ok(form, { editing: !!id })"],
    "purchase_orders.js": ["TxnWarn.ok(form, { editing: !!id })"],
    "bills.js": ["TxnWarn.ok(form, { editing: !!id })", "TxnWarn.ok(form)"],
    "banking.js": [
        "TxnWarn.ok(form, { editing: !!entryId })",
        "TxnWarn.ok(null, { date: data.date, carried })",  # the quick row (#566)
        "TxnWarn.ok(form)",
    ],
    "sales_receipts.js": ["TxnWarn.ok(form)"],
    "credit_memos.js": ["TxnWarn.ok(form)"],
    "vendor_credits.js": ["TxnWarn.ok(form)"],
    "expenses.js": ["TxnWarn.ok(form)"],
    "cc_charges.js": ["TxnWarn.ok(form)"],
    # the journal form edits too: from the journal or the register (#388)
    "journal.js": ["TxnWarn.ok(form, { editing: !!form.dataset.entryId })"],
    "payments.js": ["TxnWarn.ok(form)"],
    "batch_payments.js": ["TxnWarn.ok(form)"],
    "deposits.js": [
        "TxnWarn.ok(null, { date: $('#deposit-date').value })",
        # a deposit opened for editing from the register (#388)
        "TxnWarn.ok(form, { date: form.date.disabled ? '' : form.date.value, editing: true })",
    ],
    "bill_quick_entry.js": ["TxnWarn.ok(form)"],
    "in_kind.js": ["TxnWarn.ok(form)"],
    "job_costs.js": ["TxnWarn.ok(form)"],
    "nonprofit.js": ["TxnWarn.ok(form)"],
}


def test_every_transaction_form_asks_before_it_saves():
    missing = [
        f"{f}: {call}"
        for f, calls in FORMS.items()
        for call in calls
        if call not in _js(f)
    ]
    assert not missing


def _fn(src, head):
    body = src[src.index(head) :]
    return body[: body.index("\n    },")]


@pytest.mark.parametrize(
    "file,head,call",
    [
        ("invoices.js", "    async save(e, id) {", "API.put(`/invoices/${id}`"),
        ("bills.js", "    async save(e, id = null) {", "API.put(`/bills/${id}`"),
        ("bills.js", "    async savePay(e) {", "API.post('/bill-payments'"),
        ("expenses.js", "    async save(e) {", "API.post('/expenses'"),
        (
            "banking.js",
            "    async postQuickEntry(accountId) {",
            "API.post('/banking/transactions'",
        ),
        ("journal.js", "    async save(e) {", "API.post('/journal'"),
        ("deposits.js", "    async saveEdit(id) {", "API.put(`/deposits/${id}`"),
        ("bill_quick_entry.js", "    async save(e) {", "API.post('/bills'"),
    ],
)
def test_the_question_comes_before_the_request(file, head, call):
    body = _fn(_js(file), head)
    assert body.index("TxnWarn.ok(") < body.index(call)


@pytest.mark.parametrize(
    "file,head",
    [
        ("banking.js", "    async postQuickEntry(accountId) {"),
        ("banking.js", "    async saveEntry(e, accountId) {"),
        ("banking.js", "    async _putEntry(entryId, data, accountId) {"),
        ("expenses.js", "    async save(e) {"),
        ("bills.js", "    async savePay(e) {"),
    ],
)
def test_the_check_number_question_is_answered_where_numbers_are_typed(file, head):
    assert "CheckNumberWarn.send(" in _fn(_js(file), head)


def test_settings_draws_named_boxes_saved_as_true_or_false():
    src = _js("settings.js")
    for key in (
        "warn_date_past",
        "warn_date_future",
        "warn_duplicate_check_number",
        "warn_edit_transaction",
    ):
        assert f"SettingsPage._warnBox(s, '{key}'" in src
    assert 'input[type="checkbox"][data-bool][name]' in src
    assert "SettingsPage._warnDays(s, 'warn_date_past_days'" in src


def _imported(db, accounts, num, name, kind=None, key=None):
    """An entry QuickBooks Desktop's ledger import posted, money out of
    Checking, with QuickBooks' Num as its reference and its Type kept."""
    from datetime import date
    from decimal import Decimal

    from app.services.accounting import create_journal_entry

    create_journal_entry(
        db,
        date(2026, 9, 2),
        name,
        [
            {
                "account_id": accounts["6100"].id,
                "debit": Decimal("12.50"),
                "credit": Decimal("0"),
            },
            {
                "account_id": accounts["1000"].id,
                "debit": Decimal("0"),
                "credit": Decimal("12.50"),
            },
        ],
        source_type="qb_ledger_import",
        reference=num,
        source_kind=kind,
        source_ref=key,
    )
    db.commit()


@pytest.mark.parametrize(
    "kind, key",
    [
        ("Check", "Check 5001 · 2026-09-02 · Gas Co"),
        ("Bill Pmt -Check", "Bill Pmt -Check 5001 · 2026-09-02 · Gas Co"),
        ("Paycheque", "Paycheque 5001 · 2026-09-02 · Gas Co"),
        # no Type stored: the key says it
        (None, "Check 5001 · 2026-09-02 · Gas Co"),
    ],
)
def test_an_imported_check_is_asked_about(client, db_session, seed_accounts, kind, key):
    """A QuickBooks Desktop ledger import's check keeps its number a used
    one (#663), though the next check number does not follow it."""
    _imported(db_session, seed_accounts, "5001", "Gas Co", kind, key)
    _turn_on(client)
    r = _entry(client, seed_accounts, "5001")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["uses"] == [
        {"date": "2026-09-02", "payee": "Gas Co", "amount": "12.50"}
    ]
    assert _entry(client, seed_accounts, "5001", query=OVERRIDE).status_code == 201


@pytest.mark.parametrize(
    "kind, key",
    [
        ("General Journal", "General Journal 31 · 2026-09-02"),
        ("Transfer", "Transfer 31 · 2026-09-02"),
        (None, "General Journal 31 · 2026-09-02"),
    ],
)
def test_an_imported_journal_entrys_number_is_no_check(
    client, db_session, seed_accounts, kind, key
):
    """A payroll journal entry QuickBooks numbered 31, or an EFT's
    confirmation number, is not check 31 (#663)."""
    _imported(db_session, seed_accounts, "31", "", kind, key)
    _turn_on(client)
    assert _entry(client, seed_accounts, "31").status_code == 201
