"""Long-poll wait (wakes within 1 s of a routed commit; the ack in a waiting request is applied once;
takeover during the wait; caps 2/principal and 50/instance; no lock or transaction held while waiting)."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from glassboard import db, longpoll


@pytest.fixture
def w(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    return gb


def attach(gb, who="jose/claude"):
    r = gb.req("POST", "/api/v1/sessions/s1/sync", who, json={}, rec=gb.rec())
    assert r.status_code == 200
    return r.json()


def drain(gb, tok, who="jose/claude"):
    j = gb.req("POST", "/api/v1/sessions/s1/sync", who, json={"consumer": tok}, rec=gb.rec(), key=None).json()
    return gb.req("POST", "/api/v1/sessions/s1/sync", who, json={"consumer": tok, "ack_through": j["delivered_through"]},
                  rec=gb.rec(), key=None).json()


def wait_in_thread(gb, body, who="jose/claude", out=None):
    rec = gb.rec()
    def run():
        c = TestClient(gb.app, raise_server_exceptions=False)
        t0 = time.monotonic()
        r = c.post("/api/v1/sessions/s1/sync", json=body, headers=gb.headers(who, rec=rec, key=None))
        end = time.monotonic()
        out.update(r=r, elapsed=end - t0, end=end)
    t = threading.Thread(target=run)
    t.start()
    return t


def until_waiting(gb, n, timeout=10.0):
    """Wait until `n` syncs are really waiting (registered), instead of guessing with a sleep."""
    deadline = time.monotonic() + timeout
    while gb.app.state.waiters.count < n:
        assert time.monotonic() < deadline, f"only {gb.app.state.waiters.count} waiting"
        time.sleep(0.01)
    time.sleep(0.05)                                         # past TX1, inside the wait


def post(gb, who, to, body="hi"):
    r = gb.req("POST", "/api/v1/sessions/s1/messages", who, json={"kind": "note", "body": body, "to": to}, rec=gb.rec())
    assert r.status_code == 201
    return r.json()["seq"]


def test_wakes_within_one_second_of_a_routed_commit(w):
    tok = attach(w)["consumer"]
    drain(w, tok)
    out = {}
    t = wait_in_thread(w, {"consumer": tok, "wait": 20}, out=out)
    until_waiting(w, 1)
    t0 = time.monotonic()
    seq = post(w, "jose", ["jose/claude"], "wake up")       # a write lands while the sync waits
    post_took = time.monotonic() - t0
    committed = t0 + post_took
    t.join(timeout=10)
    out["woke_after_commit_s"] = out["end"] - committed
    assert post_took < 2.0                                   # the waiting sync holds no lock
    assert out["r"].status_code == 200 and [e["seq"] for e in out["r"].json()["inbox"]] == [seq]
    assert out["woke_after_commit_s"] < 1.0                  # woke within 1 s of the commit
    assert out["r"].json()["delivered_through"] >= seq


def test_unrelated_events_do_not_wake(w):
    tok = attach(w)["consumer"]
    drain(w, tok)
    out = {}
    t = wait_in_thread(w, {"consumer": tok, "wait": 1}, out=out)
    until_waiting(w, 1)
    post(w, "jose", ["ana"], "not for claude")
    post(w, "jose/claude", [], "claude's own broadcast")     # own events never wake (not in the inbox)
    t.join(timeout=10)
    assert out["r"].json()["inbox"] == [] and out["elapsed"] >= 0.95


def test_broadcasts_wake_everyone_who_did_not_write_them(w):
    tok = attach(w)["consumer"]
    drain(w, tok)
    out = {}
    t = wait_in_thread(w, {"consumer": tok, "wait": 20}, out=out)
    until_waiting(w, 1)
    post(w, "ana", [], "to all")
    t.join(timeout=10)
    assert out["elapsed"] < 2 and out["r"].json()["inbox"][0]["body"] == "to all"


def test_at_1_9_the_ack_in_a_waiting_request_is_applied_once(w):
    tok = attach(w)["consumer"]
    s1 = post(w, "jose", ["jose/claude"], "first")
    got = w.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"consumer": tok}, rec=w.rec(), key=None).json()
    out = {}
    t = wait_in_thread(w, {"consumer": tok, "ack_through": got["delivered_through"], "wait": 20}, out=out)
    until_waiting(w, 1)
    s2 = post(w, "jose", ["jose/claude"], "second")
    t.join(timeout=10)
    j = out["r"].json()
    assert j["cursor"] == got["delivered_through"] and [e["seq"] for e in j["inbox"]] == [s2]
    assert j["delivered_through"] >= s2 > s1


def test_at_2_11_takeover_during_the_wait(w):
    tok = attach(w)["consumer"]
    drain(w, tok)
    before = w.raw.execute("SELECT delivered_seq FROM member WHERE principal_id=?", (w.pid("jose/claude"),)).fetchone()[0]
    out = {}
    t = wait_in_thread(w, {"consumer": tok, "wait": 20}, out=out)
    until_waiting(w, 1)
    t_takeover = time.monotonic()
    r = w.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"takeover": {"reason": "new runtime"}}, rec=w.rec())
    assert r.status_code == 200
    t.join(timeout=10)
    assert out["r"].status_code == 409 and out["r"].json()["error"]["code"] == "consumer_mismatch"
    assert out["end"] - t_takeover < 1.5                       # woken by its own consumer change
    after = w.raw.execute("SELECT delivered_seq FROM member WHERE principal_id=?", (w.pid("jose/claude"),)).fetchone()[0]
    assert after <= before or after == r.json()["delivered_through"]   # nothing delivered to the old consumer


def test_no_wait_when_the_inbox_is_not_empty(w):
    tok = attach(w)["consumer"]
    t0 = time.monotonic()
    r = w.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"consumer": tok, "wait": 20}, rec=w.rec(), key=None)
    assert time.monotonic() - t0 < 1 and r.json()["inbox"]


def test_caps_two_per_principal(w):
    tok = attach(w)["consumer"]
    drain(w, tok)
    outs = [{}, {}]
    ts = [wait_in_thread(w, {"consumer": tok, "wait": 3}, out=o) for o in outs]
    until_waiting(w, 2)
    r = w.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"consumer": tok, "wait": 3}, rec=w.rec(), key=None)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and r.headers["retry-after"] == "1"
    assert w.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"consumer": tok}, rec=w.rec(),
                 key=None).status_code == 200                 # a sync without waiting is never capped
    [t.join(timeout=10) for t in ts]
    assert w.app.state.waiters.count == 0                     # every waiter released


def test_caps_per_instance():
    async def scenario():
        ws = longpoll.Waiters(per_principal=10, per_instance=3)
        held = [ws.register("s", f"p{i}") for i in range(3)]
        with pytest.raises(Exception) as e:
            ws.register("s", "p9")
        assert getattr(e.value, "status", None) == 429
        ws.release(held[0])
        ws.register("s", "p9")
    import asyncio
    asyncio.run(scenario())


def test_rolled_back_writes_wake_nobody(tmp_path):
    d = db.Database(str(tmp_path / "x.db"))
    d.migrate()
    fired = []
    with pytest.raises(RuntimeError):
        with d.write():
            db.on_commit(lambda: fired.append("rolled back"))
            raise RuntimeError
    with d.write():
        db.on_commit(lambda: fired.append("committed"))
    assert fired == ["committed"]
    d.close()


def test_cli_sync_wait(w, live_server):
    L = live_server
    assert L.cli("jose/claude", "use", "s1").returncode == 0
    assert L.cli("jose/claude", "sync").returncode == 0
    assert L.cli("jose/claude", "sync", "--ack").returncode == 0
    t0 = time.monotonic()
    r = L.cli("jose/claude", "sync", "--wait", "1")
    assert r.returncode == 0 and "inbox: 0 new" in r.stdout and 0.9 <= time.monotonic() - t0 < 10
