"""The visibility pack: checklists (progress), what a blocked task waits on, the session board,
a human's attention queue across sessions, and the overview."""
import re

import pytest

from test_render import assert_canonical
from test_tasks import api, claim, task

FENCE = re.compile(r"T\d+g\d+-[a-z2-7]{10}")


@pytest.fixture
def b(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana/codex")
    return gb


def mk(gb, title="Write the FAQ", assignee="jose/claude", checklist=None, who="jose"):
    body = {"title": title, "scope": "docs/faq.md", "deliverable": "a page", "assignee": assignee}
    if checklist is not None:
        body["checklist"] = checklist
    r = api(gb, "POST", "/tasks", who, body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


def view(gb, n, fmt=""):
    r = gb.req("GET", f"/api/v1/sessions/s1/tasks/{n}{fmt}", "jose", key=None)
    return r.text if fmt else r.json()


# --- checklists ---------------------------------------------------------------------------------------

def test_checklist_progress_is_ticked_by_the_claimant_with_its_fence(b):
    n = mk(b, checklist=["Install", "Join a session", "Recover after a restore"])
    v = view(b, n)
    assert v["progress"] == {"done": 0, "total": 3} and [i["id"] for i in v["checklist"]] == [1, 2, 3]
    fence = claim(b, n).json()["fence"]
    before = task(b, n)["last_activity_at"]
    r = api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 2})
    assert r.status_code == 200, r.text
    assert view(b, n)["progress"] == {"done": 1, "total": 3}
    ev = b.events()[-1]
    assert ev["type"] == "task.checked" and ev["data"]["progress"] == "1/3" and ev["data"]["title"] == "Join a session"
    assert b.recipients("s1", ev["seq"]) == []                                         # routed to no inbox
    assert task(b, n)["last_activity_at"] > before                                     # counts as activity
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 2, "done": False})
    assert view(b, n)["progress"]["done"] == 0
    text = view(b, n, "?format=text")
    assert_canonical(text)
    assert "· 0/3 done" in text and "  · todo: «2. Join a session»" in text


