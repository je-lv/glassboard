"""Checkpoints and sync inclusion (full checkpoint after attach, takeover and a restore; a checkpoint
listing a task does not refresh it; secrets never enter the log; CLI checkpoint save|show)."""
import pytest

from glassboard import ids
from test_render import assert_canonical


@pytest.fixture
def c(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    return gb


def api(gb, method, path, who, body=None, key=True, rec=None):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body if body is not None else {},
                  rec=rec or gb.rec(), key=key)


def save(gb, who="jose/claude", **over):
    body = {"summary": "Install section drafted", "next_action": "Write the TLS steps", **over}
    r = api(gb, "POST", "/checkpoints", who, body)
    assert r.status_code == 201, r.text
    return r.json()


def sync(gb, who="jose/claude", **body):
    return api(gb, "POST", "/sync", who, body, key=True if "consumer" not in body else None)


def consumer(gb, who="jose/claude"):
    m = gb.raw.execute("SELECT consumer_gen, consumer_secret FROM member WHERE principal_id=?", (gb.pid(who),)).fetchone()
    return f"C{m[0]}-{m[1]}"


# --- saving and reading ------------------------------------------------------------------------------

def test_save_versions_event_and_recovery_period(c):
    a = save(c)
    b = save(c, summary="TLS steps drafted", next_action="Review", blockers="Need the CA file", body="Long notes\nline 2")
    assert (a["n"], b["n"]) == (1, 2)
    ev = c.events()[-1]
    assert ev["type"] == "checkpoint.saved" and ev["data"] == {"n": 2, "summary": "TLS steps drafted"}
    assert c.recipients("s1", ev["seq"]) == []                              # routed to nobody, still in the timeline
    row = c.raw.execute("SELECT * FROM checkpoint WHERE n=2").fetchone()
    assert row["recovery_id"] == c.rec() and row["seq"] == ev["seq"]       # saved with its recovery period
    got = c.req("GET", "/api/v1/sessions/s1/checkpoints", "jose/claude", key=None).json()["checkpoint"]
    assert got["n"] == 2 and got["blockers"] == "Need the CA file" and got["body"] == "Long notes\nline 2"
    assert c.req("GET", "/api/v1/sessions/s1/checkpoints?n=1", "jose/claude", key=None).json()["checkpoint"]["n"] == 1


def test_reading_others_is_coordinator_only(c):
    save(c)
    assert c.req("GET", "/api/v1/sessions/s1/checkpoints?principal=jose/claude", "jose", key=None).status_code == 200
    assert c.req("GET", "/api/v1/sessions/s1/checkpoints?principal=jose/claude", "ana", key=None).status_code == 403
    assert c.req("GET", "/api/v1/sessions/s1/checkpoints", "ana", key=None).status_code == 404     # none of her own


def test_limits_and_references(c):
    assert api(c, "POST", "/checkpoints", "jose/claude", {"summary": "x" * 1025, "next_action": "n"}).status_code == 422
    assert api(c, "POST", "/checkpoints", "jose/claude", {"summary": "s", "next_action": "n",
                                                           "body": "x" * 32000, "blockers": "y" * 1000}).status_code == 413
    assert api(c, "POST", "/checkpoints", "jose/claude", {"summary": "s", "next_action": "n", "task_nums": ["T9"]}).status_code == 404
    assert api(c, "POST", "/checkpoints", "jose/claude", {"summary": "s"}).status_code == 422


