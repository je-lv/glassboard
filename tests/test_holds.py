"""B41 (ERRATA E36): decline, hold and stand-down. Agents raise; humans decide; nothing an agent raises
stops the session, and only a human stand-down or a pause stops work."""
from datetime import datetime, timedelta, timezone

import pytest

from glassboard import api_holds, ids
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import login
from test_render import assert_canonical
from test_tasks import api, claim, task


def attention(gb, who="jose"):
    r = gb.req("GET", "/api/v1/me/attention", who, key=None)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def owed(gb, who):
    r = gb.req("GET", f"/api/v1/sessions/s1/brief?principal=me", who, key=None)
    return [o["type"] for o in r.json()["owed"]["items"]]


def hold(gb, who="jose/claude", **extra):
    body = {"body": "Wait before deleting the old docs", "default": "I keep them and continue", "deadline_min": 30, **extra}
    return api(gb, "POST", "/holds", who, body)


# --- decline --------------------------------------------------------------------------------------------

def test_b41_agent_declines_an_assigned_task_with_a_visible_reason(b):
    n = mk(b)                                                   # assigned to jose/claude, open
    r = api(b, "POST", f"/tasks/{n}/decline", "jose/claude", {"reason": "Outside what I can verify here"})
    assert r.status_code == 200, r.text
    t = task(b, n)
    assert (t["status"], t["assignee_id"], t["claimant_id"], t["fence_secret"]) == ("open", None, None, None)
    ev = b.events()[-1]
    assert ev["type"] == "task.declined" and ev["body"] == "Outside what I can verify here"
    assert b.recipients("s1", ev["seq"]) == ["jose"]            # the coordinators
    items = [i for i in attention(b) if i["kind"] == "declined"]
    assert items and items[0]["task"] == n and items[0]["from"] == "jose/claude"
    text = b.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None).text
    assert_canonical(text)
    assert f"[task.declined] T{n} g1" in text and "  | Outside what I can verify here" in text
    # once a human reassigns it, the attention item is gone
    api(b, "POST", f"/tasks/{n}/reassign", "jose", {"if_version": t["version"], "to": "ana/codex"})
    assert not [i for i in attention(b) if i["kind"] == "declined"]


def test_b41_claimant_declines_with_its_fence_and_the_fence_dies(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    r = api(b, "POST", f"/tasks/{n}/decline", "jose/claude", {"reason": "Blocked by a licence question", "fence": fence})
    assert r.status_code == 200 and r.json()["status"] == "open"
    assert api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "x"}).json()["error"]["code"] == "fence_mismatch"


