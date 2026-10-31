"""Waking idle agents: a durable wake mailbox (ERRATA E30, decision 0035).

A wake means "inspect the board", never "execute this message".

Generation. The wake generation of principal P is the seq of the newest event that is eligible
to wake P. It is derived from the log (never stored per event), so several arrivals coalesce into
one pending generation. P has pending demand when generation > `wake_inspected`.

Eligible events (not authored by P; broadcasts only after P joined; never a loop-flagged event):
  question   a question routed to P
  message    a message, decision or approval request written by a human, routed to P or broadcast
  answer     an answer routed to P
  task       any task event routed to P (assignment, review, return, stale, ...)
  approval   an approval grant or denial routed to P
  session    a human resuming the session
Agent broadcasts, board notes, wake events and everything else never wake anyone.

Fences. The human-armed supervisor holds a dispatch fence `W<gen>-<10 base32>`, separate from the
inbox consumer. It appears only in the arm response and its idempotent replay. One supervisor per
identity: arming while another holds the fence is 409 `wake_conflict`; an explicit takeover with a
reason is a visible event. Restores and membership changes release it (`RESET_SQL`).

Events (one per mutation): wake.armed, wake.takeover, wake.dispatched, wake.inspected, wake.paused,
wake.resumed, wake.stopped, wake.disarmed, wake.held. Waiting, reconnecting and timeouts write nothing.
[P] "wake requested" from the design is represented by `wake.dispatched` and by the eligible events
themselves: a server-written event inside another principal's mutation would break the
one-event-per-mutation rule (§4.1).

Inspection. The agent records the generation it inspected (`POST …/wake/inspected`). Inbox acks do
not count: events can arrive during a turn. Recording generation N clears demand only through N and
closes an outstanding dispatch for a generation ≤ N. Recording N ≤ the stored value is a no-op
without an event, like re-taking one's own lease.

Holds (ERRATA E31). A supervisor resumes a client headless only on positive evidence that its session
is not open anywhere on that machine. Otherwise it holds the wake (`POST …/wake/hold`, one `wake.held`
event routed to the owner and coordinators, shown in their attention queue) and keeps checking. The
same reason again writes nothing. A dispatch, an inspection that clears all demand, or any supervisor
control clears the hold.

Presence. Whether the supervisor is connected is kept in memory (`Presence`), from its own reads and
waits on the wake signal: never an event, never stored. While a dispatch is outstanding the supervisor
runs a client and does not poll, so the outstanding dispatch and its time are shown instead.
"""
from __future__ import annotations

import asyncio
import re
import secrets as pysecrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import Field, StrictInt
from starlette.concurrency import run_in_threadpool

from . import db, longpoll, sessions, tokens
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx

router = APIRouter(prefix="/api/v1")
B32 = "abcdefghijklmnopqrstuvwxyz234567"
FENCE_RE = re.compile(r"^W([0-9]{1,9})-([a-z2-7]{10})$")
ADAPTERS = ("claude", "codex", "grok", "opencode", "script")
CATEGORIES = ("question", "message", "answer", "task", "approval", "session")
MESSAGE_TYPES = ("question", "answer", "update", "proposal", "blocker", "note", "decision", "approval")

RESET_SQL = db.WAKE_RESET_SQL
CLEAR_HOLD = "wake_hold=NULL, wake_hold_seq=NULL, wake_dispatched_at=NULL"

