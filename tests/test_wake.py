"""Waking idle agents (ERRATA E30, decision 0035): wake generations and the eligibility filter, the
non-consuming wait, inspection records, the dispatch fence and the visible transition events."""
import threading
import time

from fastapi.testclient import TestClient

from glassboard import render


def setup(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana/codex")
    return gb


def post(gb, who, kind="note", body="hi", to=None, re=None, task=None):
    j = {"kind": kind, "body": body, "to": to or []}
    if re is not None:
        j["re"] = re
    if task is not None:
        j["task"] = task
    r = gb.req("POST", "/api/v1/sessions/s1/messages", who, json=j, rec=gb.rec())
    assert r.status_code == 201, r.text
    return r.json()["seq"]


def status(gb, who="jose/claude", **query):
    q = "&".join(f"{k}={v}" for k, v in query.items())
    r = gb.req("GET", f"/api/v1/sessions/s1/wake{'?' + q if q else ''}", who, key=None)
    assert r.status_code == 200, r.text
    return r.json()


def arm(gb, who="jose/claude", adapter="claude", takeover=None, key=True):
    body = {"adapter": adapter}
    if takeover:
        body["takeover"] = {"reason": takeover}
    return gb.req("POST", "/api/v1/sessions/s1/wake/arm", who, json=body, rec=gb.rec(), key=key,
                  extra={"X-Glassboard-Runtime": "wake-one"})


def op(gb, name, body, who="jose/claude"):
    return gb.req("POST", f"/api/v1/sessions/s1/wake/{name}", who, json=body, rec=gb.rec())


def inspected(gb, generation, who="jose/claude"):
    return op(gb, "inspected", {"generation": generation}, who)


# --- eligibility -------------------------------------------------------------------------------

def test_human_messages_wake_direct_and_broadcast(gb):
    setup(gb)
    a = post(gb, "jose", body="please look", to=["jose/claude"])
    b = post(gb, "jose", kind="update", body="for everyone")
    s = status(gb)
    assert s["pending"] and s["generation"] == b > a
    assert s["counts"] == {"message": 2}


def test_agent_broadcasts_and_agent_direct_notes_never_wake(gb):
    setup(gb)
    post(gb, "ana/codex", kind="update", body="progress for all")
    post(gb, "ana/codex", body="fyi", to=["jose/claude"])
    s = status(gb)
    assert not s["pending"] and s["counts"] == {}


def test_questions_answers_and_tasks_routed_to_me_wake(gb):
    setup(gb)
    q = post(gb, "ana/codex", kind="question", body="which file?", to=["jose/claude"])
    mine = post(gb, "jose/claude", kind="question", body="headings only?", to=["ana/codex"])
    post(gb, "ana/codex", kind="answer", body="headings only", to=["jose/claude"], re=mine)
    r = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose", rec=gb.rec(),
               json={"title": "t", "scope": "s", "deliverable": "d", "assignee": "jose/claude"})
    assert r.status_code == 201, r.text
    s = status(gb)
    assert s["counts"] == {"question": 1, "answer": 1, "task": 1}
    assert s["generation"] == gb.head() and q < s["generation"]


def test_my_own_events_and_wake_events_never_wake_me(gb):
    setup(gb)
    post(gb, "jose/claude", kind="update", body="mine")
    assert arm(gb).status_code == 200
    assert not status(gb)["pending"]


def test_a_member_sees_only_broadcasts_after_joining(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    post_seq = gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "note", "body": "early"},
                      rec=gb.rec()).json()["seq"]
    gb.add("s1", "jose/claude")
    s = status(gb)
    assert not s["pending"] and s["generation"] < gb.head() and post_seq < gb.head()


# --- the non-consuming wait -----------------------------------------------------------------

def test_status_never_attaches_or_changes_the_inbox_consumer(gb):
    setup(gb)
    post(gb, "jose", body="hi", to=["jose/claude"])
    before = gb.events()
    status(gb)
    status(gb, wait=1, after=0)
    row = gb.raw.execute("SELECT consumer_secret, consumer_gen, cursor_seq, delivered_seq FROM member m "
                         "JOIN principal p ON p.id=m.principal_id WHERE p.handle='jose/claude'").fetchone()
    assert row["consumer_secret"] is None and row["consumer_gen"] == 0
    assert row["cursor_seq"] == row["delivered_seq"]
    assert gb.events() == before                                   # no event, no ack


