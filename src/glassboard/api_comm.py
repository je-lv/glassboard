"""Read-only communication views: a digest of what changed since a point (by default the caller's
acknowledged cursor), the decision log, and each member's latest stand-up. None of them moves a
cursor; all have canonical text forms (server values on Section lines, participant text quoted)."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .api_tasks import _age_s
from .deps import authenticate, get_db
from .sessions import require_member
from .textviews import view_lines

router = APIRouter(prefix="/api/v1")
DIGEST_SCAN = 5000                       # events summarised per digest; beyond that it says "N more"
TEXT_CHARS = 200
TASK_VERBS = {"task.created": "created", "task.edited": "edited", "task.acknowledged": "acknowledged", "task.claimed": "claimed", "task.note": "note",
              "task.checked": "checked", "task.blocked": "blocked", "task.unblocked": "unblocked",
              "task.submitted": "submitted", "task.released": "released", "task.reclaimed": "reclaimed",
              "task.accepted": "accepted", "task.returned": "returned", "task.reassigned": "reassigned",
              "task.cancelled": "cancelled", "task.reopened": "reopened", "task.stale": "stale"}


def _wants_text(request: Request, format: str) -> bool:
    return format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain")


def digest(c, sess, me, after: int | None) -> dict:
    after = me["cursor_seq"] if after is None else after
    rows = c.execute("SELECT * FROM event WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                     (sess["id"], after, DIGEST_SCAN + 1)).fetchall()
    more = len(rows) > DIGEST_SCAN
    rows = rows[:DIGEST_SCAN]
    h = inbox.handle_map(c, [r["actor_id"] for r in rows])
    tasks: dict[int, dict] = {}
    asked, resolved, decisions, members, states, flags = [], [], [], [], [], []
    for r in rows:
        t, data = r["type"], json.loads(r["data"]) if r["data"] else {}
        if t in TASK_VERBS and r["task_num"] is not None:
            e = tasks.setdefault(r["task_num"], {"task": r["task_num"], "steps": []})
            verb = TASK_VERBS[t]
            if e["steps"] and e["steps"][-1][0] == verb:
                e["steps"][-1][1] += 1
            else:
                e["steps"].append([verb, 1])
        elif t == "question" or t == "approval":
            asked.append({"seq": r["seq"], "from": h.get(r["actor_id"]), "text": (r["body"] or "")[:TEXT_CHARS]})
        elif t in ("answer", "question.resolved") and r["reply_to"]:
            resolved.append(r["reply_to"])
        elif t == "decision":
            decisions.append({"seq": r["seq"], "by": h.get(r["actor_id"]), "text": (r["body"] or "")[:TEXT_CHARS]})
        elif t in ("member.added", "member.removed", "member.changed"):
            members.append({"seq": r["seq"], "type": t, "handle": data.get("handle")})
        elif t.startswith("session.") or t == "system.restored":
            states.append({"seq": r["seq"], "type": t})
        elif t == "note" and data.get("flag"):
            flags.append({"seq": r["seq"], "flag": data["flag"]})
    for n, e in tasks.items():
        row = c.execute("SELECT title, status FROM task WHERE session_id=? AND num=?", (sess["id"], n)).fetchone()
        e.update(title=row["title"] if row else "", status=row["status"] if row else "?")
    return {"session": sess["handle"], "after": after, "through": rows[-1]["seq"] if rows else after,
            "head": sess["head_seq"], "events": len(rows), "more": more,
            "tasks": [tasks[k] for k in sorted(tasks)], "asked": asked, "answered": sorted(set(resolved)),
            "decisions": decisions, "members": members, "states": states, "flags": flags}


def digest_text(d: dict) -> str:
    lines = [render.section(f"--- digest · {d['session']} · after #{d['after']} · {d['events']} event(s) "
                            f"through #{d['through']} · head #{d['head']} ---")]
    if not d["events"]:
        lines.append(render.section("--- nothing new ---"))
    for t in d["tasks"]:
        steps = ", ".join(v if n == 1 else f"{v} ×{n}" for v, n in t["steps"])
        lines += [render.section(f"--- T{t['task']} · now {t['status']} · {steps} ---"), render.inline_line("title", t["title"])]
    for q in d["asked"]:
        state = "answered" if q["seq"] in d["answered"] else "open"
        lines += [render.section(f"--- asked #{q['seq']} by {q['from']} · {state} ---"), render.inline_line("text", q["text"])]
    for x in d["decisions"]:
        lines += [render.section(f"--- decision #{x['seq']} by {x['by']} ---"), render.inline_line("text", x["text"])]
    for m in d["members"]:
        lines += [render.section(f"--- {m['type']} #{m['seq']} ---"), render.inline_line("handle", m["handle"] or "")]
    for st in d["states"]:
        lines.append(render.section(f"--- {st['type']} #{st['seq']} ---"))
    for f in d["flags"]:
        lines.append(render.section(f"--- flag #{f['seq']}: {f['flag']} ---"))
    if d["more"] or d["through"] < d["head"]:
        lines.append(render.section(f"next: more after #{d['through']} (`after={d['through']}`)"))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/digest")
def session_digest(s: str, request: Request, after: int | None = Query(None, ge=0),
                   format: Literal["json", "text"] = Query("json"),
                   p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, me = require_member(c, s, p)
        d = digest(c, sess, me, after)
    return PlainTextResponse(digest_text(d)) if _wants_text(request, format) else d


@router.get("/sessions/{s}/decisions")
def decision_log(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                 p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """Every decision, oldest first, with the number of replies it got."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        rows = c.execute("SELECT * FROM event WHERE session_id=? AND type='decision' ORDER BY seq", (sess["id"],)).fetchall()
        views = inbox.views(c, rows)
        replies = {r[0]: r[1] for r in c.execute("SELECT reply_to, count(*) FROM event WHERE session_id=? AND reply_to IN "
                                                  "(SELECT seq FROM event WHERE session_id=? AND type='decision') GROUP BY reply_to",
                                                  (sess["id"], sess["id"]))}
    for v in views:
        v["replies"] = replies.get(v["seq"], 0)
    if _wants_text(request, format):
        lines = [render.section(f"--- decisions · {sess['handle']} · {len(views)} ---")]
        for v in views:
            lines += view_lines(v)
            if v["replies"]:
                lines.append(render.section(f"--- {v['replies']} repl{'y' if v['replies'] == 1 else 'ies'}: history --thread {v['seq']} ---"))
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"session": sess["handle"], "decisions": views}


