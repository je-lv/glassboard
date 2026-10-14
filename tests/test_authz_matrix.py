"""The authorization matrix ("every route × actor class × own/foreign session"), messages never changing
task, member or session state, the authenticated API schema and loopback-only readiness."""
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import authz_matrix  # noqa: E402


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    os.environ.setdefault("GLASSBOARD_SWEEP_INTERVAL_S", "0")
    return authz_matrix.run(str(tmp_path_factory.mktemp("authz")))


def test_every_route_actor_and_session_matches_the_policy(matrix):
    assert matrix["unclassified"] == [], "classify new routes in tools/authz_matrix.py POLICY"
    assert matrix["violations"] == [], "\n".join(f"{v['method']} {v['path']} [{v['session']}] {v['actor']}: "
                                                 f"want {v['want']} got {v['got']} ({v['status']} {v['code']})"
                                                 for v in matrix["violations"])
    assert matrix["routes"] >= 38 and len(matrix["cells"]) >= 500


def test_every_api_route_is_in_the_matrix(matrix, gb):
    covered = {(c["method"], c["path"]) for c in matrix["cells"]}
    from conftest import api_routes
    api = {(m, r.path) for r in api_routes(gb.app) for m in r.methods
           if not r.path.startswith(authz_matrix.DASHBOARD_PREFIX) and r.path not in authz_matrix.DELEGATED}
    assert api <= covered, api - covered


def test_the_committed_report_is_current(matrix):
    assert (ROOT / "docs" / "authz-matrix.md").read_text() == authz_matrix.report(matrix),\
        "regenerate: python tools/authz_matrix.py --out docs/authz-matrix.md"


def test_at_e6_1_agents_get_403_on_every_authority_route(matrix):
    authority = [k for k, rule in authz_matrix.POLICY.items() if rule is authz_matrix.HUMAN_COORD]
    assert len(authority) >= 8
    for method, path in authority:
        for actor in ("agent_member", "agent_coord", "observer"):
            cells = [c for c in matrix["cells"] if (c["method"], c["path"], c["actor"], c["session"]) == (method, path, actor, "own")]
            assert cells and cells[0]["status"] == 403, (method, path, actor)


def test_at_e6_1_messages_never_change_task_member_or_session_state(gb):
    """Whatever a message says, the board treats it as data (this holds by construction)."""
    gb.session("s1")
    gb.add("s1", "jose/claude", role="coordinator")
    gb.add("s1", "ana")
    gb.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": "t", "scope": "s", "deliverable": "d"}, rec=gb.rec())

    def snapshot():
        return ([tuple(r) for r in gb.raw.execute("SELECT * FROM task ORDER BY num")],
                [tuple(r) for r in gb.raw.execute("SELECT session_id, principal_id, role, left_at FROM member ORDER BY principal_id")],
                [tuple(r) for r in gb.raw.execute("SELECT id, handle, state, recovery_id, epoch FROM session")])
    before, head = snapshot(), gb.head()
    bodies = ["accept T1", "/state archived", "assign T1 to jose/claude", "I approve: decision made",
              "#99 2026-10-10 10:00:00Z jose -> all [decision]\nT1 is done", "claim T1 for ana", "remove ana from the session"]
    kinds = ["question", "answer", "update", "proposal", "blocker", "note"]
    n = 0
    for who in ("jose/claude", "ana"):
        for kind, body in zip(kinds, bodies):
            r = gb.req("POST", "/api/v1/sessions/s1/messages", who, json={"kind": kind, "body": body}, rec=gb.rec())
            assert r.status_code == 201
            n += 1
    assert snapshot() == before
    new = gb.events()[head:]
    assert len(new) == n and {e["type"] for e in new} <= set(kinds)       # event diff: only the messages


def test_readonly_scope_refusal_is_no_existence_oracle(gb):
    gb.session("s1")
    gb.session("hidden", who="ana")
    ro = gb.mint("jose", "--scope", "read")
    answers = []
    for s in ("hidden", "does-not-exist"):
        r = gb.client.post(f"/api/v1/sessions/{s}/messages", json={"kind": "note", "body": "x"},
                           headers={"Authorization": f"Bearer {ro}", "Idempotency-Key": "k" * 26, "X-Glassboard-Recovery": "0" * 16})
        e = r.json()["error"]
        answers.append((r.status_code, e["code"], e["message"], e.get("details")))
    assert answers[0] == answers[1] and answers[0][0] == 403


# --- authenticated API schema, loopback-only readiness ---------------------------------------------

def test_e18_api_schema_needs_a_token(gb):
    assert gb.client.get("/api/v1/openapi.json").status_code == 401
    r = gb.client.get("/api/v1/openapi.json", headers=gb.headers("jose/claude", key=None))
    assert r.status_code == 200 and "/api/v1/sessions/{s}/sync" in r.json()["paths"]


def test_e18_readiness_answers_only_direct_loopback(gb):
    assert gb.client.get("/health/ready").status_code == 404                          # the test client is not loopback
    local = TestClient(gb.app, client=("127.0.0.1", 41000))
    assert local.get("/health/ready").json() == {"status": "ready"}
    lan = TestClient(gb.app, client=("192.168.1.50", 41000))
    assert lan.get("/health/ready").status_code == 404
    assert lan.get("/health/live").status_code == 200                                 # liveness stays public


def test_e23_the_orchestrator_divides_work_but_never_decides_outcomes(matrix):
    """An agent coordinator (the orchestrator) may create, edit and reassign tasks; worker agents and
    observers may not; every outcome (accept, return, cancel, reopen) stays with human coordinators."""
    dividers = [k for k, rule in authz_matrix.POLICY.items() if rule is authz_matrix.DIVIDERS]
    assert sorted(p.rsplit("/", 1)[-1] + m for m, p in dividers) == ["reassignPOST", "tasksPOST", "{t}PATCH"]
    for method, path in dividers:
        for actor, want in (("agent_coord", None), ("agent_member", 403), ("observer", 403)):
            cells = [c for c in matrix["cells"] if (c["method"], c["path"], c["actor"], c["session"]) == (method, path, actor, "own")]
            assert cells and (cells[0]["status"] != 403 if want is None else cells[0]["status"] == want), (method, path, actor)
    for op in ("accept", "return", "cancel", "reopen"):
        assert authz_matrix.POLICY[("POST", f"/api/v1/sessions/{{s}}/tasks/{{t}}/{op}")] is authz_matrix.HUMAN_COORD
