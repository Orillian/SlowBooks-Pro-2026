"""#659: the pages make a portal or payment link by POST and never lean on
a GET's side effect. Pinned on the source, in the repo's style, because
putting the GET back left every API test green (skeptic pass on #660)."""

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js"


def test_copy_payment_link_posts():
    js = (JS / "invoices.js").read_text(encoding="utf-8")
    assert "API.post(`/payments/payment-link/${id}`)" in js
    assert "API.get(`/payments/payment-link/" not in js


def test_employee_page_offers_make_link_when_there_is_none():
    js = (JS / "employees.js").read_text(encoding="utf-8")
    assert "'Make Link' : 'Rotate Token'" in js
    # Decided by the server's has_link, never guessed from expires_at: a
    # link from before the expiry column exists and works (Air pass).
    assert "const none = token.has_link === false;" in js
    assert "EmployeesPage._regeneratePortalToken(${id})" in js


def test_settings_flags_a_refused_admin_token():
    js = (JS / "settings.js").read_text(encoding="utf-8")
    assert "t.refused ?" in js
    assert "no longer accepted: tokens can't be administrators" in js