def test_check_needs_the_fence_and_a_real_item(b):
    n = mk(b, checklist=["One"])
    fence = claim(b, n).json()["fence"]
    r = api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 7})
    assert r.status_code == 422 and r.json()["error"]["code"] == "checklist_item_unknown"
    r = api(b, "POST", f"/tasks/{n}/check", "ana/codex", {"fence": fence, "item": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "fence_mismatch"
    assert view(b, n)["progress"]["done"] == 0


def test_only_humans_define_the_checklist_and_edits_keep_ticks(b):
    n = mk(b, checklist=["A", "B"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    v = view(b, n)
    r = api(b, "PATCH", f"/tasks/{n}", "jose/claude", {"if_version": v["version"], "checklist": ["mine"]})
    assert r.status_code == 403
    r = api(b, "PATCH", f"/tasks/{n}", "jose", {"if_version": v["version"], "checklist": ["C", "A"]})
    assert r.status_code == 200, r.text
    assert [(i["id"], i["text"], i["done"]) for i in view(b, n)["checklist"]] == [(1, "C", False), (2, "A", True)]
    assert b.events()[-1]["data"]["checklist"] == ["C", "A"]
    assert view(b, n)["gen"] == v["gen"]                                               # editing never bumps gen
    r = api(b, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d", "checklist": [""]})
    assert r.status_code == 422


def test_check_is_refused_while_paused(b):
    n = mk(b, checklist=["A"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", "/state", "jose", {"state": "paused", "reason": "lunch"})
    r = api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_paused"


def test_submit_requires_complete_checklist_or_records_an_explicit_override(b):
    n = mk(b, checklist=["Draft", "Verify"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    refused = api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "Draft ready"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "checklist_incomplete"
    assert refused.json()["error"]["details"]["unchecked_items"] == [2]
    accepted = api(b, "POST", f"/tasks/{n}/submit", "jose/claude",
                   {"fence": fence, "summary": "Draft ready", "incomplete_checklist_reason": "External review is pending."})
    assert accepted.status_code == 200
    assert b.events()[-1]["data"]["incomplete_checklist_reason"] == "External review is pending."


# --- waiting on ---------------------------------------------------------------------------------------

def test_waiting_on_a_question_task_or_member(b):
    n = mk(b)
    other = mk(b, title="Collect questions", assignee="ana/codex")
    fence = claim(b, n).json()["fence"]
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which format?", "to": ["ana"]}).json()["seq"]
    r = api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "need the format", "waiting_on": f"#{q}"})
    assert r.status_code == 200, r.text
    assert view(b, n)["waiting_on"] == {"kind": "question", "label": f"#{q}", "resolved": False}
    api(b, "POST", "/messages", "ana", {"kind": "answer", "body": "Markdown", "re": q})
    assert view(b, n)["waiting_on"]["resolved"] is True                               # shown, never acted on
    assert view(b, n)["status"] == "blocked"
    api(b, "POST", f"/tasks/{n}/unblock", "jose/claude", {"fence": fence})
    assert view(b, n)["waiting_on"] is None                                            # cleared when it leaves blocked
    api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "r", "waiting_on": f"T{other}"})
    assert view(b, n)["waiting_on"] == {"kind": "task", "label": f"T{other}", "resolved": False}
    api(b, "POST", f"/tasks/{n}/unblock", "jose/claude", {"fence": fence})
    r = api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "r", "waiting_on": "@ana"})
    ev = b.events()[-1]
    assert ev["data"]["waiting_on"] == "@ana" and "ana" in b.recipients("s1", ev["seq"])   # the waited-on member is told
    assert view(b, n)["waiting_on"] == {"kind": "member", "label": "@ana", "resolved": None}


@pytest.mark.parametrize("ref", ["#999", "T99", "@nobody", "ana", "#1x", "@ANA"])
def test_waiting_on_unknown_targets_are_refused_without_echo(b, ref):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    r = api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "r", "waiting_on": ref})
    assert r.status_code == 422 and ref not in r.text
    assert view(b, n)["status"] == "in_progress"


def test_sync_claims_show_progress_and_waiting(b):
    n = mk(b, checklist=["A", "B"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "r", "waiting_on": "@jose"})
    txt = b.client.post("/api/v1/sessions/s1/sync?format=text", json={}, headers=b.headers("jose/claude", rec=b.rec())).text
    assert f"claims: T{n} blocked g1 1/2 waiting on @jose" in txt


# --- board --------------------------------------------------------------------------------------------

def test_board_columns_cards_and_text(b):
    a = mk(b, title="Open one", assignee=None)
    w = mk(b, title="Working", checklist=["x", "y"])
    fence = claim(b, w).json()["fence"]
    api(b, "POST", f"/tasks/{w}/note", "jose/claude", {"fence": fence, "note": "Half way \u202e there"})
    c = mk(b, title="Cancelled one")
    api(b, "POST", f"/tasks/{c}/cancel", "jose", {"if_version": view(b, c)["version"]})
    r = b.req("GET", "/api/v1/sessions/s1/board", "ana/codex", key=None)
    j = r.json()
    cols = {col["status"]: [t["task"] for t in col["tasks"]] for col in j["columns"]}
    assert cols["open"] == [a] and cols["in_progress"] == [w] and j["counts"]["cancelled"] == 1
    card = j["columns"][1]["tasks"][0]
    assert card["last_note"]["by"] == "jose/claude" and card["progress"] == {"done": 0, "total": 2}
    assert not FENCE.search(r.text)
    text = b.req("GET", "/api/v1/sessions/s1/board?format=text", "ana/codex", key=None).text
    assert_canonical(text)
    assert text.startswith("--- board · s1 (active) · open 1 · in progress 1 · blocked 0 · review 0 · done 0 · cancelled 1 ---")
    assert "  · last note: «jose/claude, 0m ago: Half way ⟨U+202E⟩ there»" in text and not FENCE.search(text)


def test_board_is_members_only(b):
    b.session("s2", who="ana")
    assert b.req("GET", "/api/v1/sessions/s2/board", "jose/claude", key=None).status_code == 404


def test_board_planning_cues_show_unrecorded_roles_and_independent_children(b):
    parent = mk(b, title="Project")
    child = api(b, "POST", "/tasks", "jose", {"title": "Build", "scope": "work", "deliverable": "code",
                                              "parent": f"T{parent}"}).json()["task"]
    board = b.req("GET", "/api/v1/sessions/s1/board", "jose", key=None).json()
    assert "jose/claude" in board["planning"]["agents_without_project_role"]
    assert child in board["planning"]["child_tasks_without_dependencies"]
    from test_dashboard import login
    html = login(b).get("/s/s1/board").text
    assert "Project role not recorded" in html and "No dependencies recorded" in html


def test_task_review_trail_and_quiet_timeline_keep_full_history(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "Implemented parser in commit abc123"})
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude",
        {"fence": fence, "summary": "Parser implemented in commit abc123; tests pass."})
    trail = b.req("GET", f"/api/v1/sessions/s1/tasks/{n}/trail", "jose", key=None).json()["trail"]
    assert [item["type"] for item in trail] == ["task.created", "task.claimed", "task.note", "task.submitted"]
    assert "commit abc123" in trail[-1]["detail"]
    from test_dashboard import login
    c = login(b)
    assert "Review trail" in c.get(f"/s/s1/t/{n}").text
    assert "Implemented parser in commit abc123" in c.get(f"/s/s1/t/{n}").text
    assert "work · hide leases" in c.get("/s/s1?f=work").text


