"""Event core: seq allocation, recovery check, idempotency digest, immutable
receipts + Glassboard-Replayed, routing, runtime label validation.
(Also a rotated recovery ID and the one-event-per-change rows for membership operations.)
Message rows and the recipient-order case are tested with messages."""
import sqlite3
import threading

import pytest
from fastapi import Depends, Header

from conftest import VALID_BODIES, fill_path, prepare_path, route_has_body, session_routes
from glassboard import db, events, ids, routing, tokens
from glassboard.deps import get_db, require_full_scope
from glassboard.idempotency import idempotency_key, respond
from glassboard.mutation import runtime_label, session_tx
from glassboard.models import StrictModel


def setup(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    return gb


def members_post(gb, key, handle="ana", role="member", s="s1", who="jose", rec=None):
    return gb.req("POST", f"/api/v1/sessions/{s}/members", who, json={"handle": handle, "role": role},
                  rec=rec or gb.rec("s1" if s in ("s1",) else s), key=key)


# --- receipts ---------------------------------------------------------------------------------

def test_receipt_is_the_exact_response_and_replays_byte_identical(gb):
    setup(gb)
    k = ids.new_ulid()
    a = members_post(gb, k, role="coordinator")
    b = members_post(gb, k, role="coordinator")
    assert a.status_code == b.status_code == 200
    assert a.content == b.content and b.headers["glassboard-replayed"] == "true"
    stored = gb.raw.execute("SELECT receipt FROM event WHERE op_id=?", (k,)).fetchone()[0]
    assert stored.encode() == a.content
    assert sum(1 for e in gb.events() if e["op_id"] == k) == 1


def test_at_4_3_replay_with_changed_body_is_rejected(gb):
    setup(gb)
    k = ids.new_ulid()
    assert members_post(gb, k, role="coordinator").status_code == 200
    r = members_post(gb, k, role="observer")
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert gb.raw.execute("SELECT role FROM member WHERE principal_id=?", (gb.pid("ana"),)).fetchone()[0] == "coordinator"


def test_at_4_4_receipt_cannot_be_rewritten(gb):
    setup(gb)
    k = ids.new_ulid()
    first = members_post(gb, k, role="coordinator").content
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        gb.raw.execute("UPDATE event SET receipt='{\"forged\":true}' WHERE op_id=?", (k,))
    assert members_post(gb, k, role="coordinator").content == first


def test_no_code_path_builds_a_replay_from_current_state(gb):
    """After the state moves on, the replay still returns the original receipt."""
    setup(gb)
    k = ids.new_ulid()
    first = members_post(gb, k, role="coordinator")
    members_post(gb, ids.new_ulid(), role="observer")              # state changes afterwards
    again = members_post(gb, k, role="coordinator")
    assert again.content == first.content and again.json()["role"] == "coordinator"


# --- digest binding ----------------------------------------------------------------------------

def test_at_e2_1_same_key_and_body_against_another_target(gb):
    setup(gb)
    k = ids.new_ulid()
    assert gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec(), key=k).status_code == 200
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec(), key=k)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert gb.req("GET", "/api/v1/sessions/s1/members", "jose/claude", key=None).status_code == 200


def test_at_e2_2_same_key_different_operation_same_target(gb):
    setup(gb)
    k = ids.new_ulid()
    assert members_post(gb, k, role="observer").status_code == 200
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec(), key=k)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_at_e2_3_session_handle_and_id_are_the_same_request(gb):
    setup(gb)
    sid = gb.raw.execute("SELECT id FROM session WHERE handle='s1'").fetchone()[0]
    k = ids.new_ulid()
    a = members_post(gb, k, role="coordinator")
    b = gb.req("POST", f"/api/v1/sessions/{sid}/members", "jose", json={"handle": "ana", "role": "coordinator"},
               rec=gb.rec(), key=k)
    assert b.headers.get("glassboard-replayed") == "true" and b.content == a.content
    # A removal retried after it committed: the member is gone, the receipt still answers.
    k2 = ids.new_ulid()
    r1 = gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec(), key=k2)
    r2 = gb.req("DELETE", f"/api/v1/sessions/{sid}/members/jose/claude", "jose", rec=gb.rec(), key=k2)
    assert r1.status_code == r2.status_code == 200 and r2.content == r1.content


class RtBody(StrictModel):
    note: str


