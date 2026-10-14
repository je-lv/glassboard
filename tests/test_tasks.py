"""Tasks: PATCH and fenced activity, immutable receipts, inbox takeover leaving fences alone, fences shown
only where minted, idempotency-key binding on task routes, the state table for every (state, operation,
actor), the claim race (drill D1), and the default for a returned task (back to open, assigned to the
previous claimant). The state-table oracle below is written from the documented task state machine by
hand, not derived from the code."""
import itertools
import re
import threading

import pytest

from glassboard import ids
from test_render import assert_canonical

FENCE_RE = re.compile(r"T\d+g\d+-[a-z2-7]{10}")
SECRET_RE = re.compile(r"T\d+g\d+-[a-z2-7]{10}|C\d+-[a-z2-7]{10}")


@pytest.fixture
def t(gb):
    gb.session("s1")
    gb.add("s1", "ana")                                  # human member (not coordinator)
    gb.add("s1", "jose/claude")                          # the claimant in most tests
    gb.add("s1", "ana/codex", role="coordinator")        # an agent coordinator
    return gb


def api(gb, method, path, who, body=None, key=True, rec=True, extra=None):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body if body is not None else {},
                  rec=gb.rec() if rec else None, key=key, extra=extra)


def create(gb, title="Write onboarding", assignee=None, who="jose"):
    body = {"title": title, "scope": "Cover install, tokens and TLS.", "deliverable": "docs/onboarding.md"}
    if assignee:
        body["assignee"] = assignee
    r = api(gb, "POST", "/tasks", who, body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


def claim(gb, n, who="jose/claude", key=True, extra=None):
    r = api(gb, "POST", f"/tasks/{n}/claim", who, key=key, extra=extra)
    return r


def task(gb, n):
    return gb.raw.execute("SELECT * FROM task WHERE num=?", (n,)).fetchone()


def test_assignment_acknowledgment_is_visible_and_resets_on_reassignment(t):
    n = create(t, assignee="jose/claude")
    path = f"/tasks/{n}"
    assert api(t, "GET", path, "jose").json()["assignment_stage"] == "assigned"
    assert api(t, "POST", f"{path}/acknowledge", "ana").status_code == 403
    assert api(t, "POST", f"{path}/acknowledge", "jose/claude").status_code == 200
    assert api(t, "GET", path, "jose").json()["assignment_stage"] == "acknowledged"
    assert api(t, "POST", f"{path}/acknowledge", "jose/claude").status_code == 409
    assert claim(t, n).status_code == 201
    assert api(t, "GET", path, "jose").json()["assignment_stage"] == "working"
    assert api(t, "POST", f"{path}/reassign", "jose", {"if_version": task(t, n)["version"], "to": "ana"}).status_code == 200
    assert api(t, "GET", path, "jose").json()["assignment_stage"] == "assigned"
    assert api(t, "POST", f"{path}/acknowledge", "ana").status_code == 200
    assert [e["type"] for e in t.events() if e["task_num"] == n].count("task.acknowledged") == 2


def test_assignment_sync_is_distinct_from_acknowledgment(t):
    n = create(t, assignee="jose/claude")
    path = f"/tasks/{n}"
    initial = api(t, "GET", path, "jose").json()
    assert initial["assignment_stage"] == "assigned"
    assert initial["assignee_synced_since_assignment"] is False
    assert initial["assignee_last_seen_at"] is None

    assert api(t, "POST", "/sync", "jose/claude", {}).status_code == 200
    synced = api(t, "GET", path, "jose").json()
    assert synced["assignment_stage"] == "assigned"
    assert synced["assignee_synced_since_assignment"] is True
    assert synced["assignee_last_seen_at"] is not None

    assert api(t, "POST", f"{path}/reassign", "jose", {"if_version": task(t, n)["version"], "to": "ana"}).status_code == 200
    reassigned = api(t, "GET", path, "jose").json()
    assert reassigned["assignee_synced_since_assignment"] is False
    assert api(t, "POST", "/sync", "ana", {}).status_code == 200
    assert api(t, "GET", path, "jose").json()["assignee_synced_since_assignment"] is True


def test_placeholder_submission_is_refused_and_short_legacy_summary_is_flagged(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    before = t.head()
    for value in ("-", "...", "n/a"):
        r = api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": value})
        assert r.status_code == 422 and r.json()["error"]["code"] == "summary_required"
        assert t.head() == before
    assert api(t, "POST", f"/tasks/{n}/submit", "jose/claude",
               {"fence": fence, "summary": "Implementation complete; see commit abc123 and test report."}).status_code == 200
    assert api(t, "GET", f"/tasks/{n}", "jose").json()["summary_needs_detail"] is False


# --- the state table ---------------------------------------------------------------------

STATES = ("open", "in_progress", "blocked", "review", "done", "cancelled")
ACTORS = ("claimant", "coord", "member", "agentcoord")
WHO = {"claimant": "jose/claude", "coord": "jose", "member": "ana", "agentcoord": "ana/codex"}
OPS = ("claim", "note", "block", "unblock", "submit", "release", "release_coord", "reclaim", "accept", "return",
       "reassign", "cancel", "reopen", "patch")


def expected(state, op, actor):
    """The documented task transitions and request check order, written independently of api_tasks.py.
    Returns the HTTP status (and error code for 409s)."""
    active = state in ("in_progress", "blocked")
    non_terminal = state in ("open", "in_progress", "blocked", "review")
    human_coord = actor == "coord"
    # claimant identity after each state's history (submit keeps the claimant for accept/return;
    # cancel clears it; open has none)
    is_claimant = actor == "claimant" and state in ("in_progress", "blocked", "review", "done")
    if op == "claim":
        if state == "open":
            return 201
        return (409, "task_already_claimed") if active else (409, "task_not_claimable")
    if op in ("note", "block", "unblock", "submit", "release"):
        # fenced: anyone but the claimant holding the live fence → fence_mismatch, before state
        if not (actor == "claimant" and active):
            return (409, "fence_mismatch")
        allowed = {"note": ("in_progress",), "block": ("in_progress",), "unblock": ("blocked",),
                   "submit": ("in_progress", "blocked"), "release": ("in_progress", "blocked")}[op]
        return 200 if state in allowed else (409, "invalid_transition")
    if op == "release_coord":
        if not human_coord:
            return 403
        return 200 if active else (409, "invalid_transition")
    if op == "reclaim":
        if not (is_claimant or actor in ("coord", "agentcoord")):
            return 403
        return 200 if active else (409, "invalid_transition")
    if op in ("accept", "return", "reassign", "cancel", "reopen", "patch"):
        # dividing work (reassign, patch) is also the orchestrator's: an agent coordinator (ERRATA E23)
        if not (human_coord or (actor == "agentcoord" and op in ("reassign", "patch"))):
            return 403
        allowed = {"accept": ("review",), "return": ("review",),
                   "reassign": ("open", "in_progress", "blocked", "review"),
                   "cancel": ("open", "in_progress", "blocked", "review"), "reopen": ("done", "cancelled"),
                   "patch": ("open", "in_progress", "blocked", "review")}[op]
        return 200 if state in allowed else (409, "invalid_transition")
    raise AssertionError(op)


def put_in_state(gb, state):
    """A fresh task in `state`; returns (num, the claimant's fence or None)."""
    n = create(gb)
    fence = None
    if state in ("in_progress", "blocked", "review", "done"):
        fence = claim(gb, n).json()["fence"]
    if state == "blocked":
        assert api(gb, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "waiting"}).status_code == 200
    if state in ("review", "done"):
        assert api(gb, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "done"}).status_code == 200
    if state == "done":
        assert api(gb, "POST", f"/tasks/{n}/accept", "jose", {"if_version": task(gb, n)["version"]}).status_code == 200
    if state == "cancelled":
        assert api(gb, "POST", f"/tasks/{n}/cancel", "jose", {"if_version": task(gb, n)["version"]}).status_code == 200
    return n, fence


