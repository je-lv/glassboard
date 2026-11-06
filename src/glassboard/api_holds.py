"""Decline, hold and stand-down (B41, ERRATA E36): sanctioned ways for an agent to say no or to object,
and for a human to ask an agent to stop and hand off. Agents raise; humans decide.

- Decline: `POST /sessions/{s}/tasks/{t}/decline {reason, fence?}`. The assignee of an open task (no fence)
  or its claimant (with the fence) gives the task back with a reason. The task returns to `open` with no
  assignee and no claimant (gen+1, the fence dies); one `task.declined` event reaches the coordinators,
  and human coordinators see it in their attention queue until someone else picks the task up.
- Hold: `POST /sessions/{s}/holds {body, default, deadline_min, task?}`. An objection that never blocks:
  it changes no task or session state. It is routed to the human coordinators, or, for a child task in an
  autonomous session (E27), to every coordinator (agent coordinators included). Every hold states what
  happens if nobody answers (`default`) and a deadline (5-1440 min). A recipient decides it once with
  `POST /sessions/{s}/holds/{seq} {decision: uphold|proceed, note?}`; past the deadline the sweeper
  records `hold.expired` and the stated default applies. Holds are owed by their recipients until then.
- Stand-down: `POST /sessions/{s}/standdowns {member, reason}` by a human coordinator or the agent's owner.
  The agent owes an answer: it releases every claim and lease, saves a checkpoint, then
  `POST /sessions/{s}/standdowns/{seq}/done`; the board refuses `done` until those are true. Until a
  human lifts it (`POST /sessions/{s}/standdowns/{seq}/lift`) the agent cannot claim tasks or take
  leases. Other agents keep working: only a stand-down or a session pause stops work.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import Field, StrictInt

from . import db, events, ids, inbox, routing
from .api_messages import parse_task
from .api_tasks import ACTIVE, _check_fence, _load, _num, _recovery_and_replay, _result, _update
from .deps import get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import PRINCIPAL_HANDLE_RE, require_state
from . import tokens

router = APIRouter(prefix="/api/v1")
MAX_TEXT = 16 * 1024
DEADLINE_MIN, DEADLINE_MAX = 5, 1440
TS = "%Y-%m-%dT%H:%M:%S.%fZ"


def _ts(dt: datetime) -> str:
    return dt.strftime(TS)


# --- decline -------------------------------------------------------------------------------------------

class DeclineIn(StrictModel):
    reason: str = Field(min_length=1, max_length=MAX_TEXT)
    fence: str | None = Field(default=None, max_length=32)      # absent: the assignee of an open task


@router.post("/sessions/{s}/tasks/{t}/decline")
def decline_task(s: str, t: str, body: DeclineIn, key: str = Depends(idempotency_key),
                 recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                 runtime: str | None = Depends(runtime_label),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Give a task back with a reason: the assignee of an open task, or the claimant with its fence."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        replay = _recovery_and_replay(tx, recovery, key, "POST /sessions/{s}/tasks/{t}/decline", task,
                                      body.model_dump(exclude_none=True))
        if replay is not None:
            return replay
        if body.fence is not None:
            _check_fence(c, task, p, body.fence)
            if task["status"] not in ACTIVE:
                raise ApiError(409, "invalid_transition", f"Cannot decline a task that is {task['status']}.")
        elif task["status"] != "open" or task["assignee_id"] != p.id:
            raise ApiError(409, "invalid_transition", "Only the assignee of an open task, or its claimant with the fence, "
                                                      "may decline it.",
                           hint="If you hold the task, pass your fence.")
        require_state(tx.sess, "task.finish")
        gen, version = task["gen"] + 1, task["version"] + 1
        _update(c, sid, task["num"], status="open", gen=gen, version=version, fence_secret=None, claimant_id=None,
                claimant_runtime=None, assignee_id=None, waiting_on=None)
        _, text = tx.event("task.declined", task_num=task["num"], body=body.reason, data={"gen": gen},
                           receipt=lambda seq: _result(task["num"], "open", gen, version, seq))
    return respond(text, 200)


def declined_open(c, sid: str) -> list:
    """Open, unassigned tasks whose latest task event is a decline: they need a human to act."""
    return c.execute(
        "SELECT t.num, t.title, e.seq, e.ts, e.actor_id, e.body FROM task t JOIN event e ON e.session_id=t.session_id "
        "AND e.task_num=t.num AND e.seq=(SELECT MAX(seq) FROM event x WHERE x.session_id=t.session_id "
        "AND x.task_num=t.num AND x.type LIKE 'task.%') "
        "WHERE t.session_id=? AND t.status='open' AND t.assignee_id IS NULL AND e.type='task.declined' ORDER BY t.num",
        (sid,)).fetchall()


# --- hold ----------------------------------------------------------------------------------------------

class HoldIn(StrictModel):
    body: str = Field(min_length=1, max_length=MAX_TEXT)           # what should wait, and why
    default: str = Field(min_length=1, max_length=1024)            # what happens if nobody decides in time
    deadline_min: StrictInt = Field(ge=DEADLINE_MIN, le=DEADLINE_MAX)
    task: str | StrictInt | None = None


class HoldDecision(StrictModel):
    decision: Literal["uphold", "proceed"]
    note: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)


def hold_deciders(c, sess, task) -> list[str]:
    """Human coordinators; for a child task in an autonomous session, every coordinator."""
    rows = c.execute("SELECT m.principal_id, p.kind FROM member m JOIN principal p ON p.id=m.principal_id "
                     "WHERE m.session_id=? AND m.role='coordinator' AND m.left_at IS NULL", (sess["id"],)).fetchall()
    agents_too = bool(sess["autonomous"]) and task is not None and task["parent"] is not None
    return [r["principal_id"] for r in rows if r["kind"] == "human" or (agents_too and r["kind"] == "agent")]


def hold_status(c, sid: str, seq: int):
    """The decision or expiry event of hold #seq, or None while it is open."""
    return c.execute("SELECT * FROM event WHERE session_id=? AND reply_to=? AND type IN ('hold.decided','hold.expired')",
                     (sid, seq)).fetchone()


