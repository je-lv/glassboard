"""Dashboard v0 (XSS corpus inert; missing CSRF → 403; CSP header present) and
GET /metrics admin-only."""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from glassboard import admin_auth, db, ids
from glassboard.dashboard import COOKIE, CSP, LOGIN_COOKIE

XSS = [
    "<script>alert(1)</script>",
    "\"><img src=x onerror=alert(1)>",
    "</div><script src=//evil.example/x.js></script>",
    "javascript:alert(1)",
    "{{ 7*7 }} {% raw %}",
    "<svg onload=alert(1)>",
    "\u202eevil.exe\u2066txt.\u2069",
    "&lt;already&gt;",
]


def browser(gb):
    return TestClient(gb.app, base_url="https://testserver", raise_server_exceptions=False, follow_redirects=False)


def login(gb, who="jose"):
    c = browser(gb)
    r = c.get("/login")
    nonce = r.cookies.get(LOGIN_COOKIE) or c.cookies.get(LOGIN_COOKIE)
    r = c.post("/login", data={"csrf": nonce, "token": gb.tok[who]})
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    return c


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def d(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    return gb


# --- login and web sessions -------------------------------------------------------------------

def test_public_docs_guide_is_available_without_login(gb):
    r = browser(gb).get("/docs")
    assert r.status_code == 200
    assert 'id="overview"' in r.text and 'id="troubleshooting"' in r.text
    assert r.headers["content-security-policy"] == CSP


def test_login_sets_a_hardened_cookie(d):
    c = browser(d)
    r = c.get("/login")
    assert r.headers["content-security-policy"] == CSP
    nonce = c.cookies.get(LOGIN_COOKIE)
    r = c.post("/login", data={"csrf": nonce, "token": d.tok["jose"]})
    assert r.status_code == 303
    setc = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith(COOKIE)][0]
    for attr in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attr.lower() in setc.lower(), setc
    assert "domain" not in setc.lower()
    sid = setc.split(";")[0].split("=", 1)[1]
    stored = d.raw.execute("SELECT id_sha256 FROM web_session").fetchone()[0]
    assert len(stored) == 32 and sid.encode() not in bytes(stored)          # only a hash is stored
    assert d.tok["jose"] not in r.text


def test_admin_password_login_keeps_a_revocable_browser_session_for_thirty_days(d):
    assert d.admin("add-human", "owner", "--admin")[0] == 0
    d.raw.execute("INSERT INTO admin_password(principal_id,salt,password_hash,changed_at) VALUES (?,?,?,?)",
                  (d.pid("owner"), b"test-salt-123456", admin_auth._derive("a long admin password", b"test-salt-123456"),
                   "2026-01-01T00:00:00.000000Z"))
    c = browser(d)
    c.get("/admin/login")
    r = c.post("/admin/login", data={"csrf": c.cookies[LOGIN_COOKIE], "username": "owner",
                                     "password": "a long admin password", "remember": "1"})
    assert r.status_code == 303
    row = d.raw.execute("SELECT created_at, expires_at FROM web_session WHERE principal_id=?", (d.pid("owner"),)).fetchone()
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert (expires - created).days == 30
    assert c.get("/admin").status_code == 200
    assert c.get("/").status_code == 200
    assert d.admin("revoke-web-sessions", "owner")[0] == 0
    assert c.get("/").status_code == 303


def test_admin_password_change_invalidates_older_sessions(d):
    assert d.admin("add-human", "owner", "--admin")[0] == 0
    d.raw.execute("INSERT INTO admin_password(principal_id,salt,password_hash,changed_at) VALUES (?,?,?,?)",
                  (d.pid("owner"), b"test-salt-123456", admin_auth._derive("a long admin password", b"test-salt-123456"),
                   "2026-01-01T00:00:00.000000Z"))
    c = browser(d)
    assert c.get("/admin").headers["location"] == "/admin/login"
    c.get("/admin/login")
    assert c.post("/admin/login", data={"csrf": c.cookies[LOGIN_COOKIE], "username": "owner",
                                        "password": "wrong"}).headers["location"] == "/admin/login?e=1"
    c.get("/admin/login")
    assert c.post("/admin/login", data={"csrf": c.cookies[LOGIN_COOKIE], "username": "owner",
                                        "password": "a long admin password"}).status_code == 303
    csrf = csrf_of(c.get("/admin").text)
    assert c.post("/admin/password", data={"csrf": csrf, "current_password": "a long admin password",
                                           "new_password": "a different long admin password"}).status_code == 303
    assert c.get("/admin").headers["location"] == "/admin/login"
    c.get("/admin/login")
    assert c.post("/admin/login", data={"csrf": c.cookies[LOGIN_COOKIE], "username": "owner",
                                        "password": "a different long admin password"}).status_code == 303


