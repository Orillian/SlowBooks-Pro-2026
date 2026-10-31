# ============================================================================
# Duplicate check numbers (#481): QuickBooks Desktop's "Warn about duplicate
# check numbers", off until turned on (Settings → Transaction Warnings).
# Turned on, a check, a register payment or a bill payment whose number is already on that bank account is refused with a
# 409 the person answers: keep the number (the client sends the same
# request again with ?allow_duplicate_check_number=true) or go back and
# edit it. Possible Duplicates matches amount, payee and date afterwards and
# never looks at the number; this looks at the number before the save.
#
# A check number is digits alone ("5001"): "ACH", "DEBIT", "EFT" and an
# expense's "rcpt 1187" repeat or name a receipt, and are not checks.
# Only money out of the account counts (a deposit's slip number is not a
# check), and a void, or the reversal that voided it, frees its number. A
# QuickBooks Desktop ledger import's entry counts when QuickBooks' Type is a
# check's (#663): the Num of a journal entry or an EFT is not a check number.
# ============================================================================

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.services.account_label import label_for
from app.models.accounts import Account
from app.models.bills import BillPayment
from app.models.contacts import Vendor
from app.models.transactions import Transaction, TransactionLine

SETTING = "warn_duplicate_check_number"
CODE = "duplicate_check_number"


def _number(text) -> str:
    n = (text or "").strip()
    return n if n.isdigit() else ""


# QuickBooks' types that write a check, US and Canadian spellings
CHECK_TYPES = frozenset(
    t.lower()
    for t in (
        "Check",
        "Cheque",
        "Bill Pmt -Check",
        "Bill Pmt -Cheque",
        "Paycheck",
        "Paycheque",
        "Liability Check",
        "Liability Cheque",
        "Sales Tax Payment",
    )
)


def _is_check(txn) -> bool:
    """Whether a posting's number is a check's: always, but for a
    QuickBooks Desktop ledger import's entry, whose Type must be a check's.
    An entry with no Type stored is read by its key, which starts with it."""
    from app.services.entry_text import QB_LEDGER_SOURCE

    if txn.source_type != QB_LEDGER_SOURCE:
        return True
    if txn.source_kind:
        return txn.source_kind.strip().lower() in CHECK_TYPES
    key = (txn.source_ref or txn.reference or "").lower()
    return any(key.startswith(t + " ") for t in CHECK_TYPES)


def warning_on(db: Session) -> bool:
    from app.services.settings_service import get_all_settings

    return get_all_settings(db).get(SETTING, "false") == "true"


def uses_of(
    db: Session,
    account_id: int,
    number,
    exclude_txn_id: int | None = None,
    exclude_bill_payment_id: int | None = None,
) -> list[dict]:
    """Every live posting that already paid out of `account_id` under check
    number `number`: register entries, expenses and checks (the posting's
    reference) and bill payments (the payment's check number)."""
    from app.services.accounting import get_checking_account_id
    from app.services.bank_register import payees_for, voided_transaction_ids

    num = _number(number)
    if not num or account_id is None:
        return []
    wanted = num.lower()
    rows = (
        db.query(Transaction, TransactionLine)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(TransactionLine.account_id == account_id)
        .filter(TransactionLine.credit > 0)
        .filter(func.lower(func.trim(Transaction.reference)) == wanted)
        .all()
    )
    txns = {
        t.id: (t, ln.credit)
        for t, ln in rows
        if t.id != exclude_txn_id
        and not (t.source_type or "").endswith("_void")
        and _is_check(t)
    }
    # a bill payment keeps its number on the payment, not the posting
    try:
        checking = get_checking_account_id(db)
    except Exception:  # no 1000 Checking: no default to match against
        checking = None
    payments = [
        bp
        for bp in db.query(BillPayment)
        .filter(func.lower(func.trim(BillPayment.check_number)) == wanted)
        .filter(BillPayment.is_voided.is_(False))
        .all()
        if bp.id != exclude_bill_payment_id
        and (bp.pay_from_account_id or checking) == account_id
    ]
    paid_txn_ids = {bp.transaction_id for bp in payments if bp.transaction_id}
    for txn_id in paid_txn_ids:
        txns.pop(txn_id, None)
    voided = voided_transaction_ids(db, list(txns.keys()))
    live = [(t, amt) for tid, (t, amt) in txns.items() if tid not in voided]
    payees = payees_for(db, [t for t, _ in live])
    out = [
        {
            "date": t.date.isoformat() if t.date else None,
            "payee": payees.get(t.id, "") or "",
            "amount": f"{Decimal(str(amt)):.2f}",
        }
        for t, amt in live
    ]
    if payments:
        vendors = {
            v.id: v.name
            for v in db.query(Vendor).filter(
                Vendor.id.in_({bp.vendor_id for bp in payments})
            )
        }
        out += [
            {
                "date": bp.date.isoformat() if bp.date else None,
                "payee": vendors.get(bp.vendor_id, ""),
                "amount": f"{Decimal(str(bp.amount)):.2f}",
            }
            for bp in payments
        ]
    return sorted(out, key=lambda u: u["date"] or "")


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}/{y}"


