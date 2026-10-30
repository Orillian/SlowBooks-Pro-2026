# ============================================================================
# QuickBooks Desktop's Transaction Detail by Account, as journal entries
# (2.24.0).
#
# The report QuickBooks Desktop gives an accountant for "everything": every
# transaction of every type (deposits, checks, transfers, general journal
# entries, opening balances), grouped by account. Each account is a heading
# row, its sub-accounts indented one column under it; each transaction shows
# once in every account it touches, with the other side named in Split
# ("-SPLIT-" when there are several); every section ends with QuickBooks'
# own "Total <account>" row, and the report with a TOTAL row. The columns
# are interleaved with empty ones, and the Clr column carries QuickBooks'
# check glyph in the Windows code page.
#
# Read as it comes, each row is one line on its section's account; the rows
# of one transaction (its type, date and number, across every section) make
# one balanced journal entry. Same-day transactions with no number that
# share a key are kept apart by their name when each name balances on its
# own. Before anything is written the whole file is checked: every account
# in the chart (the lists IIF first), every transaction balanced, every
# section's lines adding up to the total QuickBooks printed for it. A file
# that passes imports. One that does not posts nothing on its first upload
# and answers with its problems instead (#663): each transaction that does
# not balance, each account not in the chart, and a notice when the file
# looks cut short or edited. The page lists them for the person to accept
# or leave out and sends the same file again with the answers: what checks
# out imports with what was accepted (an accepted transaction's difference
# on Import Differences, an accepted account made with the type chosen),
# and the rest is left out and counted. A second upload posts nothing twice.
#
# Built against a real company's report (a year of a small practice's books:
# about 5,700 rows, every transaction balanced, the section totals matched);
# the fixture in tests/fixtures/qb_desktop/ is invented books in that layout.
# Nothing of the real file is here.
# ============================================================================

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.accounts import (
    BASE_TYPE_OF,
    DETAIL_TYPES,
    Account,
    AccountType,
    DetailType,
)
from app.models.transactions import Transaction
from app.services.account_label import account_label, label_for
from app.services.accounting import create_journal_entry, ensure_account
from app.services.safe_errors import DataProblem
from app.services.settings_service import use_account_numbers

SOURCE_TYPE = "qb_ledger_import"
# QuickBooks' Type for a journal entry on this report
GENERAL_JOURNAL_TYPES = frozenset({"General Journal", "Journal", "Journal Entry"})
LEDGER_COLUMNS = ("Type", "Date", "Num", "Name", "Memo", "Split", "Debit", "Credit")
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")
_HOW = (
    "Export it from QuickBooks Desktop as Reports > Accountant & Taxes > "
    "Transaction Detail by Account, with the dates set to All, and save it as CSV."
)


def is_ledger_header(cols: dict | None) -> bool:
    """The account-grouped layout: the ledger's columns and no Account
    column (the account is each section's heading)."""
    return (
        bool(cols) and all(c in cols for c in LEDGER_COLUMNS) and "Account" not in cols
    )


def has_account_sections(rows: list[list[str]], header_at: int, cols: dict) -> bool:
    """Recognize ledger grouping even when an added Account column masks it.

    The section headings sit left of the report's own columns, so they are
    looked for left of the first of those; the added Account column is left
    out of that reach and of the heading read, wherever it was inserted (as
    column A ahead of the headings, 2.24.1 gate W-27, or anywhere else)."""
    heading = None
    own = [v for k, v in cols.items() if k != "Account"]
    first = min(own) if own else 0
    skip = cols.get("Account")
    for row in rows[header_at + 1 :]:
        ttype = row[cols["Type"]].strip() if cols["Type"] < len(row) else ""
        if not ttype:
            label = next(
                (
                    c.strip()
                    for i, c in enumerate(row[:first])
                    if i != skip and c.strip()
                ),
                "",
            )
            if label:
                heading = (
                    None if label.startswith("Total ") or label == "TOTAL" else label
                )
            continue
        account = row[cols["Account"]].strip() if cols["Account"] < len(row) else ""
        # Sub-account sections carry a leaf name; Account may carry the full path.
        if (
            heading
            and account
            and heading.casefold()
            in (
                account.casefold(),
                account.rsplit(":", 1)[-1].strip().casefold(),
            )
        ):
            return True
    return False