def run_op(gb, n, fence, op, actor):
    who = WHO[actor]
    f = fence or f"T{n}g1-aaaaaaaaaa"
    current = task(gb, n)
    submission = next((e for e in reversed(gb.events()) if e["task_num"] == n and e["type"] == "task.submitted"), None)
    body = {"claim": {}, "note": {"fence": f, "note": "progress"}, "block": {"fence": f, "reason": "r"},
            "unblock": {"fence": f}, "submit": {"fence": f, "summary": "s"}, "release": {"fence": f},
            "release_coord": {}, "reclaim": {"reason": "lost my context"},
            "accept": {"if_version": current["version"]},
            "return": {"if_version": current["version"], "submission_seq": submission["seq"] if submission else 1,
                       "feedback": "fix x"},
            "reassign": {"if_version": current["version"], "to": "ana"},
            "cancel": {"if_version": current["version"]}, "reopen": {"if_version": current["version"]},
            "patch": {"if_version": current["version"], "title": "renamed"}}[op]
    path = {"patch": f"/tasks/{n}", "release_coord": f"/tasks/{n}/release"}.get(op, f"/tasks/{n}/{op}")
    return api(gb, "PATCH" if op == "patch" else "POST", path, who, body)


def test_state_table_for_every_state_operation_and_actor(t):
    mismatches = []
    for state, op, actor in itertools.product(STATES, OPS, ACTORS):
        n, fence = put_in_state(t, state)
        head = t.head()
        r = run_op(t, n, fence, op, actor)
        want = expected(state, op, actor)
        got = r.status_code if r.status_code < 400 or r.status_code in (403, 404) else (r.status_code, r.json()["error"]["code"])
        if got != want:
            mismatches.append(f"{state:12} {op:14} {actor:10} want {want} got {got}")
        # one event exactly when the operation succeeded
        if r.status_code < 300 and not (op == "patch" and r.json().get("seq") is None):
            assert t.head() == head + 1, (state, op, actor)
        elif r.status_code >= 400:
            assert t.head() == head, (state, op, actor)
    assert not mismatches, "\n" + "\n".join(mismatches)