def test_b41_only_the_assignee_or_claimant_may_decline(b):
    n = mk(b)
    r = api(b, "POST", f"/tasks/{n}/decline", "ana/codex", {"reason": "not mine"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    fence = claim(b, n).json()["fence"]
    r = api(b, "POST", f"/tasks/{n}/decline", "jose/claude", {"reason": "no fence"})   # claimed: the fence is needed
    assert r.status_code == 409
    r = api(b, "POST", f"/tasks/{n}/decline", "ana/codex", {"reason": "stolen", "fence": fence})
    assert r.json()["error"]["code"] == "fence_mismatch"
    assert task(b, n)["status"] == "in_progress"


# --- holds ----------------------------------------------------------------------------------------------

def test_b41_hold_routes_to_humans_and_states_a_default_and_deadline(b):
    r = hold(b)
    assert r.status_code == 201, r.text
    seq = r.json()["seq"]
    assert b.recipients("s1", seq) == ["jose"]                  # human coordinators; ana is a plain member
    assert "hold" in owed(b, "jose")
    items = [i for i in attention(b) if i["kind"] == "hold"]
    assert items[0]["default"] == "I keep them and continue" and items[0]["deadline_at"]
    text = b.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None).text
    assert_canonical(text)
    assert "  · default: «I keep them and continue»" in text and "  · deadline_at: «" in text
    for bad in ({"deadline_min": 0}, {"deadline_min": 1441}, {"default": ""}):
        assert hold(b, **bad).status_code == 422
    body = {"body": "x", "deadline_min": 30}                    # no default: refused
    assert api(b, "POST", "/holds", "jose/claude", body).status_code == 422


def test_b41_a_hold_never_blocks_work(b):
    n = mk(b)
    hold(b, task=f"T{n}")
    r = claim(b, n)
    assert r.status_code == 201
    assert api(b, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": r.json()["fence"], "note": "going on"}).status_code == 200
    assert b.raw.execute("SELECT state FROM session WHERE handle='s1'").fetchone()[0] == "active"


def test_b41_a_human_decides_a_hold_once(b):
    seq = hold(b).json()["seq"]
    assert api(b, "POST", f"/holds/{seq}", "ana", {"decision": "proceed"}).status_code == 403     # not a decider
    assert api(b, "POST", f"/holds/{seq}", "ana/codex", {"decision": "proceed"}).status_code == 403
    r = api(b, "POST", f"/holds/{seq}", "jose", {"decision": "uphold", "note": "Keep them for now"})
    assert r.status_code == 200 and r.json()["decision"] == "uphold"
    ev = b.events()[-1]
    assert ev["type"] == "hold.decided" and ev["reply_to"] == seq
    assert b.recipients("s1", ev["seq"]) == ["jose/claude"]     # back to the agent that raised it
    assert "hold" not in owed(b, "jose")
    assert api(b, "POST", f"/holds/{seq}", "jose", {"decision": "proceed"}).json()["error"]["code"] == "already_resolved"


def test_b41_past_its_deadline_a_hold_expires_and_the_default_applies(b):
    seq = hold(b).json()["seq"]
    assert api_holds.expire_holds(b.app.state.db) == 0          # not yet
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    assert api_holds.expire_holds(b.app.state.db, later) == 1
    assert api_holds.expire_holds(b.app.state.db, later) == 0   # once
    ev = b.events()[-1]
    assert ev["type"] == "hold.expired" and ev["reply_to"] == seq and ev["actor_id"] == ids.BOARD_PRINCIPAL_ID
    assert ev["data"]["default"] == "I keep them and continue"
    assert sorted(b.recipients("s1", ev["seq"])) == ["jose", "jose/claude"]
    assert "hold" not in owed(b, "jose")
    assert api(b, "POST", f"/holds/{seq}", "jose", {"decision": "proceed"}).json()["error"]["code"] == "already_resolved"


def test_b41_autonomous_child_task_holds_go_to_the_agent_coordinator_too(gb):
    gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "s1", "title": "Build", "autonomous": True})
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana/codex", role="coordinator")
    root = api(gb, "POST", "/tasks", "jose", {"title": "Root", "scope": "s", "deliverable": "d"}).json()["task"]
    child = api(gb, "POST", "/tasks", "ana/codex", {"title": "Child", "scope": "s", "deliverable": "d",
                                                    "parent": f"T{root}"}).json()["task"]
    seq = hold(gb, task=f"T{child}").json()["seq"]
    assert gb.recipients("s1", seq) == ["ana/codex", "jose"]
    r = api(gb, "POST", f"/holds/{seq}", "ana/codex", {"decision": "proceed"})
    assert r.status_code == 200, r.text
    # a hold on the root task stays with the humans
    seq = hold(gb, task=f"T{root}").json()["seq"]
    assert gb.recipients("s1", seq) == ["jose"]
    assert api(gb, "POST", f"/holds/{seq}", "ana/codex", {"decision": "proceed"}).status_code == 403


# --- stand-down -----------------------------------------------------------------------------------------

def standdown(gb, who="jose", member="jose/claude"):
    return api(gb, "POST", "/standdowns", who, {"member": member, "reason": "Pause your part while we re-plan"})


def test_b41_only_a_human_coordinator_or_the_owner_asks_for_a_stand_down(b):
    assert standdown(b, "ana").status_code == 403               # a human member who does not own the agent
    assert standdown(b, "ana/codex").status_code == 403         # agents never
    assert standdown(b, "ana", member="ana/codex").status_code == 201    # ana owns ana/codex
    assert standdown(b, "jose", member="ana").status_code == 404         # humans are not stood down
    r = standdown(b)
    assert r.status_code == 201
    assert standdown(b).json()["error"]["code"] == "already_requested"
    assert "standdown.requested" in owed(b, "jose/claude")


