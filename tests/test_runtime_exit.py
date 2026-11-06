"""B37 (ERRATA E34): a client that exits while its runtime holds work is reported, never acted on."""
import sys

from test_cli import L, ok  # noqa: F401  (fixture)


def _claim(gb, who="jose/claude", runtime="rt1"):
    gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
           json={"title": "Write docs", "scope": "s", "deliverable": "d", "assignee": who}, rec=gb.rec())
    r = gb.req("POST", "/api/v1/sessions/s1/tasks/1/claim", who, json={}, rec=gb.rec(),
               extra={"X-Glassboard-Runtime": runtime})
    assert r.status_code == 201, r.text
    return r.json()


def _exited(gb, who="jose/claude", runtime="rt1", **body):
    return gb.req("POST", "/api/v1/sessions/s1/runtime/exited", who, json=body or {"exit_code": 1}, rec=gb.rec(),
                  extra={"X-Glassboard-Runtime": runtime} if runtime else {})


def test_b37_exit_with_a_claim_is_one_visible_event_and_an_attention_item(L, gb):  # noqa: F811
    _claim(gb)
    r = _exited(gb, exit_code=3)
    assert r.status_code == 200, r.text
    ev = gb.events()[-1]
    assert ev["type"] == "runtime.exited"
    assert ev["data"] == {"exited": "exit code 3", "holding": "claims T1 g1"}
    task = gb.raw.execute("SELECT status, fence_secret FROM task WHERE num=1").fetchone()
    assert task["status"] == "in_progress" and task["fence_secret"]           # nothing released
    items = gb.req("GET", "/api/v1/me/attention", "jose").json()["items"]
    assert any(i["kind"] == "runtime_exited" and i["from"] == "jose/claude" and "T1" in i["text"] for i in items)
    text = gb.req("GET", "/api/v1/sessions/s1/events?format=text", "jose").text
    assert "[runtime.exited]" in text and "  · exited: «exit code 3»" in text and "  · holding: «claims T1 g1»" in text


def test_b37_exit_without_holdings_writes_nothing(L, gb):  # noqa: F811
    before = len(gb.events())
    r = _exited(gb, runtime="idle1")
    assert r.status_code == 200 and r.json()["seq"] is None
    assert len(gb.events()) == before


def test_b37_needs_a_runtime_label(L, gb):  # noqa: F811
    assert _exited(gb, runtime=None).status_code == 422


def test_b37_glassboard_run_reports_the_exit_and_keeps_the_status(L, gb):  # noqa: F811
    gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
           json={"title": "Write docs", "scope": "s", "deliverable": "d", "assignee": "jose/claude"}, rec=gb.rec())
    ok(L.cli("jose/claude", "use", "s1"))
    client = (f"import subprocess, sys; c = [sys.executable, '-m', 'glassboard.cli'];"
              f"subprocess.run(c + ['sync'], check=True); subprocess.run(c + ['task', 'claim', 'T1'], check=True);"
              f"sys.exit(5)")
    r = L.cli("jose/claude", "run", "--", sys.executable, "-c", client)
    assert r.returncode == 5, (r.stdout, r.stderr)
    assert "exited while holding work" in r.stderr
    ev = [e for e in gb.events() if e["type"] == "runtime.exited"][-1]
    assert ev["data"]["exited"] == "exit code 5" and "T1" in ev["data"]["holding"] and "inbox consumer" in ev["data"]["holding"]


def test_b37_a_signal_is_reported_as_a_signal(L, gb):  # noqa: F811
    ok(L.cli("jose/claude", "use", "s1"))
    client = (f"import os, signal, subprocess, sys; c = [sys.executable, '-m', 'glassboard.cli'];"
              f"subprocess.run(c + ['sync'], check=True); os.kill(os.getpid(), signal.SIGTERM)")
    r = L.cli("jose/claude", "run", "--", sys.executable, "-c", client)
    assert r.returncode == 143, (r.stdout, r.stderr)
    ev = [e for e in gb.events() if e["type"] == "runtime.exited"][-1]
    assert ev["data"]["exited"] == "signal 15"