@router.post("/sessions/{s}/holds", status_code=201)
def raise_hold(s: str, body: HoldIn, key: str = Depends(idempotency_key),
               recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
               runtime: str | None = Depends(runtime_label),
               p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Object to something without stopping anyone: the deciders owe an answer before the deadline."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        num = parse_task(body.task) if body.task is not None else None
        replay = tx.replay(key, "POST /sessions/{s}/holds", {**body.model_dump(exclude_none=True), "task": num},
                           status=201)
        if replay is not None:
            return replay
        if tx.me["role"] == "observer":
            raise ApiError(403, "forbidden", "Observers may read but not post.")
        task = _load(c, sid, num) if num is not None else None
        require_state(tx.sess, "message")
        to = [i for i in hold_deciders(c, tx.sess, task) if i != p.id]
        if not to:
            raise ApiError(409, "no_decider", "Nobody in this session can decide this hold.",
                           hint="A human Session Manager must be a member; ask one to join, or post a question.")
        deadline = _ts(datetime.now(timezone.utc) + timedelta(minutes=body.deadline_min))
        data = {"default": body.default, "deadline_at": deadline, "deadline_min": body.deadline_min}
        _, text = tx.event("hold", body=body.body, data=data, task_num=num, to=to,
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "type": "hold",
                                                "deadline_at": deadline})
    return respond(text, 201)