# --- PATCH and fenced activity ------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("assignee", "ana"), ("status", "done"), ("claimant", "ana"),
                                         ("gen", 9), ("fence", "T1g1-aaaaaaaaaa"), ("version", 9)])
def test_at_6_1_ownership_fields_are_not_patchable(t, field, value):
    n = create(t)
    before, head = tuple(task(t, n)), t.head()
    r = api(t, "PATCH", f"/tasks/{n}", "jose", {"if_version": 1, field: value})
    assert r.status_code == 422 and r.json()["error"]["code"] == "field_not_patchable"
    assert "reassign" in r.json()["error"]["hint"]
    assert tuple(task(t, n)) == before and t.head() == head


def test_at_6_2_patch_keeps_the_claim(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    before = task(t, n)
    r = api(t, "PATCH", f"/tasks/{n}", "jose", {"if_version": before["version"], "scope": "Now also cover Windows."})
    assert r.status_code == 200
    after = task(t, n)
    assert (after["gen"], after["fence_secret"], after["last_activity_at"]) ==\
           (before["gen"], before["fence_secret"], before["last_activity_at"])
    ev = t.events()[-1]
    assert ev["type"] == "task.edited" and ev["data"]["scope"] == "Now also cover Windows."
    assert t.recipients("s1", ev["seq"]) == ["jose/claude"]
    assert api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "ok"}).status_code == 200
    stale = api(t, "PATCH", f"/tasks/{n}", "jose", {"if_version": before["version"], "title": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"


def test_at_6_3_messages_about_a_task_do_not_refresh_activity(t):
    n = create(t)
    claim(t, n)
    t.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    r = api(t, "POST", "/messages", "jose/claude", {"kind": "update", "body": "working on it", "task": f"T{n}"})
    assert r.status_code == 201 and t.events()[-1]["task_num"] == n
    assert task(t, n)["last_activity_at"] == "2026-01-01T00:00:00.000000Z"


def test_at_6_4_and_6_5_only_a_valid_fenced_op_refreshes(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    t.raw.execute("UPDATE task SET last_activity_at='2026-01-01T00:00:00.000000Z' WHERE num=?", (n,))
    bad = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence[:-1] + ("a" if fence[-1] != "a" else "b"), "note": "x"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "fence_mismatch"
    assert task(t, n)["last_activity_at"] == "2026-01-01T00:00:00.000000Z"
    ok = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "real progress"})
    assert ok.status_code == 200 and task(t, n)["last_activity_at"] > "2026-01-01"


def test_at_6_6_agents_cannot_patch(t):
    """Worker agents never edit tasks. Amended by ERRATA E23: an agent the humans made coordinator (the
    orchestrator, ana/codex here) divides the work, so it may edit; it still cannot accept or cancel."""
    n = create(t)
    assert api(t, "PATCH", f"/tasks/{n}", "jose/claude", {"if_version": 1, "title": "x"}).status_code == 403
    assert api(t, "PATCH", f"/tasks/{n}", "ana/codex", {"if_version": 1, "title": "x"}).status_code == 200
    assert api(t, "POST", f"/tasks/{n}/cancel", "ana/codex", {"if_version": task(t, n)["version"], "reason": "no"}).status_code == 403


def test_at_6_7_reassign_kills_the_fence(t):
    n = create(t)
    j = claim(t, n).json()
    r = api(t, "POST", f"/tasks/{n}/reassign", "jose", {"if_version": task(t, n)["version"], "to": "ana", "reason": "ana knows TLS"})
    assert r.status_code == 200 and r.json()["gen"] == j["gen"] + 1 and task(t, n)["assignee_id"] == t.pid("ana")
    ev = t.events()[-1]
    assert ev["type"] == "task.reassigned" and t.recipients("s1", ev["seq"]) == ["ana", "jose/claude"]
    old = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": j["fence"], "note": "x"})
    assert old.status_code == 409 and old.json()["error"]["code"] == "fence_mismatch"
    assert claim(t, n, who="jose/claude").json()["error"]["code"] == "task_not_claimable"   # assigned to ana now
    assert claim(t, n, who="ana").status_code == 201


# --- immutable receipts for task operations -----------------------------------------------------

def test_at_4_1_replayed_claim_returns_the_original_dead_fence(t):
    n = create(t)
    k = ids.new_ulid()
    a = claim(t, n, key=k)
    api(t, "POST", f"/tasks/{n}/reassign", "jose", {"if_version": task(t, n)["version"], "to": "ana"})
    b = claim(t, n, key=k)
    assert b.content == a.content and b.headers["glassboard-replayed"] == "true" and b.status_code == 201
    assert re.search(rf"T{n}g1-", a.json()["fence"])
    r = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": a.json()["fence"], "note": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "fence_mismatch"


def test_at_4_2_replay_never_returns_a_newer_fence(t):
    n = create(t)
    k = ids.new_ulid()
    g1 = claim(t, n, key=k).json()["fence"]
    g2 = api(t, "POST", f"/tasks/{n}/reclaim", "jose/claude", {"reason": "restarted"}).json()["fence"]
    assert g2.startswith(f"T{n}g2-") and g1.startswith(f"T{n}g1-")
    assert claim(t, n, key=k).json()["fence"] == g1


def test_at_4_6_replayed_submit_after_accept(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    k = ids.new_ulid()
    a = api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "draft complete"}, key=k)
    api(t, "POST", f"/tasks/{n}/accept", "jose", {"if_version": task(t, n)["version"]})
    head = t.head()
    b = api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "draft complete"}, key=k)
    assert b.content == a.content and b.json()["status"] == "review"
    assert task(t, n)["status"] == "done" and t.head() == head


