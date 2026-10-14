"""Staleness sweeper and per-task override."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from glassboard import ids, sweeper
from glassboard.app import create_app


@pytest.fixture
def s(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    gb.add("s1", "jose/claude")
    return gb


def api(gb, method, path, who, body=None, key=True):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body or {}, rec=gb.rec(), key=key)


def new_task(gb, **extra):
    r = api(gb, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d", **extra})
    assert r.status_code == 201, r.text
    return r.json()["task"]


def activity(gb, n):
    return datetime.strptime(gb.raw.execute("SELECT last_activity_at FROM task WHERE num=?", (n,)).fetchone()[0],
                             "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def stale_events(gb):
    return [e for e in gb.events() if e["type"] == "task.stale"]


def test_at_8_9_one_stale_event_per_idle_period(s):
    n = new_task(s, stale_after_s=600)                       # session threshold stays 1800
    fence = api(s, "POST", f"/tasks/{n}/claim", "jose/claude").json()["fence"]
    t0 = activity(s, n)
    db_ = s.app.state.db
    assert sweeper.sweep(db_, t0 + timedelta(seconds=599)) == []          # not yet
    assert sweeper.sweep(db_, t0 + timedelta(seconds=601)) != []          # > 600 s: reported once
    ev = stale_events(s)
    assert len(ev) == 1 and ev[0]["actor_id"] == ids.BOARD_PRINCIPAL_ID and ev[0]["task_num"] == n
    assert ev[0]["data"]["threshold_s"] == 600 and ev[0]["data"]["gen"] == 1
    assert s.recipients("s1", ev[0]["seq"]) == ["ana", "jose", "jose/claude"]   # claimant + coordinators
    for later in (700, 3000, 99999):                                        # still idle: nothing more
        assert sweeper.sweep(db_, t0 + timedelta(seconds=later)) == []
    assert len(stale_events(s)) == 1
    row = s.raw.execute("SELECT status, gen, fence_secret, claimant_id FROM task WHERE num=?", (n,)).fetchone()
    assert (row["status"], row["gen"], row["claimant_id"]) == ("in_progress", 1, s.pid("jose/claude")) and row["fence_secret"]
    assert api(s, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "back"}).status_code == 200
    t1 = activity(s, n)
    assert sweeper.sweep(db_, t1 + timedelta(seconds=300)) == []
    assert sweeper.sweep(db_, t1 + timedelta(seconds=601)) != []          # a new idle period
    assert len(stale_events(s)) == 2


def test_session_threshold_applies_without_override(s):
    n = new_task(s)
    api(s, "POST", f"/tasks/{n}/claim", "jose/claude")
    t0 = activity(s, n)
    assert sweeper.sweep(s.app.state.db, t0 + timedelta(seconds=1799)) == []
    assert sweeper.sweep(s.app.state.db, t0 + timedelta(seconds=1801)) != []


@pytest.mark.parametrize("value", [100, 299, 14401, 20000])
def test_at_8_10_override_out_of_range_is_422(s, value):
    n = new_task(s)
    assert api(s, "PATCH", f"/tasks/{n}", "jose", {"if_version": 1, "stale_after_s": value}).status_code == 422
    assert api(s, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d",
                                             "stale_after_s": value}).status_code == 422


def test_at_8_11_changing_the_override_does_not_refresh_activity(s):
    n = new_task(s)
    api(s, "POST", f"/tasks/{n}/claim", "jose/claude")
    s.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    v = s.raw.execute("SELECT version FROM task WHERE num=?", (n,)).fetchone()[0]
    assert api(s, "PATCH", f"/tasks/{n}", "jose", {"if_version": v, "stale_after_s": 3600}).status_code == 200
    assert s.raw.execute("SELECT last_activity_at FROM task WHERE num=?", (n,)).fetchone()[0] == "2026-01-01T00:00:00.000000Z"


def test_only_active_claims_in_active_sessions_are_swept(s):
    a = new_task(s)                                             # open: never stale
    b = new_task(s)
    fb = api(s, "POST", f"/tasks/{b}/claim", "jose/claude").json()["fence"]
    api(s, "POST", f"/tasks/{b}/submit", "jose/claude", {"fence": fb, "summary": "done"})   # review: not swept
    c = new_task(s)
    api(s, "POST", f"/tasks/{c}/claim", "jose/claude")
    far = datetime.now(timezone.utc) + timedelta(days=2)
    s.raw.execute("UPDATE session SET state='paused' WHERE handle='s1'")
    assert sweeper.sweep(s.app.state.db, far) == []             # paused: skipped
    s.raw.execute("UPDATE session SET state='active' WHERE handle='s1'")
    assert [num for _, num in sweeper.sweep(s.app.state.db, far)] == [c]
    assert a not in [e["task_num"] for e in stale_events(s)]


def test_stale_shows_in_views_and_metrics(s):
    n = new_task(s, stale_after_s=300)
    api(s, "POST", f"/tasks/{n}/claim", "jose/claude")
    s.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    view = s.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()
    assert view["stale"] is True and view["effective_stale_after_s"] == 300
    assert [x["task"] for x in s.req("GET", "/api/v1/sessions/s1/tasks?stale=true", "jose", key=None).json()["tasks"]] == [n]
    sync = api(s, "POST", "/sync", "jose/claude").json()
    assert sync["claims"][0]["stale"] is True
    s.admin("add-human", "root", "--admin")
    root = s.mint("root")
    m = s.client.get("/metrics", headers={"Authorization": f"Bearer {root}"}).text
    assert "glassboard_stale_tasks 1" in m


def test_sweeper_thread_runs_in_the_app_lifespan(s):
    n = new_task(s, stale_after_s=300)
    api(s, "POST", f"/tasks/{n}/claim", "jose/claude")
    s.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    app = create_app(s.db, allowed_hosts=["testserver"], sweep_interval_s=0.05)
    with TestClient(app):                                       # the context manager runs the lifespan
        for _ in range(100):
            if stale_events(s):
                break
            time.sleep(0.05)
    assert len(stale_events(s)) == 1
    assert app.state.sweeper._thread is None                    # stopped with the app
    off = create_app(s.db, allowed_hosts=["testserver"], sweep_interval_s=0)
    with TestClient(off):
        assert off.state.sweeper._thread is None                # 0 disables


def test_a_fenced_op_during_the_sweep_wins(s, monkeypatch):
    """The sweep re-checks inside its own transaction: activity that lands after the scan is not
    reported as stale."""
    n = new_task(s, stale_after_s=300)
    fence = api(s, "POST", f"/tasks/{n}/claim", "jose/claude").json()["fence"]
    s.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    real_write = s.app.state.db.write
    calls = {"n": 0}

    def write_after_note():
        if calls["n"] == 0:
            calls["n"] += 1
            api(s, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "just in time"})
        return real_write()
    monkeypatch.setattr(s.app.state.db, "write", write_after_note)
    assert sweeper.sweep(s.app.state.db, datetime.now(timezone.utc) + timedelta(seconds=10)) == []
    assert stale_events(s) == []
