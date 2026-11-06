"""Runtime identification with an env-independent fallback.

Primary: GLASSBOARD_RUNTIME env var exported by `glassboard run`.
Fallback: `glassboard run` registers its own PID (+ process start time, against PID reuse) and then
exec()s the client, so the client keeps that PID. Any CLI process launched by the client's tool
(even with a sanitised environment) walks its ancestor chain and finds the registration.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys

LABEL_RE = re.compile(r"^[a-z0-9-]{1,16}$")
_ALPH = "abcdefghijkmnpqrstuvwxyz23456789"


def state_dir() -> str:
    base = (os.environ.get("GLASSBOARD_RUNTIME_STATE") or os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))
    d = os.path.join(base, "glassboard", "runtimes")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def new_label() -> str:
    return "".join(secrets.choice(_ALPH) for _ in range(4))


def _proc_info(pid: int) -> tuple[int, str] | None:
    """Return (ppid, start_marker) or None."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
        except OSError:
            return None
        rest = data[data.rfind(")") + 2:].split()
        return int(rest[1]), rest[19]          # ppid, starttime (clock ticks since boot)
    try:  # macOS / BSD
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    ppid, _, lstart = out.partition(" ")
    return int(ppid), " ".join(lstart.split())


def register(label: str, pid: int | None = None) -> str:
    pid = pid or os.getpid()
    info = _proc_info(pid)
    path = os.path.join(state_dir(), f"{pid}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"label": label, "pid": pid, "start": info[1] if info else None}, f)
    return path


def resolve() -> tuple[str | None, str]:
    """Return (label, source) where source is 'env', 'ancestor:<pid>' or 'none'."""
    env = os.environ.get("GLASSBOARD_RUNTIME", "")
    if LABEL_RE.match(env):
        return env, "env"
    pid, seen = os.getpid(), 0   # include self: shells may exec() the final command in place
    while pid > 1 and seen < 64:
        info = _proc_info(pid)
        if info is None:
            break
        reg = os.path.join(state_dir(), f"{pid}.json")
        if os.path.exists(reg):
            try:
                with open(reg) as f:
                    rec = json.load(f)
                if rec.get("start") == info[1] and LABEL_RE.match(rec.get("label", "")):
                    return rec["label"], f"ancestor:{pid}"
            except (OSError, ValueError):
                pass
        pid, seen = info[0], seen + 1
    return None, "none"


def _die_with_parent() -> None:
    """Linux: the client gets SIGTERM if this wrapper dies (even by SIGKILL), as it did when the
    wrapper exec'd into it. Best effort; other systems rely on SIGHUP/SIGTERM forwarding."""
    import ctypes
    import signal
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def run(cmd: list[str], on_exit=None) -> int:
    """Start the client as a new runtime. With `on_exit` (B37) this process stays as the client's
    parent: the terminal and stdio are inherited, Ctrl-C and Ctrl-\\ go to the client (the terminal
    signals the whole foreground group, so the parent ignores them), SIGTERM and SIGHUP are passed on,
    and when the client exits `on_exit(label, returncode)` reports it. Returns the client's status."""
    import signal
    label = new_label()
    register(label)
    os.environ["GLASSBOARD_RUNTIME"] = label
    print(f"glassboard: runtime {label}", file=sys.stderr)
    if on_exit is None:
        os.execvp(cmd[0], cmd)
    try:
        child = subprocess.Popen(cmd, preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
    except OSError as e:
        print(f"glassboard: cannot start {cmd[0]}: {e.strerror}", file=sys.stderr)
        return 127

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    saved = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGHUP)}
    saved.update({sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGQUIT)})
    try:
        code = child.wait()
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    try:
        on_exit(label, code)
    except Exception as e:                       # reporting must never change the client's exit status
        print(f"glassboard: could not report the exit: {e}", file=sys.stderr)
    return 128 - code if code < 0 else code
