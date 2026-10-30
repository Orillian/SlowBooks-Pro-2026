"""Small Transaction Detail by Account files, written row by row (#663).

QuickBooks Desktop's layout: a heading row per account (a sub-account one
column further in), each transaction row under it with its Type, Date, Num,
Name, Memo, Split, Debit and Credit in the report's interleaved columns, a
"Total <account>" row closing each section and the report's TOTAL row at the
end. The tests of the import build the files they need from these, so each
reads as the report it stands for.
"""

import csv
import hashlib
import io
import json

URL = "/api/csv/import/qb-report"
HEADER = (
    ",,,,,Type,,Date,,Num,,Adj,,Name,,Memo,,Clr,,Split,,Debit,,Credit,,"
    "Original Amount,,Balance"
)
WIDTH = 28


def _cells():
    return [""] * WIDTH


def heading(name, depth=1):
    c = _cells()
    c[depth] = name
    return c


def total(name, debit, credit, depth=1):
    c = _cells()
    c[depth] = f"Total {name}"
    c[21] = debit
    c[23] = credit
    return c


def grand(debit, credit):
    c = _cells()
    c[0] = "TOTAL"
    c[21] = debit
    c[23] = credit
    return c


def row(ttype, day, num, name, memo, split, debit="", credit=""):
    c = _cells()
    c[5], c[7], c[9], c[13], c[15], c[19], c[21], c[23] = (
        ttype,
        day,
        num,
        name,
        memo,
        split,
        debit,
        credit,
    )
    return c


def build(rows) -> bytes:
    out = io.StringIO()
    out.write(HEADER + "\r\n")
    w = csv.writer(out, lineterminator="\r\n")
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode("cp1252")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def post(client, data: bytes, answers=None, adjusting=False):
    """The upload as the page sends it; answers carry the file's sha256
    unless they say otherwise."""
    form = {}
    if answers is not None:
        answers = {"file_sha256": sha(data), **answers}
        form["answers"] = json.dumps(answers)
    if adjusting:
        form["adjusting_only"] = "true"
    return client.post(
        URL,
        files={
            "file": ("Transaction Detail by Account.csv", io.BytesIO(data), "text/csv")
        },
        data=form,
    )


def send(client, data: bytes, answers=None, adjusting=False) -> dict:
    r = post(client, data, answers, adjusting)
    assert r.status_code == 200, r.text
    return r.json()
