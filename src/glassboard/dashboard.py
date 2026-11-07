"""Dashboard v0: login, timeline, read-only agent view, post box.

Server-rendered (Jinja2, autoescape on). Security:
- humans log in by pasting a token; the board stores only SHA-256 of a random web-session ID, sent
  as `__Host-gb_session` (Secure, HttpOnly, SameSite=Strict, Path=/; the `__Host-` prefix forbids
  a Domain attribute); the login form has its own nonce cookie against login CSRF;
- every form carries the web session's CSRF token (checked in constant time) and every POST must
  come from an allowed Origin when the browser sends one;
- CSP `default-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'` plus
  `base-uri 'none'; form-action 'self'`; no inline script or style, no external assets;
- every line shown is canonical text (render.py: control/bidi characters made harmless, inline values
  quoted and truncated) and Jinja2 escapes it again;
  `display_name` (dashboard-only) has control/bidi characters made harmless, then is escaped;
- post forms carry the session's recovery ID; a stale form after a restore fails with
  `recovery_mismatch` and the page says to reload;
- the dashboard never consumes cursors; the agent view is computed read-only.
`static/poll.js` refreshes live fragments every 5 s and progressively enhances timeline filters and
Observe replay links. Fragment responses are same-origin, type-labelled server-rendered HTML; normal
navigation remains the fallback and mutations never use the fragment path.
"""
from __future__ import annotations

import contextvars
import contextlib
import hashlib
import hmac
import json
import re
import os
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response, FileResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.staticfiles import StaticFiles

from . import admin_auth, flash, conversations, db, wake, events, idempotency, ids, inbox, journal, observe, render, routing, run_browser, sessions, tlsboot, tokens
from .api_messages import MessageIn, send_message
from .deps import client_addr, get_db
from .errors import ApiError
from .textviews import sync_text, view_lines

COOKIE = "__Host-gb_session"
LOGIN_COOKIE = "__Host-gb_login"
# In plain-HTTP mode browsers refuse `__Host-` and Secure cookies, so the cookies
# lose both (still HttpOnly, SameSite=Strict, Path=/), and every page shows the NOT ENCRYPTED banner.
# Set per request by app.py's request middleware.
PLAIN_HTTP: contextvars.ContextVar[bool] = contextvars.ContextVar("gb_plain_http", default=False)


def session_cookie() -> str:
    return "gb_session" if PLAIN_HTTP.get() else COOKIE


def login_cookie() -> str:
    return "gb_login" if PLAIN_HTTP.get() else LOGIN_COOKIE
SESSION_TTL = timedelta(hours=12)
OWNER_SESSION_TTL = timedelta(days=30)
CSP = ("default-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
TIMELINE_LIMIT = 200
FILTERS = {"all": "all", "work": "work · hide leases", "me": "to me", "questions": "questions", "decisions": "decisions", "task": "task"}
# The Workbench and Observe views are part of the normal browser UI. A host can
# temporarily opt out with GLASSBOARD_EXPERIMENTAL_UI=0 while comparing an older layout.
EXPERIMENTAL_UI = os.environ.get("GLASSBOARD_EXPERIMENTAL_UI", "1") != "0"
PREVIEW_SNAPSHOT = os.environ.get("GLASSBOARD_PREVIEW_SNAPSHOT") == "1"

_pkg = resources.files("glassboard")
_env = Environment(loader=FileSystemLoader(str(_pkg.joinpath("templates"))),
                   autoescape=select_autoescape(["html"], default=True), trim_blocks=True, lstrip_blocks=True)


_env.globals["err_text"] = flash.err_text
from .actors import badge as _actor_badge  # noqa: E402
_env.globals["actor_badge"] = _actor_badge     # (mark, name, style) for a handle: the same colors as the timeline


def _ago(seconds) -> str:
    """UI analysis F08: one readable age everywhere ("just now", "4m ago", "3h 5m ago", "22d ago")."""
    from .api_status import age_text
    if seconds is None:
        return "never"
    return "just now" if seconds < 60 else age_text(int(seconds)) + " ago"


_env.filters["ago"] = _ago
_env.filters["ago_min"] = lambda minutes: _ago(None if minutes is None else int(minutes) * 60)
_env.filters["size"] = lambda n: human_size(n)


def _since(ts: str | None) -> str:
    """An ISO timestamp as a readable age ("22m ago"); the exact time stays in a title attribute."""
    from datetime import datetime, timezone
    if not ts:
        return "never"
    t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return _ago(max(0, int((datetime.now(timezone.utc) - t).total_seconds())))


_env.filters["since"] = _since


class NeedLogin(Exception):
    pass


# The agent clients the "Add an agent" form knows how to start (the board never starts anything itself).
JOIN_CLIENTS = {
    "claude": {"label": "Claude Code", "default": "claude", "launch": "claude"},
    "codex": {"label": "Codex", "default": "codex", "launch": "codex"},
    "other": {"label": "Another agent or script", "default": "agent", "launch": "<your agent command>"},
}


# Browser access names describe authority; job roles such as lead describe project work.
TEAM_ORDER = ["Session Manager", "Task Manager", "Contributor", "Viewer"]


def team_title(kind: str, role: str) -> str:
    if role == "coordinator":
        return "Session Manager" if kind == "human" else "Task Manager"
    if role == "member":
        return "Contributor"
    return "Viewer"


def _security_headers(resp: Response) -> Response:
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    # same-origin, not no-referrer: under no-referrer browsers send `Origin: null` on every form POST
    # (Fetch spec), which check_post must refuse; same-origin still sends nothing to other sites.
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def page(name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("plain_http", PLAIN_HTTP.get())
    ctx.setdefault("experimental_ui", EXPERIMENTAL_UI)
    ctx.setdefault("snapshot_preview", PREVIEW_SNAPSHOT)
    ctx.setdefault("flash", flash.CURRENT.get())
    return _security_headers(HTMLResponse(_env.get_template(name).render(**ctx), status_code=status))


def _sha(v: str) -> bytes:
    return hashlib.sha256(v.encode()).digest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")


def _line_class(line: str) -> str:
    if render.HEADER_RE.match(line):
        return "hdr"
    if line.startswith(render.INLINE):
        key = line[len(render.INLINE):].split(":", 1)[0]
        if key in {"title", "deliverable", "check", "summary", "done", "doing", "blocked"}:
            return f"inl field-{key}"
        return "inl"
    for prefix, cls in ((render.BLOCK, "blk"), ("  ↳ ", "ref")):
        if line.startswith(prefix):
            return cls
    return "sec"


_HEAD = re.compile(r"^#(?P<seq>\d+) (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z) (?P<actor>[a-z0-9/-]+?)"
                   r"(?:@(?P<rt>[a-z0-9-]+))?(?: -> (?P<to>.+?))? \[(?P<kind>[a-z.]+)\]"
                   r"(?: T(?P<task>\d+)(?: g(?P<gen>\d+))?)?(?: re:#(?P<re>\d+))?$")
_STRUCTURAL = re.compile(r"^(\s{2,}|[-*+\u2022>#|]\s|\d+[.)]\s|```)")


def _paragraphs(lines: list[str]) -> list[list[dict]]:
    """B35: Block text as paragraphs for the browser only (canonical text is unchanged). Blank lines
    separate paragraphs; consecutive prose lines flow together; list items, quotes, indented and
    fenced lines keep their own line."""
    paras, cur, fenced = [], [], False
    for raw in lines:
        text = raw[len(render.BLOCK):] if raw.startswith(render.BLOCK) else ""
        if text.startswith("```"):
            fenced = not fenced
            cur.append({"text": text, "pre": True})
            continue
        if fenced:
            cur.append({"text": text, "pre": True})
            continue
        if not text.strip():
            if cur:
                paras.append(cur); cur = []
            continue
        if cur and not cur[-1]["pre"] and not _STRUCTURAL.match(text):
            cur[-1]["text"] += " " + text.strip()
        else:
            cur.append({"text": text if _STRUCTURAL.match(text) else text.strip(), "pre": bool(_STRUCTURAL.match(text))})
    if cur:
        paras.append(cur)
    return paras


QUIET_READ_TYPES = ("consumer.", "wake.", "context.", "runtime.", "lease.", "checkpoint.", "message.preflight")


def event_card(v: dict) -> dict:
    """One timeline event for the browser: a structured header, body paragraphs, muted metadata.
    `lines` keeps the canonical text for the forensic trace and for copying."""
    from .actors import badge, run_label
    if v.get("read") == "unknown" or v["type"].startswith(QUIET_READ_TYPES):
        v = {**v, "read": None}           # B54 clutter: unknown, or bookkeeping where reading is beside the point
    lines = view_lines(v)
    m = _HEAD.match(lines[0]) if lines else None
    head = None
    if m:
        to = m["to"] or ""
        head = {"ts": m["ts"], "runtime": m["rt"], "run": run_label(v["actor"], m["rt"]), "kind": m["kind"], "task": m["task"], "gen": m["gen"],
                "re": int(m["re"]) if m["re"] else None,
                "to": ["all"] if to == "all" else [h for h in to.split(", ") if h]}
    body, block = [], []
    for ln in lines[1:]:
        if ln.startswith(render.BLOCK) or ln == render.BLOCK.rstrip():
            block.append(ln if ln.startswith(render.BLOCK) else render.BLOCK)
            continue
        if block:
            body.append({"kind": "paras", "paras": _paragraphs(block)}); block = []
        cls = _line_class(ln)
        meta = cls == "inl"
        body.append({"kind": "line", "cls": cls + (" meta" if meta else ""), "text": ln})
    if block:
        body.append({"kind": "paras", "paras": _paragraphs(block)})
    mark, name, style = badge(v["actor"])
    # E42 compact row: the first line of participant text, else the first metadata line (both already canonical)
    first = next((ln[len(render.BLOCK):] for ln in lines[1:] if ln.startswith(render.BLOCK) and ln[len(render.BLOCK):].strip()),
                 next((ln.strip() for ln in lines[1:] if ln.startswith(render.INLINE)), ""))
    summary = first if len(first) <= 160 else first[:159] + "…"
    return {"seq": v["seq"], "type": v["type"], "lines": lines_view(lines), "head": head, "body": body,
            "actor": v["actor"], "actor_mark": mark, "actor_name": name, "actor_style": style, "answered": False,
            "summary": summary}


def lines_view(lines: list[str]) -> list[dict]:
    return [{"text": ln, "cls": _line_class(ln)} for ln in lines]


def _external_read(path: str):
    """A read-only SQLite snapshot connection to an operator-configured live board."""
    c = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def run_status(tasks: list[dict]) -> dict:
    """Summarize recorded task state without guessing what an agent is doing off-board."""
    titles = {t["task"]: render.inline(t["title"], 70) for t in tasks}
    parent_nums = {t["parent"] for t in tasks if t["parent"] is not None}
    work = [t for t in tasks if t["task"] not in parent_nums and t["status"] != "cancelled"]
    accepted = sum(t["status"] == "done" for t in work)
    focus = []
    order = {"review": 0, "blocked": 1, "in_progress": 2, "open": 3}
    for task in work:
        state = task["status"]
        if state == "done":
            continue
        if state == "review":
            detail = f"Awaiting review by @{task['reviewer']}" if task["reviewer"] else "Awaiting review"
        elif state == "in_progress":
            who = task["claimant"] or task["assignee"]
            detail = f"Claimed by @{who}" if who else "In progress"
        elif state == "blocked":
            detail = f"Blocked: {task['waiting_on']['label']}" if task["waiting_on"] else "Blocked"
        elif not task["ready"]:
            pending = ", ".join(f"T{dep['task']} ({titles.get(dep['task'], 'unknown task')})"
                                for dep in task["depends_on"] if dep["status"] != "done")
            detail = f"Waiting for {pending}"
        else:
            who = task["assignee"]
            detail = f"Ready for @{who}" if who else "Ready to assign"
        focus.append({"task": task["task"], "title": render.r2(task["title"]), "status": state,
                      "detail": render.r2(detail), "target": render.inline(task["deliverable"], 180),
                      "progress": task["progress"], "stale": task["stale"],
                      "age_min": task["activity_age_s"] // 60 if task["activity_age_s"] is not None else None})
    focus.sort(key=lambda t: (order.get(t["status"], 4), t["task"]))
    achieved = sorted((t for t in work if t["status"] == "done"),
                      key=lambda t: t["updated_at"], reverse=True)
    achieved = [{"task": t["task"], "title": render.r2(t["title"]),
                 "by": t["claimant"] or t["assignee"],
                 "result": render.inline(t["summary"] or t["deliverable"], 220)} for t in achieved]
    stale_parents = []
    for parent in tasks:
        if parent["task"] not in parent_nums or not parent["stale"]:
            continue
        children = [t for t in tasks if t["parent"] == parent["task"] and t["status"] != "cancelled"]
        stale_parents.append({"task": parent["task"], "title": render.r2(parent["title"]),
                              "children": [{"task": t["task"], "title": render.r2(t["title"]),
                                            "status": t["status"]} for t in children]})
    counts = {state: sum(t["status"] == state for t in work) for state in ("open", "in_progress", "blocked", "review", "done")}
    return {"accepted": accepted, "total": len(work), "counts": counts,
            "percent": round(100 * accepted / len(work)) if work else 0,
            "focus": focus, "achieved": achieved, "stale_parents": stale_parents}


def token_state(cred: dict, now: str | None = None) -> str:
    """Analysis F02: revoked, expired or active. An expired token no longer works and offers no Revoke."""
    if cred.get("revoked_at"):
        return "revoked"
    if cred.get("expires_at") and cred["expires_at"] <= (now or db.utcnow()):
        return "expired"
    return "active"


def human_size(n) -> str:
    """Owner fix F6: 14122 -> '13.8 KB' (binary units, one decimal under 100)."""
    size = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} byte" + ("" if int(size) == 1 else "s")
            return f"{size:.1f} {unit}" if size < 100 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} GB"


def artifact_groups(items: list[dict]) -> list[dict]:
    """Owner fix F6: one entry per file name, newest first; older uploads with the same name are its
    earlier versions (shown as superseded, never hidden). `items` must be newest first."""
    groups: dict[str, dict] = {}
    order = []
    for item in items:
        view = dict(item, size_h=human_size(item.get("size")), short_sha=(item.get("sha256") or "")[:12])
        group = groups.get(item["name"])
        if group is None:
            groups[item["name"]] = {"latest": view, "earlier": []}
            order.append(item["name"])
        else:
            group["earlier"].append(view)
    return [groups[name] for name in order]


# B39: status tone per derived state (same chip tones as the kanban cards).
AGENT_TONE = {"exited_holding": "danger", "stale": "danger", "blocked": "warn", "on_task": "progress",
              "in_review": "review", "owes_answers": "warn", "idle": "muted"}


def agent_rows_for_display(rows: list[dict]) -> list[dict]:
    from .actors import badge
    from .api_status import age_text
    out = []
    for r in rows:
        mark, name, style = badge(r["member"])
        last = r["last_event"]
        out.append(dict(r, mark=mark, name=name, style=style, tone=AGENT_TONE[r["state"]],
                        last_text=(f"#{last['seq']} {last['type']} · {age_text(last['age_s'])} ago" if last else "no events yet"),
                        checkpoint_text=(f"#{r['checkpoint']['n']} · {age_text(r['checkpoint']['age_s'])} ago"
                                         if r["checkpoint"] else "none"),
                        doing_age=age_text(r["doing"]["age_s"]) if r["doing"] else None,
                        run_started=(age_text(r["runs"][0]["started_age_s"]) if r["runs"] else None),
                        compaction_text=(f"#{r['compaction']['seq']} · {age_text(r['compaction']['age_s'])} ago "
                                         f"({r['compaction']['trigger']})" if r.get("compaction") else None),
                        compacted_unsaved=bool(r.get("compaction") and not r["compaction"]["checkpoint_since"])))
    return out


def _short_age(seconds) -> str | None:
    if seconds is None:
        return None
    m = max(0, int(seconds)) // 60
    return f"{m}m" if m < 60 else (f"{m // 60}h" if m < 48 * 60 else f"{m // 1440}d")


def _iso_age_s(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        then = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - then).total_seconds())


def task_counts(c, session_id: str) -> dict[int, dict]:
    """B38: per task, files attached and questions still open about it (for the card face)."""
    counts: dict[int, dict] = {}
    for num, n in c.execute("SELECT task_num, count(*) FROM artifact WHERE session_id=? AND task_num IS NOT NULL "
                            "GROUP BY task_num", (session_id,)):
        counts.setdefault(num, {})["files"] = n
    for num, n in c.execute(
            "SELECT q.task_num, count(*) FROM event q WHERE q.session_id=? AND q.type='question' AND q.task_num IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM event a WHERE a.session_id=q.session_id AND a.reply_to=q.seq "
            "AND a.type IN ('answer','question.resolved')) GROUP BY q.task_num", (session_id,)):
        counts.setdefault(num, {})["questions"] = n
    return counts


def card_face(task: dict, counts: dict) -> dict:
    """B38: what a card shows at a glance, derived only from recorded board state (never a timer)."""
    from .actors import badge
    status = task["status"]
    activity = _short_age(task.get("activity_age_s"))
    changed = _short_age(_iso_age_s(task.get("updated_at")))
    if status == "review":
        reviewer = task.get("reviewer")
        state, tone, age = ("in review" + (f" · @{reviewer}" if reviewer else "")), "review", changed and f"submitted {changed} ago"
    elif status == "in_progress":
        if task.get("stale"):
            state, tone, age = f"stale {activity}" if activity else "stale", "danger", None
        else:
            state, tone, age = "claimed", "progress", activity and f"last activity {activity} ago"
    elif status == "blocked":
        state, tone, age = "blocked", "warn", activity and f"last activity {activity} ago"
    elif status == "open":
        if task.get("ack_overdue"):
            state, tone = "awaiting acknowledgment", "warn"
        elif task.get("assignee"):
            state, tone = "assigned", "muted"
        else:
            state, tone = "unclaimed", "muted"
        age = changed and f"created {changed} ago" if task.get("version", 1) == 1 else changed and f"updated {changed} ago"
    elif status == "done":
        state, tone, age = "accepted", "done", changed and f"{changed} ago"
    else:
        state, tone, age = status.replace("_", " "), "muted", None
    review = task.get("review") or {}
    face = {"state": state, "tone": tone, "age": age, "counts": []}
    if review.get("verdict") and status in ("review", "in_progress"):
        face["counts"].append(f"review: {review['verdict']}")
    c = counts.get(task["task"], {})
    if c.get("questions"):
        face["counts"].append(f"{c['questions']} open question" + ("s" if c["questions"] > 1 else ""))
    if c.get("files"):
        face["counts"].append(f"{c['files']} file" + ("s" if c["files"] > 1 else ""))
    if task.get("claimant"):
        face["badge"] = dict(zip(("mark", "name", "style"), badge(task["claimant"])))
        face["badge"]["handle"] = task["claimant"]
    return face


def board_for_display(board: dict, status: dict, full_notes: dict[int, str], counts: dict | None = None) -> dict:
    """Add safe compact text and complete modal text to a read-only board view."""
    stale_parent_map = {parent["task"]: parent for parent in status["stale_parents"]}
    for col in board["columns"]:
        col["label"] = col["status"].replace("_", " ")
        for task in col["tasks"]:
            task["face"] = card_face(task, counts or {})
            task["title_line"] = render.inline(task["title"], 120)
            task["title_full"] = render.r2(task["title"])
            task["deliverable_full"] = render.r2(task["deliverable"])
            task["summary_full"] = render.r2(task["summary"]) if task["summary"] else None
            task["scope_full"] = render.r2(task["scope"], allow_newline=True)
            task["checklist_full"] = [{**item, "text": render.r2(item["text"])}
                                      for item in task["checklist"]]
            if task["task"] in stale_parent_map:
                task["child_statuses"] = stale_parent_map[task["task"]]["children"]
            if task.get("last_note"):
                note = task["last_note"]
                task["note_line"] = render.inline(f"{note['by']}, {_short_age(note['age_s'])} ago: {note['text']}", 160)
                task["note_full"] = render.r2(full_notes.get(task["task"], note["text"]), allow_newline=True)
    return board


def latest_task_note_bodies(c, session_id: str) -> dict[int, str]:
    """The modal shows complete notes; compact cards keep the board API's excerpt."""
    rows = c.execute(
        "SELECT e.task_num, e.body FROM event e WHERE e.session_id=? AND e.type='task.note' "
        "AND e.seq=(SELECT MAX(seq) FROM event x WHERE x.session_id=e.session_id "
        "AND x.task_num=e.task_num AND x.type='task.note')", (session_id,)).fetchall()
    return {row["task_num"]: row["body"] or "" for row in rows}


# --- web sessions ---------------------------------------------------------------------------------

class Viewer:
    def __init__(self, principal: tokens.Principal, csrf: str):
        self.p, self.csrf = principal, csrf


def current_viewer(request: Request, database: db.Database = Depends(get_db)) -> Viewer:
    sid = request.cookies.get(session_cookie())
    if not sid:
        raise NeedLogin()
    with database.read() as c:
        row = c.execute(
            "SELECT w.csrf_token, w.expires_at, w.revoked_at, w.scope, w.credential_id, "
            "c.scope credential_scope, c.expires_at credential_expires_at, c.revoked_at credential_revoked_at, "
            "p.id, p.handle, p.kind, p.active, p.is_admin, o.active owner_active, "
            "a.changed_at password_changed_at, w.created_at "
            "FROM web_session w JOIN principal p ON p.id = w.principal_id "
            "LEFT JOIN credential c ON c.id = w.credential_id AND c.principal_id = w.principal_id "
            "LEFT JOIN admin_password a ON a.principal_id = p.id "
            "LEFT JOIN principal o ON o.id = p.owner_id "
            "WHERE w.id_sha256 = ?", (_sha(sid),)).fetchone()
    if (row is None or row["revoked_at"] or not row["active"] or row["kind"] != "human"
            or row["expires_at"] <= _ts(_now())
            or row["credential_revoked_at"] or (row["credential_expires_at"] and row["credential_expires_at"] <= _ts(_now()))):
        raise NeedLogin()
    if row["credential_id"] is None:
        if (not row["is_admin"] or row["scope"] != "full" or not row["password_changed_at"]
                or row["created_at"] < row["password_changed_at"]):
            raise NeedLogin()
    elif row["credential_scope"] != row["scope"]:
        raise NeedLogin()
    p = tokens.Principal(id=row["id"], handle=row["handle"], kind="human", owner_handle=None,
                         is_admin=bool(row["is_admin"]), credential_id=row["credential_id"], scope=row["scope"],
                         expires_at=row["credential_expires_at"])
    request.state.principal_id = p.id
    return Viewer(p, row["csrf_token"])


async def form_data(request: Request) -> dict[str, str]:
    """urlencoded forms only (stdlib parsing; no multipart dependency). An empty body is an empty
    form, so a request without fields reaches the CSRF check and gets 403, not 415."""
    raw_bytes = await request.body()
    if not raw_bytes:
        return {}
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
        raise ApiError(415, "unsupported_media_type", "Forms must be application/x-www-form-urlencoded.")
    try:
        raw = raw_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ApiError(422, "invalid_request", "The form is not valid UTF-8.") from None
    return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True, strict_parsing=False).items()}