def mount_runtime_routes(app):
    """Test-only operations: one whose digest binds the runtime label (like claim, reclaim, attach
    and takeover) and one that does not (like post)."""
    for path, bind in (("/api/v1/sessions/{s}/_test/bound", True), ("/api/v1/sessions/{s}/_test/unbound", False)):
        def make(bind=bind, path=path):
            def op(s: str, body: RtBody, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
                with session_tx(database, p, s, runtime) as tx:
                    tx.check_recovery(recovery)
                    r = tx.replay(key, "POST " + path, body.model_dump(), bind_runtime=bind)
                    if r is not None:
                        return r
                    _, text = tx.event("note", body=body.note, receipt=lambda seq: {"seq": seq, "runtime": runtime})
                return respond(text, 200)
            return op
        app.post(path)(make())


def test_at_e2_4_runtime_bound_only_where_it_is_part_of_the_effect(gb):
    setup(gb)
    mount_runtime_routes(gb.app)
    k = ids.new_ulid()
    rt = lambda label: {"X-Glassboard-Runtime": label}
    a = gb.req("POST", "/api/v1/sessions/s1/_test/bound", "jose/claude", json={"note": "x"}, rec=gb.rec(), key=k, extra=rt("a1f"))
    b = gb.req("POST", "/api/v1/sessions/s1/_test/bound", "jose/claude", json={"note": "x"}, rec=gb.rec(), key=k, extra=rt("b77"))
    assert a.status_code == 200 and b.status_code == 409 and b.json()["error"]["code"] == "idempotency_key_reused"
    k2 = ids.new_ulid()
    c = gb.req("POST", "/api/v1/sessions/s1/_test/unbound", "jose/claude", json={"note": "x"}, rec=gb.rec(), key=k2, extra=rt("a1f"))
    d = gb.req("POST", "/api/v1/sessions/s1/_test/unbound", "jose/claude", json={"note": "x"}, rec=gb.rec(), key=k2, extra=rt("b77"))
    assert d.headers.get("glassboard-replayed") == "true" and d.content == c.content    # a replacement runtime resends
    assert gb.events()[-1]["runtime"] == "a1f"                                          # first runtime recorded


def test_at_e2_5_keys_are_scoped_per_session(gb):
    setup(gb)
    gb.session("s2")
    k = ids.new_ulid()
    assert members_post(gb, k, s="s1").status_code == 200
    r = gb.req("POST", "/api/v1/sessions/s2/members", "jose", json={"handle": "ana"}, rec=gb.rec("s2"), key=k)
    assert r.status_code == 200 and "glassboard-replayed" not in r.headers
    assert r.json()["change"] == "added"


def test_concurrent_identical_requests_converge(gb):
    setup(gb)
    k, results, rec = ids.new_ulid(), [], gb.rec()      # read here: gb.raw belongs to this thread
    def go():
        from fastapi.testclient import TestClient
        c = TestClient(gb.app, raise_server_exceptions=False)
        results.append(c.post("/api/v1/sessions/s1/members", json={"handle": "ana", "role": "coordinator"},
                              headers=gb.headers("jose", rec=rec, key=k)))
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert {r.status_code for r in results} == {200}
    assert len({r.content for r in results}) == 1
    assert sum(1 for e in gb.events() if e["op_id"] == k) == 1
    assert sum(1 for r in results if r.headers.get("glassboard-replayed") == "true") == 5


# --- recovery ------------------------------------------------------------------------------------

def test_at_3_3_every_session_mutation_requires_the_recovery_header(gb):
    """Walks every session mutation route; later items' routes join automatically."""
    setup(gb)
    routes = [(m, p) for m, p in session_routes(gb.app, methods=("POST", "PATCH", "DELETE", "PUT"))]
    assert len(routes) >= 2
    for method, path in routes:
        if route_has_body(gb.app, method, path):
            assert (method, path) in VALID_BODIES, f"add a valid body for {method} {path}"
        url = fill_path(path, **prepare_path(gb, method, path))
        head = gb.head()
        r = gb.req(method, url, "jose", json=VALID_BODIES.get((method, path), {}))
        assert r.status_code == 428, (method, path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "recovery_required"
        assert gb.head() == head


def test_rotated_recovery_id_rejects_old_ops_before_replay(gb):
    """At the core level: a manually rotated recovery ID (what a restore does)."""
    setup(gb)
    rec0, k = gb.rec(), ids.new_ulid()
    first = members_post(gb, k, role="coordinator", rec=rec0)
    gb.raw.execute("UPDATE session SET recovery_id=?, epoch=epoch+1 WHERE handle='s1'", (ids.new_random_b32(16),))
    head = gb.head()
    r = members_post(gb, k, role="coordinator", rec=rec0)          # the idempotency table is not consulted
    assert r.status_code == 409 and r.json()["error"]["code"] == "recovery_mismatch"
    assert r.json()["error"]["details"] == {"epoch": 2} and gb.head() == head
    resent = members_post(gb, k, role="coordinator", rec=gb.rec())  # --resend: original receipt
    assert resent.content == first.content and resent.headers["glassboard-replayed"] == "true"


# --- runtime label ------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["A1F", "a" * 17, "a1f;x", "a b", "a_1", ""])
def test_invalid_runtime_label_is_rejected(gb, label):
    setup(gb)
    head = gb.head()
    r = gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "ana", "role": "observer"},
               rec=gb.rec(), extra={"X-Glassboard-Runtime": label})
    assert r.status_code == 422 and gb.head() == head
    assert r.json()["error"]["details"]["fields"][0]["loc"] == ["header", "x-glassboard-runtime"]
    assert not label or label not in r.text      # (the empty string is "in" every string)


