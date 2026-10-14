"""Bounded autonomous child-task outcomes keep root acceptance human-only."""

from fastapi.testclient import TestClient

from glassboard.dashboard import LOGIN_COOKIE


def _api(gb, method, path, who, body=None, s="auto"):
    body = body.copy() if body is not None else {}
    import re
    match = re.fullmatch(r"/tasks/(\d+)/(accept|return|reassign|cancel|reopen)", path)
    if method == "POST" and match:
        task = int(match.group(1))
        row = gb.raw.execute("SELECT version FROM task WHERE num=? AND session_id=(SELECT id FROM session WHERE handle=?)",
                             (task, s)).fetchone()
        if row:
            body.setdefault("if_version", row["version"])
        if match.group(2) == "return":
            event = gb.raw.execute("SELECT seq FROM event WHERE session_id=(SELECT id FROM session WHERE handle=?) "
                                   "AND task_num=? AND type='task.submitted' ORDER BY seq DESC LIMIT 1", (s, task)).fetchone()
            if event:
                body.setdefault("submission_seq", event["seq"])
    return gb.req(method, f"/api/v1/sessions/{s}{path}", who, json=body, rec=gb.rec(s))


def _task(gb, title, **extra):
    response = _api(gb, "POST", "/tasks", "jose/claude",
                    {"title": title, "scope": title, "deliverable": title, **extra})
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _submit(gb, task, who):
    claimed = _api(gb, "POST", f"/tasks/{task}/claim", who)
    assert claimed.status_code == 201, claimed.text
    submitted = _api(gb, "POST", f"/tasks/{task}/submit", who,
                     {"fence": claimed.json()["fence"], "summary": "Finished"})
    assert submitted.status_code == 200, submitted.text
    return submitted.json()["seq"]


def test_peer_reviewed_child_unblocks_dependent_work_but_root_needs_human(gb):
    created = gb.req("POST", "/api/v1/sessions", "jose",
                     json={"handle": "auto", "title": "Build project", "autonomous": True})
    assert created.status_code == 201 and created.json()["autonomous"] is True
    gb.add("auto", "jose/claude", role="coordinator")
    gb.add("auto", "ana/codex")
    root = gb.req("POST", "/api/v1/sessions/auto/tasks", "jose",
                  json={"title": "Final project", "scope": "project", "deliverable": "complete app",
                        "assignee": "ana/codex"}, rec=gb.rec("auto")).json()["task"]
    child = _task(gb, "Design the app", parent=f"T{root}", assignee="ana/codex", reviewer="jose/claude")
    dependent = _task(gb, "Implement the app", parent=f"T{root}", assignee="ana/codex",
                      reviewer="jose/claude", depends_on=[f"T{child}"])
    alternate_root = _task(gb, "Alternate root")
    root_view = _api(gb, "GET", f"/tasks/{root}", "jose/claude").json()
    attempted_reparent = gb.req("PATCH", f"/api/v1/sessions/auto/tasks/{root}", "jose/claude",
                                json={"if_version": root_view["version"], "parent": f"T{alternate_root}"},
                                rec=gb.rec("auto"))
    assert attempted_reparent.status_code == 403
    assert _api(gb, "GET", f"/tasks/{dependent}", "ana/codex").json()["ready"] is False
    submitted = _submit(gb, child, "ana/codex")
    refused = _api(gb, "POST", f"/tasks/{child}/accept", "jose/claude")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "peer_review_required"
    approved = _api(gb, "POST", f"/tasks/{child}/review", "jose/claude",
                    {"verdict": "approve", "submission_seq": submitted})
    assert approved.status_code == 200, approved.text
    accepted = _api(gb, "POST", f"/tasks/{child}/accept", "jose/claude")
    assert accepted.status_code == 200 and accepted.json()["status"] == "done"
    assert _api(gb, "GET", f"/tasks/{dependent}", "ana/codex").json()["ready"] is True
    _submit(gb, root, "ana/codex")
    assert _api(gb, "POST", f"/tasks/{root}/accept", "jose/claude").status_code == 403
    assert _api(gb, "POST", f"/tasks/{root}/accept", "jose").status_code == 200


