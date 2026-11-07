"""B72: history never silently shows only a window (ERRATA E47).
API JSON marks truncation explicitly and gives the cursor for the rest; text ends with one board-authored
`next: partial` Section line; bare `glassboard history` shows the latest N and says how to page."""
import json

import pytest

from glassboard import render
from test_render import assert_canonical


@pytest.fixture
def m(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    for i in range(6):
        r = gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "note", "body": f"n{i}"}, rec=gb.rec())
        assert r.status_code == 201, r.text
    return gb


def get(gb, qs):
    r = gb.req("GET", f"/api/v1/sessions/s1/events?{qs}", "jose", key=None)
    assert r.status_code == 200, r.text
    return r


def test_b72_api_complete_window_is_not_truncated(m):
    j = get(m, "limit=100").json()
    assert j["truncated"] is False and j["more"] is False and j["more_before"] is False and j["more_after"] is False
    assert j["total"] == j["shown"] == len(j["events"]) == m.head()
    assert j["next_after"] is None and j["next_before"] is None
    text = get(m, "limit=100&format=text").text
    assert "partial" not in text


def test_b72_api_oldest_window_marks_truncation_and_the_cursor(m):
    head = m.head()
    j = get(m, "limit=3").json()
    assert [e["seq"] for e in j["events"]] == [1, 2, 3]
    assert j["truncated"] is True and j["window"] == "oldest" and j["total"] == head and j["shown"] == 3
    assert j["more"] is True and j["more_after"] is True and j["next_after"] == 3       # old fields keep their meaning
    assert j["more_before"] is False and j["next_before"] is None and (j["first"], j["last"]) == (1, 3)
    text = get(m, "limit=3&format=text").text
    assert_canonical(text)
    last = text.rstrip("\n").split("\n")[-1]
    assert last == f"next: partial: showing the first 3 of {head} events (#1-#3); later: `after=3`"
    assert render.PARTIAL_RE.match(last)


def test_b72_api_latest_window_pages_backwards_to_the_start(m):
    head = m.head()
    j = get(m, "limit=3&latest=true").json()
    assert [e["seq"] for e in j["events"]] == [head - 2, head - 1, head]                # still oldest first
    assert j["window"] == "latest" and j["truncated"] is True and j["more_before"] is True
    assert j["next_before"] == head - 2 and j["more"] is False and j["next_after"] is None
    seen, before = [e["seq"] for e in j["events"]], j["next_before"]
    while before is not None:
        page = get(m, f"limit=3&latest=true&before={before}").json()
        seen = [e["seq"] for e in page["events"]] + seen
        before = page["next_before"]
    assert seen == list(range(1, head + 1))                                           # nothing missed
    text = get(m, "limit=3&latest=true&format=text").text
    assert_canonical(text)
    assert text.rstrip("\n").split("\n")[-1] == (
        f"next: partial: showing the latest 3 of {head} events (#{head - 2}-#{head}); earlier: `before={head - 2}`")


def test_b72_total_counts_only_matching_events(m):
    j = get(m, "type=note&limit=2&latest=true").json()
    assert j["total"] == 6 and j["shown"] == 2 and j["truncated"] is True


def test_b72_partial_line_rewording_is_whole_line_only():
    line = "next: partial: showing the latest 3 of 9 events (#7-#9); earlier: `before=7`"
    assert render.reword_partial(line, "cli") == (
        "next: partial: showing the latest 3 of 9 events (#7-#9); earlier: `glassboard history --before 7` "
        "(repeat your other options; --limit up to 100)")
    assert "call board_history with before=7" in render.reword_partial(line, "mcp")
    forged = "  | " + line                                                               # participant Block line
    assert render.reword_partial(forged, "cli") == forged
    assert render.reword_partial(line + " x", "cli") == line + " x"


# --- the CLI ---------------------------------------------------------------------------------

@pytest.fixture
def L(m, live_server):
    return live_server


def test_b72_bare_glassboard_history_says_it_shows_only_the_latest_n(L, m):
    head = m.head()
    r = L.cli("jose", "--session", "s1", "history", "--limit", "3")
    assert r.returncode == 0, r.stderr
    assert_canonical(r.stdout)
    assert f"  | n5" in r.stdout and "  | n2" not in r.stdout                           # the latest window
    assert r.stdout.rstrip("\n").split("\n")[-1] == (
        f"next: partial: showing the latest 3 of {head} events (#{head - 2}-#{head}); "
        f"earlier: `glassboard history --before {head - 2}` (repeat your other options; --limit up to 100)")
    r = L.cli("jose", "--session", "s1", "history", "--after", "0", "--limit", "3")
    assert r.stdout.rstrip("\n").split("\n")[-1].endswith("later: `glassboard history --after 3` "
                                                         "(repeat your other options; --limit up to 100)")
    r = L.cli("jose", "--session", "s1", "history", "--before", str(head - 2), "--limit", "3")
    assert f"(#{head - 5}-#{head - 3})" in r.stdout


def test_b72_complete_cli_history_says_nothing(L, m):
    r = L.cli("jose", "--session", "s1", "history")
    assert r.returncode == 0 and "partial" not in r.stdout and r.stderr == ""
    r = L.cli("jose", "--session", "s1", "history", "--json")
    assert json.loads(r.stdout)["truncated"] is False and r.stderr == ""


def test_b72_cli_json_keeps_stdout_pure_and_notes_truncation_on_stderr(L, m):
    head = m.head()
    r = L.cli("jose", "--session", "s1", "history", "--limit", "2", "--json")
    j = json.loads(r.stdout)
    assert j["truncated"] is True and j["window"] == "latest"
    assert f"showing the latest 2 of {head} events" in r.stderr and f"--before {head - 1}" in r.stderr
