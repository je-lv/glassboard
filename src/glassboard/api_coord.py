"""Coordination: advisory resource leases and flow metrics.

Leases. A member says "I'm working on <resource>" (a path, a document, a service: free text). One
holder per resource per session; a second taker gets `409 resource_held` naming the holder. Leases are
advisory: the board cannot stop work done outside it. A lease expires when its holder has been idle
(no sync, no write) for longer than the session's staleness threshold; the sweeper then records
`lease.expired`, authored by `board`. The holder or a human coordinator releases it.

Flow metrics are computed from events only (read-only): per task, time to claim, time in progress,
time blocked, review turnaround, returns and self-reported effort; per member, tasks completed,
median cycle time, current claims and effort; per day, tasks accepted.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field

from . import db, events, ids, inbox, render, routing, tokens
from .api_tasks import _age_s
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, receipt_body, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import require_member, require_state

router = APIRouter(prefix="/api/v1")


def _wants_text(request: Request, format: str) -> bool:
    return format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


# --- leases ---------------------------------------------------------------------------------------------

class LeaseIn(StrictModel):
    resource: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, min_length=1, max_length=1024)


class LeaseRelease(StrictModel):
    resource: str = Field(min_length=1, max_length=200)


def lease_views(c, sid: str) -> list[dict]:
    rows = c.execute("SELECT l.*, m.last_seen_at FROM lease l LEFT JOIN member m ON m.session_id=l.session_id "
                     "AND m.principal_id=l.holder_id WHERE l.session_id=? ORDER BY l.resource", (sid,)).fetchall()
    h = inbox.handle_map(c, [r["holder_id"] for r in rows])
    return [{"resource": r["resource"], "holder": h.get(r["holder_id"]), "note": r["note"], "seq": r["seq"],
             "age_s": _age_s(r["taken_at"])} for r in rows]


@router.post("/sessions/{s}/leases", status_code=201)
def take_lease(s: str, body: LeaseIn, key: str = Depends(idempotency_key),
               recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
               runtime: str | None = Depends(runtime_label),
               p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/leases", body.model_dump(), status=201)
        if replay is not None:
            return replay
        if tx.me["role"] == "observer":
            raise ApiError(403, "forbidden", "Observers may read but not take leases.")
        require_state(tx.sess, "message")
        held = c.execute("SELECT * FROM lease WHERE session_id=? AND resource=?", (sid, body.resource)).fetchone()
        if held is not None:
            if held["holder_id"] == p.id:
                # A restarted runtime may repeat a take before it can see or release its own prior
                # lease. Treat that as success instead of queueing forever behind itself.
                return respond(receipt_body({"session": tx.sess["handle"], "seq": held["seq"],
                                             "resource": body.resource, "already_held": True}), 201)
            raise ApiError(409, "resource_held", "Someone already holds a lease on this resource.",
                           hint="Coordinate with the holder, or ask a coordinator to release it.",
                           details={"holder": inbox.handle_map(c, [held["holder_id"]]).get(held["holder_id"]),
                                    "age_s": _age_s(held["taken_at"])})
        holder = {"session": tx.sess["handle"]}

        def receipt(seq):
            return {**holder, "seq": seq, "resource": body.resource}
        seq, text = tx.event("lease.taken", body=body.note, data={"resource": body.resource}, receipt=receipt)
        c.execute("INSERT INTO lease(session_id,resource,holder_id,note,taken_at,seq) VALUES (?,?,?,?,?,?)",
                  (sid, body.resource, p.id, body.note, db.utcnow(), seq))
    return respond(text, 201)


@router.post("/sessions/{s}/leases/release")
def release_lease(s: str, body: LeaseRelease, key: str = Depends(idempotency_key),
                  recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                  runtime: str | None = Depends(runtime_label),
                  p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/leases/release", body.model_dump())
        if replay is not None:
            return replay
        held = c.execute("SELECT * FROM lease WHERE session_id=? AND resource=?", (sid, body.resource)).fetchone()
        if held is None:
            raise ApiError(404, "not_found", "No lease on this resource.")
        if held["holder_id"] != p.id and not (p.kind == "human" and tx.me["role"] == "coordinator"):
            raise ApiError(403, "forbidden", "Only the holder or a human coordinator may release this lease.")
        c.execute("DELETE FROM lease WHERE session_id=? AND resource=?", (sid, body.resource))
        holder = inbox.handle_map(c, [held["holder_id"]]).get(held["holder_id"])
        _, text = tx.event("lease.released", data={"resource": body.resource, "handle": holder},
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "resource": body.resource})
    return respond(text, 200)


@router.get("/sessions/{s}/leases")
def list_leases(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        leases = lease_views(c, sess["id"])
    if _wants_text(request, format):
        lines = [render.section(f"--- leases · {sess['handle']} · {len(leases)} ---")]
        for x in leases:
            lines += [render.section(f"--- held by {x['holder']} since #{x['seq']} ({x['age_s'] // 60}m) ---"),
                      render.inline_line("resource", x["resource"])]
            if x["note"]:
                lines.append(render.inline_line("note", x["note"]))
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"session": sess["handle"], "leases": leases}


def expire_leases(database: db.Database, now: datetime | None = None) -> int:
    """One pass: drop leases whose holder has been idle beyond the session's threshold; one
    `lease.expired` event each (board-authored, routed to the holder)."""
    now = now or datetime.now(timezone.utc)
    with database.read() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT l.session_id, l.resource, l.holder_id, l.taken_at, m.last_seen_at, s.stale_after_s FROM lease l "
            "JOIN session s ON s.id=l.session_id LEFT JOIN member m ON m.session_id=l.session_id AND m.principal_id=l.holder_id "
            "WHERE s.state='active'")]
    expired = 0
    for r in rows:
        last = max(x for x in (r["taken_at"], r["last_seen_at"]) if x)
        if (now - _parse(last)).total_seconds() <= r["stale_after_s"]:
            continue
        with database.write() as c:
            if c.execute("SELECT 1 FROM lease WHERE session_id=? AND resource=? AND holder_id=? AND taken_at=?",
                         (r["session_id"], r["resource"], r["holder_id"], r["taken_at"])).fetchone() is None:
                continue                                        # released or re-taken meanwhile
            c.execute("DELETE FROM lease WHERE session_id=? AND resource=?", (r["session_id"], r["resource"]))
            recipients, broadcast = routing.route(c, r["session_id"], "lease.expired", ids.BOARD_PRINCIPAL_ID,
                                                  member=r["holder_id"])
            holder = inbox.handle_map(c, [r["holder_id"]]).get(r["holder_id"])
            events.append(c, r["session_id"], ids.BOARD_PRINCIPAL_ID, "lease.expired", recipients=recipients,
                          broadcast=broadcast, body=f"The holder was idle longer than {r['stale_after_s'] // 60} min.",
                          data={"resource": r["resource"], "handle": holder})
            expired += 1
    return expired


# --- flow metrics ----------------------------------------------------------------------------------------

def flow(c, sess) -> dict:
    sid = sess["id"]
    tasks = {r["num"]: dict(r) for r in c.execute("SELECT num, title, status, claimant_id FROM task WHERE session_id=?", (sid,))}
    evs = c.execute("SELECT seq, ts, type, task_num, actor_id, data FROM event WHERE session_id=? AND task_num IS NOT NULL "
                    "ORDER BY seq", (sid,)).fetchall()
    per: dict[int, dict] = {n: {"task": n, "returns": 0, "effort_min": 0, "blocked_s": 0} for n in tasks}
    state: dict[int, dict] = {}
    for e in evs:
        n, t, ts = e["task_num"], e["type"], _parse(e["ts"])
        if n not in per:
            continue
        x, st = per[n], state.setdefault(n, {})
        data = json.loads(e["data"]) if e["data"] else {}
        if t == "task.created":
            st["created"] = ts
        elif t == "task.claimed":
            st.setdefault("first_claim", ts)
            st["claim"] = ts
            x["claimed_by"] = e["actor_id"]
        elif t == "task.blocked":
            st["blocked"] = ts
        elif t in ("task.unblocked", "task.submitted", "task.released", "task.reassigned", "task.cancelled") and st.get("blocked"):
            x["blocked_s"] += int((ts - st.pop("blocked")).total_seconds())
        if t == "task.submitted":
            st["submitted"] = ts
            x["submitted_by"] = e["actor_id"]
        elif t in ("task.accepted", "task.returned") and st.get("submitted"):
            x["review_s"] = int((ts - st.pop("submitted")).total_seconds())
        if t == "task.returned":
            x["returns"] += 1
        elif t == "task.accepted":
            st["done"] = ts
        elif t == "task.note" and data.get("effort_min"):
            x["effort_min"] += data["effort_min"]
        if "created" in st and "first_claim" in st:
            x["to_claim_s"] = int((st["first_claim"] - st["created"]).total_seconds())
        if "first_claim" in st and "done" in st:
            x["cycle_s"] = int((st["done"] - st["first_claim"]).total_seconds())
            x["done_day"] = st["done"].strftime("%Y-%m-%d")
    handles = inbox.handle_map(c, [x.get("submitted_by") or x.get("claimed_by") for x in per.values()]
                               + [t["claimant_id"] for t in tasks.values()])
    members: dict[str, dict] = {}
    for n, x in per.items():
        x.update(title=tasks[n]["title"], status=tasks[n]["status"])
        who = handles.get(x.pop("submitted_by", None) or x.pop("claimed_by", None))
        x.pop("claimed_by", None)
        if who:
            m = members.setdefault(who, {"member": who, "done": 0, "cycles": [], "effort_min": 0, "claims": 0})
            m["effort_min"] += x["effort_min"]
            if x["status"] == "done" and "cycle_s" in x:
                m["done"] += 1
                m["cycles"].append(x["cycle_s"])
    for t in tasks.values():
        if t["status"] in ("in_progress", "blocked") and handles.get(t["claimant_id"]):
            members.setdefault(handles[t["claimant_id"]], {"member": handles[t["claimant_id"]], "done": 0, "cycles": [],
                                                           "effort_min": 0, "claims": 0})["claims"] += 1
    for m in members.values():
        cycles = m.pop("cycles")
        m["median_cycle_s"] = int(statistics.median(cycles)) if cycles else None
    per_day: dict[str, int] = {}
    for x in per.values():
        if x.get("done_day") and x["status"] == "done":
            per_day[x["done_day"]] = per_day.get(x["done_day"], 0) + 1
    return {"session": sess["handle"], "tasks": [per[n] for n in sorted(per)],
            "members": sorted(members.values(), key=lambda m: m["member"]),
            "done_per_day": dict(sorted(per_day.items()))}


def _dur(s) -> str:
    if s is None:
        return "-"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def flow_text(f: dict) -> str:
    lines = [render.section(f"--- flow · {f['session']} · {len(f['tasks'])} task(s) ---")]
    for x in f["tasks"]:
        lines += [render.section(f"--- T{x['task']} · {x['status']} · to claim {_dur(x.get('to_claim_s'))} · cycle "
                                 f"{_dur(x.get('cycle_s'))} · blocked {_dur(x['blocked_s'])} · review {_dur(x.get('review_s'))} "
                                 f"· returns {x['returns']} · effort {x['effort_min']}m ---"),
                  render.inline_line("title", x["title"])]
    for m in f["members"]:
        lines.append(render.section(f"--- {m['member']} · done {m['done']} · median cycle {_dur(m['median_cycle_s'])} "
                                    f"· claims {m['claims']} · effort {m['effort_min']}m ---"))
    for day, n in f["done_per_day"].items():
        lines.append(render.section(f"--- {day}: {n} done ---"))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/flow")
def session_flow(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                 p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        f = flow(c, sess)
    return PlainTextResponse(flow_text(f)) if _wants_text(request, format) else f