# --- attention and overview ------------------------------------------------------------------------------

def test_attention_collects_what_needs_a_human(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "done"})
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which tone?", "to": ["jose"]}).json()["seq"]
    m = mk(b, title="Blocked on jose")
    f2 = claim(b, m).json()["fence"]
    api(b, "POST", f"/tasks/{m}/block", "jose/claude", {"fence": f2, "reason": "r", "waiting_on": "@jose"})
    p = api(b, "POST", "/messages", "jose/claude", {"kind": "proposal", "body": "Split the FAQ in two"}).json()["seq"]
    a = b.req("GET", "/api/v1/me/attention", "jose", key=None).json()
    kinds = {(i["kind"], i.get("task") or i.get("seq")) for i in a["items"]}
    assert {("review", n), ("owed", q), ("waiting_on_you", m), ("proposal", p)} <= kinds
    text = b.req("GET", "/api/v1/me/attention?format=text", "jose", key=None).text
    assert_canonical(text)
    assert f"--- review · s1 T{n} · 0m ---" in text and "  · text: «Split the FAQ in two»" in text
    # agents never get coordinator items, and ana (not a coordinator, not waited on) gets nothing
    agent = b.req("GET", "/api/v1/me/attention", "jose/claude", key=None).json()
    assert not [i for i in agent["items"] if i["kind"] in ("review", "stale", "proposal", "loop")]
    assert b.req("GET", "/api/v1/me/attention?format=text", "ana", key=None).text.endswith("nothing needs you ---\n")


def test_overview_counts_per_session(b):
    mk(b, assignee=None)
    n = mk(b)
    claim(b, n)
    b.session("s2", who="jose", title="Second goal")
    o = b.req("GET", "/api/v1/me/overview", "jose", key=None).json()
    s1 = next(r for r in o["sessions"] if r["session"] == "s1")
    assert s1["counts"]["open"] == 1 and s1["counts"]["in_progress"] == 1 and s1["role"] == "coordinator"
    assert [r["session"] for r in o["sessions"]] == ["s1", "s2"]
    text = b.req("GET", "/api/v1/me/overview?format=text", "jose", key=None).text
    assert_canonical(text)
    assert "--- s1 · active · coordinator · open 1 · in progress 1 · blocked 0 · review 0 · done 0 · stale 0 · owed 0 ---" in text


# --- CLI ------------------------------------------------------------------------------------------------

def test_cli_checklist_board_attention(b, live_server):
    for who in ("jose", "jose/claude"):
        assert live_server.cli(who, "use", "s1").returncode == 0
    r = live_server.cli("jose", "task", "create", "Write the FAQ", "--scope", "docs/faq.md", "--deliverable", "a page",
                        "--assignee", "jose/claude", "--check", "Install", "--check", "Recovery")
    assert r.returncode == 0, r.stderr
    assert live_server.cli("jose/claude", "task", "claim", "T1").returncode == 0
    r = live_server.cli("jose/claude", "task", "check", "T1", "Recovery")
    assert r.returncode == 0, r.stderr
    bad = live_server.cli("jose/claude", "task", "check", "T1", "Missing label")
    assert bad.returncode == 2 and "1. Install" in bad.stderr and "2. Recovery" in bad.stderr
    r = live_server.cli("jose/claude", "task", "block", "T1", "need input", "--waiting-on", "@jose")
    assert r.returncode == 0, r.stderr
    board = live_server.cli("jose", "board")
    assert board.returncode == 0 and "1/2 done · waiting on @jose" in board.stdout
    assert_canonical(board.stdout)
    att = live_server.cli("jose", "attention")
    assert att.returncode == 0 and "--- waiting on you · s1 T1" in att.stdout
    ov = live_server.cli("jose", "overview")
    assert ov.returncode == 0 and "--- s1 · active · coordinator" in ov.stdout
    for out in (board.stdout, att.stdout, ov.stdout):
        assert not FENCE.search(out)


