"""The coordination pack: approval requests, peer review, structured handoff, effort, advisory
leases (with expiry), and flow metrics."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from test_board import b, mk, view  # noqa: F401  (fixture and helpers)
from test_render import assert_canonical
from test_tasks import api, claim

FENCE = re.compile(r"T\d+g\d+-[a-z2-7]{10}")


def text_of(gb, path, who="jose"):
    r = gb.req("GET", f"/api/v1/sessions/s1{path}{'&' if '?' in path else '?'}format=text", who, key=None)
    assert r.status_code == 200, r.text
    return r.text


# --- approval requests -----------------------------------------------------------------------------------

def test_approval_request_is_owed_by_the_named_human_until_decided(b):
    r = api(b, "POST", "/messages", "jose/claude", {"kind": "approval", "body": "May I run the full test suite?",
                                                   "to": ["jose"]})
    assert r.status_code == 201, r.text
    seq = r.json()["seq"]
    att = b.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"]
    assert {"kind": "approval", "seq": seq, "text": "May I run the full test suite?"}.items() <= att[0].items()
    sync = b.client.post("/api/v1/sessions/s1/sync?format=text", json={}, headers=b.headers("jose", rec=b.rec())).text
    assert f"owed: #{seq} approval request from jose/claude" in sync
    assert api(b, "POST", f"/approvals/{seq}", "ana", {"decision": "grant"}).status_code == 403     # not named, not coordinator
    assert api(b, "POST", f"/approvals/{seq}", "jose/claude", {"decision": "grant"}).status_code == 403   # agents never decide
    api(b, "POST", "/members", "jose", {"handle": "ana/codex", "role": "coordinator"})
    r = api(b, "POST", f"/approvals/{seq}", "ana/codex", {"decision": "grant"})                   # not even agent coordinators
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = api(b, "POST", f"/approvals/{seq}", "jose", {"decision": "grant", "note": "Yes, once."})
    assert r.status_code == 200 and r.json()["decision"] == "grant"
    ev = b.events()[-1]
    assert ev["type"] == "approval.granted" and ev["reply_to"] == seq and b.recipients("s1", ev["seq"]) == ["jose/claude"]
    assert not [i for i in b.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"] if i["kind"] == "approval"]
    r = api(b, "POST", f"/approvals/{seq}", "jose", {"decision": "deny"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_resolved"


def test_approval_requests_name_humans_only(b):
    for to in ([], ["ana/codex"], ["jose", "ana/codex"]):
        r = api(b, "POST", "/messages", "jose/claude", {"kind": "approval", "body": "May I?", "to": to})
        assert r.status_code == 422, to
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "?", "to": ["jose"]}).json()["seq"]
    assert api(b, "POST", f"/approvals/{q}", "jose", {"decision": "grant"}).status_code == 404     # not an approval


# --- peer review ---------------------------------------------------------------------------------------

def test_peer_review_by_the_named_reviewer_is_advice(b):
    n = mk(b)
    v = view(b, n)
    assert api(b, "PATCH", f"/tasks/{n}", "jose", {"if_version": v["version"], "reviewer": "ana/codex"}).status_code == 200
    assert view(b, n)["reviewer"] == "ana/codex"
    f = claim(b, n).json()["fence"]
    r = api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "approve", "submission_seq": 1})
    assert r.status_code == 409                                                     # nothing submitted yet
    submitted = api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": f, "summary": "draft",
                    "commit": "a" * 40, "tests": [{"command": "pytest -q", "outcome": "pass", "detail": "47 passed"}]}).json()["seq"]
    att = b.req("GET", "/api/v1/me/attention", "ana/codex", key=None).json()["items"]
    assert any(i["kind"] == "review_requested" and i["task"] == n for i in att)
    assert api(b, "POST", f"/tasks/{n}/review", "ana", {"verdict": "approve", "submission_seq": submitted,
                                                          "commit": "a" * 40}).status_code == 403   # not the reviewer
    assert api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "approve", "submission_seq": submitted - 1,
                                                                "commit": "a" * 40}).json()["error"]["code"] == "submission_mismatch"
    assert api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "approve", "submission_seq": submitted,
                                                                "commit": "b" * 40}).json()["error"]["code"] == "submission_mismatch"
    r = api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "changes", "submission_seq": submitted,
                                                                     "commit": "a" * 40, "notes": "Add Q4."})
    assert r.status_code == 200 and r.json()["verdict"] == "changes"
    ev = b.events()[-1]
    assert ev["type"] == "task.reviewed" and ev["data"]["submission_seq"] == submitted
    assert ev["data"]["commit"] == "a" * 40 and "jose/claude" in b.recipients("s1", ev["seq"])
    assert view(b, n)["status"] == "review" and view(b, n)["review"]["verdict"] == "changes"   # a human still decides
    assert "review: changes" in text_of(b, f"/tasks/{n}").splitlines()[0]
    assert not any(i["kind"] == "review_requested" for i in
                   b.req("GET", "/api/v1/me/attention", "ana/codex", key=None).json()["items"])


# --- handoff and effort ---------------------------------------------------------------------------------

def test_structured_handoff_shows_first_on_the_task(b):
    n = mk(b)
    f = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": f, "note": "outline", "effort_min": 30})
    r = api(b, "POST", f"/tasks/{n}/release", "jose/claude",
            {"fence": f, "handoff": "Stopping here.", "handoff_fields": {"done": "Q1-Q2", "next": "Q3", "gotchas": "tone"}})
    assert r.status_code == 200, r.text
    text = text_of(b, f"/tasks/{n}")
    assert_canonical(text)
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("--- latest handoff #"))
    assert lines[i + 1:i + 4] == ["  · done: «Q1-Q2»", "  · next: «Q3»", "  · gotchas: «tone»"]
    assert "  · effort_min: «30»" in text


# --- leases ---------------------------------------------------------------------------------------------

def test_leases_are_exclusive_advisory_and_released_by_holder_or_coordinator(b):
    r = api(b, "POST", "/leases", "jose/claude", {"resource": "docs/faq.md", "note": "rewriting"})
    assert r.status_code == 201, r.text
    r = api(b, "POST", "/leases", "ana/codex", {"resource": "docs/faq.md"})
    assert r.status_code == 409 and r.json()["error"]["details"]["holder"] == "jose/claude"
    assert "docs/faq.md" not in r.text or r.json()["error"]["code"] == "resource_held"
    text = text_of(b, "/leases")
    assert_canonical(text)
    assert "--- held by jose/claude since #" in text and "  · resource: «docs/faq.md»" in text
    assert api(b, "POST", "/leases/release", "ana/codex", {"resource": "docs/faq.md"}).status_code == 403
    assert api(b, "POST", "/leases/release", "jose", {"resource": "docs/faq.md"}).status_code == 200   # human coordinator
    assert api(b, "POST", "/leases", "ana/codex", {"resource": "docs/faq.md"}).status_code == 201
    assert [e["type"] for e in b.events()][-3:] == ["lease.taken", "lease.released", "lease.taken"]


def test_taking_a_lease_already_held_by_the_same_identity_is_idempotent(b):
    first = api(b, "POST", "/leases", "jose/claude", {"resource": "docs/faq.md"})
    head = b.head()
    again = api(b, "POST", "/leases", "jose/claude", {"resource": "docs/faq.md"})
    assert again.status_code == 201 and again.json() == {
        "session": "s1", "seq": first.json()["seq"], "resource": "docs/faq.md", "already_held": True,
    }
    assert b.head() == head


def test_leases_expire_when_the_holder_goes_idle(b):
    from glassboard.api_coord import expire_leases
    api(b, "POST", "/leases", "jose/claude", {"resource": "service: staging db"})
    assert expire_leases(b.app.state.db) == 0                                      # fresh
    later = datetime.now(timezone.utc) + timedelta(seconds=1801)                  # past the 1800 s default
    assert expire_leases(b.app.state.db, now=later) == 1
    ev = b.events()[-1]
    assert ev["type"] == "lease.expired" and b.recipients("s1", ev["seq"]) == ["jose/claude"]
    assert b.req("GET", "/api/v1/sessions/s1/leases", "jose", key=None).json()["leases"] == []
    assert expire_leases(b.app.state.db, now=later) == 0


# --- flow -----------------------------------------------------------------------------------------------

def test_flow_metrics_from_events(b):
    n = mk(b)
    f = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": f, "note": "a", "effort_min": 20})
    api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": f, "reason": "r"})
    api(b, "POST", f"/tasks/{n}/unblock", "jose/claude", {"fence": f})
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": f, "summary": "s"})
    current = view(b, n)
    api(b, "POST", f"/tasks/{n}/return", "jose", {"if_version": current["version"],
                                                      "submission_seq": current["submission"]["seq"],
                                                      "feedback": "more"})
    f = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": f, "summary": "s2"})
    api(b, "POST", f"/tasks/{n}/accept", "jose", {"if_version": view(b, n)["version"]})
    fl = b.req("GET", "/api/v1/sessions/s1/flow", "ana", key=None).json()
    t = fl["tasks"][0]
    assert t["returns"] == 1 and t["effort_min"] == 20 and t["status"] == "done" and "cycle_s" in t
    assert fl["members"][0]["member"] == "jose/claude" and fl["members"][0]["done"] == 1
    assert sum(fl["done_per_day"].values()) == 1
    text = text_of(b, "/flow", "ana")
    assert_canonical(text)
    assert f"--- T{n} · done · to claim " in text and "returns 1 · effort 20m ---" in text


# --- CLI ------------------------------------------------------------------------------------------------

def test_cli_coordination(b, live_server):
    for who in ("jose", "jose/claude", "ana/codex"):
        assert live_server.cli(who, "use", "s1").returncode == 0
    r = live_server.cli("jose/claude", "post", "approval", "May I reformat the docs?", "--to", "jose")
    assert r.returncode == 0, r.stderr
    seq = int(r.stdout.split("#")[1].split()[0])
    r = live_server.cli("jose", "approval", "grant", str(seq), "Go ahead.")
    assert r.returncode == 0 and "granted" in r.stdout, r.stderr
    r = live_server.cli("jose", "task", "create", "FAQ", "--scope", "s", "--deliverable", "d", "--assignee", "jose/claude",
                        "--reviewer", "ana/codex")
    assert r.returncode == 0, r.stderr
    assert live_server.cli("jose/claude", "task", "claim", "T1").returncode == 0
    assert live_server.cli("jose/claude", "lease", "take", "docs/faq.md", "--note", "editing").returncode == 0
    r = live_server.cli("ana/codex", "lease", "take", "docs/faq.md")
    assert r.returncode == 5 and "resource_held" in r.stderr
    assert live_server.cli("jose/claude", "task", "note", "T1", "drafted", "--effort", "45").returncode == 0
    r = live_server.cli("jose/claude", "task", "submit", "T1", "ready", "--done", "all", "--next", "publish")
    assert r.returncode == 0, r.stderr
    submission = b.head()
    r = live_server.cli("ana/codex", "task", "review", "T1", "approve", "--submission", str(submission),
                        "--notes", "Looks good.")
    assert r.returncode == 0, r.stderr
    assert live_server.cli("jose/claude", "lease", "release", "docs/faq.md").returncode == 0
    for cmd in (("lease", "list"), ("flow",), ("task", "show", "T1")):
        out = live_server.cli("jose", *cmd)
        assert out.returncode == 0, (cmd, out.stderr)
        assert_canonical(out.stdout)
        assert not FENCE.search(out.stdout)
    assert "review: approve" in live_server.cli("jose", "task", "show", "T1").stdout


# --- dashboard ------------------------------------------------------------------------------------------

def test_dashboard_grants_an_approval(b):
    from test_dashboard import login
    seq = api(b, "POST", "/messages", "jose/claude", {"kind": "approval", "body": "May I \u202e deploy?",
                                                     "to": ["jose"]}).json()["seq"]
    c = login(b, "jose")
    home = c.get("/").text
    assert f'action="/s/s1/approvals/{seq}"' in home and "\u202e" not in home
    csrf = re.search(r'name="csrf" value="([^"]+)"', home).group(1)
    r = c.post(f"/s/s1/approvals/{seq}", data={"csrf": csrf, "op": "dash-approve-1", "decision": "deny", "note": "Not now"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert b.events()[-1]["type"] == "approval.denied"
    r = c.post(f"/s/s1/approvals/{seq}", data={"csrf": csrf, "op": "dash-approve-2", "decision": "grant"})
    assert r.headers["location"] == "/?err=already_resolved"


def test_home_shows_a_fixed_error_message_never_the_url_text(b):
    from test_dashboard import login
    c = login(b, "jose")
    assert "That was already decided." in c.get("/?err=already_resolved").text
    assert "<b>x</b>" not in c.get("/?err=<b>x</b>").text
