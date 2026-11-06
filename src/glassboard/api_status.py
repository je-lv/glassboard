"""Per-agent status row (B39, report item N3, decision 0035 #18).

One row per agent member, built only from recorded facts and their ages: the claim it holds and
the last fenced activity on it, its checklist, owed questions, leases, the last event it wrote and
from which run, its latest checkpoint, its own stand-up line (labelled self-reported), and whether
a run exited while still holding work. The derived state names the most pressing fact and says why.
There is no "working" or "online" state from a timer: presence is transport, so the row shows the
last fact and its age instead. Nothing here acts on what it reports (§7)."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from . import db, inbox, render, tokens
from .actors import run_label
from .api_runtime import holding_text, holdings
from .api_tasks import _age_s, checklist_of, effective_stale_after, is_stale, progress_of, waiting_view
from .deps import authenticate, get_db
from .sessions import require_member

router = APIRouter(prefix="/api/v1")

# Most pressing first. Each state is a fact the board recorded, never a guess about activity.
STATES = ("exited_holding", "stale", "blocked", "on_task", "in_review", "owes_answers", "idle")
STATE_LABEL = {"exited_holding": "exited holding work", "stale": "stale claim", "blocked": "blocked",
               "on_task": "holds a claim", "in_review": "in review", "owes_answers": "owes answers", "idle": "idle"}


def age_text(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds < 36000 else f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _claims(c, sess, pid: str) -> list[dict]:
    out = []
    for t in c.execute("SELECT * FROM task WHERE session_id=? AND claimant_id=? AND status IN "
                       "('in_progress','blocked','review') ORDER BY num", (sess["id"], pid)):
        held = t["fence_secret"] is not None and t["status"] in ("in_progress", "blocked")
        out.append({"task": t["num"], "title": t["title"], "status": t["status"], "gen": t["gen"],
                    "runtime": t["claimant_runtime"], "fence_valid": held,
                    "checklist": progress_of(checklist_of(t)),
                    "last_activity_age_s": _age_s(t["last_activity_at"]),
                    "stale": bool(held and is_stale(t, sess)), "stale_after_s": effective_stale_after(t, sess),
                    "updated_age_s": _age_s(t["updated_at"]),
                    "waiting_on": waiting_view(c, sess["id"], t) if t["status"] == "blocked" else None})
    return out


def _runs(c, sid: str, pid: str, limit: int = 5) -> list[dict]:
    rows = c.execute("SELECT runtime, MIN(ts) AS first_ts, MAX(ts) AS last_ts, MIN(seq) AS first_seq, "
                     "MAX(seq) AS last_seq, COUNT(*) AS n FROM event WHERE session_id=? AND actor_id=? "
                     "AND runtime IS NOT NULL GROUP BY runtime ORDER BY last_seq DESC LIMIT ?",
                     (sid, pid, limit)).fetchall()
    return [{"runtime": r["runtime"], "events": r["n"], "first_seq": r["first_seq"], "last_seq": r["last_seq"],
             "started_age_s": _age_s(r["first_ts"]), "last_age_s": _age_s(r["last_ts"])} for r in rows]


def _exited(c, sid: str, pid: str) -> dict | None:
    """The latest exit report of a run that still holds a claim or the inbox consumer."""
    for r in c.execute("SELECT seq, ts, runtime, data FROM event WHERE session_id=? AND actor_id=? "
                       "AND type='runtime.exited' AND runtime IS NOT NULL ORDER BY seq DESC LIMIT 5", (sid, pid)):
        h = holdings(c, sid, pid, r["runtime"])
        if h["claims"] or h["inbox_consumer"]:
            return {"seq": r["seq"], "runtime": r["runtime"], "age_s": _age_s(r["ts"]),
                    "exited": json.loads(r["data"] or "{}").get("exited"), "holding": holding_text(h)}
    return None


def _derive(row: dict) -> tuple[str, str]:
    ex = row["exited"]
    if ex:
        return "exited_holding", (f"Run {ex['runtime']} exited ({ex['exited'] or 'unknown'}) "
                                  f"{age_text(ex['age_s'])} ago while holding {ex['holding']}.")
    held = [x for x in row["claims"] if x["fence_valid"]]
    for x in held:
        if x["stale"]:
            return "stale", (f"T{x['task']}: no fenced activity for {age_text(x['last_activity_age_s'])} "
                             f"(stale after {age_text(x['stale_after_s'])}).")
    for x in held:
        if x["status"] == "blocked":
            w = x["waiting_on"]
            return "blocked", f"T{x['task']} is blocked" + (f", waiting on {w['label']}." if w else ".")
    if held:
        x = held[0]
        more = f" (+{len(held) - 1} more)" if len(held) > 1 else ""
        return "on_task", (f"Holds the claim on T{x['task']}{more}; last fenced activity "
                           f"{age_text(x['last_activity_age_s'])} ago.")
    review = [x for x in row["claims"] if x["status"] == "review"]
    if review:
        x = review[0]
        return "in_review", f"T{x['task']} submitted for review {age_text(x['updated_age_s'])} ago."
    if row["owed"]["count"]:
        n = row["owed"]["count"]
        return "owes_answers", f"No claim; owes {n} answer{'' if n == 1 else 's'}."
    return "idle", "No claim and nothing owed."


def agent_status(c, sess) -> list[dict]:
    sid = sess["id"]
    rows = []
    for m in c.execute("SELECT m.*, p.handle, p.kind FROM member m JOIN principal p ON p.id=m.principal_id "
                       "WHERE m.session_id=? AND m.left_at IS NULL AND p.kind IN ('agent','client') "
                       "ORDER BY p.handle", (sid,)).fetchall():
        pid = m["principal_id"]
        last = c.execute("SELECT seq, ts, type, runtime FROM event WHERE session_id=? AND actor_id=? "
                         "ORDER BY seq DESC LIMIT 1", (sid, pid)).fetchone()
        owed, n_owed = inbox.owed(c, sid, pid, show=5)
        cp = c.execute("SELECT n, seq, created_at FROM checkpoint WHERE session_id=? AND principal_id=? "
                       "ORDER BY n DESC LIMIT 1", (sid, pid)).fetchone()
        su = c.execute("SELECT seq, ts, data FROM event WHERE session_id=? AND actor_id=? AND type='update' "
                       "AND json_extract(data, '$.standup.doing') IS NOT NULL ORDER BY seq DESC LIMIT 1",
                       (sid, pid)).fetchone()
        runs = _runs(c, sid, pid)
        row = {
            "member": m["handle"], "role": m["role"], "job_role": m["job_role"],
            "claims": _claims(c, sess, pid),
            "owed": {"count": n_owed, "items": owed},
            "leases": [{"resource": r["resource"], "age_s": _age_s(r["taken_at"])} for r in c.execute(
                "SELECT resource, taken_at FROM lease WHERE session_id=? AND holder_id=? ORDER BY resource", (sid, pid))],
            "last_event": ({"seq": last["seq"], "type": last["type"], "age_s": _age_s(last["ts"]),
                            "runtime": last["runtime"], "run": run_label(m["handle"], last["runtime"])}
                           if last else None),
            "last_sync_age_s": _age_s(m["last_seen_at"]),
            "inbox_run": run_label(m["handle"], m["consumer_runtime"]) if m["consumer_secret"] else None,
            "runs": [dict(r, run=run_label(m["handle"], r["runtime"])) for r in runs],
            "checkpoint": {"n": cp["n"], "seq": cp["seq"], "age_s": _age_s(cp["created_at"])} if cp else None,
            "doing": ({"text": json.loads(su["data"])["standup"]["doing"], "seq": su["seq"], "age_s": _age_s(su["ts"]),
                       "source": "self-reported"} if su else None),
            "exited": _exited(c, sid, pid),
        }
        row["state"], row["reason"] = _derive(row)
        row["state_label"] = STATE_LABEL[row["state"]]
        rows.append(row)
    rows.sort(key=lambda r: (STATES.index(r["state"]), r["member"]))
    return rows


def status_text(handle: str, rows: list[dict]) -> str:
    lines = [render.section(f"--- agent status · {handle} · {len(rows)} agent(s) · facts and their ages, "
                            "never a presence timer ---")]
    for r in rows:
        last = r["last_event"]
        lines.append(render.section(f"--- @{r['member']} · {r['state_label']} ---"))
        lines.append(render.inline_line("why", r["reason"]))
        for x in r["claims"]:
            prog = f" · checklist {x['checklist']['done']}/{x['checklist']['total']}" if x["checklist"] else ""
            lines.append(render.inline_line(f"t{x['task']}", f"{x['status']} g{x['gen']}{prog} · {x['title']}"))
        if r["owed"]["count"]:
            lines.append(render.inline_line("owes", ", ".join(f"#{o['seq']}" for o in r["owed"]["items"])
                                            + (f" (+{r['owed']['count'] - len(r['owed']['items'])} more)"
                                               if r["owed"]["count"] > len(r["owed"]["items"]) else "")))
        if r["leases"]:
            lines.append(render.inline_line("leases", ", ".join(x["resource"] for x in r["leases"])))
        lines.append(render.inline_line("last event", f"#{last['seq']} {last['type']} {age_text(last['age_s'])} ago"
                                        + (f" · {last['run']}" if last["run"] else "") if last else "none"))
        if r["inbox_run"]:
            lines.append(render.inline_line("inbox held by", r["inbox_run"]))
        lines.append(render.inline_line("checkpoint", f"#{r['checkpoint']['n']} {age_text(r['checkpoint']['age_s'])} ago"
                                        if r["checkpoint"] else "none"))
        if r["doing"]:
            lines.append(render.inline_line("doing", f"(self-reported, {age_text(r['doing']['age_s'])} ago) "
                                            + r["doing"]["text"]))
    return "\n".join(lines) + "\n"


@router.get("/sessions/{s}/status")
def session_status(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                   p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """One status row per agent member: derived state with its reason, from recorded facts only."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        rows = agent_status(c, sess)
    if format == "text" or "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(status_text(sess["handle"], rows))
    return {"session": sess["handle"], "agents": rows}
