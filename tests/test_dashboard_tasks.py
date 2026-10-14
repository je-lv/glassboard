"""Dashboard task page and board panel ("full task lifecycle without the CLI"; by default a task can
be made from a proposal)."""
import re

import pytest

from glassboard import ids
from test_dashboard import csrf_of, login


@pytest.fixture
def d(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    return gb


def agent(gb, method, path, body=None, who="jose/claude", key=True):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body or {}, rec=gb.rec(), key=key)


def form_post(c, gb, url, page_url, **fields):
    html = c.get(page_url).text
    op = re.search(r'name="op" value="([^"]+)"', html).group(1)
    return c.post(url, data={"csrf": csrf_of(html), "recovery": gb.rec(), "op": op + "-x", **fields})


def test_full_task_lifecycle_without_the_cli(d):
    """The human uses only the dashboard; the agent uses only the HTTP API."""
    c = login(d)
    r = form_post(c, d, "/s/s1/tasks", "/s/s1/new-task", title="Write onboarding", scope="Install, tokens, TLS.",
                  deliverable="docs/onboarding.md", assignee="jose/claude", stale_after_s="900")
    assert r.status_code == 303 and r.headers["location"] == "/s/s1/t/1"
    page = c.get("/s/s1/t/1").text
    assert "T1 <span class=\"tag st st-open\">open</span>" in page and "«Write onboarding»" in page
    fence = agent(d, "POST", "/tasks/1/claim").json()["fence"]
    agent(d, "POST", "/tasks/1/note", {"fence": fence, "note": "install drafted"})
    board = c.get("/s/s1").text
    assert re.search(r'<a href="/s/s1/t/1">T1</a>\s*<span class="handle">@jose/claude</span> g1', board)
    # the coordinator edits the scope while the agent works (PATCH keeps the claim)
    r = form_post(c, d, "/s/s1/t/1/edit", "/s/s1/t/1", if_version=str(d.raw.execute("SELECT version FROM task").fetchone()[0]),
                  title="Write onboarding", deliverable="docs/onboarding.md", scope="Install, tokens, TLS, Windows.")
    assert r.status_code == 303 and r.headers["location"] == "/s/s1/t/1"
    assert agent(d, "POST", "/tasks/1/submit", {"fence": fence, "summary": "draft done"}).status_code == 200
    current = agent(d, "GET", "/tasks/1").json()
    r = form_post(c, d, "/s/s1/t/1/return", "/s/s1/t/1", if_version=str(current["version"]),
                  submission_seq=str(current["submission"]["seq"]), feedback="Add the macOS steps.")
    assert r.status_code == 303 and d.raw.execute("SELECT status FROM task").fetchone()[0] == "open"
    fence = agent(d, "POST", "/tasks/1/claim").json()["fence"]
    agent(d, "POST", "/tasks/1/submit", {"fence": fence, "summary": "now with macOS"})
    current = agent(d, "GET", "/tasks/1").json()
    assert form_post(c, d, "/s/s1/t/1/accept", "/s/s1/t/1", if_version=str(current["version"])).status_code == 303
    assert d.raw.execute("SELECT status FROM task").fetchone()[0] == "done"
    current = agent(d, "GET", "/tasks/1").json()
    assert form_post(c, d, "/s/s1/t/1/reopen", "/s/s1/t/1", if_version=str(current["version"]), reason="one more fix").status_code == 303
    current = agent(d, "GET", "/tasks/1").json()
    assert form_post(c, d, "/s/s1/t/1/reassign", "/s/s1/t/1", if_version=str(current["version"]), to="ana", reason="ana has time").status_code == 303
    assert d.raw.execute("SELECT assignee_id FROM task").fetchone()[0] == d.pid("ana")
    current = agent(d, "GET", "/tasks/1").json()
    assert form_post(c, d, "/s/s1/t/1/cancel", "/s/s1/t/1", if_version=str(current["version"]), reason="not needed").status_code == 303
    types = [e["type"] for e in d.events() if e["task_num"] == 1]
    assert types == ["task.created", "task.claimed", "task.note", "task.edited", "task.submitted", "task.returned",
                     "task.claimed", "task.submitted", "task.accepted", "task.reopened", "task.reassigned", "task.cancelled"]
    history = c.get("/s/s1/t/1").text
    for t in ("task.returned", "task.accepted", "task.cancelled"):
        assert f"[{t}]" in history


def test_coordinator_release_from_the_task_page(d):
    c = login(d)
    form_post(c, d, "/s/s1/tasks", "/s/s1/new-task", title="t", scope="s", deliverable="d")
    fence = agent(d, "POST", "/tasks/1/claim").json()["fence"]
    assert form_post(c, d, "/s/s1/t/1/release", "/s/s1/t/1", handoff="runtime is gone").status_code == 303
    assert d.raw.execute("SELECT status, fence_secret FROM task").fetchone()[:] == ("open", None)
    assert agent(d, "POST", "/tasks/1/note", {"fence": fence, "note": "x"}).json()["error"]["code"] == "fence_mismatch"


def test_board_panel_groups_stale_and_owed(d):
    for title in ("a", "b", "c"):
        d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": title, "scope": "s", "deliverable": "d",
              "stale_after_s": 300}, rec=d.rec())
    agent(d, "POST", "/tasks/2/claim")
    agent(d, "POST", "/tasks/3/claim")
    d.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=3")
    d.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "question", "body": "q?", "to": ["jose/claude"]},
          rec=d.rec())
    c = login(d)
    page = c.get("/s/s1").text
    task_panel = page.split('<section class="card board">', 1)[1]
    assert task_panel.index('st-open">open</span>') < task_panel.index('st-in_progress">in progress</span>')
    assert re.search(r'<li class="stale"><a href="/s/s1/t/3">T3</a>', page) and 'class="tag stale">stale' in page
    assert re.search(r'<li class=""><a href="/s/s1/t/2">T2</a>', page)
    assert "@jose/claude: 1 (oldest #" in page                              # owed questions per agent