def _money(raw: str) -> Decimal:
    raw = (raw or "").replace(",", "").replace("$", "").strip()
    if not raw:
        return Decimal("0")
    negative = raw.startswith("(") and raw.endswith(")")
    value = Decimal(raw.strip("()"))
    return -value if negative else value


def _date(raw: str):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unreadable date {raw!r}")


def parse_ledger(rows: list[list[str]], header_at: int, cols: dict):
    """(lines, errors, notes, cut): each transaction row as {path, type,
    date, num, name, memo, debit, credit, row}, its account the path of the
    headings above it ("Parent:Child"). `errors` refuse the file (rows with
    no account above them); `cut` says where it looks cut short or edited:
    the sections whose lines do not add up to QuickBooks' total, the rows
    that could not be read, and the section it ends in, before that
    section's Total line."""
    first = cols["Type"]  # the label columns are the ones before Type
    lines: list[dict] = []
    errors: list[str] = []
    notes: list[str] = []
    cut: dict = {"sections": [], "unreadable": [], "ends_in": None}
    totals = 0
    # each open section: (depth, name, [debit, credit] of the lines under it
    # so far, its sub-accounts' included)
    stack: list[tuple[int, str, list[Decimal]]] = []

    def cell(row, name):
        i = cols.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        ttype = cell(row, "Type")
        if not ttype:
            label = next(
                (
                    (j, row[j].strip())
                    for j in range(min(first, len(row)))
                    if row[j].strip()
                ),
                None,
            )
            if label is None:
                continue
            depth, text = label
            if text.upper() == "TOTAL":
                continue  # the report's grand total
            if text.startswith("Total "):
                while stack and stack[-1][0] > depth:
                    stack.pop()
                if stack and stack[-1][0] == depth:
                    # QuickBooks' own total: the section's lines (and its
                    # sub-accounts') add up to it, or the file is not what
                    # it says. Checked against the section being closed, so
                    # an account the report lists as two sections has each
                    # total measured against its own lines.
                    path = ":".join(name for _, name, _ in stack)
                    got_d, got_c = stack[-1][2]
                    try:
                        debit = _money(cell(row, "Debit"))
                        credit = _money(cell(row, "Credit"))
                    except Exception:
                        notes.append(
                            f"Row {n}: the total for {text[6:]} could not be read"
                        )
                    else:
                        if (got_d, got_c) != (debit, credit):
                            cut["sections"].append(
                                {
                                    "path": path,
                                    "debit": got_d,
                                    "credit": got_c,
                                    "total_debit": debit,
                                    "total_credit": credit,
                                }
                            )
                    totals += 1
                    stack.pop()
                continue
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, text, [Decimal("0"), Decimal("0")]))
            continue
        if not stack:
            errors.append(
                "This report lists its transactions without the account each one "
                "belongs to above it, so they cannot be posted. " + _HOW
            )
            return [], errors, notes, {}
        try:
            debit, credit = _money(cell(row, "Debit")), _money(cell(row, "Credit"))
            day = _date(cell(row, "Date"))
        except Exception:
            # the cells as they read, never the exception's own words
            cut["unreadable"].append(
                f"Row {n}: the date ({cell(row, 'Date')!r}) or an amount "
                f"({cell(row, 'Debit')!r} / {cell(row, 'Credit')!r}) could not be read"
            )
            continue
        if debit == 0 and credit == 0:
            continue
        for _, _, sums in stack:
            sums[0] += debit
            sums[1] += credit
        lines.append(
            {
                "path": ":".join(name for _, name, _ in stack),
                "type": ttype,
                "date": day,
                "num": cell(row, "Num"),
                "name": cell(row, "Name"),
                "memo": cell(row, "Memo"),
                "debit": debit,
                "credit": credit,
                "row": n,
            }
        )
    if stack and totals:
        # QuickBooks closes every section with its Total line; a file that
        # stops inside one was cut there (Save as CSV stops at 32,768 rows)
        cut["ends_in"] = ":".join(name for _, name, _ in stack)
    if not (cut["sections"] or cut["unreadable"] or cut["ends_in"]):
        cut = {}
    return lines, errors, notes, cut