# --- dashboard ------------------------------------------------------------------------------------------

def test_dashboard_board_page_and_attention_home(b):
    from test_dashboard import login
    n = mk(b, title="Draft \u202e the FAQ", checklist=["A", "B"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "note <script>x</script>"})
    api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "r", "waiting_on": "@jose"})
    c = login(b, "jose")
    r = c.get("/s/s1/board")
    assert r.status_code == 200 and "content-security-policy" in r.headers
    html = r.text
    assert f'<a href="/s/s1/t/{n}">T{n}</a>' in html and '<progress value="1" max="2">' in html
    assert "waiting on @jose" in html and "\u202e" not in html and "⟨U+202E⟩" in html
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert 'style="' not in html                                         # the CSP forbids inline styles
    home = c.get("/").text
    assert "Needs your attention" in home and f"s1 T{n}" in home and "waiting on you" in home
    assert ">open 0</span> <span class=\"tag st st-in_progress\">in progress 0</span> <span class=\"tag st st-blocked\">blocked 1</span>" in home
    assert c.get("/s/nope/board").status_code == 404


def test_progress_tab_shows_recorded_work_and_who_did_it(b):
    from test_dashboard import login

    n = mk(b, title="Write <script>unsafe</script> FAQ", checklist=["Draft page"])
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/check", "jose/claude", {"fence": fence, "item": 1})
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "FAQ drafted"})
    api(b, "POST", f"/tasks/{n}/accept", "jose", {"if_version": view(b, n)["version"]})

    c = login(b, "ana")
    before = b.raw.execute("SELECT cursor_seq FROM member WHERE session_id=(SELECT id FROM session WHERE handle='s1') "
                           "AND principal_id=(SELECT id FROM principal WHERE handle='ana')").fetchone()[0]
    r = c.get("/s/s1/progress")
    assert r.status_code == 200 and "content-security-policy" in r.headers
    html = r.text
    assert 'aria-current="page">Progress</a>' in html
    assert "FAQ drafted" in html and "@jose/claude" in html and "@jose" in html
    assert "1 of 1</strong> work tasks accepted" in html and "Who is doing what" in html
    assert "Checklist completed" in html and "Submitted for review" in html and "Accepted" in html
    assert '<progress value="1" max="1">' in html
    assert "<script>unsafe</script>" not in html and "&lt;script&gt;unsafe&lt;/script&gt;" in html
    assert 'style="' not in html
    timeline = c.get("/s/s1").text
    assert 'class="ln inl field-title"' in timeline and 'class="ln inl field-deliverable"' in timeline
    assert 'class="ln inl field-check"' in timeline
    after = b.raw.execute("SELECT cursor_seq FROM member WHERE session_id=(SELECT id FROM session WHERE handle='s1') "
                          "AND principal_id=(SELECT id FROM principal WHERE handle='ana')").fetchone()[0]
    assert before == after
    assert c.get("/s/nope/progress").status_code == 404


def test_progress_overall_counts_child_work_once(b):
    from test_dashboard import login

    parent = mk(b, title="Coordinate the project")
    child = api(b, "POST", "/tasks", "jose", {"title": "Build component", "scope": "implementation",
                                           "deliverable": "component", "assignee": "jose/claude", "parent": parent}).json()["task"]
    fence = claim(b, child).json()["fence"]
    api(b, "POST", f"/tasks/{child}/submit", "jose/claude", {"fence": fence, "summary": "component built"})
    api(b, "POST", f"/tasks/{child}/accept", "jose", {"if_version": view(b, child)["version"]})
    html = login(b, "ana").get("/s/s1/progress").text
    assert "1 of 1</strong> work tasks accepted" in html
    assert "Linked tasks: 1/1 done" in html