# --- inbox takeover does not touch fences -------------------------------------------------------

def test_at_2_9_takeover_leaves_task_fences_until_reclaim(t):
    rt = lambda label: {"X-Glassboard-Runtime": label}
    n = create(t)
    a_fence = claim(t, n, extra=rt("a1f")).json()["fence"]
    api(t, "POST", "/sync", "jose/claude", {}, extra=rt("a1f"))
    api(t, "POST", "/sync", "jose/claude", {"takeover": {"reason": "new runtime"}}, extra=rt("b77"))
    assert api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": a_fence, "note": "A still works"},
               extra=rt("a1f")).status_code == 200
    b_fence = api(t, "POST", f"/tasks/{n}/reclaim", "jose/claude", {"reason": "B takes the task"}, extra=rt("b77")).json()["fence"]
    assert task(t, n)["claimant_runtime"] == "b77"
    r = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": a_fence, "note": "A again"}, extra=rt("a1f"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "fence_mismatch"
    assert api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": b_fence, "note": "B"}, extra=rt("b77")).status_code == 200


def test_coordinator_reclaim_kills_the_fence_without_taking_it(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    r = api(t, "POST", f"/tasks/{n}/reclaim", "jose", {"reason": "runtime is gone"})
    assert r.status_code == 200 and r.json()["fence"] is None and task(t, n)["claimant_id"] == t.pid("jose/claude")
    assert t.events()[-1]["data"]["on_behalf_of"] == "jose/claude"
    assert api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "x"}).json()["error"]["code"] == "fence_mismatch"
    assert api(t, "POST", f"/tasks/{n}/reclaim", "jose/claude", {"reason": "back"}).json()["fence"].startswith(f"T{n}g3-")


