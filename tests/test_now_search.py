"""ERRATA E42 and B45: the session Now page, tab badges, compact activity with an agent filter, the jump
list, and session-scoped full-text search (API, CLI, dashboard)."""
import re

from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import login
from test_render import assert_canonical
from test_tasks import api, claim


def search(gb, q, who="jose", fmt=False, s="s1"):
    r = gb.req("GET", f"/api/v1/sessions/{s}/search?q={q}" + ("&format=text" if fmt else ""), who, key=None)
    return r


# --- search ---------------------------------------------------------------------------------------------

def test_b45_search_finds_messages_titles_and_tasks_in_one_session(b):
    api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "The pagination cursor resets after a restore"})
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "Lunch at noon"})
    n = mk(b, "Fix pagination after restore")
    r = search(b, "pagination restore").json()
    assert [(e["type"], e["body"]) for e in r["events"]] == [("task.created", None),          # newest first; title match
                                                             ("note", "The pagination cursor resets after a restore")]
    assert [t["task"] for t in r["tasks"]] == [n]
    assert search(b, "paginat").json()["events"]                                # the last word matches as a prefix
    assert search(b, "pagination lunch").json()["events"] == []                # every word must appear
    text = search(b, "pagination", fmt=True).text
    assert_canonical(text)
    assert text.startswith("--- search · s1 · 1 task(s), 2 event(s)")          # the note and task.created (title)


def test_b45_search_input_cannot_break_the_query_or_echo(b):
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "alpha beta"})
    for q in ('"', "alpha OR", "NEAR(alpha", "*", "alpha^", "-beta", "a:b"):
        r = search(b, q)
        assert r.status_code == 200, (q, r.text)
    assert search(b, "%22%22%22").json()["events"] == []                       # only quotes: no words, no results
    r = search(b, "x" * 201)
    assert r.status_code == 422 and "xxxx" not in r.text


def test_b45_search_is_bound_to_the_session_and_members(b):
    b.session("s2", who="ana")
    b.req("POST", "/api/v1/sessions/s2/messages", "ana", json={"kind": "note", "body": "secret roadmap"}, rec=b.rec("s2"))
    assert search(b, "roadmap").json()["events"] == []                         # not in s1
    assert search(b, "roadmap", s="s2").status_code == 404                     # jose is not a member of s2
    assert search(b, "roadmap", who="ana", s="s2").json()["events"][0]["body"] == "secret roadmap"


def test_b45_message_drafts_are_not_indexed(b):
    r = b.req("POST", "/api/v1/sessions/s1/messages/preflight", "ana", json={"kind": "note", "body": "draftonly words"},
              rec=b.rec())
    assert r.status_code in (200, 201), r.text
    assert search(b, "draftonly").json()["events"] == []


def test_b45_search_cli_and_page(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "Rotate the staging certificate"))
    out = ok(L.cli("jose", "search", "staging", "certificate"))
    assert "--- search · s1 · 0 task(s), 1 event(s) ---" in out and "Rotate the staging certificate" in out
    c = login(gb, "jose")
    page = c.get("/s/s1/search?q=staging").text
    assert "Rotate the staging certificate" in page and 'class="ev-row"' in page


# --- Now page, badges, compact activity, jump list -------------------------------------------------------

def test_now_page_ranks_what_needs_you_with_an_action(b):
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which licence?", "to": ["jose"]}).json()["seq"]
    n = mk(b, "Pick licence")
    fence = claim(b, n).json()["fence"]
    api(b, "POST", f"/tasks/{n}/block", "jose/claude", {"fence": fence, "reason": "need it", "waiting_on": f"#{q}"})
    later = mk(b, "Header", assignee=None)
    v = b.req("GET", f"/api/v1/sessions/s1/tasks/{later}", "jose", key=None).json()["version"]
    api(b, "PATCH", f"/tasks/{later}", "jose", {"if_version": v, "depends_on": [n]})
    c = login(b, "jose")
    page = c.get("/s/s1/now").text
    assert "Do next" in page and "Owed by you · #%d from @jose/claude" % q in page
    assert f"#{q} question holds up 2 task(s)" in page and f'href="/s/s1/t/{n}"' not in page.split("Do next")[0]
    assert "Agents" in page and "actor=jose/claude&amp;density=compact" in page
    badges = re.search(r'Now <span class="tab-badge"[^>]*>(\d+)</span>', page)
    assert badges and int(badges.group(1)) >= 2
    assert re.search(r'Conversations <span class="tab-badge"[^>]*>1</span>', page)            # one question owed here
    assert 'href="/s/s1/now"' in c.get("/").text                                               # the front door


def test_compact_activity_filters_to_one_agent(b):
    api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "from claude"})
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "from ana"})
    c = login(b, "jose")
    page = c.get("/s/s1?f=all&actor=jose/claude&density=compact").text
    assert 'class="ev-row"' in page and "from claude" in page and "from ana" not in page
    assert "only @jose/claude" in page
    assert "from ana" in c.get("/s/s1?f=all&density=compact").text
    assert c.get("/s/s1?actor=../../etc").status_code == 200                   # a bad handle is ignored


def test_jump_list_is_text_for_members_only(b):
    mk(b, "Write the <b>FAQ</b>")
    c = login(b, "jose")
    j = c.get("/s/s1/jump.json").json()["items"]
    kinds = {i["kind"] for i in j}
    assert kinds == {"view", "task", "member"}
    assert any(i["label"].startswith("T1 Write the <b>FAQ</b>") and i["href"] == "/s/s1/t/1" for i in j)  # text, inserted as text
    b.session("s2", who="ana")
    assert c.get("/s/s2/jump.json").status_code == 404
    js = c.get("/static/keys.js").text
    assert "innerHTML" not in js and "textContent" in js
