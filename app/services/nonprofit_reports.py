"""Nonprofit statements — the four reports a treasurer and an auditor ask
for, computed from the same posted lines as the P&L and balance sheet so
they always reconcile to them.

    Statement of Financial Position   balance sheet with equity shown as
                                      net assets without / with donor
                                      restrictions
    Statement of Activities           P&L in two columns by restriction,
                                      with releases between them; the
                                      total change equals P&L net income
    Fund balances                     per restricted fund: beginning,
                                      contributions, spending, releases,
                                      ending, still-unreleased
    Statement of Functional Expenses  natural expense accounts by
                                      program / management / fundraising
                                      (Form 990 Part IX), plus the
                                      program-by-program breakout

Net assets without a year-end close
    Nothing closes income and expense to equity in this ledger (the
    balance sheet adds a synthetic "Net Income" equity row instead), so
    the net-asset split is done the same way at report time: income
    attributed to a restricted fund counts as "with donor restrictions";
    every other income line and ALL expenses count as "without" (ASU
    2016-14: expenses are always reported without restrictions — a
    restricted fund's spending is the evidence for a release, not a
    reduction of restricted net assets). Releases are real entries
    DR 3400 / CR 3300, so the two account balances carry them. The two
    composed lines add up to exactly the balance sheet's total equity.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func as sqlfunc
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.classes import FUNCTIONS, TxnClass
from app.models.transactions import Transaction, TransactionLine
from app.services import cash_basis
from app.services.accounting import (
    NONPROFIT_ACCOUNTS,
    _q,
    find_nonprofit_account,
)
from app.services.classes_service import (
    UNCATEGORIZED_NAME,
    class_attribution,
    find_uncategorized_class_id,
    restricted_class_ids,
)
from app.services.csv_export import _SafeWriter
from app.services.statement_layout import (
    account_chart,
    account_path,
    bs_group_totals,
    bs_groups,
    bs_print_details,
    mark_bs_lines,
    family_rows,
)

_DEBIT_NORMAL = {AccountType.ASSET, AccountType.EXPENSE, AccountType.COGS}
_EXPENSE_TYPES = (AccountType.EXPENSE, AccountType.COGS)
ZERO = Decimal("0")


def _dec(v) -> Decimal:
    return _q(Decimal(str(v or 0)))


def _f(v) -> float:
    return float(_q(Decimal(str(v or 0))))


# ── base queries ─────────────────────────────────────────────────────────


def _account_totals(
    db: Session,
    acct_type: AccountType,
    start: Optional[date],
    end: Optional[date],
    basis: str = "accrual",
) -> list[dict]:
    """Per-account natural-balance totals (mirror of the balance sheet's
    helper, kept here so the statements never depend on a route module)."""
    q = (
        db.query(
            Account.id,
            Account.name,
            Account.account_number,
            Account.detail_type,
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
        .filter(Account.account_type == acct_type)
    )
    if start is not None:
        q = q.filter(Transaction.date >= start)
    if end is not None:
        q = q.filter(Transaction.date <= end)
    rows = []
    for acct_id, name, number, detail, dr, cr in (
        q.group_by(
            Account.id, Account.name, Account.account_number, Account.detail_type
        )
        .execution_options(**cash_basis.reading(basis))
        .all()
    ):
        dr, cr = Decimal(str(dr)), Decimal(str(cr))
        amount = (dr - cr) if acct_type in _DEBIT_NORMAL else (cr - dr)
        rows.append(
            {
                "account_id": acct_id,
                "account_name": name,
                "account_number": number,
                "detail_type": detail,
                "amount": _f(amount),
            }
        )
    rows.sort(key=lambda r: (r["account_number"] or "", r["account_name"]))
    return rows


def _class_totals(
    db: Session,
    acct_types: tuple,
    start: Optional[date],
    end: Optional[date],
    account_id: Optional[int] = None,
    basis: str = "accrual",
) -> list[tuple]:
    """(class_id, account_id, account_name, account_number, account_type,
    debit, credit) grouped by the fund a line belongs to."""
    uncat = find_uncategorized_class_id(db)
    cls = class_attribution(uncat).label("cls")
    q = (
        db.query(
            cls,
            Account.id,
            Account.name,
            Account.account_number,
            Account.account_type,
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
    )
    if account_id is not None:
        q = q.filter(TransactionLine.account_id == account_id)
    else:
        q = q.filter(Account.account_type.in_(acct_types))
    if start is not None:
        q = q.filter(Transaction.date >= start)
    if end is not None:
        q = q.filter(Transaction.date <= end)
    return (
        q.group_by(
            cls, Account.id, Account.name, Account.account_number, Account.account_type
        )
        .execution_options(**cash_basis.reading(basis))
        .all()
    )


def _income_by_restriction(
    db: Session,
    start: Optional[date],
    end: Optional[date],
    basis: str = "accrual",
) -> tuple[Decimal, Decimal, dict]:
    """(without, with, per-account {id: {name, number, without, with}})."""
    restricted = restricted_class_ids(db)
    per: dict[int, dict] = {}
    tot_without = tot_with = ZERO
    for cls_id, acct_id, name, number, _t, dr, cr in _class_totals(
        db, (AccountType.INCOME,), start, end, basis=basis
    ):
        amt = Decimal(str(cr)) - Decimal(str(dr))
        row = per.setdefault(
            acct_id,
            {
                "account_id": acct_id,
                "account_name": name,
                "account_number": number,
                "without": ZERO,
                "with": ZERO,
            },
        )
        if cls_id in restricted:
            row["with"] += amt
            tot_with += amt
        else:
            row["without"] += amt
            tot_without += amt
    return tot_without, tot_with, per


def _net_releases(
    db: Session,
    start: Optional[date],
    end: Optional[date],
    basis: str = "accrual",
) -> Decimal:
    """Net debits on Net Assets With Donor Restrictions in the period —
    releases, less any voided ones."""
    with_acct = find_nonprofit_account(db, "3400")
    if with_acct is None:
        return ZERO
    q = (
        db.query(
            sqlfunc.coalesce(
                sqlfunc.sum(TransactionLine.debit - TransactionLine.credit), 0
            )
        )
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(TransactionLine.account_id == with_acct.id)
    )
    if start is not None:
        q = q.filter(Transaction.date >= start)
    if end is not None:
        q = q.filter(Transaction.date <= end)
    return _dec(q.execution_options(**cash_basis.reading(basis)).scalar())


# ── Statement of Financial Position ──────────────────────────────────────


def _net_asset_account(db: Session, number: str) -> tuple:
    """(id, name, number) of a net-asset account, 3300 or 3400. While the
    chart has none (a read never adds it, #662) it is the row the account
    would be, with no id: its name, and its number unless the chart uses it."""
    acct = find_nonprofit_account(db, number)
    if acct is not None:
        return acct.id, acct.name, acct.account_number
    name = next(n for num, n, _t in NONPROFIT_ACCOUNTS if num == number)
    taken = db.query(Account.id).filter(Account.account_number == number).first()
    return None, name, None if taken else number


def statement_of_financial_position(
    db: Session, as_of: date, basis: str = "accrual"
) -> dict:
    with_id, with_name, with_number = _net_asset_account(db, "3400")
    without_id, without_name, without_number = _net_asset_account(db, "3300")
    # a default-typed sub-account prints in its parent's group, as on the
    # Balance Sheet (2.28.0): display only
    placed = bs_print_details(db)
    assets = mark_bs_lines(
        _account_totals(db, AccountType.ASSET, None, as_of, basis), placed
    )
    liabilities = mark_bs_lines(
        _account_totals(db, AccountType.LIABILITY, None, as_of, basis), placed
    )
    equity = _account_totals(db, AccountType.EQUITY, None, as_of, basis)

    real = {r["account_id"]: Decimal(str(r["amount"])) for r in equity}
    other_equity = [r for r in equity if r["account_id"] not in (with_id, without_id)]
    other_total = sum((Decimal(str(r["amount"])) for r in other_equity), ZERO)

    income_without, income_with, _ = _income_by_restriction(db, None, as_of, basis)
    expenses = sum(
        (
            Decimal(str(r["amount"]))
            for r in _account_totals(db, AccountType.EXPENSE, None, as_of, basis)
        ),
        ZERO,
    ) + sum(
        (
            Decimal(str(r["amount"]))
            for r in _account_totals(db, AccountType.COGS, None, as_of, basis)
        ),
        ZERO,
    )
    without_composed = real.get(without_id, ZERO) + income_without - expenses
    with_composed = real.get(with_id, ZERO) + income_with

    net_assets = list(other_equity) + [
        {
            "account_id": without_id,
            "account_name": without_name,
            "account_number": without_number,
            "amount": _f(without_composed),
            "detail": {
                "opening": _f(real.get(without_id, ZERO)),
                "current_activity": _f(income_without - expenses),
            },
        },
        {
            "account_id": with_id,
            "account_name": with_name,
            "account_number": with_number,
            "amount": _f(with_composed),
            "detail": {
                "opening": _f(real.get(with_id, ZERO)),
                "current_activity": _f(income_with),
            },
        },
    ]
    total_assets = sum((Decimal(str(r["amount"])) for r in assets), ZERO)
    total_liabilities = sum((Decimal(str(r["amount"])) for r in liabilities), ZERO)
    net_without = other_total + without_composed
    return {
        "as_of_date": as_of.isoformat(),
        "basis": basis,
        "basis_note": cash_basis.receivable_note(db, as_of, basis),
        "assets": assets,
        "liabilities": liabilities,
        # Current / Fixed / Other Assets, Current / Long Term Liabilities
        # (#390), as the Balance Sheet groups them; Net Assets as before
        "groups": {
            side: [
                {"heading": heading, "total_key": key, "lines": lines}
                for heading, key, lines in bs_groups(rows, side)
            ]
            for side, rows in (("assets", assets), ("liabilities", liabilities))
        },
        **bs_group_totals(assets, "assets"),
        **bs_group_totals(liabilities, "liabilities"),
        "net_assets": net_assets,
        "total_assets": _f(total_assets),
        "total_liabilities": _f(total_liabilities),
        "net_assets_without": _f(net_without),
        "net_assets_with": _f(with_composed),
        "total_net_assets": _f(net_without + with_composed),
        "total_liabilities_and_net_assets": _f(
            total_liabilities + net_without + with_composed
        ),
    }


# ── Statement of Activities ──────────────────────────────────────────────


def statement_of_activities(
    db: Session, start: date, end: date, basis: str = "accrual"
) -> dict:
    inc_without, inc_with, per = _income_by_restriction(db, start, end, basis)
    revenue = [
        {
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "account_number": r["account_number"],
            "without": _f(r["without"]),
            "with": _f(r["with"]),
            "total": _f(r["without"] + r["with"]),
        }
        for r in sorted(
            per.values(), key=lambda r: (r["account_number"] or "", r["account_name"])
        )
    ]
    releases = _net_releases(db, start, end, basis)
    expense_rows = _account_totals(
        db, AccountType.COGS, start, end, basis
    ) + _account_totals(db, AccountType.EXPENSE, start, end, basis)
    expenses = [
        {
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "account_number": r["account_number"],
            "without": r["amount"],
            "with": 0.0,
            "total": r["amount"],
        }
        for r in expense_rows
    ]
    total_expenses = sum((Decimal(str(r["amount"])) for r in expense_rows), ZERO)
    change_without = inc_without + releases - total_expenses
    change_with = inc_with - releases
    return _activities_layout(
        db,
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "basis": basis,
            "basis_note": cash_basis.receivable_note(db, end, basis),
            "revenue": revenue,
            "releases": {"without": _f(releases), "with": _f(-releases), "total": 0.0},
            "expenses": expenses,
            "totals": {
                "revenue_without": _f(inc_without),
                "revenue_with": _f(inc_with),
                "revenue": _f(inc_without + inc_with),
                "releases": _f(releases),
                "expenses": _f(total_expenses),
                "change_without": _f(change_without),
                "change_with": _f(change_with),
                "change_total": _f(change_without + change_with),
            },
        },
    )


def _activities_layout(db: Session, data: dict) -> dict:
    """Sub-accounts beneath their parents (#416): `tree` holds the revenue
    and expense lines as printed rows, a family total under each parent,
    for the screen and the PDF; each line also carries its full
    `account_path` ("Travel: Hotel") for the flat CSV. A parent of the
    other side (an expense under an income account) does not gather it."""
    chart = account_chart(db)
    side = {
        aid: ("revenue" if atype == AccountType.INCOME else "expenses")
        for aid, atype in db.query(Account.id, Account.account_type)
        if atype in (AccountType.INCOME,) + _EXPENSE_TYPES
    }
    keys = ("without", "with", "total")
    if data.get("compare") == "prior_year":
        keys += ("prior_total", "change")
    data["tree"] = {}
    for key in ("revenue", "expenses"):
        for line in data[key]:
            line["account_path"] = (
                account_path(chart, line["account_id"]) or line["account_name"]
            )
        data["tree"][key] = family_rows(
            data[key],
            chart,
            lambda aid, key=key: side.get(aid) == key,
            keys,
            lambda aid: (chart[aid]["number"], chart[aid]["name"]),
            numbered=False,
        )
    return data


# ── Fund balances ────────────────────────────────────────────────────────


def _periodized_by_class(
    db: Session, amount_expr, filters: list, start: date, end: date
) -> dict[int, tuple[Decimal, Decimal]]:
    """{class_id: (before start, within period)} for a signed line amount;
    None for untagged lines while the company has no default class."""
    uncat = find_uncategorized_class_id(db)
    cls = class_attribution(uncat).label("cls")
    before = sqlfunc.coalesce(
        sqlfunc.sum(case((Transaction.date < start, amount_expr), else_=0)), 0
    )
    within = sqlfunc.coalesce(
        sqlfunc.sum(
            case(
                ((Transaction.date >= start) & (Transaction.date <= end), amount_expr),
                else_=0,
            )
        ),
        0,
    )
    q = (
        db.query(cls, before, within)
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
        .filter(Transaction.date <= end, *filters)
        .group_by(cls)
    )
    return {(None if c is None else int(c)): (_dec(b), _dec(w)) for c, b, w in q.all()}


def fund_balances(db: Session, start: date, end: date) -> dict:
    restricted = restricted_class_ids(db)
    with_acct = find_nonprofit_account(db, "3400")
    income = _periodized_by_class(
        db,
        TransactionLine.credit - TransactionLine.debit,
        [Account.account_type == AccountType.INCOME],
        start,
        end,
    )
    spent = _periodized_by_class(
        db,
        TransactionLine.debit - TransactionLine.credit,
        [Account.account_type.in_(_EXPENSE_TYPES)],
        start,
        end,
    )
    released = (
        _periodized_by_class(
            db,
            TransactionLine.debit - TransactionLine.credit,
            [TransactionLine.account_id == with_acct.id],
            start,
            end,
        )
        if with_acct is not None
        else {}
    )
    names = {c.id: c for c in db.query(TxnClass).all()}
    funds = []
    unassigned_before = unassigned_within = ZERO
    for cls_id in sorted(
        set(income) | set(spent) | set(released),
        key=lambda i: names[i].name.lower() if i in names else "",
    ):
        if cls_id in restricted:
            inc_b, inc_w = income.get(cls_id, (ZERO, ZERO))
            sp_b, sp_w = spent.get(cls_id, (ZERO, ZERO))
            rl_b, rl_w = released.get(cls_id, (ZERO, ZERO))
            beginning = inc_b - rl_b
            ending = beginning + inc_w - rl_w
            fund = names[cls_id]
            funds.append(
                {
                    "class_id": cls_id,
                    "class_name": fund.name,
                    "restriction": fund.restriction,
                    "donor_name": fund.donor_name,
                    "purpose": fund.purpose,
                    "beginning": _f(beginning),
                    "contributions": _f(inc_w),
                    "expenses": _f(sp_w),
                    "releases": _f(rl_w),
                    "ending": _f(ending),
                    "unreleased": _f((sp_b + sp_w) - (rl_b + rl_w)),
                }
            )
        else:
            # 3400 activity carrying no restricted fund (a manual entry, or
            # a fund whose restriction was removed later)
            rl_b, rl_w = released.get(cls_id, (ZERO, ZERO))
            unassigned_before -= rl_b
            unassigned_within -= rl_w
    for fund in names.values():
        if fund.id in restricted and fund.id not in {f["class_id"] for f in funds}:
            funds.append(
                {
                    "class_id": fund.id,
                    "class_name": fund.name,
                    "restriction": fund.restriction,
                    "donor_name": fund.donor_name,
                    "purpose": fund.purpose,
                    "beginning": 0.0,
                    "contributions": 0.0,
                    "expenses": 0.0,
                    "releases": 0.0,
                    "ending": 0.0,
                    "unreleased": 0.0,
                }
            )
    funds.sort(key=lambda f: f["class_name"].lower())
    unassigned = None
    if unassigned_before or unassigned_within:
        unassigned = {
            "class_id": None,
            "class_name": "Unassigned",
            "beginning": _f(unassigned_before),
            "contributions": 0.0,
            "expenses": 0.0,
            "releases": _f(-unassigned_within),
            "ending": _f(unassigned_before + unassigned_within),
            "unreleased": 0.0,
        }
    rows = funds + ([unassigned] if unassigned else [])
    totals = {
        k: _f(sum((Decimal(str(r[k])) for r in rows), ZERO))
        for k in (
            "beginning",
            "contributions",
            "expenses",
            "releases",
            "ending",
            "unreleased",
        )
    }
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "funds": funds,
        "unassigned": unassigned,
        "totals": totals,
    }


# ── Statement of Functional Expenses ─────────────────────────────────────


def functional_expenses(
    db: Session, start: date, end: date, basis: str = "accrual"
) -> dict:
    uncat = find_uncategorized_class_id(db)
    cls = class_attribution(uncat)
    fn = sqlfunc.coalesce(TransactionLine.function, TxnClass.default_function).label(
        "fn"
    )
    q = (
        db.query(
            Account.id,
            Account.name,
            Account.account_number,
            fn,
            cls.label("cls"),
            sqlfunc.coalesce(
                sqlfunc.sum(TransactionLine.debit - TransactionLine.credit), 0
            ),
        )
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
        .outerjoin(TxnClass, TxnClass.id == cls)
        .filter(
            Account.account_type.in_(_EXPENSE_TYPES),
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .group_by(Account.id, Account.name, Account.account_number, fn, "cls")
        .execution_options(**cash_basis.reading(basis))
    )
    names = {c.id: c.name for c in db.query(TxnClass.id, TxnClass.name).all()}
    names[None] = UNCATEGORIZED_NAME
    rows: dict[int, dict] = {}
    programs: dict[int, Decimal] = {}
    cols = list(FUNCTIONS) + ["unassigned"]
    for acct_id, name, number, function, cls_id, amt in q.all():
        amt = Decimal(str(amt or 0))
        row = rows.setdefault(
            acct_id,
            {
                "account_id": acct_id,
                "account_name": name,
                "account_number": number,
                **{c: ZERO for c in cols},
                "total": ZERO,
            },
        )
        col = function if function in FUNCTIONS else "unassigned"
        row[col] += amt
        row["total"] += amt
        if col == "program":
            programs[cls_id] = programs.get(cls_id, ZERO) + amt
    out_rows = []
    for r in sorted(
        rows.values(), key=lambda r: (r["account_number"] or "", r["account_name"])
    ):
        out_rows.append(
            {k: (_f(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
        )
    totals = {c: _f(sum((r[c] for r in rows.values()), ZERO)) for c in cols}
    totals["total"] = _f(sum((r["total"] for r in rows.values()), ZERO))
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "basis": basis,
        "basis_note": cash_basis.receivable_note(db, end, basis),
        "rows": out_rows,
        "totals": totals,
        "programs": [
            {"class_id": cid, "class_name": names.get(cid, "Unknown"), "amount": _f(a)}
            for cid, a in sorted(
                programs.items(), key=lambda kv: names.get(kv[0], "").lower()
            )
            if a
        ],
    }


# ── CSV ──────────────────────────────────────────────────────────────────


def _csv(header: list, rows: list[list]) -> str:
    buf = io.StringIO()
    w = _SafeWriter(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def financial_position_csv(data: dict) -> str:
    rows = []
    for label, key, total_key in (
        ("Assets", "assets", "total_assets"),
        ("Liabilities", "liabilities", "total_liabilities"),
        ("Net Assets", "net_assets", "total_net_assets"),
    ):
        rows.append([label, ""])
        groups = (data.get("groups") or {}).get(key)
        if groups is None:
            for r in data[key]:
                rows.append([f"  {r['account_name']}", f"{r['amount']:.2f}"])
        for g in groups or ():
            rows.append([f"  {g['heading']}", ""])
            for r in g["lines"]:
                rows.append([f"    {r['account_name']}", f"{r['amount']:.2f}"])
            rows.append([f"  Total {g['heading']}", f"{data[g['total_key']]:.2f}"])
        rows.append([f"Total {label}", f"{data[total_key]:.2f}"])
    rows.append(
        ["Liabilities + Net Assets", f"{data['total_liabilities_and_net_assets']:.2f}"]
    )
    as_of = f"As of {data['as_of_date']}"
    if data.get("basis") == "cash":
        as_of += " (cash basis)"
    return _csv(["", as_of], rows)


def _cell(v) -> Decimal:
    """A money cell: a Decimal, which the safe writer leaves a number (a
    "-500.00" string got the formula guard's apostrophe)."""
    return _dec(v)


def activities_csv(data: dict) -> str:
    """The Statement of Activities as a spreadsheet. Amounts are number
    cells (_cell), so a negative one stays a number, not guarded text."""
    t = data["totals"]
    cmp = data.get("compare") == "prior_year"
    pt = data.get("prior", {}).get("totals", {}) if cmp else {}

    def ext(total, prior):
        if not cmp:
            return []
        return [_cell(prior or 0), _cell((total or 0) - (prior or 0))]

    blank = [""] * 2 if cmp else []
    rows = [["Revenue & Support", "", "", ""] + blank]
    for r in data["revenue"]:
        rows.append(
            [
                f"  {r.get('account_path') or r['account_name']}",  # #416
                _cell(r["without"]),
                _cell(r["with"]),
                _cell(r["total"]),
            ]
            + ext(r["total"], r.get("prior_total"))
        )
    rows.append(
        [
            "Total Revenue & Support",
            _cell(t["revenue_without"]),
            _cell(t["revenue_with"]),
            _cell(t["revenue"]),
        ]
        + ext(t["revenue"], pt.get("revenue"))
    )
    rl = data["releases"]
    rows.append(
        [
            "Net assets released from restrictions",
            _cell(rl["without"]),
            _cell(rl["with"]),
            _cell(0),
        ]
        + ext(0, 0)
    )
    rows.append(["Expenses", "", "", ""] + blank)
    for r in data["expenses"]:
        rows.append(
            [
                f"  {r.get('account_path') or r['account_name']}",  # #416
                _cell(r["without"]),
                _cell(0),
                _cell(r["total"]),
            ]
            + ext(r["total"], r.get("prior_total"))
        )
    rows.append(
        ["Total Expenses", _cell(t["expenses"]), _cell(0), _cell(t["expenses"])]
        + ext(t["expenses"], pt.get("expenses"))
    )
    rows.append(
        [
            "Change in Net Assets",
            _cell(t["change_without"]),
            _cell(t["change_with"]),
            _cell(t["change_total"]),
        ]
        + ext(t["change_total"], pt.get("change_total"))
    )
    header = [
        "(cash basis)" if data.get("basis") == "cash" else "",
        "Without Donor Restrictions",
        "With Donor Restrictions",
        "Total",
    ]
    if cmp:
        header += [f"Prior year ({data['prior']['start_date'][:4]})", "Change"]
    return _csv(header, rows)


def fund_balances_csv(data: dict) -> str:
    keys = (
        "beginning",
        "contributions",
        "expenses",
        "releases",
        "ending",
        "unreleased",
    )
    rows = [[f["class_name"]] + [f"{f[k]:.2f}" for k in keys] for f in data["funds"]]
    if data["unassigned"]:
        rows.append(
            [data["unassigned"]["class_name"]]
            + [f"{data['unassigned'][k]:.2f}" for k in keys]
        )
    rows.append(["Total"] + [f"{data['totals'][k]:.2f}" for k in keys])
    return _csv(
        [
            "Fund",
            "Beginning",
            "Contributions",
            "Spent",
            "Released",
            "Ending",
            "Unreleased",
        ],
        rows,
    )


def functional_expenses_csv(data: dict) -> str:
    # Form 990 Part IX column order: (A) Total, (B) Program services,
    # (C) Management and general, (D) Fundraising; unassigned last so a
    # preparer sees what still needs a function.
    keys = ("total", "program", "management", "fundraising", "unassigned")
    cmp = data.get("compare") == "prior_year"

    def ext(r):
        if not cmp:
            return []
        return [f"{r.get('prior_total', 0):.2f}", f"{r.get('change', 0):.2f}"]

    rows = [
        [f"{r['account_number'] or ''} {r['account_name']}".strip()]
        + [f"{r[k]:.2f}" for k in keys]
        + ext(r)
        for r in data["rows"]
    ]
    pt = data.get("prior", {}).get("totals", {}) if cmp else {}
    total_ext = (
        [
            f"{pt.get('total', 0):.2f}",
            f"{data['totals']['total'] - pt.get('total', 0):.2f}",
        ]
        if cmp
        else []
    )
    rows.append(["Total"] + [f"{data['totals'][k]:.2f}" for k in keys] + total_ext)
    header = [
        "Expense (cash basis)" if data.get("basis") == "cash" else "Expense",
        "Total (A)",
        "Program services (B)",
        "Management and general (C)",
        "Fundraising (D)",
        "Unassigned",
    ]
    if cmp:
        header += [f"Prior year ({data['prior']['start_date'][:4]})", "Change"]
    return _csv(header, rows)


# ── Prior-year comparison ────────────────────────────────────────────────


def _shift_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # Feb 29
        return d.replace(year=d.year - 1, day=28)


def _merge_prior(
    rows: list[dict], prior_rows: list[dict], key: str, value: str
) -> list[dict]:
    """Attach `prior_<value>` and `change` to each row, adding rows that only
    exist in the prior period, so the two columns share one account list."""
    prior_by = {r[key]: r for r in prior_rows}
    out = []
    seen = set()
    for r in rows:
        pr = prior_by.get(r[key])
        prior_val = pr[value] if pr else 0.0
        out.append(
            {
                **r,
                f"prior_{value}": prior_val,
                "change": _f(Decimal(str(r[value])) - Decimal(str(prior_val))),
            }
        )
        seen.add(r[key])
    for r in prior_rows:
        if r[key] in seen:
            continue
        blank = {k: (0.0 if isinstance(v, float) else v) for k, v in r.items()}
        out.append(
            {**blank, f"prior_{value}": r[value], "change": _f(-Decimal(str(r[value])))}
        )
    out.sort(key=lambda r: (r.get("account_number") or "", r.get("account_name") or ""))
    return out


def statement_of_activities_compared(
    db: Session, start: date, end: date, basis: str = "accrual"
) -> dict:
    """The statement with a prior-year column: same dates one year earlier."""
    cur = statement_of_activities(db, start, end, basis)
    p_start, p_end = _shift_year(start), _shift_year(end)
    prior = statement_of_activities(db, p_start, p_end, basis)
    cur["revenue"] = _merge_prior(
        cur["revenue"], prior["revenue"], "account_id", "total"
    )
    cur["expenses"] = _merge_prior(
        cur["expenses"], prior["expenses"], "account_id", "total"
    )
    cur["prior"] = {
        "start_date": p_start.isoformat(),
        "end_date": p_end.isoformat(),
        "totals": prior["totals"],
        "releases": prior["releases"],
    }
    cur["compare"] = "prior_year"
    return _activities_layout(db, cur)


def functional_expenses_compared(
    db: Session, start: date, end: date, basis: str = "accrual"
) -> dict:
    cur = functional_expenses(db, start, end, basis)
    p_start, p_end = _shift_year(start), _shift_year(end)
    prior = functional_expenses(db, p_start, p_end, basis)
    cur["rows"] = _merge_prior(cur["rows"], prior["rows"], "account_id", "total")
    cur["prior"] = {
        "start_date": p_start.isoformat(),
        "end_date": p_end.isoformat(),
        "totals": prior["totals"],
    }
    cur["compare"] = "prior_year"
    return cur