@router.post("/sessions/{s}/holds/{seq}")
def decide_hold(s: str, seq: int, body: HoldDecision, key: str = Depends(idempotency_key),
                recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                runtime: str | None = Depends(runtime_label),
                p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A recipient of the hold (or any human coordinator) decides it once: uphold or proceed."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        q = c.execute("SELECT * FROM event WHERE session_id=? AND seq=? AND type='hold'", (sid, seq)).fetchone()
        if q is None:
            raise ApiError(404, "not_found", "No such hold in this session.")
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/holds/{seq}", body.model_dump(), target={"type": "event", "id": seq})
        if replay is not None:
            return replay
        named = c.execute("SELECT 1 FROM event_recipient WHERE session_id=? AND seq=? AND principal_id=?",
                          (sid, seq, p.id)).fetchone() is not None
        human_coord = p.kind == "human" and tx.me["role"] == "coordinator"
        if q["actor_id"] == p.id or not (named or human_coord):
            raise ApiError(403, "forbidden", "Only a decider the hold names, or a human coordinator, may decide it.")
        require_state(tx.sess, "resolve")
        if hold_status(c, sid, seq) is not None:
            raise ApiError(409, "already_resolved", "This hold was already decided or has expired.")
        others = [r[0] for r in c.execute("SELECT principal_id FROM event_recipient WHERE session_id=? AND seq=?", (sid, seq))]
        _, text = tx.event("hold.decided", body=body.note, reply_to=seq, task_num=q["task_num"],
                           data={"hold": seq, "decision": body.decision}, asker=q["actor_id"], resolved_for=others,
                           receipt=lambda ev: {"session": tx.sess["handle"], "seq": ev, "hold": seq,
                                               "decision": body.decision})
    return respond(text, 200)


def expire_holds(database: db.Database, now: datetime | None = None) -> int:
    """One pass: every open hold past its deadline gets one board-authored `hold.expired`; the default applies.
    Holds expire in paused sessions too: a deadline is a promise to the agent that raised it."""
    now = now or datetime.now(timezone.utc)
    with database.read() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT h.session_id, h.seq FROM event h JOIN session s ON s.id=h.session_id "
            "WHERE h.type='hold' AND s.state != 'archived' AND json_extract(h.data, '$.deadline_at') <= ? "
            "AND NOT EXISTS (SELECT 1 FROM event d WHERE d.session_id=h.session_id AND d.reply_to=h.seq "
            "AND d.type IN ('hold.decided','hold.expired'))", (_ts(now),))]
    n = 0
    for r in rows:
        with database.write() as c:
            h = c.execute("SELECT * FROM event WHERE session_id=? AND seq=?", (r["session_id"], r["seq"])).fetchone()
            if hold_status(c, r["session_id"], r["seq"]) is not None:
                continue
            others = [x[0] for x in c.execute("SELECT principal_id FROM event_recipient WHERE session_id=? AND seq=?",
                                              (r["session_id"], r["seq"]))]
            recipients, broadcast = routing.route(c, r["session_id"], "hold.expired", ids.BOARD_PRINCIPAL_ID,
                                                  asker=h["actor_id"], resolved_for=others)
            default = json.loads(h["data"])["default"]
            events.append(c, r["session_id"], ids.BOARD_PRINCIPAL_ID, "hold.expired", recipients=recipients,
                          broadcast=broadcast, reply_to=r["seq"], task_num=h["task_num"],
                          body="Nobody decided this hold before its deadline; the stated default applies.",
                          data={"hold": r["seq"], "default": default})
            n += 1
    return n


# --- stand-down ----------------------------------------------------------------------------------------

class StanddownIn(StrictModel):
    member: str = Field(pattern=PRINCIPAL_HANDLE_RE.pattern)
    reason: str = Field(min_length=1, max_length=MAX_TEXT)


class StanddownNote(StrictModel):
    note: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)


def open_standdown(c, sid: str, principal_id: str):
    """The stand-down request for this agent that no human has lifted yet (done or not), or None."""
    return c.execute(
        "SELECT * FROM event e WHERE e.session_id=? AND e.type='standdown.requested' "
        "AND json_extract(e.data, '$.handle')=(SELECT handle FROM principal WHERE id=?) AND NOT EXISTS (SELECT 1 FROM event l WHERE "
        "l.session_id=e.session_id AND l.reply_to=e.seq AND l.type='standdown.lifted') ORDER BY e.seq DESC LIMIT 1",
        (sid, principal_id)).fetchone()


def standdown_view(c, sid: str, principal_id: str) -> dict | None:
    r = open_standdown(c, sid, principal_id)
    if r is None:
        return None
    done = c.execute("SELECT seq FROM event WHERE session_id=? AND reply_to=? AND type='standdown.done'",
                     (sid, r["seq"])).fetchone()
    by = inbox.handle_map(c, [r["actor_id"]]).get(r["actor_id"])
    return {"seq": r["seq"], "ts": r["ts"], "by": by, "reason": r["body"], "done_seq": done["seq"] if done else None}


def refuse_if_standing_down(c, sid: str, principal_id: str) -> None:
    r = open_standdown(c, sid, principal_id)
    if r is not None:
        raise ApiError(409, "standing_down", "A human asked you to stand down in this session.",
                       hint="Release your work, save a checkpoint and run `glassboard standdown done`; "
                            "a human lifts the stand-down when you may work again.",
                       details={"request_seq": r["seq"]})


def _may_direct(c, tx, agent) -> bool:
    return tx.p.kind == "human" and (tx.me["role"] == "coordinator" or agent["owner_id"] == tx.p.id)