# --- fences appear only where they are minted -------------------------------------------------

def test_at_e1_1_and_e1_2_fence_only_in_claim_and_reclaim(t):
    seen, orig = [], t.client.request

    def rec(method, url, *a, **k):
        r = orig(method, url, *a, **k)
        path = url.split("?")[0]
        minting = method == "POST" and (path.endswith("/claim") or path.endswith("/reclaim") or
                                        (path.endswith("/sync") and "consumer" not in (k.get("json") or {})))
        seen.append((method, url, minting, r))
        return r
    t.client.request = rec
    n = create(t, assignee="jose/claude")
    k = ids.new_ulid()
    fence = claim(t, n, key=k).json()["fence"]
    claim(t, n, key=k)                                                          # replay of a minting op
    c = api(t, "POST", "/sync", "jose/claude", {}).json()["consumer"]
    api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "step"})
    api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence[:-1] + "q", "note": "bad"})
    api(t, "POST", f"/tasks/{n}/note", "ana", {"fence": fence, "note": "stolen"})
    s = api(t, "POST", "/sync", "jose/claude", {"consumer": c}, key=None)
    assert s.json()["claims"] == [{"task": n, "status": "in_progress", "gen": 1, "activity_age_s": 0, "stale": False,
                                      "progress": None, "waiting_on": None}]
    txt = t.client.post("/api/v1/sessions/s1/sync?format=text", json={"consumer": c},
                        headers=t.headers("jose/claude", rec=t.rec(), key=None)).text
    assert f"claims: T{n} in_progress g1 (activity 0m ago)" in txt
    view = t.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()
    assert view["fence"] == f"T{n} g1"
    for p in (f"/tasks/{n}", f"/tasks/{n}?format=text", "/tasks", f"/events?task={n}", "/events?format=text",
              "/export?format=md", "/export?format=jsonl"):
        t.req("GET", f"/api/v1/sessions/s1{p}", "jose", key=None)
    api(t, "POST", f"/tasks/{n}/reclaim", "jose/claude", {"reason": "r"})
    api(t, "PATCH", f"/tasks/{n}", "jose", {"if_version": task(t, n)["version"], "title": "x"})
    t.client.request = orig
    assert len(seen) > 15
    for method, url, minting, r in seen:
        if minting:
            continue
        text = r.text + " ".join(f"{a}: {b}" for a, b in r.headers.items())
        assert not SECRET_RE.search(text), (method, url, text[:300])


def test_at_e1_3_fence_mismatch_carries_no_secret(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    api(t, "POST", f"/tasks/{n}/reclaim", "jose/claude", {"reason": "r"})
    r = api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "x"})
    e = r.json()["error"]
    assert e["code"] == "fence_mismatch" and e["details"] == {"task": n, "gen": 2, "claimant": "jose/claude"}
    assert not SECRET_RE.search(r.text.replace(fence, "")) and task(t, n)["fence_secret"] not in r.text


# --- idempotency keys bind to the exact task request ---------------------------------------------

def test_at_e2_1_same_key_on_another_task(t):
    a, b = create(t), create(t)
    k = ids.new_ulid()
    assert claim(t, a, key=k).status_code == 201
    r = claim(t, b, key=k)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert task(t, b)["status"] == "open"