def test_default_session_keeps_all_task_outcomes_human_only(gb):
    gb.session("auto")
    gb.add("auto", "jose/claude", role="coordinator")
    gb.add("auto", "ana/codex")
    root = gb.req("POST", "/api/v1/sessions/auto/tasks", "jose",
                  json={"title": "Root", "scope": "r", "deliverable": "r"}, rec=gb.rec("auto")).json()["task"]
    child = _task(gb, "Child", parent=f"T{root}", assignee="ana/codex", reviewer="jose/claude")
    submitted = _submit(gb, child, "ana/codex")
    assert _api(gb, "POST", f"/tasks/{child}/review", "jose/claude",
                {"verdict": "approve", "submission_seq": submitted}).status_code == 200
    assert _api(gb, "POST", f"/tasks/{child}/accept", "jose/claude").status_code == 403
    assert _api(gb, "POST", f"/tasks/{child}/accept", "jose").status_code == 200


def test_agent_cannot_accept_self_reviewed_child(gb):
    gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "auto", "title": "Build", "autonomous": True})
    gb.add("auto", "jose/claude", role="coordinator")
    root = gb.req("POST", "/api/v1/sessions/auto/tasks", "jose",
                  json={"title": "Root", "scope": "r", "deliverable": "r"}, rec=gb.rec("auto")).json()["task"]
    child = _task(gb, "Child", parent=f"T{root}", assignee="jose/claude", reviewer="jose/claude")
    submitted = _submit(gb, child, "jose/claude")
    assert _api(gb, "POST", f"/tasks/{child}/review", "jose/claude",
                {"verdict": "approve", "submission_seq": submitted}).status_code == 200
    refused = _api(gb, "POST", f"/tasks/{child}/accept", "jose/claude")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "peer_review_required"


def test_agent_can_return_and_reopen_only_child_tasks_in_autonomous_session(gb):
    gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "auto", "title": "Build", "autonomous": True})
    gb.add("auto", "jose/claude", role="coordinator")
    gb.add("auto", "ana/codex")
    root = gb.req("POST", "/api/v1/sessions/auto/tasks", "jose",
                  json={"title": "Root", "scope": "r", "deliverable": "r"}, rec=gb.rec("auto")).json()["task"]
    child = _task(gb, "Child", parent=f"T{root}", assignee="ana/codex")
    _submit(gb, child, "ana/codex")
    returned = _api(gb, "POST", f"/tasks/{child}/return", "jose/claude", {"feedback": "Revise it"})
    assert returned.status_code == 200 and returned.json()["status"] == "open"
    assert _api(gb, "POST", f"/tasks/{child}/cancel", "jose/claude").status_code == 200
    assert _api(gb, "POST", f"/tasks/{child}/reopen", "jose/claude").status_code == 200
    assert _api(gb, "POST", f"/tasks/{root}/cancel", "jose/claude").status_code == 403


def test_browser_and_cli_can_select_autonomous_mode(gb, live_server):
    c = TestClient(gb.app, base_url="https://testserver", raise_server_exceptions=False, follow_redirects=False)
    c.get("/login")
    c.post("/login", data={"csrf": c.cookies[LOGIN_COOKIE], "token": gb.tok["jose"]})
    html = c.get("/").text
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = c.post("/new-session", data={"csrf": csrf, "op": "session-create-browser-1",
                                     "handle": "browserauto", "title": "Browser test", "autonomous": "1"})
    assert r.status_code == 303
    assert gb.raw.execute("SELECT autonomous FROM session WHERE handle='browserauto'").fetchone()[0] == 1
    cli = live_server.cli("jose", "session", "create", "cliauto", "CLI test", "--autonomous", "--json")
    assert cli.returncode == 0 and '"autonomous": true' in cli.stdout
