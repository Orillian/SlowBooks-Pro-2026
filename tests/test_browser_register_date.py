"""The register's date after a post, and the date keys, in a real browser
(#566, from discussion #561). After a post the quick row and the + Entry
dialog open with the date just posted — per register, for the page's life
— and + / - step any date box a day, T is today. The #481 far-date
question is asked for a date typed, not for one carried over. The entry
row stays under a date filter, and a post from it keeps the dates (#663).
The API is stubbed; the page is the real one (tests/test_browser_ui.py's
fixtures)."""

import datetime as dt

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from tests.test_browser_register_quick_add import CHART, REGISTER  # noqa: E402
from tests.test_browser_ui import _open  # noqa: E402
from tests.test_theme_contrast import browser_fixture  # noqa: E402,F401  (the fixture)


def _register_page(browser, posted, at="#/banking/1", register=None):
    """The Checking register (#/banking/1, or the address `at`) with the
    post stubbed; `posted` collects the bodies sent. The register's GET
    answers `register`, which a test may change as it goes."""

    def _transactions(route):
        posted.append(route.request.post_data_json)
        route.fulfill(status=201, json={"id": len(posted)})

    reg = REGISTER if register is None else register
    page = _open(browser, 1280, 800, "#/")
    page.route("**/api/banking/check-register*", lambda r: r.fulfill(json=reg))
    page.route("**/api/accounts*", lambda r: r.fulfill(json=CHART))
    page.route("**/api/banking/transactions", _transactions)
    page.goto(page.url.split("#")[0] + at)
    page.wait_for_selector("#reg-start")
    return page


def _visit(page, at, ready):
    page.goto(page.url.split("#")[0] + at)
    page.wait_for_selector(ready)


def _post_quick(page, date, amount="25"):
    page.fill("#qe-date", date)
    page.select_option("#qe-category", "2")
    page.fill("#qe-out", amount)
    page.click("#reg-quick-entry button:has-text('Post')")
    page.wait_for_selector("#toast-container .toast:has-text('Entry posted')")
    # the register redraws after the post; wait for the fresh row
    page.wait_for_function("() => document.getElementById('qe-out').value === ''")


def test_the_date_sticks_after_a_post_in_the_row_and_the_dialog(browser):
    posted = []
    page = _register_page(browser, posted)
    try:
        today = dt.date.today().isoformat()
        assert page.input_value("#qe-date") == today

        _post_quick(page, "2026-10-10")
        assert posted[-1]["date"] == "2026-10-10"
        # the next row opens on the date just posted, not today
        assert page.input_value("#qe-date") == "2026-10-10"
        assert page.get_attribute("#qe-date", "data-carried") == "2026-10-10"

        # and so does + Entry
        page.click("text=+ Entry")
        page.wait_for_selector("#entry-payee")
        assert page.input_value("#modal-body [name='date']") == "2026-10-10"
        assert (
            page.get_attribute("#modal-body [name='date']", "data-carried")
            == "2026-10-10"
        )
        # a post from the dialog carries its own date back to the row
        page.fill("#modal-body [name='date']", "2026-10-12")
        page.fill("#modal-body [name='amount']", "-40")
        page.select_option("#entry-category", "2")
        page.click("#modal-body button[type='submit']")
        page.wait_for_function(
            "() => document.getElementById('qe-date')"
            " && document.getElementById('qe-date').value === '2026-10-12'"
        )
        assert posted[-1]["date"] == "2026-10-12"

        # another register starts on today: the date is one register's
        page.goto(page.url.split("#")[0] + "#/banking/4")
        page.wait_for_function(
            "() => location.hash === '#/banking/4' && document.getElementById('qe-date')"
            " && document.getElementById('qe-date').value !== '2026-10-12'"
        )
        assert page.input_value("#qe-date") == today
        assert page.get_attribute("#qe-date", "data-carried") == ""
        # and back: Checking still remembers its own
        page.goto(page.url.split("#")[0] + "#/banking/1")
        page.wait_for_function(
            "() => location.hash === '#/banking/1' && document.getElementById('qe-date')"
            " && document.getElementById('qe-date').value === '2026-10-12'"
        )
    finally:
        page.close()