def test_o5_make_a_task_from_a_proposal(d):
    d.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
          json={"kind": "proposal", "body": "Add a troubleshooting section\nCover the three common TLS errors."}, rec=d.rec())
    prop = d.head()
    c = login(d)
    assert f'href="/s/s1/new-task?from={prop}"' in c.get("/s/s1").text
    form = c.get(f"/s/s1/new-task?from={prop}").text
    assert 'value="Add a troubleshooting section"' in form and "Cover the three common TLS errors." in form
    r = form_post(c, d, "/s/s1/tasks", f"/s/s1/new-task?from={prop}", title="Add a troubleshooting section",
                  scope="Cover the three common TLS errors.", deliverable="docs/troubleshooting.md", from_proposal=str(prop))
    assert r.status_code == 303
    ev = [e for e in d.events() if e["type"] == "task.created"][-1]
    assert ev["reply_to"] == prop and ev["data"]["from_proposal"] == prop                  # traceable to the proposal
    ana = login(d, "ana")
    assert "make a task from" not in ana.get("/s/s1").text                                  # coordinators only
    assert d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": "t", "scope": "s", "deliverable": "d",
                 "from_proposal": 1}, rec=d.rec()).status_code == 404                       # #1 is not a proposal


def test_task_forms_are_coordinator_only_and_csrf_protected(d):
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": "t", "scope": "s", "deliverable": "d"}, rec=d.rec())
    ana = login(d, "ana")
    page = ana.get("/s/s1/t/1").text
    assert "Only human Session Managers manage tasks here" in page and "/t/1/cancel" not in page
    r = ana.post("/s/s1/t/1/cancel", data={"csrf": csrf_of(page), "recovery": d.rec(), "op": ids.new_ulid()})
    assert r.status_code == 303 and r.headers["location"].endswith("err=forbidden")
    jose = login(d)
    assert jose.post("/s/s1/t/1/cancel", data={"recovery": d.rec()}).status_code == 403          # no CSRF token
    assert d.raw.execute("SELECT status FROM task").fetchone()[0] == "open"


def test_assigned_human_acknowledges_in_browser(d):
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose",
          json={"title": "t", "scope": "s", "deliverable": "d", "assignee": "ana"}, rec=d.rec())
    ana = login(d, "ana")
    assert "Acknowledge assignment" in ana.get("/s/s1/t/1").text
    assert form_post(ana, d, "/s/s1/t/1/acknowledge", "/s/s1/t/1").status_code == 303
    assert "Assignment: acknowledged" in ana.get("/s/s1/t/1").text


def test_named_reviewer_binds_browser_verdict_to_exact_submission(d):
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose",
          json={"title": "t", "scope": "s", "deliverable": "d", "assignee": "jose/claude",
                "reviewer": "ana"}, rec=d.rec())
    fence = agent(d, "POST", "/tasks/1/claim").json()["fence"]
    commit = "a" * 40
    submitted = agent(d, "POST", "/tasks/1/submit",
                      {"fence": fence, "summary": "ready", "commit": commit}).json()["seq"]
    ana = login(d, "ana")
    page = ana.get("/s/s1/t/1").text
    assert "Record peer review" in page and f'value="{submitted}"' in page and f'value="{commit}"' in page
    result = form_post(ana, d, "/s/s1/t/1/review", "/s/s1/t/1", verdict="approve",
                       submission_seq=str(submitted), commit=commit, notes="Checked exact commit.")
    assert result.status_code == 303
    reviewed = d.events()[-1]
    assert reviewed["type"] == "task.reviewed" and reviewed["data"]["submission_seq"] == submitted
    assert reviewed["data"]["commit"] == commit


def test_stale_version_edit_says_reload(d):
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": "t", "scope": "s", "deliverable": "d"}, rec=d.rec())
    c = login(d)
    r = form_post(c, d, "/s/s1/t/1/edit", "/s/s1/t/1", if_version="99", title="x")
    assert r.status_code == 303 and r.headers["location"].endswith("err=version_conflict")
    assert "The task changed since this page was loaded" in c.get(r.headers["location"]).text


def test_task_pages_are_inert_and_canonical(d):
    x = "<script>alert(1)</script>\u202e"
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": x, "scope": x, "deliverable": x}, rec=d.rec())
    c = login(d)
    for path in ("/s/s1/t/1", "/s/s1", "/s/s1/new-task"):
        t = c.get(path).text
        assert "<script>alert" not in t and "\u202e" not in t
    assert "⟨U+202E⟩" in c.get("/s/s1/t/1").text
    assert c.get("/s/s1/t/99").status_code == 404


def test_editing_one_field_never_rewrites_a_field_with_hidden_characters(d):
    title = "Fix\u202e the build"
    d.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": title, "scope": "s", "deliverable": "d"}, rec=d.rec())
    c = login(d)
    page = c.get("/s/s1/t/1").text
    assert "contains hidden or control characters" in page and "\u202e" not in page
    assert re.search(r'name="title" value=""', page)                       # not pre-filled
    r = form_post(c, d, "/s/s1/t/1/edit", "/s/s1/t/1", if_version="1", title="", deliverable="d", scope="new scope")
    assert r.status_code == 303
    row = d.raw.execute("SELECT title, scope FROM task").fetchone()
    assert row["title"] == title and row["scope"] == "new scope"           # exact bytes kept