def test_at_e2_2_same_key_note_then_block(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    k = ids.new_ulid()
    assert api(t, "POST", f"/tasks/{n}/note", "jose/claude", {"fence": fence, "note": "x"}, key=k).status_code == 200
    r = api(t, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "x"}, key=k)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_at_e2_3_task_number_forms_are_the_same_request(t):
    n = create(t)
    k = ids.new_ulid()
    a = t.req("POST", f"/api/v1/sessions/s1/tasks/{n}/claim", "jose/claude", json={}, rec=t.rec(), key=k)
    b = t.req("POST", f"/api/v1/sessions/s1/tasks/T{n}/claim", "jose/claude", json={}, rec=t.rec(), key=k)
    assert b.headers.get("glassboard-replayed") == "true" and b.content == a.content


def test_at_e2_4_claim_binds_the_runtime(t):
    n = create(t)
    k = ids.new_ulid()
    assert claim(t, n, key=k, extra={"X-Glassboard-Runtime": "a1f"}).status_code == 201
    r = claim(t, n, key=k, extra={"X-Glassboard-Runtime": "b77"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert claim(t, n, key=k, extra={"X-Glassboard-Runtime": "a1f"}).headers.get("glassboard-replayed") == "true"


# --- D1: the claim race ----------------------------------------------------------------------------

def test_d1_two_principals_claim_at_once(t):
    n = create(t)
    rec, results = t.rec(), {}

    def go(who):
        from fastapi.testclient import TestClient
        c = TestClient(t.app, raise_server_exceptions=False)
        results[who] = c.post(f"/api/v1/sessions/s1/tasks/{n}/claim", json={}, headers=t.headers(who, rec=rec))
    ts = [threading.Thread(target=go, args=(w,)) for w in ("jose/claude", "ana")]
    [x.start() for x in ts]
    [x.join() for x in ts]
    codes = sorted(r.status_code for r in results.values())
    assert codes == [201, 409]
    loser = [r for r in results.values() if r.status_code == 409][0]
    assert loser.json()["error"]["code"] == "task_already_claimed"
    assert sum(1 for e in t.events() if e["type"] == "task.claimed") == 1


# --- default return target and routing ----------------------------------------------------------

def test_o1_default_return_goes_to_open_assigned_to_the_previous_claimant(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "draft"})
    submitted = t.events()[-1]["seq"]
    r = api(t, "POST", f"/tasks/{n}/return", "jose", {"if_version": task(t, n)["version"],
                                                          "submission_seq": submitted,
                                                          "feedback": "Add the macOS steps."})
    assert r.status_code == 200 and r.json()["status"] == "open"
    row = task(t, n)
    assert row["assignee_id"] == t.pid("jose/claude") and row["claimant_id"] is None and row["fence_secret"] is None
    ev = t.events()[-1]
    assert ev["type"] == "task.returned" and ev["data"]["feedback"] == "Add the macOS steps."
    assert t.recipients("s1", ev["seq"]) == ["jose/claude"]
    assert claim(t, n, who="ana").json()["error"]["code"] == "task_not_claimable"
    assert claim(t, n, who="jose/claude").status_code == 201
    assert api(t, "POST", f"/tasks/{n}/return", "jose", {}).status_code == 422      # feedback is required


def test_return_rejects_a_stale_task_version_and_submission(t):
    n = create(t)
    fence = claim(t, n).json()["fence"]
    first = api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "first"}).json()
    first_version = first["version"]
    api(t, "POST", f"/tasks/{n}/return", "jose",
        {"if_version": first_version, "submission_seq": first["seq"], "feedback": "revise"})
    fence = claim(t, n).json()["fence"]
    second = api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "second"}).json()
    stale = api(t, "POST", f"/tasks/{n}/return", "jose",
                {"if_version": first_version, "submission_seq": first["seq"], "feedback": "old review"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    wrong_submission = api(t, "POST", f"/tasks/{n}/return", "jose",
                           {"if_version": second["version"], "submission_seq": first["seq"], "feedback": "old review"})
    assert wrong_submission.status_code == 409 and wrong_submission.json()["error"]["code"] == "submission_mismatch"
    assert task(t, n)["status"] == "review"


def test_task_event_routing(t):
    n = create(t, assignee="jose/claude")
    assert t.recipients("s1", t.head()) == ["jose/claude"]                     # task.created → assignee
    fence = claim(t, n).json()["fence"]
    assert t.recipients("s1", t.head()) == []                                  # task.claimed → nobody
    api(t, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "waiting for the CA"})
    assert t.recipients("s1", t.head()) == ["ana/codex", "jose"]               # task.blocked → coordinators
    api(t, "POST", f"/tasks/{n}/unblock", "jose/claude", {"fence": fence})
    api(t, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "done",
                                                          "refs": [{"label": "draft", "loc": "docs/onboarding.md@3f9e2c1",
                                                                    "access": "owner_only"}]})
    ev = t.events()[-1]
    assert ev["type"] == "task.submitted" and ev["data"]["refs"][0]["label"] == "draft"
    assert t.recipients("s1", ev["seq"]) == ["ana/codex", "jose"]
    api(t, "POST", f"/tasks/{n}/accept", "jose", {"if_version": task(t, n)["version"]})
    assert t.recipients("s1", t.head()) == ["jose/claude"]                     # task.accepted → claimant
    view = t.req("GET", f"/api/v1/sessions/s1/tasks/{n}?format=text", "ana", key=None).text
    assert view.startswith(f"--- T{n} · done ·") and "  · summary: «done»" in view
    assert_canonical(view)


