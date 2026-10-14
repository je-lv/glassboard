"""Sessions, membership, recovery_id, self-service agents and tokens
(token replay hides the secret, crash-replay of journaled reductions, session creation; non-member → 404
on every session route)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from conftest import VALID_BODIES, api_routes, fill_path, route_has_body

from glassboard import ids, journal

CROCKFORD16 = r"^[0-9A-HJKMNP-TV-Z]{16}$"


# --- session creation ---------------------------------------------------------------

def test_at_e14_1_human_creates_session_and_becomes_coordinator(gb):
    j = gb.session("s1")
    assert j["session"] == "s1" and j["role"] == "coordinator" and j["state"] == "active"
    assert j["epoch"] == 1 and j["seq"] == 1 and j["head"] == 1
    import re
    assert re.match(CROCKFORD16, j["recovery_id"])               # 80-bit recovery ID
    ev = gb.events("s1")
    assert [(e["seq"], e["type"]) for e in ev] == [(1, "session.created")]
    assert ev[0]["actor_id"] == gb.pid("jose") and ev[0]["broadcast"] == 1
    m = gb.raw.execute("SELECT role, joined_seq, cursor_seq, delivered_seq FROM member").fetchone()
    assert tuple(m) == ("coordinator", 1, 0, 0)
    assert gb.raw.execute("SELECT count(*) FROM audit WHERE action='session.create'").fetchone()[0] == 1


def test_recovery_ids_are_fresh_per_session(gb):
    assert gb.session("s1")["recovery_id"] != gb.session("s2")["recovery_id"]


@pytest.mark.parametrize("who,code", [("jose/claude", 403), ("read", 403)])
def test_only_full_scope_humans_create_sessions(gb, who, code):
    if who == "read":
        gb.tok["read"] = gb.mint("jose", "--scope", "read")
    r = gb.req("POST", "/api/v1/sessions", who, json={"handle": "s1", "title": "t"})
    assert r.status_code == code and gb.raw.execute("SELECT count(*) FROM session").fetchone()[0] == 0


def test_session_create_validation_and_conflicts(gb):
    gb.session("s1")
    r = gb.req("POST", "/api/v1/sessions", "ana", json={"handle": "s1", "title": "t"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "handle_taken"
    for bad in ({"handle": "1abc", "title": "t"}, {"handle": "S1", "title": "t"}, {"handle": "a" * 33, "title": "t"},
                {"handle": "ok", "title": ""}, {"handle": "ok", "title": "t", "stale_after_s": 100},
                {"handle": "ok", "title": "t", "owner": "ana"}):
        assert gb.req("POST", "/api/v1/sessions", "jose", json=bad).status_code == 422, bad
    assert gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "ok", "title": "t"}, key=None).status_code == 422
    assert gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "ok", "title": "t"}, key="short").status_code == 422


def test_at_e14_2_session_create_replay_is_byte_identical(gb):
    k = ids.new_ulid()
    a = gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "s1", "title": "t"}, key=k)
    b = gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "s1", "title": "t"}, key=k)
    assert a.status_code == b.status_code == 201
    assert a.content == b.content and b.headers["glassboard-replayed"] == "true"
    assert "glassboard-replayed" not in a.headers
    assert gb.raw.execute("SELECT count(*) FROM session").fetchone()[0] == 1 and len(gb.events("s1")) == 1
    c = gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "s2", "title": "t"}, key=k)
    assert c.status_code == 409 and c.json()["error"]["code"] == "idempotency_key_reused"


def test_sessions_list_and_whoami_show_memberships(gb):
    gb.session("s1")
    gb.session("s2", who="ana")
    gb.add("s1", "jose/claude")
    mine = gb.req("GET", "/api/v1/sessions", "jose/claude", key=None).json()["sessions"]
    assert [(s["session"], s["role"]) for s in mine] == [("s1", "member")]
    w = gb.req("GET", "/api/v1/whoami", "jose/claude", key=None).json()["sessions"]
    assert w == mine and w[0]["recovery_id"] == gb.rec("s1")


# --- non-member → 404 on every session route -------------------------------------------------

def _session_routes(app):
    for r in api_routes(app):
        if "{s}" in r.path and r.path.startswith("/api/"):
            for m in r.methods:
                yield m, r.path


def _route_has_body(gb, method, path):
    return route_has_body(gb.app, method, path)


def test_non_member_gets_404_on_every_session_route(gb):
    """Walks every registered route with {s}; later items' routes are covered automatically.
    1. With a valid request, a non-member gets 404 (membership is checked before everything else).
    2. A session the caller is not in and a session that does not exist answer identically, also
       when the body is invalid (validation runs before the membership check: 422 for both)."""
    gb.session("s1")
    routes = list(_session_routes(gb.app))
    assert len(routes) >= 3
    for method, path in routes:
        url = fill_path(path)
        has_body = _route_has_body(gb, method, path)
        if has_body:
            assert (method, path) in VALID_BODIES, f"add a valid body for {method} {path}"
        body = VALID_BODIES.get((method, path), {})
        for target in (url, url.replace("/s1", "/nope", 1)):
            r = gb.client.request(method, target, headers=gb.headers("ana", rec=gb.rec("s1")), json=body)
            assert r.status_code == 404, (method, target, r.status_code, r.text)
            e = r.json()["error"]
            assert (e["code"], e["message"]) == ("not_found", "Not found."), (method, target)
        if has_body:
            got = []
            for target in (url, url.replace("/s1", "/nope", 1)):
                r = gb.client.request(method, target, headers=gb.headers("ana", rec=gb.rec("s1")), json={"bogus": 1})
                got.append((r.status_code, r.json()["error"]["code"], r.json()["error"].get("details")))
            assert got[0] == got[1], (method, path, got)


# --- membership --------------------------------------------------------------------------------

def test_at_e14_3_member_added_event_and_cursor(gb):
    gb.session("s1")
    r = gb.add("s1", "jose/claude")
    assert r == {"member": "jose/claude", "role": "member", "change": "added", "seq": 2}
    e = gb.events()[-1]
    assert e["type"] == "member.added" and e["data"] == {"handle": "jose/claude", "role": "member"}
    assert gb.recipients("s1", 2) == ["jose/claude"]
    m = gb.raw.execute("SELECT joined_seq, cursor_seq, delivered_seq FROM member WHERE principal_id=?",
                       (gb.pid("jose/claude"),)).fetchone()
    assert tuple(m) == (2, 1, 1)                # inbox starts with its own member.added
    lst = gb.req("GET", "/api/v1/sessions/s1/members", "jose/claude", key=None).json()["members"]
    assert {m["handle"]: m["role"] for m in lst} == {"jose": "coordinator", "jose/claude": "member"}


def test_membership_is_human_coordinator_only(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude", role="coordinator")          # an agent may hold the role...
    gb.add("s1", "ana", role="member")
    for who in ("jose/claude", "ana"):                        # ...but never use human-only ops
        r = gb.req("POST", "/api/v1/sessions/s1/members", who, json={"handle": "ana/codex"}, rec=gb.rec())
        assert r.status_code == 403
        r = gb.req("DELETE", "/api/v1/sessions/s1/members/ana", who, rec=gb.rec())
        assert r.status_code == 403
    assert gb.head() == 3


def test_recovery_header_required_and_checked(gb):
    gb.session("s1")
    before = gb.head()
    r = gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "ana"})
    assert r.status_code == 428 and r.json()["error"]["code"] == "recovery_required"
    r = gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "ana"}, rec="0" * 16)
    assert r.status_code == 409 and r.json()["error"]["code"] == "recovery_mismatch"
    assert r.json()["error"]["details"] == {"epoch": 1}
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/jose", "jose")
    assert r.status_code == 428
    assert gb.head() == before                                  # nothing happened


def test_member_add_unknown_or_system_principal_is_404(gb):
    gb.session("s1")
    for h in ("nobody", "board", "jose/nobody"):
        r = gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": h}, rec=gb.rec())
        assert r.status_code == 404, h
    gb.admin("deactivate", "ana/codex")
    assert gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "ana/codex"}, rec=gb.rec()).status_code == 404


def test_role_changes_reductions_journaled_grants_not(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="observer")
    up = gb.add("s1", "ana", role="coordinator")
    assert up["change"] == "role_changed"
    assert not os.path.exists(gb.app.state.journal_path)     # a grant is never journaled
    down = gb.add("s1", "ana", role="member")
    assert down["change"] == "role_changed"
    lines = open(gb.app.state.journal_path).read().splitlines()
    e = json.loads(lines[-1])
    assert e["action"] == "member.role_reduce" and e["role"] == "member" and e["principal_id"] == gb.pid("ana")
    same = gb.add("s1", "ana", role="member")
    assert same == {"member": "ana", "role": "member", "change": "none", "seq": None}
    types = [(x["type"], x["data"].get("from"), x["data"].get("to")) for x in gb.events()[2:]]
    assert types == [("member.changed", "observer", "coordinator"), ("member.changed", "coordinator", "member")]


def test_remove_member_journaled_and_access_ends(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    gb.raw.execute("UPDATE member SET consumer_secret='x', consumer_gen=1, consumer_runtime='a1f' WHERE principal_id=?",
                   (gb.pid("jose/claude"),))
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec())
    assert r.status_code == 200 and r.json() == {"member": "jose/claude", "removed": True, "seq": 3}
    e = json.loads(open(gb.app.state.journal_path).read().splitlines()[-1])
    assert e["action"] == "member.remove" and e["session_id"] == gb.raw.execute("SELECT id FROM session").fetchone()[0]
    assert gb.events()[-1]["type"] == "member.removed" and gb.recipients("s1", 3) == ["jose/claude"]
    assert gb.req("GET", "/api/v1/sessions/s1/members", "jose/claude", key=None).status_code == 404
    row = gb.raw.execute("SELECT left_at, consumer_secret FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()
    assert row["left_at"] and row["consumer_secret"] is None
    # Re-added: fresh joined_seq, cursor just before it, consumer generation moved on.
    again = gb.add("s1", "jose/claude")
    m = gb.raw.execute("SELECT joined_seq, cursor_seq, delivered_seq, consumer_gen, left_at FROM member WHERE principal_id=?",
                       (gb.pid("jose/claude"),)).fetchone()
    assert again["change"] == "added" and tuple(m) == (4, 3, 3, 2, None)
    assert gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec()).status_code == 200
    assert gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec()).status_code == 404


def test_at_e14_4_session_keeps_a_human_coordinator(gb):
    gb.session("s1")
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/jose", "jose", rec=gb.rec())
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    r = gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "jose", "role": "member"}, rec=gb.rec())
    assert r.status_code == 409
    gb.add("s1", "jose/claude", role="coordinator")            # an agent coordinator does not count
    assert gb.req("DELETE", "/api/v1/sessions/s1/members/jose", "jose", rec=gb.rec()).status_code == 409
    gb.add("s1", "ana", role="coordinator")
    assert gb.req("DELETE", "/api/v1/sessions/s1/members/jose", "jose", rec=gb.rec()).status_code == 200


def test_each_session_op_commits_exactly_one_event(gb):
    """Every session and membership operation commits exactly one event."""
    gb.session("s1")
    assert gb.head() == 1
    for op in (lambda: gb.add("s1", "ana"), lambda: gb.add("s1", "ana", role="coordinator"),
               lambda: gb.add("s1", "ana", role="observer"),
               lambda: gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())):
        h = gb.head()
        op()
        assert gb.head() == h + 1
    h, audits = gb.head(), gb.raw.execute("SELECT count(*) FROM audit").fetchone()[0]
    gb.req("POST", "/api/v1/me/agents", "jose", json={"name": "helper"})
    gb.req("POST", "/api/v1/me/tokens", "jose", json={})
    assert gb.head() == h                                      # instance-level ops: no event...
    assert gb.raw.execute("SELECT count(*) FROM audit").fetchone()[0] == audits + 2   # ...one audit row each


# --- /me: agents and tokens ----------------------------------------------------------------------

def test_me_agents(gb):
    r = gb.req("POST", "/api/v1/me/agents", "jose", json={"name": "claude-2"})
    assert r.status_code == 201 and r.json()["handle"] == "jose/claude-2" and r.json()["owner"] == "jose"
    assert gb.req("POST", "/api/v1/me/agents", "jose", json={"name": "claude-2"}).status_code == 409
    assert gb.req("POST", "/api/v1/me/agents", "jose/claude", json={"name": "x"}).status_code == 403
    for bad in ({"name": "a/b"}, {"name": "A"}, {"name": "x", "owner": "ana"}, {"name": "x", "kind": "human"}):
        assert gb.req("POST", "/api/v1/me/agents", "jose", json=bad).status_code == 422, bad


def test_me_tokens_for_self_and_owned_agents_only(gb):
    r = gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose/claude", "label": "laptop"})
    assert r.status_code == 201
    j = r.json()
    assert j["principal"] == "jose/claude" and j["secret"].startswith("gbt_") and j["secret_already_shown"] is False
    w = gb.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {j['secret']}"})
    assert w.status_code == 200 and w.json()["handle"] == "jose/claude"
    assert gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "ana/codex"}).status_code == 404
    assert gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "ana"}).status_code == 404
    assert gb.req("POST", "/api/v1/me/tokens", "jose/claude", json={}).status_code == 403
    me = gb.req("POST", "/api/v1/me/tokens", "jose", json={"scope": "read", "expires_days": 1}).json()
    assert me["principal"] == "jose" and me["scope"] == "read" and me["expires_at"]
    ro = gb.client.post("/api/v1/me/agents", json={"name": "x"},
                        headers={"Authorization": f"Bearer {me['secret']}", "Idempotency-Key": ids.new_ulid()})
    assert ro.status_code == 403
    # The secret is stored only as a hash.
    for suffix in ("", "-wal"):
        if os.path.exists(gb.db + suffix):
            data = open(gb.db + suffix, "rb").read()
            assert j["secret"].split("_", 2)[2].encode() not in data


def test_at_4_7_token_creation_replay_hides_secret(gb):
    k = ids.new_ulid()
    a = gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose/claude"}, key=k)
    b = gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose/claude"}, key=k)
    assert a.status_code == b.status_code == 201
    ja, jb = a.json(), b.json()
    assert ja["secret"] and jb["secret"] is None and jb["secret_already_shown"] is True
    assert {k_: v for k_, v in ja.items() if not k_.startswith("secret")} ==\
           {k_: v for k_, v in jb.items() if not k_.startswith("secret")}
    assert b.headers["glassboard-replayed"] == "true"
    assert gb.raw.execute("SELECT count(*) FROM credential WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()[0] == 2
    # Replaying twice gives the same bytes (the stored receipt).
    c = gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose/claude"}, key=k)
    assert c.content == b.content
    # "Myself" by omission and by handle are the same request (handles are hashed as IDs).
    k2 = ids.new_ulid()
    gb.req("POST", "/api/v1/me/tokens", "jose", json={}, key=k2)
    assert gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose"}, key=k2).headers.get("glassboard-replayed") == "true"


def test_revoke_token_through_journal(gb):
    j = gb.req("POST", "/api/v1/me/tokens", "jose", json={"principal": "jose/claude"}).json()
    k = ids.new_ulid()
    r = gb.req("DELETE", f"/api/v1/me/tokens/{j['id']}", "jose", key=k)
    assert r.status_code == 200 and r.json() == {"id": j["id"], "revoked": True}
    assert gb.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {j['secret']}"}).status_code == 401
    e = json.loads(open(gb.app.state.journal_path).read().splitlines()[-1])
    assert e == {**e, "action": "credential.revoke", "credential_id": j["id"]}
    again = gb.req("DELETE", f"/api/v1/me/tokens/{j['id']}", "jose", key=k)
    assert again.content == r.content and again.headers["glassboard-replayed"] == "true"
    assert gb.req("DELETE", f"/api/v1/me/tokens/{j['id']}", "jose").status_code == 200   # already revoked: fine
    assert len(open(gb.app.state.journal_path).read().splitlines()) == 1                  # journaled once
    other = gb.req("POST", "/api/v1/me/tokens", "ana", json={}).json()
    assert gb.req("DELETE", f"/api/v1/me/tokens/{other['id']}", "jose").status_code == 404
    assert gb.req("DELETE", "/api/v1/me/tokens/zzzzzzzzzz", "jose").status_code == 404


# --- crash-replay of a session-scoped reduction is visible ----------------------------------------

CRASH_SCRIPT = r"""
import sys
from fastapi.testclient import TestClient
from glassboard.app import create_app
db, tok, rec = sys.argv[1:4]
c = TestClient(create_app(db, allowed_hosts=["testserver"]))
c.delete("/api/v1/sessions/s1/members/ana",
         headers={"Authorization": "Bearer " + tok, "X-Glassboard-Recovery": rec,
                  "Idempotency-Key": "crash-test-key-1"})
