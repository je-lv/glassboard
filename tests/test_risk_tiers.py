"""UI/UX analysis 2026-11-05, phase 3 (findings F01, F02): confirmations follow the risk of the action, and
an expired token reads "expired" with no Revoke."""
import glob
import os
import pathlib
import re

from test_admin_console import _owner
from test_dashboard import login
from test_tasks import api

TEMPLATES = pathlib.Path(__file__).parents[1] / "src" / "glassboard" / "templates"
# Danger buttons that do not use the shared dialog, and why.
EXEMPT = {
    ("home.html", 'value="deny"'): "deny is one step with an optional note (owner decision 2026-11-05)",
    ("owner.html", "revoke-open"): "opens the admin token dialog, which is the confirmation",
    ("owner.html", ">Revoke token"): "the confirm button inside that dialog",
    ("owner.html", ">Purge live workspace"): "typed name plus the off-host-copy checkbox",
}


def test_every_danger_button_confirms_or_has_a_recorded_exemption():
    missing = []
    for f in sorted(glob.glob(str(TEMPLATES / "*.html"))):
        text = open(f, encoding="utf-8").read()
        for m in re.finditer(r'<button[^>]*class="[^"]*\bdanger\b[^"]*"[^>]*>[^<]*', text):
            tag = m.group(0)
            if "data-confirm" in tag:
                continue
            if not any(os.path.basename(f) == name and needle in tag for name, needle in EXEMPT):
                missing.append((os.path.basename(f), tag[:120]))
    assert missing == []


def test_grant_asks_first_and_deny_does_not(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    api(gb, "POST", "/messages", "jose/claude", {"kind": "approval", "body": "May I publish?", "to": ["jose"]})
    page = login(gb).get("/").text
    grant = re.search(r'<button[^>]*value="grant"[^>]*>', page).group(0)
    deny = re.search(r'<button[^>]*value="deny"[^>]*>', page).group(0)
    assert 'data-confirm="Grant approval request #' in grant and 'data-confirm-yes="Grant approval"' in grant
    assert "data-confirm" not in deny
    assert 'id="confirm-dialog"' in page and '<script src="/static/confirm.js"></script>' in page
    js = (TEMPLATES.parent / "static" / "confirm.js").read_text(encoding="utf-8")
    assert "textContent" in js and "innerHTML" not in js and "keep.focus()" in js


def test_expired_token_reads_expired_and_offers_no_revoke(gb):
    owner = _owner(gb)
    gb.raw.execute("UPDATE credential SET expires_at='2026-01-01T00:00:00.000000Z' WHERE principal_id="
                   "(SELECT id FROM principal WHERE handle='jose')")
    html = owner.get("/admin?tab=credentials").text
    row = next(r for r in html.split("<tr") if "<strong>@jose</strong>" in r).split("</tr>")[0]
    assert ">expired</span>" in row and "Revoke" not in row and ">active<" not in row
    assert ">active</span>" in html                       # the owner's own live token
    agents = login(gb, "owner")
    assert agents.get("/agents").status_code == 200
