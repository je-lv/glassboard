"""ERRATA E44: task replay, the task's recorded events as a sequence between members."""
from glassboard import replay
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_dashboard import login
from test_tasks import api, claim

SHA = "c" * 40


def test_replay_draws_routed_events_and_includes_replies(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which tone?", "to": ["jose"], "task": f"T{n}"}).json()["seq"]
    a = api(b, "POST", "/messages", "jose", {"kind": "answer", "body": "Plain and short", "re": q}).json()["seq"]   # no task
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "Wrote it; see docs/faq.md", "commit": SHA})
    r = replay.replay(b.raw, b.raw.execute("SELECT id FROM session WHERE handle='s1'").fetchone()[0], n)
    seqs = [s["seq"] for s in r["steps"]]
    assert q in seqs and a in seqs                              # the answer joins through reply_to
    assert [l["handle"] for l in r["lanes"]][:2] == ["jose", "jose/claude"]
    claimed = next(s for s in r["steps"] if s["type"] == "task.claimed")
    assert claimed["self"] and not claimed["x_targets"]          # routed to nobody: a dot on its own lane
    asked = next(s for s in r["steps"] if s["seq"] == q)
    assert asked["to"] == ["jose"] and len(asked["x_targets"]) == 1 and asked["cat"] == "question"


def test_replay_page_works_without_script_and_links_back(b):
    n = mk(b)
    claim(b, n)
    c = login(b, "jose")
    page = c.get(f"/s/s1/t/{n}/replay").text
    assert 'class="card replay" data-replay' in page and 'class="rp-svg"' in page and 'class="rp-list"' in page
    assert "data-rp-controls hidden" in page                    # controls appear only with the script
    assert '<script src="/static/replay.js"></script>' in page and " style=" not in page
    assert f'href="/s/s1/t/{n}/replay"' in c.get(f"/s/s1/t/{n}").text
    assert c.get("/s/s1/t/999/replay").status_code == 404
    js = c.get("/static/replay.js").text
    assert "innerHTML" not in js