def test_b41_stand_down_is_answered_by_release_and_checkpoint(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    api(b, "POST", "/leases", "jose/claude", {"resource": "docs/faq.md"})
    seq = standdown(b).json()["seq"]
    # until it is lifted the agent cannot take new work
    other = mk(b, title="Other")
    assert claim(b, other).json()["error"]["code"] == "standing_down"
    assert api(b, "POST", "/leases", "jose/claude", {"resource": "x"}).json()["error"]["code"] == "standing_down"
    r = api(b, "POST", f"/standdowns/{seq}/done", "jose/claude", {})
    assert r.status_code == 409 and r.json()["error"]["details"] == {"claims": [f"T{n}"], "leases": 1,
                                                                     "checkpoint_saved": False}
    assert api(b, "POST", f"/standdowns/{seq}/done", "ana/codex", {}).status_code == 403
    api(b, "POST", f"/tasks/{n}/release", "jose/claude", {"fence": fence, "handoff": "Half done; see notes"})
    api(b, "POST", "/leases/release", "jose/claude", {"resource": "docs/faq.md"})
    api(b, "POST", "/checkpoints", "jose/claude", {"summary": "Stood down", "next_action": "Wait for the lift"})
    r = api(b, "POST", f"/standdowns/{seq}/done", "jose/claude", {"note": "Released T1"})
    assert r.status_code == 200, r.text
    ev = b.events()[-1]
    assert ev["type"] == "standdown.done" and ev["reply_to"] == seq
    assert b.recipients("s1", ev["seq"]) == ["jose"]            # owner and coordinators
    assert "standdown.requested" not in owed(b, "jose/claude")
    assert claim(b, other).json()["error"]["code"] == "standing_down"     # still until lifted
    brief = b.req("GET", "/api/v1/sessions/s1/brief?format=text", "jose/claude", key=None).text
    assert_canonical(brief)
    assert f"--- stand-down #{seq} requested by jose: answered at #{ev['seq']}, waiting for a human to lift it ---" in brief
    assert api(b, "POST", f"/standdowns/{seq}/lift", "ana", {}).status_code == 403
    assert api(b, "POST", f"/standdowns/{seq}/lift", "jose", {"note": "Carry on"}).status_code == 200
    assert claim(b, other).status_code == 201
    assert api(b, "POST", f"/standdowns/{seq}/lift", "jose", {}).json()["error"]["code"] == "already_resolved"


def test_b41_other_agents_keep_working_during_a_stand_down(b):
    standdown(b)
    n = mk(b, assignee="ana/codex")
    assert claim(b, n, who="ana/codex").status_code == 201


def test_b41_lift_before_an_answer_resolves_the_obligation(b):
    seq = standdown(b).json()["seq"]
    assert api(b, "POST", f"/standdowns/{seq}/lift", "jose", {}).status_code == 200
    assert "standdown.requested" not in owed(b, "jose/claude")
    assert api(b, "POST", f"/standdowns/{seq}/done", "jose/claude", {}).json()["error"]["code"] == "already_resolved"


# --- CLI and dashboard ----------------------------------------------------------------------------------

def test_b41_cli_hold_decline_and_stand_down(L, gb):
    for who in ("jose", "jose/claude"):
        ok(L.cli(who, "use", "s1"))
    out = ok(L.cli("jose/claude", "hold", "raise", "Wait before the rename", "--default", "I rename at the deadline",
                   "--deadline", "1h"))
    assert out.startswith("hold #") and "otherwise the stated default applies" in out
    seq = int(out.split("#")[1].split()[0])
    assert ok(L.cli("jose", "hold", "decide", str(seq), "proceed")).startswith(f"hold #{seq} overruled")
    n = ok(L.cli("jose", "task", "create", "Rename", "--scope", "s", "--deliverable", "d", "--assignee", "jose/claude"))
    num = n.split()[0]
    ok(L.cli("jose/claude", "task", "claim", num))
    assert " open " in ok(L.cli("jose/claude", "task", "decline", num, "Needs a decision first"))
    m = ok(L.cli("jose", "task", "create", "Docs", "--scope", "s", "--deliverable", "d", "--assignee", "jose/claude")).split()[0]
    ok(L.cli("jose/claude", "task", "claim", m))
    ok(L.cli("jose/claude", "lease", "take", "docs/x.md"))
    sd = ok(L.cli("jose", "standdown", "request", "jose/claude", "Re-planning"))
    sd_seq = int(sd.split("#")[1].split()[0])
    r = L.cli("jose/claude", "standdown", "done")
    assert r.returncode != 0 and "checkpoint" in (r.stdout + r.stderr)          # released, but no checkpoint yet
    ok(L.cli("jose/claude", "checkpoint", "save", "--summary", "Stood down", "--next", "Wait"))
    out = ok(L.cli("jose/claude", "standdown", "done"))
    assert f"stand-down #{sd_seq} answered" in out
    assert ok(L.cli("jose", "standdown", "lift", str(sd_seq))).startswith(f"stand-down #{sd_seq} lifted")


def test_b41_dashboard_hold_and_stand_down_forms(b):
    seq = hold(b).json()["seq"]
    c = login(b, "jose")
    home = c.get("/")
    assert f"/s/s1/holds/{seq}" in home.text and "I keep them and continue" in home.text
    import re
    token = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
    r = c.post(f"/s/s1/holds/{seq}", data={"csrf": token, "decision": "uphold"}, follow_redirects=False)
    assert r.status_code == 303
    assert b.events()[-1]["type"] == "hold.decided"
    page = c.get("/s/s1/agent?as=jose/claude")
    assert "Ask to stand down" in page.text
    r = c.post("/s/s1/standdowns", data={"csrf": token, "member": "jose/claude", "reason": "Re-plan"}, follow_redirects=False)
    assert r.status_code == 303 and b.events()[-1]["type"] == "standdown.requested"
    page = c.get("/s/s1/agent?as=jose/claude")
    assert "Lift stand-down" in page.text and "has not answered yet" in page.text
    sd = b.events()[-1]["seq"]
    r = c.post(f"/s/s1/standdowns/{sd}/lift", data={"csrf": token, "member": "jose/claude"}, follow_redirects=False)
    assert r.status_code == 303 and b.events()[-1]["type"] == "standdown.lifted"
