"""Pause/archive enforcement ("allowed-ops matrix per session state"), POST /sessions/{s}/state,
archive journaled and survivable unarchive, CLI and dashboard controls."""
import json
import re

import pytest

from glassboard import ids, sessions


def api(gb, method, path, who, body=None, key=True):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body if body is not None else {}, rec=gb.rec(), key=key)


def prepare(gb):
    """s1 with jose (coordinator), ana (member), jose/claude (member, attached, holding T1 and T3),
    T2 open, T4 in review, and a question from jose to jose/claude."""
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    for n in range(4):
        body = {"title": "t", "scope": "s", "deliverable": "d"}
        if n == 1:
            body["assignee"] = "ana"
        assert api(gb, "POST", "/tasks", "jose", body).status_code == 201
    f = {n: api(gb, "POST", f"/tasks/{n}/claim", "jose/claude").json()["fence"] for n in (1, 3, 4)}
    api(gb, "POST", "/tasks/4/submit", "jose/claude", {"fence": f[4], "summary": "done"})
    q = api(gb, "POST", "/messages", "jose", {"kind": "question", "body": "q?", "to": ["jose/claude"]}).json()["seq"]
    consumer = api(gb, "POST", "/sync", "jose/claude").json()["consumer"]
    return {"fence": f, "q": q, "consumer": consumer}


def set_state(gb, state, who="jose", reason="testing"):
    return api(gb, "POST", "/state", who, {"state": state, "reason": reason})


def go(gb, st, state):
    """Put s1 into `state` through the API."""
    if state == "paused":
        assert set_state(gb, "paused").status_code == 200
    elif state == "archived":
        assert set_state(gb, "archived").status_code == 200


def run(gb, ctx, op):
    """One representative request per matrix row."""
    f, q = ctx["fence"], ctx["q"]
    return {
        "message": lambda: api(gb, "POST", "/messages", "ana", {"kind": "note", "body": "hi"}),
        "resolve": lambda: api(gb, "POST", f"/questions/{q}/resolve", "jose/claude", {"resolution": "declined"}),
        "sync.attach": lambda: api(gb, "POST", "/sync", "ana"),
        "sync.ack": lambda: api(gb, "POST", "/sync", "jose/claude", {"consumer": ctx["consumer"], "ack_through": 1}, key=None),
        "checkpoint": lambda: api(gb, "POST", "/checkpoints", "jose/claude", {"summary": "s", "next_action": "n"}),
        "task.create": lambda: api(gb, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"}),
        "task.acknowledge": lambda: api(gb, "POST", "/tasks/2/acknowledge", "ana"),
        "task.claim": lambda: api(gb, "POST", "/tasks/2/claim", "ana"),
        "task.progress": lambda: api(gb, "POST", "/tasks/1/note", "jose/claude", {"fence": f[1], "note": "n"}),
        "task.reclaim": lambda: api(gb, "POST", "/tasks/1/reclaim", "jose/claude", {"reason": "r"}),
        "task.finish": lambda: api(gb, "POST", "/tasks/3/release", "jose/claude", {"fence": f[3]}),
        "task.coord": lambda: api(gb, "POST", "/tasks/4/accept", "jose",
                                  {"if_version": gb.raw.execute("SELECT version FROM task WHERE num=4").fetchone()[0]}),
        "member.add": lambda: api(gb, "POST", "/members", "jose", {"handle": "ana/codex"}),
        "member.reduce": lambda: gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec()),
    }[op]()


def outcome(r):
    return "ok" if r.status_code < 300 else r.json()["error"]["code"]


@pytest.mark.parametrize("state", sessions.STATES)
def test_allowed_ops_matrix_per_session_state(gb, state):
    """Every row of sessions.ALLOWED in every state, each against a fresh session."""
    assert set(sessions.ALLOWED) == {"message", "resolve", "sync.attach", "sync.ack", "checkpoint", "task.create",
                                     "task.claim", "task.acknowledge", "task.progress", "task.reclaim", "task.finish", "task.coord",
                                     "member.add", "member.reduce"}
    refused = {"paused": "session_paused", "archived": "session_archived"}
    wrong = []
    for op in sessions.ALLOWED:
        from conftest import Board
        b = Board(gb.tmp / f"{state}-{op.replace('.', '_')}", gb.capsys)
        ctx = prepare(b)
        go(b, ctx, state)
        head = b.head()
        got = outcome(run(b, ctx, op))
        want = "ok" if state in sessions.ALLOWED[op] else refused[state]
        if got != want:
            wrong.append(f"{state:8} {op:14} want {want} got {got}")
        if got != "ok":
            assert b.head() == head, (state, op)
    assert not wrong, "\n" + "\n".join(wrong)


def test_d6_pause_mid_task_as_the_spec_fixes_it(gb):
    """Independent of the table: the documented pause-mid-task scenario (drill D6), verbatim."""
    ctx = prepare(gb)
    go(gb, ctx, "paused")
    for op in ("task.claim", "task.progress", "task.reclaim"):
        assert outcome(run(gb, ctx, op)) == "session_paused", op
    for op in ("checkpoint", "task.finish", "message", "sync.ack", "sync.attach"):
        assert outcome(run(gb, ctx, op)) == "ok", op


