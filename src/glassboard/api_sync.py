"""POST /sessions/{s}/sync: inbox delivery with a single consumer.

There is no GET /sync (405): acknowledgement is a state change and only travels in a POST body.

One transaction (TX1):
  membership (404) → recovery (428/409) →
  consumer: attach (no token, none attached) | 409 consumer_conflict (no token, one attached) |
            takeover (reason) | validate token (409 consumer_mismatch) →
  ack: ack_through > delivered_seq → 409 ack_beyond_delivered; else cursor = max(cursor, ack) →
  page = inbox after cursor (limit) → delivered_through = more ? last(page) : head →
  delivered_seq = max(delivered_seq, delivered_through); last_seen_at = now.
On attach/takeover `delivered_seq := cursor_seq` first, and `ack_through` is refused (422).
`wait` (long-poll): see longpoll.py; TX2 re-validates after the wait.

Secrets: the full consumer token `C<gen>-<10 base32>` appears only in the attach/takeover
response and its idempotent replay; every other response shows `C<gen>`. A replayed attach or
takeover returns the consumer fields from its receipt and a fresh page; if that consumer is no
longer the current one, the page is empty and nothing is delivered.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets as pysecrets

from fastapi import APIRouter, Depends, Header, Query, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import Field, StrictBool, StrictInt

from . import api_checkpoints, api_tasks, db, idempotency, inbox, longpoll, sessions, tokens
from .textviews import sync_text
from .deps import get_db, require_full_scope
from .errors import ApiError, AsciiJSONResponse
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import require_state

router = APIRouter(prefix="/api/v1")
B32 = "abcdefghijklmnopqrstuvwxyz234567"
CONSUMER_RE = re.compile(r"^C([0-9]{1,9})-([a-z2-7]{10})$")
SECRET_CHARS = 10        # 50 bits


class Takeover(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class SyncIn(StrictModel):
    consumer: str | None = Field(default=None, max_length=24)
    takeover: Takeover | None = None
    ack_through: StrictInt | None = Field(default=None, ge=0)
    wait: StrictInt = Field(default=0, ge=0, le=25)
    limit: StrictInt = Field(default=50, ge=1, le=100)
    include_checkpoint: StrictBool = False


def new_secret() -> str:
    return "".join(pysecrets.choice(B32) for _ in range(SECRET_CHARS))


def _fail(field: str, typ: str) -> ApiError:
    return ApiError(422, "invalid_request", "The request failed validation.",
                    details={"fields": [{"loc": field.split("."), "type": typ}]})


def _result(c, sess, me, p: tokens.Principal, consumer_shown: str, consumer_gen: int, delivered_through: int,
            page, more: bool, full_checkpoint: bool) -> dict:
    owed, owed_total = inbox.owed(c, sess["id"], p.id)
    cp_n, cp = api_checkpoints.sync_block(c, sess, p.id, p.handle, full=full_checkpoint)
    return {
        "me": p.handle, "session": sess["handle"], "state": sess["state"], "recovery_id": sess["recovery_id"],
        "epoch": sess["epoch"], "consumer": consumer_shown, "consumer_gen": consumer_gen,
        "head": sess["head_seq"], "cursor": me["cursor_seq"], "delivered_through": delivered_through,
        "claims": api_tasks.claims_of(c, sess, p.id), "owed": owed, "owed_total": owed_total,
        "checkpoint_n": cp_n, "checkpoint": cp, "inbox": inbox.views(c, page), "more": more,
    }


def _deliver(c, sess, me, p: tokens.Principal, limit: int, now: str):
    page, more = inbox.inbox_page(c, sess, me, me["cursor_seq"], limit)
    delivered_through = page[-1]["seq"] if more else sess["head_seq"]
    c.execute("UPDATE member SET delivered_seq=MAX(delivered_seq, ?), last_seen_at=? WHERE session_id=? AND principal_id=?",
              (delivered_through, now, sess["id"], p.id))
    return page, more, delivered_through


def _tx1(database, p: tokens.Principal, s: str, runtime, body: SyncIn, idem, recovery, minting: bool):
    """TX1: consumer attach / takeover / validation, ack, first page. Returns
    (result, replayed, token for TX2, delivered?)."""
    prior = None
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        me = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, p.id)).fetchone()
        now = db.utcnow()
        minted_token, deliver, pending_event = None, True, None

        if minting:
            takeover = body.takeover is not None
            op = "POST /sessions/{s}/sync#" + ("takeover" if takeover else "attach")
            prior = tx.replay(idem, op, {"takeover_reason": body.takeover.reason if takeover else None},
                              bind_runtime=True)
            if prior is not None:                  # consumer fields from the receipt, the rest fresh
                rec = json.loads(prior.body)
                minted_token = rec["consumer"]
                deliver = me["consumer_secret"] is not None and f"C{me['consumer_gen']}-{me['consumer_secret']}" == minted_token
            else:
                require_state(tx.sess, "sync.attach")
                if not takeover and me["consumer_secret"] is not None:
                    raise ApiError(409, "consumer_conflict", "Another runtime of yours is attached to this inbox.",
                                   hint='If it is gone for good, take over: glassboard sync --takeover "<reason>"',
                                   details={"runtime": me["consumer_runtime"], "consumer_gen": me["consumer_gen"],
                                            "consumer_since": me["consumer_since"], "last_seen_at": me["last_seen_at"]})
                gen, secret = me["consumer_gen"] + 1, new_secret()
                minted_token = f"C{gen}-{secret}"
                c.execute("UPDATE member SET consumer_gen=?, consumer_secret=?, consumer_runtime=?, consumer_since=?, "
                          "delivered_seq=cursor_seq WHERE session_id=? AND principal_id=?",
                          (gen, secret, runtime, now, sid, p.id))
                # The consumer event is appended after the page is scanned (below), so
                # delivered_through is the head before it.
                prev_runtime = me["consumer_runtime"]
                pending_event = ("consumer.takeover", gen, prev_runtime) if takeover else ("consumer.attached", gen, None)
        else:
            current = f"C{me['consumer_gen']}-{me['consumer_secret']}" if me["consumer_secret"] else None
            if body.consumer != current:
                raise _consumer_mismatch(me)
            if body.ack_through is not None:
                if body.ack_through > me["delivered_seq"]:
                    raise ApiError(409, "ack_beyond_delivered", "You cannot acknowledge events you were not given.",
                                   hint="Acknowledge up to delivered_through from your last sync.",
                                   details={"delivered_through": me["delivered_seq"]})
                c.execute("UPDATE member SET cursor_seq=MAX(cursor_seq, ?) WHERE session_id=? AND principal_id=?",
                          (body.ack_through, sid, p.id))

        me = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, p.id)).fetchone()
        sess = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        if deliver:
            page, more, delivered_through = _deliver(c, sess, me, p, body.limit, now)
        else:
            page, more, delivered_through = [], False, me["cursor_seq"]
        if pending_event is not None:
            etype, gen, prev_runtime = pending_event
            receipt = lambda seq: {"consumer": minted_token, "consumer_gen": gen}  # noqa: E731
            if etype == "consumer.takeover":
                tx.event(etype, body=body.takeover.reason, principal=p.id, capped=False, receipt=receipt,
                         data={"consumer_gen": gen, "previous_runtime": prev_runtime})
            else:
                tx.event(etype, capped=False, data={"consumer_gen": gen}, receipt=receipt)
            sess = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        me = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, p.id)).fetchone()
        token = minted_token if minting else body.consumer
        shown = minted_token if minting else f"C{me['consumer_gen']}"
        gen_shown = int(minted_token.split("-")[0][1:]) if minting else me["consumer_gen"]
        result = _result(c, sess, me, p, shown, gen_shown, delivered_through, page, more,
                         full_checkpoint=minting or body.include_checkpoint)
    return result, prior is not None, token, deliver


def _tx2(database, p: tokens.Principal, s: str, runtime, recovery, token: str, shown: str, limit: int, full_cp: bool):
    """TX2 after a wait: re-validate membership, recovery and the consumer; recompute
    the page and the delivered boundary. The ack of the request was applied in TX1 and is not
    applied again."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        me = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, p.id)).fetchone()
        current = f"C{me['consumer_gen']}-{me['consumer_secret']}" if me["consumer_secret"] else None
        if token != current:
            raise _consumer_mismatch(me)                  # superseded while waiting; nothing delivered
        sess = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        page, more, delivered_through = _deliver(c, sess, me, p, limit, db.utcnow())
        me = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, p.id)).fetchone()
        return _result(c, sess, me, p, shown, me["consumer_gen"], delivered_through, page, more, full_cp)


