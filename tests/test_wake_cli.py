"""`glassboard wake …` (ERRATA E30, decision 0035): status, done, supervisor control and the
human-armed supervisor itself, driven end to end with the script adapter against a live board."""
import os
import subprocess
import sys
import time

import pytest

from glassboard import cli, wakerun
from test_render import assert_canonical


@pytest.fixture
def L(gb, live_server):
    gb.session("s1")
    for h in ("ana", "jose/claude", "ana/codex"):
        gb.add("s1", h)
    for who in ("jose", "jose/claude"):
        r = live_server.cli(who, "use", "s1")
        assert r.returncode == 0, r.stderr
    return live_server


def ok(r):
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return r.stdout


def types(gb):
    return [e["type"] for e in gb.events() if e["type"].startswith("wake.")]


def script(tmp_path, body):
    path = tmp_path / "agent.py"
    path.write_text(f"#!{sys.executable}\nimport subprocess, sys\n"
                    f"def gb(*a):\n    return subprocess.run([sys.executable, '-m', 'glassboard.cli', *a], check=True)\n{body}")
    path.chmod(0o700)
    return str(path)


def supervise(L, *extra, timeout=60):
    return subprocess.Popen([sys.executable, "-m", "glassboard.cli", "wake", "run", "--client", "script",
                             "--agent-runtime", "agent-one", "--debounce", "0", *extra],
                            env=L.env("jose/claude"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def until(cond, timeout=30):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_status_is_canonical_and_shows_pending_demand(L, gb):
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    out = ok(L.cli("jose/claude", "wake", "status"))
    assert_canonical(out)
    assert "· pending ---" in out and "--- pending: message 1 ---" in out and "supervisor: none armed" in out


def test_done_records_the_generation_from_the_environment_or_the_option(L, gb):
    seq = int(ok(L.cli("jose", "post", "note", "one", "--to", "jose/claude")).split("#")[1].split()[0])
    r = L.cli("jose/claude", "wake", "done")
    assert r.returncode == 2 and "--generation" in r.stderr
    assert f"inspected generation {seq}" in ok(L.cli("jose/claude", "wake", "done", extra_env={wakerun.GENERATION_ENV: str(seq)}))
    assert "already recorded" in ok(L.cli("jose/claude", "wake", "done", "--generation", str(seq)))
    assert types(gb) == ["wake.inspected"]


def test_arm_keeps_the_fence_private_and_stop_releases_it(L, gb):
    out = ok(L.cli("jose/claude", "wake", "arm", "--client", "claude"))
    assert "armed claude · fence W1 kept in this runtime" in out and "W1-" not in out
    ok(L.cli("jose/claude", "wake", "stop", "done for today"))
    r = L.cli("jose/claude", "wake", "stop", "again")
    assert r.returncode == 2 and "no_wake_fence" in r.stderr
    assert types(gb) == ["wake.armed", "wake.stopped"]


def test_escalating_client_flags_are_refused(L, gb):
    r = L.cli("jose/claude", "wake", "run", "--client", "claude", "--session-id", "abc", "--agent-runtime", "agent-one",
              "--", "--dangerously-skip-permissions")
    assert r.returncode == 2 and "refused_flag" in r.stderr
    assert types(gb) == []


def test_supervisor_dispatches_once_and_the_agent_records_its_inspection(L, gb, tmp_path):
    agent = script(tmp_path, "gb('post', 'update', 'Checked the board; nothing blocks me.')\ngb('wake', 'done')\n")
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    p = supervise(L, "--script", agent, "--max-dispatches", "1")
    out, err = p.communicate(timeout=90)
    assert p.returncode == 0, err
    assert types(gb) == ["wake.armed", "wake.dispatched", "wake.inspected", "wake.stopped"]
    stopped = [e for e in gb.events() if e["type"] == "wake.stopped"][0]
    assert stopped["data"]["reason"] == "dispatch limit reached (1)"
    update = [e for e in gb.events() if e["type"] == "update"][0]
    assert update["runtime"] == "agent-one"                          # the agent ran under its own label
    dispatched = [e for e in gb.events() if e["type"] == "wake.dispatched"][0]
    assert dispatched["runtime"] == "w-agent-one"                    # the supervisor under its own
    assert "W1-" not in out + err


def test_a_run_without_an_inspection_pauses_until_a_human_acts(L, gb, tmp_path):
    agent = script(tmp_path, "pass\n")                               # exits without `wake done`
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    p = supervise(L, "--script", agent)
    until(lambda: "wake.paused" in types(gb))
    paused = [e for e in gb.events() if e["type"] == "wake.paused"][0]
    assert paused["data"]["reason"].startswith("client exited (code 0) without recording an inspection")
    assert gb.recipients("s1", paused["seq"]) == ["jose"]
    ok(L.cli("jose/claude", "wake", "stop", "human checked it", extra_env={"GLASSBOARD_RUNTIME": "w-agent-one"}))
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0 and "stopped while paused" in err


def test_wakes_without_progress_stop_the_supervisor(L, gb, tmp_path):
    agent = script(tmp_path, "gb('wake', 'done')\n")                  # inspects, but does no work
    p = supervise(L, "--script", agent, "--idle-stops", "1")
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    out, err = p.communicate(timeout=90)
    assert p.returncode == 0, err
    stopped = [e for e in gb.events() if e["type"] == "wake.stopped"][0]
    assert stopped["data"]["reason"] == "1 wakes in a row without progress"


def test_a_second_supervisor_on_this_machine_is_refused(L, gb, tmp_path):
    agent = script(tmp_path, "pass\n")
    p = supervise(L, "--script", agent)
    until(lambda: "wake.armed" in types(gb))
    r = L.cli("jose/claude", "wake", "run", "--client", "script", "--script", agent, "--agent-runtime", "agent-one")
    assert r.returncode == 2 and "supervisor_running" in r.stderr
    p.terminate()
    p.communicate(timeout=30)


def test_adapters_build_fixed_commands_with_the_exact_session_id():
    wakerun.bind(cli)
    argv, stdin = wakerun.command("codex", "0199-abc", wakerun.PROMPT, [], None, None, None)
    assert argv == ["codex", "exec", "resume", "0199-abc", "-"] and stdin == wakerun.PROMPT
    argv, stdin = wakerun.command("claude", "abc", wakerun.PROMPT, ["--allowedTools", "Bash(glassboard *)"], 5, None, None)
    assert argv[:4] == ["claude", "-p", "--resume", "abc"] and "--max-turns" in argv
    assert wakerun.PROMPT not in argv and stdin == wakerun.PROMPT      # variadic flags cannot swallow it
    argv, _ = wakerun.command("opencode", "ses_1", wakerun.PROMPT, [], None, "http://127.0.0.1:4096", None)
    assert argv[:4] == ["opencode", "run", "-s", "ses_1"] and "--attach" in argv
    with pytest.raises(cli.CliError):
        wakerun.command("grok", "a b; rm", wakerun.PROMPT, [], None, None, None)


def test_refused_flags_depend_on_the_client():
    wakerun.bind(cli)
    ok_cases = [("grok", ["--permission-mode", "dontAsk", "--allow", "Bash(glassboard *)"]),
                ("claude", ["--permission-mode=default", "--allowedTools", "Bash(glassboard *)"]),
                ("codex", ["-c", "sandbox_workspace_write.network_access=true", "--skip-git-repo-check"])]
    for client, extra in ok_cases:
        wakerun.check_extra(client, extra)
    refused = [("grok", ["--permission-mode", "bypassPermissions"]), ("claude", ["--permission-mode=acceptEdits"]),
               ("codex", ["-c", 'sandbox_mode="danger-full-access"']), ("codex", ["-c", "approval_policy=never"]),
               ("claude", ["-c"]), ("opencode", ["-s", "other"]), ("grok", ["--resume", "x"]),
               ("codex", ["--dangerously-bypass-approvals-and-sandbox"]), ("opencode", ["--fork"])]
    for client, extra in refused:
        with pytest.raises(cli.CliError) as e:
            wakerun.check_extra(client, extra)
        assert e.value.kind == "refused_flag", (client, extra)


def test_a_read_only_state_directory_is_a_clean_error_not_a_traceback(L, tmp_path):
    ro = tmp_path / "ro-state"
    ro.mkdir(mode=0o500)
    r = L.cli("jose/claude", "use", "s1", extra_env={"XDG_STATE_HOME": str(ro)})
    assert r.returncode == cli.EXIT_CONFIG and "state_unwritable" in r.stderr
    assert "Traceback" not in r.stderr


def test_repeated_to_adds_recipients_instead_of_keeping_the_last(L, gb):
    out = ok(L.cli("jose", "post", "note", "both of you", "--to", "jose/claude", "--to", "ana/codex"))
    seq = int(out.split("#")[1].split()[0])
    assert gb.recipients("s1", seq) == ["ana/codex", "jose/claude"]


def test_a_human_resume_continues_the_same_supervisor(L, gb, tmp_path):
    agent = script(tmp_path, "pass\n")                               # exits without `wake done`
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    p = supervise(L, "--script", agent)
    sup = {"GLASSBOARD_RUNTIME": "w-agent-one"}
    until(lambda: types(gb).count("wake.paused") == 1)
    ok(L.cli("jose/claude", "wake", "resume", "human looked", extra_env=sup))
    until(lambda: types(gb).count("wake.paused") == 2)               # resumed: dispatched again, paused again
    assert types(gb) == ["wake.armed", "wake.dispatched", "wake.paused", "wake.resumed",
                         "wake.dispatched", "wake.paused"]
    ok(L.cli("jose/claude", "wake", "stop", "done", extra_env=sup))
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0 and "supervisor_running" not in err, err


def test_a_human_stop_while_armed_ends_the_supervisor_cleanly(L, gb, tmp_path):
    agent = script(tmp_path, "pass\n")
    p = supervise(L, "--script", agent)
    until(lambda: "wake.armed" in types(gb))
    ok(L.cli("jose/claude", "wake", "stop", "human: finished", extra_env={"GLASSBOARD_RUNTIME": "w-agent-one"}))
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    assert "fence was already released" in err and "Traceback" not in err
    assert types(gb) == ["wake.armed", "wake.stopped"]


# --- live-session evidence and holds (ERRATA E31) ------------------------------------------------

def launcher(L, who, *cmd):
    return subprocess.Popen([sys.executable, "-m", "glassboard.cli", "run", "--", *cmd], env=L.env(who),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def held(gb):
    return [e for e in gb.events() if e["type"] == "wake.held"]


def test_done_without_a_generation_records_the_outstanding_dispatch(L, gb, tmp_path):
    agent = script(tmp_path, "import os\nos.environ.pop('GLASSBOARD_WAKE_GENERATION')\n"   # as in a pushed turn
                             "gb('post', 'update', 'Looked.')\ngb('wake', 'done')\n")
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    p = supervise(L, "--script", agent, "--max-dispatches", "1")
    out, err = p.communicate(timeout=90)
    assert p.returncode == 0, err
    assert types(gb) == ["wake.armed", "wake.dispatched", "wake.inspected", "wake.stopped"]
    r = L.cli("jose/claude", "wake", "done")                         # nothing outstanding now
    assert r.returncode == cli.EXIT_USAGE and "No wake dispatch is outstanding" in r.stderr


def test_a_client_open_through_the_launcher_holds_the_wake_until_it_exits(L, gb, tmp_path):
    agent = script(tmp_path, "gb('post', 'update', 'Looked.')\ngb('wake', 'done')\n")
    tui = launcher(L, "jose/claude", sys.executable, "-c", "import time; time.sleep(120)")
    try:
        until(lambda: tui.poll() is None and (tmp_path / "home-jose-claude" / "state" / "glassboard" / "sessions"
                                              / "live.lock").exists())
        p = supervise(L, "--script", agent, "--max-dispatches", "1", "--recheck", "1")
        ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
        until(lambda: held(gb))
        assert held(gb)[0]["data"]["reason"] == "open through `glassboard run` or `glassboard join` on this machine"
        assert "wake.dispatched" not in types(gb)
        time.sleep(3)
        assert len(held(gb)) == 1                                    # rechecks write nothing
        out = ok(L.cli("jose/claude", "wake", "status"))
        assert "--- wake held · #" in out and "  · reason: «open through" in out
    finally:
        tui.kill()
        tui.communicate(timeout=30)
    out, err = p.communicate(timeout=90)
    assert p.returncode == 0, err
    assert types(gb) == ["wake.armed", "wake.held", "wake.dispatched", "wake.inspected", "wake.stopped"]


def test_a_launcher_waits_while_a_wake_run_uses_the_agent(L, tmp_path):
    import fcntl
    L.env("jose/claude")
    lock = tmp_path / "home-jose-claude" / "state" / "glassboard" / "sessions" / "live.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                                   # as the supervisor does around a run
    tui = launcher(L, "jose/claude", "true")
    try:
        time.sleep(2)
        assert tui.poll() is None
    finally:
        os.close(fd)
    out, err = tui.communicate(timeout=30)
    assert tui.returncode == 0 and "a wake run is using this agent now" in err


def test_a_client_without_an_open_session_signal_is_held_unless_the_human_attests(L, gb):
    sup = {"GLASSBOARD_RUNTIME": "w-agent-one"}
    p = subprocess.Popen([sys.executable, "-m", "glassboard.cli", "wake", "run", "--client", "opencode",
                          "--session-id", "ses_abc", "--agent-runtime", "agent-one", "--debounce", "0"],
                         env=L.env("jose/claude"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ok(L.cli("jose", "post", "note", "please look", "--to", "jose/claude"))
    until(lambda: held(gb))
    assert held(gb)[0]["data"]["reason"].startswith("opencode gives no open-session signal; restart the supervisor "
                                                    "with --headless-only")
    ok(L.cli("jose/claude", "wake", "stop", "human: will attest", extra_env=sup))
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    assert "wake.dispatched" not in types(gb)


def test_native_signals_read_open_closed_or_unknown(tmp_path, monkeypatch):
    import fcntl
    import json as _json
    from glassboard import livesession as ls
    for var, d in (("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex"), ("HOME", "home")):
        monkeypatch.setenv(var, str(tmp_path / d))
    dead = subprocess.Popen(["true"])
    dead.wait()                                                      # a reaped PID: no such process
    me = os.getpid()
    assert ls.native("claude", "s1")[0] == ls.UNKNOWN
    (tmp_path / "claude" / "sessions").mkdir(parents=True)
    assert ls.native("claude", "s1")[0] == ls.CLOSED
    (tmp_path / "claude" / "sessions" / "1.json").write_text(_json.dumps({"pid": dead.pid, "sessionId": "s1"}))
    assert ls.native("claude", "s1")[0] == ls.CLOSED                 # a dead process's leftover record
    (tmp_path / "claude" / "sessions" / "2.json").write_text(_json.dumps({"pid": me, "sessionId": "s1"}))
    assert ls.native("claude", "s1") == (ls.OPEN, f"open in Claude Code (process {me})")
    assert ls.native("claude", "s2")[0] == ls.CLOSED
    (tmp_path / "claude" / "sessions" / "3.json").write_text("{torn")
    assert ls.native("claude", "s2")[0] == ls.UNKNOWN                # unreadable: never "closed"

    assert ls.native("codex", "t1")[0] == ls.UNKNOWN
    (tmp_path / "codex" / "thread-writer-locks").mkdir(parents=True)
    assert ls.native("codex", "t1")[0] == ls.CLOSED
    lock = tmp_path / "codex" / "thread-writer-locks" / "t1.lock"
    lock.write_text("")
    assert ls.native("codex", "t1")[0] == ls.CLOSED                  # the file outlives its holder
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert ls.native("codex", "t1") == (ls.OPEN, f"loaded in Codex (process {me})")
    finally:
        os.close(fd)

    assert ls.native("grok", "g1")[0] == ls.UNKNOWN
    (tmp_path / "home" / ".grok").mkdir(parents=True)
    rows = tmp_path / "home" / ".grok" / "active_sessions.json"
    rows.write_text(_json.dumps([{"session_id": "g1", "pid": dead.pid}]))
    assert ls.native("grok", "g1")[0] == ls.CLOSED
    rows.write_text(_json.dumps([{"session_id": "g1", "pid": me}]))
    assert ls.native("grok", "g1")[0] == ls.OPEN
    rows.write_text(_json.dumps({"sessions": []}))
    assert ls.native("grok", "g1")[0] == ls.UNKNOWN

    assert ls.native("script", None)[0] == ls.CLOSED
    assert ls.may_resume("opencode", "x", headless_only=False)[0] is False
    assert ls.may_resume("opencode", "x", headless_only=True)[0] is True
    rows.write_text(_json.dumps([{"session_id": "g1", "pid": me}]))
    assert ls.may_resume("grok", "g1", headless_only=True)[0] is False   # an open signal beats the attestation


def test_wake_commands_load_without_fcntl_and_run_refuses_windows():
    """Windows has no fcntl: every wake command but `run` must still work, and `run` refuses cleanly."""
    code = ("import os, sys, argparse\n"
            "from glassboard import cli\n"                  # its lock module picks msvcrt on Windows
            "sys.modules['fcntl'] = None\n"
            "from glassboard import wakerun\n"
            "os.name = 'nt'\n"
            "try:\n"
            "    wakerun.main(argparse.Namespace(action='run'), cli)\n"
            "except cli.CliError as e:\n"
            "    print(e.kind, e.code)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["unsupported_platform", str(cli.EXIT_USAGE)]
