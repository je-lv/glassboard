"""B39 (report item N3): one status row per agent, derived only from recorded facts and their ages."""
import pathlib

from glassboard import dashboard
from test_board import b, mk  # noqa: F401  (fixture)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import login
from test_render import assert_canonical
from test_tasks import api, claim

STATIC = pathlib.Path(dashboard.__file__).parent / "static"


def status(gb, who="jose", text=False):
    r = gb.req("GET", "/api/v1/sessions/s1/status" + ("?format=text" if text else ""), who, key=None)
    assert r.status_code == 200, r.text
    return r.text if text else {a["member"]: a for a in r.json()["agents"]}


def test_b39_row_shows_state_reason_claim_checklist_owed_leases_doing_checkpoint(b):  # noqa: F811
    n = mk(b, checklist=["Draft", "Review"])
    assert claim(b, n, extra={"X-Glassboard-Runtime": "r1ab"}).status_code == 201
    api(b, "POST", "/messages", "ana", {"kind": "question", "body": "Which format?", "to": ["jose/claude"]})
    assert api(b, "POST", "/leases", "jose/claude", {"resource": "docs/faq.md"}).status_code == 201
    api(b, "POST", "/messages", "jose/claude", {"kind": "update", "body": "x", "standup": {"doing": "Drafting Q4"}})
    assert api(b, "POST", "/checkpoints", "jose/claude", {"summary": "Q1-Q3", "next_action": "Q4"}).status_code == 201
    row = status(b)["jose/claude"]
    assert row["state"] == "on_task" and row["reason"].startswith(f"Holds the claim on T{n}; last fenced activity")
    assert row["claims"][0]["task"] == n and row["claims"][0]["checklist"] == {"done": 0, "total": 2}
    assert row["owed"]["count"] == 1 and [x["resource"] for x in row["leases"]] == ["docs/faq.md"]
    assert row["doing"]["text"] == "Drafting Q4" and row["doing"]["source"] == "self-reported"
    assert row["checkpoint"]["n"] == 1 and row["checkpoint"]["age_s"] is not None
    assert row["last_event"]["type"] == "checkpoint.saved" and row["last_event"]["age_s"] is not None
    assert row["runs"][0]["run"] == "claude · run r1ab"
    assert status(b)["ana/codex"]["state"] == "idle"
    text = status(b, text=True)
    assert_canonical(text)
    assert "--- @jose/claude · holds a claim ---" in text and "  · doing: «(self-reported, " in text
    assert "  · leases: «docs/faq.md»" in text and f"  · t{n}: «in_progress g1 · checklist 0/2 · Write the FAQ»" in text


def test_b39_states_stale_blocked_review_owed_and_exited_holding(b):  # noqa: F811
    n = mk(b)
    claim(b, n, extra={"X-Glassboard-Runtime": "r1ab"})
    b.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    row = status(b)["jose/claude"]
    assert row["state"] == "stale" and "no fenced activity for" in row["reason"] and "stale after" in row["reason"]
    r = b.req("POST", "/api/v1/sessions/s1/runtime/exited", "jose/claude", json={"exit_code": 2}, rec=b.rec(),
              extra={"X-Glassboard-Runtime": "r1ab"})
    assert r.status_code == 200 and r.json()["seq"]
    row = status(b)["jose/claude"]
    assert row["state"] == "exited_holding" and row["reason"].startswith("Run r1ab exited (exit code 2)")
    q = api(b, "POST", "/messages", "jose", {"kind": "question", "body": "Status?", "to": ["ana/codex"]}).json()["seq"]
    assert status(b)["ana/codex"]["state"] == "owes_answers" and status(b)["ana/codex"]["owed"]["items"][0]["seq"] == q
    m = mk(b, title="Second", assignee="ana/codex")
    claim(b, m, who="ana/codex")
    assert status(b)["ana/codex"]["state"] == "on_task"


def test_b39_never_a_timer_based_working_state(b):  # noqa: F811
    from glassboard import api_status
    assert set(api_status.STATES) == {"exited_holding", "stale", "blocked", "on_task", "in_review", "owes_answers", "idle"}
    joined = " ".join(api_status.STATE_LABEL.values())
    assert "working" not in joined and "online" not in joined and "active" not in joined
    n = mk(b)
    claim(b, n)
    page = login(b).get("/s/s1/board").text
    assert '<span class="tag st st-in_progress">claimed</span>' in page and ">working<" not in page
    assert "nothing here claims one is working right now" in page


def test_b39_dashboard_board_shows_rows_and_both_regions_refresh(b):  # noqa: F811
    n = mk(b)
    claim(b, n, extra={"X-Glassboard-Runtime": "r1ab"})
    c = login(b)
    page = c.get("/s/s1/board").text
    assert 'id="agents-live" data-src="/s/s1/board/agents"' in page and "holds a claim" in page
    assert "claude · run r1ab" in page and "@53xp" not in page
    frag = c.get("/s/s1/board/agents")
    assert frag.status_code == 200 and frag.headers["X-Glassboard-Fragment"] == "poll" and "agent-row" in frag.text
    assert c.get("/s/nope/board/agents").status_code == 404
    poll = (STATIC / "poll.js").read_text(encoding="utf-8")       # B38 regression: the board region was never polled
    assert "#kanban-live[data-src]" in poll and "#agents-live[data-src]" in poll


def test_b39_cli_status_text_and_json(L, gb):  # noqa: F811
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))
    out = ok(L.cli("jose/claude", "status"))
    assert "--- agent status · s1 · " in out and "--- @jose/claude · idle ---" in out
    assert '"agents"' in ok(L.cli("jose/claude", "status", "--json"))


def test_b39_timeline_and_conversations_show_runs_not_at_labels(b):  # noqa: F811
    api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "Hello"}, extra={"X-Glassboard-Runtime": "zz9a"})
    c = login(b)
    for path in ("/s/s1", "/s/s1/conversations?mode=all"):
        html = c.get(path).text
        assert "claude · run zz9a" in html and " · @zz9a</span>" not in html and "runtime zz9a" not in html, path