def test_owner_console_lists_and_joins_a_workspace_with_one_event(d):
    d.session("other", who="ana")
    assert d.admin("add-human", "owner", "--admin")[0] == 0
    d.tok["owner"] = d.mint("owner")
    c = login(d, "owner")
    page = c.get("/owner")
    assert page.status_code == 200
    assert "other" in page.text and "Join as Session Manager" in page.text
    assert c.get("/admin/s/other").status_code == 200
    d.raw.execute("UPDATE session SET state='archived' WHERE handle='other'")
    assert "archived" in c.get("/admin/s/other").text
    d.raw.execute("UPDATE session SET state='active' WHERE handle='other'")
    assert login(d).get("/admin/s/other").status_code == 404
    assert login(d).get("/owner").status_code == 404
    head = d.raw.execute("SELECT head_seq FROM session WHERE handle='other'").fetchone()[0]
    assert c.post("/owner/s/other/join", data={}).status_code == 403
    import re
    form = re.search(r'<form method="post" action="/owner/s/other/join".*?</form>', page.text, re.S).group()
    values = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', form))
    result = c.post("/owner/s/other/join", data=values)
    assert result.status_code == 303 and result.headers["location"] == "/s/other"
    assert c.get("/s/other").status_code == 200
    event = d.raw.execute("SELECT type, data FROM event WHERE session_id=(SELECT id FROM session WHERE handle='other') "
                          "ORDER BY seq DESC LIMIT 1").fetchone()
    assert event["type"] == "member.added" and "Admin console join" in event["data"]
    assert "Admin console join" in c.get("/s/other").text
    assert d.raw.execute("SELECT head_seq FROM session WHERE handle='other'").fetchone()[0] == head + 1
    assert c.post("/owner/s/other/join", data=values).status_code == 303
    assert d.raw.execute("SELECT head_seq FROM session WHERE handle='other'").fetchone()[0] == head + 1


def test_admin_console_inspects_a_live_external_board_read_only(d):
    assert d.admin("add-human", "owner", "--admin")[0] == 0
    d.tok["owner"] = d.mint("owner")
    external_path = str(d.tmp / "external.db")
    external = db.Database(external_path)
    external.migrate()
    with external.write() as c:
        c.execute("INSERT INTO session(id,handle,title,state,recovery_id,protocol_version,created_at,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (db.new_id(), "external-run", "Live external run", "active",
                                            "ABCDEFGHIJKLMNOP", "1", db.utcnow(), db.utcnow()))
    d.app.state.admin_external_dbs = [external_path]
    c = login(d, "owner")
    try:
        assert "external-run" in c.get("/admin").text
        page = c.get("/admin/s/external-run")
        assert page.status_code == 200 and "Live read-only view" in page.text
        assert "Join as Session Manager" not in c.get("/admin").text.split("external-run", 1)[1].split("</li>", 1)[0]
        with external.write() as conn:
            conn.execute("UPDATE session SET title='Updated while open' WHERE handle='external-run'")
        assert "Updated while open" in c.get("/admin/s/external-run").text
        assert login(d).get("/admin/s/external-run").status_code == 404
    finally:
        external.close()