def _off(group) -> Decimal:
    return sum((ln["debit"] - ln["credit"] for ln in group), Decimal("0"))


def group_journals(lines: list[dict]) -> tuple[list[list[dict]], list[str]]:
    """The lines as balanced journals: one per transaction (type, date,
    number), split by name when that is what tells same-key transactions
    apart. QuickBooks can print a journal entry's number on one of its lines
    and not the other: two halves of the same type and day that balance
    each other are one entry. A transaction that still does not balance is
    named."""
    journals, unbalanced = group_transactions(lines)
    return journals, [_unbalanced_words(group) for group in unbalanced]


def _unbalanced_words(group: list[dict]) -> str:
    first = group[0]
    label = f"{first['type']} {first['num']}".strip()
    return (
        f"{label} of {first['date']:%m/%d/%Y} does not balance ({_off(group):,.2f} "
        f"over {len(group)} lines, rows {', '.join(str(ln['row']) for ln in group[:6])})"
    )


def group_transactions(
    lines: list[dict],
) -> tuple[list[list[dict]], list[list[dict]]]:
    """(journals, unbalanced): the balanced journals group_journals makes,
    and the lines of each transaction that still does not balance."""
    by_key: OrderedDict = OrderedDict()
    for ln in lines:
        by_key.setdefault((ln["type"], ln["date"], ln["num"]), []).append(ln)

    journals: list[list[dict]] = []
    loose: list[list[dict]] = []
    for group in by_key.values():
        by_name: OrderedDict = OrderedDict()
        for ln in group:
            by_name.setdefault(ln["name"], []).append(ln)
        if len(by_name) > 1 and all(_off(part) == 0 for part in by_name.values()):
            journals.extend(by_name.values())
        elif _off(group) == 0:
            journals.append(group)
        else:
            loose.append(group)
    unbalanced: list[list[dict]] = []
    journals.extend(_combine_split_numbers(loose, _off))
    while loose:
        group = loose.pop(0)
        first = group[0]
        mate = next(
            (
                other
                for other in loose
                if other[0]["type"] == first["type"]
                and other[0]["date"] == first["date"]
                and _off(other) == -_off(group)
                and (not other[0]["num"] or not first["num"])
            ),
            None,
        )
        if mate is not None:
            loose.remove(mate)
            numbered = group if first["num"] else mate
            journals.append(numbered + (mate if numbered is group else group))
            continue
        unbalanced.append(group)
    journals.sort(key=lambda j: (j[0]["date"], j[0]["row"]))
    return journals, unbalanced


# How many same-day numbered halves one unnumbered half is tried against.
# A deposit of a dozen customer checks is common; past this the search
# (every subset) would grow too large, and the halves are named instead.
_SPLIT_SEARCH_LIMIT = 12


def _combine_split_numbers(loose: list[list[dict]], off) -> list[list[dict]]:
    """One transaction QuickBooks printed under several numbers: a bank
    deposit of two customer checks shows its bank line with no number and
    each income line with that check's own number, so grouping by number
    splits it into an unnumbered half and two numbered halves that only
    balance together (a real 2013 deposit of 182.86 = 137.82 + 45.04, on
    discussion #349). An unnumbered half and the same-day, same-type
    numbered halves that add up to it are one journal. Removes what it joins
    from ``loose`` and returns the joined journals."""
    from itertools import combinations

    joined: list[list[dict]] = []
    for group in [g for g in loose if not g[0]["num"]]:
        if group not in loose:
            continue
        first = group[0]
        target = -off(group)
        candidates = [
            other
            for other in loose
            if other is not group
            and other[0]["num"]
            and other[0]["type"] == first["type"]
            and other[0]["date"] == first["date"]
        ]
        if len(candidates) < 2 or len(candidates) > _SPLIT_SEARCH_LIMIT:
            continue
        match = None
        for size in range(2, len(candidates) + 1):
            match = next(
                (
                    combo
                    for combo in combinations(candidates, size)
                    if sum((off(part) for part in combo), Decimal("0")) == target
                ),
                None,
            )
            if match:
                break
        if not match:
            continue
        journal = [ln for part in match for ln in part] + group
        for part in match:
            loose.remove(part)
        loose.remove(group)
        joined.append(journal)
    return joined


