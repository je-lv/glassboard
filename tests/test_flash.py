"""B34 (owner fix F1): every dashboard action tells the user whether it worked, with a toast."""
import re

from glassboard import flash
from test_admin_console import _owner
from test_dashboard import csrf_of, login

HOST = "testserver"


def _toast(page: str, kind: str) -> str | None:
    m = re.search(rf'<div class="toast toast-{kind}" role="(?:status|alert)">.*?<span class="toast-text">(.*?)</span>',
                  page, re.S)
    return m.group(1) if m else None


def test_b34_every_dashboard_post_route_reports_its_outcome(gb):
    routes = {r.path for r in gb.client.app.routes
              if "POST" in getattr(r, "methods", set()) and not r.path.startswith(("/api/", "/mcp"))}
    covered = set(flash.ROUTE_OK) | flash.NO_TOAST | flash.RESULT_PAGE
    assert routes - covered == set(), "add a success code to flash.ROUTE_OK (or NO_TOAST / RESULT_PAGE)"
    assert set(flash.ROUTE_OK.values()) | set(flash.TASK_OP_OK.values()) <= set(flash.OK)


def test_b34_admin_token_revoke_shows_green_toast(gb):
    owner = _owner(gb)
    csrf = csrf_of(owner.get("/admin?tab=credentials").text)
    issued = owner.post("/admin/credentials/create", data={"csrf": csrf, "handle": "jose", "scope": "read",
                                                           "days": "30", "label": "tablet"})
    cid = re.search(r"gbt_([a-z2-7]{10})_", issued.text).group(1)
    r = owner.post("/admin/credentials/revoke", data={"csrf": csrf, "credential": cid, "confirm": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/admin?tab=credentials&ok=token_revoked"
    page = owner.get(r.headers["location"]).text
    assert _toast(page, "ok") == "Token revoked. It no longer works."
    assert 'data-clean="/admin?tab=credentials"' in page and '<script src="/static/toast.js"></script>' in page


def test_b34_admin_failure_returns_to_the_console_with_red_toast(gb):
    owner = _owner(gb)
    page = owner.get("/admin?tab=access").text
    referer = f"http://{HOST}/admin?tab=access"
    r = owner.post("/admin/access/change", headers={"referer": referer, "accept": "text/html"},
                   data={"csrf": csrf_of(page), "handle": "owner", "confirm": "owner", "action": "deactivate"})
    assert r.status_code == 303 and r.headers["location"] == "/admin?tab=access&err=self_access"
    shown = owner.get(r.headers["location"]).text
    assert _toast(shown, "err") == "You cannot remove your own admin access here."
    assert 'role="alert"' in shown


def test_b34_admin_access_change_names_the_action(gb):
    owner = _owner(gb)
    assert gb.admin("add-human", "reviewer")[0] == 0
    csrf = csrf_of(owner.get("/admin?tab=access").text)
    r = owner.post("/admin/access/change", data={"csrf": csrf, "handle": "reviewer", "confirm": "reviewer",
                                                 "action": "deactivate"})
    assert r.headers["location"].endswith("ok=deactivated")


def test_b34_toast_text_never_comes_from_the_url(gb):
    c = login(gb)
    assert _toast(c.get("/?ok=token_revoked<script>").text, "ok") is None
    assert _toast(c.get("/?ok=no_such_code").text, "ok") is None
    page = c.get("/?err=%3Cscript%3Ealert(1)%3C/script%3E").text
    assert _toast(page, "err") == flash.DEFAULT_ERR and "<script>alert" not in page
    assert _toast(c.get("/").text, "ok") is None and 'id="toasts"' not in c.get("/").text


def test_b34_redirect_helpers():
    assert flash.with_code("/s/a#team", "ok", "done") == "/s/a?ok=done#team"
    assert flash.with_code("/s/a?x=1&err=forbidden", "ok", "done") == "/s/a?x=1&err=forbidden"
    assert flash.back_with_error(f"http://{HOST}/admin?tab=access&ok=done", HOST, "last_admin") == \
        "/admin?tab=access&err=last_admin"
    assert flash.back_with_error("http://evil.example/admin", HOST, "x") is None
    assert flash.back_with_error(f"http://{HOST}/api/v1/whoami", HOST, "x") is None
    assert flash.back_with_error("", HOST, "x") is None
    assert flash.back_with_error(f"http://{HOST}/", HOST, "BAD CODE") == "/?err=invalid_request"


def test_b34_task_operations_name_what_happened(gb):
    assert set(flash.TASK_OP_OK) >= {"acknowledge", "review", "accept", "return", "reassign", "cancel", "reopen",
                                     "release", "edit"}
    assert flash.ok_code("/s/{s}/t/{n}/{op}", {"op": "accept"}, None) == "task_accepted"
    assert flash.ok_code("/login", {}, None) is None
    assert flash.ok_code("/agents", {}, "token_revoked") == "token_revoked"
