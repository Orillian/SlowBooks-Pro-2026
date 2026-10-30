"""Employee portal link: absolute, and never swallowed by the desktop shim.

Field report: clicking the portal link inside the desktop app rendered the
dashboard in a document window whose tabs did nothing. The shim fetched the
same-origin _blank link and showed the HTML with no origin to resolve links
against. The portal is the employee's own cookie-based site — it must open
in a real browser, with an absolute URL the employee can actually reach."""

from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def test_portal_url_is_absolute(client, seed_accounts):
    emp = client.post(
        "/api/employees",
        json={
            "first_name": "Pat",
            "last_name": "Portal",
            "pay_type": "hourly",
            "pay_rate": 20,
        },
    ).json()
    tok = client.post(f"/api/employees/{emp['id']}/portal-token").json()
    assert tok["portal_url"].startswith("http://testserver/portal/")
    assert tok["portal_url"].endswith(tok["portal_token"])
    rotated = client.post(f"/api/employees/{emp['id']}/portal-token").json()
    assert rotated["portal_url"].startswith("http://testserver/portal/")
    assert rotated["portal_token"] != tok["portal_token"]
    # the link still claims a session end to end
    r = client.get(rotated["portal_url"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal/"


def test_desktop_shim_leaves_portal_links_to_the_browser():
    shim = (ROOT / "app/static/js/desktop_shim.js").read_text(encoding="utf-8")
    assert "function isPortalUrl" in shim
    assert "isSameOrigin(url) && !isPortalUrl(url)" in shim  # window.open path
    assert "if (isPortalUrl(a.href))" in shim  # click path
    assert "open_external" in shim


def test_launcher_open_external_uses_the_system_browser():
    import desktop_launcher

    api = desktop_launcher.PickerApi(3001)
    with patch("webbrowser.open") as opened:
        assert api.open_external("http://127.0.0.1:3001/portal/abc") == {
            "success": True
        }
        opened.assert_called_once_with("http://127.0.0.1:3001/portal/abc")
        bad = api.open_external("file:///etc/passwd")
        assert bad["success"] is False
        assert opened.call_count == 1


def test_launcher_open_external_hands_a_mailto_to_the_mail_program():
    """Contact support's Send is a mailto: link; WKWebView can't load one, so
    the shim sends it over the bridge (2.30.0 gate NEW-185)."""
    import desktop_launcher

    api = desktop_launcher.PickerApi(3001)
    href = "mailto:support@slowbookspro.com?subject=%5BSBP%5D%20hi&body=x"
    with patch("webbrowser.open") as opened:
        assert api.open_external(href) == {"success": True}
        opened.assert_called_once_with(href)
        assert api.open_external("MAILTO:support@slowbookspro.com")["success"]
        assert api.open_external("javascript:alert(1)")["success"] is False
        assert opened.call_count == 2


def test_a_read_never_makes_a_portal_link(client, db_session):
    """#659: GET answers what exists. With none yet it says so and mints
    nothing, so a read-only role or an agent's read tool cannot make an
    employee's login; POST makes it, and GET then reads the same one."""
    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    from app.main import app

    emp = client.post(
        "/api/employees",
        json={
            "first_name": "Read",
            "last_name": "Only",
            "pay_type": "hourly",
            "pay_rate": 20,
        },
    ).json()
    # a new employee is given a link on creation; one from before that
    # (or whose link was never made) has none
    db_session.execute(
        sa.text(
            "UPDATE employees SET portal_token_hash = NULL, portal_token_enc = NULL,"
            " portal_token_expires_at = NULL WHERE id = :i"
        ),
        {"i": emp["id"]},
    )
    db_session.commit()
    none = client.get(f"/api/employees/{emp['id']}/portal-token").json()
    assert none["portal_token"] is None and none["portal_url"] is None
    assert none["has_link"] is False
    assert "no portal link yet" in none["note"]
    assert "POST" not in none["note"]  # a person reads this on the page
    assert (
        client.get(f"/api/employees/{emp['id']}/portal-token").json()["portal_token"]
        is None
    )
    ro = client.post("/api/tokens", json={"label": "ro", "role": "readonly"}).json()
    reader = TestClient(app)
    reader.headers["Authorization"] = f"Bearer {ro['token']}"
    assert reader.post(f"/api/employees/{emp['id']}/portal-token").status_code == 403
    assert reader.get(f"/api/employees/{emp['id']}/portal-token").status_code == 403
    made = client.post(f"/api/employees/{emp['id']}/portal-token").json()
    assert (
        client.get(f"/api/employees/{emp['id']}/portal-token").json()["has_link"]
        is True
    )
    assert made["portal_token"]
    read = client.get(f"/api/employees/{emp['id']}/portal-token").json()
    assert read["portal_token"] == made["portal_token"] and "note" not in read