def _import_key(journal: list[dict], seen: dict) -> str:
    """The journal's key, what a second upload checks: type, number, date
    and its one name, numbered when a key repeats in the file. It was the
    reference until #663 and kept its form, so a file imported before still
    matches."""
    first = journal[0]
    base = f"{first['type']} {first['num']}".strip() + f" · {first['date']:%Y-%m-%d}"
    names = {ln["name"] for ln in journal if ln["name"]}
    if len(names) == 1:
        base += f" · {next(iter(names))}"
    seen[base] = seen.get(base, 0) + 1
    if seen[base] > 1:
        base += f" #{seen[base]}"
    return base[:100]


OTHER_SECTION_SUFFIX = " - Other"


def _other_section_target(path: str) -> str | None:
    """The path of the account a "<Parent> - Other" section belongs to, or
    None for any other section."""
    segments = [part.strip() for part in path.split(":") if part.strip()]
    if not segments or not segments[-1].endswith(OTHER_SECTION_SUFFIX):
        return None
    leaf = segments[-1][: -len(OTHER_SECTION_SUFFIX)].strip()
    if not leaf:
        return None
    parents = segments[:-1]
    if parents and parents[-1].lower() == leaf.lower():
        return ":".join(parents)
    return ":".join(parents + [leaf])


def _parent_of_other_section(db: Session, path: str):
    """The account a "<Parent> - Other" section belongs to. When a parent
    account has sub-accounts, QuickBooks reports the parent's own postings
    in a section it names "<Parent> - Other", though no such account
    exists in its chart (a 16-year export on discussion #349 was refused
    as "accounts not in the chart" for every one of them). Those lines
    belong to the parent itself. The section sits under its parent in the
    report ("Parent:Parent - Other"), or stands by its name alone when the
    export carries one label column."""
    from app.services.iif_import import _find_account

    target = _other_section_target(path)
    return _find_account(db, target) if target else None


# The account types an account missing from the chart can be made with
# (#663), QuickBooks' own as the chart's form offers them: Accounts
# Receivable and Payable belong to 1100 and 2000 alone.
ACCOUNT_TYPES = tuple(
    d
    for d in DETAIL_TYPES
    if d not in (DetailType.ACCOUNTS_RECEIVABLE, DetailType.ACCOUNTS_PAYABLE)
)
_BANK_KINDS = {DetailType.BANK: "bank", DetailType.CREDIT_CARD: "credit_card"}

# Where an accepted transaction that does not balance in the file puts its
# difference (#663), made on first use. Equity, as 3900 Opening Balance
# Equity holds what bringing books across leaves over: the balance stays on
# the balance sheet until someone clears it, where an expense account would
# close it into Retained Earnings at the year end. 3950 sits beside 3900 and
# is no control account, no seeded one and none another feature makes.
DIFFERENCES_NUMBER = "3950"
DIFFERENCES_NAME = "Import Differences"
DIFFERENCE_MEMO = "Difference in the QuickBooks file"
_UNREAD_ANSWERS = "The answers to the file's problems could not be read."


def read_answers(raw) -> dict | None:
    """The person's answers to a file's problems, as the page sends them
    (#663): {"unbalanced": [import key, ...], "accounts": {path: account
    type}, "cut_off": true}. None when none were sent (a first upload).
    A problem the answers do not accept is left out."""
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise DataProblem(_UNREAD_ANSWERS)
    keys = data.get("unbalanced") or []
    chosen = data.get("accounts") or {}
    if (
        not isinstance(keys, list)
        or not all(isinstance(k, str) for k in keys)
        or not isinstance(chosen, dict)
    ):
        raise DataProblem(_UNREAD_ANSWERS)
    offered = {d.value: d for d in ACCOUNT_TYPES}
    types = {}
    for path, value in chosen.items():
        if not isinstance(value, str) or value not in offered:
            raise DataProblem(
                f"The type chosen for {path} ({value}) is not one the import can make."
            )
        types[str(path)] = offered[value]
    return {
        "unbalanced": set(keys),
        "accounts": types,
        "cut_off": data.get("cut_off") is True,
    }