def standups(c, sess) -> list[dict]:
    rows = c.execute(
        "SELECT e.* FROM event e WHERE e.session_id=? AND e.type='update' AND json_extract(e.data, '$.standup') IS NOT NULL "
        "AND e.seq = (SELECT MAX(x.seq) FROM event x WHERE x.session_id=e.session_id AND x.actor_id=e.actor_id "
        "AND x.type='update' AND json_extract(x.data, '$.standup') IS NOT NULL) ORDER BY e.seq", (sess["id"],)).fetchall()
    h = inbox.handle_map(c, [r["actor_id"] for r in rows])
    return [{"member": h.get(r["actor_id"]), "seq": r["seq"], "age_s": _age_s(r["ts"]),
             **json.loads(r["data"])["standup"]} for r in rows]


@router.get("/sessions/{s}/standup")
def standup_view(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                 p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        su = standups(c, sess)
    if _wants_text(request, format):
        lines = [render.section(f"--- stand-up · {sess['handle']} · {len(su)} member(s) ---")]
        for x in su:
            lines.append(render.section(f"--- {x['member']} · #{x['seq']} · {x['age_s'] // 60}m ago ---"))
            lines += [render.inline_line(k, x[k]) for k in ("done", "doing", "blocked") if x.get(k)]
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"session": sess["handle"], "standups": su}