print("not reached")
"""


def test_at_e12_3_crash_between_journal_and_commit_is_replayed_visibly(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    head = gb.head()
    env = {**os.environ, journal.FAULT_ENV: "after_journal_append"}
    r = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, gb.db, gb.tok["jose"], gb.rec()],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 70, r.stderr
    assert gb.head() == head                                    # the removal never committed
    gb.restart()                                                # replay applies it, visibly
    ev = gb.events()[-1]
    assert gb.head() == head + 1 and ev["type"] == "member.removed"
    assert ev["actor_id"] == ids.BOARD_PRINCIPAL_ID and ev["data"] == {"handle": "ana", "journal_n": 1}
    assert gb.recipients("s1", ev["seq"]) == ["ana"]
    assert gb.req("GET", "/api/v1/sessions/s1/members", "ana", key=None).status_code == 404


def test_at_e12_4_replay_that_changes_nothing_emits_nothing(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    gb.add("s1", "ana", role="member")                          # journaled and committed normally
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())
    head = gb.head()
    gb.restart()
    gb.restart()
    assert gb.head() == head


def test_replay_of_role_reduce_and_archive_are_visible(gb):
    """The two other session-scoped reductions, applied by replay without the original op."""
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    sid = gb.raw.execute("SELECT id FROM session").fetchone()[0]
    jp = gb.app.state.journal_path
    # Simulate entries that were journaled but whose ops crashed before COMMIT.
    gb.app.state.db.close()
    db_ = __import__("glassboard.db", fromlist=["Database"]).Database(gb.db)
    with db_.write():
        journal.append(jp, "member.role_reduce", session_id=sid, principal_id=gb.pid("ana"), role="observer")
        journal.append(jp, "session.archive", session_id=sid)
    db_.close()
    head = gb.head()
    gb.restart()
    ev = gb.events()[-2:]
    assert [e["type"] for e in ev] == ["member.changed", "session.archived"]
    assert ev[0]["data"] == {"handle": "ana", "from": "coordinator", "to": "observer", "journal_n": 1}
    assert ev[1]["broadcast"] == 1 and gb.head() == head + 2


# --- CLI against a live server --------------------------------------------------------------------

@pytest.fixture
def live(live_server):
    return live_server.cli


def test_cli_session_members_agent_token_flow(live, gb, tmp_path):
    r = live("jose", "session", "create", "docs", "Write the docs\x1b[31m")
    assert r.returncode == 0, r.stderr
    assert "session docs created" in r.stdout and "\x1b" not in r.stdout
    assert "  · title: «Write the docs»" in r.stdout          # control characters are stripped
    r = live("jose", "agent", "add", "scribe", "--kind", "client")
    assert r.returncode == 0 and "@jose/scribe (client) registered" in r.stdout
    out = tmp_path / "scribe.token"
    r = live("jose", "token", "new", "jose/scribe", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert oct(os.stat(out).st_mode & 0o777) == "0o600" and out.read_text().startswith("gbt_")
    assert live("jose", "members", "add", "docs", "jose/scribe").stdout.startswith("@jose/scribe added · member")
    assert live("jose", "members", "add", "docs", "ana", "--role", "coordinator").returncode == 0
    lst = live("jose", "members", "list", "docs").stdout
    assert "@jose/scribe (client) · member" in lst and "@ana (human) · coordinator" in lst
    assert "removed" in live("jose", "members", "remove", "docs", "jose/scribe").stdout
    r = live("jose/claude", "members", "add", "docs", "ana/codex")
    assert r.returncode == 5 and "not_found" in r.stderr       # not a member of docs
    ss = json.loads(live("jose", "sessions", "--json").stdout)["sessions"]
    assert [s["session"] for s in ss] == ["docs"]
    cid = gb.raw.execute("SELECT c.id FROM credential c JOIN principal p ON p.id=c.principal_id WHERE p.handle='jose/scribe'").fetchone()[0]
    assert "revoked" in live("jose", "token", "revoke", cid).stdout
    bad = tmp_path / "never.token"
    r = live("jose", "token", "new", "ana/codex", "--out", str(bad))
    assert r.returncode == 5 and not bad.exists()              # failed mint leaves no empty file
