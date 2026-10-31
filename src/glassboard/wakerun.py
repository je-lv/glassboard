"""`glassboard wake …`: waking idle agents from the agent's own machine (ERRATA E30, decision 0035).

    wake status [--wait S]             pending demand, counts and the supervisor's state
    wake done [--generation N]         the agent records the generation it inspected
    wake arm|pause|resume|stop|disarm  supervisor control (the fence stays in this runtime's state)
    wake run --client C --session-id ID --agent-runtime LABEL [options] [-- EXTRA_CLIENT_ARGS]

`wake run` is the human-armed supervisor. It never runs on the board and never reads message
bodies: it waits on the non-consuming wake signal, records a dispatch, and resumes the client by
its exact session ID with a fixed prompt. It resumes only on positive evidence that the session is
not open on this machine (`livesession`, ERRATA E31); otherwise it holds the wake, visibly on the
board, and checks again every `--recheck` seconds until the session closes or the work is handled. Message text never reaches the argument vector or the
prompt; the agent reads the board itself through `glassboard sync`. One client run at a time: the
next dispatch happens only after the previous run exited (and a local lock keeps a second
supervisor for the same identity off this machine; the board's dispatch fence keeps it off others).

Stops (terminal; a human re-arms by running `wake run` again): hourly budget used, dispatch limit
reached, too many wakes without progress, session archived, board restored, fence released
elsewhere (for example a human `wake stop`). Pauses (a human resumes with `glassboard wake resume
REASON`, and the same supervisor continues): the client exited without recording an inspection, or
the client could not be started.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

from . import livesession, render

# The running CLI module, set by `bind()`. Under `python -m glassboard.cli` that module is `__main__`;
# importing `glassboard.cli` here would load a second copy whose CliError the CLI does not catch.
cli = None


def bind(module) -> None:
    global cli
    cli = module

PROMPT = ("New Glassboard work may be waiting for you. Inspect it: run `glassboard sync`, handle what it "
          "shows under your existing operator instructions, then run `glassboard wake done`.\n")
ADAPTERS = ("claude", "codex", "grok", "opencode", "script")
# Flags an adapter must never receive from the operator's extra arguments: they escalate
# permissions, or point the resume at a session other than the recorded one.
REFUSED = ("--always-approve", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
           "--dangerously-bypass-hook-trust", "--yolo", "--auto", "--fork-session", "--fork", "--restore-code",
           "--session-id", "--session", "-s", "--last", "--continue", "-c", "--resume", "-r", "--full-auto")
# Permission modes that answer prompts on the human's behalf; allow rules (`--allow`, `--allowedTools`)
# are the way to let a headless run use a tool.
REFUSED_MODES = ("acceptEdits", "auto", "bypassPermissions")
GENERATION_ENV = "GLASSBOARD_WAKE_GENERATION"


def _fence(ctx) -> str:
    fence = ctx.state.runtime(ctx.label).get("wake_fence")
    if not fence:
        raise cli.CliError(cli.EXIT_USAGE, "no_wake_fence", "This runtime holds no wake fence.",
                       {"hint": "Arm a supervisor with `glassboard wake run …` (or `glassboard wake arm`)."})
    return fence


def _status(ctx, wait: int = 0, after: int | None = None) -> dict:
    q = f"?wait={wait}" + (f"&after={after}" if after is not None else "") if wait else ""
    return cli._call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/wake{q}")


def _status_lines(s: dict) -> list[str]:
    sup = s["supervisor"]
    counts = " · ".join(f"{k} {v}" for k, v in s["counts"].items()) or "nothing"
    lines = [render.section(f"--- wake · {s['session']} ({s['session_state']}) · generation {s['generation']} · "
                            f"inspected {s['inspected']} · {'pending' if s['pending'] else 'nothing pending'} ---"),
             render.section(f"--- pending: {counts} ---")]
    if sup["state"] == "none":
        lines.append(render.section("--- supervisor: none armed ---"))
        return lines
    if sup["outstanding"] is not None:
        out = f" · running the client for dispatch {sup['outstanding']} since {sup['dispatched_at']}"
    else:
        out = "" if sup["connected"] is not False else " · not connected"
    lines.append(render.section(f"--- supervisor: {sup['state']} · {sup['adapter']} · W{sup['fence_gen']}{out} ---"))
    if sup["hold"] is not None:
        lines.append(render.section(f"--- wake held · #{sup['hold']['seq']} ---"))
        lines.append(render.inline_line("reason", sup["hold"]["reason"]))
    return lines


def _op(ctx, name: str, body: dict, what: str, effect: str | None = None) -> dict:
    with ctx.state.locked():
        return cli.send_op(ctx, "POST", f"{ctx.path}/wake/{name}", body, what, fence_effect=effect)


def arm(ctx, adapter: str, takeover: str | None = None) -> dict:
    body = {"adapter": adapter, **({"takeover": {"reason": takeover}} if takeover else {})}
    return _op(ctx, "arm", body, "wake arm", effect="wake_store")


def control(ctx, name: str, reason: str | None = None) -> dict:
    body = {"fence": _fence(ctx), **({"reason": reason} if reason is not None else {})}
    return _op(ctx, name, body, f"wake {name}", effect="wake_drop" if name in ("stop", "disarm") else None)


# --- adapters ---------------------------------------------------------------------------------

def check_extra(client: str, extra: list[str]) -> None:
    def refuse(flag: str) -> None:
        raise cli.CliError(cli.EXIT_USAGE, "refused_flag", f"`{flag}` is not allowed in a wake supervisor.",
                           {"hint": "The supervisor resumes only the recorded session, with the client's normal permissions."})
    for i, arg in enumerate(extra):
        flag, _, value = arg.partition("=")
        if not value and i + 1 < len(extra):
            value = extra[i + 1]
        if flag == "-c" and client == "codex":              # Codex: a config override, not "continue"
            if "danger" in value or value.split("=", 1)[0].strip() == "approval_policy":
                refuse(f"-c {value.split('=', 1)[0]}")
            continue
        if flag == "--permission-mode":
            if value in REFUSED_MODES:
                refuse(f"--permission-mode {value}")
            continue
        if flag in REFUSED or flag.startswith("--dangerously"):
            refuse(flag)


def command(client: str, session_id: str, prompt: str, extra: list[str], max_turns: int | None,
            attach: str | None, script: str | None) -> tuple[list[str], str | None]:
    """(argv, stdin text). The prompt is the fixed wake prompt, never board content."""
    if client == "script":
        if not script:
            raise cli.CliError(cli.EXIT_USAGE, "usage", "The script adapter needs --script PATH.")
        return [script, *extra], None
    if not session_id or not all(ch.isalnum() or ch in "-_" for ch in session_id):
        raise cli.CliError(cli.EXIT_USAGE, "usage", "--session-id must be the client's exact session ID.")
    turns = ["--max-turns", str(max_turns)] if max_turns else []
    if client == "claude":
        # On stdin: variadic options such as `--allowedTools A B` would swallow a trailing prompt.
        return ["claude", "-p", "--resume", session_id, *turns, *extra], prompt
    if client == "codex":
        return ["codex", "exec", "resume", session_id, *extra, "-"], prompt
    if client == "grok":
        return ["grok", "--single", prompt, "--resume", session_id, *turns, *extra], None
    if client == "opencode":
        return ["opencode", "run", "-s", session_id, *(["--attach", attach] if attach else []), *extra, prompt], None
    raise cli.CliError(cli.EXIT_USAGE, "usage", f"Unknown client {client!r}.")


def read_prompt(path: str | None) -> str:
    if path is None:
        return PROMPT
    st = os.stat(path)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_uid != os.getuid():
        raise cli.CliError(cli.EXIT_USAGE, "unsafe_prompt_file", "The prompt file must be yours and not writable by others.")
    return open(path, encoding="utf-8").read()


# --- the supervisor -------------------------------------------------------------------------

class Stop(Exception):
    def __init__(self, reason: str, pause: bool = False):
        super().__init__(reason)
        self.reason, self.pause = reason, pause


def _lock(ctx):
    path = os.path.join(cli.clistate.state_root(), f"wake-{ctx.info['id']}.lock")
    import fcntl                                         # POSIX only: `run` refuses Windows before this
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise cli.CliError(cli.EXIT_USAGE, "supervisor_running", "Another wake supervisor for this session runs on this machine.")
    return fd


def _progress_since(ctx, seq: int, handle: str) -> int:
    """Events the agent wrote after `seq`, other than wake bookkeeping: proof of useful work."""
    q = f"?after={seq}&actor={handle}&limit=100"
    events = cli._call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/events{q}").get("events", [])
    return sum(1 for e in events if not e["type"].startswith("wake."))


def _log(msg: str) -> None:
    print(f"wake: {msg}", file=sys.stderr, flush=True)


def run(a) -> int:
    if os.name == "nt":                                  # the other wake commands work on Windows
        raise cli.CliError(cli.EXIT_USAGE, "unsupported_platform",
                           "`glassboard wake run` needs Linux or macOS; `wake status`, `wake done` and the "
                           "controls work here.")
    if not a.client or not a.agent_runtime or (a.client != "script" and not a.session_id):
        raise cli.CliError(cli.EXIT_USAGE, "usage", "usage: glassboard wake run --client C --session-id ID --agent-runtime LABEL "
                                            "[options] [-- EXTRA_CLIENT_ARGS]")
    # argparse hands the first word after `--` to the optional REASON positional; `run` takes none.
    extra = [x for x in ([a.reason] if a.reason else []) + list(a.command) if x != "--"]
    check_extra(a.client, extra)
    prompt = read_prompt(a.prompt_file)
    command(a.client, a.session_id, prompt, extra, a.max_turns, a.attach, a.script)        # validate early
    if not cli.runtime.LABEL_RE.match(a.agent_runtime):
        raise cli.CliError(cli.EXIT_USAGE, "usage", "--agent-runtime must be the agent's runtime label ([a-z0-9-], ≤16).")
    os.environ["GLASSBOARD_RUNTIME"] = ("w-" + a.agent_runtime)[:16]          # the supervisor's own label
    ctx = cli.open_session(a)
    lock = _lock(ctx)
    me = cli._call(ctx.cfg, ctx.tok, "GET", "/api/v1/whoami")["handle"]
    try:
        r = arm(ctx, a.client, a.takeover)
        _log(f"armed {r['adapter']} as {me} · fence W{r['wake_gen']} · #{r['seq']}")
        while True:
            try:
                return _loop(ctx, a, me, prompt, extra)
            except Stop as st:
                if not st.pause:
                    _finish(ctx, "stop", st.reason)
                    return cli.EXIT_OK
                control(ctx, "pause", st.reason)
                label = os.environ["GLASSBOARD_RUNTIME"]
                _log(f"paused: {st.reason}. Resolve it, then: GLASSBOARD_RUNTIME={label} glassboard wake resume REASON")
                if not _wait_resumed(ctx):
                    return cli.EXIT_OK
    except KeyboardInterrupt:
        _finish(ctx, "disarm", None)
        return cli.EXIT_OK
    except cli.CliError as e:
        if e.code == cli.EXIT_RECOVERY:
            _log("the board was restored: stopping. Tell your human, then `glassboard reconcile`.")
        raise
    finally:
        os.close(lock)


def _hold(ctx, s: dict, why: str) -> None:
    """Record that pending demand is not delivered, once per reason (the board ignores a repeat)."""
    current = s["supervisor"]["hold"]
    if current is not None and current["reason"] == why:
        return
    try:
        r = _op(ctx, "hold", {"fence": _fence(ctx), "reason": why[:500]}, "wake hold")
    except cli.CliError as e:
        if e.kind == "nothing_pending":                              # handled meanwhile
            return
        raise
    _log(f"held generation {s['generation']}: {why}" + (f" · #{r['seq']}" if "seq" in r else ""))


def _loop(ctx, a, me: str, prompt: str, extra: list[str]) -> int:
    """Wait, dispatch, run the client; raises Stop. Budgets restart after a human resume."""
    dispatched: list[float] = []
    idle = 0
    held: int | None = None                                          # generation held at the last check
    while True:
        s = _status(ctx, wait=25) if held is None else _status(ctx, wait=max(1, int(a.recheck)), after=held)
        if s["supervisor"]["state"] == "none":
            raise Stop("fence released elsewhere (a human stop, a takeover, a restore or a membership change)")
        if s["session_state"] == "archived":
            raise Stop("session archived")
        if not s["pending"]:
            held = None
            continue
        if s["supervisor"]["state"] == "paused" or s["session_state"] == "paused":
            _status(ctx, wait=25, after=s["generation"])             # demand stays pending: wait, do not spin
            continue
        if held is None:
            time.sleep(a.debounce)                                   # let a burst coalesce
            s = _status(ctx)
            if not s["pending"]:
                continue
        now = time.monotonic()
        dispatched = [t for t in dispatched if now - t < 3600]
        if len(dispatched) >= a.max_per_hour:
            raise Stop(f"hourly wake budget used ({a.max_per_hour})")
        gen = s["generation"]
        lease = livesession.try_exclusive()                          # a launcher opening meanwhile waits for it
        if lease is None:
            resume, why = False, "open through `glassboard run` or `glassboard join` on this machine"
        else:
            resume, why = livesession.may_resume(a.client, a.session_id, a.headless_only)
        if not resume:
            if lease is not None:
                livesession.release(lease)
            _hold(ctx, s, why)
            held = gen
            continue
        held = None
        try:
            d = _op(ctx, "dispatch", {"fence": _fence(ctx), "generation": gen}, "wake dispatch")
            dispatched.append(now)
            _log(f"dispatch generation {gen} · #{d['seq']}")
            argv, stdin = command(a.client, a.session_id, prompt, extra, a.max_turns, a.attach, a.script)
            env = {**os.environ, "GLASSBOARD_RUNTIME": a.agent_runtime, GENERATION_ENV: str(gen)}
            try:
                done = subprocess.run(argv, input=stdin, text=True, env=env, cwd=a.cwd)
                code = done.returncode
            except OSError as e:
                raise Stop(f"client could not be started ({e.__class__.__name__})", pause=True)
        finally:
            livesession.release(lease)
        after = _status(ctx)
        if after["supervisor"]["outstanding"] is not None:
            raise Stop(f"client exited (code {code}) without recording an inspection", pause=True)
        if _progress_since(ctx, d["seq"], me) == 0:
            idle += 1
            if idle >= a.idle_stops:
                raise Stop(f"{idle} wakes in a row without progress")
        else:
            idle = 0
        if a.max_dispatches and len(dispatched) >= a.max_dispatches:
            raise Stop(f"dispatch limit reached ({a.max_dispatches})")


def _finish(ctx, name: str, reason: str | None) -> None:
    try:
        control(ctx, name, reason)
        _log(f"{name}{': ' + reason if reason else ''}")
    except cli.CliError as e:
        if e.kind not in ("wake_mismatch", "no_wake_fence"):
            raise
        _log(f"{name}: {reason or 'done'}; the fence was already released")


def _wait_resumed(ctx) -> bool:
    """Paused: dispatch nothing until a human resumes (True) or stops (False) this supervisor."""
    while True:
        s = _status(ctx, wait=25)
        state = s["supervisor"]["state"]
        if state == "armed":
            _log("resumed")
            return True
        if state == "none":
            _log("stopped while paused")
            return False


def main(a, module) -> int:
    bind(module)
    act = a.action
    if act == "run":
        return run(a)
    ctx = cli.open_session(a, allow_reconcile=act == "status")
    if act == "status":
        s = _status(ctx, wait=a.wait)
        if a.json:
            cli._print_json(s)
        else:
            sys.stdout.write("\n".join(_status_lines(s)) + "\n")
        return cli.EXIT_OK
    if act == "done":
        gen = a.generation if a.generation is not None else os.environ.get(GENERATION_ENV)
        if gen is None:                       # no supervisor started this process: the dispatch it is answering
            gen = _status(ctx)["supervisor"]["outstanding"]
        if gen is None:
            raise cli.CliError(cli.EXIT_USAGE, "usage", "Name the generation you inspected: glassboard wake done --generation N",
                           {"hint": "No wake dispatch is outstanding; `glassboard wake status` shows the current generation."})
        r = _op(ctx, "inspected", {"generation": int(gen)}, "wake done")
        print(f"inspected generation {int(gen)}" + (f" · #{r['seq']}" if "seq" in r else " · already recorded"))
        return cli.EXIT_OK
    if act == "arm":
        if a.client not in ADAPTERS:
            raise cli.CliError(cli.EXIT_USAGE, "usage", f"--client must be one of {', '.join(ADAPTERS)}.")
        r = arm(ctx, a.client, a.takeover)
        print(f"armed {r['adapter']} · fence W{r['wake_gen']} kept in this runtime · #{r['seq']}")
        return cli.EXIT_OK
    if act in ("pause", "resume", "stop"):
        if not a.reason:
            raise cli.CliError(cli.EXIT_USAGE, "usage", f"usage: glassboard wake {act} REASON")
        r = control(ctx, act, a.reason)
    else:
        r = control(ctx, "disarm")
    print(f"wake {act} · #{r['seq']}")
    return cli.EXIT_OK
