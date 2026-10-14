"""Who receives an event in their inbox.

`route()` is the only place that decides routing. It returns (recipient principal IDs, broadcast).
`broadcast=True` means "all": no recipient rows; every member whose `joined_seq` is below the
event sees it (inbox rule). The actor is never a recipient of its own event: the
inbox excludes self-authored events anyway, and leaving the row out keeps "routed to me" honest.
Types routed to nobody (task.claimed, checkpoint.saved, consumer.attached, ...) still appear in the
timeline and in history; they just do not enter an inbox.
"""
from __future__ import annotations

import sqlite3

from . import events

ALL = "all"

# type → routing rule name.
RULES = {
    "message.preflight": None, "artifact.added": ALL,
    "question": "explicit_or_all", "answer": "explicit_or_all", "update": "explicit_or_all",
    "proposal": "explicit_or_all", "blocker": "explicit_or_all", "note": "explicit_or_all",
    "approval": "explicit_or_all", "approval.granted": "resolution", "approval.denied": "resolution",
    "task.reviewed": "claimant_and_coordinators",
    "lease.taken": None, "lease.released": None, "lease.expired": "member",
    "decision": ALL,
    "question.resolved": "resolution",
    "task.created": "assignee", "task.edited": "claimant_else_assignee",
    "task.acknowledged": "coordinators", "task.claimed": None, "task.note": None, "task.unblocked": None, "task.checked": None,
    "task.blocked": "coordinators_and_waited", "task.submitted": "coordinators", "task.released": "coordinators",
    "task.reclaimed": "coordinators",
    "task.accepted": "claimant", "task.returned": "claimant",
    "task.reassigned": "previous_claimant_and_assignee", "task.cancelled": "previous_claimant_and_assignee",
    "task.reopened": "previous_claimant_and_assignee",
    "task.stale": "claimant_and_coordinators",
    "checkpoint.saved": None, "consumer.attached": None,
    "consumer.takeover": "owner_and_coordinators",
    "member.added": "member", "member.removed": "member", "member.changed": "member", "member.jobrole": "member",
    "session.created": ALL, "session.paused": ALL, "session.resumed": ALL, "session.archived": ALL,
    "system.restored": ALL,
}
assert set(RULES) == set(events.TYPES), "every event type needs a routing rule"


def route(conn: sqlite3.Connection, session_id: str, type_: str, actor_id: str, *,
          to: list[str] | None = None, member: str | None = None, assignee: str | None = None,
          claimant: str | None = None, previous_claimant: str | None = None,
          asker: str | None = None, resolved_for: list[str] | None = None,
          principal: str | None = None, waited: str | None = None) -> tuple[list[str], bool]:
    """All arguments are principal IDs. `to=None` (or empty) on explicit_or_all means broadcast."""
    rule = RULES[type_]
    if rule == ALL:
        return [], True
    if rule is None:
        return [], False
    if rule == "explicit_or_all":
        if not to:
            return [], True
        ids = list(to)
    elif rule == "member":
        ids = [member]
    elif rule == "assignee":
        ids = [assignee]
    elif rule == "claimant":
        ids = [claimant]
    elif rule == "claimant_else_assignee":
        ids = [claimant or assignee]
    elif rule == "coordinators":
        ids = events.coordinator_ids(conn, session_id)
    elif rule == "coordinators_and_waited":       # a blocked task also reaches the member it waits on
        ids = [waited, *events.coordinator_ids(conn, session_id)]
    elif rule == "claimant_and_coordinators":
        ids = [claimant, *events.coordinator_ids(conn, session_id)]
    elif rule == "previous_claimant_and_assignee":
        ids = [previous_claimant, assignee]
    elif rule == "owner_and_coordinators":
        owner = conn.execute("SELECT owner_id FROM principal WHERE id=?", (principal,)).fetchone()
        ids = [owner["owner_id"] if owner else None, *events.coordinator_ids(conn, session_id)]
    elif rule == "resolution":
        ids = [asker, *(resolved_for or [])]
    else:  # pragma: no cover
        raise ValueError(rule)
    return sorted({i for i in ids if i and i != actor_id}), False
