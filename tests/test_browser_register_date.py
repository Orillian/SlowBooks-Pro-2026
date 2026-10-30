"""The register's date after a post, and the date keys, in a real browser
(#566, from discussion #561). After a post the quick row and the + Entry
dialog open with the date just posted — per register, for the page's life
— and + / - step any date box a day, T is today. The #481 far-date
question is asked for a date typed, not for one carried over. The entry
row stays under a date filter, and a post from it keeps the dates (#663).
The API is stubbed; the page is the real one (tests/test_browser_ui.py's
fixtures)."""

import datetime as dt
from urllib.parse import parse_qs, urlsplit

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


# ── #663: a save, a void or a feed action keeps the dates and the place ──


class _Ledger:
    """A long register for the stubs to answer from: an entry a day from
    Sep 1, filtered by the address's dates and class as the service filters
    them, with the posts, edits, voids and the feed's excludes applied."""

    def __init__(self, days=60):
        start = dt.date(2026, 9, 1)
        self.entries = [
            self._entry(100 + i, (start + dt.timedelta(days=i)).isoformat())
            for i in range(days)
        ]
        self.review = []

    @staticmethod
    def _entry(txn, date, payee=None, class_id=None):
        payee = payee or f"Payee {txn}"
        return {
            **OCT_ENTRY,
            "line_id": txn,
            "transaction_id": txn,
            "date": date,
            "payee": payee,
            "description": payee,
            "class_id": class_id,
        }

    def _by_id(self, txn):
        return next(e for e in self.entries if e["transaction_id"] == txn)

    def register(self, route):
        q = parse_qs(urlsplit(route.request.url).query)
        start, end, cls = (
            q.get(k, [""])[0] for k in ("start_date", "end_date", "class_id")
        )
        rows = sorted(
            (
                e
                for e in self.entries
                if (not start or e["date"] >= start)
                and (not end or e["date"] <= end)
                and (not cls or str(e["class_id"]) == cls)
            ),
            key=lambda e: (e["date"], e["transaction_id"]),
        )
        route.fulfill(
            json={
                **REGISTER,
                "entries": rows,
                "class_name": "Site Prep" if cls else None,
            }
        )

    def transactions(self, route):
        req = route.request
        path = urlsplit(req.url).path
        if req.method == "POST" and path.endswith("/api/banking/transactions"):
            body = req.post_data_json
            txn = 900 + len(self.entries)
            self.entries.append(
                self._entry(txn, body["date"], body.get("payee"), body.get("class_id"))
            )
            return route.fulfill(status=201, json={"id": txn, "date": body["date"]})
        if req.method == "POST" and path.endswith("/exclude"):
            line = int(path.split("/")[-2])
            self.review = [t for t in self.review if t["id"] != line]
            return route.fulfill(json={"status": "excluded"})
        # the review queue, a page at a time (fetchAllPages)
        skip = int(parse_qs(urlsplit(req.url).query).get("skip", ["0"])[0])
        route.fulfill(json=[] if skip else self.review)

    def entries_route(self, route):
        req = route.request
        parts = urlsplit(req.url).path.rstrip("/").split("/")
        if parts[-1] == "void":
            e = self._by_id(int(parts[-2]))
            e.update(voided=True, voidable=False, editable=False)
            return route.fulfill(json={"id": e["transaction_id"], "status": "void"})
        e = self._by_id(int(parts[-1]))
        if req.method == "PUT":
            body = req.post_data_json
            e.update(date=body["date"], payee=body.get("payee") or e["payee"])
            return route.fulfill(json={"id": e["transaction_id"], "date": e["date"]})
        route.fulfill(
            json={
                "id": e["transaction_id"],
                "date": e["date"],
                "amount": -25.0,
                "payee": e["payee"],
                "reference": "",
                "description": "",
                "category_account_id": 2,
                "class_id": None,
                "splits": None,
                "gift_letters": [],
            }
        )


def _ledger_page(browser, ledger, at, classes=None, feed=False):
    page = _open(browser, 1280, 800, "#/")
    page.route("**/api/banking/check-register*", ledger.register)
    page.route("**/api/accounts*", lambda r: r.fulfill(json=CHART))
    page.route("**/api/banking/transactions**", ledger.transactions)
    page.route("**/api/banking/entries/**", ledger.entries_route)
    if classes is not None:
        page.route("**/api/classes*", lambda r: r.fulfill(json=classes))
    if feed:
        page.route(
            "**/api/banking/overview",
            lambda r: r.fulfill(
                json=[{"account_id": 1, "feed": {"bank_account_id": 5, "name": "Feed"}}]
            ),
        )
    page.on("dialog", lambda d: d.accept())
    page.goto(page.url.split("#")[0] + at)
    page.wait_for_selector("#page-content tr[data-txn]")
    return page


