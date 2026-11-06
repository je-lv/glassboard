"""B56 (ERRATA E38): quiet hours. Wakes for an agent and browser notifications for a human wait for a
daily window to end; nothing is hidden or dropped; only humans set them."""
from datetime import datetime, timedelta, timezone

import pytest

from glassboard import quiet
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import csrf_of, login
from test_render import assert_canonical
from test_wake import arm, op, post, setup, status


def window_now(minutes_before=60, minutes_after=60):
    """A UTC window that contains the current minute."""
    now = datetime.now(timezone.utc)
    return ((now - timedelta(minutes=minutes_before)).strftime("%H:%M"),
            (now + timedelta(minutes=minutes_after)).strftime("%H:%M"))


def window_later():
    now = datetime.now(timezone.utc)
    return (now + timedelta(hours=3)).strftime("%H:%M"), (now + timedelta(hours=4)).strftime("%H:%M")


def set_quiet(gb, who, member, start=None, end=None, tz="UTC", urgent=False):
    body = {"member": member} if start is None else {"member": member, "start": start, "end": end, "tz": tz,
                                                     "urgent": urgent}
    return gb.req("POST", "/api/v1/sessions/s1/quiet-hours", who, json=body, rec=gb.rec())


# --- the window -----------------------------------------------------------------------------------------

def test_b56_window_crosses_midnight_in_its_time_zone():
    at = datetime(2026, 11, 6, 22, 30, tzinfo=timezone.utc)              # 23:30 in Madrid
    q = quiet.state("22:00", "07:00", "Europe/Madrid", at)
    assert q == {"window": "22:00-07:00 Europe/Madrid", "active": True, "until": "2026-11-07T06:00:00Z"}
    q = quiet.state("22:00", "07:00", "Europe/Madrid", datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))
    assert q["active"] is False and q["until"] == "2026-11-06T21:00:00Z"
    assert quiet.state("09:00", "17:00", "UTC", datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc))["active"] is False
    assert quiet.state(None, None, None) is None


@pytest.mark.parametrize("body", [
    {"start": "22:00", "end": "22:00", "tz": "UTC"}, {"start": "24:00", "end": "07:00", "tz": "UTC"},
    {"start": "22:00", "end": "07:00", "tz": "Mars/Olympus"}, {"start": "22:00", "end": "07:00", "tz": "../../etc"},
    {"start": "22:00", "end": "07:00"}, {"start": "2200", "end": "0700", "tz": "UTC"}])
def test_b56_bad_windows_are_refused_without_echo(gb, body):
    setup(gb)
    r = gb.req("POST", "/api/v1/sessions/s1/quiet-hours", "jose", json={"member": "jose/claude", **body}, rec=gb.rec())
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert "Olympus" not in r.text and "etc" not in r.text


# --- who sets it ----------------------------------------------------------------------------------------

def test_b56_only_humans_set_quiet_hours(gb):
    setup(gb)
    s, e = window_later()
    assert set_quiet(gb, "jose", "jose/claude", s, e).status_code == 200        # owner and coordinator
    assert set_quiet(gb, "ana", "ana/codex", s, e).status_code == 200           # owner, not a coordinator
    assert set_quiet(gb, "ana", "jose/claude", s, e).status_code == 403         # neither owner nor coordinator
    assert set_quiet(gb, "ana", "ana", s, e).status_code == 200                 # a human for themselves
    assert set_quiet(gb, "jose", "ana", s, e).status_code == 403                # not for another human
    assert set_quiet(gb, "jose/claude", "jose/claude", s, e).status_code == 403  # agents never
    assert set_quiet(gb, "jose", "nobody", s, e).status_code in (404, 422)


