"""Resume brief and compaction reports (B40, ERRATA E35).

The brief is what an agent needs right after its client compacted, resumed or cleared its context: the
live board state first (claims with their checklist, whether the fence is valid for this run, owed
questions, unacknowledged inbox), then explicit conflict lines where its own latest checkpoint disagrees
with that state, then the checkpoint itself, then decisions since the checkpoint, then where to look
next. It is deterministic server text (no model, no ages: timestamps and sequence numbers only), capped
at BRIEF_CHARS so client hooks pass it to the model whole (Claude Code moves hook context over 10,000
characters to a file; Codex spills over about 2,500 tokens). Participant text stays quoted. Reading it
changes nothing.

A compaction report (`POST /sessions/{s}/context/compacted`) is sent by an opt-in client hook after the
client compacted its context. It commits one `context.compacted` event (routed to nobody; the timeline
shows it), which makes "compacted, no checkpoint since" a recorded fact. It never acts on anything."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .api_checkpoints import latest
from .api_tasks import checklist_of, progress_of, waiting_view
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import require_member, require_state

router = APIRouter(prefix="/api/v1")

BRIEF_CHARS = 6000          # well under Claude Code's 10,000-character hook limit and Codex's ~2,500 tokens
TEXT = 280                  # characters of participant text quoted per item
OPEN_ITEMS = 5              # unticked checklist items listed per claim
OWED_SHOWN = 5
DECISIONS_SHOWN = 5
INBOX_COUNT_CAP = 500       # "500+ events" past this; counting stays cheap


class Compacted(StrictModel):
    trigger: Literal["auto", "manual", "unknown"] = "unknown"
    client: Literal["claude", "codex", "other"] = "other"


def _cut(text: str | None, n: int = TEXT) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def last_compaction(c, sid: str, pid: str):
    return c.execute("SELECT seq, ts, runtime, data FROM event WHERE session_id=? AND actor_id=? "
                     "AND type='context.compacted' ORDER BY seq DESC LIMIT 1", (sid, pid)).fetchone()


def compaction_view(c, sid: str, pid: str, checkpoint_seq: int | None) -> dict | None:
    """The latest compaction report and whether a checkpoint was saved after it."""
    r = last_compaction(c, sid, pid)
    if r is None:
        return None
    data = json.loads(r["data"] or "{}")
    return {"seq": r["seq"], "ts": r["ts"], "runtime": r["runtime"], "trigger": data.get("trigger"),
            "client": data.get("client"),
            "checkpoint_since": checkpoint_seq is not None and checkpoint_seq > r["seq"]}


def _inbox_unacked(c, sess, me) -> int:
    rows = c.execute(f"SELECT COUNT(*) FROM ({inbox.INBOX_SQL})",
                     {"s": sess["id"], "cursor": me["cursor_seq"], "head": sess["head_seq"],
                      "p": me["principal_id"], "joined": me["joined_seq"], "limit": INBOX_COUNT_CAP + 1}).fetchone()
    return rows[0]


def brief(c, sess, me, handle: str, runtime: str | None, own: bool) -> dict:
    """The brief as data. `own`: the caller reads its own brief (fence validity is then shown for
    the caller's runtime)."""
    sid, pid = sess["id"], me["principal_id"]
    claims = []
    for t in c.execute("SELECT * FROM task WHERE session_id=? AND claimant_id=? AND status IN "
                       "('in_progress','blocked','review') ORDER BY num", (sid, pid)):
        items = checklist_of(t)
        held = t["fence_secret"] is not None and t["status"] in ("in_progress", "blocked")
        if not own or t["status"] == "review":
            fence = None
        elif not runtime:
            fence = "unknown"
        else:
            fence = "yes" if held and t["claimant_runtime"] == runtime else "no"
        claims.append({"task": t["num"], "title": t["title"], "status": t["status"], "gen": t["gen"],
                       "deliverable": t["deliverable"], "checklist": progress_of(items),
                       "open_items": [i["text"] for i in items if not i.get("done")][:OPEN_ITEMS],
                       "fence_for_this_run": fence,
                       "waiting_on": waiting_view(c, sid, t) if t["status"] == "blocked" else None,
                       "updated_at": t["updated_at"]})
    owed_rows = c.execute(inbox.OWED_SQL, {"s": sid, "p": pid}).fetchall()
    names = inbox.handle_map(c, [r["actor_id"] for r in owed_rows[:OWED_SHOWN]])
    owed = []
    for r in owed_rows[:OWED_SHOWN]:
        body = c.execute("SELECT body FROM event WHERE session_id=? AND seq=?", (sid, r["seq"])).fetchone()
        owed.append({"seq": r["seq"], "type": r["type"], "from": names.get(r["actor_id"], "?"), "ts": r["ts"],
                     "text": _cut(body["body"] if body else "")})
    cp = latest(c, sid, pid)
    checkpoint = None
    conflicts: list[str] = []
    if cp is not None:
        since = c.execute("SELECT COUNT(*) FROM event WHERE session_id=? AND seq>?", (sid, cp["seq"])).fetchone()[0]
        listed = json.loads(cp["task_nums"])
        checkpoint = {"n": cp["n"], "seq": cp["seq"], "saved_at": cp["created_at"], "runtime": cp["runtime"],
                      "summary": _cut(cp["summary"], 1024), "next_action": _cut(cp["next_action"], 1024),
                      "blockers": _cut(cp["blockers"], 512) if cp["blockers"] else None,
                      "task_nums": listed, "events_since": since}
        held_nums = {x["task"] for x in claims}
        for n in listed:
            t = c.execute("SELECT status, claimant_id FROM task WHERE session_id=? AND num=?", (sid, n)).fetchone()
            if t is None:
                continue
            if t["claimant_id"] != pid or t["status"] not in ("in_progress", "blocked", "review"):
                who = ""
                if t["claimant_id"] and t["claimant_id"] != pid:
                    who = f", claimed by {inbox.handle_map(c, [t['claimant_id']]).get(t['claimant_id'], '?')}"
                conflicts.append(f"checkpoint #{cp['n']} lists T{n}; T{n} is now {t['status']}{who}")
        if listed:
            for n in sorted(held_nums - set(listed)):
                conflicts.append(f"you hold T{n}; checkpoint #{cp['n']} does not list it")
    compaction = compaction_view(c, sid, pid, cp["seq"] if cp else None)
    if compaction and not compaction["checkpoint_since"]:
        conflicts.append(f"context compacted at #{compaction['seq']} ({compaction['trigger']}); "
                         + (f"checkpoint #{cp['n']} is older" if cp else "no checkpoint saved"))
    after = cp["seq"] if cp else 0
    drows = c.execute("SELECT seq, ts, actor_id, body FROM event WHERE session_id=? AND type='decision' AND seq>? "
                      "ORDER BY seq DESC LIMIT ?", (sid, after, DECISIONS_SHOWN + 1)).fetchall()
    dnames = inbox.handle_map(c, [r["actor_id"] for r in drows])
    decisions = [{"seq": r["seq"], "ts": r["ts"], "by": dnames.get(r["actor_id"], "?"), "text": _cut(r["body"])}
                 for r in reversed(drows[:DECISIONS_SHOWN])]
    from .api_holds import standdown_view
    standdown = standdown_view(c, sid, pid)
    if standdown is not None:
        standdown["reason"] = _cut(standdown["reason"] or "")
    return {"session": sess["handle"], "state": sess["state"], "member": handle, "runtime": runtime if own else None,
            "standdown": standdown,
            "role": me["role"], "job_role": me["job_role"], "as_of_seq": sess["head_seq"],
            "claims": claims, "owed": {"count": len(owed_rows), "items": owed},
            "inbox": {"unacked": _inbox_unacked(c, sess, me), "unacked_cap": INBOX_COUNT_CAP,
                      "acked_through": me["cursor_seq"], "delivered_through": me["delivered_seq"]},
            "conflicts": conflicts, "checkpoint": checkpoint, "compaction": compaction,
            "decisions": decisions, "more_decisions": len(drows) > DECISIONS_SHOWN,
            "decisions_after": after}


def brief_text(b: dict) -> str:
    """Canonical text, at most BRIEF_CHARS. Facts, not commands; participant text quoted."""
    me = b["member"] + (f"@{b['runtime']}" if b.get("runtime") else "")
    head = [render.section(f"--- resume brief · {me} · {b['session']} ({b['state']}) · as of #{b['as_of_seq']} ---"),
            render.NOTE_LINE,
            render.section("note: live board state first; the checkpoint below is this agent's own earlier note "
                           "and loses where they disagree.")]
    live: list[str] = []
    sd = b.get("standdown")
    if sd:                                          # B41: a human asked this agent to stop and hand off
        state = f"answered at #{sd['done_seq']}, waiting for a human to lift it" if sd["done_seq"] else \
            "unanswered: release every claim and lease, save a checkpoint, then answer it"
        live.append(render.section(f"--- stand-down #{sd['seq']} requested by {sd['by']}: {state} ---"))
        if sd["reason"]:
            live.append(render.inline_line("reason", sd["reason"]))
    if not b["claims"]:
        live.append(render.section("claims: none"))
    for x in b["claims"]:
        prog = f" checklist {x['checklist']['done']}/{x['checklist']['total']}" if x["checklist"] else ""
        fence = f" · fence valid for this run: {x['fence_for_this_run']}" if x["fence_for_this_run"] else ""
        state = "submitted for review" if x["status"] == "review" else x["status"]
        live.append(render.section(f"claims: T{x['task']} {state} g{x['gen']}{prog}{fence}"))
        live.append(render.inline_line("title", x["title"]))
        if x["waiting_on"]:
            live.append(render.inline_line("waiting_on", x["waiting_on"]["label"]))
        for item in x["open_items"]:
            live.append(render.inline_line("open item", item))
        if x["checklist"] and x["checklist"]["total"] - x["checklist"]["done"] > len(x["open_items"]):
            live.append(render.section(f"note: T{x['task']} has more open items: `glassboard task show T{x['task']}`"))
    for o in b["owed"]["items"]:
        what = inbox.owed_what(o["type"])
        live.append(render.section(f"owed: #{o['seq']} {what} from {o['from']} at {o['ts'][:16]}Z, unresolved"))
        if o["text"]:
            live.append(render.inline_line("text", o["text"]))
    if b["owed"]["count"] > len(b["owed"]["items"]):
        live.append(render.section(f"owed: … {b['owed']['count'] - len(b['owed']['items'])} more"))
    ib = b["inbox"]
    n = f"{ib['unacked_cap']}+" if ib["unacked"] > ib["unacked_cap"] else str(ib["unacked"])
    live.append(render.section(f"inbox: {n} event(s) after acked #{ib['acked_through']} "
                               f"(delivered through #{ib['delivered_through']})"))
    for line in b["conflicts"]:
        live.append(render.section(f"note: conflict: {line}"))
    cp = b["checkpoint"]
    if cp:
        live.append(render.section(f"checkpoint: #{cp['n']} saved {cp['saved_at'][:16]}Z at #{cp['seq']} · "
                                   f"{cp['events_since']} event(s) since"))
        live.append(render.inline_line("summary", cp["summary"]))
        live.append(render.inline_line("next_action", cp["next_action"]))
        if cp["blockers"]:
            live.append(render.inline_line("blockers", cp["blockers"]))
        if cp["task_nums"]:
            live.append(render.inline_line("tasks", ", ".join(f"T{t}" for t in cp["task_nums"])))
    else:
        live.append(render.section("checkpoint: none saved"))
    decisions: list[str] = []
    if b["decisions"]:
        since = f"since checkpoint #{cp['n']}" if cp else "latest"
        decisions.append(render.section(f"--- decisions {since}: {len(b['decisions'])}"
                                        + (" (more: `glassboard decisions`)" if b["more_decisions"] else "") + " ---"))
        for d in b["decisions"]:
            decisions.append(render.section(f"note: decision #{d['seq']} by {d['by']} at {d['ts'][:16]}Z"))
            decisions.append(render.inline_line("text", d["text"]))
    tasks = " ".join(f"`glassboard task show T{x['task']}`" for x in b["claims"][:3])
    tail = [render.section("next: `glassboard sync` (inbox), `glassboard checkpoint show` (full checkpoint), "
                           "`glassboard digest` (what changed)" + (f", {tasks}" if tasks else ""))]
    cut_note = render.section("note: brief cut to fit; `glassboard digest` and `glassboard sync` have the rest")
    out = head + live + decisions + tail
    text = "\n".join(out) + "\n"
    if len(text) <= BRIEF_CHARS:
        return text
    budget = BRIEF_CHARS - len("\n".join(tail + [cut_note])) - 2
    kept, size = [], 0
    for line in head + live + decisions:
        if size + len(line) + 1 > budget:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept + [cut_note] + tail) + "\n"