def test_browser_can_mint_and_revoke_owned_token_without_leaking_it_in_url(d):
    c = login(d)
    page = c.get("/agents?tab=tokens")
    assert page.status_code == 200 and "Your access tokens" in page.text
    csrf = csrf_of(page.text)
    data = {"csrf": csrf, "op": ids.new_ulid(), "principal": "jose/claude", "scope": "read",
            "expires_days": "30", "label": "review tablet"}
    issued = c.post("/agents/token", data=data)
    assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
    assert "gbt_" in issued.text and "review tablet" not in issued.headers.get("location", "")
    assert c.post("/agents/token", data=data).status_code == 200
    assert "cannot be shown again" in c.post("/agents/token", data=data).text
    row = d.raw.execute("SELECT id, scope, label FROM credential WHERE principal_id=? ORDER BY created_at DESC LIMIT 1",
                        (d.pid("jose/claude"),)).fetchone()
    assert row["scope"] == "read" and row["label"] == "review tablet"
    revoke = c.post("/agents", data={"csrf": csrf, "op": ids.new_ulid(),
                                      "credential": row["id"], "action": "revoke"})
    assert revoke.status_code == 303
    assert d.raw.execute("SELECT revoked_at FROM credential WHERE id=?", (row["id"],)).fetchone()[0]


def test_remember_request_does_not_extend_nonadmin_login(d):
    c = browser(d)
    c.get("/login")
    r = c.post("/login", data={"csrf": c.cookies[LOGIN_COOKIE], "token": d.tok["jose"], "remember": "1"})
    assert r.status_code == 303
    row = d.raw.execute("SELECT created_at, expires_at FROM web_session WHERE principal_id=?", (d.pid("jose"),)).fetchone()
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert (expires - created).total_seconds() == 12 * 3600


def test_read_token_stays_read_only_in_browser_and_revocation_ends_session(d):
    read_token = d.mint("ana", "--scope", "read")
    d.tok["ana"] = read_token
    c = login(d, "ana")
    page = c.get("/s/s1")
    assert page.status_code == 200
    assert "Read-only access" in page.text and re.search(r'<body class="[^"]*\bread-only\b', page.text)
    assert 'id="dismiss-read-only"' in page.text and 'aria-label="Dismiss read-only notice"' in page.text
    assert page.text.index('/static/banner-preload.js') < page.text.index('/static/app.css')
    assert c.get('/static/banner-preload.js').status_code == 200
    csrf = csrf_of(page.text)
    head = d.head()
    assert c.post("/s/s1/post", data={"csrf": csrf, "recovery": d.rec(), "op": ids.new_ulid(),
                                     "kind": "note", "body": "should not post"}).status_code == 403
    assert c.post("/new-session", data={"csrf": csrf, "handle": "not-created", "title": "No"}).status_code == 403
    assert d.head() == head
    assert c.get("/s/s1/board").status_code == 200
    cid = read_token.split("_")[1]
    assert d.admin("revoke-token", cid)[0] == 0
    r = c.get("/s/s1")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_read_token_can_log_out(d):
    d.tok["ana"] = d.mint("ana", "--scope", "read")
    c = login(d, "ana")
    assert c.post("/logout", data={"csrf": csrf_of(c.get("/s/s1").text)}).status_code == 303


def test_login_refuses_agents_bad_tokens_and_missing_nonce(d):
    c = browser(d)
    c.get("/login")
    nonce = c.cookies.get(LOGIN_COOKIE)
    for tok in (d.tok["jose/claude"], "gbt_aaaaaaaaaa_" + "A" * 43, ""):
        r = c.post("/login", data={"csrf": nonce, "token": tok})
        assert r.status_code == 303 and r.headers["location"] == "/login?e=1"
    assert d.raw.execute("SELECT count(*) FROM web_session").fetchone()[0] == 0
    fresh = browser(d)
    assert fresh.post("/login", data={"token": d.tok["jose"]}).status_code == 403            # no nonce
    fresh.get("/login")
    assert fresh.post("/login", data={"csrf": "wrong", "token": d.tok["jose"]}).status_code == 403


