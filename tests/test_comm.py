"""The communication pack: the digest (what changed since your last ack), the decision log,
structured stand-up updates, and the dashboard's count-only attention endpoint for notifications."""
import json
import re

from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_render import assert_canonical
from test_tasks import api, claim


def get(gb, path, who="jose", fmt=False):
    r = gb.req("GET", f"/api/v1/sessions/s1{path}" + ("&format=text" if fmt and "?" in path else "?format=text" if fmt else ""),
               who, key=None)
    assert r.status_code == 200, r.text
    return r.text if fmt else r.json()


def sync(gb, who, body=None):
    return gb.client.post("/api/v1/sessions/s1/sync", json=body or {}, headers=gb.headers(who, rec=gb.rec())).json()


# --- digest -------------------------------------------------------------------------------------------

def test_digest_summarises_since_the_last_ack(b):
    first = sync(b, "jose/claude")
    sync(b, "jose/claude", {"consumer": first["consumer"], "ack_through": first["delivered_through"]})
    cursor = b.raw.execute("SELECT cursor_seq FROM member WHERE principal_id=?", (b.pid("jose/claude"),)).fetchone()[0]
    n = mk(b, title="Write the FAQ")
    fence = claim(b, n).json()["fence"]
    for i in range(2):
        api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": f"step {i}"})
    q = api(b, "POST", "/messages", "ana", {"kind": "question", "body": "Which tone?", "to": ["jose/claude"]}).json()["seq"]
    api(b, "POST", "/messages", "jose/claude", {"kind": "answer", "body": "Plain.", "re": q})
    api(b, "POST", "/messages", "jose", {"kind": "decision", "body": "Ship with 0.1."})
    d = get(b, "/digest", "jose/claude")
    assert d["after"] == cursor and d["events"] > 0
    assert d["tasks"] == [{"task": n, "steps": [["created", 1], ["claimed", 1], ["note", 2]], "title": "Write the FAQ",
                           "status": "in_progress"}]
    assert d["asked"][0]["seq"] == q and q in d["answered"] and d["decisions"][0]["text"] == "Ship with 0.1."
    text = get(b, "/digest", "jose/claude", fmt=True)
    assert_canonical(text)
    assert f"--- T{n} · now in_progress · created, claimed, note ×2 ---" in text
    assert f"--- asked #{q} by ana · answered ---" in text and "  · text: «Ship with 0.1.»" in text
    # it never moves the cursor
    assert b.raw.execute("SELECT cursor_seq FROM member WHERE principal_id=?", (b.pid("jose/claude"),)).fetchone()[0] == cursor
    assert get(b, f"/digest?after={b.head()}", "jose/claude")["events"] == 0
    assert "--- nothing new ---" in get(b, f"/digest?after={b.head()}", "jose/claude", fmt=True)


def test_digest_is_members_only(b):
    b.session("s2", who="ana")
    assert b.req("GET", "/api/v1/sessions/s2/digest", "jose/claude", key=None).status_code == 404


# --- decision log ---------------------------------------------------------------------------------------

def test_decision_log_lists_decisions_with_reply_counts(b):
    d1 = api(b, "POST", "/messages", "jose", {"kind": "decision", "body": "Markdown only."}).json()["seq"]
    api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "noted", "re": d1})
    api(b, "POST", "/messages", "jose", {"kind": "note", "body": "not a decision"})
    j = get(b, "/decisions", "jose/claude")
    assert [x["seq"] for x in j["decisions"]] == [d1] and j["decisions"][0]["replies"] == 1
    text = get(b, "/decisions", "jose/claude", fmt=True)
    assert_canonical(text)
    assert text.startswith("--- decisions · s1 · 1 ---") and f"--- 1 reply: history --thread {d1} ---" in text


# --- stand-up updates ---------------------------------------------------------------------------------

def test_standup_updates(b):
    api(b, "POST", "/messages", "jose/claude", {"kind": "update", "body": "Day 1",
                                               "standup": {"done": "outline", "doing": "Q1-Q3"}})
    api(b, "POST", "/messages", "ana/codex", {"kind": "update", "body": "Day 1",
                                             "standup": {"blocked": "waiting on the style guide"}})
    s2 = api(b, "POST", "/messages", "jose/claude", {"kind": "update", "body": "Day 2",
                                                    "standup": {"done": "Q1-Q3", "doing": "Q4"}}).json()["seq"]
    su = get(b, "/standup")["standups"]
    assert [(x["member"], x.get("done")) for x in su] == [("ana/codex", None), ("jose/claude", "Q1-Q3")]
    assert su[1]["seq"] == s2
    text = get(b, "/standup", fmt=True)
    assert_canonical(text)
    assert "  · blocked: «waiting on the style guide»" in text and "  · doing: «Q4»" in text
    hist = b.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None).text
    assert "  · done: «Q1-Q3»" in hist
    for bad in ({"kind": "note", "body": "x", "standup": {"done": "y"}}, {"kind": "update", "body": "x", "standup": {}}):
        assert api(b, "POST", "/messages", "jose/claude", bad).status_code == 422


# --- CLI ------------------------------------------------------------------------------------------------

def test_cli_digest_decisions_standup(b, live_server):
    for who in ("jose", "jose/claude"):
        assert live_server.cli(who, "use", "s1").returncode == 0
    r = live_server.cli("jose/claude", "post", "update", "--done", "outline", "--doing", "Q1")
    assert r.returncode == 0, r.stderr
    assert live_server.cli("jose", "post", "decision", "Markdown only.").returncode == 0
    su = live_server.cli("jose", "standup")
    assert su.returncode == 0 and "  · done: «outline»" in su.stdout
    de = live_server.cli("jose/claude", "decisions")
    assert de.returncode == 0 and "  | Markdown only." in de.stdout
    dg = live_server.cli("jose/claude", "digest", "--after", "0")
    assert dg.returncode == 0 and "--- decision #" in dg.stdout
    for out in (su.stdout, de.stdout, dg.stdout):
        assert_canonical(out)
    assert live_server.cli("jose", "post", "note").returncode == 2                 # body still required for a note


# --- dashboard ------------------------------------------------------------------------------------------

def test_dashboard_standup_panel_and_attention_count(b):
    from test_dashboard import login
    api(b, "POST", "/messages", "jose/claude", {"kind": "update", "body": "x", "standup": {"doing": "the <b>FAQ</b> \u202e"}})
    api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which tone?", "to": ["jose"]})
    c = login(b, "jose")
    html = c.get("/s/s1").text
    assert "Stand-up" in html and "the &lt;b&gt;FAQ&lt;/b&gt;" in html and "\u202e" not in html
    r = c.get("/attention-count")
    assert r.status_code == 200 and r.json() == {"count": 1} and "content-security-policy" in r.headers
    assert '<script src="/static/notify.js"></script>' in c.get("/").text
    js = c.get("/static/notify.js").text
    assert "innerHTML" not in js and "/attention-count" in js
    from test_dashboard import browser
    assert browser(b).get("/attention-count").status_code in (303, 403)          # logged out: no number