def test_progress_tab_is_visible_to_observer(gb):
    from test_dashboard import login

    gb.session("s1")
    gb.add("s1", "ana", role="observer")
    page = login(gb, "ana").get("/s/s1/progress")
    assert page.status_code == 200
    assert "Overall progress" in page.text and "Who is doing what" in page.text


def test_run_status_names_tasks_dependencies_and_results_without_moving_cursor(b):
    from test_dashboard import login

    completed = mk(b, title="Build archive verifier")
    fence = claim(b, completed).json()["fence"]
    api(b, "POST", f"/tasks/{completed}/submit", "jose/claude",
        {"fence": fence, "summary": "Verifier implemented and reviewed"})
    api(b, "POST", f"/tasks/{completed}/accept", "jose", {"if_version": view(b, completed)["version"]})
    pending = api(b, "POST", "/tasks", "jose", {
        "title": "Restore archive", "scope": "restore.py", "deliverable": "Atomic restore",
        "assignee": "ana/codex"}).json()["task"]
    integration = api(b, "POST", "/tasks", "jose", {
        "title": "Integrate CLI", "scope": "cli.py", "deliverable": "Three commands",
        "assignee": "ana/codex", "depends_on": [pending]}).json()["task"]

    c = login(b, "ana")
    before = b.raw.execute("SELECT cursor_seq FROM member WHERE session_id=(SELECT id FROM session WHERE handle='s1') "
                           "AND principal_id=(SELECT id FROM principal WHERE handle='ana')").fetchone()[0]
    for path in ("/s/s1/status", "/s/s1"):
        r = c.get(path)
        assert r.status_code == 200 and "content-security-policy" in r.headers
        assert "Run status" in r.text
        assert '<details class="run-status-group" data-persist="current-work">' in r.text
        assert '<details class="run-status-group" data-persist="achieved-work">' in r.text
        assert "Build archive verifier" in r.text and "Verifier implemented and reviewed" in r.text
        assert "Integrate CLI" in r.text
        assert f"T{pending} (" in r.text and "Restore archive" in r.text
        assert "1 of 3</strong> work tasks accepted" in r.text
    board = c.get("/s/s1/board")
    assert board.status_code == 200 and "Run status" not in board.text
    after = b.raw.execute("SELECT cursor_seq FROM member WHERE session_id=(SELECT id FROM session WHERE handle='s1') "
                          "AND principal_id=(SELECT id FROM principal WHERE handle='ana')").fetchone()[0]
    assert before == after
    assert c.get("/s/nope/status").status_code == 404


def test_kanban_card_can_expand_full_task_text(b):
    from test_dashboard import login

    title = "Create archive " + "source safety " * 12 + "final detail"
    deliverable = "A complete implementation with a readable result"
    n = api(b, "POST", "/tasks", "jose", {
        "title": title, "scope": "create.py", "deliverable": deliverable,
        "assignee": "jose/claude", "reviewer": "ana/codex"}).json()["task"]
    html = login(b, "ana").get("/s/s1/board").text
    assert "<details class=\"kdetails\">" in html and "Show full task details" in html
    assert title in html and deliverable in html
    assert "Reviewer</dt><dd>@ana/codex" in html
    assert f"/s/s1/t/{n}" in html


def test_idle_coordination_task_names_its_linked_work(b):
    from test_dashboard import login

    parent = mk(b, title="Coordinate snapshot project", assignee="ana/codex")
    child = api(b, "POST", "/tasks", "jose", {
        "title": "Implement archive verifier", "scope": "archive.py", "deliverable": "Verifier",
        "assignee": "jose/claude", "parent": parent}).json()["task"]
    assert api(b, "POST", f"/tasks/{parent}/claim", "ana/codex").status_code == 201
    assert claim(b, child).status_code == 201
    b.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (parent,))

    c = login(b, "ana")
    status = c.get("/s/s1/status").text
    board = c.get("/s/s1/board").text
    assert status.index("Coordination needs an update") < status.index("Current work")
    for html in (status, board):
        assert "Implement archive verifier" in html
        assert "Own task idle" in html or "Coordination needs an update" in html
        assert f"T{child}" in html and "in progress" in html