def test_pages_need_a_login(d):
    c = browser(d)
    for path in ("/", "/s/s1", "/s/s1/timeline", "/s/s1/agent?as=ana"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert c.post("/s/s1/post", data={"body": "x"}).status_code == 403


def test_logout_and_deactivation_end_the_web_session(d):
    c = login(d)
    page = c.get("/s/s1").text
    r = c.post("/logout", data={"csrf": csrf_of(page)})
    assert r.status_code == 303 and c.get("/s/s1").status_code == 303
    c = login(d)
    d.admin("deactivate", "jose")
    assert c.get("/s/s1").status_code == 303


def test_admin_revokes_web_sessions_through_the_journal(d):
    c = login(d)
    assert c.get("/s/s1").status_code == 200
    rc, out = d.admin("revoke-web-sessions", "jose")
    assert rc == 0 and "revoked 1 web session" in out.out
    assert c.get("/s/s1").status_code == 303
    import json
    e = json.loads(open(d.app.state.journal_path).read().splitlines()[-1])
    assert e["action"] == "web_session.revoke_all" and e["principal_id"] == d.pid("jose")


def test_non_members_get_404(d):
    d.session("other", who="ana")
    c = login(d, "jose")
    for path in ("/s/other", "/s/other/timeline", "/s/other/agent?as=ana", "/s/nope"):
        assert c.get(path).status_code == 404, path


# --- CSP, CSRF -------------------------------------------------------------------------------------

def test_csp_and_security_headers_on_every_page(d):
    c = login(d)
    for path in ("/", "/s/s1", "/s/s1/timeline", "/s/s1/agent?as=ana", "/login"):
        r = c.get(path)
        assert r.headers["content-security-policy"] == CSP, path
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert "<style" not in r.text and " style=" not in r.text and "onclick" not in r.text, path
        # Only the known local scripts for this page; never inline or remote code.
        scripts = re.findall(r"<script[^>]*>", r.text)
        expected = {
            "/": ["panels.js", "notify.js"],
            "/s/s1": ["poll.js", "workbench.js", "notify.js"],
            "/s/s1/timeline": [],
            "/s/s1/agent?as=ana": ["notify.js"],
            "/login": [],
        }[path]
        expected = [f'<script src="/static/{name}">' for name in expected]
        assert scripts == expected, path


def test_missing_or_wrong_csrf_is_403(d):
    c = login(d)
    html = c.get("/s/s1").text
    base = {"kind": "note", "body": "hi", "recovery": d.rec(), "op": ids.new_ulid()}
    head = d.head()
    assert c.post("/s/s1/post", data=base).status_code == 403
    assert c.post("/s/s1/post", data={**base, "csrf": "x" * 43}).status_code == 403
    assert c.post("/s/s1/post", data={**base, "csrf": csrf_of(html)},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/logout", data={}).status_code == 403
    assert d.head() == head
    r = c.post("/s/s1/post", data={**base, "csrf": csrf_of(html)}, headers={"Origin": "https://testserver"})
    assert r.status_code == 303 and d.head() == head + 1


# --- posting ------------------------------------------------------------------------------------

def test_post_box_posts_as_the_human_with_recovery_and_idempotency(d):
    c = login(d)
    html = c.get("/s/s1").text
    op = re.search(r'name="op" value="([^"]+)"', html).group(1)
    form = {"csrf": csrf_of(html), "recovery": d.rec(), "op": op, "kind": "decision", "body": "Port 8443 is final.", "to": ""}
    assert c.post("/s/s1/post", data=form).status_code == 303
    e = d.events()[-1]
    assert (e["type"], e["actor_id"], e["body"], e["broadcast"], e["op_id"]) == ("decision", d.pid("jose"),
                                                                                 "Port 8443 is final.", 1, op)
    head = d.head()
    assert c.post("/s/s1/post", data=form).status_code == 303 and d.head() == head     # double submit = replay
    r = c.post("/s/s1/post", data={**form, "op": ids.new_ulid(), "kind": "note", "to": "nobody"})
    assert r.status_code == 303 and r.headers["location"].endswith("err=recipient_not_member")
    assert "Some recipients are not members" in c.get(r.headers["location"]).text


def test_stale_form_after_restore_says_reload(d):
    c = login(d)
    html = c.get("/s/s1").text
    form = {"csrf": csrf_of(html), "recovery": d.rec(), "op": ids.new_ulid(), "kind": "note", "body": "late"}
    d.raw.execute("UPDATE session SET recovery_id=? WHERE handle='s1'", (ids.new_random_b32(16),))
    head = d.head()
    r = c.post("/s/s1/post", data=form)
    assert r.status_code == 409 and "Reload" in r.text and d.head() == head
    assert r.headers["content-security-policy"] == CSP


def test_unknown_error_codes_in_the_url_show_no_attacker_text(d):
    c = login(d)
    r = c.get("/s/s1?err=%3Cscript%3EPWN")
    assert "PWN" not in r.text
    assert "That did not work." in r.text                     # the fixed default message


# --- the XSS corpus is inert ---------------------------------------------------------------------

def test_at_5_6_xss_corpus_is_inert(d):
    d.admin("add-agent", "ana", "evil", "--display-name", "<script>alert('dn')</script>\u202e")
    d.tok["ana/evil"] = d.mint("ana/evil")
    d.add("s1", "ana/evil")
    for i, x in enumerate(XSS):
        d.req("POST", "/api/v1/sessions/s1/messages", "ana", json={"kind": "note", "body": x, "to": ["jose"],
              "refs": [{"label": x[:100], "loc": x[:100], "access": "shared"}]}, rec=d.rec())
    d.req("POST", "/api/v1/sessions", "ana", json={"handle": "xss", "title": XSS[0] + XSS[1]})
    d.req("POST", "/api/v1/sessions/xss/members", "ana", json={"handle": "jose"}, rec=d.rec("xss"))
    c = login(d)
    pages = [c.get(p) for p in ("/", "/s/s1", "/s/s1/timeline", "/s/s1/agent?as=jose", "/s/s1?f=me", "/s/xss")]
    for r in pages:
        assert r.status_code == 200
        t = r.text
        assert "<script>alert" not in t and "<img" not in t and "<svg onload" not in t, r.url
        assert "evil.example/x.js" not in t or "&lt;script src=//evil.example/x.js&gt;" in t
        assert "onerror=alert" not in t or "&#34;&gt;&lt;img src=x onerror=alert(1)&gt;" in t
        assert "\u202e" not in t and "\u2066" not in t                       # bidi never raw...
    s1 = pages[1].text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in s1                  # ...content shown, escaped
    assert "⟨U+202E⟩evil.exe" in s1                             # bidi markers visible
    assert "{{ 7*7 }}" in s1 and ">49<" not in s1                          # no template evaluation
    assert "&lt;script&gt;alert(&#39;dn&#39;)&lt;/script&gt;" in s1      # display_name sanitized, then escaped
    assert re.findall(r"<script[^>]*>", s1) == ['<script src="/static/poll.js">',
                                                   '<script src="/static/workbench.js">',
                                                   '<script src="/static/notify.js">']


# --- read-only views -----------------------------------------------------------------------------

def test_dashboard_never_moves_cursors(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "ana", json={"kind": "note", "body": "for jose", "to": ["jose"]}, rec=d.rec())
    before = d.raw.execute("SELECT principal_id, cursor_seq, delivered_seq, consumer_gen FROM member ORDER BY principal_id").fetchall()
    c = login(d)
    page = c.get("/s/s1").text
    assert "unread for you: 1" in page                     # only ana's note is routed to jose
    for p in ("/s/s1/timeline", "/s/s1/agent?as=jose", "/s/s1/agent?as=jose/claude", "/s/s1?f=me"):
        assert c.get(p).status_code == 200
    after = d.raw.execute("SELECT principal_id, cursor_seq, delivered_seq, consumer_gen FROM member ORDER BY principal_id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_overdue_assignment_explains_sync_without_claiming_work_started(d, monkeypatch):
    r = d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={
        "title": "Draft guide", "scope": "Write the guide", "deliverable": "A reviewed guide",
        "assignee": "jose/claude"}, rec=d.rec())
    assert r.status_code == 201
    n = r.json()["task"]
    monkeypatch.setattr("glassboard.api_tasks._age_s", lambda ts: 360)
    c = login(d)
    before = c.get(f"/s/s1/t/{n}").text
    assert "No board sync recorded for the assignee since assignment" in before
    assert "Check with the assignee or reassign the task if needed" in before
    assert "no sync since assignment" in c.get("/s/s1/board").text
    assert "no board sync since assignment" in c.get("/").text
    attention = d.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"]
    overdue = next(item for item in attention if item["kind"] == "unacknowledged")
    assert overdue["assignee"] == "jose/claude"
    assert overdue["assignee_synced_since_assignment"] is False
    attention_text = d.req("GET", "/api/v1/me/attention?format=text", "jose", key=None).text
    assert "  · assignee: «@jose/claude»" in attention_text
    assert "--- board sync since assignment: no ---" in attention_text
    assert d.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={}, rec=d.rec()).status_code == 200
    after = c.get(f"/s/s1/t/{n}").text
    assert "Assignee has synced with this board since assignment" in after
    assert "but has not acknowledged" in after
    assert " · synced" in c.get("/s/s1/board").text
    assert "synced, no acknowledgment" in c.get("/").text


def test_agent_view_matches_what_sync_then_shows(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "question", "body": "ready?", "to": ["jose/claude"]},
          rec=d.rec())
    c = login(d)
    view = c.get("/s/s1/agent?as=jose/claude").text
    assert "  | ready?" in view and "owed: #" in view and "consumer none" in view
    r = d.req("POST", "/api/v1/sessions/s1/sync?format=text", "jose/claude", json={}, rec=d.rec())
    for needle in ("  | ready?", "owed: #", "inbox: "):
        assert needle in r.text


