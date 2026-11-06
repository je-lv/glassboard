"""A runtime that exits while it still holds work (B37, ERRATA E34).

`glassboard run -- CLIENT` stays alive as the client's parent. When the client exits, it reports the
exit once per session where that runtime may hold something. The board writes one `runtime.exited`
event, routed to the agent's human and the session's coordinators, only when the runtime still holds
a task claim (a valid fence), the inbox consumer, or the identity still holds leases. It reports;
it never releases, reassigns or takes anything over (staleness is reported, never acted on, §7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import Field, StrictInt

from . import db, tokens
from .deps import get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx

router = APIRouter(prefix="/api/v1")


class RuntimeExit(StrictModel):
    exit_code: StrictInt | None = Field(default=None, ge=-255, le=255)
    signal: StrictInt | None = Field(default=None, ge=1, le=64)


def holdings(c, session_id: str, principal_id: str, runtime: str) -> dict:
    claims = [{"task": r["num"], "gen": r["gen"]} for r in c.execute(
        "SELECT num, gen FROM task WHERE session_id=? AND claimant_id=? AND claimant_runtime=? "
        "AND fence_secret IS NOT NULL AND status IN ('in_progress','blocked') ORDER BY num",
        (session_id, principal_id, runtime))]
    consumer = c.execute("SELECT 1 FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL "
                         "AND consumer_secret IS NOT NULL AND consumer_runtime=?",
                         (session_id, principal_id, runtime)).fetchone() is not None
    leases = [r[0] for r in c.execute("SELECT resource FROM lease WHERE session_id=? AND holder_id=? ORDER BY resource",
                                      (session_id, principal_id))]
    return {"claims": claims, "inbox_consumer": consumer, "leases": leases}


def holding_text(h: dict) -> str:
    parts = []
    if h["claims"]:
        parts.append("claims " + ", ".join(f"T{x['task']} g{x['gen']}" for x in h["claims"]))
    if h["leases"]:
        parts.append("leases " + ", ".join(h["leases"]))
    if h["inbox_consumer"]:
        parts.append("the inbox consumer")
    return "; ".join(parts)


@router.post("/sessions/{s}/runtime/exited")
def runtime_exited(s: str, body: RuntimeExit, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Report that this runtime's client process exited. One visible event if it still holds work;
    nothing otherwise. Never releases or reassigns anything."""
    with session_tx(database, p, s, runtime) as tx:
        tx.check_recovery(recovery)
        if not runtime:
            raise ApiError(422, "invalid_request", "Name the runtime that exited (X-Glassboard-Runtime).")
        replay = tx.replay(key, "POST /sessions/{s}/runtime/exited", body.model_dump(), bind_runtime=True)
        if replay is not None:
            return replay
        h = holdings(tx.conn, tx.session_id, p.id, runtime)
        if not (h["claims"] or h["leases"] or h["inbox_consumer"]):
            return {"session": tx.sess["handle"], "seq": None, "runtime": runtime, "holding": h}
        exited = (f"signal {body.signal}" if body.signal else
                  f"exit code {body.exit_code}" if body.exit_code is not None else "unknown")
        _, text = tx.event("runtime.exited", principal=p.id,
                           data={"exited": exited, "holding": holding_text(h)},
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "runtime": runtime,
                                                "holding": h})
    return respond(text, 200)
