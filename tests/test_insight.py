"""0.1.5 (ERRATA E41): biggest blockers and the verification strip. Recorded links only; nothing inferred
from timing; nothing changes state."""
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import login
from test_render import assert_canonical
from test_tasks import api, claim

SHA = "a" * 40


def blockers(gb, fmt=False):
    r = gb.req("GET", "/api/v1/sessions/s1/blockers" + ("?format=text" if fmt else ""), "jose", key=None)
    assert r.status_code == 200, r.text
    return r.text if fmt else r.json()


def edit(gb, n, **fields):
    v = gb.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()["version"]
    r = api(gb, "PATCH", f"/tasks/{n}", "jose", {"if_version": v, **fields})
    assert r.status_code == 200, r.text


# --- blockers -------------------------------------------------------------------------------------------

def test_blockers_follow_recorded_links_to_the_root(b):
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which licence?", "to": ["ana"]}).json()["seq"]
    t1, t2, t3 = mk(b, "Pick licence"), mk(b, "Write header", assignee=None), mk(b, "Ship", assignee=None)
    edit(b, t2, depends_on=[t1])
    edit(b, t3, depends_on=[t2])
    fence = claim(b, t1).json()["fence"]
    api(b, "POST", f"/tasks/{t1}/block", "jose/claude", {"fence": fence, "reason": "need an answer", "waiting_on": f"#{q}"})
    t4 = mk(b, "Review copy", assignee="ana/codex")
    f4 = claim(b, t4, who="ana/codex").json()["fence"]
    api(b, "POST", f"/tasks/{t4}/block", "ana/codex", {"fence": f4, "reason": "ask ana", "waiting_on": "@ana"})
    got = blockers(b)
    top = got["blockers"][0]
    assert (top["kind"], top["seq"], top["owed_by"]) == ("question", q, ["ana"])
    assert [h["task"] for h in top["holds"]] == [t1, t2, t3]                     # through two dependencies
    member = next(i for i in got["blockers"] if i["kind"] == "member")
    assert member["handle"] == "ana" and [h["task"] for h in member["holds"]] == [t4]
    text = blockers(b, fmt=True)
    assert_canonical(text)
    assert f"--- #{q} question owed by @ana · holds up 3 task(s) ---" in text
    assert f"  · holds up: «T{t1}, T{t2}, T{t3}»" in text


def test_an_unfinished_task_is_a_root_and_a_resolved_cause_shows_a_stale_block(b):
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Go?", "to": ["ana"]}).json()["seq"]
    t1, t2 = mk(b, "Base"), mk(b, "On top", assignee=None)
    edit(b, t2, depends_on=[t1])
    fence = claim(b, t1).json()["fence"]
    root = blockers(b)["blockers"][0]
    assert (root["kind"], root["task"], root["claimant"], [h["task"] for h in root["holds"]]) == ("task", t1, "jose/claude", [t2])
    api(b, "POST", f"/tasks/{t1}/block", "jose/claude", {"fence": fence, "reason": "wait", "waiting_on": f"#{q}"})
    api(b, "POST", "/messages", "ana", {"kind": "answer", "body": "Yes", "re": q})
    root = blockers(b)["blockers"][0]
    assert root["kind"] == "stale_block" and root["task"] == t1 and [h["task"] for h in root["holds"]] == [t2]
    assert f"--- T{t1} is still marked blocked though what it waited on is resolved ---" in blockers(b, fmt=True)


def test_nothing_waiting_means_no_blockers_and_reading_changes_nothing(b):
    mk(b)
    head = b.head()
    assert blockers(b) == {"session": "s1", "count": 0, "blockers": [], "more": 0}
    assert "nothing is waiting on anything" in blockers(b, fmt=True)
    assert b.head() == head