def test_the_far_date_question_is_asked_once_not_for_the_carried_date(browser):
    posted = []
    page = _register_page(browser, posted)
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), d.accept()))
    try:
        page.evaluate(
            "() => { App.settings = Object.assign(App.settings || {},"
            " { warn_date_past: 'true', warn_date_past_days: '90' }); }"
        )
        # typed: asked, and accepted
        _post_quick(page, "2020-01-15")
        assert len(asked) == 1 and "in the past" in asked[0]
        assert posted[-1]["date"] == "2020-01-15"
        # carried from that post: not asked again
        _post_quick(page, "2020-01-15")
        assert len(asked) == 1
        assert posted[-1]["date"] == "2020-01-15"
        # moved a day with the key: a changed date, asked again
        page.focus("#qe-date")
        page.keyboard.press("+")
        assert page.input_value("#qe-date") == "2020-01-16"
        page.select_option("#qe-category", "2")
        page.fill("#qe-out", "5")
        page.click("#reg-quick-entry button:has-text('Post')")
        page.wait_for_function("() => document.getElementById('qe-out').value === ''")
        assert len(asked) == 2
        assert posted[-1]["date"] == "2020-01-16"
    finally:
        page.close()


def test_plus_minus_and_t_move_the_row_date(browser):
    page = _register_page(browser, [])
    try:
        today = dt.date.today().isoformat()
        page.fill("#qe-date", "2026-10-31")
        page.focus("#qe-date")
        page.keyboard.press("+")
        assert page.input_value("#qe-date") == "2026-11-01"
        page.keyboard.press("-")
        page.keyboard.press("-")
        assert page.input_value("#qe-date") == "2026-10-30"
        page.keyboard.press("h")
        assert page.input_value("#qe-date") == "2026-10-31"
        page.keyboard.press("m")
        assert page.input_value("#qe-date") == "2026-10-01"
        page.keyboard.press("t")
        assert page.input_value("#qe-date") == today
        # a text box keeps its + and -
        page.fill("#qe-memo", "")
        page.focus("#qe-memo")
        page.keyboard.type("a+b-c")
        assert page.input_value("#qe-memo") == "a+b-c"
        # the register's From filter is a date box too: the key moves it
        # and its onchange redraws the register
        page.fill("#reg-start", "2026-09-01")  # its change redraws the register
        page.wait_for_function(
            "() => location.hash.includes('start_date=2026-09-01')"
            " && document.getElementById('reg-start').value === '2026-09-01'"
        )
        page.focus("#reg-start")
        page.keyboard.press("-")
        page.wait_for_function(
            "() => location.hash.includes('start_date=2026-08-31')"
            " && document.getElementById('reg-start').value === '2026-08-31'"
        )
    finally:
        page.close()


def test_minus_still_types_a_minus_in_the_amount_boxes(browser):
    # the key handler takes only date boxes: a money box keeps its - (and +)
    page = _register_page(browser, [])
    try:
        page.focus("#qe-out")
        page.keyboard.type("-12.5")
        assert page.input_value("#qe-out") == "-12.5"
        page.click("text=+ Entry")
        page.wait_for_selector("#entry-payee")
        page.focus("#modal-body [name='amount']")
        page.keyboard.type("-40")
        assert page.input_value("#modal-body [name='amount']") == "-40"
        # and the dialog's date box did not move
        assert (
            page.input_value("#modal-body [name='date']") == dt.date.today().isoformat()
        )
    finally:
        page.close()


# ── #663: the entry row under a filter, and a post that keeps the dates ──

