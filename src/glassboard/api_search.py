"""Session-scoped full-text search (B45, ERRATA E42): `GET /sessions/{s}/search?q=`.

Events are matched through the FTS5 index `event_fts` (migration 021), always bound to the session;
tasks by title, scope and deliverable. The query is reduced to plain words, each quoted, so FTS5
syntax in the input can neither fail nor widen the match; every word must appear (prefix match on
the last one). Results are newest first and render in the canonical grammar. Reading changes nothing.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .deps import authenticate, get_db
from .errors import ApiError
from .sessions import require_member
from .textviews import view_lines

router = APIRouter(prefix="/api/v1")
WORD = re.compile(r"\w+", re.UNICODE)
MAX_WORDS = 8


def fts_query(q: str) -> str | None:
    words = WORD.findall(q or "")[:MAX_WORDS]
    if not words:
        return None
    quoted = [f'"{w}"' for w in words]
    quoted[-1] += "*"                                   # the last word may still be typed
    return " ".join(quoted)


def search(c, sess, q: str, limit: int = 20) -> dict:
    match = fts_query(q)
    if match is None:
        return {"session": sess["handle"], "events": [], "tasks": [], "more": False}
    rows = c.execute("SELECT e.* FROM event_fts f JOIN event e ON e.session_id=f.session_id AND e.seq=f.seq "
                     "WHERE event_fts MATCH ? AND f.session_id=? ORDER BY e.seq DESC LIMIT ?",
                     (match, sess["id"], limit + 1)).fetchall()
    words = [w.lower() for w in WORD.findall(q)[:MAX_WORDS]]
    tasks = []
    for t in c.execute("SELECT num, title, status, scope, deliverable FROM task WHERE session_id=? ORDER BY num DESC",
                       (sess["id"],)):
        hay = " ".join(x or "" for x in (t["title"], t["scope"], t["deliverable"])).lower()
        if all(w in hay for w in words):
            tasks.append({"task": t["num"], "title": t["title"], "status": t["status"]})
            if len(tasks) >= limit:
                break
    return {"session": sess["handle"], "events": inbox.views(c, rows[:limit]), "tasks": tasks, "more": len(rows) > limit}


def search_text(r: dict) -> str:
    n, m = len(r["events"]), len(r["tasks"])
    lines = [render.section(f"--- search · {r['session']} · {m} task(s), {n} event(s){' (more: narrow the words)' if r['more'] else ''} ---")]
    for t in r["tasks"]:
        lines.append(render.section(f"--- T{t['task']} · {t['status'].replace('_', ' ')} ---"))
        lines.append(render.inline_line("title", t["title"]))
    for v in r["events"]:
        lines.extend(view_lines(v))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/search")
def session_search(s: str, request: Request, q: str = Query("", max_length=200),
                   limit: int = Query(20, ge=1, le=100), format: Literal["json", "text"] = Query("json"),
                   p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """Find events (message text, titles, summaries, reasons, feedback) and tasks in one session. An empty
    query finds nothing (membership is checked first, so a non-member always gets 404)."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        try:
            r = search(c, sess, q, limit)
        except Exception:                               # an index problem must not echo the query
            raise ApiError(503, "unavailable", "Search is unavailable right now.") from None
    if format == "text" or "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(search_text(r))
    return r