def test_b56_a_change_is_one_visible_event(gb):
    setup(gb)
    r = set_quiet(gb, "jose", "jose/claude", "22:00", "07:00", "Europe/Madrid", urgent=True)
    assert r.status_code == 200 and r.json()["quiet"] == "22:00-07:00 Europe/Madrid" and r.json()["urgent"] is True
    ev = gb.events()[-1]
    assert ev["type"] == "member.quiet" and gb.recipients("s1", ev["seq"]) == ["jose/claude"]
    text = gb.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None).text
    assert_canonical(text)
    assert "  · quiet: «22:00-07:00 Europe/Madrid»" in text
    r = set_quiet(gb, "jose", "jose/claude")                                     # clear
    assert r.json()["quiet"] == "off" and gb.events()[-1]["data"]["quiet"] == "off"
    row = gb.raw.execute("SELECT quiet_start, quiet_urgent FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()
    assert tuple(row) == (None, 0)


# --- effects --------------------------------------------------------------------------------------------

def test_b56_wakes_wait_during_quiet_hours_and_go_out_once_after(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    s, e = window_now()
    set_quiet(gb, "jose", "jose/claude", s, e)
    post(gb, "jose", body="one", to=["jose/claude"])
    g = post(gb, "ana", body="two", to=["jose/claude"])
    st = status(gb)
    assert st["pending"] and st["quiet"]["active"] and st["quiet"]["blocks_dispatch"]
    r = op(gb, "dispatch", {"fence": fence, "generation": g})
    assert r.status_code == 409 and r.json()["error"]["code"] == "quiet_hours" and r.json()["error"]["details"]["until"]
    # messages and sync are unchanged: the agent can still read everything
    synced = gb.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={}, rec=gb.rec()).json()
    assert [x["body"] for x in synced["inbox"]][-2:] == ["one", "two"]
    set_quiet(gb, "jose", "jose/claude")                                         # the window ends (cleared here)
    st = status(gb)
    assert st["generation"] >= g and not (st["quiet"] or {}).get("blocks_dispatch")
    assert op(gb, "dispatch", {"fence": fence, "generation": st["generation"]}).status_code == 200   # once, coalesced


def test_b56_urgent_lets_human_caused_wakes_through(gb):
    setup(gb)
    fence = arm(gb).json()["fence"]
    s, e = window_now()
    set_quiet(gb, "jose", "jose/claude", s, e, urgent=True)
    g = post(gb, "ana/codex", kind="question", body="agent question", to=["jose/claude"])
    assert status(gb)["quiet"]["blocks_dispatch"] is True                       # an agent's question waits
    assert op(gb, "dispatch", {"fence": fence, "generation": g}).json()["error"]["code"] == "quiet_hours"
    g = post(gb, "jose", body="the human needs you now", to=["jose/claude"])
    assert status(gb)["quiet"]["blocks_dispatch"] is False
    assert op(gb, "dispatch", {"fence": fence, "generation": g}).status_code == 200


def test_b56_status_shows_who_is_quiet_and_until_when(gb):
    setup(gb)
    s, e = window_now()
    set_quiet(gb, "jose", "jose/claude", s, e)
    rows = gb.req("GET", "/api/v1/sessions/s1/status", "jose", key=None).json()["agents"]
    q = next(r for r in rows if r["member"] == "jose/claude")["quiet"]
    assert q["active"] is True and q["window"] == f"{s}-{e} UTC"
    text = gb.req("GET", "/api/v1/sessions/s1/status?format=text", "jose", key=None).text
    assert_canonical(text)
    assert f"  · quiet hours: «{s}-{e} UTC · quiet now, until " in text


def test_b56_a_quiet_human_gets_counts_but_no_notifications(gb):
    setup(gb)
    post(gb, "jose/claude", kind="question", body="Which tone?", to=["jose"])
    c = login(gb, "jose")
    assert c.get("/attention-count").json() == {"count": 1, "notify": 1}
    s, e = window_now()
    set_quiet(gb, "jose", "jose", s, e)
    assert c.get("/attention-count").json() == {"count": 1, "notify": 0}
    assert "Which tone?" in c.get("/").text                                       # the queue itself is unchanged


def test_b56_dashboard_form_sets_and_clears(gb):
    setup(gb)
    c = login(gb, "jose")
    page = c.get("/s/s1/agent?as=jose/claude")
    assert "Set quiet hours" in page.text
    token = csrf_of(page.text)
    r = c.post("/s/s1/quiet-hours", data={"csrf": token, "member": "jose/claude", "start": "22:00", "end": "07:00",
                                          "tz": "Europe/Madrid", "urgent": "1", "action": "set"}, follow_redirects=False)
    assert r.status_code == 303 and gb.events()[-1]["data"]["quiet"] == "22:00-07:00 Europe/Madrid"
    assert "22:00-07:00 Europe/Madrid" in c.get("/s/s1/agent?as=jose/claude").text
    r = c.post("/s/s1/quiet-hours", data={"csrf": token, "member": "jose/claude", "action": "clear"}, follow_redirects=False)
    assert r.status_code == 303 and gb.events()[-1]["data"]["quiet"] == "off"
    ana = login(gb, "ana")
    assert "Set quiet hours" not in ana.get("/s/s1/agent?as=jose/claude").text   # not ana's agent


def test_b56_cli_quiet_set_show_clear(L, gb):
    for who in ("jose", "jose/claude"):
        ok(L.cli(who, "use", "s1"))
    out = ok(L.cli("jose", "quiet", "set", "--for", "jose/claude", "--from", "22:00", "--to", "07:00", "--tz", "UTC",
                   "--urgent"))
    assert out.startswith("@jose/claude quiet hours: 22:00-07:00 UTC · human-caused wakes go through")
    assert "quiet hours: «22:00-07:00 UTC" in ok(L.cli("jose", "quiet", "show"))
    assert ok(L.cli("jose", "quiet", "clear", "--for", "jose/claude")).startswith("@jose/claude quiet hours: off")
    r = L.cli("jose/claude", "quiet", "set", "--from", "22:00", "--to", "07:00", "--tz", "UTC")
    assert r.returncode != 0 and "forbidden" in (r.stdout + r.stderr)