def test_messages_may_name_existing_tasks(t):
    n = create(t)
    r = api(t, "POST", "/messages", "ana", {"kind": "question", "body": "scope?", "task": f"T{n}", "to": ["jose"]})
    assert r.status_code == 201 and t.events()[-1]["task_num"] == n
    assert api(t, "POST", "/messages", "ana", {"kind": "note", "body": "x", "task": "T99"}).status_code == 404


def test_create_task_validation_and_authority(t):
    assert api(t, "POST", "/tasks", "jose/claude", {"title": "t", "scope": "s", "deliverable": "d"}).status_code == 403
    assert api(t, "POST", "/tasks", "ana/codex", {"title": "t", "scope": "s", "deliverable": "d"}).status_code == 201  # orchestrator (E23)
    assert api(t, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d", "assignee": "nobody"}).status_code == 404
    for bad in ({"title": "", "scope": "s", "deliverable": "d"}, {"title": "t", "scope": "s", "deliverable": "d", "stale_after_s": 100},
                {"title": "t", "scope": "s", "deliverable": "d", "stale_after_s": 20000}, {"title": "t", "scope": "s"}):
        assert api(t, "POST", "/tasks", "jose", bad).status_code == 422, bad
    a, b = create(t), create(t)
    assert (a, b) == (2, 3)                                   # T1 was made by the orchestrator above


def test_task_list_filters(t):
    a = create(t, assignee="jose/claude")
    b = create(t)
    claim(t, b, who="ana")
    lst = lambda q="", who="jose": [x["task"] for x in t.req("GET", f"/api/v1/sessions/s1/tasks{q}", who, key=None).json()["tasks"]]
    assert lst() == [a, b] and lst("?status=in_progress") == [b] and lst("?assignee=jose/claude") == [a]
    assert lst("?mine=true", who="ana") == [b] and lst("?stale=true") == []


def test_at_8_1_task_rows(t):
    n = create(t)
    rows = [("claim", lambda: claim(t, n), 1), ("view", lambda: t.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None), 0),
            ("list", lambda: t.req("GET", "/api/v1/sessions/s1/tasks", "jose", key=None), 0)]
    for name, op, d in rows:
        h = t.head()
        op()
        assert t.head() - h == d, name


# --- CLI task commands ---------------------------------------------------------------------------

@pytest.fixture
def L(t, live_server):
    for who in ("jose", "jose/claude", "ana"):
        r = live_server.cli(who, "use", "s1")
        assert r.returncode == 0, r.stderr
    return live_server


def runtime_file(L, who):
    import json as _j
    import os as _o
    p = _o.path.join(L.env(who)["XDG_STATE_HOME"], "glassboard", "sessions", "s1", "runtime-norun.json")
    return _j.load(open(p)) if _o.path.exists(p) else {}