IN_VIEW = """(txn) => {
    const row = document.querySelector(`#page-content tr[data-txn="${txn}"]`);
    if (!row) return false;
    const box = document.getElementById('content').getBoundingClientRect();
    const r = row.getBoundingClientRect();
    return r.top >= box.top && r.bottom <= box.bottom;
}"""
SCROLLED = "() => document.getElementById('content').scrollTop"


def _scroll_to(page, txn):
    page.evaluate(
        '(txn) => document.querySelector(`#page-content tr[data-txn="${txn}"]`)'
        ".scrollIntoView({ block: 'center' })",
        txn,
    )
    return page.evaluate(SCROLLED)


def test_a_new_entry_keeps_the_dates_and_comes_into_view(browser):
    """+ Entry went back to the register's bare address: the dates went,
    and the page stayed at the top, the entry somewhere below (#663). The
    register comes back on its dates with the entry's row in view; one
    dated past To is said so, and the page stays where it was."""
    ledger = _Ledger()
    at = "#/banking/1?start_date=2026-09-01"
    page = _ledger_page(browser, ledger, at)
    try:
        assert page.evaluate(SCROLLED) == 0
        page.click("text=+ Entry")
        page.wait_for_selector("#entry-payee")
        page.fill("#modal-body [name='date']", "2026-10-20")
        page.fill("#modal-body [name='amount']", "-40")
        page.fill("#entry-payee", "Harbor Fuel")
        page.select_option("#entry-category", "2")
        page.click("#modal-body button[type='submit']")
        _toast_with(page, "Entry posted")
        new = ledger.entries[-1]["transaction_id"]
        page.wait_for_function(IN_VIEW, arg=new)
        assert page.evaluate(SCROLLED) > 0
        assert page.evaluate("() => location.hash") == at
        assert page.input_value("#reg-start") == "2026-09-01"

        # dated past the To date: said, and the page stays where it was
        at = "#/banking/1?start_date=2026-09-01&end_date=2026-09-30"
        _visit(page, at, "#reg-end[value='2026-09-30']")
        page.click("text=+ Entry")
        page.wait_for_selector("#entry-payee")
        before = page.evaluate(SCROLLED)
        page.evaluate(
            "() => { document.getElementById('reg-start').dataset.drawn = 'before'; }"
        )
        page.fill("#modal-body [name='date']", "2026-10-15")
        page.fill("#modal-body [name='amount']", "-12")
        page.select_option("#entry-category", "2")
        page.click("#modal-body button[type='submit']")
        _toast_with(
            page,
            "Entry posted. Its date, Oct 15, 2026, is after the To date,"
            " so the register does not show it.",
        )
        # the register drawn again, on the same dates and at the same place
        page.wait_for_function(
            "() => !document.getElementById('reg-start').dataset.drawn"
        )
        assert page.evaluate("() => location.hash") == at
        assert abs(page.evaluate(SCROLLED) - before) <= 1
    finally:
        page.close()


def test_an_edit_keeps_the_dates_and_brings_the_entry_into_view(browser):
    """An edit's Save went back to the bare address too (#663). The
    register keeps its dates, and the entry, moved to an earlier day, is
    brought into view where it now sits."""
    ledger = _Ledger()
    at = "#/banking/1?start_date=2026-09-01&end_date=2026-10-31"
    page = _ledger_page(browser, ledger, at)
    try:
        assert _scroll_to(page, 150) > 0
        page.locator('tr[data-txn="150"] button', has_text="Edit").click()
        page.wait_for_function(
            "() => document.getElementById('modal-title').textContent === 'Edit entry'"
        )
        page.fill("#modal-body [name='date']", "2026-09-03")
        page.click("#modal-body button[type='submit']")
        _toast_with(page, "Entry saved")
        page.wait_for_function(
            "() => (document.querySelector('tr[data-txn=\"150\"]') || {}).textContent"
            " && document.querySelector('tr[data-txn=\"150\"]').textContent.includes('Sep 3, 2026')"
        )
        page.wait_for_function(IN_VIEW, arg=150)
        assert page.evaluate("() => location.hash") == at
    finally:
        page.close()


def test_a_void_keeps_the_dates_and_the_place_on_the_page(browser):
    ledger = _Ledger()
    at = "#/banking/1?start_date=2026-09-01"
    page = _ledger_page(browser, ledger, at)
    try:
        top = _scroll_to(page, 140)
        assert top > 0
        page.locator('tr[data-txn="140"] button', has_text="Void").click()
        _toast_with(page, "Voided")
        page.wait_for_function(
            "() => !document.querySelector('tr[data-txn=\"140\"] button[data-destructive]')"
        )
        assert page.evaluate("() => location.hash") == at
        assert abs(page.evaluate(SCROLLED) - top) <= 1
    finally:
        page.close()


