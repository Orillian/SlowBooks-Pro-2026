# ============================================================================
# Class dimension helpers — default-row management and strict lookup.
# ============================================================================

from typing import Optional

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.models.classes import TxnClass, is_restricted
from app.models.transactions import Transaction, TransactionLine

UNCATEGORIZED_NAME = "Uncategorized"


def uncategorized_class_id(db: Session) -> int:
    """Id of the system-default class, creating it on first use.

    Kept get-or-create (rather than assuming seed order) so fresh
    databases, migrated desktop files, and the test harness all converge
    on one canonical row. For the write paths only: a read asks
    find_uncategorized_class_id, which never makes it (#662).
    """
    row = db.query(TxnClass).filter(TxnClass.is_system_default).first()
    if row:
        return row.id
    # A pre-existing user class named "Uncategorized" gets promoted rather
    # than colliding with the unique name constraint.
    row = db.query(TxnClass).filter(TxnClass.name == UNCATEGORIZED_NAME).first()
    if row:
        row.is_system_default = True
    else:
        row = TxnClass(name=UNCATEGORIZED_NAME, is_system_default=True)
        db.add(row)
    db.flush()
    return row.id


def find_uncategorized_class_id(db: Session) -> Optional[int]:
    """Id of the system-default class, or None while the company has none.

    A read never makes it (#662): untagged lines then carry no class, and
    the reports name that bucket Uncategorized. A class named Uncategorized
    stands in for a missing default, as uncategorized_class_id would
    promote it.
    """
    row = db.query(TxnClass.id).filter(TxnClass.is_system_default).first()
    if row is None:
        row = db.query(TxnClass.id).filter(TxnClass.name == UNCATEGORIZED_NAME).first()
    return row[0] if row else None


def resolve_class_id(db: Session, name: str) -> Optional[int]:
    """Strict class lookup by exact then case-insensitive name.

    Returns the id or None — callers (e.g. the IIF importer) raise when
    None so a missing CLASS surfaces the same way a missing vendor or
    account does. No fuzzy matching: classes are user-defined labels and
    a near-miss silently filed under the wrong class is worse than an
    error.
    """
    if not name:
        return None
    row = db.query(TxnClass).filter(TxnClass.name == name).first()
    if not row:
        row = db.query(TxnClass).filter(TxnClass.name.ilike(name)).first()
    return row.id if row else None


def class_attribution(uncat_id: Optional[int]):
    """SQL expression for the class a posted line belongs to: the line's
    own class, else the transaction header's, else the system default.
    Every by-class / by-fund report groups on this so a bill with three
    line classes and a blank header lands in three funds, not in
    Uncategorized (mirror of jobs_service.job_attribution). With no
    default yet (find_uncategorized_class_id is None) an untagged line's
    class is NULL."""
    if uncat_id is None:
        return sqlfunc.coalesce(TransactionLine.class_id, Transaction.class_id)
    return sqlfunc.coalesce(TransactionLine.class_id, Transaction.class_id, uncat_id)


def restricted_class_ids(db: Session) -> set[int]:
    """Ids of the funds that carry donor restrictions."""
    return {
        c.id
        for c in db.query(TxnClass.id, TxnClass.restriction).all()
        if is_restricted(c.restriction)
    }


def default_function_of(
    db: Session, class_id: Optional[int], cache: dict
) -> Optional[str]:
    """The function a line inherits from its class (memoised per posting)."""
    if not class_id:
        return None
    if class_id not in cache:
        row = db.get(TxnClass, class_id)
        cache[class_id] = row.default_function if row else None
    return cache[class_id]
