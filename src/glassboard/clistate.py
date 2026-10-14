"""Participant-side state of the CLI. Standard library only.

Layout under $XDG_STATE_HOME/glassboard/sessions/<session handle>/ (dirs 0700, files 0600):
  session.json         {"session", "id", "recovery_id", "epoch", "needs_reconcile", "old_recovery_id"}
  runtime-<label>.json {"consumer", "delivered_through", "pending_attach", "fences"}
  outbox.jsonl         operations not yet confirmed by the board, in order
  quarantine.jsonl     operations created under a recovery ID the board no longer accepts
  .lock                flock, so two CLI processes of one session do not interleave

Rules:
- the recovery ID is fetched once (`use`, or first use of a session) and stored; the CLI NEVER
  silently adopts a different one. Only `glassboard reconcile`, run by a human, adopts it;
- every mutation is written to the outbox (with its op ID and recovery ID) BEFORE it is sent and
  removed only when the board answered it; a network failure leaves it for the next command,
  which sends it again with the same op ID (idempotent replay);
- on `recovery_mismatch` every entry created under the old recovery ID moves to quarantine, the
  runtime's consumer token and fences are discarded, and the session is marked
  `needs_reconcile`: no further request carries the old recovery ID.
Consumer tokens and fences live only in runtime files, never in checkpoints (protocol rule 10).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from typing import Iterator

from . import client_lock

SESSION_ARG_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31}|[0-9A-Z]{26})$")   # handle or ULID: safe as a path part
NO_RUNTIME = "norun"


def state_root() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "glassboard", "sessions")


def _mkdir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path: str, obj) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_lines(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _write_lines(path: str, entries: list[dict]) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=True, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class SessionState:
    def __init__(self, session: str):
        if not SESSION_ARG_RE.match(session):
            raise ValueError("invalid session name")
        self.session = session
        self.dir = _mkdir(os.path.join(_mkdir(state_root()), session))

    # -- session.json -----------------------------------------------------------------------------
    @property
    def path(self) -> str:
        return os.path.join(self.dir, "session.json")

    def load(self) -> dict | None:
        return _read_json(self.path, None)

    def save(self, info: dict) -> None:
        _write_json(self.path, info)

    # -- runtime file -------------------------------------------------------------------------------
    def runtime_path(self, label: str | None) -> str:
        return os.path.join(self.dir, f"runtime-{label or NO_RUNTIME}.json")

    def runtime(self, label: str | None) -> dict:
        return _read_json(self.runtime_path(label), {"consumer": None, "delivered_through": None,
                                                     "pending_attach": None, "fences": {}})

    def save_runtime(self, label: str | None, rt: dict) -> None:
        _write_json(self.runtime_path(label), rt)

    def discard_runtime_secrets(self) -> None:
        """After a restore: every runtime's consumer token and fences are dead."""
        for name in os.listdir(self.dir):
            if name.startswith("runtime-") and name.endswith(".json"):
                p = os.path.join(self.dir, name)
                rt = _read_json(p, {})
                rt.update({"consumer": None, "pending_attach": None, "fences": {}, "delivered_through": None})
                _write_json(p, rt)

    # -- outbox / quarantine --------------------------------------------------------------------
    @property
    def outbox_path(self) -> str:
        return os.path.join(self.dir, "outbox.jsonl")

    @property
    def quarantine_path(self) -> str:
        return os.path.join(self.dir, "quarantine.jsonl")

    def outbox(self) -> list[dict]:
        return _read_lines(self.outbox_path)

    def quarantine(self) -> list[dict]:
        return _read_lines(self.quarantine_path)

    def add_outbox(self, entry: dict) -> None:
        _write_lines(self.outbox_path, [*self.outbox(), entry])

    def remove_outbox(self, op: str) -> None:
        _write_lines(self.outbox_path, [e for e in self.outbox() if e["op"] != op])

    def quarantine_recovery(self, old_recovery_id: str) -> list[dict]:
        """Move every outbox entry created under `old_recovery_id` to quarantine."""
        moved = [e for e in self.outbox() if e["recovery_id"] == old_recovery_id]
        if moved:
            _write_lines(self.quarantine_path, [*self.quarantine(), *moved])
            _write_lines(self.outbox_path, [e for e in self.outbox() if e["recovery_id"] != old_recovery_id])
        return moved

    def remove_quarantine(self, op: str) -> dict | None:
        q = self.quarantine()
        hit = next((e for e in q if e["op"] == op), None)
        if hit:
            _write_lines(self.quarantine_path, [e for e in q if e["op"] != op])
        return hit

    @contextlib.contextmanager
    def locked(self) -> Iterator["SessionState"]:
        fd = os.open(os.path.join(self.dir, ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            client_lock.lock(fd)
            yield self
        finally:
            client_lock.unlock(fd)
            os.close(fd)