def test_blockers_cli_and_board_card(L, gb):
    ok(L.cli("jose", "use", "s1"))
    q = int(ok(L.cli("jose/claude", "post", "question", "Which port?", "--to", "ana", "-s", "s1")).split("#")[1].split()[0])
    out = ok(L.cli("jose", "task", "create", "Open the port", "--scope", "s", "--deliverable", "d", "--assignee", "jose/claude"))
    num = out.split()[0]
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "task", "claim", num))
    ok(L.cli("jose/claude", "task", "block", num, "need the port", "--waiting-on", f"#{q}"))
    assert f"--- #{q} question owed by @ana · holds up 1 task(s) ---" in ok(L.cli("jose", "blockers"))
    c = login(gb, "jose")
    page = c.get("/s/s1/board").text
    assert "Biggest blockers" in page and f"#{q} question" in page


# --- verification ---------------------------------------------------------------------------------------

def submit(gb, n, fence, tests=("pass",), commit=SHA):
    body = {"fence": fence, "summary": "Wrote the page; see docs/faq.md",
            "tests": [{"command": "pytest -q", "outcome": o} for o in tests]}
    if commit:
        body["commit"] = commit
    r = api(gb, "POST", f"/tasks/{n}/submit", "jose/claude", body)
    assert r.status_code == 200, r.text
    return r.json()["seq"]


def verification(gb, n):
    return gb.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()["verification"]


def accept(gb, n):
    v = gb.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()["version"]
    r = api(gb, "POST", f"/tasks/{n}/accept", "jose", {"if_version": v})
    assert r.status_code == 200, r.text


def test_a_reviewed_and_accepted_submission_is_verified(b):
    n = mk(b)
    edit(b, n, reviewer="ana/codex")
    seq = submit(b, n, claim(b, n).json()["fence"])
    assert verification(b, n)["gaps"] == ["waiting for @ana/codex to review"]
    api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "approve", "submission_seq": seq, "commit": SHA})
    accept(b, n)
    v = verification(b, n)
    assert v["verified"] is True and v["gaps"] == []
    assert v["steps"]["submitted"] == {"seq": seq, "commit": SHA, "tests": {"pass": 1, "fail": 0, "skipped": 0}}
    assert v["steps"]["reviewed"]["by"] == "ana/codex" and v["steps"]["accepted"]["by"] == "jose"
    text = b.req("GET", f"/api/v1/sessions/s1/tasks/{n}?format=text", "jose", key=None).text
    assert_canonical(text)
    assert "--- verification: verified ---" in text


def test_gaps_are_named(b):
    n = mk(b)
    submit(b, n, claim(b, n).json()["fence"], tests=("pass", "fail"), commit=None)
    accept(b, n)
    v = verification(b, n)
    assert v["verified"] is False
    assert v["gaps"] == ["no commit recorded", "1 test(s) failed", "no reviewer named",
                         "accepted without an approving review of this submission"]
    assert "--- verification: gaps: no commit recorded; 1 test(s) failed;" in \
        b.req("GET", f"/api/v1/sessions/s1/tasks/{n}?format=text", "jose", key=None).text


def test_a_review_of_an_earlier_submission_does_not_count(b):
    n = mk(b)
    edit(b, n, reviewer="ana/codex")
    first = submit(b, n, claim(b, n).json()["fence"])
    api(b, "POST", f"/tasks/{n}/review", "ana/codex", {"verdict": "approve", "submission_seq": first, "commit": SHA})
    v = b.req("GET", f"/api/v1/sessions/s1/tasks/{n}", "jose", key=None).json()["version"]
    api(b, "POST", f"/tasks/{n}/return", "jose", {"if_version": v, "submission_seq": first, "feedback": "one more pass"})
    submit(b, n, claim(b, n).json()["fence"], commit="b" * 40)
    gaps = verification(b, n)["gaps"]
    assert "reviewed only an earlier submission" in gaps


def test_nothing_submitted_shows_no_strip_and_the_page_renders_it(b):
    n = mk(b)
    assert verification(b, n) is None
    seq = submit(b, n, claim(b, n).json()["fence"])
    c = login(b, "jose")
    page = c.get(f"/s/s1/t/{n}").text
    assert "Verification" in page and f'href="#e{seq}"' in page and "no reviewer named" in page