def test_a_feed_action_keeps_the_dates_and_the_place_on_the_page(browser):
    """The feed's Add, Payout, Exclude, Match and the rest redrew the
    register at its bare address (#663): they keep its dates, and the page
    stays where it was."""
    ledger = _Ledger()
    ledger.review = [
        {
            "id": 70 + i,
            "date": "2026-10-01",
            "payee": f"Statement line {i}",
            "description": "",
            "amount": -5.0,
        }
        for i in range(20)
    ]
    at = "#/banking/1?start_date=2026-09-15"
    page = _ledger_page(browser, ledger, at, feed=True)
    try:
        page.wait_for_selector("text=Statement line 12")
        row = page.locator("tr", has_text="Statement line 12")
        top = row.evaluate(
            "(tr) => { tr.scrollIntoView({ block: 'center' });"
            " return document.getElementById('content').scrollTop; }"
        )
        assert top > 0
        row.locator("button", has_text="Exclude").click()
        page.wait_for_function(
            "() => !document.body.textContent.includes('Statement line 12')"
        )
        assert page.evaluate("() => location.hash") == at
        assert abs(page.evaluate(SCROLLED) - top) <= 1
    finally:
        page.close()


def test_an_entry_the_class_leaves_out_is_said_so(browser):
    """Kept on the register's class, an entry posted with another class
    leaves the list: the toast says so (#663)."""
    ledger = _Ledger(days=3)
    ledger.entries[0]["class_id"] = 2
    classes = [
        {"id": 1, "name": "Unassigned", "is_system_default": True},
        {"id": 2, "name": "Site Prep"},
    ]
    at = "#/banking/1?class_id=2"
    page = _ledger_page(browser, ledger, at, classes=classes)
    try:
        page.click("text=+ Entry")
        page.wait_for_selector("#entry-payee")
        page.fill("#modal-body [name='date']", "2026-09-02")
        page.fill("#modal-body [name='amount']", "-9")
        page.select_option("#entry-category", "2")
        page.click("#modal-body button[type='submit']")
        _toast_with(
            page,
            "This entry is not in class Site Prep, so the register does not show it.",
        )
        assert page.evaluate("() => location.hash") == at
    finally:
        page.close()


# ── #663: the register opens on the last From date it used ──────────────

ON_REGISTER = """([n, start]) => typeof BankingPage !== 'undefined' && BankingPage._ctx
    && BankingPage._ctx.accountId === n && document.getElementById('reg-start')
    && document.getElementById('reg-start').value === start"""


def _open_register(page, n, start):
    page.goto(page.url.split("#")[0] + f"#/banking/{n}")
    page.wait_for_function(ON_REGISTER, arg=[n, start])


def test_the_register_opens_on_the_from_date_it_last_used(browser):
    """The filter lived only in the address, so every visit opened on the
    whole history (#663). A register opened with no dates in its address
    opens on the From date it was last read from, in its From box and its
    address, after a reload too; each account and company keeps its own,
    and Clear forgets it."""
    page = _register_page(browser, [])
    try:
        assert page.input_value("#reg-start") == ""
        page.fill("#reg-start", "2026-10-01")  # its change redraws the register
        page.wait_for_function(
            "() => location.hash === '#/banking/1?start_date=2026-10-01'"
        )

        # another register has none; this one, opened by its bare address
        # (a card on Banking, the chart, a link), opens on the date
        _open_register(page, 4, "")
        assert page.evaluate("() => location.hash") == "#/banking/4"
        _open_register(page, 1, "2026-10-01")
        assert (
            page.evaluate("() => location.hash") == "#/banking/1?start_date=2026-10-01"
        )
        assert "from Oct 1, 2026" in page.inner_text("#reg-filter-note")

        # a new page load keeps it
        page.goto(page.url.split("#")[0] + "#/")
        page.reload()
        page.wait_for_function("window.App && document.readyState === 'complete'")
        page.evaluate(
            "() => { const s = document.getElementById('splash'); if (s) s.classList.add('hidden'); }"
        )
        _open_register(page, 1, "2026-10-01")

        # another company's register 1 is not this one
        page.evaluate("() => { App._tabsOwner = '|Another Company'; }")
        _open_register(page, 4, "")
        _open_register(page, 1, "")
        page.evaluate("() => { App._tabsOwner = '|Harbor Light Bakery Two'; }")
        _open_register(page, 4, "")
        _open_register(page, 1, "2026-10-01")

        # Clear forgets it
        page.get_by_role("button", name="Clear").click()
        page.wait_for_function("() => location.hash === '#/banking/1'")
        _open_register(page, 4, "")
        _open_register(page, 1, "")
        assert page.evaluate("() => location.hash") == "#/banking/1"
    finally:
        page.close()