def refuse_duplicate(
    db: Session,
    account_id: int,
    number,
    allow: bool = False,
    exclude_txn_id: int | None = None,
    exclude_bill_payment_id: int | None = None,
) -> None:
    """The 409, unless the person already said keep it (`allow`), the
    company turned the warning off, or the number is new on the account."""
    if allow or not _number(number) or not warning_on(db):
        return
    account = db.query(Account).filter(Account.id == account_id).first()
    if account is None or account.bank_kind == "credit_card":
        return  # a card has no checks
    uses = uses_of(db, account_id, number, exclude_txn_id, exclude_bill_payment_id)
    if not uses:
        return
    first = uses[0]
    name = label_for(account, db)
    said = ", ".join(
        part
        for part in (
            first["payee"],
            _when(first["date"]),
            f"${Decimal(first['amount']):,.2f}",
        )
        if part
    )
    more = f" and {len(uses) - 1} more" if len(uses) > 1 else ""
    raise HTTPException(
        status_code=409,
        detail={
            "code": CODE,
            "number": _number(number),
            "message": (
                f"Check number {_number(number)} is already used on {name} "
                f"({said}{more})."
            ),
            "uses": uses,
        },
    )


def _paying_account_id(db: Session, bp: BillPayment) -> int | None:
    from app.services.accounting import get_checking_account_id

    if bp.pay_from_account_id:
        return bp.pay_from_account_id
    try:
        return get_checking_account_id(db)
    except Exception:  # no 1000 Checking: no default account
        return None


def unprinted_payments(db: Session, account_id: int) -> list[BillPayment]:
    """The checks Pay Bills wrote on `account_id` that have no check number
    yet (#603): a live payment by check that nobody has numbered. Numbering
    a payment is what prints it, so this is the "to be printed" list; a
    payment typed in with its number was written by hand and is not on it.
    A payment dated on or before the closing date is left off: numbering it
    would change a closed period, and a company that paid by check for
    years without typing numbers is not offered all of them to renumber."""
    from app.services.closing_date import get_closing_date

    q = (
        db.query(BillPayment)
        .filter(BillPayment.is_voided.is_(False))
        .filter(func.coalesce(func.trim(BillPayment.check_number), "") == "")
        .filter(or_(BillPayment.method.is_(None), BillPayment.method == "check"))
    )
    closing = get_closing_date(db)
    if closing is not None:
        q = q.filter(BillPayment.date > closing)
    rows = q.order_by(BillPayment.date, BillPayment.id).all()
    return [bp for bp in rows if _paying_account_id(db, bp) == account_id]


def next_check_number(db: Session, account_id: int) -> str:
    """One past the highest numeric check number on `account_id` (registers,
    expenses and bill payments alike); "1001" for an account with none.

    A QuickBooks Desktop ledger import's numbers are left out (#663): its
    reference is whatever QuickBooks printed in Num, and one long number
    would put the next check in the millions. uses_of still finds them."""
    from app.services.bank_register import voided_transaction_ids
    from app.services.entry_text import QB_LEDGER_SOURCE

    highest = 0
    refs = (
        db.query(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(TransactionLine.account_id == account_id)
        .filter(TransactionLine.credit > 0)
        .filter(Transaction.reference.isnot(None))
        .all()
    )
    voided = voided_transaction_ids(db, [t.id for t in refs])
    for t in refs:
        if t.source_type == QB_LEDGER_SOURCE:
            continue
        n = _number(t.reference)
        if n and t.id not in voided and not (t.source_type or "").endswith("_void"):
            highest = max(highest, int(n))
    for bp in db.query(BillPayment).filter(BillPayment.is_voided.is_(False)):
        n = _number(bp.check_number)
        if n and _paying_account_id(db, bp) == account_id:
            highest = max(highest, int(n))
    return str(highest + 1) if highest else "1001"