def _agent_member(c, sid: str, handle: str):
    row = c.execute("SELECT p.id, p.handle, p.kind, p.owner_id FROM principal p JOIN member m ON m.principal_id=p.id "
                    "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (handle, sid)).fetchone()
    if row is None or row["kind"] != "agent":
        raise ApiError(404, "not_found", "No such agent in this session.")
    return row


def holdings(c, sid: str, principal_id: str) -> dict:
    claims = [r[0] for r in c.execute("SELECT num FROM task WHERE session_id=? AND claimant_id=? AND status IN "
                                      "('in_progress','blocked') ORDER BY num", (sid, principal_id))]
    leases = [r[0] for r in c.execute("SELECT resource FROM lease WHERE session_id=? AND holder_id=? ORDER BY resource",
                                      (sid, principal_id))]
    return {"claims": claims, "leases": leases}


@router.post("/sessions/{s}/standdowns", status_code=201)
def request_standdown(s: str, body: StanddownIn, key: str = Depends(idempotency_key),
                      recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                      runtime: str | None = Depends(runtime_label),
                      p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A human coordinator, or the agent's owner, asks one agent to stop and hand off."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        agent = _agent_member(c, sid, body.member)
        replay = tx.replay(key, "POST /sessions/{s}/standdowns", {"member": agent["id"], "reason": body.reason}, status=201)
        if replay is not None:
            return replay
        if not _may_direct(c, tx, agent):
            raise ApiError(403, "forbidden", "Only a human coordinator or the agent's owner may ask it to stand down.")
        require_state(tx.sess, "message")
        if open_standdown(c, sid, agent["id"]) is not None:
            raise ApiError(409, "already_requested", "This agent already has a stand-down that nobody lifted.")
        _, text = tx.event("standdown.requested", body=body.reason, data={"handle": agent["handle"]},
                           member=agent["id"],
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "member": agent["handle"]})
    return respond(text, 201)


@router.post("/sessions/{s}/standdowns/{seq}/done")
def standdown_done(s: str, seq: int, body: StanddownNote, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """The agent answers its stand-down: refused until it holds no claim or lease and has a checkpoint
    saved after the request."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        r = c.execute("SELECT * FROM event WHERE session_id=? AND seq=? AND type='standdown.requested'", (sid, seq)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", "No such stand-down request in this session.")
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/standdowns/{seq}/done", body.model_dump(exclude_none=True),
                           target={"type": "event", "id": seq})
        if replay is not None:
            return replay
        if json.loads(r["data"])["handle"] != p.handle:
            raise ApiError(403, "forbidden", "Only the agent asked to stand down may answer it.")
        require_state(tx.sess, "resolve")
        if c.execute("SELECT 1 FROM event WHERE session_id=? AND reply_to=? AND type IN ('standdown.done','standdown.lifted')",
                     (sid, seq)).fetchone():
            raise ApiError(409, "already_resolved", "This stand-down was already answered or lifted.")
        held = holdings(c, sid, p.id)
        checkpoint = c.execute("SELECT 1 FROM event WHERE session_id=? AND actor_id=? AND type='checkpoint.saved' AND seq>?",
                               (sid, p.id, seq)).fetchone() is not None
        if held["claims"] or held["leases"] or not checkpoint:
            raise ApiError(409, "standdown_incomplete", "Release your work and save a checkpoint first.",
                           hint="Release each claim (with a handoff) and lease, then `glassboard checkpoint save`.",
                           details={"claims": [f"T{n}" for n in held["claims"]], "leases": len(held["leases"]),
                                    "checkpoint_saved": checkpoint})
        _, text = tx.event("standdown.done", body=body.note, reply_to=seq, data={"request": seq}, principal=p.id,
                           receipt=lambda ev: {"session": tx.sess["handle"], "seq": ev, "request": seq})
    return respond(text, 200)


@router.post("/sessions/{s}/standdowns/{seq}/lift")
def lift_standdown(s: str, seq: int, body: StanddownNote, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A human coordinator, or the agent's owner, lets the agent work again (answered or not)."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        r = c.execute("SELECT * FROM event WHERE session_id=? AND seq=? AND type='standdown.requested'", (sid, seq)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", "No such stand-down request in this session.")
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/standdowns/{seq}/lift", body.model_dump(exclude_none=True),
                           target={"type": "event", "id": seq})
        if replay is not None:
            return replay
        agent = c.execute("SELECT id, owner_id FROM principal WHERE handle=?", (json.loads(r["data"])["handle"],)).fetchone()
        member_id = agent["id"]
        if not _may_direct(c, tx, agent):
            raise ApiError(403, "forbidden", "Only a human coordinator or the agent's owner may lift a stand-down.")
        require_state(tx.sess, "resolve")
        if c.execute("SELECT 1 FROM event WHERE session_id=? AND reply_to=? AND type='standdown.lifted'", (sid, seq)).fetchone():
            raise ApiError(409, "already_resolved", "This stand-down was already lifted.")
        _, text = tx.event("standdown.lifted", body=body.note, reply_to=seq, data={"request": seq}, member=member_id,
                           receipt=lambda ev: {"session": tx.sess["handle"], "seq": ev, "request": seq})
    return respond(text, 200)