_ELIGIBLE_SQL = f"""
SELECT category, count(*) AS n, max(seq) AS last FROM (
  SELECT e.seq,
    CASE
      WHEN e.type = 'question' AND e.direct THEN 'question'
      WHEN e.type IN ({",".join("'%s'" % t for t in MESSAGE_TYPES)}) AND pa.kind = 'human' THEN 'message'
      WHEN e.type = 'answer' AND (e.direct OR EXISTS (SELECT 1 FROM event q WHERE q.session_id = e.session_id
                                   AND q.seq = e.reply_to AND q.actor_id = :p)) THEN 'answer'   -- my question, even broadcast
      WHEN e.type LIKE 'task.%' AND e.direct THEN 'task'
      WHEN e.type IN ('approval.granted', 'approval.denied') AND e.direct THEN 'approval'
      WHEN e.type = 'session.resumed' AND pa.kind = 'human' THEN 'session'
    END AS category
  FROM (SELECT ev.*, EXISTS (SELECT 1 FROM event_recipient r WHERE r.session_id = ev.session_id
                                AND r.seq = ev.seq AND r.principal_id = :p) AS direct
        FROM event ev
        WHERE ev.session_id = :s AND ev.seq > :after AND ev.seq <= :head AND ev.actor_id != :p) e
  JOIN principal pa ON pa.id = e.actor_id
  WHERE (e.direct OR (e.broadcast = 1 AND e.seq > :joined))
    AND NOT EXISTS (SELECT 1 FROM event f WHERE f.session_id = e.session_id AND f.reply_to = e.seq
                    AND f.type = 'note' AND json_extract(f.data, '$.flag') = 'loop')
) WHERE category IS NOT NULL GROUP BY category
"""


PRESENCE_GRACE_S = 45     # between two waits (debounce, a status read) the supervisor still counts as connected


class Presence:
    """When each identity's supervisor last read or waited on its wake signal (in memory only)."""

    def __init__(self) -> None:
        self._open: dict[tuple[str, str], int] = {}
        self._last: dict[tuple[str, str], float] = {}

    def touch(self, key: tuple[str, str]) -> None:
        self._last[key] = time.monotonic()

    def enter(self, key: tuple[str, str]) -> None:
        self._open[key] = self._open.get(key, 0) + 1
        self.touch(key)

    def leave(self, key: tuple[str, str]) -> None:
        self._open[key] = self._open.get(key, 1) - 1
        self.touch(key)

    def connected(self, key: tuple[str, str]) -> bool:
        last = self._last.get(key)
        return self._open.get(key, 0) > 0 or (last is not None and time.monotonic() - last < PRESENCE_GRACE_S)


def demand(c, sess, me) -> dict:
    """Pending wake demand of `me`: generation, counts by category since the inspected generation."""
    inspected = me["wake_inspected"]
    rows = c.execute(_ELIGIBLE_SQL, {"s": sess["id"], "p": me["principal_id"], "after": inspected,
                                     "head": sess["head_seq"], "joined": me["joined_seq"]}).fetchall()
    counts = {r["category"]: r["n"] for r in rows}
    generation = max([inspected, *(r["last"] for r in rows)])
    return {"generation": generation, "inspected": inspected, "pending": generation > inspected,
            "counts": {k: counts[k] for k in CATEGORIES if k in counts}}


def status(c, sess, me, presence: Presence | None = None) -> dict:
    d = demand(c, sess, me)
    armed = me["wake_secret"] is not None
    hold = {"reason": me["wake_hold"], "seq": me["wake_hold_seq"]} if armed and me["wake_hold"] is not None else None
    connected = presence.connected((sess["id"], me["principal_id"])) if armed and presence is not None else None
    return {"session": sess["handle"], "session_state": sess["state"], **d,
            "supervisor": {"state": me["wake_state"] if armed else "none", "adapter": me["wake_adapter"],
                           "runtime": me["wake_runtime"], "since": me["wake_since"], "fence_gen": me["wake_gen"],
                           "outstanding": me["wake_outstanding"],
                           "dispatched_at": me["wake_dispatched_at"] if me["wake_outstanding"] is not None else None,
                           "hold": hold, "connected": connected}}


# --- the non-consuming wait -----------------------------------------------------------------

def _read_status(database, p: tokens.Principal, s: str, presence: Presence | None) -> tuple[str, dict]:
    with database.read() as c:
        sess, me = sessions.require_member(c, s, p)
        return sess["id"], status(c, sess, me, presence)


