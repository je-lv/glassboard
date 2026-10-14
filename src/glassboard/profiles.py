"""Several agent identities on one machine, with no environment juggling (stdlib only).

`glassboard join` stores each identity as a *profile*: its own config and state directories under
`~/.config/glassboard/profiles/<handle with / as ->`. Which profile a `glassboard` command uses is
decided in this order:

  1. `--as PROFILE` on the command line;
  2. the `GLASSBOARD_PROFILE` environment variable (set by `glassboard join … -- <client>`);
  3. a *process binding*: `glassboard join` run by an agent from inside its client (Claude Code,
     Codex, …) records that client process (PID + start time, against PID reuse). Every later
     command walks its own ancestors and finds it. Several agents can therefore share one working
     directory, and the human's own terminal (an ancestor, never a descendant) stays the human's;
  4. a `.glassboard-profile` pointer file (no secret in it) in the working directory or a parent,
     written by `glassboard join --here`;
  5. otherwise the default configuration (`glassboard login`).

Codex runs its tool commands in a PID namespace, so it cannot see its ancestors: there, use
`glassboard join … -- codex`, `--here`, or `--as`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from . import runtime

NAME_RE = re.compile(r"^[a-z0-9-]{1,65}$")
POINTER = ".glassboard-profile"
DEFAULT_CLIENTS = ("claude", "codex")


def _base(var: str, default: str) -> str:
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), default)


# The user's own base directories. Activation repoints XDG_* for this process and its children, so it
# also exports the originals; a child (the agent client launched by `join … -- CMD`, and every command
# that client runs) must find the profiles where they really are, not inside the active profile.
ORIG_CONFIG = os.environ.get("GLASSBOARD_BASE_CONFIG") or _base("XDG_CONFIG_HOME", ".config")
ORIG_STATE = os.environ.get("GLASSBOARD_RUNTIME_STATE") or _base("XDG_STATE_HOME", os.path.join(".local", "state"))


def name_for(handle: str) -> str:
    return handle.replace("/", "-")


def profile_root(name: str) -> str:
    return os.path.join(ORIG_CONFIG, "glassboard", "profiles", name)


def exists(name: str) -> bool:
    return os.path.isfile(os.path.join(profile_root(name), "cfg", "glassboard", "config.json"))


def _bindings_dir() -> str:
    d = os.path.join(ORIG_STATE, "glassboard", "bindings")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _comm(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except OSError:
            return ""
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
        return os.path.basename(out.strip())
    except Exception:
        return ""


def client_names() -> tuple[str, ...]:
    extra = tuple(x.strip() for x in os.environ.get("GLASSBOARD_CLIENTS", "").split(",") if x.strip())
    return DEFAULT_CLIENTS + extra


def find_client_ancestor() -> tuple[int, str, str] | None:
    """The nearest ancestor that is an agent client process: (pid, start marker, name)."""
    pid, seen = os.getppid(), 0
    names = client_names()
    while pid > 1 and seen < 16:
        info = runtime._proc_info(pid)
        if info is None:
            return None
        name = _comm(pid)
        if name in names:
            return pid, info[1], name
        pid, seen = info[0], seen + 1
    return None


def bind(name: str) -> tuple[int, str] | None:
    anc = find_client_ancestor()
    if anc is None:
        return None
    pid, start, client = anc
    path = os.path.join(_bindings_dir(), f"{pid}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"profile": name, "pid": pid, "start": start, "client": client}, f)
    return pid, client


def _from_binding() -> tuple[str, str] | None:
    pid, seen = os.getpid(), 0
    d = _bindings_dir()
    while pid > 1 and seen < 64:
        info = runtime._proc_info(pid)
        if info is None:
            return None
        reg = os.path.join(d, f"{pid}.json")
        if os.path.exists(reg):
            try:
                with open(reg) as f:
                    rec = json.load(f)
                if rec.get("start") == info[1] and NAME_RE.match(rec.get("profile", "")):
                    return rec["profile"], f"bound to {rec.get('client', 'client')} process {pid}"
            except (OSError, ValueError):
                pass
        pid, seen = info[0], seen + 1
    return None


def pointer() -> tuple[str, str] | None:
    """Return ``(profile, pointer path)`` for the nearest workspace pointer."""
    d = os.getcwd()
    while True:
        p = os.path.join(d, POINTER)
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    name = f.read().strip()
            except OSError:
                return None
            return (name, p) if NAME_RE.match(name) else None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve(explicit: str | None = None) -> tuple[str | None, str]:
    if explicit:
        return explicit, "--as"
    env = os.environ.get("GLASSBOARD_PROFILE", "").strip()
    if env:
        return env, "GLASSBOARD_PROFILE"
    selected = _from_binding()
    if selected:
        return selected
    pointed = pointer()
    return (pointed[0], pointed[1]) if pointed else (None, "default")


def activate(name: str) -> None:
    """Point this process (and its children) at the profile's own config and state directories."""
    if not NAME_RE.match(name):
        raise ValueError("not a profile name")
    root = profile_root(name)
    os.environ["GLASSBOARD_RUNTIME_STATE"] = ORIG_STATE          # runtime registrations stay shared
    os.environ["GLASSBOARD_BASE_CONFIG"] = ORIG_CONFIG
    os.environ["XDG_CONFIG_HOME"] = os.path.join(root, "cfg")
    os.environ["XDG_STATE_HOME"] = os.path.join(root, "state")
    os.environ["GLASSBOARD_PROFILE"] = name


def write_pointer(name: str, directory: str | None = None) -> str:
    path = os.path.join(directory or os.getcwd(), POINTER)
    with open(path, "w") as f:
        f.write(name + "\n")
    return path
