"""The optional chime when a transaction posts (#476, from a QuickBooks
Desktop user who keys a stack and misses the toast).

- the per-user preference post_sound round-trips through the preferences
  routes as {"enabled": bool}, and the key passes the route's validation;
- the Settings page shows the per-user toggle and saves it through the
  preferences API, not the company-settings form;
- utils.js rings the chime only where a transaction posts (toastPosted),
  never from a plain toast; the posting paths call it, deletes and voids
  do not;
- a read-only or accountant sign-in, which cannot write preferences, does
  not see the toggle.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
UTILS_JS = (ROOT / "app/static/js/utils.js").read_text(encoding="utf-8")


def test_the_post_sound_preference_round_trips(client):
    assert client.get("/api/preferences/post_sound").json() == {
        "key": "post_sound",
        "value": None,
    }
    saved = client.put("/api/preferences/post_sound", json={"value": {"enabled": True}})
    assert saved.status_code == 200, saved.text
    assert client.get("/api/preferences/post_sound").json()["value"] == {
        "enabled": True
    }
    # turning it off keeps the row, with enabled false
    client.put("/api/preferences/post_sound", json={"value": {"enabled": False}})
    assert client.get("/api/preferences/post_sound").json()["value"] == {
        "enabled": False
    }
    # delete is back to the default (off)
    assert client.delete("/api/preferences/post_sound").status_code == 200
    assert client.get("/api/preferences/post_sound").json()["value"] is None


def test_the_settings_toggle_uses_the_preferences_api(client):
    assert 'id="post-sound-pref"' in SETTINGS_JS
    assert "SettingsPage.savePostSound(this.checked)" in SETTINGS_JS
    assert (
        "API.put('/preferences/post_sound', { value: { enabled: !!on } })"
        in SETTINGS_JS
    )
    assert "API.get('/preferences/post_sound')" in SETTINGS_JS


def test_the_chime_rings_only_where_a_transaction_posts():
    import re

    toast_fn = re.search(r"function toast\(message.*?\n}\n", UTILS_JS, re.S).group(0)
    assert "playPostSound" not in toast_fn  # a plain toast never rings
    assert "function toastPosted(message)" in UTILS_JS
    assert "if (opts.posted) playPostSound();" in UTILS_JS
    # guarded for a browser with no AudioContext
    assert "window.AudioContext || window.webkitAudioContext" in UTILS_JS


_POSTINGS = {
    "invoices.js": "toastPosted(Terms.text(id ? 'Invoice updated' : 'Invoice created'))",
    "bills.js": "toastPosted('Bill saved')",
    "payments.js": "toastPosted('Payment recorded')",
    "sales_receipts.js": "toastPosted(`${T('Sales Receipt')} #",
    "deposits.js": "toastPosted(`Deposited ${formatCurrency(total)}`)",
    "expenses.js": "toastPosted('Expense recorded')",
    "journal.js": "'Adjusting entry created' : 'Journal entry created')",
    "banking.js": "toastPosted(outside ? `Transfer posted. ${outside}` : 'Transfer posted')",
    "payroll.js": "toastPosted('Payroll processed')",
    "credit_memos.js": "toastPosted('Credit memo created')",
    "vendor_credits.js": "toastPosted('Vendor credit created')",
}
_SILENT = {
    "payments.js": "toast('Payment voided')",
    "deposits.js": "toast('Deposit voided; its payments are back on the list')",
    "journal.js": "toast('Journal entry voided')",
    "invoices.js": "toast(`Duplicated as ${T('Invoice')}",
    "expenses.js": "toast('Attachment deleted')",
}


def test_the_posting_paths_ring_and_the_rest_stay_silent():
    js = ROOT / "app/static/js"
    for name, call in _POSTINGS.items():
        src = (js / name).read_text(encoding="utf-8")
        assert call in src, name
    assert "toastPosted(form.is_adjusting" in (js / "journal.js").read_text(
        encoding="utf-8"
    )
    # the register's quick row and its entry form, each saying in the same
    # toast when the entry is dated outside the register's dates (#663)
    banking = (js / "banking.js").read_text(encoding="utf-8")
    assert (
        banking.count(
            "toastPosted(outside ? `Entry posted. ${outside}` : 'Entry posted')"
        )
        == 2
    )
    for name, call in _SILENT.items():
        src = (js / name).read_text(encoding="utf-8")
        assert call in src, name


def test_every_sign_in_sees_the_toggle():
    # every signed-in role writes its own preferences since #480, so the
    # Sounds section is no longer hidden from read-only and accountant
    assert 'id="settings-sounds"' in SETTINGS_JS
    body = SETTINGS_JS[SETTINGS_JS.index("async loadPostSoundPref() {") :]
    body = body[: body.index("\n    },\n")]
    assert "isLocked" not in body


def test_the_toggle_primes_audio_inside_its_change_handler():
    body = SETTINGS_JS[SETTINGS_JS.index("async savePostSound(on) {") :]
    body = body[: body.index("\n    },\n")]
    assert body.index("primePostAudio()") < body.index("await API.put")