def _find_differences(db: Session):
    """3950 Import Differences by its number first, as Opening Balance
    Equity is found, so one renamed is still the one."""
    return (
        db.query(Account)
        .filter(
            Account.account_number == DIFFERENCES_NUMBER,
            Account.account_type == AccountType.EQUITY,
        )
        .first()
    ) or db.query(Account).filter(Account.name == DIFFERENCES_NAME).first()


def _differences_label(db: Session) -> str:
    found = _find_differences(db)
    if found is not None:
        return label_for(found, db)
    return account_label(
        DIFFERENCES_NUMBER, DIFFERENCES_NAME, use_account_numbers(db), " "
    )


def _make_account(db: Session, path: str, detail: DetailType) -> Account:
    """The account at `path`, made with the type the person chose, with any
    parent the chart lacks made the same type (in QuickBooks a sub-account
    shares its parent's)."""
    from app.services.iif_import import _account_at_path

    segments = [part.strip() for part in path.split(":") if part.strip()]
    parent = None
    for depth in range(1, len(segments) + 1):
        acct = _account_at_path(db, segments[:depth])
        if acct is None:
            acct = Account(
                name=segments[depth - 1][:200],
                account_type=BASE_TYPE_OF[detail],
                detail_type=detail,
                bank_kind=_BANK_KINDS.get(detail),
                parent_id=parent.id if parent is not None else None,
                balance=Decimal("0"),
            )
            db.add(acct)
            db.flush()
        parent = acct
    return parent


def _cut_off_problem(cut: dict) -> dict:
    details = [
        f"The lines under {s['path']} come to {s['debit']:,.2f} debit and "
        f"{s['credit']:,.2f} credit, and QuickBooks' total says "
        f"{s['total_debit']:,.2f} and {s['total_credit']:,.2f}."
        for s in cut["sections"]
    ]
    if cut["ends_in"]:
        details.append(
            f"The file ends inside {cut['ends_in']}, before QuickBooks' Total "
            "line for it."
        )
    details += cut["unreadable"]
    return {
        "kind": "cut_off",
        "key": "file",
        "details": details,
        "sections": [
            {k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in s.items()}
            for s in cut["sections"]
        ],
        "text": " ".join(details),
    }


def _unbalanced_problem(txn: dict) -> dict:
    journal = txn["lines"]
    first = journal[0]
    names = [ln["name"] for ln in journal if ln["name"]]
    debit = sum((ln["debit"] for ln in journal), Decimal("0"))
    credit = sum((ln["credit"] for ln in journal), Decimal("0"))
    return {
        "kind": "unbalanced",
        "key": txn["key"],
        "date": first["date"].isoformat(),
        "type": first["type"],
        "num": first["num"],
        "name": names[0] if names else "",
        "amount": f"{max(debit, credit):.2f}",
        "off": f"{debit - credit:.2f}",
        "rows": [ln["row"] for ln in journal],
        "text": _unbalanced_words(journal),
    }


def _problems(pending: list[dict], cut: dict, missing: dict) -> list[dict]:
    """What the person answers before anything posts, among the
    transactions not in the books already: a notice when the file looks
    cut short or edited, each account not in the chart with how many
    transactions touch it, and each transaction that does not balance,
    every one however many (discussion #349)."""
    if not pending:
        return []
    out = [_cut_off_problem(cut)] if cut else []
    for target in OrderedDict.fromkeys(missing.values()):
        n = sum(1 for t in pending if target in t["needs"])
        if not n:
            continue
        touching = f"{n} transaction{'s' if n != 1 else ''}"
        out.append(
            {
                "kind": "missing_account",
                "key": target,
                "path": target,
                "transactions": n,
                "text": f"{target} is not in the chart ({touching})",
            }
        )
    out += [_unbalanced_problem(t) for t in pending if not t["balanced"]]
    return out