@router.get("/sessions/{s}/wake")
async def wake_status(s: str, request: Request, wait: int = Query(0, ge=0, le=longpoll.MAX_WAIT_S),
                      after: int | None = Query(None, ge=0), runtime: str | None = Depends(runtime_label),
                      p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """Generation, counts and categories only: no body, no acknowledgement, never the inbox
    consumer. With `wait`, returns as soon as the generation exceeds `after` (default: the
    inspected generation) or after `wait` seconds. It counts toward the two-wait limit. Reads and
    waits from the supervisor's own runtime label keep it shown as connected."""
    presence: Presence = request.app.state.wake_presence
    sid, result = await run_in_threadpool(_read_status, database, p, s, presence)
    key = (sid, p.id)
    mine = runtime is not None and runtime == result["supervisor"]["runtime"]
    if mine:
        presence.touch(key)
        result["supervisor"]["connected"] = True
    threshold = result["inspected"] if after is None else after
    if wait == 0 or result["generation"] > threshold:
        return result
    waiters: longpoll.Waiters = request.app.state.waiters
    waiter = waiters.register(sid, p.id)
    if mine:
        presence.enter(key)
    try:
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            # Re-read after registering, so a commit between the first read and the wait is not missed.
            _, result = await run_in_threadpool(_read_status, database, p, s, presence)
            left = deadline - asyncio.get_running_loop().time()
            if result["generation"] > threshold or left <= 0:
                return result
            waiter.event.clear()
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=left)
            except asyncio.TimeoutError:
                pass
    finally:
        waiters.release(waiter)
        if mine:
            presence.leave(key)


# --- supervisor and agent mutations ----------------------------------------------------------

class Takeover(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class ArmIn(StrictModel):
    adapter: Literal[ADAPTERS]
    takeover: Takeover | None = None


class FenceIn(StrictModel):
    fence: str = Field(max_length=24)


class DispatchIn(FenceIn):
    generation: StrictInt = Field(ge=1)


class ReasonIn(FenceIn):
    reason: str = Field(min_length=1, max_length=2000)


class HoldIn(FenceIn):
    reason: str = Field(min_length=1, max_length=500)


class InspectedIn(StrictModel):
    generation: StrictInt = Field(ge=0)


def _new_secret() -> str:
    return "".join(pysecrets.choice(B32) for _ in range(10))


def _require_agent(p: tokens.Principal, me) -> None:
    if p.kind not in ("agent", "client"):
        raise ApiError(403, "forbidden", "Only agents and clients are woken.")
    if me["role"] == "observer":
        raise ApiError(403, "forbidden", "Observers are not woken.")


def _check_fence(me, fence: str) -> None:
    if not FENCE_RE.match(fence):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       details={"fields": [{"loc": ["body", "fence"], "type": "string_pattern_mismatch"}]})
    current = f"W{me['wake_gen']}-{me['wake_secret']}" if me["wake_secret"] else None
    if fence != current:
        raise ApiError(409, "wake_mismatch", "This wake fence is no longer valid.",
                       hint="Another supervisor took over, the board was restored, or the supervisor was stopped.",
                       details={"wake_gen": me["wake_gen"]})


def _me(c, sid: str, pid: str):
    return c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, pid)).fetchone()


@router.post("/sessions/{s}/wake/arm")
def arm(s: str, body: ArmIn, key: str = Depends(idempotency_key),
        recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
        runtime: str | None = Depends(runtime_label),
        p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)) -> Response:
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/wake/arm", body.model_dump(), bind_runtime=True)
        if replay is not None:
            return replay
        _require_agent(p, tx.me)
        sessions.require_state(tx.sess, "wake.arm")
        if tx.me["wake_secret"] is not None and body.takeover is None:
            raise ApiError(409, "wake_conflict", "Another wake supervisor is armed for this identity.",
                           hint="Stop it first. Take over only when your human confirms it is gone.",
                           details={"runtime": tx.me["wake_runtime"], "since": tx.me["wake_since"]})
        gen, secret, now = tx.me["wake_gen"] + 1, _new_secret(), db.utcnow()
        c.execute("UPDATE member SET wake_gen=?, wake_secret=?, wake_runtime=?, wake_since=?, wake_state='armed', "
                  "wake_adapter=?, wake_outstanding=NULL, wake_hold=NULL, wake_hold_seq=NULL, wake_dispatched_at=NULL "
                  "WHERE session_id=? AND principal_id=?",
                  (gen, secret, runtime, now, body.adapter, sid, p.id))
        fence = f"W{gen}-{secret}"
        receipt = lambda seq: {"session": tx.sess["handle"], "fence": fence, "wake_gen": gen,  # noqa: E731
                               "adapter": body.adapter, "seq": seq}
        if body.takeover is not None:
            _, text = tx.event("wake.takeover", principal=p.id, receipt=receipt,
                               data={"adapter": body.adapter, "wake_gen": gen, "reason": body.takeover.reason})
        else:
            _, text = tx.event("wake.armed", receipt=receipt, data={"adapter": body.adapter, "wake_gen": gen})
    return respond(text, 200)


