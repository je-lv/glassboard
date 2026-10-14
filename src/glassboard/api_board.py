"""Read-only views for supervising work: the session board (kanban), a human's attention queue across
sessions, and a one-line-per-session overview. Nothing here changes state or moves a cursor.

Text forms are canonical: server values on Section lines, participant text (titles, notes, proposal
bodies) only on Inline lines, so the views are safe to show to agents and humans alike.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .textviews import task_head
from .api_tasks import is_stale, task_view, assignment_view, _age_s
from .deps import authenticate, get_db
from .sessions import require_member

router = APIRouter(prefix="/api/v1")
COLUMNS = ("open", "in_progress", "blocked", "review", "done")
DONE_SHOWN = 10
NOTE_CHARS = 200


def _wants_text(request: Request, format: str) -> bool:
    return format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain")


def _last_notes(c, sid: str) -> dict[int, dict]:
    """The latest task.note per task: who, when, the first NOTE_CHARS characters."""
    rows = c.execute("SELECT e.task_num, e.actor_id, e.ts, e.body FROM event e WHERE e.session_id=? AND e.type='task.note' "
                     "AND e.seq = (SELECT MAX(seq) FROM event x WHERE x.session_id=e.session_id AND x.task_num=e.task_num "
                     "AND x.type='task.note')", (sid,)).fetchall()
    h = inbox.handle_map(c, [r["actor_id"] for r in rows])
    return {r["task_num"]: {"by": h.get(r["actor_id"]), "age_s": _age_s(r["ts"]), "text": (r["body"] or "")[:NOTE_CHARS]}
            for r in rows}


def board(c, sess) -> dict:
    tasks = c.execute("SELECT * FROM task WHERE session_id=? ORDER BY num", (sess["id"],)).fetchall()
    notes = _last_notes(c, sess["id"])
    cols: dict[str, list] = {k: [] for k in COLUMNS}
    cancelled = 0
    for t in tasks:
        if t["status"] == "cancelled":
            cancelled += 1
            continue
        cols[t["status"]].append({**task_view(c, t, sess), "last_note": notes.get(t["num"])})
    from .api_tasks import PRIORITY_RANK
    for k in ("open", "in_progress", "blocked", "review"):        # most urgent first, then by number
        cols[k].sort(key=lambda v: (PRIORITY_RANK[v["priority"]], v["task"]))
    done_total = len(cols["done"])
    cols["done"] = sorted(cols["done"], key=lambda v: v["updated_at"], reverse=True)[:DONE_SHOWN]
    unassigned_roles = [r["handle"] for r in c.execute(
        "SELECT p.handle FROM member m JOIN principal p ON p.id=m.principal_id "
        "WHERE m.session_id=? AND m.left_at IS NULL AND p.kind='agent' AND m.job_role IS NULL ORDER BY p.handle",
        (sess["id"],))]
    independent_children = [t["num"] for t in tasks if t["parent"] is not None and t["status"] in ("open", "in_progress", "blocked")
                            and not json.loads(t["depends_on"] or "[]")]
    return {"session": sess["handle"], "state": sess["state"],
            "columns": [{"status": k, "tasks": cols[k]} for k in COLUMNS],
            "counts": {**{k: (done_total if k == "done" else len(cols[k])) for k in COLUMNS}, "cancelled": cancelled},
            "planning": {"agents_without_project_role": unassigned_roles,
                         "child_tasks_without_dependencies": independent_children}}


def board_text(b: dict) -> str:
    counts = " · ".join(f"{k.replace('_', ' ')} {n}" for k, n in b["counts"].items())
    lines = [render.section(f"--- board · {b['session']} ({b['state']}) · {counts} ---")]
    if b["planning"]["agents_without_project_role"]:
        lines.append(render.inline_line("roles to record", ", ".join("@" + h for h in b["planning"]["agents_without_project_role"])))
    if b["planning"]["child_tasks_without_dependencies"]:
        lines.append(render.inline_line("independent tasks", ", ".join("T" + str(n) for n in b["planning"]["child_tasks_without_dependencies"])))
    for col in b["columns"]:
        n = b["counts"][col["status"]]
        shown = f"last {len(col['tasks'])} of {n}" if col["status"] == "done" and n > len(col["tasks"]) else str(n)
        lines.append(render.section(f"--- {col['status'].replace('_', ' ')} ({shown}) ---"))
        for v in col["tasks"]:
            lines += [task_head(v), render.inline_line("title", v["title"])]
            if v.get("last_note"):
                ln = v["last_note"]
                lines.append(render.inline_line("last note", f"{ln['by']}, {ln['age_s'] // 60}m ago: {ln['text']}"))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/board")
def session_board(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                  p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        b = board(c, sess)
    return PlainTextResponse(board_text(b)) if _wants_text(request, format) else b


# --- across sessions ----------------------------------------------------------------------------------

def _my_sessions(c, pid: str):
    return c.execute("SELECT s.*, m.role FROM member m JOIN session s ON s.id=m.session_id "
                     "WHERE m.principal_id=? AND m.left_at IS NULL AND s.state != 'archived' ORDER BY s.handle", (pid,)).fetchall()


def attention(c, p: tokens.Principal) -> dict:
    """What needs this principal now. Coordinator items only for humans (agents never coordinate)."""
    items: list[dict] = []
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for sess in _my_sessions(c, p.id):
        sid, sh = sess["id"], sess["handle"]
        coord = p.kind == "human" and sess["role"] == "coordinator"
        if coord:
            for t in c.execute("SELECT * FROM task WHERE session_id=? AND status='review' ORDER BY updated_at", (sid,)):
                items.append({"kind": "review", "session": sh, "task": t["num"], "title": t["title"],
                              "age_s": _age_s(t["updated_at"])})
        for t in c.execute("SELECT * FROM task WHERE session_id=? AND status='review' AND reviewer_id=?", (sid, p.id)):
            done = c.execute("SELECT 1 FROM event WHERE session_id=? AND task_num=? AND type='task.reviewed' AND actor_id=? "
                             "AND seq > (SELECT COALESCE(MAX(seq), 0) FROM event WHERE session_id=? AND task_num=? "
                             "AND type='task.submitted')", (sid, t["num"], p.id, sid, t["num"])).fetchone()
            if not done:
                items.append({"kind": "review_requested", "session": sh, "task": t["num"], "title": t["title"],
                              "age_s": _age_s(t["updated_at"])})
        owed, _ = inbox.owed(c, sid, p.id, show=50)
        for o in owed:
            item = {"kind": "approval" if o["type"] == "approval" else "owed", "session": sh, "seq": o["seq"],
                    "from": o["from"], "age_s": o["age_s"]}
            row = c.execute("SELECT body FROM event WHERE session_id=? AND seq=?", (sid, o["seq"])).fetchone()
            item["text"] = (row["body"] or "")[:NOTE_CHARS]               # what is being asked, quoted
            items.append(item)
        for t in c.execute("SELECT * FROM task WHERE session_id=? AND status='blocked' AND waiting_on IS NOT NULL", (sid,)):
            w = json.loads(t["waiting_on"])
            if w["kind"] == "member" and w["ref"] == p.id:
                items.append({"kind": "waiting_on_you", "session": sh, "task": t["num"], "title": t["title"],
                              "age_s": _age_s(t["updated_at"])})
        if coord:
            for t in c.execute("SELECT * FROM task WHERE session_id=? AND status IN ('in_progress','blocked')", (sid,)):
                if is_stale(t, sess):
                    items.append({"kind": "stale", "session": sh, "task": t["num"], "title": t["title"],
                                  "age_s": _age_s(t["last_activity_at"])})
        if sess["role"] == "coordinator":
            for t in c.execute("SELECT t.*, p.handle AS assignee_handle FROM task t "
                               "JOIN principal p ON p.id=t.assignee_id "
                               "WHERE t.session_id=? AND t.status='open'", (sid,)):
                assignment = assignment_view(c, sid, t)
                if assignment["ack_overdue"]:
                    items.append({"kind": "unacknowledged", "session": sh, "task": t["num"],
                                  "title": t["title"], "age_s": assignment["assignment_age_s"],
                                  "assignee": t["assignee_handle"],
                                  "assignee_synced_since_assignment": assignment["assignee_synced_since_assignment"]})
        if coord:
            rows = c.execute(
                "SELECT e.* FROM event e WHERE e.session_id=? AND e.type='proposal' AND NOT EXISTS "
                "(SELECT 1 FROM event r WHERE r.session_id=e.session_id AND r.reply_to=e.seq) "
                "AND NOT EXISTS (SELECT 1 FROM event done JOIN task t ON t.session_id=done.session_id "
                "AND t.num=done.task_num WHERE done.session_id=e.session_id AND done.seq>e.seq "
                "AND done.type='task.accepted' AND t.parent IS NULL) ORDER BY e.seq LIMIT 20",
                (sid,)).fetchall()
            h = inbox.handle_map(c, [r["actor_id"] for r in rows])
            for r in rows:
                items.append({"kind": "proposal", "session": sh, "seq": r["seq"], "from": h.get(r["actor_id"]),
                              "text": (r["body"] or "")[:NOTE_CHARS], "age_s": _age_s(r["ts"])})
            for r in c.execute("SELECT * FROM event WHERE session_id=? AND type='note' AND actor_id='00000000000000000000000000' "
                               "AND json_extract(data, '$.flag')='loop' AND ts > ? ORDER BY seq", (sid, day_ago)):
                items.append({"kind": "loop", "session": sh, "seq": r["seq"],
                              "thread": json.loads(r["data"]).get("thread"), "age_s": _age_s(r["ts"])})
    return {"principal": p.handle, "items": items}


_ATTN = {"review": "review", "review_requested": "your review", "owed": "owed by you", "approval": "approval request", "waiting_on_you": "waiting on you", "stale": "stale", "unacknowledged": "assignment awaiting acknowledgment",
         "proposal": "proposal", "loop": "loop flag"}


def attention_text(a: dict) -> str:
    n = len(a["items"])
    lines = [render.section(f"--- attention · @{a['principal']} · " + (f"{n} item(s) ---" if n else "nothing needs you ---"))]
    for i in a["items"]:
        where = f"T{i['task']}" if "task" in i else f"#{i['seq']}"
        extra = f" from {i['from']}" if i.get("from") else ""
        extra += f" in thread #{i['thread']}" if i.get("thread") else ""
        lines.append(render.section(f"--- {_ATTN[i['kind']]} · {i['session']} {where}{extra} · {i['age_s'] // 60}m ---"))
        if i.get("title"):
            lines.append(render.inline_line("title", i["title"]))
        if i["kind"] == "unacknowledged":
            lines.append(render.inline_line("assignee", "@" + i["assignee"]))
            lines.append(render.section("--- board sync since assignment: "
                                        + ("yes" if i["assignee_synced_since_assignment"] else "no") + " ---"))
        if i.get("text"):
            lines.append(render.inline_line("text", i["text"]))
    return "\n".join(lines) + "\n"


@router.get("/me/attention")
def my_attention(request: Request, format: Literal["json", "text"] = Query("json"),
                 p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        a = attention(c, p)
    return PlainTextResponse(attention_text(a)) if _wants_text(request, format) else a


def overview(c, p: tokens.Principal) -> dict:
    rows = []
    for sess in _my_sessions(c, p.id):
        counts = {k: 0 for k in (*COLUMNS, "cancelled")}
        stale = 0
        for t in c.execute("SELECT * FROM task WHERE session_id=?", (sess["id"],)):
            counts[t["status"]] += 1
            stale += is_stale(t, sess)
        _, owed = inbox.owed(c, sess["id"], p.id, show=0)
        rows.append({"session": sess["handle"], "title": sess["title"], "state": sess["state"], "role": sess["role"],
                     "counts": counts, "stale": stale, "owed": owed})
    return {"principal": p.handle, "sessions": rows}


def overview_text(o: dict) -> str:
    lines = [render.section(f"--- overview · @{o['principal']} · {len(o['sessions'])} session(s) ---")]
    for r in o["sessions"]:
        counts = " · ".join(f"{k.replace('_', ' ')} {n}" for k, n in r["counts"].items() if k != "cancelled")
        lines.append(render.section(f"--- {r['session']} · {r['state']} · {r['role']} · {counts} · stale {r['stale']} "
                                    f"· owed {r['owed']} ---"))
        lines.append(render.inline_line("title", r["title"]))
    return "\n".join(lines) + "\n"


@router.get("/me/overview")
def my_overview(request: Request, format: Literal["json", "text"] = Query("json"),
                p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        o = overview(c, p)
    return PlainTextResponse(overview_text(o)) if _wants_text(request, format) else o