def test_filters(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "decision", "body": "DECIDED"}, rec=d.rec())
    d.req("POST", "/api/v1/sessions/s1/messages", "ana", json={"kind": "question", "body": "ASKED", "to": ["jose"]}, rec=d.rec())
    c = login(d)
    assert "DECIDED" in c.get("/s/s1?f=decisions").text and "ASKED" not in c.get("/s/s1/timeline?f=decisions").text
    assert "ASKED" in c.get("/s/s1/timeline?f=questions").text
    assert "ASKED" in c.get("/s/s1/timeline?f=me").text and "DECIDED" not in c.get("/s/s1/timeline?f=me").text


def test_activity_order_survives_filters_and_fragment_refresh(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "decision", "body": "FIRST"}, rec=d.rec())
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "decision", "body": "SECOND"}, rec=d.rec())
    c = login(d)
    cursors_before = [tuple(r) for r in d.raw.execute(
        "SELECT principal_id, cursor_seq, delivered_seq, consumer_gen FROM member ORDER BY principal_id")]
    oldest = c.get("/s/s1/timeline?f=decisions&order=oldest").text
    newest = c.get("/s/s1/timeline?f=decisions&order=newest").text
    assert oldest.index("FIRST") < oldest.index("SECOND")
    assert newest.index("SECOND") < newest.index("FIRST")
    invalid = c.get("/s/s1/timeline?f=decisions&order=invalid").text
    assert invalid.index("FIRST") < invalid.index("SECOND")
    assert 'aria-current="true">newest' in newest
    page = c.get("/s/s1?f=decisions&order=newest").text
    assert 'data-src="/s/s1/timeline?f=decisions&amp;order=newest"' in page
    assert 'href="/s/s1?f=questions&amp;order=newest"' in page
    assert 'name="order" value="newest"' in page
    assert [tuple(r) for r in d.raw.execute(
        "SELECT principal_id, cursor_seq, delivered_seq, consumer_gen FROM member ORDER BY principal_id")] == cursors_before


