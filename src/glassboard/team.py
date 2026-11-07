"""`glassboard team …`: start scripts that start each agent already joined (B60, B73, ERRATA E49). Stdlib only.

    team up SESSION AGENT:FOLDER [AGENT:FOLDER …] [--client C] [--role R] [--local] [--no-hooks] [--no-hold]
            [--no-wake] [--force]
    team start SESSION AGENT [--role R] [--local] [--no-hooks] [--no-hold] [--no-wake] -- CLIENT…

`team up` is run by a human Session Manager. It changes nothing on the board: it checks that the caller
may invite agents into the session and writes one small shell script per agent into the agent's
folder (`start-glassboard.sh`, mode 0700). The script holds no token and no join code; it runs
`glassboard team start` in its own folder.

`team start` is what the script runs, in a terminal, when the human wants that agent to start:
  1. under the human's own login (the default login, or `--as PROFILE`; workspace pointers, agent
     bindings and GLASSBOARD_PROFILE are ignored, so a script in an agent's folder never acts as that
     agent) it asks the board for a fresh one-time join code;
  2. it redeems the code in the same process, so the code is never printed, written to a file, put in
     an environment variable or passed as an argument;
  3. it writes the folder's `.glassboard-profile` pointer (as `join --here`), and revokes the agent's
     previous credential for this folder when the new one replaced it;
  4. for Claude Code and Codex it installs the delivery hooks into the folder's own client settings
     (`hooks install --project . --deliver [--hold]`), so new board events reach the agent after each
     tool call and once more before a turn ends (B73);
  5. it starts the client as the agent (as `join … -- CLIENT`) and, when the client exits, offers the
     wake supervisor (`wake run`) for that exact client session, so new work resumes the agent while
     it is closed (B73).
Starting agents: hard rule 10 (ERRATA E49) is about the board. The board server, and anything board
content triggers, never starts an agent; a command a human runs on their own machine may.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
import uuid

from . import hooks, livesession, profiles, render

SCRIPT = "start-glassboard.sh"
AGENT_RE = re.compile(r"^[a-z0-9-]{1,32}$")
KNOWN = ("claude", "codex", "grok", "opencode")
WAKE_CLIENTS = ("claude", "codex", "grok")      # OpenCode keeps no open-session record: its wakes are held
TOKEN_ID_RE = re.compile(r"^gbt_([a-z2-7]{10})_")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Claude Code flags that pick the conversation themselves: then the script does not set --session-id.
CLAUDE_SESSION_FLAGS = ("--session-id", "--resume", "-r", "--continue", "-c", "--print", "-p", "--fork-session")

cli = None                                       # the running CLI module (see wakerun.bind)


def bind(module) -> None:
    global cli
    cli = module


def main(a, module) -> int:
    bind(module)
    return up(a) if a.action == "up" else start(a)


# --- the human's identity ---------------------------------------------------------------------------------

def use_human(name: str | None) -> None:
    """Point this process at the human's own login: `--as NAME`, else the default configuration. Never a
    workspace pointer, an agent binding or an inherited GLASSBOARD_PROFILE."""
    for var in ("GLASSBOARD_PROFILE", "GLASSBOARD_PROFILE_ROOT", "GLASSBOARD_RUNTIME"):
        os.environ.pop(var, None)
    if name:
        if not profiles.NAME_RE.match(name) or not profiles.exists(name):
            raise cli.CliError(cli.EXIT_CONFIG, "no_profile", "No identity by that --as name on this machine.")
        profiles.activate(name)
        return
    os.environ["XDG_CONFIG_HOME"] = profiles.ORIG_CONFIG
    os.environ["XDG_STATE_HOME"] = profiles.ORIG_STATE
    os.environ["GLASSBOARD_RUNTIME_STATE"] = profiles.ORIG_STATE
    os.environ["GLASSBOARD_BASE_CONFIG"] = profiles.ORIG_CONFIG


def _human(a) -> tuple[dict, str, dict]:
    use_human(a.as_profile)
    cfg, tok = cli._authed()
    me = cli._call(cfg, tok, "GET", "/api/v1/whoami")
    if me.get("kind") != "human":
        raise cli.CliError(cli.EXIT_USAGE, "not_human", "Team start scripts are made and run under a human's own login.",
                           {"hint": "Run it in your own terminal, logged in as yourself (`glassboard login`, or "
                                    "--as YOUR-PROFILE); agents never invite agents."})
    return cfg, tok, me


def _check_coordinator(cfg, tok, session: str, me: dict) -> dict:
    s = cli._fetch_session(cfg, tok, session)
    members = cli._call(cfg, tok, "GET", f"/api/v1/sessions/{cli._quote(s['session'])}/members").get("members", [])
    mine = next((m for m in members if m.get("handle") == me["handle"]), None)
    if not mine or mine.get("role") != "coordinator":
        raise cli.CliError(cli.EXIT_HTTP, "forbidden", "Only a Session Manager of the session adds agents to it.",
                           {"hint": "Ask a Session Manager to make you one, or to run `glassboard team up`."})
    return s


# --- team up: write the scripts ---------------------------------------------------------------------------

def parse_pairs(pairs: list[str]) -> list[tuple[str, str]]:
    out, agents, folders = [], set(), set()
    for p in pairs:
        agent, sep, folder = p.partition(":")
        if not sep or not folder:
            raise cli.CliError(cli.EXIT_USAGE, "usage", "Name each agent as AGENT:FOLDER (for example writer:./writer).")
        if not AGENT_RE.match(agent):
            raise cli.CliError(cli.EXIT_USAGE, "usage", "Agent names are 1-32 lowercase letters, digits or dashes.")
        folder = os.path.abspath(os.path.expanduser(folder))
        if agent in agents or folder in folders:
            raise cli.CliError(cli.EXIT_USAGE, "usage", "Each agent and each folder appears once: one agent per folder.")
        agents.add(agent)
        folders.add(folder)
        out.append((agent, folder))
    return out


def client_argv(client: str) -> list[str]:
    try:
        argv = shlex.split(client)
    except ValueError:
        raise cli.CliError(cli.EXIT_USAGE, "usage", "--client is a client name or a command line.") from None
    if not argv:
        raise cli.CliError(cli.EXIT_USAGE, "usage", "--client is a client name or a command line.")
    return argv


def client_name(argv: list[str]) -> str | None:
    name = os.path.basename(argv[0]) if argv else ""
    return name if name in KNOWN else None


def describe(argv: list[str], hooks_on: bool, hold: bool, wake: bool) -> tuple[str, str]:
    name = client_name(argv)
    if not hooks_on:
        h = "off (--no-hooks)"
    elif name in hooks.CLIENTS:
        h = f"delivery hooks for {name} in ./{'.claude/settings.json' if name == 'claude' else '.codex/hooks.json'}" + \
            (" (after each tool call and once before a turn ends)" if hold else " (after each tool call)")
    else:
        h = f"none: {name or argv[0]} has no hooks Glassboard can install"
    if not wake:
        w = "off (--no-wake)"
    elif name in WAKE_CLIENTS:
        w = "offered when the client exits"
    elif name == "opencode":
        w = "not offered: OpenCode keeps no open-session record (see `glassboard wake run --headless-only`)"
    else:
        w = "not offered: no wake adapter for this client"
    return h, w


def render_script(session: str, handle: str, agent: str, argv: list[str], opts: list[str], human: str | None,
                  hooks_line: str, wake_line: str, written: str) -> str:
    args = ["glassboard", *(["--as", human] if human else []), "team", "start", session, agent, *opts, "--", *argv]
    return "\n".join([
        "#!/bin/sh",
        f"# Glassboard start script for @{handle} in session {session}.",
        f"# Written by `glassboard team up` on {written}. Run it yourself, in a terminal, when this agent",
        "# should start: ./" + SCRIPT,
        "#",
        "# It holds no token and no join code. Each run:",
        "#   1. asks the board for a fresh one-time join code under your own login"
        + (f" (--as {human})" if human else "") + ";",
        "#      the code stays inside that one process: never printed, saved, or passed as an argument;",
        f"#   2. joins this folder as @{handle} (identity file .glassboard-profile, no secret inside)",
        "#      and revokes the credential an earlier run left here;",
        f"#   3. hooks: {hooks_line};",
        f"#   4. starts the client: {shlex.join(argv)};",
        f"#   5. wake supervisor: {wake_line}.",
        "set -eu",
        'cd "$(dirname "$0")"',
        "exec " + shlex.join(args),
        "",
    ])


def _write_script(folder: str, text: str, force: bool) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, SCRIPT)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o700)
    if os.path.lexists(path) and not force:
        os.unlink(tmp)
        raise cli.CliError(cli.EXIT_USAGE, "exists", f"{path} exists; pass --force to replace it.")
    os.replace(tmp, path)
    return path


def up(a) -> int:
    pairs = parse_pairs(a.pairs)
    argv = client_argv(a.client)
    for _, folder in pairs:                                   # refuse before anything is written
        if os.path.lexists(os.path.join(folder, SCRIPT)) and not a.force:
            raise cli.CliError(cli.EXIT_USAGE, "exists", f"{os.path.join(folder, SCRIPT)} exists; pass --force to replace it.")
        if os.path.exists(folder) and not os.path.isdir(folder):
            raise cli.CliError(cli.EXIT_USAGE, "usage", f"{folder} is not a folder.")
    cfg, tok, me = _human(a)
    s = _check_coordinator(cfg, tok, a.session_name, me)
    opts = ["--role", a.role] + (["--local"] if a.local else []) + (["--no-hooks"] if a.no_hooks else []) + \
        (["--no-hold"] if a.no_hold else []) + (["--no-wake"] if a.no_wake else [])
    h, w = describe(argv, not a.no_hooks, not a.no_hold, not a.no_wake)
    written = cli._now()[:10]
    for agent, folder in pairs:
        handle = f"{me['handle']}/{agent}"
        text = render_script(s["session"], handle, agent, argv, opts, a.as_profile, h, w, written)
        path = _write_script(folder, text, a.force)
        print(f"wrote {path} (0700) for @{render.r2(handle)} · {render.r2(shlex.join(argv))}")
    print(f"hooks: {h}")
    print(f"wake: {w}")
    print("Nothing changed on the board and no join code exists yet. Run each script in its own terminal when that "
          "agent should start; it asks the board for a fresh code then.")
    return cli.EXIT_OK


# --- team start: what the script runs ---------------------------------------------------------------------

def _credential_file(name: str, folder: str, local: bool) -> str:
    root = profiles.local_root(folder) if local else profiles.profile_root(name)
    return os.path.join(root, "cfg", "glassboard", "credentials")


def _previous_credential(path: str) -> str | None:
    """The public credential ID of the token an earlier run left (never the secret itself)."""
    try:
        with open(path, encoding="utf-8") as f:
            m = TOKEN_ID_RE.match(f.read().strip())
    except OSError:
        return None
    return m.group(1) if m else None


def with_session_id(argv: list[str]) -> tuple[list[str], str | None]:
    """Claude Code: start the conversation under a session ID chosen here, so the wake supervisor can resume
    exactly that conversation later. Left alone when the command line already picks one."""
    if client_name(argv) != "claude" or any(x.split("=", 1)[0] in CLAUDE_SESSION_FLAGS for x in argv[1:]):
        return argv, None
    sid = str(uuid.uuid4())
    return [argv[0], "--session-id", sid, *argv[1:]], sid


def wake_args(client: str, session_id: str, label: str, folder: str, local: bool, name: str) -> list[str]:
    """`glassboard wake run …` for this agent, with the allow rules the onboarding guide lists per client."""
    extra: list[str] = []
    if client == "claude":
        extra = ["--allowedTools", "Bash(glassboard *)"]
    elif client == "grok":
        extra = ["--permission-mode", "dontAsk", "--allow", "Bash(glassboard *)"]
    elif client == "codex":
        extra = ["-c", "sandbox_workspace_write.network_access=true", "--skip-git-repo-check"]
        if not local:                                      # the profile's config and state live outside the folder
            roots = [profiles.profile_root(name), os.path.join(profiles.ORIG_STATE, "glassboard")]
            extra[2:2] = ["-c", "sandbox_workspace_write.writable_roots=[" + ", ".join(f'"{r}"' for r in roots) + "]"]
    return ["wake", "run", "--client", client, "--session-id", session_id, "--agent-runtime", label,
            "--cwd", folder, "--", *extra]


def _ask(prompt: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt).strip()                       # nosec B322 - Python 3 only
    except EOFError:
        return None


def offer_wake(a, client: str | None, sid: str | None, label: str | None, folder: str, name: str, handle: str,
               code: int) -> int:
    if a.no_wake or client not in WAKE_CLIENTS or not label:
        if client == "opencode" and not a.no_wake:
            print("wake: not offered for OpenCode (no open-session record); see `glassboard wake run --headless-only`.",
                  file=sys.stderr)
        return code
    if sid is None:
        answer = _ask(f"Session ID of the {client} conversation that just ended, to resume it when new work "
                      "arrives (Enter to skip): ")
        if not answer:
            print(f"wake: not started. Later: glassboard wake run --client {client} --session-id ID "
                  f"--agent-runtime {label} (in {folder})", file=sys.stderr)
            return code
        if not SESSION_ID_RE.match(answer):
            print("wake: that does not look like a session ID; not started.", file=sys.stderr)
            return code
        sid = answer
    argv = wake_args(client, sid, label, folder, a.local, name)
    shown = "glassboard " + shlex.join(argv)
    answer = _ask(f"Keep @{handle} reachable while it is closed? The wake supervisor resumes this conversation "
                  "headless when work arrives for it (Ctrl-C stops it). Start it now? [Y/n] ")
    if answer is None:
        print(f"wake: to keep @{handle} reachable while it is closed, run here: {shown}", file=sys.stderr)
        return code
    if answer.lower() not in ("", "y", "yes"):
        print(f"wake: not started. Later, in {folder}: {shown}", file=sys.stderr)
        return code
    print(f"starting: {shown}", file=sys.stderr, flush=True)
    return cli._main(argv)


def start(a) -> int:
    argv = list(a.command[1:] if a.command[:1] == ["--"] else a.command)
    if not argv:
        raise cli.CliError(cli.EXIT_USAGE, "usage", "usage: glassboard team start SESSION AGENT [options] -- CLIENT…")
    if not AGENT_RE.match(a.agent):
        raise cli.CliError(cli.EXIT_USAGE, "usage", "Agent names are 1-32 lowercase letters, digits or dashes.")
    folder = os.getcwd()
    cfg, tok, me = _human(a)
    handle = f"{me['handle']}/{a.agent}"
    name = profiles.name_for(handle)
    cred_path = _credential_file(name, folder, a.local)
    previous = _previous_credential(cred_path)
    a.session = a.session_name
    ctx = cli.open_session(a)
    with ctx.state.locked():
        r = cli.send_op(ctx, "POST", f"{ctx.path}/invites", {"agent": a.agent, "role": a.role}, f"invite {a.agent}")
    if not r.get("code"):
        raise cli.CliError(cli.EXIT_HTTP, "no_code", "The board sent no join code (an answer to an earlier request).",
                           {"hint": "Run the script again: each run asks for a new code."})
    board = {k: cfg[k] for k in ("url", "ca_file", "insecure_http") if k in cfg}
    j = cli.redeem(board, r["code"], local=a.local)            # the code never leaves this process
    r = None
    print(f"identity file: {profiles.write_pointer(name, local=a.local)} (no secret inside)")
    if previous and previous != j["credential_id"]:
        try:
            cli._call(cfg, tok, "DELETE", f"/api/v1/me/tokens/{previous}", headers={"Idempotency-Key": cli.ids.new_ulid()})
            print(f"revoked credential {previous}, which an earlier run left in this folder")
        except cli.CliError as e:
            if e.status != 404:
                print(f"warning: could not revoke credential {previous} ({render.r2(e.kind)}); revoke it on My agents",
                      file=sys.stderr)
    client = client_name(argv)
    if not a.no_hooks and client in hooks.CLIENTS:
        path, changed, _ = hooks.install(client, folder, None, True, not a.no_hold)
        print(f"{'installed' if changed else 'kept'} the glassboard delivery hooks for {client}: {path}")
        if client == "codex":
            print("codex: open /hooks in Codex once to review and trust them; Codex skips new hooks until trusted")
    elif not a.no_hooks:
        print(f"no delivery hooks for {render.r2(client or argv[0])}: it sees new work when it runs `glassboard sync`"
              + (" or a wake resumes it" if client in WAKE_CLIENTS else ""))
    argv, sid = with_session_id(argv)
    sys.stdout.flush()                                     # our lines first, then the client's
    print(f"starting {render.r2(argv[0])} as @{render.r2(handle)}…", file=sys.stderr)
    fd = livesession.hold_for_client()
    seen: dict = {}

    def on_exit(label: str, rc: int) -> None:
        seen["label"] = label
        cli._report_exit(label, rc)

    rc = cli.runtime.run(cli._with_compaction(argv, a.compact_at), on_exit=on_exit)
    if fd is not None:
        livesession.release(fd)                            # the supervisor needs the lease while it runs the client
    return offer_wake(a, client, sid, seen.get("label"), folder, name, handle, rc)