def check_post(request: Request, form: dict, viewer: Viewer | None, expected_csrf: str | None = None,
               allow_read_scope: bool = False) -> None:
    """Origin (when sent) must be one of ours; the CSRF token must match. Missing → 403."""
    origin = request.headers.get("origin")
    if origin:
        host = urllib.parse.urlsplit(origin).hostname or ""
        if host not in request.app.state.allowed_hosts:
            raise ApiError(403, "forbidden", "Cross-origin form submission refused.")
    want = expected_csrf if expected_csrf is not None else (viewer.csrf if viewer else None)
    got = form.get("csrf", "")
    if not want or not got or not hmac.compare_digest(want.encode(), got.encode()):
        raise ApiError(403, "forbidden", "Missing or invalid form token; reload the page.")
    if viewer is not None and viewer.p.scope != "full" and not allow_read_scope:
        raise ApiError(403, "read_only", "This login is read-only.")


def check_raw_post(request: Request, viewer: Viewer) -> None:
    """CSRF and Origin checks for a same-origin JavaScript upload with a raw request body."""
    origin = request.headers.get("origin")
    if origin:
        host = urllib.parse.urlsplit(origin).hostname or ""
        if host not in request.app.state.allowed_hosts:
            raise ApiError(403, "forbidden", "Cross-origin upload refused.")
    got = request.headers.get("X-Glassboard-CSRF", "")
    if not got or not hmac.compare_digest(viewer.csrf.encode(), got.encode()):
        raise ApiError(403, "forbidden", "Missing or invalid form token; reload the page.")
    if viewer.p.scope != "full":
        raise ApiError(403, "read_only", "This login is read-only.")


# --- routes -----------------------------------------------------------------------------------------