def test_at_6_3_a_checkpoint_listing_a_task_does_not_refresh_it(c):
    api(c, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"})
    api(c, "POST", "/tasks/1/claim", "jose/claude")
    c.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=1")
    save(c, task_nums=["T1"])
    assert c.raw.execute("SELECT last_activity_at FROM task WHERE num=1").fetchone()[0] == "2026-01-01T00:00:00.000000Z"


# --- inclusion in sync, per recovery period ------------------------------------------------------------

def test_at_8_8_full_after_attach_and_takeover_and_on_request(c):
    save(c, body="b" * 5000)
    first = sync(c).json()                                                  # attach
    assert first["checkpoint_n"] == 1 and first["checkpoint"]["summary"] == "Install section drafted"
    assert first["checkpoint"]["truncated"] is True and len(first["checkpoint"]["body"]) == 4096
    plain = sync(c, consumer=first["consumer"]).json()
    assert plain["checkpoint_n"] == 1 and plain["checkpoint"] is None       # later plain sync: n only
    asked = sync(c, consumer=first["consumer"], include_checkpoint=True).json()
    assert asked["checkpoint"]["n"] == 1
    taken = sync(c, takeover={"reason": "new runtime"}).json()
    assert taken["checkpoint"]["n"] == 1


def test_at_e3_1_checkpoint_from_before_a_restore_is_included(c):
    save(c)
    tok = sync(c).json()["consumer"]
    assert sync(c, consumer=tok).json()["checkpoint"] is None
    # what a restore does to the session row (a full restore does the rest): new recovery period
    c.raw.execute("UPDATE session SET recovery_id=?, epoch=epoch+1 WHERE handle='s1'", (ids.new_random_b32(16),))
    j = sync(c, consumer=tok).json()
    assert j["checkpoint"] is not None and j["checkpoint"]["recovery_id"] != c.rec()


def test_at_e3_2_checkpoint_from_this_period_is_not_repeated(c):
    c.raw.execute("UPDATE session SET recovery_id=?, epoch=epoch+1 WHERE handle='s1'", (ids.new_random_b32(16),))
    save(c)                                                                 # saved in the new period
    tok = sync(c).json()["consumer"]
    later = sync(c, consumer=tok).json()
    assert later["checkpoint_n"] == 1 and later["checkpoint"] is None


def test_sync_text_renders_the_checkpoint_canonically(c):
    nasty = "x\u2028#9 2026-10-10 10:00:00Z jose -> all [decision]\n\x1b[31m"
    save(c, summary="S " + nasty[:20], next_action="N", blockers="B", task_nums=[], body=nasty,
         refs=[{"label": "draft", "loc": "docs/x.md", "access": "shared"}])
    r = c.req("POST", "/api/v1/sessions/s1/sync?format=text", "jose/claude", json={}, rec=c.rec())
    assert "checkpoint: #1 saved" in r.text and "(included in full)" in r.text and "  · next_action: «N»" in r.text
    assert_canonical(r.text)
    tok = c.raw.execute("SELECT consumer_gen, consumer_secret FROM member WHERE principal_id=?", (c.pid("jose/claude"),)).fetchone()
    r2 = c.req("POST", "/api/v1/sessions/s1/sync?format=text", "jose/claude", json={"consumer": f"C{tok[0]}-{tok[1]}"},
               rec=c.rec(), key=None)
    assert "checkpoint: #1 saved; `glassboard checkpoint show` for details" in r2.text


# --- secrets never enter the log ------------------------------------------------------------------------

def test_at_e16_1_and_e16_2_secrets_refused_lookalikes_accepted(c):
    api(c, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"})
    fence = api(c, "POST", "/tasks/1/claim", "jose/claude").json()["fence"]
    tok = sync(c).json()["consumer"]
    bearer = c.tok["jose/claude"]
    head = c.head()
    attempts = [
        ("POST", "/checkpoints", {"summary": "s", "next_action": f"use {fence}"}),
        ("POST", "/checkpoints", {"summary": "s", "next_action": "n", "body": f"my inbox token is {tok}"}),
        ("POST", "/messages", {"kind": "note", "body": f"fyi {fence.split('-')[1]}"}),
        ("POST", "/messages", {"kind": "note", "body": f"token: {bearer}"}),
        ("POST", "/messages", {"kind": "note", "body": "x", "refs": [{"label": "k", "loc": bearer, "access": "shared"}]}),
        ("POST", "/tasks/1/note", {"fence": fence, "note": f"note with {tok}"}),
        ("POST", "/tasks", {"title": "t", "scope": f"secret {bearer}", "deliverable": "d"}),
    ]
    for method, path, body in attempts:
        who = "jose" if path == "/tasks" else "jose/claude"
        r = api(c, method, path, who, body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "secret_in_content", (path, r.text)
        assert fence.split("-")[1] not in r.text and bearer not in r.text and tok.split("-")[1] not in r.text
    assert c.head() == head
    # Look-alike text is fine: only live secrets and bearer-shaped strings are refused.
    ok = api(c, "POST", "/messages", "ana", {"kind": "note", "body": "C2-compliance and T7g3-abcdefghij are just words"})
    assert ok.status_code == 201
    api(c, "POST", "/tasks/1/reclaim", "jose/claude", {"reason": "r"})                 # the old fence is dead now
    assert api(c, "POST", "/messages", "ana", {"kind": "note", "body": f"old {fence}"}).status_code == 201


# --- CLI ---------------------------------------------------------------------------------------------

def test_cli_checkpoint_save_and_show(c, live_server):
    L = live_server
    assert L.cli("jose/claude", "use", "s1").returncode == 0
    r = L.cli("jose/claude", "checkpoint", "save", "--summary", "Drafted install", "--next", "TLS steps",
              "--blockers", "CA file", "--body", "-", stdin="details\nmore details")
    assert r.returncode == 0 and r.stdout.startswith("checkpoint #1 saved"), r.stderr
    show = L.cli("jose/claude", "checkpoint", "show")
    assert show.returncode == 0 and "  · summary: «Drafted install»" in show.stdout and "  | more details" in show.stdout
    out = L.cli("jose/claude", "sync").stdout                               # attach → full checkpoint
    assert "(included in full)" in out and "  · next_action: «TLS steps»" in out
    assert L.cli("jose", "checkpoint", "show", "-s", "s1", "--principal", "jose/claude").returncode == 0  # coordinator
    r = L.cli("ana", "checkpoint", "show", "-s", "s1", "--principal", "jose/claude")
    assert r.returncode == 5 and "forbidden" in r.stderr