OCT_ENTRY = {
    "line_id": 11,
    "transaction_id": 11,
    "date": "2026-10-05",
    "description": "Fuel",
    "payee": "Shell",
    "reference": "",
    "source_type": "bank_entry",
    "source_link": None,
    "amount": -25.0,
    "debit": 0,
    "credit": 25.0,
    "payment": 25.0,
    "deposit": 0,
    "balance": -25.0,
    "cleared": False,
    "reconciliation_id": None,
    "editable": True,
    "printable": False,
    "voided": False,
    "voidable": True,
}

ROW_UNDER_ENTRIES = """() => {
    const row = document.getElementById('reg-quick-entry');
    const table = row && row.closest('table');
    return !!table && table.tBodies[0].textContent.includes('Shell');
}"""


def test_the_entry_row_stays_under_a_date_filter_not_a_class_one(browser):
    """Any From, To or class hid the entry row, so a register always worked
    from the start of the month never showed it (#663). A date filter keeps
    it, with entries in the range or none yet; a class filter puts a line
    in its place, since the row has no class box and the running balance
    counts that class only."""
    register = {**REGISTER, "entries": []}
    page = _register_page(browser, [], "#/banking/1?start_date=2026-10-01", register)
    try:
        # nothing in the range yet: said, and the row is there for the first
        assert "Nothing posted to this account for these filters" in page.inner_text(
            "#page-content"
        )
        assert page.locator("#reg-quick-entry").count() == 1

        # entries in the range: the row follows them
        register["entries"] = [OCT_ENTRY]
        _visit(
            page,
            "#/banking/1?start_date=2026-10-01&end_date=2026-10-31",
            "#reg-opening",
        )
        assert page.evaluate(ROW_UNDER_ENTRIES)

        # a class: the line says why there is no row, and All classes brings it back
        register["class_name"] = "Unassigned"
        _visit(
            page, "#/banking/1?start_date=2026-10-01&class_id=1", "#reg-quick-entry-off"
        )
        assert page.locator("#reg-quick-entry").count() == 0
        assert page.inner_text("#reg-quick-entry-off") == (
            "The entry row is hidden while a class is chosen: it has no class box,"
            " and the running balance counts this class only. Choose All classes"
            " to use it, or post with + Entry."
        )
        page.select_option("#reg-class", "")
        page.wait_for_selector("#reg-quick-entry")
        assert (
            page.evaluate("() => location.hash") == "#/banking/1?start_date=2026-10-01"
        )

        # and an empty register under a class says it in the same place
        register["entries"] = []
        _visit(page, "#/banking/1?class_id=1", "#reg-quick-entry-off")
        assert page.locator("#reg-quick-entry").count() == 0
    finally:
        page.close()


def _toast_with(page, words):
    page.locator("#toast-container .toast", has_text=words).first.wait_for()


def test_a_post_from_the_row_keeps_the_dates_and_says_when_it_falls_outside(browser):
    """The row's post went back to the register's bare address, which
    dropped the dates (#663). It keeps them; an entry dated before From or
    after To, which the list leaves out, is said so in the toast."""
    posted = []
    at = "#/banking/1?start_date=2026-10-01&end_date=2026-10-31"
    page = _register_page(browser, posted, at)
    try:
        _post_quick(page, "2026-10-12")
        assert posted[-1]["date"] == "2026-10-12"
        assert page.evaluate("() => location.hash") == at
        assert page.input_value("#reg-start") == "2026-10-01"
        assert page.input_value("#reg-end") == "2026-10-31"
        assert page.locator("#toast-container .toast", has_text="show it").count() == 0

        _post_quick(page, "2026-09-28")
        _toast_with(
            page,
            "Entry posted. Its date, Sep 28, 2026, is before the From date,"
            " so the register does not show it.",
        )
        _post_quick(page, "2026-11-02")
        _toast_with(
            page,
            "Entry posted. Its date, Nov 2, 2026, is after the To date,"
            " so the register does not show it.",
        )
        assert posted[-1]["date"] == "2026-11-02"
        assert page.evaluate("() => location.hash") == at
    finally:
        page.close()
