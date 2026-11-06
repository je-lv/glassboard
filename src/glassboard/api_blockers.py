"""Biggest blockers (0.1.5, ERRATA E41): what holds up the most work, from recorded links only.

Edges, all recorded by the board, never inferred from timing:
- a blocked task waits on what its `waiting_on` names: a question or approval (#seq), a task (T#)
  or a member (@handle);
- an open, in-progress or blocked task needs each task in its `depends_on` that is not done.
Each chain is followed to its root: an unresolved question or approval, a member, or a task that
is unfinished but not itself waiting (or a blocked task whose named cause is already resolved: it
is held only by its own stale "blocked" mark). Roots are ranked by how many distinct tasks they
hold up, directly or through other tasks. Nothing here changes state.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .deps import authenticate, get_db
from .sessions import require_member

router = APIRouter(prefix="/api/v1")
LIVE = ("open", "in_progress", "blocked")
SHOWN = 10


def _edges(c, sid: str) -> tuple[dict, dict]:
    """(tasks by num, waits: num -> list of node keys). Node keys: 'T7', '#12', '@alice'."""
    tasks = {t["num"]: t for t in c.execute("SELECT * FROM task WHERE session_id=?", (sid,))}
    waits: dict[int, list[str]] = {}
    for n, t in tasks.items():
        if t["status"] not in LIVE:
            continue
        out = []
        if t["status"] == "blocked" and t["waiting_on"]:
            w = json.loads(t["waiting_on"])
            out.append(w["label"] if w["kind"] != "task" else f"T{w['ref']}")
        for d in json.loads(t["depends_on"] or "[]"):
            dt = tasks.get(d)
            if dt is not None and dt["status"] not in ("done", "cancelled"):
                out.append(f"T{d}")
        if out:
            waits[n] = list(dict.fromkeys(out))
    return tasks, waits


def _unresolved(c, sid: str, seq: int) -> bool:
    return bool(inbox.obligation_holders(c, sid, seq))


def blockers(c, sess) -> dict:
    sid = sess["id"]
    tasks, waits = _edges(c, sid)

    def roots(node: str, seen: frozenset) -> set[str]:
        if node in seen:
            return {node}                                          # a cycle: report where it closes
        if node.startswith("T"):
            n = int(node[1:])
            nxt = waits.get(n, [])
            live = [x for x in nxt if not (x.startswith("#") and not _unresolved(c, sid, int(x[1:])))]
            if not live:
                return {node}
            out: set[str] = set()
            for x in live:
                out |= roots(x, seen | {node})
            return out
        if node.startswith("#") and not _unresolved(c, sid, int(node[1:])):
            return set()
        return {node}

    held: dict[str, set[int]] = {}
    for n, targets in waits.items():
        for t in targets:
            for r in roots(t, frozenset({f"T{n}"})):
                held.setdefault(r, set()).add(n)
        if tasks[n]["status"] == "blocked" and not any(
                x for x in targets if not (x.startswith("#") and not _unresolved(c, sid, int(x[1:])))):
            held.setdefault(f"T{n}", set())                       # blocked, but its named cause is resolved
    # a root that is itself one of the held tasks counts only the others
    items = []
    for key, nums in held.items():
        nums = {x for x in nums if f"T{x}" != key}
        items.append(_describe(c, sess, tasks, key, sorted(nums)))
    items = [i for i in items if i["holds"] or i["kind"] == "stale_block"]
    items.sort(key=lambda i: (-len(i["holds"]), i["key"]))
    return {"session": sess["handle"], "count": len(items), "blockers": items[:SHOWN], "more": max(0, len(items) - SHOWN)}


def _describe(c, sess, tasks: dict, key: str, holds: list[int]) -> dict:
    sid = sess["id"]
    out = {"key": key, "holds": [{"task": n, "title": tasks[n]["title"], "status": tasks[n]["status"]} for n in holds]}
    if key.startswith("#"):
        ev = c.execute("SELECT actor_id, type, body FROM event WHERE session_id=? AND seq=?", (sid, int(key[1:]))).fetchone()
        owers = inbox.handle_map(c, inbox.obligation_holders(c, sid, int(key[1:])))
        out.update(kind="approval" if ev and ev["type"] == "approval" else "question", seq=int(key[1:]),
                   owed_by=sorted(owers.values()), text=(ev["body"] or "")[:200] if ev else "")
    elif key.startswith("@"):
        out.update(kind="member", handle=key[1:])
    else:
        t = tasks[int(key[1:])]
        h = inbox.handle_map(c, [t["claimant_id"], t["assignee_id"]])
        stale = t["status"] == "blocked"
        out.update(kind="stale_block" if stale else "task", task=t["num"], title=t["title"], status=t["status"],
                   claimant=h.get(t["claimant_id"]), assignee=h.get(t["assignee_id"]))
    return out


def blockers_text(b: dict) -> str:
    n = b["count"]
    lines = [render.section(f"--- blockers · {b['session']} · " + (f"{n} root cause(s), biggest first ---" if n
                                                                  else "nothing is waiting on anything ---"))]
    for i in b["blockers"]:
        held = len(i["holds"])
        if i["kind"] in ("question", "approval"):
            who = ", ".join("@" + h for h in i["owed_by"]) or "nobody"
            head = f"--- #{i['seq']} {i['kind']} owed by {who} · holds up {held} task(s) ---"
        elif i["kind"] == "member":
            head = f"--- @{i['handle']} is waited on · holds up {held} task(s) ---"
        elif i["kind"] == "stale_block":
            head = f"--- T{i['task']} is still marked blocked though what it waited on is resolved ---"
        else:
            who = f"claimed by @{i['claimant']}" if i["claimant"] else (f"assigned to @{i['assignee']}" if i["assignee"]
                                                                      else "nobody has claimed it")
            head = f"--- T{i['task']} {i['status'].replace('_', ' ')}, {who} · holds up {held} task(s) ---"
        lines.append(render.section(head))
        if i.get("title"):
            lines.append(render.inline_line("title", i["title"]))
        if i.get("text"):
            lines.append(render.inline_line("text", i["text"]))
        if i["holds"]:
            lines.append(render.inline_line("holds up", ", ".join(f"T{x['task']}" for x in i["holds"])))
    if b["more"]:
        lines.append(render.section(f"--- … {b['more']} more ---"))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/blockers")
def session_blockers(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                     p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """What holds up the most work: root causes of waiting tasks, from recorded links only."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        b = blockers(c, sess)
    if format == "text" or "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(blockers_text(b))
    return b
