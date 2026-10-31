"""Scoped API tokens (v2.5.1): issuance, bearer auth, role enforcement,
no-self-escalation, revocation, and audit attribution."""

from fastapi.testclient import TestClient

from app.main import app
from app.models.api_tokens import ApiToken
from app.models.audit import AuditLog


def _mint(client, label="agent", role="readonly"):
    r = client.post("/api/tokens", json={"label": label, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(token):
    """Fresh cookie-less client so the Bearer path (not the session) auths."""
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


# ---------------------------------------------------------------------------
# Issuance + management (admin sessions only)
# ---------------------------------------------------------------------------


def test_create_returns_secret_once(client, db_session):
    created = _mint(client, "claude-code", "readonly")
    assert created["token"].startswith("sbp_")
    assert created["token_hint"] == created["token"][:10]

    listed = client.get("/api/tokens").json()
    assert len(listed) == 1
    assert "token" not in listed[0]  # never again
    row = db_session.query(ApiToken).one()
    assert created["token"] not in (row.token_hash, row.token_hint)  # hashed at rest


def test_validation_and_dupes(client):
    r = client.post("/api/tokens", json={"label": "x1", "role": "boss"})
    assert r.status_code == 400
    _mint(client, "dupe", "readonly")
    r = client.post("/api/tokens", json={"label": "dupe", "role": "readonly"})
    assert r.status_code == 409


def test_non_admin_sessions_cannot_manage(client, db_session):
    from app.models.users import ROLE_BOOKKEEPER, User
    from app.services import auth as auth_service

    db_session.add(
        User(
            username="keeper",
            display_name="Keeper",
            password_hash=auth_service.hash_password("keeper-password-1"),
            role=ROLE_BOOKKEEPER,
            is_active=True,
        )
    )
    db_session.commit()
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login", json={"username": "keeper", "password": "keeper-password-1"}
    )
    assert client.get("/api/tokens").status_code == 403
    assert (
        client.post("/api/tokens", json={"label": "sneak", "role": "admin"}).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Bearer authentication + role enforcement
# ---------------------------------------------------------------------------


def test_readonly_token_reads_but_never_writes(client):
    tok = _mint(client, "reporter", "readonly")["token"]
    api = _bearer(tok)
    assert api.get("/api/customers").status_code == 200
    assert api.post("/api/customers", json={"name": "Nope"}).status_code == 403


def test_bookkeeper_token_daily_writes_only(client):
    tok = _mint(client, "keeper-bot", "bookkeeper")["token"]
    api = _bearer(tok)
    assert api.post("/api/customers", json={"name": "Bot Corp"}).status_code in (
        200,
        201,
    )
    assert api.put("/api/settings", json={"invoice_prefix": "T-"}).status_code == 403


def test_an_admin_token_is_refused(client):
    """A token is never an administrator (#434): roles are its scope, and
    the one that reaches settings and identities stays with a person."""
    r = client.post("/api/tokens", json={"label": "root-bot", "role": "admin"})
    assert r.status_code == 400, r.text
    assert "cannot be an administrator" in r.json()["detail"]
    assert _mint(client, "keeper-bot", "bookkeeper")["role"] == "bookkeeper"


def test_an_admin_token_from_before_2_29_is_refused_and_flagged(
    client, db_session, legacy_admin_token
):
    """#659: one made before 2.29.0 kept working until revoked. Now every
    request it makes is a 403 that says to revoke it and make a scoped
    token, the list flags it, and revoking it still works and is final."""
    old = _bearer(legacy_admin_token("old-root"))
    for call in (
        lambda: old.get("/api/customers"),
        lambda: old.get("/api/settings"),
        lambda: old.put("/api/settings", json={"invoice_prefix": "A-"}),
        lambda: old.get("/api/users"),
        lambda: old.post("/api/tokens", json={"label": "evil", "role": "admin"}),
    ):
        r = call()
        assert r.status_code == 403, r.text
        assert '"old-root"' in r.json()["detail"]
        assert "can't be administrators" in r.json()["detail"]
        assert "Revoke it" in r.json()["detail"]
    fine = _mint(client, "keeper", "bookkeeper")
    assert {t["label"]: t["refused"] for t in client.get("/api/tokens").json()} == {
        "old-root": True,
        "keeper": False,
    }
    assert _bearer(fine["token"]).get("/api/customers").status_code == 200
    row = db_session.query(ApiToken).filter_by(label="old-root").one()
    # Refused requests do not count as use (skeptic pass): "last used" would
    # read as if the program got in.
    db_session.refresh(row)
    assert row.last_used_at is None
    assert (
        client.put(f"/api/tokens/{row.id}", json={"is_active": False}).status_code
        == 200
    )
    revoked = old.get("/api/customers")
    assert revoked.status_code == 401 and "was revoked" in revoked.json()["detail"]
    assert {t["label"]: t["refused"] for t in client.get("/api/tokens").json()}[
        "old-root"
    ] is False


def test_tokens_cannot_manage_identities(client):
    """The no-self-escalation wall: a token of any role is kept out of
    users and tokens (a leaked bookkeeper token must not mint itself an
    admin one). Admin tokens, the old vector, are refused outright now."""
    api = _bearer(_mint(client, "keeper-bot", "bookkeeper")["token"])
    assert api.put("/api/settings", json={"invoice_prefix": "A-"}).status_code == 403
    assert api.get("/api/users").status_code == 403
    assert api.get("/api/tokens").status_code == 403
    assert (
        api.post("/api/tokens", json={"label": "evil", "role": "admin"}).status_code
        == 403
    )


def test_bad_and_revoked_tokens_rejected(client):
    assert _bearer("sbp_totally-fake").get("/api/customers").status_code == 401
    created = _mint(client, "shortlived", "readonly")
    api = _bearer(created["token"])
    assert api.get("/api/customers").status_code == 200
    client.put(f"/api/tokens/{created['id']}", json={"is_active": False})
    r = api.get("/api/customers")
    assert r.status_code == 401
    # a revoked token is told so (#434); a secret that matches nothing is not
    assert "shortlived" in r.json()["detail"] and "revoked" in r.json()["detail"]
    fake = _bearer("sbp_totally-fake").get("/api/customers")
    assert fake.json()["detail"] == "Not authenticated"


def test_no_credentials_still_401(client):
    assert TestClient(app).get("/api/customers").status_code == 401


# ---------------------------------------------------------------------------
# Attribution + telemetry
# ---------------------------------------------------------------------------


def test_token_writes_attributed_in_audit(client, db_session):
    tok = _mint(client, "receipt-service", "bookkeeper")["token"]
    _bearer(tok).post("/api/customers", json={"name": "Attributed Bot Inc"})
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "customers")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.username == "token:receipt-service"


def test_last_used_recorded(client, db_session):
    created = _mint(client, "tracked", "readonly")
    assert (
        db_session.query(ApiToken).filter_by(label="tracked").one().last_used_at is None
    )
    _bearer(created["token"]).get("/api/customers")
    db_session.expire_all()
    assert (
        db_session.query(ApiToken).filter_by(label="tracked").one().last_used_at
        is not None
    )


def test_an_accountant_read_of_an_employee_link_is_refused_as_a_read(client):
    """The portal-token GET is an administrator's even as a read; the
    accountant's refusal used to say "cannot make this change" on a GET
    (skeptic pass on #660)."""
    acct = _bearer(_mint(client, "cpa", "accountant")["token"])
    r = acct.get("/api/employees/1/portal-token")
    assert r.status_code == 403, r.text
    assert "cannot make this change" not in r.json()["detail"]
    assert "administrator" in r.json()["detail"]