def test_valid_runtime_label_is_recorded(gb):
    setup(gb)
    gb.req("POST", "/api/v1/sessions/s1/members", "jose", json={"handle": "ana", "role": "observer"},
           rec=gb.rec(), extra={"X-Glassboard-Runtime": "a1f"})
    assert gb.events()[-1]["runtime"] == "a1f"


# --- gapless seq and exactly one event per change (member rows) ---------------------------------

def test_seq_is_gapless(gb):
    setup(gb)
    for role in ("coordinator", "observer", "member"):
        members_post(gb, ids.new_ulid(), role=role)
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())
    seqs = [e["seq"] for e in gb.events()]
    assert seqs == list(range(1, len(seqs) + 1)) and gb.head() == seqs[-1]


def test_at_8_1_member_rows(gb):
    """Coordination mutations add exactly one event; bookkeeping and instance-level operations add
    none (instance ops add one audit row). Message rows are tested with messages."""
    gb.session("s1")
    audit = lambda: gb.raw.execute("SELECT count(*) FROM audit").fetchone()[0]
    rows = [
        ("member add", lambda: gb.add("s1", "ana"), 1, 0),
        ("role change", lambda: gb.add("s1", "ana", role="coordinator"), 1, 0),
        ("role unchanged", lambda: gb.add("s1", "ana", role="coordinator"), 0, 0),
        ("member remove", lambda: gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec()), 1, 0),
        ("token use (whoami)", lambda: gb.req("GET", "/api/v1/whoami", "jose", key=None), 0, 0),
        ("member list", lambda: gb.req("GET", "/api/v1/sessions/s1/members", "jose", key=None), 0, 0),
        ("agent register", lambda: gb.req("POST", "/api/v1/me/agents", "jose", json={"name": "x"}), 0, 1),
        ("token mint", lambda: gb.req("POST", "/api/v1/me/tokens", "jose", json={}), 0, 1),
    ]
    for name, op, d_events, d_audit in rows:
        h, a = gb.head(), audit()
        op()
        assert (gb.head() - h, audit() - a) == (d_events, d_audit), name


# --- routing ------------------------------------------------------------------------------

def test_every_event_type_has_a_routing_rule():
    assert set(routing.RULES) == set(events.TYPES)


def test_routing_rules(gb):
    setup(gb)
    gb.add("s1", "ana", role="coordinator")
    c = sqlite3.connect(gb.db)
    c.row_factory = sqlite3.Row
    sid = c.execute("SELECT id FROM session").fetchone()[0]
    J, A, CL, CO = gb.pid("jose"), gb.pid("ana"), gb.pid("jose/claude"), gb.pid("ana/codex")
    R = lambda t, actor=J, **k: routing.route(c, sid, t, actor, **k)
    assert R("decision") == ([], True) and R("session.paused") == ([], True)
    assert R("question", to=[CL, A]) == (sorted([CL, A]), False)
    assert R("question") == ([], True)                                    # no recipients → all
    assert R("note", to=[J, A]) == ([A], False)                            # never the actor itself
    assert R("task.submitted", actor=CL) == (sorted([J, A]), False)        # coordinators
    assert R("task.blocked", actor=J) == ([A], False)                      # coordinators minus actor
    assert R("task.claimed", actor=CL) == ([], False) and R("checkpoint.saved") == ([], False)
    assert R("task.stale", actor=ids.BOARD_PRINCIPAL_ID, claimant=CL) == (sorted([CL, J, A]), False)
    assert R("task.reassigned", previous_claimant=CL, assignee=CO) == (sorted([CL, CO]), False)
    assert R("task.edited", claimant=None, assignee=CO) == ([CO], False)
    assert R("consumer.takeover", actor=CL, principal=CL) == (sorted([J, A]), False)   # owner jose + coordinators
    assert R("question.resolved", actor=CL, asker=A, resolved_for=[CO]) == (sorted([A, CO]), False)
    assert R("member.added", member=CO) == ([CO], False)
    c.close()