@router.get("/sessions/{s}/brief")
def get_brief(s: str, request: Request, principal: str = Query("me", max_length=80),
              format: Literal["json", "text"] = Query("json"),
              runtime: str | None = Depends(runtime_label),
              p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """Your resume brief, or (coordinators) another member's. Reading it changes nothing."""
    with database.read() as c:
        sess, me = require_member(c, s, p)
        own = principal in ("me", p.handle)
        if own:
            who, handle = me, p.handle
        else:
            if me["role"] != "coordinator":
                raise ApiError(403, "forbidden", "Only coordinators read other members' briefs.")
            row = c.execute("SELECT m.*, p.handle FROM member m JOIN principal p ON p.id=m.principal_id "
                            "WHERE m.session_id=? AND p.handle=? AND m.left_at IS NULL", (sess["id"], principal)).fetchone()
            if row is None:
                raise ApiError(404, "not_found", "No such member.")
            who, handle = row, row["handle"]
        b = brief(c, sess, who, handle, runtime, own)
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        return PlainTextResponse(brief_text(b))
    return b


@router.post("/sessions/{s}/context/compacted")
def context_compacted(s: str, body: Compacted, key: str = Depends(idempotency_key),
                      recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                      runtime: str | None = Depends(runtime_label),
                      p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Record that this member's client compacted its context (sent by an opt-in client hook). One
    `context.compacted` event, routed to nobody; it acts on nothing."""
    with session_tx(database, p, s, runtime) as tx:
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/context/compacted", body.model_dump(), status=201)
        if replay is not None:
            return replay
        require_state(tx.sess, "checkpoint")
        _, text = tx.event("context.compacted", data={"trigger": body.trigger, "client": body.client},
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq})
    return respond(text, 201)