def test_wait_returns_within_a_second_of_an_eligible_commit(gb):
    setup(gb)
    out = {}
    rec_headers = gb.headers("jose/claude", key=None)

    def run():
        c = TestClient(gb.app, raise_server_exceptions=False)
        t0 = time.monotonic()
        r = c.get("/api/v1/sessions/s1/wake?wait=10", headers=rec_headers)
        out.update(r=r, end=time.monotonic(), t0=t0)
    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 10
    while gb.app.state.waiters.count < 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.05)
    sent = time.monotonic()
    post(gb, "jose", body="wake up", to=["jose/claude"])
    t.join(15)
    assert out["r"].status_code == 200 and out["r"].json()["pending"]
    assert out["end"] - sent < 1.0


def test_wait_times_out_without_demand(gb):
    setup(gb)
    t0 = time.monotonic()
    s = status(gb, wait=1)
    assert not s["pending"] and time.monotonic() - t0 >= 0.9


def test_wait_counts_toward_the_two_wait_limit(gb):
    setup(gb)
    headers = gb.headers("jose/claude", key=None)
    threads = [threading.Thread(target=lambda: TestClient(gb.app).get("/api/v1/sessions/s1/wake?wait=3", headers=headers))
               for _ in range(2)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while gb.app.state.waiters.count < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    r = gb.req("GET", "/api/v1/sessions/s1/wake?wait=3", "jose/claude", key=None)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    for t in threads:
        t.join(10)


# --- inspection --------------------------------------------------------------------------------

def test_inspection_clears_only_through_the_inspected_generation(gb):
    setup(gb)
    first = post(gb, "jose", body="one", to=["jose/claude"])
    assert inspected(gb, first).status_code == 200
    s = status(gb)
    assert not s["pending"] and s["inspected"] == first
    second = post(gb, "jose", body="two", to=["jose/claude"])
    s = status(gb)
    assert s["pending"] and s["generation"] == second and s["counts"] == {"message": 1}
    ev = gb.events()[-2]
    assert ev["type"] == "wake.inspected" and ev["data"] == {"generation": first}


def test_inspecting_an_old_generation_is_a_noop_without_an_event(gb):
    setup(gb)
    g = post(gb, "jose", body="one", to=["jose/claude"])
    assert inspected(gb, g).status_code == 200
    head = gb.head()
    r = inspected(gb, g)
    assert r.status_code == 200 and gb.head() == head


def test_inspecting_a_future_generation_is_refused(gb):
    setup(gb)
    r = inspected(gb, gb.head() + 5)
    assert r.status_code == 409 and r.json()["error"]["code"] == "generation_ahead"


def test_humans_are_not_woken(gb):
    setup(gb)
    assert arm(gb, who="jose").status_code == 403
    assert inspected(gb, 0, who="jose").status_code == 403


# --- the dispatch fence --------------------------------------------------------------------

def test_arm_returns_the_fence_once_and_replays_it(gb):
    setup(gb)
    key = "01JAAAAAAAAAAAAAAAAAAAAAAA"
    r = arm(gb, key=key)
    assert r.status_code == 200, r.text
    fence = r.json()["fence"]
    assert fence.startswith("W1-")
    again = arm(gb, key=key)
    assert again.status_code == 200 and again.json()["fence"] == fence
    assert fence not in gb.req("GET", "/api/v1/sessions/s1/events", "jose", key=None).text
    assert fence not in str(status(gb))
    ev = [e for e in gb.events() if e["type"] == "wake.armed"]
    assert len(ev) == 1 and ev[0]["data"] == {"adapter": "claude", "wake_gen": 1}


def test_a_second_supervisor_conflicts_and_takeover_is_visible(gb):
    setup(gb)
    old = arm(gb).json()["fence"]
    r = arm(gb)
    assert r.status_code == 409 and r.json()["error"]["code"] == "wake_conflict"
    t = arm(gb, takeover="previous supervisor crashed")
    assert t.status_code == 200
    ev = gb.events()[-1]
    assert ev["type"] == "wake.takeover" and ev["data"]["reason"] == "previous supervisor crashed"
    assert gb.recipients("s1", ev["seq"]) == ["jose"]               # the owner (also the coordinator)
    r = op(gb, "dispatch", {"fence": old, "generation": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "wake_mismatch"


def test_dispatch_needs_pending_demand_and_allows_one_outstanding(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    r = op(gb, "dispatch", {"fence": fence, "generation": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_pending"
    g = post(gb, "jose", body="go", to=["jose/claude"])
    assert op(gb, "dispatch", {"fence": fence, "generation": g}).status_code == 200
    ev = gb.events()[-1]
    assert ev["type"] == "wake.dispatched" and ev["data"] == {"generation": g, "adapter": "claude"}
    g2 = post(gb, "jose", body="more", to=["jose/claude"])
    r = op(gb, "dispatch", {"fence": fence, "generation": g2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dispatch_outstanding"
    assert inspected(gb, g).status_code == 200                       # closes the outstanding dispatch
    assert status(gb)["supervisor"]["outstanding"] is None
    assert op(gb, "dispatch", {"fence": fence, "generation": g2}).status_code == 200


def test_a_paused_session_wakes_nobody(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    g = post(gb, "jose", body="go", to=["jose/claude"])
    r = gb.req("POST", "/api/v1/sessions/s1/state", "jose", json={"state": "paused", "reason": "lunch"}, rec=gb.rec())
    assert r.status_code == 200, r.text
    r = op(gb, "dispatch", {"fence": fence, "generation": g})
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_paused"


def test_pause_resume_stop_and_disarm_are_visible_transitions(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    g = post(gb, "jose", body="go", to=["jose/claude"])
    assert op(gb, "pause", {"fence": fence, "reason": "permission prompt"}).status_code == 200
    paused = gb.events()[-1]
    assert paused["type"] == "wake.paused" and paused["data"]["reason"] == "permission prompt"
    assert gb.recipients("s1", paused["seq"]) == ["jose"]
    r = op(gb, "dispatch", {"fence": fence, "generation": g})
    assert r.status_code == 409 and r.json()["error"]["code"] == "wake_paused"
    assert op(gb, "resume", {"fence": fence, "reason": "approved"}).status_code == 200
    assert gb.events()[-1]["type"] == "wake.resumed"
    assert op(gb, "stop", {"fence": fence, "reason": "hourly budget used"}).status_code == 200
    stopped = gb.events()[-1]
    assert stopped["type"] == "wake.stopped" and gb.recipients("s1", stopped["seq"]) == ["jose"]
    r = op(gb, "dispatch", {"fence": fence, "generation": g})
    assert r.status_code == 409 and r.json()["error"]["code"] == "wake_mismatch"   # stopped is terminal
    fence2 = arm(gb).json()["fence"]
    assert op(gb, "disarm", {"fence": fence2}).status_code == 200
    assert gb.events()[-1]["type"] == "wake.disarmed" and status(gb)["supervisor"]["state"] == "none"


def test_removing_a_member_releases_the_wake_fence(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    r = gb.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=gb.rec())
    assert r.status_code == 200, r.text
    gb.add("s1", "jose/claude")
    assert status(gb)["supervisor"]["state"] == "none"
    r = op(gb, "dispatch", {"fence": fence, "generation": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "wake_mismatch"


def test_wake_events_render_in_the_canonical_grammar(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    g = post(gb, "jose", body="go", to=["jose/claude"])
    op(gb, "dispatch", {"fence": fence, "generation": g})
    inspected(gb, g)
    op(gb, "stop", {"fence": fence, "reason": "done for today"})
    r = gb.req("GET", "/api/v1/sessions/s1/events?format=text&limit=100", "jose", key=None)
    assert r.status_code == 200
    assert render.text_ok(r.text)
    for t in ("[wake.armed]", "[wake.dispatched]", "[wake.inspected]", "[wake.stopped]"):
        assert t in r.text
    assert "  · generation: «" in r.text and fence not in r.text


def supervisor_reads(gb, who="jose/claude"):
    r = gb.req("GET", "/api/v1/sessions/s1/wake", who, key=None, extra={"X-Glassboard-Runtime": "wake-one"})
    assert r.status_code == 200, r.text
    return r.json()


def test_team_panel_shows_the_supervisor_state_and_waiting_work(gb):
    from test_dashboard import login
    setup(gb)
    fence = arm(gb).json()["fence"]
    c = login(gb)
    html = c.get("/s/s1").text
    assert "wake supervisor armed (claude) · <strong>supervisor not connected</strong>" in html   # never polled
    assert "work waiting" not in html
    supervisor_reads(gb)
    post(gb, "jose", body="please look", to=["jose/claude"])
    assert "wake supervisor armed (claude) · <strong>work waiting</strong>" in c.get("/s/s1").text
    op(gb, "hold", {"fence": fence, "reason": "open in Claude Code (process 42)"})
    assert "<strong>wake held</strong>: «open in Claude Code (process 42)»" in c.get("/s/s1").text
    op(gb, "dispatch", {"fence": fence, "generation": status(gb)["generation"]})
    html = c.get("/s/s1").text
    assert "wake supervisor armed (claude) · running the client since " in html and "wake held" not in html
    op(gb, "pause", {"fence": fence, "reason": "permission prompt"})
    assert "wake supervisor paused (claude)" in c.get("/s/s1").text
    assert fence not in c.get("/s/s1").text


# --- holds and presence (ERRATA E31) ------------------------------------------------------------

def test_presence_comes_only_from_the_supervisors_own_reads(gb):
    setup(gb)
    arm(gb)
    assert status(gb)["supervisor"]["connected"] is False            # the agent's own read is not the supervisor
    assert supervisor_reads(gb)["supervisor"]["connected"] is True
    assert status(gb)["supervisor"]["connected"] is True             # within the grace period
    assert status(gb, "ana/codex")["supervisor"]["connected"] is None  # nothing armed for ana/codex


def test_a_hold_is_one_routed_event_per_reason_and_reaches_the_owners_attention(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    assert op(gb, "hold", {"fence": fence, "reason": "open"}).json()["error"]["code"] == "nothing_pending"
    gen = post(gb, "ana", body="please look", to=["jose/claude"])
    r = op(gb, "hold", {"fence": fence, "reason": "open in Codex (process 7)"})
    assert r.status_code == 200, r.text
    held = r.json()["seq"]
    assert op(gb, "hold", {"fence": fence, "reason": "open in Codex (process 7)"}).status_code == 200
    events = [e for e in gb.events() if e["type"] == "wake.held"]
    assert len(events) == 1 and events[0]["data"] == {"adapter": "claude", "generation": gen,
                                                      "reason": "open in Codex (process 7)"}
    assert gb.recipients("s1", held) == ["jose"]                    # the agent's owner (no coordinator but jose)
    s = status(gb)["supervisor"]
    assert s["hold"] == {"reason": "open in Codex (process 7)", "seq": held}
    for who in ("jose", "ana"):
        a = gb.req("GET", "/api/v1/me/attention", who, key=None).json()["items"]
        mine = [i for i in a if i["kind"] == "wake_held"]
        assert mine == ([{"kind": "wake_held", "session": "s1", "seq": held, "from": "jose/claude",
                          "text": "open in Codex (process 7)", "age_s": mine[0]["age_s"]}] if who == "jose" else [])
    text = gb.req("GET", "/api/v1/me/attention?format=text", "jose", key=None).text
    assert f"--- wake held · s1 #{held} from jose/claude ·" in text and "  · text: «open in Codex (process 7)»" in text
    assert op(gb, "hold", {"fence": "W9-aaaaaaaaaa", "reason": "x"}).json()["error"]["code"] == "wake_mismatch"


def test_a_hold_ends_with_a_dispatch_an_inspection_that_clears_demand_or_a_control(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    first = post(gb, "ana", body="one", to=["jose/claude"])
    op(gb, "hold", {"fence": fence, "reason": "open"})
    second = post(gb, "ana", body="two", to=["jose/claude"])
    assert inspected(gb, first).status_code == 200                   # demand remains: still held
    assert status(gb)["supervisor"]["hold"] is not None
    assert inspected(gb, second).status_code == 200                  # the open session handled it all
    assert status(gb)["supervisor"]["hold"] is None
    post(gb, "ana", body="three", to=["jose/claude"])
    op(gb, "hold", {"fence": fence, "reason": "open"})
    op(gb, "dispatch", {"fence": fence, "generation": status(gb)["generation"]})
    s = status(gb)["supervisor"]
    assert s["hold"] is None and s["dispatched_at"] is not None
    inspected(gb, status(gb)["generation"])
    assert status(gb)["supervisor"]["dispatched_at"] is None
    post(gb, "ana", body="four", to=["jose/claude"])
    op(gb, "hold", {"fence": fence, "reason": "open"})
    op(gb, "pause", {"fence": fence, "reason": "human looks"})
    assert status(gb)["supervisor"]["hold"] is None


def test_a_submission_reaches_and_wakes_the_named_reviewer(gb):
    setup(gb)
    r = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose", rec=gb.rec(),
               json={"title": "t", "scope": "s", "deliverable": "d", "assignee": "jose/claude", "reviewer": "ana/codex"})
    assert r.status_code == 201, r.text
    n = r.json()["task"]
    c = gb.req("POST", f"/api/v1/sessions/s1/tasks/{n}/claim", "jose/claude", json={}, rec=gb.rec())
    assert c.status_code == 201, c.text
    s = gb.req("POST", f"/api/v1/sessions/s1/tasks/{n}/submit", "jose/claude", rec=gb.rec(),
               json={"fence": c.json()["fence"], "summary": "done"})
    assert s.status_code == 200, s.text
    assert "ana/codex" in gb.recipients("s1", s.json()["seq"])
    w = status(gb, "ana/codex")
    assert w["pending"] and w["counts"].get("task") and w["generation"] == s.json()["seq"]


def test_a_broadcast_answer_to_my_question_wakes_me(gb):
    setup(gb)
    q = post(gb, "jose/claude", kind="question", body="which file?", to=["ana/codex"])
    post(gb, "ana/codex", kind="update", body="progress for all")                  # agent broadcast: no wake
    assert not status(gb)["pending"]
    a = post(gb, "ana/codex", kind="answer", body="the README", re=q)               # broadcast answer to my question
    s = status(gb)
    assert s["pending"] and s["counts"] == {"answer": 1} and s["generation"] == a