def _consumer_mismatch(me) -> ApiError:
    return ApiError(409, "consumer_mismatch", "This consumer token is no longer valid.",
                    hint="Another runtime took over, or the board was restored. Sync without a token to see who holds the inbox.",
                    details={"consumer_gen": me["consumer_gen"]})


def _session_id(database, p: tokens.Principal, s: str) -> str:
    with database.read() as c:
        sess, _ = sessions.require_member(c, s, p)
        return sess["id"]


@router.post("/sessions/{s}/sync")
async def sync(s: str, body: SyncIn, request: Request,
               idem: str | None = Header(None, alias="Idempotency-Key"),
               recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
               runtime: str | None = Depends(runtime_label), format: str | None = Query(None, max_length=8),
               p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    minting = body.consumer is None              # attach, or takeover (which also carries no token)
    if body.takeover is not None and body.consumer is not None:
        raise _fail("body.consumer", "takeover_carries_no_token")
    if minting and body.ack_through is not None:
        raise _fail("body.ack_through", "not_allowed_on_attach")
    if body.consumer is not None and not CONSUMER_RE.match(body.consumer):
        raise _fail("body.consumer", "string_pattern_mismatch")
    if minting:
        if idem is None:
            raise _fail("header.idempotency-key", "missing")
        idempotency.idempotency_key(idem)                                # format check

    waiters: longpoll.Waiters = request.app.state.waiters
    waiter = None
    if body.wait > 0:
        # Registered BEFORE TX1, so a commit between TX1 and the wait still wakes us.
        waiter = waiters.register(await run_in_threadpool(_session_id, database, p, s), p.id)
    try:
        result, replayed, token, deliver = await run_in_threadpool(_tx1, database, p, s, runtime, body, idem,
                                                                   recovery, minting)
        if waiter is not None and deliver and not result["inbox"]:
            try:                                          # no lock, no transaction while waiting
                await asyncio.wait_for(waiter.event.wait(), timeout=body.wait)
            except asyncio.TimeoutError:
                pass
            result = await run_in_threadpool(_tx2, database, p, s, runtime, recovery, token, result["consumer"],
                                             body.limit, minting or body.include_checkpoint)
    finally:
        if waiter is not None:
            waiters.release(waiter)
    headers = {idempotency.REPLAY_HEADER: "true"} if (minting and replayed) else None
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        resp = PlainTextResponse(sync_text(result, runtime) + "\n", headers=headers)
    else:
        resp = AsciiJSONResponse(result, headers=headers)
    request.app.state.metrics.sync_response(len(resp.body))      # sync_bytes metric: token-cost proxy
    return resp
