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
import os
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, Response, FileResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.staticfiles import StaticFiles

from . import admin_auth, db, events, idempotency, ids, inbox, journal, observe, render, routing, sessions, tlsboot, tokens
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
    return {"accepted": accepted, "total": len(work),
            "percent": round(100 * accepted / len(work)) if work else 0,
            "focus": focus, "achieved": achieved, "stale_parents": stale_parents}


def board_for_display(board: dict, status: dict, full_notes: dict[int, str]) -> dict:
    """Add safe compact text and complete modal text to a read-only board view."""
    stale_parent_map = {parent["task"]: parent for parent in status["stale_parents"]}
    for col in board["columns"]:
        col["label"] = col["status"].replace("_", " ")
        for task in col["tasks"]:
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
                task["note_line"] = render.inline(f"{note['by']}, {note['age_s'] // 60}m ago: {note['text']}", 160)
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
                  "text_line": render.inline(i["text"], 120) if i.get("text") else None} for i in attn["items"]]
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
        handle = form.get("handle", "").strip()
        if not handle or not hmac.compare_digest(handle, form.get("confirm", "").strip()):
            return _security_headers(RedirectResponse(
                "/admin?tab=operations&err=confirmation_required", status_code=303))
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
                          "consumer_since=NULL, consumer_gen=consumer_gen+1 "
                          "WHERE session_id=? AND principal_id=?",
                          (seq, seq - 1, seq - 1, now, sess["id"], viewer.p.id))
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
            api_messages.decide_approval(s, seq, body, key, sess["recovery_id"], None, viewer.p, database)
        except ValidationError:
            return _security_headers(RedirectResponse("/?err=invalid_request", status_code=303))
        except ApiError as e:
            if e.code in ("forbidden", "already_resolved", "not_found", "session_archived"):
                return _security_headers(RedirectResponse(f"/?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse("/", status_code=303))

    @app.get("/attention-count", include_in_schema=False)
    def attention_count(viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        """For browser notifications: only a number, never participant text."""
        from . import api_board
        with database.read() as c:
            n = len(api_board.attention(c, viewer.p)["items"])
        return _security_headers(JSONResponse({"count": n}))

    @app.get("/s/{s}/board", include_in_schema=False)
    def board_page(s: str, viewer: Viewer = Depends(current_viewer), database: db.Database = Depends(get_db)):
        from . import api_board
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            b = board_for_display(api_board.board(c, sess), _run_status(c, sess),
                                  latest_task_note_bodies(c, sess["id"]))
        return page("board.html", viewer=viewer, sess=sess, me=me, board=b)

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
                    deliverables=[item for item in artifacts if item["purpose"] == "deliverable"],
                    evidence=[item for item in artifacts if item["purpose"] == "evidence"], tasks=tasks,
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
                  order: str) -> list[dict]:
        where, args = ["e.session_id = :s"], {"s": sess["id"]}
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
        out = []
        views = inbox.views(c, rows)
        for v in reversed(views) if order == "oldest" else views:
            handle = v["actor"]
            agent = handle.rsplit("/", 1)[-1] if "/" in handle else None
            known = {"codex": ("C", "Codex"), "claude": ("Cl", "Claude"), "grok": ("G", "Grok")}
            mark, name = known.get(agent, (agent[:2].upper(), agent) if agent else ("H", handle))
            out.append({"seq": v["seq"], "type": v["type"], "lines": lines_view(view_lines(v)),
                        "actor": handle, "actor_mark": mark, "actor_name": name,
                        "actor_style": agent if agent in known else ("agent" if agent else "human")})
        return out

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
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            unread_rows, _ = inbox.inbox_page(c, sess, me, me["cursor_seq"], 1000)
            members = c.execute("SELECT m.role, m.job_role, m.job_role_version, m.last_seen_at, p.handle, p.kind, p.display_name FROM member m "
                                "JOIN principal p ON p.id = m.principal_id WHERE m.session_id=? AND m.left_at IS NULL "
                                "ORDER BY p.handle", (sess["id"],)).fetchall()
            timeline = _timeline(c, sess, me, viewer, flt, task, before, order)
            board = _board(c, sess)
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
        query = {k: v for k, v in (("f", flt), ("t", task), ("before", before), ("order", order)) if v is not None}
        return {"sess": sess, "me": me, "flt": flt, "task": task, "order": order,
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
                                    "title": team_title(m["kind"], m["role"]),
                                    "display_name": render.r2(m["display_name"])} for m in members),
                                  key=lambda m: (TEAM_ORDER.index(m["title"]), m["handle"])),
                "query": urllib.parse.urlencode(query)}

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
        text_lines = sync_text(result, target["consumer_runtime"]).split("\n")
        return page("agent.html", viewer=viewer, sess=sess, who=target["handle"], members=members,
                    lines=lines_view(text_lines))

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
            for a in c.execute("SELECT id, handle, kind, active, created_at FROM principal WHERE owner_id=? ORDER BY handle",
                               (viewer.p.id,)):
                creds = [dict(r) for r in c.execute(
                    "SELECT id, label, scope, created_at, last_used_at, expires_at, revoked_at FROM credential "
                    "WHERE principal_id=? ORDER BY created_at DESC", (a["id"],))]
                for cr in creds:
                    cr["label"] = render.r2(cr["label"] or "")
                sessions_of = [dict(r) for r in c.execute(
                    "SELECT s.handle, m.role, m.last_seen_at FROM member m JOIN session s ON s.id=m.session_id "
                    "WHERE m.principal_id=? AND m.left_at IS NULL ORDER BY s.handle", (a["id"],))]
                for so in sessions_of:
                    so["title"] = team_title(a["kind"], so["role"])
                agents.append({"handle": a["handle"], "kind": a["kind"], "active": bool(a["active"]),
                               "created_at": a["created_at"], "tokens": creds, "sessions": sessions_of,
                               "live_tokens": sum(1 for cr in creds if not cr["revoked_at"])})
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
        from .api_tasks import task_view, task_trail
        with database.read() as c:
            sess, me = sessions.require_member(c, s, viewer.p)
            t = c.execute("SELECT * FROM task WHERE session_id=? AND num=?", (sess["id"], n)).fetchone()
            if t is None:
                raise ApiError(404, "not_found", "No such task.")
            view = task_view(c, t, sess)
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
        return page("task.html", viewer=viewer, sess=sess, me=me, t=view, edit=edit, head=lines_view(head), history=history,
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
                                  f"{d['title']} · deliverable: {d['deliverable']}" + (f" · for @{d['assignee']}" if d.get("assignee") else ""), 300)}
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
                body = api_tasks.TaskCreate(**{k: d[k] for k in ("title", "scope", "deliverable", "assignee") if d.get(k)},
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
        to = [h.strip() for h in form.get("to", "").split(",") if h.strip()]
        try:
            body = MessageIn(kind=form.get("kind", ""), body=form.get("body", ""), to=to,
                             re=int(form["re"]) if form.get("re", "").isdigit() else None,
                             preflight_seq=int(form["preflight_seq"]) if form.get("preflight_seq", "").isdigit() else None)
        except ValidationError:
            return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err=invalid", status_code=303))
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
                          "payload_too_large", "idempotency_key_reused"):
                return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}?err={e.code}", status_code=303))
            raise
        return _security_headers(RedirectResponse(f"/s/{urllib.parse.quote(s)}", status_code=303))

    @app.post("/s/{s}/preflight", include_in_schema=False)
    async def preflight_box(s: str, request: Request, viewer: Viewer = Depends(current_viewer),
                            database: db.Database = Depends(get_db)):
        from .api_messages import record_preflight
        form = await form_data(request)
        check_post(request, form, viewer)
        draft = {"kind": form.get("kind", ""), "body": form.get("body", ""),
                 "to": [h.strip() for h in form.get("to", "").split(",") if h.strip()]}
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