def _problem_notes(db: Session, problems: list[dict]) -> dict:
    """Each kind's sentences for the page: what is likely wrong, and what a
    tick does."""
    kinds = {p["kind"] for p in problems}
    notes = {}
    if "cut_off" in kinds:
        notes["cut_off"] = (
            "The file looks cut short or edited. QuickBooks' Save as CSV stops a "
            "report at 32,768 rows without a warning, so export it again for a "
            "shorter date range: one year per file, or a year in two files split "
            "at a month end, imported oldest first. Ticked, the import takes what "
            "is there; unticked, nothing is imported."
        )
    if "missing_account" in kinds:
        notes["missing_account"] = (
            "These accounts are in the report and not in the chart. Import the "
            "lists first (in QuickBooks: File > Utilities > Export > Lists to IIF "
            "Files), then this report; or choose a type and tick an account to "
            "make it now. A transaction that touches an account left unticked is "
            "left out."
        )
    if "unbalanced" in kinds:
        notes["unbalanced"] = (
            "These transactions do not balance in the file. A report filtered to "
            "some accounts leaves their other side out: set Customize Report > "
            "Filters > Account to All accounts. Ticked, one posts with its "
            f"difference on {_differences_label(db)}, an equity account, for you "
            "or your accountant to clear. Unticked, it is left out, and a "
            "corrected file brings it in later."
        )
    return notes


