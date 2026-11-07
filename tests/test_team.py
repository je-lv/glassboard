"""Team start scripts (B60) that turn on event delivery and offer the wake supervisor (B73), ERRATA E49."""
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

import glassboard
from glassboard import cli, team, wakerun
from test_board import b  # noqa: F401  (fixture)

SRC = str(pathlib.Path(glassboard.__file__).resolve().parents[1])
ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE = re.compile(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}")          # how a join code is shown


def _shows_a_code(gb, text: str) -> bool:
    """True when any 12-character base32 run in `text` is one of the board's join codes (by its hash)."""
    from glassboard.api_join import _hash
    hashes = {bytes(r[0]) for r in gb.raw.execute("SELECT code_sha256 FROM invite")}
    runs = re.findall(r"(?=([A-Za-z2-7]{12}))", re.sub(r"[-\s]", "", text))
    return bool(CODE.search(text)) or any(_hash(x) in hashes for x in runs)


def _env(live_server, tmp_path, who="jose"):
    """The human's own login, with a `glassboard` command on PATH that runs this source tree."""
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        shim = bin_dir / "glassboard"
        shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m glassboard.cli "$@"\n')
        shim.chmod(0o755)
        fake = bin_dir / "claude"                         # stands in for Claude Code: records its arguments
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > claude-args.txt\nglassboard whoami --json > claude-whoami.json\n')
        fake.chmod(0o755)
    env = dict(live_server.env(who))
    env.update(PATH=f"{bin_dir}:{env['PATH']}", PYTHONPATH=SRC)
    return env


def _cli(env, *args, cwd=None):
    return subprocess.run([sys.executable, "-m", "glassboard.cli", *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def _run_script(env, folder):
    return subprocess.run(["sh", str(folder / team.SCRIPT)], env=env, cwd="/", capture_output=True, text=True,
                          timeout=90, stdin=subprocess.DEVNULL)


def _invites(gb):
    return tuple(gb.raw.execute("SELECT COUNT(*), COUNT(used_at) FROM invite").fetchone())


def test_team_up_writes_one_secret_free_script_per_agent_and_changes_nothing_on_the_board(b, live_server, tmp_path):
    env = _env(live_server, tmp_path)
    before, invites = len(b.events()), _invites(b)
    r = _cli(env, "team", "up", "s1", f"writer:{tmp_path / 'w'}", f"reviewer:{tmp_path / 'r'}", "--client", "codex")
    assert r.returncode == 0, r.stderr
    assert len(b.events()) == before and _invites(b) == invites          # no code exists until a script runs
    for agent, folder in (("writer", tmp_path / "w"), ("reviewer", tmp_path / "r")):
        path = folder / team.SCRIPT
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        text = path.read_text()
        assert text.startswith("#!/bin/sh\n") and "gbt_" not in text and not CODE.search(text)
        assert f"exec glassboard team start s1 {agent} --role member -- codex\n" in text
        assert "tmux" not in text                                       # any terminal, no other dependency
        assert "delivery hooks for codex" in text and "offered when the client exits" in text
    assert "Nothing changed on the board" in r.stdout


def test_team_up_is_for_human_session_managers_only(b, live_server, tmp_path):
    assert _cli(_env(live_server, tmp_path, "ana"), "team", "up", "s1", f"x:{tmp_path / 'x'}").returncode != 0
    agent = _cli({**live_server.env("jose/claude"), "PYTHONPATH": SRC}, "team", "up", "s1", f"x:{tmp_path / 'y'}")
    assert agent.returncode != 0 and "not_human" in agent.stderr
    assert not (tmp_path / "x" / team.SCRIPT).exists() and not (tmp_path / "y" / team.SCRIPT).exists()


def test_team_up_refuses_bad_pairs_and_existing_scripts_without_force(b, live_server, tmp_path):
    env = _env(live_server, tmp_path)
    for bad in (["Bad:./a"], ["noseparator"], [f"a:{tmp_path}", f"a:{tmp_path / 'b'}"], [f"a:{tmp_path}", f"b:{tmp_path}"]):
        assert _cli(env, "team", "up", "s1", *bad).returncode == 2, bad
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / team.SCRIPT).write_text("mine\n")
    r = _cli(env, "team", "up", "s1", f"writer:{tmp_path / 'w'}")
    assert r.returncode == 2 and "--force" in r.stderr and (tmp_path / "w" / team.SCRIPT).read_text() == "mine\n"
    assert _cli(env, "team", "up", "s1", f"writer:{tmp_path / 'w'}", "--force").returncode == 0
    assert "team start s1 writer" in (tmp_path / "w" / team.SCRIPT).read_text()


def test_start_script_joins_with_a_fresh_code_each_run_and_starts_the_client(b, live_server, tmp_path):
    env = _env(live_server, tmp_path)
    folder = tmp_path / "w"
    inner = "glassboard whoami --json"
    r = _cli(env, "team", "up", "s1", f"writer:{folder}", "--client", f"sh -c '{inner}'")
    assert r.returncode == 0, r.stderr
    assert "none: sh has no hooks" in r.stdout and "no wake adapter" in r.stdout
    first = _run_script(env, folder)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout.splitlines()[-1])["handle"] == "jose/writer"     # the client runs as the agent
    assert _invites(b) == (1, 1)                                         # one fresh code, redeemed at once
    out = first.stdout + first.stderr
    assert "gbt_" not in out and not _shows_a_code(b, out)               # neither code nor token is shown
    assert (folder / ".glassboard-profile").read_text() == "jose-writer\n"
    assert json.loads(_cli(env, "whoami", "--json", cwd=folder).stdout)["handle"] == "jose/writer"
    assert json.loads(_cli(env, "whoami", "--json", cwd=tmp_path).stdout)["handle"] == "jose"
    creds = [r[0] for r in b.raw.execute("SELECT c.id FROM credential c JOIN principal p ON p.id=c.principal_id "
                                         "WHERE p.handle='jose/writer' ORDER BY c.created_at")]
    second = _run_script(env, folder)                                    # a later run: a new code, not a stored one
    assert second.returncode == 0, second.stderr
    assert _invites(b) == (2, 2)
    assert f"revoked credential {creds[0]}" in second.stdout
    rows = dict(b.raw.execute("SELECT c.id, c.revoked_at FROM credential c JOIN principal p ON p.id=c.principal_id "
                              "WHERE p.handle='jose/writer'").fetchall())
    assert rows[creds[0]] is not None and sum(v is None for v in rows.values()) == 1


