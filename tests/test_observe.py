"""Observe replay uses the recorded event prefix, including task ownership transitions."""
from glassboard import observe
from test_dashboard import login


def _state(gb, at):
    with gb.app.state.db.read() as c:
        sess = c.execute("SELECT * FROM session WHERE handle='s1'").fetchone()
        return observe.model(c, sess, at)


def test_observe_replays_return_without_leaking_future_task_state(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    created = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
                     json={"title": "Original title", "scope": "Build it", "deliverable": "A report",
                           "assignee": "jose/claude"}, rec=gb.rec())
    assert created.status_code == 201
    task = created.json()["task"]
    created_seq = created.json()["seq"]
    claim = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/claim", "jose/claude",
                   json={}, rec=gb.rec())
    assert claim.status_code == 201
    claim_seq = claim.json()["seq"]
    submitted = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/submit", "jose/claude",
                       json={"fence": claim.json()["fence"], "summary": "Implementation and checks complete."}, rec=gb.rec())
    assert submitted.status_code == 200
    returned = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/return", "jose",
                      json={"if_version": submitted.json()["version"], "submission_seq": submitted.json()["seq"],
                            "feedback": "Please expand the report."}, rec=gb.rec())
    assert returned.status_code == 200
    first = _state(gb, created_seq)["tasks"][0]
    working = _state(gb, claim_seq)["tasks"][0]
    final = _state(gb, returned.json()["seq"])["tasks"][0]
    assert (first["status"], first["claimant"], first["assignee"]) == ("open", None, "jose/claude")
    assert (working["status"], working["claimant"]) == ("in_progress", "jose/claude")
    assert (final["status"], final["claimant"], final["assignee"]) == ("open", None, "jose/claude")
    c = login(gb)
    assert c.get(f"/s/s1/observe?at={claim_seq}").status_code == 200
    assert c.get(f"/s/s1/observe?at={gb.head() + 1}").status_code == 404
    gb.session("hidden", who="ana")
    assert c.get("/s/hidden/observe").status_code == 404


def test_coordinator_reclaim_does_not_change_claimant_or_blocked_status(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana/codex", role="coordinator")
    created = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
                     json={"title": "Investigate", "scope": "Find a cause", "deliverable": "Finding"}, rec=gb.rec())
    task = created.json()["task"]
    claim = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/claim", "jose/claude", json={}, rec=gb.rec())
    blocked = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/block", "jose/claude",
                     json={"fence": claim.json()["fence"], "reason": "Waiting for input"}, rec=gb.rec())
    assert blocked.status_code == 200
    reclaimed = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/reclaim", "ana/codex",
                       json={"reason": "Client restarted"}, rec=gb.rec())
    assert reclaimed.status_code == 200
    task_at = _state(gb, reclaimed.json()["seq"])["tasks"][0]
    assert task_at["status"] == "blocked" and task_at["claimant"] == "jose/claude"
    assert task_at["status_seq"] == blocked.json()["seq"]
