"""The task-division pack: dependencies (ready vs waiting), parent/child tasks, breakdown proposals
with task drafts, priority, and `task next`."""
import pytest

from test_board import b, mk, view  # noqa: F401  (fixture and helpers)
from test_render import assert_canonical
from test_tasks import api, claim


def mkx(gb, title, **extra):
    body = {"title": title, "scope": "s", "deliverable": "d", **extra}
    r = api(gb, "POST", "/tasks", "jose", body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


# --- dependencies ---------------------------------------------------------------------------------------

def test_a_task_is_ready_only_when_its_dependencies_are_done(b):
    a = mkx(b, "Outline", assignee="jose/claude")
    w = mkx(b, "Write", assignee="jose/claude", depends_on=[f"T{a}"])
    v = view(b, w)
    assert v["ready"] is False and v["depends_on"] == [{"task": a, "status": "open"}]
    r = claim(b, w)
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_waiting"
    assert r.json()["error"]["details"]["needs"] == [f"T{a}"]
    f = claim(b, a).json()["fence"]
    api(b, "POST", f"/tasks/{a}/submit", "jose/claude", {"fence": f, "summary": "outline done"})
    api(b, "POST", f"/tasks/{a}/accept", "jose", {"if_version": view(b, a)["version"]})
    assert view(b, w)["ready"] is True and claim(b, w).status_code == 201
    assert "needs" not in view(b, w, "?format=text").splitlines()[0]


def test_dependency_validation_and_cycles(b):
    a = mkx(b, "A")
    c = mkx(b, "C", depends_on=[a])
    r = api(b, "POST", "/tasks", "jose", {"title": "x", "scope": "s", "deliverable": "d", "depends_on": ["T99"]})
    assert r.status_code == 422
    v = view(b, a)
    r = api(b, "PATCH", f"/tasks/{a}", "jose", {"if_version": v["version"], "depends_on": [f"T{c}"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "dependency_cycle"
    r = api(b, "PATCH", f"/tasks/{a}", "jose", {"if_version": v["version"], "depends_on": [f"T{a}"]})
    assert r.status_code == 422
    r = api(b, "PATCH", f"/tasks/{c}", "jose/claude", {"if_version": view(b, c)["version"], "depends_on": []})
    assert r.status_code == 403                                                    # humans define the work
    r = api(b, "PATCH", f"/tasks/{c}", "jose", {"if_version": view(b, c)["version"], "depends_on": []})
    assert r.status_code == 200 and view(b, c)["depends_on"] == [] and b.events()[-1]["data"]["depends_on"] == "none"


# --- parents and children -----------------------------------------------------------------------------

def test_children_roll_up_and_accepting_a_parent_early_warns(b):
    parent = mkx(b, "The whole FAQ", assignee="jose/claude")
    k1 = mkx(b, "Q1", parent=f"T{parent}", assignee="jose/claude")
    mkx(b, "Q2", parent=parent)
    assert view(b, parent)["children"] == {"done": 0, "total": 2} and view(b, k1)["parent"] == parent
    f = claim(b, k1).json()["fence"]
    api(b, "POST", f"/tasks/{k1}/submit", "jose/claude", {"fence": f, "summary": "q1"})
    api(b, "POST", f"/tasks/{k1}/accept", "jose", {"if_version": view(b, k1)["version"]})
    assert view(b, parent)["children"] == {"done": 1, "total": 2}
    assert "children 1/2 done" in view(b, parent, "?format=text").splitlines()[0]
    fp = claim(b, parent).json()["fence"]
    api(b, "POST", f"/tasks/{parent}/submit", "jose/claude", {"fence": fp, "summary": "all"})
    r = api(b, "POST", f"/tasks/{parent}/accept", "jose", {"if_version": view(b, parent)["version"]})
    assert r.status_code == 200 and r.json()["warning"] == "1 child task(s) not done"


def test_parent_cycles_are_refused(b):
    a = mkx(b, "A")
    c = mkx(b, "C", parent=a)
    r = api(b, "PATCH", f"/tasks/{a}", "jose", {"if_version": view(b, a)["version"], "parent": f"T{c}"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "dependency_cycle"


# --- breakdown proposals --------------------------------------------------------------------------------

def test_proposal_drafts_render_and_become_tasks(b):
    drafts = [{"title": "Install section", "scope": "docs/install.md", "deliverable": "a page", "assignee": "jose/claude"},
              {"title": "Recovery section", "scope": "docs/recovery.md", "deliverable": "a page"}]
    r = api(b, "POST", "/messages", "jose/claude", {"kind": "proposal", "body": "Split the FAQ", "drafts": drafts})
    assert r.status_code == 201, r.text
    seq = r.json()["seq"]
    text = b.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None).text
    assert_canonical(text)
    assert "  · draft: «1. Install section · deliverable: a page · for @jose/claude»" in text
    assert any(i["kind"] == "proposal" for i in b.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"])
    r = api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "x", "drafts": drafts})
    assert r.status_code == 422                                                    # drafts only in proposals
    for d in drafts:
        n = mkx(b, d["title"], from_proposal=seq, **({"assignee": d["assignee"]} if d.get("assignee") else {}))
        assert b.events()[-1]["reply_to"] == seq and view(b, n)["title"] == d["title"]
    assert not any(i["kind"] == "proposal" for i in b.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"])


def test_message_digest_without_drafts_is_unchanged(b):
    """Receipts stored before drafts existed must still replay: a message without drafts hashes exactly
    as the earlier payload shape (no `drafts` key at all)."""
    from glassboard import idempotency
    k = "same-key-0001"
    r1 = api(b, "POST", "/messages", "jose", {"kind": "note", "body": "hello"}, key=k)
    row = b.raw.execute("SELECT req_digest, session_id FROM event WHERE op_id=?", (k,)).fetchone()
    old_shape = {"kind": "note", "body": "hello", "to": [], "re": None, "task": None, "refs": []}
    assert bytes(row["req_digest"]) == idempotency.digest("POST /sessions/{s}/messages", b.pid("jose"), old_shape,
                                                          session_id=row["session_id"])
    r2 = api(b, "POST", "/messages", "jose", {"kind": "note", "body": "hello", "drafts": []}, key=k)
    assert r2.status_code == 201 and r2.content == r1.content and r2.headers["glassboard-replayed"] == "true"


# --- priority and next ----------------------------------------------------------------------------------

def test_priority_orders_the_board_and_task_next(b):
    low = mkx(b, "Low one", priority="low")
    norm = mkx(b, "Normal one")
    urgent = mkx(b, "Urgent one", priority="urgent", assignee="jose/claude")
    mkx(b, "Not mine", priority="urgent", assignee="ana/codex")
    blocked_by = mkx(b, "Waiting", priority="high", depends_on=[norm])
    nxt = b.req("GET", "/api/v1/sessions/s1/tasks/next", "jose/claude", key=None).json()["tasks"]
    assert [t["task"] for t in nxt] == [urgent, norm, low]                        # ready, mine or nobody's, urgent first
    assert blocked_by not in [t["task"] for t in nxt]
    board = b.req("GET", "/api/v1/sessions/s1/board", "jose", key=None).json()
    assert [t["task"] for t in board["columns"][0]["tasks"]][:2] == [urgent, 4]
    text = b.req("GET", "/api/v1/sessions/s1/tasks/next?format=text", "jose/claude", key=None).text
    assert_canonical(text)
    assert text.startswith("--- 3 task(s) ready for you in s1 ---") and "· urgent" in text
    r = api(b, "POST", "/tasks", "jose", {"title": "x", "scope": "s", "deliverable": "d", "priority": "asap"})
    assert r.status_code == 422


# --- CLI ------------------------------------------------------------------------------------------------

def test_cli_division_flow(b, live_server):
    for who in ("jose", "jose/claude"):
        assert live_server.cli(who, "use", "s1").returncode == 0
    r = live_server.cli("jose/claude", "post", "proposal", "Split the FAQ",
                        "--draft", "Install|docs/install.md|a page|jose/claude", "--draft", "Recovery|docs/recovery.md|a page")
    assert r.returncode == 0, r.stderr
    seq = int(r.stdout.split("#")[1].split()[0])
    r = live_server.cli("jose", "task", "create", "--from-proposal", str(seq), "--draft", "all", "--priority", "high")
    assert r.returncode == 0 and r.stdout.count("created") == 2, r.stderr
    r = live_server.cli("jose", "task", "create", "Publish", "--scope", "s", "--deliverable", "d",
                        "--depends-on", "T1,T2", "--parent", "T1")
    assert r.returncode == 0, r.stderr
    nxt = live_server.cli("jose/claude", "task", "next")
    assert nxt.returncode == 0 and "T1 · open" in nxt.stdout and "T3" not in nxt.stdout
    show = live_server.cli("jose", "task", "show", "T3")
    assert "needs T1, T2" in show.stdout and "parent T1" in show.stdout
    r = live_server.cli("jose/claude", "task", "claim", "T3")
    assert r.returncode == 5 and "task_waiting" in r.stderr


# --- dashboard ------------------------------------------------------------------------------------------

def test_dashboard_creates_all_drafted_tasks(b):
    import re as _re
    from test_dashboard import login
    drafts = [{"title": "A \u202e part", "scope": "s", "deliverable": "d"}, {"title": "B part", "scope": "s", "deliverable": "d"}]
    seq = api(b, "POST", "/messages", "jose/claude", {"kind": "proposal", "body": "Split", "drafts": drafts}).json()["seq"]
    c = login(b, "jose")
    page = c.get(f"/s/s1/new-task?from={seq}")
    assert "Create all 2 tasks" in page.text and "\u202e" not in page.text
    csrf = _re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    op = _re.search(r'name="op" value="([^"]+)"', page.text).group(1)
    form = {"csrf": csrf, "op": op, "recovery": b.rec(), "from_proposal": str(seq)}
    r = c.post("/s/s1/tasks-from-drafts", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/s/s1/board"
    c.post("/s/s1/tasks-from-drafts", data=form)                                  # resubmitted: nothing twice
    titles = [t["title"] for t in b.req("GET", "/api/v1/sessions/s1/tasks", "jose", key=None).json()["tasks"]]
    assert titles == ["A \u202e part", "B part"]
    r = c.post("/s/s1/tasks-from-drafts", data={**form, "from_proposal": "1"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