def test_start_script_ignores_the_folder_identity_and_asks_as_the_human(b, live_server, tmp_path):
    """A start script run in an agent's folder (pointer present) or from inside an agent client still
    invites as the human: workspace pointers, bindings and GLASSBOARD_PROFILE are ignored."""
    env = _env(live_server, tmp_path)
    folder = tmp_path / "w"
    assert _cli(env, "team", "up", "s1", f"writer:{folder}", "--client", "true").returncode == 0
    assert _run_script(env, folder).returncode == 0
    again = _run_script({**env, "GLASSBOARD_PROFILE": "jose-writer"}, folder)
    assert again.returncode == 0, again.stderr
    assert _invites(b) == (2, 2)


def test_start_script_turns_on_delivery_hooks_and_offers_wake_for_claude_code(b, live_server, tmp_path):
    env = _env(live_server, tmp_path)
    folder = tmp_path / "w"
    assert _cli(env, "team", "up", "s1", f"writer:{folder}").returncode == 0           # default client: claude
    r = _run_script(env, folder)
    assert r.returncode == 0, r.stderr
    settings = json.loads((folder / ".claude" / "settings.json").read_text())["hooks"]
    commands = {h["command"] for groups in settings.values() for g in groups for h in g["hooks"]}
    assert "glassboard hook deliver --client claude" in commands and "glassboard hook stop --client claude" in commands
    args = (folder / "claude-args.txt").read_text().split()
    assert args[0] == "--session-id" and re.fullmatch(r"[0-9a-f-]{36}", args[1])
    assert json.loads((folder / "claude-whoami.json").read_text())["handle"] == "jose/writer"
    # No terminal: the wake supervisor is offered as the exact command for this conversation and runtime.
    m = re.search(r"wake: to keep @jose/writer reachable while it is closed, run here: (glassboard wake run .*)", r.stderr)
    assert m, r.stderr
    assert f"--session-id {args[1]}" in m.group(1) and "--agent-runtime " in m.group(1)
    assert "--allowedTools 'Bash(glassboard *)'" in m.group(1)
    # Opt-outs: no hooks, no Stop hook, no wake.
    other = tmp_path / "o"
    assert _cli(env, "team", "up", "s1", f"other:{other}", "--no-hold", "--no-wake").returncode == 0
    r = _run_script(env, other)
    assert r.returncode == 0, r.stderr
    events = json.loads((other / ".claude" / "settings.json").read_text())["hooks"]
    assert "PostToolUse" in events and "Stop" not in events and "wake run" not in r.stderr
    bare = tmp_path / "n"
    assert _cli(env, "team", "up", "s1", f"bare:{bare}", "--no-hooks").returncode == 0
    assert _run_script(env, bare).returncode == 0 and not (bare / ".claude").exists()


def test_wake_offer_carries_the_documented_allow_rules_and_passes_the_supervisor_checks():
    wakerun.bind(cli)
    team.bind(cli)
    for client, rule in (("claude", "Bash(glassboard *)"), ("grok", "dontAsk"), ("codex", "--skip-git-repo-check")):
        argv = team.wake_args(client, "0199e5a4-7c1f-7d2e-9f00-3b1c2d4e5f60", "abc123", "/w", True, "jose-w")
        assert argv[:3] == ["wake", "run", "--client"] and rule in argv
        extra = argv[argv.index("--") + 1:]
        wakerun.check_extra(client, extra)                               # never a refused flag
        parsed, _ = cli.build_parser().parse_known_args(argv)
        assert parsed.session_id.startswith("0199") and parsed.agent_runtime == "abc123"
    codex = team.wake_args("codex", "x", "abc", "/w", False, "jose-w")
    assert any(x.startswith("sandbox_workspace_write.writable_roots=") for x in codex)
    assert team.with_session_id(["claude", "--resume", "x"]) == (["claude", "--resume", "x"], None)
    assert team.with_session_id(["codex"]) == (["codex"], None)


def test_team_page_shows_the_start_script_command_to_session_managers(b):
    from test_dashboard import login
    r = login(b, "jose").get("/s/s1/team")
    assert r.status_code == 200
    assert "glassboard team up s1 " in r.text and "start-glassboard.sh" in r.text


def test_onboarding_explains_delivery_and_wake_and_documents_team_up():
    text = (ROOT / "docs" / "onboarding.md").read_text()
    assert "glassboard team up" in text and "does not see new work" in text
    errata = ROOT / "docs" / "ERRATA.md"            # internal: not in the public export
    if errata.exists():
        assert "## E49" in errata.read_text() and "Hard rule 10, clarified" in errata.read_text()