def import_ledger_report(
    db: Session,
    rows: list[list[str]],
    header_at: int,
    cols: dict,
    adjusting_only: bool = False,
    posted_by: str = None,
    answers: dict | None = None,
) -> dict:
    """``adjusting_only`` (#344): the report is an accountant's adjustments
    — General Journal rows only, refused whole when it holds anything
    else, so a whole-ledger upload can never double the books — and what
    posts is marked adjusting (#341), in ``posted_by``'s name.

    ``answers`` (#663, read_answers): None on a first upload, which posts
    nothing when the file has problems and answers with ``ready``,
    ``problems`` and ``problem_notes``. Given, what checks out posts with
    what they accept, and ``accepted`` and ``left_out`` say the rest."""
    from app.services.iif_import import _find_account

    result = {
        "journals": 0,
        "duplicates_skipped": 0,
        "errors": [],
        "warnings": [],
        "adjusting": bool(adjusting_only),
    }
    lines, errors, notes, cut = parse_ledger(rows, header_at, cols)
    result["warnings"].extend(notes)
    if errors:
        result["errors"] = errors
        return result
    if not lines:
        # rows that could not be read say so, before "no transactions"
        result["errors"] = (cut or {}).get("unreadable") or [
            "The report has no transactions in it. " + _HOW
        ]
        return result
    if adjusting_only:
        others: dict[str, int] = {}
        for ln in lines:
            if ln["type"] not in GENERAL_JOURNAL_TYPES:
                others[ln["type"]] = others.get(ln["type"], 0) + 1
        if others:
            named = ", ".join(
                f"{n} {t} row{'s' if n != 1 else ''}" for t, n in others.items()
            )
            result["errors"].append(
                "The adjusting-entries import takes General Journal rows only, and "
                f"this report also holds {named}. Nothing was posted. In QuickBooks, "
                "filter the report to Transaction Type = Journal (Customize Report > "
                "Filters) and export it again, or untick the box to import the whole "
                "report as ordinary journal entries."
            )
            return result
    accounts = {}
    missing = {}  # a section's path: the path of the account it needs
    for path in OrderedDict.fromkeys(ln["path"] for ln in lines):
        acct = _find_account(db, path)
        if acct is None:
            acct = _parent_of_other_section(db, path)
        if acct is None:
            missing[path] = _other_section_target(path) or path
        accounts[path] = acct
    journals, unbalanced = group_transactions(lines)
    # each entry's key: its source_ref, else (an entry the 2.31.2 upgrade
    # could not read back) the reference that held it before
    have = {
        key or ref
        for key, ref in db.query(Transaction.source_ref, Transaction.reference).filter(
            Transaction.source_type == SOURCE_TYPE
        )
    }
    # the balanced ones are keyed first, as before #663, so their keys are
    # the ones an earlier upload of them made whatever else this file holds
    seen: dict = {}
    txns = [
        {"lines": j, "key": _import_key(j, seen), "balanced": True} for j in journals
    ]
    txns += [
        {"lines": g, "key": _import_key(g, seen), "balanced": False} for g in unbalanced
    ]
    txns.sort(key=lambda t: (t["lines"][0]["date"], t["lines"][0]["row"]))
    for t in txns:
        t["needs"] = {missing[ln["path"]] for ln in t["lines"] if ln["path"] in missing}
    pending = [t for t in txns if t["key"] not in have]
    problems = _problems(pending, cut, missing)
    if cut and not pending:
        result["warnings"].append(_cut_off_problem(cut)["text"])
    if problems and answers is None:
        # nothing posts before the person has seen them
        result["duplicates_skipped"] = len(txns) - len(pending)
        result["ready"] = sum(1 for t in pending if t["balanced"] and not t["needs"])
        result["problems"] = problems
        result["problem_notes"] = _problem_notes(db, problems)
        if any(p["kind"] == "missing_account" for p in problems):
            result["account_types"] = [
                {
                    "value": d.value,
                    "label": DETAIL_TYPES[d][0],
                    "account_type": BASE_TYPE_OF[d].value,
                }
                for d in ACCOUNT_TYPES
            ]
        return result
    answers = answers or {"unbalanced": set(), "accounts": {}, "cut_off": False}
    if cut and problems and not answers["cut_off"]:
        result["duplicates_skipped"] = len(txns) - len(pending)
        result["errors"].append(
            "Nothing was imported: the file looks cut short or edited, and "
            "importing what is there was not chosen. Export it again for a "
            "shorter date range and import that."
        )
        return result
    made = {}
    for target in OrderedDict.fromkeys(missing.values()):
        detail = answers["accounts"].get(target)
        if detail is not None and any(target in t["needs"] for t in pending):
            made[target] = _make_account(db, target, detail)
    for path, target in missing.items():
        if target in made:
            accounts[path] = made[target]
    left_out = {"unbalanced": 0, "missing_account": 0}
    accepted = 0
    differences = None
    touches_control = 0
    from app.services.accounting import get_ap_account_id, get_ar_account_id

    try:
        control = {get_ar_account_id(db), get_ap_account_id(db)}
    except Exception:
        control = set()
    for t in txns:
        key, journal = t["key"], t["lines"]
        if key in have:
            result["duplicates_skipped"] += 1
            continue
        # counted once, under the first reason it is left out
        if t["needs"] - made.keys():
            left_out["missing_account"] += 1
            continue
        if not t["balanced"] and key not in answers["unbalanced"]:
            left_out["unbalanced"] += 1
            continue
        first = journal[0]
        names = [ln["name"] for ln in journal if ln["name"]]
        posted = [
            {
                "account_id": accounts[ln["path"]].id,
                "debit": ln["debit"],
                "credit": ln["credit"],
                "description": (ln["memo"] or ln["name"] or first["type"])[:300],
            }
            for ln in journal
        ]
        if not t["balanced"]:
            if differences is None:
                differences = _find_differences(db) or ensure_account(
                    db, DIFFERENCES_NUMBER, DIFFERENCES_NAME, AccountType.EQUITY
                )
            off = _off(journal)
            posted.append(
                {
                    "account_id": differences.id,
                    "debit": -off if off < 0 else Decimal("0"),
                    "credit": off if off > 0 else Decimal("0"),
                    "description": DIFFERENCE_MEMO,
                }
            )
            accepted += 1
        if any(p["account_id"] in control for p in posted):
            touches_control += 1
        # as a register entry reads (#663): QuickBooks' Name is the payee
        # (none on an entry with no name), its Num the reference, each
        # line's Memo on the line, its Type the register's Type
        create_journal_entry(
            db,
            first["date"],
            (names[0] if names else "")[:200],
            posted,
            source_type=SOURCE_TYPE,
            reference=first["num"][:100] or None,
            is_adjusting=bool(adjusting_only),
            posted_by=posted_by,
            source_ref=key,
            source_kind=first["type"][:50],
        )
        have.add(key)
        result["journals"] += 1
    if touches_control:
        result["warnings"].append(
            f"{touches_control} of the journals post to Accounts Receivable or "
            "Accounts Payable. The A/R and A/P aging read invoices and bills, so "
            "enter the ones still open as documents after the import."
        )
    if problems:
        result["accepted"] = {
            "unbalanced": accepted,
            "accounts": list(made),
            "cut_off": bool(cut),
        }
        result["left_out"] = left_out
        if differences is not None:
            result["differences_account"] = label_for(differences, db)
    db.commit()
    return result