def mount(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=str(_pkg.joinpath("static"))), name="static")

    def nav_badges(sess, viewer) -> dict:
        """Counts for the session tabs (E42). Never fails a page: no badges rather than an error."""
        from . import nowview
        try:
            with app.state.db.read() as c:
                return nowview.badges(c, sess, viewer.p)
        except Exception:                                   # noqa: BLE001
            return {}
    _env.globals["nav_badges"] = nav_badges

    @app.exception_handler(NeedLogin)
    async def _need_login(request: Request, exc: NeedLogin):
        if request.method == "GET":
            dest = "/admin/login" if request.url.path.startswith(("/admin", "/owner")) else "/login"
            return _security_headers(RedirectResponse(dest, status_code=303))
        raise ApiError(403, "forbidden", "Log in first.")

    @app.get("/docs", include_in_schema=False)
    def docs_page():
        """Public product and usage guide; it contains no board or participant data."""
        return page("docs.html")

    @app.get("/docs/agent-reference", include_in_schema=False)
    def agent_reference_page():
        """B36: the agent reference (every capability: API route, CLI command, dashboard page), as plain
        text, the same file `glassboard reference` prints. Public like /docs: no board data in it."""
        from importlib import resources
        text = resources.files("glassboard").joinpath("agent_reference.md").read_text(encoding="utf-8")
        return _security_headers(PlainTextResponse(text, media_type="text/plain; charset=utf-8"))

    @app.get("/login", include_in_schema=False)
    def login_form(request: Request):
        nonce = secrets.token_urlsafe(24)
        resp = page("login.html", nonce=nonce, error=request.query_params.get("e") == "1")
        _cookie(resp, login_cookie(), nonce, 600)
        return resp

    @app.post("/login", include_in_schema=False)
    async def login(request: Request, database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, None, expected_csrf=request.cookies.get(login_cookie()))
        limiter, addr = request.app.state.auth_limiter, client_addr(request)
        if limiter.retry_after(addr) is not None:
            raise ApiError(429, "rate_limited", "Too many authentication failures from this address.", retryable=True)
        with database.read() as c:
            p = tokens.verify(c, form.get("token", "").strip())
        if p is None or p.kind != "human":
            limiter.record_failure(addr)
            return _security_headers(RedirectResponse("/login?e=1", status_code=303))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = _now()
        ttl = SESSION_TTL
        with database.write() as c:     # web_session rows are bookkeeping: no event
            c.execute("INSERT INTO web_session(id_sha256,principal_id,credential_id,scope,csrf_token,created_at,expires_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (_sha(sid), p.id, p.credential_id, p.scope, csrf, _ts(now), _ts(now + ttl)))
        resp = _security_headers(RedirectResponse("/", status_code=303))
        _cookie(resp, session_cookie(), sid, int(ttl.total_seconds()))
        resp.delete_cookie(login_cookie(), path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")
        return resp

    @app.get("/admin/login", include_in_schema=False)
    def admin_login_form(request: Request):
        nonce = secrets.token_urlsafe(24)
        resp = page("admin_login.html", nonce=nonce, error=request.query_params.get("e") == "1")
        _cookie(resp, login_cookie(), nonce, 600)
        return resp

    @app.post("/admin/login", include_in_schema=False)
    async def admin_login(request: Request, database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, None, expected_csrf=request.cookies.get(login_cookie()))
        limiter, addr = request.app.state.auth_limiter, client_addr(request)
        local_http = (PLAIN_HTTP.get() and request.url.hostname in ("127.0.0.1", "localhost", "::1")
                      and addr in ("127.0.0.1", "::1"))
        if request.url.scheme != "https" and not local_http:
            raise ApiError(403, "forbidden", "Admin password login requires HTTPS or local loopback access.")
        if limiter.retry_after(addr) is not None:
            raise ApiError(429, "rate_limited", "Too many authentication failures from this address.", retryable=True)
        with database.read() as c:
            p = admin_auth.verify(c, form.get("username", "").strip(), form.get("password", ""))
        if p is None:
            limiter.record_failure(addr)
            return _security_headers(RedirectResponse("/admin/login?e=1", status_code=303))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = _now()
        ttl = OWNER_SESSION_TTL if form.get("remember") == "1" and (request.url.scheme == "https" or local_http) else SESSION_TTL
        with database.write() as c:
            c.execute("INSERT INTO web_session(id_sha256,principal_id,credential_id,scope,csrf_token,created_at,expires_at) "
                      "VALUES (?,?,?,?,?,?,?)", (_sha(sid), p["id"], None, "full", csrf, _ts(now), _ts(now + ttl)))
        resp = _security_headers(RedirectResponse("/admin", status_code=303))
        _cookie(resp, session_cookie(), sid, int(ttl.total_seconds()))
        resp.delete_cookie(login_cookie(), path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")
        return resp

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer, allow_read_scope=True)
        with database.write() as c:
            c.execute("UPDATE web_session SET revoked_at=? WHERE id_sha256=?", (_ts(_now()), _sha(request.cookies[session_cookie()])))
        resp = _security_headers(RedirectResponse("/login", status_code=303))
        resp.delete_cookie(session_cookie(), path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")
        return resp

    @app.get("/", include_in_schema=False)
    def home(request: Request, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        from . import api_board
        with database.read() as c:
            mine = sessions.my_sessions(c, viewer.p.id)
            attn = api_board.attention(c, viewer.p)
            over = {r["session"]: r for r in api_board.overview(c, viewer.p)["sessions"]}
        # Participant text goes through R2/R3 before it reaches HTML: no raw hidden characters, even escaped.
        items = [{**i, "label": api_board._ATTN[i["kind"]], "min": i["age_s"] // 60,
                  "title_line": render.inline(i["title"], 120) if i.get("title") else None,
                  "text_line": render.inline(i["text"], 120) if i.get("text") else None,
                  "default_line": render.inline(i["default"], 120) if i.get("default") else None} for i in attn["items"]]
        return page("home.html", viewer=viewer, attention=items, op=ids.new_ulid(), error=request.query_params.get("err"),
                    sessions=[{**s, "title_line": render.inline(s["title"]), "o": over.get(s["session"])} for s in mine])

    @app.post("/attention/{s}/{seq}/dismiss", include_in_schema=False)
    async def dismiss_proposal(s: str, seq: int, request: Request, viewer: Viewer = Depends(current_viewer),
                               database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            if viewer.p.kind != "human" or me["role"] != "coordinator":
                raise ApiError(403, "forbidden", "Only a human Session Manager may dismiss a proposal.")
            proposal = c.execute("SELECT 1 FROM event WHERE session_id=? AND seq=? AND type='proposal'",
                                 (sess["id"], seq)).fetchone()
            if proposal is None:
                raise ApiError(404, "not_found", "Proposal not found.")
            recovery = sess["recovery_id"]
        body = MessageIn(kind="note", body="Dismissed from the human attention queue.", re=seq)
        send_message(database, request.app.state.post_limiter, viewer.p, s, body,
                     form.get("op") or ids.new_ulid(), recovery, None)
        return _security_headers(RedirectResponse("/", status_code=303))

    @app.get("/owner", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    def owner_console(request: Request, viewer: Viewer = Depends(current_viewer),
                      database: db.Database = Depends(get_db)):
        """An administrator can find every workspace, then explicitly join it with a visible event."""
        if not viewer.p.is_admin or viewer.p.scope != "full":
            raise ApiError(404, "not_found", "Not found.")
        tab = request.query_params.get("tab", "workspaces")
        if tab not in ("workspaces", "access", "credentials", "roles", "settings", "operations", "activity"):
            tab = "workspaces"
        from .api_roles import list_roles_data
        with database.read() as c:
            rows = c.execute(
                "SELECT s.handle, s.title, s.state, s.autonomous, s.recovery_id, s.head_seq, m.role "
                "FROM session s LEFT JOIN member m ON m.session_id=s.id AND m.principal_id=? "
                "AND m.left_at IS NULL ORDER BY s.created_at DESC", (viewer.p.id,)).fetchall()
            role_templates = list_roles_data(c)
            revisions = c.execute(
                "SELECT r.name, r.version, r.prompt, r.updated_at, p.handle updated_by "
                "FROM role_template_revision r JOIN principal p ON p.id=r.updated_by "
                "ORDER BY r.name, r.version DESC").fetchall()
            people = [dict(r) for r in c.execute(
                "SELECT p.id,p.handle,p.kind,p.display_name,p.active,p.is_admin,owner.handle owner, "
                "(SELECT count(*) FROM web_session w WHERE w.principal_id=p.id AND w.revoked_at IS NULL AND w.expires_at>?) web_sessions, "
                "(SELECT count(*) FROM credential k WHERE k.principal_id=p.id AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>?)) live_tokens "
                "FROM principal p LEFT JOIN principal owner ON owner.id=p.owner_id WHERE p.kind!='system' ORDER BY p.kind,p.handle",
                (db.utcnow(), db.utcnow())).fetchall()]
            credentials = [dict(r) for r in c.execute(
                "SELECT k.id,k.scope,k.label,k.created_at,k.expires_at,k.revoked_at,k.last_used_at,p.handle "
                "FROM credential k JOIN principal p ON p.id=k.principal_id ORDER BY k.created_at DESC LIMIT 300").fetchall()]
            for cr in credentials:
                cr["state"] = token_state(cr)
            audits = [dict(r) for r in c.execute(
                "SELECT a.ts,a.action,a.target,a.data,p.handle actor FROM audit a "
                "LEFT JOIN principal p ON p.id=a.actor_id ORDER BY a.ts DESC LIMIT 150").fetchall()]
        history: dict[str, list[dict]] = {role["name"]: [] for role in role_templates}
        for revision in revisions:
            history[revision["name"]].append({**dict(revision),
                                               "prompt": render.r2(revision["prompt"], allow_newline=True)})
        for role in role_templates:
            role["history"] = history[role["name"]]
        for person in people:
            person["display_name"] = render.r2(person["display_name"] or "")
        for credential in credentials:
            credential["label"] = render.r2(credential["label"] or "")
        for audit in audits:
            audit["data"] = render.r2(audit["data"] or "")
        workspaces = [{"handle": r["handle"], "title": render.r2(r["title"]), "state": r["state"],
                       "autonomous": bool(r["autonomous"]), "recovery": r["recovery_id"],
                       "head": r["head_seq"], "role": r["role"], "external": False,
                       "inspect_url": f"/admin/s/{urllib.parse.quote(r['handle'], safe='')}"} for r in rows]
        external_errors = []
        for index, path in enumerate(request.app.state.admin_external_dbs):
            try:
                with contextlib.closing(_external_read(path)) as ext:
                    for r in ext.execute("SELECT handle,title,state,autonomous,recovery_id,head_seq FROM session ORDER BY created_at DESC"):
                        workspaces.append({"handle": r["handle"], "title": render.r2(r["title"]),
                                           "state": r["state"], "autonomous": bool(r["autonomous"]),
                                           "recovery": r["recovery_id"], "head": r["head_seq"],
                                           "role": None, "external": True, "source": index + 1,
                                           "inspect_url": f"/admin/external/{index}/s/{urllib.parse.quote(r['handle'], safe='')}"})
            except (OSError, sqlite3.Error):
                external_errors.append(index + 1)
        backup_dir = Path(database.path).resolve().parent / "backups"
        backups = sorted(({"name": p.name, "bytes": p.stat().st_size, "has_journal": Path(str(p) + ".journal").is_file(),
                           "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%MZ")}
                          for p in backup_dir.glob("*.age") if p.is_file()), key=lambda x: x["name"], reverse=True)[:20]
        settings = {"hosts": request.app.state.allowed_hosts, "plain_http": request.app.state.plain_http,
                    "mcp": request.app.state.mcp_enabled, "external_dbs": request.app.state.admin_external_dbs,
                    "external_env_dbs": request.app.state.admin_external_env_dbs,
                    "run_evidence_roots": request.app.state.admin_run_evidence_roots,
                    "run_evidence_env_roots": request.app.state.admin_run_evidence_env_roots,
                    "ca_published": Path(request.app.state.ca_file).is_file(),
                    "journal_exists": Path(request.app.state.journal_path).is_file(),
                    "sweep_interval": request.app.state.sweep_interval_s,
                    "allowed_cidrs": os.environ.get("GLASSBOARD_ALLOWED_CIDRS", "default private ranges")}
        storage = None
        if tab == "operations":
            from . import workspace_archive
            storage = workspace_archive.storage_view(database)
        return page("owner.html", viewer=viewer, workspaces=workspaces, roles=role_templates,
                    people=people, credentials=credentials, audits=audits, backups=backups,
                    settings=settings, storage=storage, tab=tab, op=ids.new_ulid(),
                    error=request.query_params.get("err"), external_errors=external_errors)

    def _admin_inspect_session(c, sess, viewer: Viewer, before: int | None, external: bool):
        from . import api_board
        timeline = _timeline(c, sess, {"joined_seq": 0}, viewer, "all", None, before, "oldest")
        board = board_for_display(api_board.board(c, sess), _run_status(c, sess),
                                  latest_task_note_bodies(c, sess["id"]))
        members = [dict(r) for r in c.execute(
            "SELECT p.handle,m.role,m.job_role FROM member m JOIN principal p ON p.id=m.principal_id "
            "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],))]
        joined = (not external and c.execute(
            "SELECT 1 FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
            (sess["id"], viewer.p.id)).fetchone() is not None)
        oldest = min((e["seq"] for e in timeline), default=0)
        return {"sess": sess, "title": render.r2(sess["title"]), "timeline": timeline,
                "older": oldest if oldest > 1 else None, "board": board,
                "members": members, "joined": joined, "external": external}

    @app.get("/admin/s/{s}", include_in_schema=False)
    def admin_inspect_session(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                              database: db.Database = Depends(get_db)):
        if not viewer.p.is_admin or viewer.p.scope != "full":
            raise ApiError(404, "not_found", "Not found.")
        before_raw = request.query_params.get("before", "")
        before = int(before_raw) if before_raw.isdigit() else None
        with database.read() as c:
            sess = sessions.find_session(c, s)
            if sess is not None:
                return page("admin_session.html", viewer=viewer, source=None,
                            **_admin_inspect_session(c, sess, viewer, before, False))
        # Older inspect links without a board number still work when the handle is unique.
        # The Admin listing always uses explicit source URLs for separate boards.
        matches = []
        for index, path in enumerate(request.app.state.admin_external_dbs):
            try:
                with contextlib.closing(_external_read(path)) as ext:
                    sess = sessions.find_session(ext, s)
                    if sess is not None:
                        matches.append(index)
            except (OSError, sqlite3.Error):
                continue
        if len(matches) > 1:
            raise ApiError(409, "ambiguous", "Session name exists on several boards; open it from Admin.")
        if matches:
            with contextlib.closing(_external_read(request.app.state.admin_external_dbs[matches[0]])) as ext:
                sess = sessions.find_session(ext, s)
                return page("admin_session.html", viewer=viewer, source=matches[0],
                            **_admin_inspect_session(ext, sess, viewer, before, True))
        raise ApiError(404, "not_found", "Not found.")

    @app.get("/admin/external/{index}/s/{s}", include_in_schema=False)
    def admin_inspect_external_session(index: int, s: str, request: Request,
                                       viewer: Viewer = Depends(current_viewer)):
        if not viewer.p.is_admin or viewer.p.scope != "full":
            raise ApiError(404, "not_found", "Not found.")
        paths = request.app.state.admin_external_dbs
        if index < 0 or index >= len(paths):
            raise ApiError(404, "not_found", "Not found.")
        before_raw = request.query_params.get("before", "")
        before = int(before_raw) if before_raw.isdigit() else None
        try:
            with contextlib.closing(_external_read(paths[index])) as ext:
                sess = sessions.find_session(ext, s)
                if sess is not None:
                    return page("admin_session.html", viewer=viewer, source=index,
                                **_admin_inspect_session(ext, sess, viewer, before, True))
        except (OSError, sqlite3.Error):
            raise ApiError(503, "unavailable", "Separate board is unavailable.") from None
        raise ApiError(404, "not_found", "Not found.")

    @app.post("/admin/password", include_in_schema=False)
    async def admin_change_password(request: Request, viewer: Viewer = Depends(current_viewer),
                                    database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        if not viewer.p.is_admin:
            raise ApiError(404, "not_found", "Not found.")
        with database.write() as c:
            if admin_auth.verify(c, viewer.p.handle, form.get("current_password", "")) is None:
                return _security_headers(RedirectResponse("/admin?tab=access&err=wrong_password", status_code=303))
            try:
                admin_auth.set_password(c, viewer.p.id, form.get("new_password", ""))
            except ValueError:
                return _security_headers(RedirectResponse("/admin?tab=access&err=invalid_password", status_code=303))
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), viewer.p.id, "admin_password.set", viewer.p.id, "{}"))
        resp = _security_headers(RedirectResponse("/admin/login", status_code=303))
        resp.delete_cookie(session_cookie(), path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")
        return resp

    def _require_admin(viewer: Viewer) -> None:
        if not viewer.p.is_admin or viewer.p.scope != "full" or viewer.p.kind != "human":
            raise ApiError(404, "not_found", "Not found.")

    @app.post("/admin/settings/boards", include_in_schema=False)
    async def admin_external_boards(request: Request, viewer: Viewer = Depends(current_viewer),
                                    database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        action = form.get("action", "")
        current = list(request.app.state.admin_external_dbs)
        if action == "add":
            path = form.get("path", "").strip()
            if not Path(path).is_absolute() or path in current:
                raise ApiError(422, "invalid_request", "Enter a new absolute database path.")
            try:
                with contextlib.closing(_external_read(path)) as ext:
                    ext.execute("SELECT handle FROM session LIMIT 1").fetchone()
            except (OSError, sqlite3.Error):
                raise ApiError(422, "invalid_request", "That path is not a readable Glassboard database.") from None
            updated = [*current, path]
        elif action == "remove":
            try:
                index = int(form.get("index", ""))
            except ValueError:
                raise ApiError(422, "invalid_request", "Invalid board selection.") from None
            if index < 0 or index >= len(current) or form.get("confirm") != "1":
                raise ApiError(422, "confirmation_required", "Confirm the board to remove.")
            updated = [path for n, path in enumerate(current) if n != index]
        elif action == "reset":
            if form.get("confirm") != "1":
                raise ApiError(422, "confirmation_required", "Confirm reset to host configuration.")
            updated = list(request.app.state.admin_external_env_dbs)
        else:
            raise ApiError(422, "invalid_request", "Unknown settings action.")
        with database.write() as c:
            if action == "reset":
                c.execute("DELETE FROM meta WHERE key='admin_external_dbs_override'")
            else:
                c.execute("INSERT INTO meta(key,value) VALUES ('admin_external_dbs_override',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(updated),))
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), viewer.p.id, "admin_external_boards.update", None,
                       json.dumps({"action": action, "before": current, "after": updated})))
        request.app.state.admin_external_dbs = updated
        return _security_headers(RedirectResponse("/admin?tab=settings", status_code=303))

    @app.post("/admin/settings/run-evidence", include_in_schema=False)
    async def admin_run_evidence_roots(request: Request, viewer: Viewer = Depends(current_viewer),
                                       database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        action = form.get("action", "")
        current = list(request.app.state.admin_run_evidence_roots)
        if action == "add":
            path = form.get("path", "").strip()
            candidate = Path(path)
            if not candidate.is_absolute() or path in current or candidate.is_symlink() or not candidate.is_dir():
                raise ApiError(422, "invalid_request", "Enter a new absolute run-evidence directory.")
            updated = [*current, path]
        elif action == "remove":
            try:
                index = int(form.get("index", ""))
            except ValueError:
                raise ApiError(422, "invalid_request", "Invalid run-evidence root selection.") from None
            if index < 0 or index >= len(current) or form.get("confirm") != "1":
                raise ApiError(422, "confirmation_required", "Confirm the run-evidence root to remove.")
            updated = [path for n, path in enumerate(current) if n != index]
        elif action == "reset":
            if form.get("confirm") != "1":
                raise ApiError(422, "confirmation_required", "Confirm reset to host configuration.")
            updated = list(request.app.state.admin_run_evidence_env_roots)
        else:
            raise ApiError(422, "invalid_request", "Unknown settings action.")
        with database.write() as c:
            if action == "reset":
                c.execute("DELETE FROM meta WHERE key='admin_run_evidence_roots_override'")
            else:
                c.execute("INSERT INTO meta(key,value) VALUES ('admin_run_evidence_roots_override',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(updated),))
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), viewer.p.id, "admin_run_evidence_roots.update", None,
                       json.dumps({"action": action, "before": current, "after": updated})))
        request.app.state.admin_run_evidence_roots = updated
        return _security_headers(RedirectResponse("/admin?tab=settings", status_code=303))

    @app.post("/admin/access/create", include_in_schema=False)
    async def admin_access_create(request: Request, viewer: Viewer = Depends(current_viewer),
                                  database: db.Database = Depends(get_db)):
        from . import admin_web
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        admin_web.create_principal(database, viewer.p, kind=form.get("kind", ""), name=form.get("name", ""),
                                   owner=form.get("owner", "").strip(), display_name=form.get("display_name", ""),
                                   admin=form.get("admin") == "1")
        return _security_headers(RedirectResponse("/admin?tab=access", status_code=303))

    @app.post("/admin/access/change", include_in_schema=False)
    async def admin_access_change(request: Request, viewer: Viewer = Depends(current_viewer),
                                  database: db.Database = Depends(get_db)):
        from . import admin_web
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        handle = form.get("handle", "")
        if not handle or form.get("confirm") != handle:
            raise ApiError(422, "confirmation_required", "Type the exact handle to confirm the access change.")
        request.state.flash_ok = {"grant-admin": "admin_granted", "revoke-admin": "admin_revoked",
                                  "deactivate": "deactivated", "revoke-sessions": "sessions_revoked"}.get(
                                      form.get("action", ""))
        admin_web.change_access(database, viewer.p, request.app.state.journal_path,
                                action=form.get("action", ""), handle=handle)
        return _security_headers(RedirectResponse("/admin?tab=access", status_code=303))

    @app.post("/admin/access/password", include_in_schema=False)
    async def admin_access_password(request: Request, viewer: Viewer = Depends(current_viewer),
                                    database: db.Database = Depends(get_db)):
        from . import admin_web
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        target = form.get("handle", "")
        admin_web.reset_password(database, viewer.p, handle=target,
                                 current=form.get("current_password", ""), new=form.get("new_password", ""))
        if target == viewer.p.handle:
            resp = _security_headers(RedirectResponse("/admin/login", status_code=303))
            resp.delete_cookie(session_cookie(), path="/", secure=not PLAIN_HTTP.get(), httponly=True, samesite="strict")
            return resp
        return _security_headers(RedirectResponse("/admin?tab=access", status_code=303))

    @app.post("/admin/credentials/create", include_in_schema=False)
    async def admin_credential_create(request: Request, viewer: Viewer = Depends(current_viewer),
                                      database: db.Database = Depends(get_db)):
        from . import admin_web
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        days_text = form.get("days", "").strip()
        try:
            days = int(days_text) if days_text else None
        except ValueError:
            raise ApiError(422, "invalid_request", "Expiry must be a number of days.") from None
        issued = admin_web.mint_token(database, viewer.p, handle=form.get("handle", ""),
                                      scope=form.get("scope", ""), days=days, label=form.get("label", ""))
        return page("admin-token-once.html", viewer=viewer, **issued)

    @app.post("/admin/credentials/revoke", include_in_schema=False)
    async def admin_credential_revoke(request: Request, viewer: Viewer = Depends(current_viewer),
                                      database: db.Database = Depends(get_db)):
        from . import admin_web
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        if form.get("confirm") != "1":
            raise ApiError(422, "confirmation_required", "Confirm token revocation.")
        admin_web.revoke_token(database, viewer.p, request.app.state.journal_path, form.get("credential", ""))
        return _security_headers(RedirectResponse("/admin?tab=credentials", status_code=303))

    @app.post("/admin/backups/create", include_in_schema=False)
    async def admin_backup_create(request: Request, viewer: Viewer = Depends(current_viewer),
                                  database: db.Database = Depends(get_db)):
        from . import backup
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        recipient = form.get("recipient", "").strip()
        if not recipient.startswith("age1") or len(recipient) > 200:
            raise ApiError(422, "invalid_request", "Enter an age public recipient beginning with age1.")
        folder = Path(database.path).resolve().parent / "backups"
        folder.mkdir(mode=0o700, exist_ok=True)
        filename = f"glassboard-{_now().strftime('%Y%m%dT%H%M%S')}-{ids.new_ulid()}.age"
        try:
            result = backup.backup(database, str(folder / filename), request.app.state.journal_path,
                                   [recipient], False)
        except backup.RestoreError:
            (folder / (filename + ".journal")).unlink(missing_ok=True)
            raise ApiError(503, "backup_failed", "Encrypted backup could not be created. Check the age recipient and host setup.") from None
        with database.write() as c:
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), viewer.p.id, "backup.create", filename,
                       json.dumps({"encrypted": True, "journal_copy": bool(result.get("journal_copy"))})))
        return _security_headers(RedirectResponse("/admin?tab=operations", status_code=303))

    @app.get("/admin/backups/{name}", include_in_schema=False)
    def admin_backup_download(name: str, viewer: Viewer = Depends(current_viewer),
                              database: db.Database = Depends(get_db)):
        _require_admin(viewer)
        if not (name.startswith("glassboard-") and (name.endswith(".age") or name.endswith(".age.journal"))) or "/" in name or ".." in name:
            raise ApiError(404, "not_found", "Backup not found.")
        folder = Path(database.path).resolve().parent / "backups"
        path = folder / name
        if not path.is_file():
            raise ApiError(404, "not_found", "Backup not found.")
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post("/admin/workspaces/archive", include_in_schema=False)
    async def admin_workspace_archive(request: Request, viewer: Viewer = Depends(current_viewer),
                                      database: db.Database = Depends(get_db)):
        from . import workspace_archive
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        handle = form.get("handle", "").strip()       # analysis F01: archiving deletes nothing, so no typing
        if not handle:
            return _security_headers(RedirectResponse(
                "/admin?tab=operations&err=invalid_request", status_code=303))
        try:
            workspace_archive.create_archive(database, handle, viewer.p.id)
        except workspace_archive.WorkspaceArchiveError:
            return _security_headers(RedirectResponse(
                "/admin?tab=operations&err=archive_failed", status_code=303))
        return _security_headers(RedirectResponse("/admin?tab=operations", status_code=303))

    @app.get("/admin/workspace-archives/{archive_id}", include_in_schema=False)
    def admin_workspace_archive_download(archive_id: str, viewer: Viewer = Depends(current_viewer),
                                         database: db.Database = Depends(get_db)):
        from . import workspace_archive
        _require_admin(viewer)
        row = workspace_archive.archive_record(database, archive_id)
        if row is None:
            raise ApiError(404, "not_found", "Workspace archive not found.")
        path = workspace_archive.archive_path(database, row)
        try:
            workspace_archive.verify_archive(path, row)
        except workspace_archive.WorkspaceArchiveError:
            raise ApiError(409, "archive_failed", "Workspace archive verification failed.") from None
        return FileResponse(path, filename=row["file_name"], media_type="application/zip")

    @app.post("/admin/workspaces/purge", include_in_schema=False)
    async def admin_workspace_purge(request: Request, viewer: Viewer = Depends(current_viewer),
                                    database: db.Database = Depends(get_db)):
        from . import workspace_archive
        form = await form_data(request)
        check_post(request, form, viewer)
        _require_admin(viewer)
        handle = form.get("handle", "").strip()
        if form.get("irreversible") != "1" or not handle or not hmac.compare_digest(
                handle, form.get("confirm", "").strip()):
            return _security_headers(RedirectResponse(
                "/admin?tab=operations&err=confirmation_required", status_code=303))
        try:
            workspace_archive.purge_workspace(
                database, request.app.state.journal_path, handle,
                form.get("archive_id", "").strip(), viewer.p.id,
            )
        except (workspace_archive.WorkspaceArchiveError, journal.JournalWriteError):
            return _security_headers(RedirectResponse(
                "/admin?tab=operations&err=purge_failed", status_code=303))
        return _security_headers(RedirectResponse("/admin?tab=operations", status_code=303))

    @app.post("/owner/roles/{name}", include_in_schema=False)
    async def owner_role_edit(name: str, request: Request, viewer: Viewer = Depends(current_viewer),
                              database: db.Database = Depends(get_db)):
        from .api_roles import RoleUpdate, update_role
        form = await form_data(request)
        check_post(request, form, viewer)
        if not viewer.p.is_admin:
            raise ApiError(404, "not_found", "Not found.")
        try:
            body = RoleUpdate(prompt=form.get("prompt", ""), if_version=int(form.get("if_version", "")))
            update_role(database, viewer.p, name, body, form.get("op") or ids.new_ulid())
        except (ValidationError, ValueError):
            return _security_headers(RedirectResponse("/admin?tab=roles&err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in ("version_conflict", "invalid_request", "secret_in_content", "idempotency_key_reused"):
                return _security_headers(RedirectResponse(f"/admin?tab=roles&err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse("/admin?tab=roles", status_code=303))

    @app.post("/owner/roles", include_in_schema=False)
    async def owner_role_create(request: Request, viewer: Viewer = Depends(current_viewer),
                                database: db.Database = Depends(get_db)):
        form = await form_data(request)
        return await owner_role_edit(form.get("name", ""), request, viewer, database)

    @app.post("/owner/s/{s}/join", include_in_schema=False)
    async def owner_join(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                         database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        if not viewer.p.is_admin:
            raise ApiError(404, "not_found", "Not found.")
        key = form.get("op", "")
        if not idempotency.KEY_RE.fullmatch(key):
            raise ApiError(422, "invalid_request", "The request failed validation.")
        with database.write() as c:
            sess = sessions.find_session(c, s)
            if sess is None:
                raise ApiError(404, "not_found", "Not found.")
            sessions.check_recovery(sess, form.get("recovery"))
            digest = idempotency.digest("POST /owner/s/{s}/join", viewer.p.id,
                                        {"role": "coordinator"}, session_id=sess["id"])
            prior = c.execute("SELECT req_digest, receipt FROM event WHERE session_id=? AND actor_id=? AND op_id=?",
                              (sess["id"], viewer.p.id, key)).fetchone()
            if idempotency.check_replay(prior, digest, 200) is not None:
                return _security_headers(RedirectResponse(f"/s/{sess['handle']}", status_code=303))
            row = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?",
                            (sess["id"], viewer.p.id)).fetchone()
            if row is not None and row["left_at"] is None:
                return _security_headers(RedirectResponse(f"/s/{sess['handle']}", status_code=303))
            sessions.require_state(sess, "member.add")
            recipients, broadcast = routing.route(c, sess["id"], "member.added", viewer.p.id,
                                                  member=viewer.p.id)
            seq, _ = events.append(c, sess["id"], viewer.p.id, "member.added", recipients=recipients,
                                   broadcast=broadcast, op_id=key, req_digest=digest,
                                   data={"handle": viewer.p.handle, "role": "coordinator",
                                         "reason": "Admin console join"},
                                   receipt=lambda n: idempotency.receipt_body(
                                       {"member": viewer.p.handle, "role": "coordinator", "change": "added", "seq": n}))
            now = db.utcnow()
            if row is None:
                c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at) "
                          "VALUES (?,?,?,?,?,?,?)", (sess["id"], viewer.p.id, "coordinator", seq, seq - 1, seq - 1, now))
            else:
                c.execute("UPDATE member SET role='coordinator', job_role=NULL, job_role_version=NULL, joined_seq=?, cursor_seq=?, delivered_seq=?, "
                          "joined_at=?, left_at=NULL, consumer_secret=NULL, consumer_runtime=NULL, "
                          "consumer_since=NULL, consumer_gen=consumer_gen+1, wake_inspected=?, " + db.WAKE_RESET_SQL +
                          " WHERE session_id=? AND principal_id=?",
                          (seq, seq - 1, seq - 1, now, seq - 1, sess["id"], viewer.p.id))
        return _security_headers(RedirectResponse(f"/s/{sess['handle']}", status_code=303))

    @app.post("/s/{s}/approvals/{seq}", include_in_schema=False)
    async def approval_form(s: str, seq: int, request: Request, viewer: Viewer = Depends(current_viewer),
                            database: db.Database = Depends(get_db)):
        """Grant or deny an approval request from the home page (the same code path as the API)."""
        from . import api_messages
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            body = api_messages.ApprovalIn(decision=form.get("decision", ""), note=form.get("note") or None)
            request.state.flash_ok = {"grant": "approval_granted", "deny": "approval_denied"}.get(body.decision)
            api_messages.decide_approval(s, seq, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _security_headers(RedirectResponse("/?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in ("forbidden", "already_resolved", "not_found", "session_archived"):
                return _security_headers(RedirectResponse(f"/?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse("/", status_code=303))

    @app.post("/s/{s}/holds/{seq}", include_in_schema=False)
    async def hold_form(s: str, seq: int, request: Request, viewer: Viewer = Depends(current_viewer),
                        database: db.Database = Depends(get_db)):
        """Uphold a hold or let the work proceed, from the home page (same code path as the API, B41)."""
        from . import api_holds
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            body = api_holds.HoldDecision(decision=form.get("decision", ""), note=form.get("note") or None)
            request.state.flash_ok = {"uphold": "hold_upheld", "proceed": "hold_proceed"}.get(body.decision)
            api_holds.decide_hold(s, seq, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _security_headers(RedirectResponse("/?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in ("forbidden", "already_resolved", "not_found", "session_archived"):
                return _security_headers(RedirectResponse(f"/?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse("/", status_code=303))

    def _standdown_back(s: str, who: str, err: str | None = None) -> RedirectResponse:
        q = urllib.parse.urlencode({"as": who, **({"err": err} if err else {})})
        return _security_headers(RedirectResponse(f"/s/{s}/agent?{q}", status_code=303))

    @app.post("/s/{s}/standdowns", include_in_schema=False)
    async def standdown_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                             database: db.Database = Depends(get_db)):
        """Ask an agent to stand down, from its agent view (same code path as the API, B41)."""
        from . import api_holds
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        who = form.get("member", "")
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            body = api_holds.StanddownIn(member=who, reason=form.get("reason", ""))
            api_holds.request_standdown(s, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _standdown_back(s, who, "invalid_request")
        except ApiError as e:
            if e.code in ("forbidden", "already_requested", "not_found", "session_archived"):
                return _standdown_back(s, who, e.code)
            raise
        return _standdown_back(s, who)

    @app.post("/s/{s}/standdowns/{seq}/lift", include_in_schema=False)
    async def standdown_lift_form(s: str, seq: int, request: Request, viewer: Viewer = Depends(current_viewer),
                                  database: db.Database = Depends(get_db)):
        from . import api_holds
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        who = form.get("member", "")
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            body = api_holds.StanddownNote(note=form.get("note") or None)
            api_holds.lift_standdown(s, seq, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _standdown_back(s, who, "invalid_request")
        except ApiError as e:
            if e.code in ("forbidden", "already_resolved", "not_found", "session_archived"):
                return _standdown_back(s, who, e.code)
            raise
        return _standdown_back(s, who)

    @app.post("/s/{s}/quiet-hours", include_in_schema=False)
    async def quiet_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                         database: db.Database = Depends(get_db)):
        """Set or clear quiet hours from the agent view (same code path as the API, B56)."""
        from . import api_quiet
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        who = form.get("member", "")
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            if form.get("action") == "clear":
                body = api_quiet.QuietIn(member=who)
            else:
                body = api_quiet.QuietIn(member=who, start=form.get("start", "")[:5], end=form.get("end", "")[:5],
                                         tz=form.get("tz", "").strip(), urgent=form.get("urgent") == "1")
            api_quiet.set_quiet(s, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _standdown_back(s, who, "invalid_request")
        except ApiError as e:
            if e.code in ("forbidden", "invalid_request", "not_found", "session_archived"):
                return _standdown_back(s, who, e.code)
            raise
        return _standdown_back(s, who)

    def _channel_back(s: str, err: str | None = None, to_chat: bool = False, channel: str | None = None) -> RedirectResponse:
        if to_chat:                                   # 0.1.7: from the Chat side panel (back to the same channel)
            q = urllib.parse.urlencode({k: v for k, v in (("channel", channel), ("err", err)) if v})
            return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}/chat" + (f"?{q}" if q else ""),
                                                      status_code=303))
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}" + (f"?err={err}" if err else "") + "#channels",
                                                  status_code=303))

    @app.post("/s/{s}/channels", include_in_schema=False)
    async def channel_create_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                                  database: db.Database = Depends(get_db)):
        """B55: create a topic channel from the session page (same code path as the API)."""
        from . import api_channels
        form = await form_data(request)
        check_post(request, form, viewer)
        to_chat = form.get("return_to") == "chat"
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        try:
            body = api_channels.ChannelIn(name=form.get("name", "").strip().lstrip("#"), topic=form.get("topic") or None,
                                          members=[h.strip().lstrip("@") for h in form.get("members", "").split(",") if h.strip()])
            api_channels.create_channel(s, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _channel_back(s, "invalid_request", to_chat)
        except ApiError as e:
            if e.code in ("forbidden", "channel_exists", "recipient_not_member", "session_archived", "invalid_request"):
                return _channel_back(s, e.code, to_chat)
            raise
        return _channel_back(s, None, to_chat)

    @app.post("/s/{s}/channels/{name}/mode", include_in_schema=False)
    async def channel_mode_form(s: str, name: str, request: Request, viewer: Viewer = Depends(current_viewer),
                                database: db.Database = Depends(get_db)):
        from . import api_channels
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        to_chat = form.get("return_to") == "chat"           # B67: the mode buttons in the Chat channel list
        back_channel = form.get("return_channel", "")
        back_channel = back_channel if api_channels.NAME_RE.match(back_channel) else None
        try:
            api_channels.set_mode(s, name, api_channels.ModeIn(mode=form.get("mode", "")), key, sess["recovery_id"], None,
                                  viewer.p, database)
        except ValidationError:
            return _channel_back(s, "invalid_request", to_chat, back_channel)
        except ApiError as e:
            if e.code in ("not_a_channel_member", "not_found", "session_archived"):
                return _channel_back(s, e.code, to_chat, back_channel)
            raise
        return _channel_back(s, None, to_chat, back_channel)

    @app.get("/attention-count", include_in_schema=False)
    def attention_count(viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """For browser notifications: only a number, never participant text."""
        from . import api_board
        from . import quiet
        with database.read() as c:
            items = api_board.attention(c, viewer.p)["items"]
            # B56: items from sessions where this human is in quiet hours still count, but do not notify
            hushed = set()
            for m in c.execute("SELECT s.handle, m.* FROM member m JOIN session s ON s.id=m.session_id "
                               "WHERE m.principal_id=? AND m.left_at IS NULL AND m.quiet_start IS NOT NULL", (viewer.p.id,)):
                q = quiet.of_member(m)
                if q and q["active"]:
                    hushed.add(m["handle"])
        notify = sum(1 for i in items if i["session"] not in hushed)
        return _security_headers(JSONResponse({"count": len(items), "notify": notify}))

    @app.get("/s/{s}/board", include_in_schema=False)
    def board_page(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        from . import api_board, api_status
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            b = board_for_display(api_board.board(c, sess), _run_status(c, sess),
                                  latest_task_note_bodies(c, sess["id"]), task_counts(c, sess["id"]))
            agents = agent_rows_for_display(api_status.agent_status(c, sess))
            from . import api_blockers
            blockers = api_blockers.blockers(c, sess)
            for i in blockers["blockers"]:                      # participant text: R2/R3 before HTML
                i["title_line"] = render.inline(i["title"], 120) if i.get("title") else None
                i["text_line"] = render.inline(i["text"], 120) if i.get("text") else None
        return page("board.html", viewer=viewer, sess=sess, me=me, board=b, agents=agents, blockers=blockers)

    @app.get("/s/{s}/board/cards", include_in_schema=False)
    def board_cards(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """B38: the kanban columns alone, refreshed in place every few seconds (same-origin fragment)."""
        from . import api_board
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            b = board_for_display(api_board.board(c, sess), _run_status(c, sess),
                                  latest_task_note_bodies(c, sess["id"]), task_counts(c, sess["id"]))
        response = page("kanban-cards.html", viewer=viewer, sess=sess, me=me, board=b)
        response.headers["X-Glassboard-Fragment"] = "poll"
        return response

    @app.get("/s/{s}/board/agents", include_in_schema=False)
    def board_agents(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """B39: the agents' status rows alone, refreshed in place with the board (same-origin fragment)."""
        from . import api_status
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            agents = agent_rows_for_display(api_status.agent_status(c, sess))
        response = page("agent-status.html", viewer=viewer, sess=sess, me=me, agents=agents)
        response.headers["X-Glassboard-Fragment"] = "poll"
        return response

    @app.get("/s/{s}/artifacts", include_in_schema=False)
    def artifacts_page(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        from . import api_artifacts
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            rows = c.execute("SELECT * FROM artifact WHERE session_id=? ORDER BY created_at DESC, id DESC",
                             (sess["id"],)).fetchall()
            artifacts = [api_artifacts.artifact_view(c, row) for row in rows]
            tasks = [row["num"] for row in c.execute("SELECT num FROM task WHERE session_id=? ORDER BY num", (sess["id"],))]
        return page("artifacts.html", viewer=viewer, sess=sess, me=me, artifacts=artifacts,
                    deliverables=artifact_groups([item for item in artifacts if item["purpose"] == "deliverable"]),
                    evidence=artifact_groups([item for item in artifacts if item["purpose"] == "evidence"]), tasks=tasks,
                    can_upload=viewer.p.scope == "full" and me["role"] != "observer", op=ids.new_ulid())

    @app.post("/s/{s}/artifacts/upload", include_in_schema=False)
    async def artifact_upload_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                                   database: db.Database = Depends(get_db)):
        from . import api_artifacts
        check_raw_post(request, viewer)
        q = request.query_params
        key = request.headers.get("Idempotency-Key", "")
        if not (8 <= len(key) <= 64):
            raise ApiError(422, "invalid_request", "Missing or invalid upload operation ID.")
        return api_artifacts.create_artifact(
            database, viewer.p, s, await request.body(), name=q.get("name", ""),
            media_type=q.get("media_type", "application/octet-stream"), description=q.get("description") or None,
            task=q.get("task") or None, purpose=q.get("purpose", "evidence"), key=key,
            recovery=request.headers.get("X-Glassboard-Recovery"), runtime=None)

    @app.get("/s/{s}/artifacts/{artifact_id}/download", include_in_schema=False)
    def artifact_download(s: str, artifact_id: str, viewer: Viewer = Depends(current_viewer),
                          database: db.Database = Depends(get_db)):
        from . import api_artifacts
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            row = api_artifacts._load(c, sess["id"], artifact_id)
            content, name, original_type = bytes(row["content"]), row["name"], row["media_type"]
        encoded = urllib.parse.quote(name, safe="")
        return _security_headers(Response(content, media_type="application/octet-stream",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
                                          "X-Glassboard-Content-Type": original_type,
                                          "X-Content-Type-Options": "nosniff"}))

    @app.get("/s/{s}/t/{n}/replay", include_in_schema=False)
    def task_replay(s: str, n: int, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """E44: the task's recorded events as a sequence between members, with step controls."""
        from . import replay
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            t = c.execute("SELECT num, title FROM task WHERE session_id=? AND num=?", (sess["id"], n)).fetchone()
            if t is None:
                raise ApiError(404, "not_found", "No such task.")
            r = replay.replay(c, sess["id"], n)
        return page("replay.html", viewer=viewer, sess=sess, me=me, t={"task": t["num"]}, r=r,
                    title_line=render.inline(t["title"], render.INLINE_LIMITS["title"]))

    @app.get("/s/{s}/now", include_in_schema=False)
    def now_page(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """E42: the session at a glance: task counts, what needs you ranked with its action, and agents."""
        from . import api_status, nowview
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            todo = nowview.items(c, sess, viewer.p)
            counts = {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM task WHERE session_id=? GROUP BY status",
                                                     (sess["id"],))}
            agents = agent_rows_for_display(api_status.agent_status(c, sess))
            unread_rows, _ = inbox.inbox_page(c, sess, me, me["cursor_seq"], 1000)
        order = ("open", "in_progress", "blocked", "review", "done", "cancelled")
        return page("now.html", viewer=viewer, sess=sess, me=me, todo=todo, agents=agents, unread=len(unread_rows),
                    counts=[(k, counts.get(k, 0)) for k in order], total=sum(counts.values()),
                    title_line=render.inline(sess["title"]),
                    can_coordinate=viewer.p.kind == "human" and me["role"] == "coordinator")

    @app.get("/s/{s}/jump.json", include_in_schema=False)
    def jump_list(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """E42: what the jump box (Ctrl/Cmd+K) can open in this session: views, tasks, members. Labels pass
        R2/R3 first; the browser inserts them as text, never as HTML."""
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            tasks = c.execute("SELECT num, title, status FROM task WHERE session_id=? ORDER BY num DESC LIMIT 300",
                              (sess["id"],)).fetchall()
            members = [r[0] for r in c.execute("SELECT p.handle FROM member m JOIN principal p ON p.id=m.principal_id "
                                                "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],))]
        sh = sess["handle"]
        views = [("Now", "/now"), ("Chat", "/chat"), ("Timeline", ""), ("Conversations", "/conversations"), ("Board", "/board"),
                 ("Progress", "/progress"), ("Artifacts", "/artifacts")]
        out = [{"kind": "view", "label": label, "href": f"/s/{sh}{path}"} for label, path in views]
        out += [{"kind": "task", "label": f"T{t['num']} {render.r2(t['title'])[:120]} ({t['status'].replace('_', ' ')})",
                 "href": f"/s/{sh}/t/{t['num']}"} for t in tasks]
        out += [{"kind": "member", "label": f"@{h}", "href": f"/s/{sh}/agent?as={h}"} for h in members]
        out += [{"kind": "member", "label": f"@{h} activity", "href": f"/s/{sh}?f=all&actor={h}&density=compact"}
                for h in members]
        return _security_headers(JSONResponse({"items": out}))

    @app.get("/s/{s}/search", include_in_schema=False)
    def search_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                    database: db.Database = Depends(get_db)):
        """B45: the same search as GET /api/v1/sessions/{s}/search, as a page."""
        from . import api_search
        q = (request.query_params.get("q") or "")[:200]
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            r = api_search.search(c, sess, q, 50) if q.strip() else None
        tasks = [{**t, "title_line": render.inline(t["title"], 140)} for t in (r or {}).get("tasks", [])]
        return page("search.html", viewer=viewer, sess=sess, me=me, search_q=q if render.r2(q) == q else "",
                    tasks=tasks, events=[event_card(v) for v in (r or {}).get("events", [])],
                    more=(r or {}).get("more"), searched=r is not None,
                    can_coordinate=viewer.p.kind == "human" and me["role"] == "coordinator")

    @app.get("/s/{s}/progress", include_in_schema=False)
    def progress_page(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """A read-only account of task ownership and recorded work milestones."""
        from .api_tasks import task_view

        milestones = {
            "task.created": "Assigned",
            "task.acknowledged": "Acknowledged",
            "task.claimed": "Started",
            "task.checked": "Checklist completed",
            "task.submitted": "Submitted for review",
            "task.reviewed": "Peer reviewed",
            "task.accepted": "Accepted",
            "task.returned": "Returned for changes",
            "task.blocked": "Blocked",
            "task.unblocked": "Resumed",
            "task.cancelled": "Cancelled",
        }
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            rows = c.execute("SELECT * FROM task WHERE session_id=? ORDER BY num",
                             (sess["id"],)).fetchall()
            tasks = []
            for row in rows:
                item = task_view(c, row, sess)
                item["title"] = render.r2(item["title"])
                item["deliverable"] = render.r2(item["deliverable"])
                item["summary"] = render.r2(item["summary"] or "")
                tasks.append(item)
            counts = {status: sum(t["status"] == status for t in tasks)
                      for status in ("open", "in_progress", "blocked", "review", "done", "cancelled")}
            status = run_status(tasks)
            overall = {key: status[key] for key in ("accepted", "total", "percent")}
            contributor_map = {}
            for task in tasks:
                handle = task["claimant"] or task["assignee"]
                if handle:
                    contributor_map.setdefault(handle, []).append(task)
            contributors = [{"handle": handle, "tasks": owned,
                             "accepted": sum(t["status"] == "done" for t in owned)}
                            for handle, owned in sorted(contributor_map.items())]
            event_rows = c.execute(
                "SELECT * FROM event WHERE session_id=? AND type IN ("
                "'task.created','task.acknowledged','task.claimed','task.checked','task.submitted','task.reviewed',"
                "'task.accepted','task.returned','task.blocked','task.unblocked','task.cancelled') "
                "ORDER BY seq DESC LIMIT 100", (sess["id"],)).fetchall()
            events = []
            for ev in reversed(inbox.views(c, event_rows)):
                data = ev["data"] or {}
                if ev["type"] == "task.checked" and not data.get("done"):
                    continue
                detail = ""
                if ev["type"] == "task.checked":
                    detail = data.get("title") or ""
                elif ev["type"] == "task.submitted":
                    detail = data.get("summary") or ""
                    if data.get("supersedes"):                          # E48: a revision of a submission in review
                        detail = f"Revises #{int(data['supersedes'])}: {detail}"
                elif ev["type"] == "task.reviewed":
                    detail = f"{data.get('verdict', '')}: {ev['body'] or ''}".strip(": ")
                elif ev["type"] in ("task.returned", "task.blocked", "task.unblocked", "task.accepted", "task.cancelled"):
                    detail = ev["body"] or ""
                events.append({"seq": ev["seq"], "time": ev["ts"][:16].replace("T", " ") + "Z",
                               "actor": ev["actor"], "task": ev["task"], "kind": ev["type"],
                               "label": milestones[ev["type"]], "detail": render.r2(detail, allow_newline=True)})
        return page("progress.html", viewer=viewer, sess=sess, me=me, tasks=tasks,
                    counts=counts, overall=overall, contributors=contributors, events=events)

    def _timeline(c, sess, me, viewer: Viewer, flt: str, task: int | None, before: int | None,
                  order: str, actor: str | None = None, channel: str | None = None) -> list[dict]:
        where, args = ["e.session_id = :s"], {"s": sess["id"]}
        if actor:                                           # E42: one member's events
            where.append("e.actor_id = (SELECT id FROM principal WHERE handle = :actor)"); args["actor"] = actor
        if channel:                                         # B55: one channel's events
            where.append("e.channel = :channel"); args["channel"] = channel
        where.append("e.type = 'message.preflight'" if flt == "trace" else "e.type != 'message.preflight'")
        if before:
            where.append("e.seq < :before"); args["before"] = before
        if flt == "work":
            where.append("e.type NOT IN ('lease.taken','lease.released','lease.expired')")
        if flt == "me":
            where.append("e.actor_id != :p AND (EXISTS (SELECT 1 FROM event_recipient r WHERE r.session_id = e.session_id "
                         "AND r.seq = e.seq AND r.principal_id = :p) OR (e.broadcast = 1 AND e.seq > :joined))")
            args.update(p=viewer.p.id, joined=me["joined_seq"])
        elif flt == "questions":
            where.append("e.type IN ('question', 'answer', 'question.resolved')")
        elif flt == "decisions":
            where.append("e.type = 'decision'")
        elif flt == "task" and task:
            where.append("e.task_num = :task"); args["task"] = task
        rows = c.execute(f"SELECT e.* FROM event e WHERE {' AND '.join(where)} ORDER BY e.seq DESC LIMIT {TIMELINE_LIMIT}",
                         args).fetchall()
        views = inbox.views(c, rows)
        views = list(reversed(views)) if order == "oldest" else views
        return [event_card(v) for v in views] if not views else _with_reply_context(c, sess, views)

    def _with_reply_context(c, sess, views) -> list[dict]:
        """B35: cards, with a short quote of each referenced event and which questions are answered."""
        cards = [event_card(v) for v in views]
        refs = sorted({card["head"]["re"] for card in cards if card["head"] and card["head"]["re"]})
        quoted = {}
        if refs:
            marks = ",".join("?" * len(refs))
            for r in c.execute(f"SELECT seq, type, body FROM event WHERE session_id=? AND seq IN ({marks})",
                               (sess["id"], *refs)):
                first = next((ln for ln in (r["body"] or "").splitlines() if ln.strip()), "")
                quoted[r["seq"]] = {"type": r["type"], "text": render.inline(first, 90)[1:-1] if first else ""}
        questions = [card["seq"] for card in cards if card["type"] == "question"]
        answered = set()
        if questions:
            marks = ",".join("?" * len(questions))
            answered = {r[0] for r in c.execute(
                f"SELECT DISTINCT reply_to FROM event WHERE session_id=? AND type IN ('answer','question.resolved') "
                f"AND reply_to IN ({marks})", (sess["id"], *questions))}
        for card in cards:
            if card["head"] and card["head"]["re"]:
                card["head"]["re_quote"] = quoted.get(card["head"]["re"])
            card["answered"] = card["seq"] in answered
        return cards

    def _board(c, sess) -> dict:
        """Board panel: tasks grouped by status (claimant, gen, activity age, stale) and owed
        questions per member."""
        from .api_tasks import task_view
        groups = {st: [] for st in ("open", "in_progress", "blocked", "review", "done", "cancelled")}
        for t in c.execute("SELECT * FROM task WHERE session_id=? ORDER BY num", (sess["id"],)):
            v = task_view(c, t, sess)
            v["title_line"] = render.inline(v["title"], 80)
            v["age_min"] = v["activity_age_s"] // 60 if v["activity_age_s"] is not None else None
            groups[v["status"]].append(v)
        owed = []
        for m in c.execute("SELECT p.id, p.handle FROM member m JOIN principal p ON p.id = m.principal_id "
                           "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],)):
            shown, total = inbox.owed(c, sess["id"], m["id"], show=1)
            if total:
                owed.append({"handle": m["handle"], "total": total, "oldest": shown[0]["seq"],
                             "age_min": shown[0]["age_s"] // 60})
        return {"groups": groups, "owed": owed}

    def _run_status(c, sess) -> dict:
        from .api_tasks import task_view
        tasks = [task_view(c, row, sess) for row in
                 c.execute("SELECT * FROM task WHERE session_id=? ORDER BY num", (sess["id"],))]
        return run_status(tasks)

    def _session_ctx(request: Request, s: str, viewer: Viewer, database: db.Database):
        flt = request.query_params.get("f", "all")
        flt = flt if flt in FILTERS else "all"
        task = request.query_params.get("t")
        task = int(task) if task and task.isdigit() else None
        before = request.query_params.get("before")
        before = int(before) if before and before.isdigit() else None
        order = request.query_params.get("order", "oldest")
        order = order if order in ("oldest", "newest") else "oldest"
        density = "compact" if request.query_params.get("density") == "compact" else None
        actor = request.query_params.get("actor") or None
        if actor is not None and not sessions.PRINCIPAL_HANDLE_RE.match(actor):
            actor = None
        from .api_channels import NAME_RE as _CH
        channel = request.query_params.get("channel") or None
        if channel is not None and not _CH.match(channel):
            channel = None
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            unread_rows, _ = inbox.inbox_page(c, sess, me, me["cursor_seq"], 1000)
            members = c.execute("SELECT m.role, m.job_role, m.job_role_version, m.last_seen_at, p.handle, p.kind, p.display_name, "
                                "m.principal_id, m.joined_seq, m.wake_secret, m.wake_state, m.wake_adapter, m.wake_inspected, "
                                "m.wake_runtime, m.wake_since, m.wake_gen, m.wake_outstanding, m.wake_dispatched_at, "
                                "m.wake_hold, m.wake_hold_seq, "
                                "EXISTS(SELECT 1 FROM invite i WHERE i.session_id=m.session_id AND i.agent_id=m.principal_id "
                                "AND i.used_at IS NULL AND i.expires_at>=?) pending_invite FROM member m "
                                "JOIN principal p ON p.id = m.principal_id WHERE m.session_id=? AND m.left_at IS NULL "
                                "ORDER BY p.handle", (db.utcnow(), sess["id"])).fetchall()
            timeline = _timeline(c, sess, me, viewer, flt, task, before, order, actor, channel)
            from . import api_channels
            channels = api_channels.list_view(c, sess, viewer.p.id)["channels"]
            for ch in channels:
                ch["topic_line"] = render.inline(ch["topic"], 120) if ch["topic"] else None
            board = _board(c, sess)
            # Wake supervisor state per member (E30, E31). The hold reason is client text: quoted, inline.
            presence = request.app.state.wake_presence
            wake_info = {}
            for m in members:
                if m["wake_secret"] is None:
                    continue
                w = wake.status(c, sess, m, presence)
                sup = w["supervisor"]
                wake_info[m["handle"]] = {
                    "state": sup["state"], "adapter": sup["adapter"], "pending": w["pending"],
                    "running_since": sup["dispatched_at"][11:16] if sup["dispatched_at"] else None,
                    "connected": sup["connected"],
                    "hold": render.inline(sup["hold"]["reason"], 200) if sup["hold"] else None}
            status = _run_status(c, sess)
            if EXPERIMENTAL_UI:
                strip_rows = c.execute("SELECT * FROM event WHERE session_id=? ORDER BY seq", (sess["id"],)).fetchall()
                experiment_strip = observe.strip(inbox.views(c, strip_rows), sess["head_seq"])
            else:
                experiment_strip = None
            from . import api_comm
            candidates = [r[0] for r in c.execute(
                "SELECT handle FROM principal WHERE active=1 AND kind IN ('human','agent','client') AND id NOT IN "
                "(SELECT principal_id FROM member WHERE session_id=? AND left_at IS NULL) ORDER BY handle", (sess["id"],))]
            standup = [{**x, "min": x["age_s"] // 60,
                        "lines": [(k, render.inline(x[k], 200)) for k in ("done", "doing", "blocked") if x.get(k)]}
                       for x in api_comm.standups(c, sess)]
            own_agents = [r[0].split("/", 1)[1] for r in c.execute(
                "SELECT handle FROM principal WHERE owner_id=? AND kind='agent' AND active=1 ORDER BY handle", (viewer.p.id,))]
            role_names = [r[0] for r in c.execute("SELECT name FROM role_template ORDER BY name")]
        query = {k: v for k, v in (("f", flt), ("t", task), ("before", before), ("order", order), ("actor", actor),
                                   ("channel", channel), ("density", density)) if v is not None}
        base = {k: v for k, v in query.items() if k not in ("density", "before")}
        return {"sess": sess, "me": me, "flt": flt, "task": task, "order": order, "actor": actor, "density": density,
                "channel": channel, "channels": channels,
                "all_channels_query": urllib.parse.urlencode({k: v for k, v in query.items() if k not in ("channel", "before")}),
                "can_manage_channels": me["role"] == "coordinator" or (viewer.p.kind == "human" and me["role"] == "member"),
                "compact_query": urllib.parse.urlencode({**base, "density": "compact"}),
                "cards_query": urllib.parse.urlencode(base),
                "all_actors_query": urllib.parse.urlencode({k: v for k, v in query.items() if k not in ("actor", "before")}),
                "experiment_strip": experiment_strip,
                "oldest_query": urllib.parse.urlencode({**query, "order": "oldest"}),
                "newest_query": urllib.parse.urlencode({**query, "order": "newest"}),
                "filters": FILTERS, "timeline": timeline, "board": board,
                "run_status": status,
                "standup": standup, "candidates": candidates, "own_agents": own_agents,
                "role_names": role_names, "join_clients": JOIN_CLIENTS,
                "can_coordinate": viewer.p.kind == "human" and me["role"] == "coordinator",
                "unread": len(unread_rows), "title_line": render.inline_line("title", sess["title"]),
                "members": sorted(({"handle": m["handle"], "kind": m["kind"], "role": m["role"],
                                    "job_role": m["job_role"], "job_role_version": m["job_role_version"],
                                    "last_seen_at": m["last_seen_at"],
                                    "pending_invite": bool(m["pending_invite"]),
                                    "wake": wake_info.get(m["handle"]),
                                    "title": team_title(m["kind"], m["role"]),
                                    "display_name": render.r2(m["display_name"])} for m in members),
                                  key=lambda m: (TEAM_ORDER.index(m["title"]), m["handle"])),
                "query": urllib.parse.urlencode(query)}

    def _conversation_options(request: Request) -> dict:
        task_raw = request.query_params.get("task", "")
        before_raw = request.query_params.get("before", "")
        event_raw = request.query_params.get("event", "")
        thread_raw = request.query_params.get("thread", "")
        reply_raw = request.query_params.get("reply", "")
        requested_mode = request.query_params.get("mode", "threads")
        mode = {"threaded": "threads", "linear": "all"}.get(requested_mode, requested_mode)
        return {
            "mode": mode if mode in ("threads", "thread", "all") else "threads",
            "author": request.query_params.get("author", "")[:80],
            "kind": request.query_params.get("kind", "")[:80],
            "q": request.query_params.get("q", "")[:200],
            "task": int(task_raw) if task_raw.isdigit() else None,
            "before": int(before_raw) if before_raw.isdigit() else None,
            "event": int(event_raw) if event_raw.isdigit() else None,
            "thread": int(thread_raw) if thread_raw.isdigit() else None,
            "reply": int(reply_raw) if reply_raw.isdigit() else None,
            "include_system": request.query_params.get("system") == "1",
        }

    def _conversation_ctx(c, sess, options: dict) -> dict:
        result = conversations.page(c, sess, author=options["author"], kind=options["kind"],
                                    query=options["q"], task=options["task"],
                                    include_system=options["include_system"], before=options["before"],
                                    thread=options["thread"], reply=options["reply"])
        query = {"mode": options["mode"]}
        for key in ("author", "kind", "q", "task", "event", "thread", "reply"):
            if options[key] not in (None, ""):
                query[key] = options[key]
        if options["include_system"]:
            query["system"] = "1"
        result["older_query"] = urllib.parse.urlencode({**query, "before": result["older_before"]}) \
            if result["older_before"] else None
        return {**options, **result}

    def _global_conversation_ctx(c, options: dict) -> dict:
        result = conversations.global_page(c, author=options["author"], kind=options["kind"],
                                           query=options["q"], task=options["task"],
                                           include_system=options["include_system"], before=options["before"],
                                           thread=options["thread"], reply=options["reply"])
        query = {"mode": options["mode"]}
        for key in ("author", "kind", "q", "task", "event", "thread", "reply"):
            if options[key] not in (None, ""):
                query[key] = options[key]
        if options["include_system"]:
            query["system"] = "1"
        result["older_query"] = urllib.parse.urlencode({**query, "before": result["older_before"]}) \
            if result["older_before"] else None
        return {**options, **result}

    @app.get("/s/{s}/conversations", include_in_schema=False)
    def conversations_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                           database: db.Database = Depends(get_db)):
        options = _conversation_options(request)
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            convo = _conversation_ctx(c, sess, options)
            members = [r[0] for r in c.execute(
                "SELECT p.handle FROM member m JOIN principal p ON p.id=m.principal_id "
                "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],))]
        return page("conversations.html", viewer=viewer, sess=sess, convo=convo, members=members,
                    can_reply=viewer.p.scope == "full" and me["role"] != "observer",
                    admin_mode=False, sessions_list=[], op=ids.new_ulid(), error=request.query_params.get("err"))

    def _chat_ctx(request: Request, s: str, viewer: Viewer, database: db.Database) -> dict:
        """B58: the Chat tab model (messages, markers, channels, members, composer defaults)."""
        from . import api_channels, chatview
        channel = request.query_params.get("channel") or None
        if channel is not None and not api_channels.NAME_RE.match(channel):
            channel = None
        show_events = request.query_params.get("events") != "0"
        def num(name):
            raw = request.query_params.get(name, "")
            return int(raw) if raw.isdigit() else None
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            channels = api_channels.list_view(c, sess, viewer.p.id)["channels"]
            if channel is not None and channel not in {ch["name"] for ch in channels}:
                channel = None
            chat = chatview.stream(c, sess, viewer, me, channel, show_events)
            # B67: unread per channel from the viewer's own read position (their inbox cursor); own posts never count.
            mt = chatview.MESSAGE_TYPES
            counts = {r[0]: r[1] for r in c.execute(
                f"SELECT channel, COUNT(*) FROM event WHERE session_id=? AND seq>? AND actor_id!=? "
                f"AND type IN ({','.join('?' * len(mt))}) GROUP BY channel",
                (sess["id"], me["cursor_seq"], viewer.p.id, *mt))}
            for ch in channels:
                ch["unread"] = counts.get(ch["name"], 0)
                ch["topic_line"] = render.inline(ch["topic"], 120) if ch["topic"] else None
            session_unread = sum(counts.values())
            team = [dict(r) for r in c.execute(
                "SELECT p.handle, p.kind, m.role, m.last_seen_at FROM member m JOIN principal p ON p.id=m.principal_id "
                "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.kind DESC, p.handle", (sess["id"],))]
            members = sorted(t["handle"] for t in team)
            ask = num("ask")
            target = chatview.reply_target(c, sess, ask if ask is not None else num("reply"))
        query = {k: v for k, v in (("channel", channel), ("events", None if show_events else "0")) if v}
        to_param = request.query_params.get("to", "").lstrip("@")
        draft = {"kind": "note", "to": to_param if to_param in members and to_param != viewer.p.handle else "", "re": None}
        if target:
            to = target["actor"] if target["actor"] != viewer.p.handle else ""
            kind = "question" if ask is not None else ("answer" if target["type"] == "question" else "note")
            draft = {"kind": kind, "to": to, "re": target}
        human_coordinator = viewer.p.kind == "human" and me["role"] == "coordinator"
        kinds = ["note", "question", "answer", "update", "proposal", "blocker"] + (["decision"] if human_coordinator else [])
        return {"viewer": viewer, "sess": sess, "me": me, "chat": chat, "channel": channel, "show_events": show_events,
                "channels": channels, "open_channels": [ch for ch in channels if not ch["archived"]],
                "archived_channels": [ch for ch in channels if ch["archived"]], "session_unread": session_unread,
                "members": members, "team": team, "draft": draft, "kinds": kinds,
                "can_manage_channels": me["role"] == "coordinator" or (viewer.p.kind == "human" and me["role"] == "member"),
                "query": urllib.parse.urlencode(query),
                "events_query": urllib.parse.urlencode({**query, "events": "0"} if show_events
                                                       else {k: v for k, v in query.items() if k != "events"}),
                "can_post": viewer.p.scope == "full" and me["role"] != "observer",
                "can_coordinate": human_coordinator, "op": ids.new_ulid()}

    @app.get("/s/{s}/chat", include_in_schema=False)
    def chat_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                  database: db.Database = Depends(get_db)):
        """B58: watch the conversation and step in: messages oldest first, a composer at the bottom."""
        return page("chat.html", **_chat_ctx(request, s, viewer, database))

    @app.get("/s/{s}/chat/stream", include_in_schema=False)
    def chat_stream(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                    database: db.Database = Depends(get_db)):
        """B58: the message stream alone, for the 5-second refresh (static/poll.js)."""
        response = page("chat-stream.html", **_chat_ctx(request, s, viewer, database))
        response.headers["X-Glassboard-Fragment"] = "chat"
        return response

    @app.get("/admin/conversations", include_in_schema=False)
    def admin_conversations(request: Request, viewer: Viewer = Depends(current_viewer),
                            database: db.Database = Depends(get_db)):
        _require_admin(viewer)
        selected = request.query_params.get("session", "")
        options = _conversation_options(request)
        with database.read() as c:
            session_rows = [dict(r) for r in c.execute(
                "SELECT handle,title,state,head_seq FROM session ORDER BY updated_at DESC")]
            sess = sessions.find_session(c, selected) if selected else None
            if selected and sess is None:
                raise ApiError(404, "not_found", "Workspace not found.")
            if sess is None:
                convo = _global_conversation_ctx(c, options)
                return page("admin_conversations.html", viewer=viewer, sess=None, convo=convo,
                            sessions_list=session_rows, members=[], can_reply=False, op=ids.new_ulid(),
                            error=request.query_params.get("err"))
            convo = _conversation_ctx(c, sess, options)
            members = [r[0] for r in c.execute(
                "SELECT p.handle FROM member m JOIN principal p ON p.id=m.principal_id "
                "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],))]
            me = c.execute("SELECT role FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                           (sess["id"], viewer.p.id)).fetchone()
        return page("admin_conversations.html", viewer=viewer, sess=sess, convo=convo,
                    sessions_list=session_rows, members=members,
                    can_reply=bool(me and me["role"] != "observer"), op=ids.new_ulid(),
                    error=request.query_params.get("err"))

    @app.get("/admin/runs", include_in_schema=False)
    def admin_runs(request: Request, viewer: Viewer = Depends(current_viewer)):
        _require_admin(viewer)
        rounds, unavailable = run_browser.list_rounds(request.app.state.admin_run_evidence_roots)
        agent = request.query_params.get("agent", "")
        status = request.query_params.get("status", "")
        shown = [r for r in rounds if (not agent or r["agent"] == agent) and (not status or r["status"] == status)]
        return page("admin_runs.html", viewer=viewer, rounds=shown, total=len(rounds), unavailable=unavailable,
                    roots=request.app.state.admin_run_evidence_roots,
                    agents=sorted({r["agent"] for r in rounds}), statuses=sorted({r["status"] for r in rounds}),
                    selected_agent=agent, selected_status=status)

    @app.get("/admin/runs/{root_index}/{round_name}", include_in_schema=False)
    def admin_run_detail(root_index: int, round_name: str, request: Request,
                         viewer: Viewer = Depends(current_viewer)):
        _require_admin(viewer)
        before_raw = request.query_params.get("before", "")
        before = int(before_raw) if before_raw.isdigit() else None
        try:
            run = run_browser.detail(request.app.state.admin_run_evidence_roots, root_index, round_name,
                                     before=before)
        except LookupError:
            raise ApiError(404, "not_found", "Agent run not found.") from None
        return page("admin_run.html", viewer=viewer, run=run)

    @app.get("/admin/runs/{root_index}/{round_name}/files/{filename}", include_in_schema=False)
    def admin_run_file(root_index: int, round_name: str, filename: str, request: Request,
                       viewer: Viewer = Depends(current_viewer)):
        _require_admin(viewer)
        before_raw = request.query_params.get("before", "")
        before = int(before_raw) if before_raw.isdigit() else None
        try:
            capture = run_browser.file_detail(request.app.state.admin_run_evidence_roots, root_index,
                                              round_name, filename, before=before)
        except LookupError:
            raise ApiError(404, "not_found", "Captured file not found.") from None
        return page("admin_run_file.html", viewer=viewer, capture=capture)

    @app.get("/s/{s}", include_in_schema=False)
    def session_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        ctx = _session_ctx(request, s, viewer, database)
        return page("session.html", viewer=viewer, op=ids.new_ulid(), error=request.query_params.get("err"), **ctx)

    @app.get("/s/{s}/trace", include_in_schema=False)
    def forensic_trace(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                       database: db.Database = Depends(get_db)):
        before_raw = request.query_params.get("before", "")
        if before_raw and (not before_raw.isdigit() or int(before_raw) < 1):
            raise ApiError(400, "invalid_cursor", "Event cursor must be a positive sequence number.")
        before = int(before_raw) if before_raw else None
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            rows = _timeline(c, sess, me, viewer, "trace", None, before, "newest")
            count = c.execute("SELECT count(*) FROM event WHERE session_id=? AND type='message.preflight'",
                              (sess["id"],)).fetchone()[0]
            older = bool(rows and c.execute("SELECT 1 FROM event WHERE session_id=? AND type='message.preflight' "
                                            "AND seq<?", (sess["id"], rows[-1]["seq"])).fetchone())
        return page("trace.html", viewer=viewer, sess=sess, trace=rows, count=count,
                    older_before=rows[-1]["seq"] if older else None)

    @app.get("/s/{s}/record.jsonl", include_in_schema=False)
    def browser_record_export(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                              database: db.Database = Depends(get_db)):
        """Complete board event stream, including unsent preflight drafts, under cookie auth."""
        subset = request.query_params.get("only", "all")
        if subset not in ("all", "trace"):
            raise ApiError(400, "invalid_request", "Choose the complete record or forensic trace.")
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            condition = " AND type='message.preflight'" if subset == "trace" else ""
            rows = inbox.views(c, c.execute("SELECT * FROM event WHERE session_id=?" + condition + " ORDER BY seq",
                                           (sess["id"],)))
        header = {"session": sess["handle"], "title": sess["title"], "state": sess["state"],
                  "epoch": sess["epoch"], "head": sess["head_seq"], "subset": subset,
                  "exported_at": db.utcnow()}
        content = "\n".join(json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                            for row in [header, *rows]) + "\n"
        return _security_headers(Response(content, media_type="application/x-ndjson",
                                          headers={"Content-Disposition":
                                                   f'attachment; filename="glassboard-{sess["handle"]}-{subset}.jsonl"'}))

    @app.get("/s/{s}/observe", include_in_schema=False)
    def observe_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                     database: db.Database = Depends(get_db)):
        if not EXPERIMENTAL_UI:
            raise ApiError(404, "not_found", "Not found.")
        at_raw = request.query_params.get("at")
        if at_raw is not None and (not at_raw.isdigit() or int(at_raw) < 1):
            raise ApiError(400, "invalid_cursor", "Event cursor must be a positive sequence number.")
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            at = int(at_raw) if at_raw is not None else sess["head_seq"]
            if at > sess["head_seq"]:
                raise ApiError(404, "not_found", "Event not found.")
            state = observe.model(c, sess, at)
        return page("observe.html", viewer=viewer, sess=sess, me=me, at=at,
                    live=at_raw is None, state=state)

    @app.get("/s/{s}/observe/content", include_in_schema=False)
    def observe_content(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                        database: db.Database = Depends(get_db)):
        if not EXPERIMENTAL_UI:
            raise ApiError(404, "not_found", "Not found.")
        at_raw = request.query_params.get("at")
        if at_raw is not None and (not at_raw.isdigit() or int(at_raw) < 1):
            raise ApiError(400, "invalid_cursor", "Event cursor must be a positive sequence number.")
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            at = int(at_raw) if at_raw is not None else sess["head_seq"]
            if at > sess["head_seq"]:
                raise ApiError(404, "not_found", "Event not found.")
            state = observe.model(c, sess, at)
        response = page("observe-content.html", viewer=viewer, sess=sess, me=me,
                        at=at, live=at_raw is None, state=state)
        response.headers["X-Glassboard-Fragment"] = "observe"
        return response

    @app.get("/s/{s}/observe/strip", include_in_schema=False)
    def observe_strip(s: str, viewer: Viewer = Depends(current_viewer),
                      database: db.Database = Depends(get_db)):
        if not EXPERIMENTAL_UI:
            raise ApiError(404, "not_found", "Not found.")
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            rows = c.execute("SELECT * FROM event WHERE session_id=? ORDER BY seq", (sess["id"],)).fetchall()
            strip = observe.strip(inbox.views(c, rows), sess["head_seq"])
        response = page("observe-strip.html", viewer=viewer, sess=sess, me=me,
                        strip=strip, at=sess["head_seq"], condensed=True)
        response.headers["X-Glassboard-Fragment"] = "poll"
        return response

    @app.get("/s/{s}/team", include_in_schema=False)
    def team_manage_page(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                         database: db.Database = Depends(get_db)):
        ctx = _session_ctx(request, s, viewer, database)
        if not ctx["can_coordinate"]:
            raise ApiError(404, "not_found", "Not found.")
        return page("team_manage.html", viewer=viewer, op=ids.new_ulid(), error=request.query_params.get("err"), **ctx)

    @app.post("/s/{s}/job-role", include_in_schema=False)
    async def job_role_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                            database: db.Database = Depends(get_db)):
        from .api_roles import JobRoleSet, set_job_role
        form = await form_data(request)
        check_post(request, form, viewer)
        try:
            body = JobRoleSet(handle=form.get("handle", ""), role=form.get("role") or None)
            set_job_role(s, body, form.get("op") or ids.new_ulid(), form.get("recovery"), None,
                         viewer.p, database)
        except ValidationError:
            return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err=invalid_request#team", status_code=303))
        except ApiError as e:
            if e.code in FORM_ERRORS + ("recovery_mismatch",):
                return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err={e.code}#team", status_code=303))
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}#team", status_code=303))

    @app.get("/s/{s}/timeline", include_in_schema=False)
    def timeline_fragment(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                          database: db.Database = Depends(get_db)):
        ctx = _session_ctx(request, s, viewer, database)
        response = page("timeline.html", **ctx)
        response.headers["X-Glassboard-Fragment"] = "timeline"
        return response

    @app.get("/s/{s}/status", include_in_schema=False)
    def status_fragment(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            status = _run_status(c, sess)
        response = page("run-status.html", sess=sess, run_status=status)
        response.headers["X-Glassboard-Fragment"] = "poll"
        return response

    @app.get("/s/{s}/agent", include_in_schema=False)
    def agent_view(s: str, request: Request, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """What `sync` would show that member right now, computed read-only (agent view)."""
        who = request.query_params.get("as", "")
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            target = c.execute("SELECT p.id, p.handle, m.* FROM principal p JOIN member m ON m.principal_id = p.id "
                               "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (who, sess["id"])).fetchone()
            if target is None:
                raise ApiError(404, "not_found", "No such member.")
            page_rows, more = inbox.inbox_page(c, sess, target, target["cursor_seq"], 50)
            owed, owed_total = inbox.owed(c, sess["id"], target["principal_id"])
            result = {"me": target["handle"], "session": sess["handle"], "state": sess["state"],
                      "recovery_id": sess["recovery_id"], "epoch": sess["epoch"],
                      "consumer": f"C{target['consumer_gen']}" if target["consumer_secret"] else "none",
                      "head": sess["head_seq"], "cursor": target["cursor_seq"],
                      "delivered_through": page_rows[-1]["seq"] if more else sess["head_seq"],
                      "claims": [], "owed": owed, "owed_total": owed_total, "inbox": inbox.views(c, page_rows), "more": more}
            members = [r[0] for r in c.execute("SELECT p.handle FROM member m JOIN principal p ON p.id = m.principal_id "
                                                "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY p.handle", (sess["id"],))]
            # B36 (parity, report X5): the member's latest checkpoint, under the same rule as the API
            # (your own, or anyone's for a coordinator).
            from . import api_checkpoints
            me = c.execute("SELECT role FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                           (sess["id"], viewer.p.id)).fetchone()
            may_read = target["principal_id"] == viewer.p.id or (me is not None and me["role"] == "coordinator")
            checkpoint, checkpoint_count = None, 0
            if may_read:
                row = api_checkpoints.latest(c, sess["id"], target["principal_id"])
                checkpoint_count = c.execute("SELECT count(*) FROM checkpoint WHERE session_id=? AND principal_id=?",
                                             (sess["id"], target["principal_id"])).fetchone()[0]
                if row is not None:
                    from .textviews import checkpoint_lines
                    checkpoint = lines_view(checkpoint_lines(api_checkpoints.checkpoint_view(row, target["handle"])))
            brief_lines = None
            if may_read:                                      # B40: same rule as GET /brief
                from . import api_brief
                b = api_brief.brief(c, sess, target, target["handle"], None, target["principal_id"] == viewer.p.id)
                brief_lines = lines_view(api_brief.brief_text(b).rstrip("\n").split("\n"))
            # B41: a human coordinator or the agent's owner may ask it to stand down, and lift that.
            standdown, may_direct = None, False
            agent = c.execute("SELECT kind, owner_id FROM principal WHERE id=?", (target["principal_id"],)).fetchone()
            if agent["kind"] == "agent":
                from . import api_holds
                may_direct = viewer.p.kind == "human" and ((me is not None and me["role"] == "coordinator")
                                                           or agent["owner_id"] == viewer.p.id)
                standdown = api_holds.standdown_view(c, sess["id"], target["principal_id"])
                if standdown is not None:
                    standdown["reason_line"] = render.inline(standdown["reason"] or "", 200)
            # B56: quiet hours, set by the human themselves or, for an agent, its owner or a human coordinator
            from . import quiet as quiet_mod
            quiet = quiet_mod.of_member(target)
            may_quiet = viewer.p.kind == "human" and (target["principal_id"] == viewer.p.id or may_direct)
        text_lines = sync_text(result, target["consumer_runtime"]).split("\n")
        return page("agent.html", quiet=quiet, may_quiet=may_quiet, quiet_start=target["quiet_start"],
                    quiet_end=target["quiet_end"], quiet_tz=target["quiet_tz"], viewer=viewer, sess=sess, who=target["handle"], members=members,
                    lines=lines_view(text_lines), checkpoint=checkpoint, checkpoint_count=checkpoint_count,
                    may_read_checkpoint=may_read, brief=brief_lines, standdown=standdown, may_direct=may_direct,
                    is_agent=agent["kind"] == "agent", op=ids.new_ulid())

    # --- tasks ---------------------------------------------------------------------------

    def _form_error(s: str, code: str, where: str = "") -> RedirectResponse:
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}{where}?err={code}", status_code=303))

    @app.post("/new-session", include_in_schema=False)
    async def new_session(request: Request, viewer: Viewer = Depends(current_viewer),
                          database: db.Database = Depends(get_db)):
        """Any human creates a session from the home page and becomes its coordinator (same code as the API)."""
        from . import api_sessions
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        try:
            stale_min = int(form.get("stale_after_min", "30"))
            body = api_sessions.SessionCreate(handle=form.get("handle", "").strip(), title=form.get("title", "").strip(),
                                              autonomous=form.get("autonomous") == "1", stale_after_s=stale_min * 60)
            api_sessions.create_session(body, request, key, viewer.p, database)
        except (ValidationError, ValueError):
            return _security_headers(RedirectResponse("/?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in ("handle_taken", "forbidden"):
                return _security_headers(RedirectResponse(f"/?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(body.handle)}", status_code=303))

    @app.post("/s/{s}/members", include_in_schema=False)
    async def members_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                           database: db.Database = Depends(get_db)):
        """Coordinators add a member, change a role, or remove one (same code paths as the API)."""
        from . import api_sessions
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        handle = form.get("handle", "").strip()
        try:
            if form.get("action") == "remove":
                api_sessions.remove_member(s, handle, request, key, form.get("recovery"), None, viewer.p, database)
            else:
                body = api_sessions.MemberSet(handle=handle, role=form.get("role") or "member")
                api_sessions.set_member(s, body, request, key, form.get("recovery"), None, viewer.p, database)
        except ValidationError:
            return _form_error(s, "invalid_request")
        except ApiError as e:
            if e.code in FORM_ERRORS:
                return _form_error(s, e.code)
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}", status_code=303))

    @app.post("/s/{s}/invite", include_in_schema=False)
    async def invite_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                          database: db.Database = Depends(get_db)):
        """A coordinator adds one of their agents: the board makes a one-time join code and this page shows,
        once, exactly what to give the agent (or type in a terminal). Same code path as the API."""
        from . import api_join
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        client = form.get("client") if form.get("client") in JOIN_CLIENTS else "other"
        name = form.get("agent", "").strip() or JOIN_CLIENTS[client]["default"]
        try:
            body = api_join.InviteIn(agent=name, role=form.get("role") or "member")
            resp = api_join.create_invite(s, body, request, key, form.get("recovery"), None, viewer.p, database)
        except ValidationError:
            return _form_error(s, "invalid_request")
        except ApiError as e:
            if e.code in FORM_ERRORS:
                return _form_error(s, e.code)
            raise
        j = json.loads(resp.body)
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
        out = page("invite.html", viewer=viewer, sess=sess, inv=j, client=client,
                   steps=_join_steps(request, j["code"], client) if j["code"] else None)
        out.headers["Cache-Control"] = "no-store"          # the code is shown once: never from a cache
        return out

    def _join_steps(request: Request, code: str, client: str) -> dict:
        """What the human gives the agent. The URL is the one this browser reached the board at."""
        url = str(request.base_url).rstrip("/")
        u = urllib.parse.urlsplit(url)
        flags = ""
        if u.scheme == "https":
            try:
                with open(request.app.state.ca_file, encoding="ascii") as f:
                    flags = f" --fetch-ca --ca-fingerprint {tlsboot.fingerprint(tlsboot.check_ca_pem(f.read(tlsboot.MAX_CA_BYTES + 1)))}"
            except (OSError, UnicodeDecodeError, tlsboot.TlsBootError):
                flags = ""                                  # no published CA: the agent's system trust store decides
        elif u.hostname not in ("127.0.0.1", "localhost", "::1"):
            flags = " --insecure-http"
        join = f"glassboard join {url} {code}{flags}"
        launch = JOIN_CLIENTS[client]["launch"]
        prompt = (f"Join my Glassboard session. Run this command once: {join}\n"
                  "Then run `glassboard protocol` and follow those rules, and run `glassboard sync` to read the goal, "
                  "your messages and your tasks. Before you edit a shared file, run `glassboard lease take <path> --wait 600`, "
                  "and `glassboard lease release <path>` when you are done.")
        return {"join": join, "terminal": f"{join} -- {launch}", "prompt": prompt, "launch": launch}

    # --- my agents: humans manage their agents and tokens without the CLI ---------------------------

    @app.get("/agents", include_in_schema=False)
    def my_agents(request: Request, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        if viewer.p.kind != "human":
            raise ApiError(403, "forbidden", "Only humans have agents.")
        tab = request.query_params.get("tab", "agents")
        if tab not in ("agents", "invitations", "tokens"):
            tab = "agents"
        agents = []
        with database.read() as c:
            coord_sessions = [dict(r) for r in c.execute(
                "SELECT s.handle, s.recovery_id, s.state FROM session s JOIN member m ON m.session_id=s.id "
                "WHERE m.principal_id=? AND m.left_at IS NULL AND m.role='coordinator' "
                "AND s.state!='archived' ORDER BY s.handle", (viewer.p.id,))]
            own_tokens = [dict(r) for r in c.execute(
                "SELECT id, label, scope, created_at, last_used_at, expires_at, revoked_at FROM credential "
                "WHERE principal_id=? ORDER BY created_at DESC", (viewer.p.id,))]
            for cr in own_tokens:
                cr["label"] = render.r2(cr["label"] or "")
                cr["state"] = token_state(cr)
            for a in c.execute("SELECT id, handle, kind, active, created_at FROM principal WHERE owner_id=? ORDER BY handle",
                               (viewer.p.id,)):
                creds = [dict(r) for r in c.execute(
                    "SELECT id, label, scope, created_at, last_used_at, expires_at, revoked_at FROM credential "
                    "WHERE principal_id=? ORDER BY created_at DESC", (a["id"],))]
                for cr in creds:
                    cr["label"] = render.r2(cr["label"] or "")
                    cr["state"] = token_state(cr)
                sessions_of = [dict(r) for r in c.execute(
                    "SELECT s.handle, m.role, m.last_seen_at FROM member m JOIN session s ON s.id=m.session_id "
                    "WHERE m.principal_id=? AND m.left_at IS NULL ORDER BY s.handle", (a["id"],))]
                for so in sessions_of:
                    so["title"] = team_title(a["kind"], so["role"])
                agents.append({"handle": a["handle"], "kind": a["kind"], "active": bool(a["active"]),
                               "created_at": a["created_at"], "tokens": creds, "sessions": sessions_of,
                               "live_tokens": sum(1 for cr in creds if cr["state"] == "active")})
        return page("agents.html", viewer=viewer, agents=agents, own_tokens=own_tokens, tab=tab,
                    coord_sessions=coord_sessions, join_clients=JOIN_CLIENTS,
                    op=ids.new_ulid(), error=request.query_params.get("err"))

    @app.post("/agents/token", include_in_schema=False)
    async def mint_browser_token(request: Request, viewer: Viewer = Depends(current_viewer),
                                 database: db.Database = Depends(get_db)):
        """Show a newly minted token once; never place it in a URL, log or cached response."""
        from . import api_me
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if idempotency.KEY_RE.fullmatch(form.get("op", "")) else ids.new_ulid()
        try:
            days = int(form["expires_days"]) if form.get("expires_days") else None
            body = api_me.TokenCreate(principal=form.get("principal") or None,
                                      scope=form.get("scope", "full"), expires_days=days,
                                      label=form.get("label") or None)
            response = api_me.create_token(body, key, viewer.p, database)
        except (ValidationError, ValueError):
            return _security_headers(RedirectResponse("/agents?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in FORM_ERRORS:
                return _security_headers(RedirectResponse(f"/agents?err={e.code}", status_code=303))
            raise
        result = json.loads(response.body)
        shown = page("token-once.html", viewer=viewer, token=result["secret"],
                     principal=result["principal"], scope=result["scope"], expires=result["expires_at"])
        shown.headers["Cache-Control"] = "no-store"
        return shown

    @app.post("/agents", include_in_schema=False)
    async def my_agents_form(request: Request, viewer: Viewer = Depends(current_viewer),
                             database: db.Database = Depends(get_db)):
        """Create an agent, or revoke one of your agents' tokens (same code paths as /api/v1/me)."""
        from . import api_me
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        try:
            if form.get("action") == "revoke":
                request.state.flash_ok = "token_revoked"
                cid = form.get("credential", "")
                if not tokens.CRED_ID_RE.match(cid):
                    raise ApiError(404, "not_found", "No such credential of yours.")
                api_me.revoke_token(cid, request, key, viewer.p, database)
            else:
                api_me.create_agent(api_me.AgentCreate(name=form.get("name", "").strip()), key, viewer.p, database)
        except ValidationError:
            return _security_headers(RedirectResponse("/agents?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in FORM_ERRORS + ("handle_taken",):
                return _security_headers(RedirectResponse(f"/agents?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse("/agents", status_code=303))

    FORM_ERRORS = ("forbidden", "invalid_transition", "invalid_request", "not_found", "version_conflict",
                   "idempotency_key_reused", "session_paused", "session_archived", "secret_in_content",
                   "field_not_patchable", "task_not_claimable", "payload_too_large", "dependency_cycle", "task_waiting",
                   "submission_mismatch", "checklist_incomplete")

    @app.get("/s/{s}/t/{n}", include_in_schema=False)
    def task_page(s: str, n: int, request: Request, viewer: Viewer = Depends(current_viewer),
                  database: db.Database = Depends(get_db)):
        from . import api_artifacts
        from .api_tasks import task_view, task_trail, verification_view
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            t = c.execute("SELECT * FROM task WHERE session_id=? AND num=?", (sess["id"], n)).fetchone()
            if t is None:
                raise ApiError(404, "not_found", "No such task.")
            view = task_view(c, t, sess)
            view["verification"] = verification_view(c, sess["id"], t, view)
            history_views = inbox.views(c, c.execute("SELECT * FROM event WHERE session_id=? AND task_num=? ORDER BY seq",
                                                      (sess["id"], n)))
            history = [{"seq": v["seq"], "type": v["type"], "lines": lines_view(view_lines(v))} for v in history_views]
            trail = [{**item, "detail": render.r2(item["detail"], allow_newline=True),
                      "refs": [{**ref, "label": render.r2(ref["label"]), "loc": render.r2(ref["loc"])}
                               for ref in item["refs"]]} for item in task_trail(c, sess["id"], n)]
            members = [r[0] for r in c.execute(
                "SELECT p.handle FROM member m JOIN principal p ON p.id = m.principal_id WHERE m.session_id=? "
                "AND m.left_at IS NULL AND m.role != 'observer' ORDER BY p.handle", (sess["id"],))]
            artifacts = [api_artifacts.artifact_view(c, row) for row in c.execute(
                "SELECT * FROM artifact WHERE session_id=? AND task_num=? ORDER BY created_at DESC, id DESC",
                (sess["id"], n)).fetchall()]
        from .textviews import checklist_lines, task_head
        head = [task_head(view), render.inline_line("title", view["title"]),
                render.inline_line("deliverable", view["deliverable"]), *checklist_lines(view),
                *render.block_lines(view["scope"])]
        if view["summary"]:
            head.append(render.inline_line("summary", view["summary"]))
        handoff = next((h for h in reversed(history_views) if (h.get("data") or {}).get("handoff_fields")), None)
        if handoff is not None:                         # the next person reads this first
            head.append(render.section(f"--- latest handoff #{handoff['seq']} by {handoff['actor']} ---"))
            head += [render.inline_line(k, handoff["data"]["handoff_fields"][k])
                     for k in ("done", "next", "gotchas") if handoff["data"]["handoff_fields"].get(k)]
        # Edit fields are pre-filled only with text that control/bidi neutralisation leaves unchanged.
        # Text with hidden or control characters is never put raw into the page; that field stays empty
        # with a note, and an empty field is not sent, so an edit never rewrites it by accident.
        edit = {}
        for f in ("title", "deliverable", "scope"):
            clean = render.r2(view[f], allow_newline=(f == "scope")) == view[f]
            edit[f] = {"value": view[f] if clean else "", "hidden": not clean}
        items = [i["text"] for i in view["checklist"]]
        clean_items = all(render.r2(x) == x for x in items)
        edit["checklist"] = {"value": "\n".join(items) if clean_items else "", "hidden": not clean_items}
        edit["depends_on"] = ", ".join(f"T{d['task']}" for d in view["depends_on"])
        edit["parent"] = f"T{view['parent']}" if view["parent"] else ""
        return page("task.html", viewer=viewer, sess=sess, me=me, t=view, title_line=render.inline(view["title"], render.INLINE_LIMITS["title"]),
                    edit=edit, head=lines_view(head), history=history,
                    trail=trail, artifacts=artifacts,
                    members=members, op=ids.new_ulid(), error=request.query_params.get("err"),
                    can_coordinate=viewer.p.kind == "human" and me["role"] == "coordinator",
                    can_review=viewer.p.scope == "full" and view["status"] == "review"
                               and view["reviewer"] == viewer.p.handle,
                    can_ack=view["assignment_stage"] == "assigned" and view["assignee"] == viewer.p.handle)

    @app.post("/s/{s}/t/{n}/{op}", include_in_schema=False)
    async def task_op(s: str, n: int, op: str, request: Request, viewer: Viewer = Depends(current_viewer),
                      database: db.Database = Depends(get_db)):
        from . import api_tasks
        form = await form_data(request)
        check_post(request, form, viewer)
        if op not in ("acknowledge", "review", "accept", "return", "reassign", "cancel", "reopen", "release", "edit"):
            raise ApiError(404, "not_found", "No such operation.")
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        reason = form.get("reason") or None
        try:
            # Authorization precedes operation-specific payload validation. Otherwise an
            # unauthorized member can receive a misleading validation error (and learn which fields
            # a hidden coordinator form expects) instead of the canonical forbidden result.
            if op in ("accept", "return", "reassign", "cancel", "reopen", "edit"):
                with database.read() as c:
                    _, me = sessions.require_member(c, s, viewer.p)
                    sessions.require_human_coordinator(viewer.p, me)
            if op == "acknowledge":
                api_tasks.acknowledge(database, viewer.p, s, str(n), api_tasks.AcknowledgeIn(), key,
                                      form.get("recovery"), None)
            elif op == "review":
                submission_seq = form.get("submission_seq", "")
                body = api_tasks.ReviewIn(verdict=form.get("verdict", ""),
                                          submission_seq=int(submission_seq) if submission_seq.isdigit() else 0,
                                          commit=form.get("commit") or None, notes=form.get("notes") or None)
                api_tasks.review_task(s, str(n), body, key, form.get("recovery"), None, viewer.p, database)
            elif op == "edit":
                fields: dict = {k: form[k] for k in ("title", "scope", "deliverable") if form.get(k)}
                if form.get("stale_after_s", "").isdigit():
                    fields["stale_after_s"] = int(form["stale_after_s"])
                if form.get("priority"):
                    fields["priority"] = form["priority"]
                if "reviewer" in form:
                    fields["reviewer"] = None if form["reviewer"] in ("", "none") else form["reviewer"]
                if "depends_on" in form:
                    fields["depends_on"] = [x.strip() for x in form["depends_on"].split(",") if x.strip()]
                if "parent" in form:
                    fields["parent"] = form["parent"].strip() or None
                # The checklist is sent only when the page showed it (never cleared by a hidden-text guard).
                if form.get("checklist_shown") == "1":
                    fields["checklist"] = [x.strip() for x in form.get("checklist", "").split("\n") if x.strip()]
                body = api_tasks.TaskPatch(if_version=int(form.get("if_version", "0") or 0), **fields)
                api_tasks.patch(database, viewer.p, s, str(n), body, key, form.get("recovery"), None)
            else:
                version = int(form.get("if_version", "0") or 0)
                submission = int(form.get("submission_seq", "0") or 0)
                body = {"accept": lambda: api_tasks.CoordIn(if_version=version, reason=reason),
                        "cancel": lambda: api_tasks.CoordIn(if_version=version, reason=reason),
                        "reopen": lambda: api_tasks.CoordIn(if_version=version, reason=reason),
                        "return": lambda: api_tasks.ReturnIn(if_version=version, submission_seq=submission,
                                                               feedback=form.get("feedback", "")),
                        "reassign": lambda: api_tasks.ReassignIn(if_version=version, to=form.get("to", ""), reason=reason),
                        "release": lambda: api_tasks.ReleaseIn(handoff=form.get("handoff") or None)}[op]()
                api_tasks.coordinator_op(op, database, viewer.p, s, str(n), body, key, form.get("recovery"))
        except ValidationError:
            return _form_error(s, "invalid_request", f"/t/{n}")
        except ApiError as e:
            if e.code == "recovery_mismatch":
                return page("error.html", status=409, viewer=viewer, code=e.code, sess_handle=s,
                            message="The board was restored since this page was loaded. Reload it and try again.")
            if e.code in FORM_ERRORS:
                return _form_error(s, e.code, f"/t/{n}")
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}/t/{n}", status_code=303))

    @app.get("/s/{s}/new-task", include_in_schema=False)
    def new_task_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                      database: db.Database = Depends(get_db)):
        """`?from=N` prefills the form from proposal #N."""
        prefill, src, drafts = {"title": "", "scope": ""}, None, []
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            frm = request.query_params.get("from", "")
            if frm.isdigit():
                ev = c.execute("SELECT seq, body, data FROM event WHERE session_id=? AND seq=? AND type='proposal'",
                               (sess["id"], int(frm))).fetchone()
                if ev is not None:
                    src = ev["seq"]
                    import json as _json
                    drafts = [{"n": i, "line": render.inline(
                                  f"{d['title']} · deliverable: {d['deliverable']}" + (f" · for @{d['assignee']}" if d.get("assignee") else "")
                                  + (f" · review by @{d['reviewer']}" if d.get("reviewer") else ""), 300)}
                              for i, d in enumerate((_json.loads(ev["data"]) if ev["data"] else {}).get("drafts", []), 1)]
                    body = render.r2(ev["body"] or "", allow_newline=True)     # never raw controls in the page
                    prefill = {"title": body.strip().split("\n", 1)[0][:200], "scope": body}
            members = [r[0] for r in c.execute(
                "SELECT p.handle FROM member m JOIN principal p ON p.id = m.principal_id WHERE m.session_id=? "
                "AND m.left_at IS NULL AND m.role != 'observer' ORDER BY p.handle", (sess["id"],))]
        return page("newtask.html", viewer=viewer, sess=sess, prefill=prefill, src=src, members=members, drafts=drafts,
                    op=ids.new_ulid(), error=request.query_params.get("err"))

    @app.post("/s/{s}/tasks", include_in_schema=False)
    async def create_task_form(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                               database: db.Database = Depends(get_db)):
        from . import api_tasks
        form = await form_data(request)
        check_post(request, form, viewer)
        key = form.get("op") if 8 <= len(form.get("op", "")) <= 64 else ids.new_ulid()
        try:
            body = api_tasks.TaskCreate(
                title=form.get("title", ""), scope=form.get("scope", ""), deliverable=form.get("deliverable", ""),
                assignee=form.get("assignee") or None,
                stale_after_s=int(form["stale_after_s"]) if form.get("stale_after_s", "").isdigit() else None,
                from_proposal=int(form["from_proposal"]) if form.get("from_proposal", "").isdigit() else None,
                priority=form.get("priority") or "normal",
                reviewer=form.get("reviewer") or None,
                depends_on=[x.strip() for x in form.get("depends_on", "").split(",") if x.strip()],
                parent=form.get("parent", "").strip() or None,
                checklist=[x.strip() for x in form.get("checklist", "").split("\n") if x.strip()])
            r = api_tasks.create(database, viewer.p, s, body, key, form.get("recovery"), None)
        except ValidationError:
            return _form_error(s, "invalid_request", "/new-task")
        except ApiError as e:
            if e.code == "recovery_mismatch":
                return page("error.html", status=409, viewer=viewer, code=e.code, sess_handle=s,
                            message="The board was restored since this page was loaded. Reload it and try again.")
            if e.code in FORM_ERRORS:
                return _form_error(s, e.code, "/new-task")
            raise
        import json as _json
        num = _json.loads(r.body)["task"]
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}/t/{num}", status_code=303))

    @app.post("/s/{s}/tasks-from-drafts", include_in_schema=False)
    async def create_from_drafts(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                                 database: db.Database = Depends(get_db)):
        """A human turns every draft of a proposal into a task: one task.created event each, each with
        its own idempotency key derived from the form's op, so a resubmitted form creates nothing twice."""
        import json as _json
        from . import api_tasks
        form = await form_data(request)
        check_post(request, form, viewer)
        op = form.get("op") if 8 <= len(form.get("op", "")) <= 60 else ids.new_ulid()
        src = int(form["from_proposal"]) if form.get("from_proposal", "").isdigit() else 0
        with database.read() as c:
            sess, _ = sessions.require_member(c, s, viewer.p)
            ev = c.execute("SELECT data FROM event WHERE session_id=? AND seq=? AND type='proposal'", (sess["id"], src)).fetchone()
        drafts = (_json.loads(ev["data"]) if ev and ev["data"] else {}).get("drafts", [])
        if not drafts:
            return _form_error(s, "invalid_request", "/new-task")
        try:
            for i, d in enumerate(drafts, 1):
                body = api_tasks.TaskCreate(**{k: d[k] for k in ("title", "scope", "deliverable", "assignee", "reviewer")
                                               if d.get(k)},
                                            from_proposal=src)
                api_tasks.create(database, viewer.p, s, body, f"{op}-d{i}", form.get("recovery"), None)
        except ApiError as e:
            if e.code in FORM_ERRORS:
                return _form_error(s, e.code, "/new-task")
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}/board", status_code=303))

    @app.post("/s/{s}/state", include_in_schema=False)
    async def state_box(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                        database: db.Database = Depends(get_db)):
        """Pause / resume buttons, through the same service as the API."""
        from .api_sessions import StateIn, set_state
        form = await form_data(request)
        check_post(request, form, viewer)
        try:
            body = StateIn(state=form.get("state", ""), reason=form.get("reason", "") or "from the dashboard")
        except ValidationError:
            return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err=invalid", status_code=303))
        try:
            set_state(database, request.app.state.journal_path, viewer.p, s, body, form.get("op") or ids.new_ulid(),
                      form.get("recovery"), None)
        except ApiError as e:
            if e.code == "recovery_mismatch":
                return page("error.html", status=409, viewer=viewer, code=e.code, sess_handle=s,
                            message="The board was restored since this page was loaded. Reload it and try again.")
            if e.code in ("forbidden", "invalid_transition", "invalid_request", "idempotency_key_reused"):
                return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}", status_code=303))

    @app.post("/s/{s}/post", include_in_schema=False)
    async def post_box(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                       database: db.Database = Depends(get_db)):
        form = await form_data(request)
        check_post(request, form, viewer)
        return_to = form.get("return_to", "")
        destination = f"/s/{urllib.parse.quote(s)}/conversations" if return_to == "conversations" \
            else f"/s/{urllib.parse.quote(s)}/chat" if return_to == "chat" else f"/s/{urllib.parse.quote(s)}"
        return_thread = form.get("return_thread", "")
        if return_to == "conversations" and return_thread.isdigit():
            destination += "?mode=thread&thread=" + return_thread
        if return_to == "chat":                                 # B58: back to the same channel filter
            from .api_channels import NAME_RE as _CH
            return_channel = form.get("return_channel", "")
            if _CH.match(return_channel):
                destination += "?channel=" + return_channel
        # B58 [P]: the browser forms accept "@ana" as well as "ana" (the @-completion in Chat inserts handles).
        to = [h.strip().removeprefix("@") for h in form.get("to", "").split(",") if h.strip().removeprefix("@")]
        try:
            body = MessageIn(kind=form.get("kind", ""), body=form.get("body", ""), to=to,
                             re=int(form["re"]) if form.get("re", "").isdigit() else None,
                             preflight_seq=int(form["preflight_seq"]) if form.get("preflight_seq", "").isdigit() else None,
                             channel=form.get("channel") or None)
        except ValidationError:
            separator = "&" if "?" in destination else "?"
            return _security_headers(RedirectResponse(f"{destination}{separator}err=invalid", status_code=303))
        key = form.get("op", "")
        if not (8 <= len(key) <= 64):
            key = ids.new_ulid()
        try:
            send_message(database, request.app.state.post_limiter, viewer.p, s, body, key, form.get("recovery"), None)
        except ApiError as e:
            if e.code == "recovery_mismatch":
                return page("error.html", status=409, viewer=viewer, code=e.code, sess_handle=s,
                            message="The board was restored since this page was loaded. Reload it and post again.")
            if e.code in ("recipient_not_member", "forbidden", "not_found", "invalid_request", "invalid_preflight", "rate_limited",
                          "payload_too_large", "idempotency_key_reused", "channel_archived", "recipient_not_in_channel"):
                separator = "&" if "?" in destination else "?"
                return _security_headers(RedirectResponse(f"{destination}{separator}err={e.code}", status_code=303))
            raise
        anchor = f"#message-{body.re}" if return_to == "conversations" and body.re else \
            "#chat-end" if return_to == "chat" else ""
        return _security_headers(RedirectResponse(destination + anchor, status_code=303))

    @app.post("/s/{s}/preflight", include_in_schema=False)
    async def preflight_box(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                            database: db.Database = Depends(get_db)):
        from .api_messages import record_preflight
        form = await form_data(request)
        check_post(request, form, viewer)
        draft = {"kind": form.get("kind", ""), "body": form.get("body", ""),
                 "to": [h.strip() for h in form.get("to", "").split(",") if h.strip()]}
        if form.get("channel"):
            draft["channel"] = form["channel"]
        if form.get("re"):
            draft["re"] = int(form["re"]) if form["re"].isdigit() else form["re"]
        raw = json.dumps(draft, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        key = form.get("op", "")
        if not (8 <= len(key) <= 64):
            key = ids.new_ulid()
        result = record_preflight(database, request.app.state.post_limiter, viewer.p, s,
                                  raw, len(raw), hashlib.sha256(raw).hexdigest(),
                                  key, form.get("recovery"), None)
        outcome = json.loads(result.body)
        return page("preflight.html", viewer=viewer, sess_handle=s, outcome=outcome,
                    draft=draft, display_kind=render.r2(draft["kind"]),
                    display_to=[render.r2(h) for h in draft["to"]],
                    display_re=render.r2(str(draft.get("re", ""))),
                    raw_body=render.r2(draft["body"], allow_newline=True),
                    to_text=form.get("to", ""), re_text=form.get("re", ""),
                    recovery=form.get("recovery", ""), op=ids.new_ulid())
