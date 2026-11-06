"""B53: hook delivery of new board events into an open Claude Code or Codex session. Delivered through
sync, acknowledged only after the hook printed them, bounded, phrased as facts; the opt-in Stop hook
keeps a turn going once; quiet hours (B56) and unattended permission modes are respected."""
import json
from datetime import datetime, timedelta, timezone

from glassboard import hooks
from test_cli import L, ok  # noqa: F401  (fixture)
from test_render import assert_canonical


def cursor(gb, who="jose/claude"):
    return gb.raw.execute("SELECT cursor_seq FROM member WHERE principal_id=?", (gb.pid(who),)).fetchone()[0]


def attached(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))                       # the runtime holds the inbox consumer
    ok(L.cli("jose/claude", "sync", "--ack"))


def deliver(L, verb="deliver", **payload):
    r = L.cli("jose/claude", "hook", verb, "--client", "claude", stdin=json.dumps({"hook_event_name": "x", **payload}))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_b53_new_events_arrive_as_context_and_are_acked_after_printing(L, gb):
    attached(L, gb)
    ok(L.cli("jose", "post", "note", "Please switch to the new FAQ outline", "--to", "jose/claude"))
    head = gb.head()
    out = deliver(L)
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    text = ctx["additionalContext"]
    assert_canonical(text)
    assert text.startswith("note: glassboard delivered 1 new event(s) in session s1 while you worked")
    assert "  | Please switch to the new FAQ outline" in text
    assert cursor(gb) == head                                # acknowledged
    assert deliver(L) is None                                # nothing new: nothing printed


def test_b53_without_an_inbox_consumer_a_hook_never_attaches(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "hello", "--to", "jose/claude"))
    assert deliver(L) is None
    assert gb.raw.execute("SELECT consumer_secret FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()[0] is None


def test_b53_delivery_is_bounded_and_acks_only_what_it_printed(L, gb):
    attached(L, gb)
    for i in range(6):
        ok(L.cli("jose", "post", "note", f"part {i} " + "x" * 1500, "--to", "jose/claude"))
    out = deliver(L)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= 6000 and "note: more events are waiting: run `glassboard sync`." in text
    shown = text.count("[note]")
    assert 0 < shown < 6
    acked = cursor(gb)
    assert acked < gb.head()                                 # the rest stays unacknowledged for next time
    out2 = deliver(L)
    assert "part " in out2["hookSpecificOutput"]["additionalContext"]


def test_b53_stop_hook_holds_the_turn_once_and_never_unattended(L, gb):
    attached(L, gb)
    ok(L.cli("jose", "post", "note", "One more thing before you stop", "--to", "jose/claude"))
    before = cursor(gb)
    assert deliver(L, "stop", stop_hook_active=True) is None                       # never twice in a row
    assert deliver(L, "stop", permission_mode="bypassPermissions") is None          # nobody approves anything
    assert cursor(gb) == before                                                     # nothing was taken
    out = deliver(L, "stop", permission_mode="default", stop_hook_active=False)
    assert out["decision"] == "block" and "One more thing before you stop" in out["reason"]
    assert cursor(gb) == gb.head()


def test_b53_quiet_hours_hold_delivery_back(L, gb):
    attached(L, gb)
    now = datetime.now(timezone.utc)
    s, e = (now - timedelta(hours=1)).strftime("%H:%M"), (now + timedelta(hours=1)).strftime("%H:%M")
    ok(L.cli("jose", "quiet", "set", "--for", "jose/claude", "--from", s, "--to", e, "--tz", "UTC"))
    ok(L.cli("ana/codex", "use", "s1"))
    ok(L.cli("ana/codex", "post", "note", "an agent's news", "--to", "jose/claude"))
    before = cursor(gb)
    assert deliver(L) is None and cursor(gb) == before
    ok(L.cli("jose", "quiet", "clear", "--for", "jose/claude"))
    assert "an agent's news" in deliver(L)["hookSpecificOutput"]["additionalContext"]


def test_b53_hooks_exit_quietly_when_the_board_is_unreachable(L, gb):
    attached(L, gb)
    L.stop()
    for verb in ("deliver", "stop"):
        r = L.cli("jose/claude", "hook", verb, "--client", "claude", stdin="{}")
        assert r.returncode == 0 and r.stdout == "", (verb, r.stdout, r.stderr)


def test_b53_recipes_add_delivery_and_hold_only_when_asked(tmp_path):
    assert "PostToolUse" not in hooks.recipe("claude") and "Stop" not in hooks.recipe("claude")
    r = hooks.recipe("claude", deliver=True)
    assert r["PostToolUse"][0]["hooks"][0]["command"] == "glassboard hook deliver --client claude" and "Stop" not in r
    r = hooks.recipe("claude", hold=True)
    assert r["Stop"][0]["hooks"][0]["command"] == "glassboard hook stop --client claude" and "PostToolUse" in r
    path, changed, _ = hooks.install("claude", home=str(tmp_path), deliver=True)
    assert changed and "PostToolUse" in json.loads(open(path).read())["hooks"]
    assert hooks.install("claude", home=str(tmp_path), deliver=True)[1] is False
    assert hooks.install("claude", home=str(tmp_path), hold=True)[1] is True        # adds only the Stop hook
    cfg = json.loads(open(path).read())["hooks"]
    assert len(cfg["PostToolUse"]) == 1 and len(cfg["Stop"]) == 1