def ok(r):
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return r.stdout


def test_cli_task_lifecycle_never_prints_a_fence(L, t):
    out = ok(L.cli("jose", "task", "create", "Write onboarding", "--scope", "Install, tokens, TLS.",
                   "--deliverable", "docs/onboarding.md", "--assignee", "jose/claude"))
    assert out.startswith("T1 created · open")
    outs = [ok(L.cli("jose/claude", "task", "claim", "T1"))]
    assert "fence T1 g1 kept for this runtime" in outs[0]
    fence = runtime_file(L, "jose/claude")["fences"]["1"]
    assert fence.startswith("T1g1-")                                         # stored, never shown
    assert task(t, 1)["fence_secret"] == fence.split("-")[1]
    outs.append(ok(L.cli("jose/claude", "task", "note", "1", "Install section drafted")))
    outs.append(ok(L.cli("jose/claude", "task", "block", "1", "Waiting for the CA file")))
    outs.append(ok(L.cli("jose/claude", "task", "unblock", "1")))
    outs.append(ok(L.cli("jose/claude", "task", "submit", "1", "Draft complete", "--ref", "draft|docs/onboarding.md@3f9e2c1|owner_only")))
    assert runtime_file(L, "jose/claude")["fences"] == {}                      # the claim ended
    outs.append(ok(L.cli("jose/claude", "task", "show", "1")))
    outs.append(ok(L.cli("jose/claude", "task", "list")))
    outs.append(ok(L.cli("jose", "task", "return", "1", "Add the macOS steps")))
    outs.append(ok(L.cli("jose/claude", "task", "claim", "1")))
    outs.append(ok(L.cli("jose/claude", "task", "submit", "1", "Now with macOS")))
    outs.append(ok(L.cli("jose", "task", "accept", "1")))
    secret = fence.split("-")[1]
    for o in outs:
        assert secret not in o and not FENCE_RE.search(o), o
    assert task(t, 1)["status"] == "done"
    assert_canonical(L.cli("jose", "task", "show", "T1").stdout)


def test_cli_without_a_fence_points_to_reclaim(L, t):
    create(t)
    claim(t, 1)                                                     # claimed by a runtime this CLI is not
    r = L.cli("jose/claude", "task", "note", "1", "x")
    assert r.returncode == 2 and "no_fence" in r.stderr and "task reclaim T1" in r.stderr
    out = ok(L.cli("jose/claude", "task", "reclaim", "1", "lost my context"))
    assert "g2" in out and runtime_file(L, "jose/claude")["fences"]["1"].startswith("T1g2-")
    ok(L.cli("jose/claude", "task", "note", "1", "back on it"))


def test_cli_fence_mismatch_drops_the_fence(L, t):
    create(t)
    ok(L.cli("jose/claude", "task", "claim", "1"))
    ok(L.cli("jose", "task", "reassign", "1", "taking it over", "--to", "ana"))
    r = L.cli("jose/claude", "task", "note", "1", "still mine?")
    assert r.returncode == 5 and "fence_mismatch" in r.stderr and "Stop work on this task" in r.stderr
    assert "1" not in runtime_file(L, "jose/claude")["fences"]
    assert state_outbox(L, "jose/claude") == []                    # refused for good: not resent


def state_outbox(L, who):
    import os as _o
    p = _o.path.join(L.env(who)["XDG_STATE_HOME"], "glassboard", "sessions", "s1", "outbox.jsonl")
    return open(p).read().splitlines() if _o.path.exists(p) else []


def test_cli_offline_claim_gets_its_fence_when_flushed(L, t):
    create(t)
    L.stop()
    r = L.cli("jose/claude", "task", "claim", "1")
    assert r.returncode == 4 and "Queued in the outbox" in r.stderr
    assert runtime_file(L, "jose/claude").get("fences", {}) == {}
    L.start()
    ok(L.cli("jose/claude", "sync"))                                # any sending command flushes
    assert runtime_file(L, "jose/claude")["fences"]["1"].startswith("T1g1-")
    assert task(t, 1)["claimant_id"] == t.pid("jose/claude")