def _fenced(s, body, key, recovery, runtime, p, database, operation: str, state_op: str, act):
    with session_tx(database, p, s, runtime) as tx:
        tx.check_recovery(recovery)
        replay = tx.replay(key, operation, body.model_dump())
        if replay is not None:
            return replay
        _check_fence(tx.me, body.fence)
        sessions.require_state(tx.sess, state_op)
        text = act(tx)
    return respond(text, 200)


@router.post("/sessions/{s}/wake/dispatch")
def dispatch(s: str, body: DispatchIn, key: str = Depends(idempotency_key),
             recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
             runtime: str | None = Depends(runtime_label),
             p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    def act(tx):
        me = tx.me
        if me["wake_state"] != "armed":
            raise ApiError(409, "wake_paused", "Wake dispatch is paused for this identity.",
                           hint="A human resumes it with `glassboard wake resume`.")
        if me["wake_outstanding"] is not None:
            raise ApiError(409, "dispatch_outstanding", "A dispatch is still waiting for its inspection.",
                           details={"outstanding": me["wake_outstanding"]})
        d = demand(tx.conn, tx.sess, me)
        if not (me["wake_inspected"] < body.generation <= d["generation"]):
            raise ApiError(409, "nothing_pending", "There is no pending wake demand at that generation.",
                           details={"generation": d["generation"], "inspected": me["wake_inspected"]})
        tx.conn.execute("UPDATE member SET wake_outstanding=?, wake_dispatched_at=?, wake_hold=NULL, wake_hold_seq=NULL "
                        "WHERE session_id=? AND principal_id=?", (body.generation, db.utcnow(), tx.session_id, tx.p.id))
        _, text = tx.event("wake.dispatched", data={"generation": body.generation, "adapter": me["wake_adapter"]},
                           receipt=lambda seq: {"session": tx.sess["handle"], "generation": body.generation, "seq": seq})
        return text
    return _fenced(s, body, key, recovery, runtime, p, database, "POST /sessions/{s}/wake/dispatch", "wake.dispatch", act)


@router.post("/sessions/{s}/wake/hold")
def hold(s: str, body: HoldIn, key: str = Depends(idempotency_key),
         recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
         runtime: str | None = Depends(runtime_label),
         p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """The supervisor did not deliver pending demand: the session is open, or it cannot tell."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid, me = tx.conn, tx.session_id, tx.me
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/wake/hold", body.model_dump())
        if replay is not None:
            return replay
        _check_fence(me, body.fence)
        sessions.require_state(tx.sess, "wake.dispatch")
        if me["wake_state"] != "armed":
            raise ApiError(409, "wake_not_armed", "The wake supervisor is not armed.")
        d = demand(c, tx.sess, me)
        if not d["pending"]:
            raise ApiError(409, "nothing_pending", "There is no pending wake demand.",
                           details={"generation": d["generation"], "inspected": me["wake_inspected"]})
        if me["wake_hold"] == body.reason:
            return status(c, tx.sess, me)                               # the same hold again: no event
        seq, text = tx.event("wake.held", principal=p.id,
                             data={"adapter": me["wake_adapter"], "generation": d["generation"], "reason": body.reason},
                             receipt=lambda seq: {"session": tx.sess["handle"], "generation": d["generation"], "seq": seq})
        c.execute("UPDATE member SET wake_hold=?, wake_hold_seq=? WHERE session_id=? AND principal_id=?",
                  (body.reason, seq, sid, p.id))
    return respond(text, 200)


@router.post("/sessions/{s}/wake/inspected")
def inspected(s: str, body: InspectedIn, key: str = Depends(idempotency_key),
              recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
              runtime: str | None = Depends(runtime_label),
              p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/wake/inspected", body.model_dump())
        if replay is not None:
            return replay
        _require_agent(p, tx.me)
        sessions.require_state(tx.sess, "wake.inspect")
        d = demand(c, tx.sess, tx.me)
        if body.generation > d["generation"]:
            raise ApiError(409, "generation_ahead", "That generation has not happened yet.",
                           details={"generation": d["generation"]})
        if body.generation <= tx.me["wake_inspected"]:
            return status(c, tx.sess, tx.me)                       # nothing new: no event
        outstanding = tx.me["wake_outstanding"]
        clears = outstanding is not None and outstanding <= body.generation
        hold_ends = body.generation >= d["generation"]                  # nothing left to deliver
        c.execute("UPDATE member SET wake_inspected=?, wake_outstanding=?, wake_dispatched_at=?, wake_hold=?, wake_hold_seq=? "
                  "WHERE session_id=? AND principal_id=?",
                  (body.generation, None if clears else outstanding, None if clears else tx.me["wake_dispatched_at"],
                   None if hold_ends else tx.me["wake_hold"], None if hold_ends else tx.me["wake_hold_seq"], sid, p.id))
        _, text = tx.event("wake.inspected", data={"generation": body.generation},
                           receipt=lambda seq: {"session": tx.sess["handle"], "generation": body.generation, "seq": seq})
    return respond(text, 200)


def _control(op: str, new_state: str | None, event: str, routed: bool, needs: str | None):
    def handler(s: str, body: ReasonIn | FenceIn, key: str, recovery, runtime, p, database):
        def act(tx):
            me = tx.me
            if needs is not None and me["wake_state"] != needs:
                raise ApiError(409, f"wake_not_{needs}", f"The wake supervisor is not {needs}.")
            if new_state is None:                                   # stop / disarm release the fence
                tx.conn.execute("UPDATE member SET wake_secret=NULL, wake_state=NULL, wake_outstanding=NULL, " + CLEAR_HOLD +
                                " WHERE session_id=? AND principal_id=?", (tx.session_id, tx.p.id))
            else:
                tx.conn.execute("UPDATE member SET wake_state=?, wake_outstanding=NULL, " + CLEAR_HOLD +
                                " WHERE session_id=? AND principal_id=?", (new_state, tx.session_id, tx.p.id))
            data = {"adapter": me["wake_adapter"]}
            if isinstance(body, ReasonIn):
                data["reason"] = body.reason
            route = {"principal": tx.p.id} if routed else {}
            _, text = tx.event(event, data=data, **route,
                               receipt=lambda seq: {"session": tx.sess["handle"], "state": new_state or "none", "seq": seq})
            return text
        return _fenced(s, body, key, recovery, runtime, p, database, f"POST /sessions/{{s}}/wake/{op}", "wake.control", act)
    return handler


_pause = _control("pause", "paused", "wake.paused", True, "armed")
_resume = _control("resume", "armed", "wake.resumed", False, "paused")
_stop = _control("stop", None, "wake.stopped", True, None)
_disarm = _control("disarm", None, "wake.disarmed", False, None)


@router.post("/sessions/{s}/wake/pause")
def pause(s: str, body: ReasonIn, key: str = Depends(idempotency_key),
          recovery: str | None = Header(None, alias="X-Glassboard-Recovery"), runtime: str | None = Depends(runtime_label),
          p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return _pause(s, body, key, recovery, runtime, p, database)


@router.post("/sessions/{s}/wake/resume")
def resume(s: str, body: ReasonIn, key: str = Depends(idempotency_key),
           recovery: str | None = Header(None, alias="X-Glassboard-Recovery"), runtime: str | None = Depends(runtime_label),
           p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return _resume(s, body, key, recovery, runtime, p, database)


@router.post("/sessions/{s}/wake/stop")
def stop(s: str, body: ReasonIn, key: str = Depends(idempotency_key),
         recovery: str | None = Header(None, alias="X-Glassboard-Recovery"), runtime: str | None = Depends(runtime_label),
         p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return _stop(s, body, key, recovery, runtime, p, database)


@router.post("/sessions/{s}/wake/disarm")
def disarm(s: str, body: FenceIn, key: str = Depends(idempotency_key),
           recovery: str | None = Header(None, alias="X-Glassboard-Recovery"), runtime: str | None = Depends(runtime_label),
           p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return _disarm(s, body, key, recovery, runtime, p, database)