def test_activity_identifies_actor_and_explains_event_colors(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "jose/claude", json={"kind": "note", "body": "CLAUDE NOTE"}, rec=d.rec())
    html = login(d).get("/s/s1/timeline").text
    assert 'class="actor-badge actor-claude"' in html
    assert 'title="@jose/claude"' in html
    assert '<span>Claude</span>' in html
    assert 'aria-label="Activity color key"' in html
    for label in ("Session", "Team / new task", "Task activity", "Submit / review / return",
                  "Accepted", "Blocked / cancelled", "System", "Message / other"):
        assert label in html


# --- /metrics -----------------------------------------------------------------------------------------

def test_metrics_are_admin_only_and_count(d):
    assert d.client.get("/metrics").status_code == 401
    assert d.client.get("/metrics", headers=d.headers("jose", key=None)).status_code == 403
    d.admin("add-human", "root", "--admin")
    d.tok["root"] = d.mint("root")
    k = ids.new_ulid()
    for _ in range(2):
        d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "note", "body": "x"}, rec=d.rec(), key=k)
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "note", "body": "x"}, rec="0" * 16)
    d.req("POST", "/api/v1/sessions/s1/members", "ana", json={"handle": "jose/claude"}, rec=d.rec())
    d.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={}, rec=d.rec())
    r = d.client.get("/metrics", headers=d.headers("root", key=None))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    # two 403s: jose reading /metrics, and ana (not a coordinator) changing membership
    for line in ("glassboard_replay_total 1", "glassboard_recovery_mismatch_total 1", "glassboard_authz_denied_total 2",
                 'glassboard_events_total{type="note"} 1', "glassboard_consumer_takeover_total 0", "glassboard_stale_tasks 0"):
        assert line in t, line
    assert 'glassboard_request_duration_seconds_count{method="POST",route="/api/v1/sessions/{s}/messages"}' in t
    assert "glassboard_sync_bytes_count 1" in t
    assert all(re.fullmatch(r"(# TYPE .*|glassboard_[a-z_]+(\{[a-z_]+=\"[^\"]*\"(,[a-z_]+=\"[^\"]*\")*\})? [0-9.e+-]+)", ln)
               for ln in t.splitlines()), t


