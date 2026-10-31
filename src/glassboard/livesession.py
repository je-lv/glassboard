"""Is the agent's client session open right now? Evidence for `glassboard wake run` (ERRATA E31).

The supervisor resumes a client headless only on positive evidence that the session is not open on
this machine; anything else holds the wake (the board shows it to the agent's human). Two sources:

Lease. `glassboard run -- CLIENT` and `glassboard join … -- CLIENT` take a shared lock on the
identity's `live.lock` and the client they exec inherits it, so it is held until the client and
everything it started have exited. The supervisor takes the same lock exclusively for each headless
run, so a launcher started meanwhile waits for that run to end. Cross-process, kernel-released on
exit, never a stale file. A process the client left running keeps it held: the wake is then held,
the safe direction.

Native signals, per client (probed 2026-10-27; a format a later client version changes reads as
unknown, never as closed):
  claude    ~/.claude/sessions/<pid>.json names the session ID of a live interactive process
  codex     ~/.codex/thread-writer-locks/<thread>.lock is flock-held by the TUI or by the app-server
            daemon that keeps the thread loaded (its background agents may still write to it)
  grok      ~/.grok/active_sessions.json lists the session ID with a live process
  opencode  no signal: unknown unless the human attests `--headless-only`
  script    no conversation: always closed
A process ID that exists counts as alive; a reused ID can only make a session look open.
"""
from __future__ import annotations

import glob
import json
import os
import sys

from . import clistate

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"


def lease_path() -> str:
    return os.path.join(clistate.state_root(), "live.lock")


def _lease_fd() -> int:
    path = lease_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def hold_for_client() -> int | None:
    """The launchers: a shared lease that the exec'd client inherits. Waits while a wake run holds it."""
    try:
        import fcntl
    except ImportError:                                  # Windows: no lease (`wake run` needs POSIX anyway)
        return None
    try:
        fd = _lease_fd()
    except OSError as e:
        print(f"glassboard: warning: no live-session lease ({e.__class__.__name__}); a wake supervisor "
              "relies on the client's own signal", file=sys.stderr)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        print("glassboard: a wake run is using this agent now; waiting for it to finish (Ctrl-C to cancel)",
              file=sys.stderr, flush=True)
        fcntl.flock(fd, fcntl.LOCK_SH)
    os.set_inheritable(fd, True)
    return fd


def try_exclusive() -> int | None:
    """The supervisor, around one headless run: None while an interactive launcher holds the lease."""
    import fcntl
    fd = _lease_fd()                                     # not inheritable: the headless client never holds it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def release(fd: int) -> None:
    os.close(fd)


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                              # someone else's process: it exists
        return True
    return True


def _home(env: str, default: str) -> str:
    return os.environ.get(env) or os.path.join(os.path.expanduser("~"), default)


def _claude(session_id: str) -> tuple[str, str]:
    d = os.path.join(_home("CLAUDE_CONFIG_DIR", ".claude"), "sessions")
    if not os.path.isdir(d):
        return UNKNOWN, "Claude Code keeps no session registry here"
    for path in glob.glob(os.path.join(d, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except FileNotFoundError:                        # removed as that session closed
            continue
        except (OSError, ValueError):
            return UNKNOWN, "a Claude Code session record could not be read"
        if isinstance(rec, dict) and rec.get("sessionId") == session_id and _alive(rec.get("pid")):
            return OPEN, f"open in Claude Code (process {rec['pid']})"
    return CLOSED, "no running Claude Code process has this session"


def _codex(session_id: str) -> tuple[str, str]:
    d = os.path.join(_home("CODEX_HOME", ".codex"), "thread-writer-locks")
    if not os.path.isdir(d):
        return UNKNOWN, "Codex keeps no thread locks here"
    path = os.path.join(d, f"{session_id}.lock")
    try:
        ino = os.stat(path).st_ino
    except FileNotFoundError:
        return CLOSED, "no Codex process has opened this thread here"
    holders = _flock_holders(ino)
    if holders is None:                                  # no /proc/locks: probe without blocking anyone
        import fcntl
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return CLOSED, "no Codex process holds this thread"
        except BlockingIOError:
            return OPEN, "open in Codex (its thread lock is held)"
        finally:
            os.close(fd)
    if not holders:
        return CLOSED, "no Codex process holds this thread"
    pid = holders[0]
    where = "the Codex app-server daemon" if "app-server" in _cmdline(pid) else "Codex"
    return OPEN, f"loaded in {where} (process {pid})"


def _flock_holders(ino: int) -> list[int] | None:
    """PIDs holding a lock on inode `ino` (any device: a collision can only make it look open)."""
    try:
        with open("/proc/locks") as f:
            rows = f.read().splitlines()
    except OSError:
        return None
    out = []
    for row in rows:
        parts = row.split()
        if len(parts) >= 6 and parts[5].rsplit(":", 1)[-1] == str(ino):
            try:
                out.append(int(parts[4]))
            except ValueError:
                pass
    return out


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _grok(session_id: str) -> tuple[str, str]:
    path = os.path.join(os.path.expanduser("~"), ".grok", "active_sessions.json")
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        return UNKNOWN, "Grok keeps no active-session list here"
    except (OSError, ValueError):
        return UNKNOWN, "Grok's active-session list could not be read"
    if not isinstance(rows, list):
        return UNKNOWN, "Grok's active-session list has an unknown format"
    for r in rows:
        if isinstance(r, dict) and r.get("session_id") == session_id and _alive(r.get("pid")):
            return OPEN, f"open in Grok (process {r['pid']})"
    return CLOSED, "no running Grok process has this session"


NATIVE = {"claude": _claude, "codex": _codex, "grok": _grok}


def native(client: str, session_id: str | None) -> tuple[str, str]:
    if client == "script":
        return CLOSED, "a script has no interactive session"
    probe = NATIVE.get(client)
    if probe is None:
        return UNKNOWN, f"{client} gives no open-session signal"
    try:
        return probe(session_id or "")
    except OSError as e:
        return UNKNOWN, f"the {client} session signal could not be read ({e.__class__.__name__})"


def may_resume(client: str, session_id: str | None, headless_only: bool) -> tuple[bool, str]:
    """(resume headless?, why). An open signal always wins; unknown resumes only on the human's attestation."""
    state, why = native(client, session_id)
    if state == OPEN:
        return False, why
    if state == UNKNOWN:
        if headless_only:
            return True, why + "; the human attested --headless-only"
        return False, why + "; restart the supervisor with --headless-only if this session is never opened in a terminal"
    return True, why