def test_e5b_archive_blocks_participant_mutations_only(gb):
    """Independent of the table: archived refuses participant writes, still reads and acks."""
    ctx = prepare(gb)
    go(gb, ctx, "archived")
    for op in ("message", "checkpoint", "task.claim", "task.progress", "task.create", "task.coord", "member.add"):
        assert outcome(run(gb, ctx, op)) == "session_archived", op
    assert outcome(run(gb, ctx, "sync.ack")) == "ok"
    assert gb.req("GET", "/api/v1/sessions/s1/events", "ana", key=None).status_code == 200
    assert gb.req("GET", "/api/v1/sessions/s1/export", "ana", key=None).status_code == 200


# --- transitions --------------------------------------------------------------------------------------

def test_transitions_events_and_authority(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude", role="coordinator")
    assert set_state(gb, "paused", who="ana").status_code == 403
    assert set_state(gb, "paused", who="jose/claude").status_code == 403               # agents: human-only
    assert set_state(gb, "active").json()["error"]["code"] == "invalid_transition"      # already active
    for state, etype in (("paused", "session.paused"), ("active", "session.resumed"), ("archived", "session.archived"),
                         ("active", "session.resumed")):
        r = set_state(gb, state, reason=f"to {state}")
        assert r.status_code == 200 and r.json()["state"] == state
        ev = gb.events()[-1]
        assert ev["type"] == etype and ev["body"] == f"to {state}" and ev["broadcast"] == 1
    assert set_state(gb, "paused").status_code == 200
    assert set_state(gb, "archived").status_code == 200
    assert set_state(gb, "paused").json()["error"]["code"] == "invalid_transition"      # archived → paused: no
    assert api(gb, "POST", "/state", "jose", {"state": "paused"}).status_code == 422     # reason required


def test_archive_is_journaled_and_unarchive_survives_a_restart(gb):
    gb.session("s1")
    set_state(gb, "archived", reason="done")
    e = json.loads(open(gb.app.state.journal_path).read().splitlines()[-1])
    assert e["action"] == "session.archive" and e["session_id"] == gb.raw.execute("SELECT id FROM session").fetchone()[0]
    assert set_state(gb, "active", reason="one more thing").status_code == 200          # unarchive: a grant
    gb.restart()
    gb.restart()
    assert gb.raw.execute("SELECT state FROM session").fetchone()[0] == "active"        # not re-archived
    pause = set_state(gb, "paused")
    assert pause.status_code == 200 and len(open(gb.app.state.journal_path).read().splitlines()) == 1   # pause: not journaled


def test_state_change_replays(gb):
    gb.session("s1")
    k = ids.new_ulid()
    a = api(gb, "POST", "/state", "jose", {"state": "paused", "reason": "lunch"}, key=k)
    b = api(gb, "POST", "/state", "jose", {"state": "paused", "reason": "lunch"}, key=k)
    assert b.content == a.content and b.headers["glassboard-replayed"] == "true"


def test_board_events_still_happen_in_archived_sessions(gb):
    """Archive blocks participant mutations only; a journaled reduction replayed after a crash
    still writes its board event."""
    from glassboard import journal
    gb.session("s1")
    gb.add("s1", "ana")
    set_state(gb, "archived")
    sid = gb.raw.execute("SELECT id FROM session").fetchone()[0]
    gb.app.state.db.close()
    d = __import__("glassboard.db", fromlist=["Database"]).Database(gb.db)
    with d.write():
        journal.append(gb.app.state.journal_path, "member.remove", session_id=sid, principal_id=gb.pid("ana"))
    d.close()
    gb.restart()
    ev = gb.events()[-1]
    assert ev["type"] == "member.removed" and ev["actor_id"] == ids.BOARD_PRINCIPAL_ID


# --- CLI and dashboard ---------------------------------------------------------------------------------

def test_cli_session_pause_resume_archive(gb, live_server):
    gb.session("s1")
    L = live_server
    assert L.cli("jose", "use", "s1").returncode == 0
    out = L.cli("jose", "session", "pause", "lunch break")
    assert out.returncode == 0 and "active → paused" in out.stdout
    r = L.cli("jose", "post", "decision", "go")                    # human posts are still fine while paused
    assert r.returncode == 0
    assert "paused → active" in L.cli("jose", "session", "resume", "back").stdout
    assert "active → archived" in L.cli("jose", "session", "archive", "finished").stdout
    r = L.cli("jose", "post", "note", "late")
    assert r.returncode == 5 and "session_archived" in r.stderr
    assert "archived → active" in L.cli("jose", "session", "resume", "reopening").stdout


def test_dashboard_pause_and_resume(gb):
    from test_dashboard import csrf_of, login
    gb.session("s1")
    gb.add("s1", "ana")
    c = login(gb)
    html = c.get("/s/s1").text
    assert 'value="paused">Pause session</button>' in html
    form = {"csrf": csrf_of(html), "recovery": gb.rec(), "op": ids.new_ulid(), "state": "paused", "reason": "meeting"}
    assert c.post("/s/s1/state", data=form).status_code == 303
    assert gb.raw.execute("SELECT state FROM session").fetchone()[0] == "paused"
    html = c.get("/s/s1").text
    assert ">Resume session</button>" in html
    assert c.post("/s/s1/state", data={**form, "csrf": "bad"}).status_code == 403
    ana = login(gb, "ana")
    assert "Pause session</button>" not in ana.get("/s/s1").text and "Resume session</button>" not in ana.get("/s/s1").text
    r = ana.post("/s/s1/state", data={"csrf": csrf_of(ana.get("/s/s1").text), "recovery": gb.rec(), "op": ids.new_ulid(),
                                      "state": "active", "reason": "x"})
    assert r.status_code == 303 and r.headers["location"].endswith("err=forbidden")