def test_real_browser_origin_headers(d):
    """Browsers send `Origin: <scheme>://<host>` on same-origin form POSTs only when the referrer policy
    allows it: under `no-referrer` they send `Origin: null`, which the CSRF check refuses. Found with a
    real Chrome: login failed. The policy is same-origin, and a same-origin Origin is accepted."""
    c = browser(d)
    r = c.get("/login")
    assert r.headers["referrer-policy"] == "same-origin" and 'content="same-origin"' in r.text
    nonce = c.cookies.get(LOGIN_COOKIE)
    r = c.post("/login", data={"csrf": nonce, "token": d.tok["jose"]}, headers={"Origin": "https://testserver"})
    assert r.status_code == 303
    c2 = browser(d)
    c2.get("/login")
    r = c2.post("/login", data={"csrf": c2.cookies.get(LOGIN_COOKIE), "token": d.tok["jose"]}, headers={"Origin": "null"})
    assert r.status_code == 403                                                    # an opaque origin is never ours


def test_sessions_and_members_from_the_dashboard(d):
    """A human creates a session from the home page and manages its members from the session page;
    the same API code paths run (events, last-coordinator guard, journaled removals)."""
    c = login(d, "jose")
    home = c.get("/").text
    form = {"csrf": csrf_of(home), "op": "dash-ns-0001", "handle": "test2", "title": "Second agent test"}
    r = c.post("/new-session", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/s/test2"
    assert c.post("/new-session", data={**form, "op": "dash-ns-0002"}).headers["location"] == "/?err=handle_taken"
    assert "A session with that handle already exists" in c.get("/?err=handle_taken").text
    assert "Manage team access and project roles" in c.get("/s/test2").text
    page = c.get("/s/test2/team").text
    assert '<option>jose/claude</option>' in page and "Add an existing participant" in page
    base = {"csrf": csrf_of(page), "recovery": d.rec("test2")}
    r = c.post("/s/test2/members", data={**base, "op": "dash-m-0001", "handle": "jose/claude", "role": "member", "action": "add"})
    assert r.status_code == 303 and r.headers["location"] == "/s/test2"
    r = c.post("/s/test2/members", data={**base, "op": "dash-m-0002", "handle": "jose/claude", "role": "observer", "action": "role"})
    assert r.status_code == 303
    assert d.raw.execute("SELECT m.role FROM member m JOIN principal p ON p.id=m.principal_id JOIN session s ON "
                         "s.id=m.session_id WHERE s.handle='test2' AND p.handle='jose/claude'").fetchone()[0] == "observer"
    r = c.post("/s/test2/members", data={**base, "op": "dash-m-0003", "handle": "jose/claude", "action": "remove"})
    assert r.status_code == 303
    assert [e["type"] for e in d.events("test2")] == ["session.created", "member.added", "member.changed", "member.removed"]
    r = c.post("/s/test2/members", data={**base, "op": "dash-m-0004", "handle": "jose", "action": "remove"})
    assert r.headers["location"] == "/s/test2?err=invalid_transition"                  # never the last human coordinator
