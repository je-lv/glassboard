"""Tasks: create, PATCH, claim, fenced operations, coordinator operations.

State machine, as tables below:
  open --claim--> in_progress (gen+1, new fence)          claimant ops need the fence:
  in_progress --note--> in_progress                         note, check, block, unblock, submit, release
  in_progress|blocked --check--> unchanged (ticks a checklist item; counts as activity)
  in_progress --block--> blocked --unblock--> in_progress
  in_progress|blocked --submit--> review (gen+1, fence dies)
  in_progress|blocked --release--> open (gen+1)             (claimant + fence, or a human coordinator)
  in_progress|blocked --reclaim--> unchanged (gen+1, new fence; same principal, or coordinator on its behalf)
  review --accept--> done            review --return--> open, assignee := previous claimant 
  open|in_progress|blocked|review --reassign--> open, assignee := target (gen+1)
  non-terminal --cancel--> cancelled (gen+1)               done|cancelled --reopen--> open
  any non-terminal --PATCH (title, scope, deliverable, stale_after_s; if_version)--> unchanged, gen unchanged

A fenced op succeeds only if the caller is the claimant AND the fence names this task AND its gen
is the current gen AND its secret matches a non-NULL fence_secret AND the state allows it;
otherwise `409 fence_mismatch` with the current claimant and gen (never a secret).
`last_activity_at` changes ONLY on a successful claim, reclaim, note, block or unblock.
Fences appear only in the claim/reclaim response and its replay; views show `T7 g3`.
Task division is available to agent coordinators. In an explicitly autonomous session, they may
also resolve child-task outcomes, with an approved independent peer review required for acceptance.
Root-task outcomes and fence-less release remain human-only.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets as pysecrets
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field, StrictInt

from . import db, events, ids, inbox, render, textviews, tokens
from .api_messages import parse_task
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import MAX_REFS, Ref, StrictModel
from .mutation import SessionTx, runtime_label, session_tx
from . import sessions
from .sessions import PRINCIPAL_HANDLE_RE, require_member
from .textviews import view_lines

router = APIRouter(prefix="/api/v1")
B32 = "abcdefghijklmnopqrstuvwxyz234567"
FENCE_RE = re.compile(r"^T([0-9]{1,9})g([0-9]{1,9})-([a-z2-7]{10})$")
ACTIVE = ("in_progress", "blocked")
NON_TERMINAL = ("open", "in_progress", "blocked", "review")
PATCHABLE = ("title", "scope", "deliverable", "stale_after_s", "checklist", "depends_on", "parent", "priority", "reviewer")
PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_RANK = {p: i for i, p in enumerate(reversed(PRIORITIES))}          # urgent first
MAX_DEPENDS = 20
_UNCHANGED = object()                                   # PATCH: a field that was not changed
MAX_CHECKLIST, MAX_ITEM = 20, 200
MAX_TEST_EVIDENCE = 20
COMMIT_RE = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
WAITING_RE = re.compile(r"^(?:#(?P<seq>[0-9]{1,9})|T(?P<task>[0-9]{1,9})|@(?P<handle>[a-z0-9-]{1,32}(?:/[a-z0-9-]{1,32})?))$")
NOT_PATCHABLE = ("assignee", "status", "claimant", "gen", "fence", "version")

# op → (allowed from-states, to-state or None for unchanged, gen bump, refreshes activity, ends claim)
FENCED = {
    "note": (("in_progress",), None, False, True, False),
    "check": (ACTIVE, None, False, True, False),
    "block": (("in_progress",), "blocked", False, True, False),
    "unblock": (("blocked",), "in_progress", False, True, False),
    "submit": (ACTIVE, "review", True, False, True),
    "release": (ACTIVE, "open", True, False, True),
}
COORD = {
    "accept": (("review",), "done", False),
    "return": (("review",), "open", False),
    "reassign": (NON_TERMINAL, "open", True),
    "cancel": (NON_TERMINAL, "cancelled", True),
    "reopen": (("done", "cancelled"), "open", False),
}
EVENT = {"note": "task.note", "check": "task.checked", "block": "task.blocked", "unblock": "task.unblocked", "submit": "task.submitted",
         "release": "task.released", "accept": "task.accepted", "return": "task.returned",
         "reassign": "task.reassigned", "cancel": "task.cancelled", "reopen": "task.reopened"}


def new_fence(num: int, gen: int) -> tuple[str, str]:
    secret = "".join(pysecrets.choice(B32) for _ in range(10))      # 50 bits
    return f"T{num}g{gen}-{secret}", secret


def _age_s(ts: str | None) -> int | None:
    if not ts:
        return None
    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


def effective_stale_after(task, sess) -> int:
    return task["stale_after_s"] if task["stale_after_s"] is not None else sess["stale_after_s"]


def is_stale(task, sess) -> bool:
    age = _age_s(task["last_activity_at"])
    return task["status"] in ACTIVE and age is not None and age > effective_stale_after(task, sess)


def checklist_of(task) -> list[dict]:
    return json.loads(task["checklist"] or "[]")


def progress_of(items: list[dict]) -> dict | None:
    return {"done": sum(1 for i in items if i["done"]), "total": len(items)} if items else None


def waiting_view(c, sid: str, task) -> dict | None:
    """What a blocked task waits on, and whether that is resolved now (never acted on automatically)."""
    if not task["waiting_on"]:
        return None
    w = json.loads(task["waiting_on"])
    resolved = None
    if w["kind"] == "question":
        resolved = not inbox.obligation_holders(c, sid, w["ref"])
    elif w["kind"] == "task":
        row = c.execute("SELECT status FROM task WHERE session_id=? AND num=?", (sid, w["ref"])).fetchone()
        resolved = bool(row) and row["status"] == "done"
    return {"kind": w["kind"], "label": w["label"], "resolved": resolved}


def depends_of(task) -> list[int]:
    return json.loads(task["depends_on"] or "[]")


def dependency_view(c, sid: str, task) -> tuple[list[dict], bool]:
    deps = depends_of(task)
    if not deps:
        return [], True
    q = ",".join("?" * len(deps))
    st = {r["num"]: r["status"] for r in c.execute(f"SELECT num, status FROM task WHERE session_id=? AND num IN ({q})",
                                                    (sid, *deps))}
    view = [{"task": n, "status": st.get(n, "missing")} for n in deps]
    return view, all(d["status"] == "done" for d in view)


def children_of(c, sid: str, num: int) -> dict | None:
    rows = c.execute("SELECT status FROM task WHERE session_id=? AND parent=? AND status != 'cancelled'", (sid, num)).fetchall()
    return {"done": sum(1 for r in rows if r["status"] == "done"), "total": len(rows)} if rows else None


def task_view(c, task, sess) -> dict:
    """Public JSON of a task. The fence is shown only as `T<n> g<gen>`."""
    h = inbox.handle_map(c, [task["assignee_id"], task["claimant_id"], task["created_by"]])
    items = checklist_of(task)
    return {"task": task["num"], "id": task["id"], "title": task["title"], "scope": task["scope"],
            "deliverable": task["deliverable"], "status": task["status"], "version": task["version"], "gen": task["gen"],
            "fence": f"T{task['num']} g{task['gen']}" if task["fence_secret"] else None,
            "assignee": h.get(task["assignee_id"]), "claimant": h.get(task["claimant_id"]),
            "claimant_runtime": task["claimant_runtime"], "last_activity_at": task["last_activity_at"],
            "activity_age_s": _age_s(task["last_activity_at"]), "stale": is_stale(task, sess),
            "stale_after_s": task["stale_after_s"], "effective_stale_after_s": effective_stale_after(task, sess),
            "summary": task["summary"], "summary_needs_detail": bool(task["summary"]) and summary_needs_detail(task["summary"]),
            "created_by": h.get(task["created_by"]), "created_at": task["created_at"],
            "updated_at": task["updated_at"], "checklist": items, "progress": progress_of(items),
            "waiting_on": waiting_view(c, sess["id"], task), **assignment_view(c, sess["id"], task),
            **_division_view(c, sess["id"], task)}


def assignment_view(c, sid: str, task) -> dict:
    """Acknowledgment is an event tied to the current assignment, never a hidden inbox ack."""
    if task["status"] != "open":
        return {"assignment_stage": "working" if task["status"] in ACTIVE else task["status"],
                "assignment_age_s": None, "ack_overdue": False,
                "assignee_synced_since_assignment": None, "assignee_last_seen_at": None}
    if not task["assignee_id"]:
        return {"assignment_stage": "unassigned", "assignment_age_s": None, "ack_overdue": False,
                "assignee_synced_since_assignment": None, "assignee_last_seen_at": None}
    row = c.execute("SELECT type, ts FROM event WHERE session_id=? AND task_num=? AND type IN "
                    "('task.created','task.reassigned','task.returned','task.released','task.reopened',"
                    "'task.acknowledged') ORDER BY seq DESC LIMIT 1", (sid, task["num"])).fetchone()
    acknowledged = row is not None and row["type"] == "task.acknowledged"
    age = _age_s(row["ts"] if row is not None else task["updated_at"])
    member = c.execute("SELECT last_seen_at FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                       (sid, task["assignee_id"])).fetchone()
    last_seen = member["last_seen_at"] if member is not None else None
    assignment_event = (c.execute("SELECT ts FROM event WHERE session_id=? AND task_num=? AND type IN "
                                  "('task.created','task.reassigned','task.returned','task.released','task.reopened') "
                                  "ORDER BY seq DESC LIMIT 1", (sid, task["num"])).fetchone()
                        if acknowledged else row)
    assigned_at = assignment_event["ts"] if assignment_event is not None else task["updated_at"]
    return {"assignment_stage": "acknowledged" if acknowledged else "assigned",
            "assignment_age_s": age, "ack_overdue": not acknowledged and age is not None and age >= 300,
            "assignee_synced_since_assignment": bool(last_seen and last_seen >= assigned_at),
            "assignee_last_seen_at": last_seen}


def summary_needs_detail(summary: str | None) -> bool:
    """Flag summaries that cannot explain what the claimant delivered."""
    value = (summary or "").strip()
    return not value or len(value) < 12 or len(value.split()) < 2 or value.casefold() in {"n/a", "none", "tbd", "todo"}


def _division_view(c, sid: str, task) -> dict:
    deps, ready = dependency_view(c, sid, task)
    return {"priority": task["priority"], "depends_on": deps, "ready": ready, "parent": task["parent"],
            "children": children_of(c, sid, task["num"]), **review_view(c, sid, task)}


def review_view(c, sid: str, task) -> dict:
    """The named reviewer, and the latest review verdict given since the task was last submitted."""
    h = inbox.handle_map(c, [task["reviewer_id"]])
    submitted = latest_submission(c, sid, task["num"])
    last_submit = submitted["seq"] if submitted else None
    row = c.execute("SELECT seq, actor_id, data FROM event WHERE session_id=? AND task_num=? AND type='task.reviewed' "
                    "AND seq > ? ORDER BY seq DESC LIMIT 1", (sid, task["num"], last_submit or 0)).fetchone()
    review = None
    if row is not None:
        data = json.loads(row["data"])
        review = {"seq": row["seq"], "by": inbox.handle_map(c, [row["actor_id"]]).get(row["actor_id"]),
                  "verdict": data["verdict"], "submission_seq": data.get("submission_seq"),
                  "commit": data.get("commit")}
    return {"reviewer": h.get(task["reviewer_id"]), "submission": submitted, "review": review}


def latest_submission(c, sid: str, num: int) -> dict | None:
    row = c.execute("SELECT seq, data FROM event WHERE session_id=? AND task_num=? AND type='task.submitted' "
                    "ORDER BY seq DESC LIMIT 1", (sid, num)).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"] or "{}")
    return {"seq": row["seq"], "commit": data.get("commit"), "tests": data.get("tests", []),
            "refs": data.get("refs", []), "summary": data.get("summary")}


TRAIL_TYPES = ("task.created", "task.acknowledged", "task.claimed", "task.reassigned", "task.edited",
               "task.note", "task.checked", "task.blocked", "task.unblocked", "task.submitted",
               "task.reviewed", "task.returned", "task.accepted", "task.released", "task.reopened", "task.cancelled")


def task_trail(c, sid: str, num: int) -> list[dict]:
    """Review-oriented projection of the append-only task record; raw events stay available."""
    marks = ",".join("?" for _ in TRAIL_TYPES)
    rows = c.execute(f"SELECT * FROM event WHERE session_id=? AND task_num=? AND type IN ({marks}) ORDER BY seq",
                     (sid, num, *TRAIL_TYPES)).fetchall()
    result = []
    for ev in inbox.views(c, rows):
        data = ev.get("data") or {}
        detail = (data.get("summary") or data.get("feedback") or data.get("reason") or
                  data.get("note") or ev.get("body") or data.get("title") or "")
        result.append({"seq": ev["seq"], "ts": ev["ts"], "by": ev["actor"], "type": ev["type"],
                       "detail": detail, "refs": data.get("refs") or []})
    return result


def claims_of(c, sess, principal_id: str) -> list[dict]:
    """The `claims` block of a sync response."""
    rows = c.execute("SELECT * FROM task WHERE session_id=? AND claimant_id=? AND status IN ('in_progress','blocked') "
                     "ORDER BY num", (sess["id"], principal_id)).fetchall()
    return [{"task": t["num"], "status": t["status"], "gen": t["gen"], "activity_age_s": _age_s(t["last_activity_at"]),
             "stale": is_stale(t, sess), "progress": progress_of(checklist_of(t)),
             "waiting_on": waiting_view(c, sess["id"], t)} for t in rows]


def _load(c, sid: str, num: int):
    t = c.execute("SELECT * FROM task WHERE session_id=? AND num=?", (sid, num)).fetchone()
    if t is None:
        raise ApiError(404, "not_found", "No such task in this session.")
    return t


def _work_divider(tx: SessionTx) -> None:
    """Dividing the work (create, edit, reassign): a human coordinator, or an agent the humans made a
    coordinator of the session (shown as a Task Manager). [owner direction 2026-10-07,
    ERRATA E23, narrowed for autonomous child tasks by E27]"""
    if tx.me["role"] != "coordinator" or tx.p.kind not in ("human", "agent"):
        raise ApiError(403, "forbidden", "Only a Session Manager or Task Manager may do this.")


def _human_coordinator(tx: SessionTx) -> None:
    if tx.p.kind != "human" or tx.me["role"] != "coordinator":
        raise ApiError(403, "forbidden", "Only a human coordinator may do this.")


def _outcome_coordinator(tx: SessionTx, task) -> bool:
    """Return True only for an agent coordinator acting on an autonomous child task."""
    if tx.p.kind == "human":
        _human_coordinator(tx)
        return False
    _work_divider(tx)
    if not tx.sess["autonomous"] or task["parent"] is None:
        raise ApiError(403, "forbidden", "Only a human coordinator may decide this task outcome.")
    return True


def _check_version(task, if_version: int | None) -> None:
    if if_version is not None and if_version != task["version"]:
        raise ApiError(409, "version_conflict", "The task changed since you read it.",
                       hint="Re-read the task and try again.", details={"version": task["version"]})


def _fence_mismatch(c, task) -> ApiError:
    h = inbox.handle_map(c, [task["claimant_id"]])
    return ApiError(409, "fence_mismatch", "This fence is not valid for the task now.",
                    hint="Stop work on this task and sync; reclaim it if it is still yours.",
                    details={"task": task["num"], "gen": task["gen"], "claimant": h.get(task["claimant_id"])})


def _state(tx: SessionTx, op: str) -> None:
    sessions.require_state(tx.sess, op)


def _result(task_num: int, status: str, gen: int, version: int, seq: int, **extra) -> dict:
    return {"task": task_num, "status": status, "gen": gen, "seq": seq, "version": version, **extra}


def _update(c, sid: str, num: int, **cols) -> None:
    cols["updated_at"] = db.utcnow()
    sets = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE task SET {sets} WHERE session_id=? AND num=?", (*cols.values(), sid, num))


def _recovery_and_replay(tx: SessionTx, recovery, key, op: str, task, payload: dict, bind_runtime=False, status=200):
    tx.check_recovery(recovery)
    return tx.replay(key, op, payload, target={"type": "task", "id": task["id"]}, bind_runtime=bind_runtime, status=status)


# --- create, list, show, PATCH -----------------------------------------------------------------------

class TaskCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=16 * 1024)
    deliverable: str = Field(min_length=1, max_length=512)
    assignee: str | None = Field(default=None, pattern=PRINCIPAL_HANDLE_RE.pattern)
    stale_after_s: StrictInt | None = Field(default=None, ge=300, le=14400)
    checklist: list[str] = Field(default_factory=list, max_length=MAX_CHECKLIST)
    depends_on: list[int | str] = Field(default_factory=list, max_length=MAX_DEPENDS)
    parent: int | str | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    reviewer: str | None = Field(default=None, pattern=PRINCIPAL_HANDLE_RE.pattern)
    # A task may be made from a proposal; the task.created event replies to it.
    from_proposal: StrictInt | None = Field(default=None, ge=1)


def _bad_field(field: str, kind: str, message: str = "The request failed validation.") -> ApiError:
    return ApiError(422, "dependency_cycle" if kind == "cycle" else "invalid_request", message,
                    details={"fields": [{"loc": ["body", field], "type": kind}]})


def check_division(c, sid: str, num: int, depends_on, parent) -> tuple[list[int], int | None]:
    """Dependencies and parent must name other tasks of this session and never form a cycle."""
    try:
        deps = sorted({parse_task(d) for d in depends_on})
        par = parse_task(parent) if parent is not None else None
    except ValueError:
        raise _bad_field("depends_on", "task_ref") from None
    existing = {r["num"]: r for r in c.execute("SELECT num, depends_on, parent FROM task WHERE session_id=?", (sid,))}
    if any(d == num or d not in existing for d in deps):
        raise _bad_field("depends_on", "unknown_task")
    if par is not None and (par == num or par not in existing):
        raise _bad_field("parent", "unknown_task")
    graph = {n: json.loads(r["depends_on"] or "[]") for n, r in existing.items()}
    graph[num] = deps
    seen, stack = set(), list(deps)
    while stack:                                           # can num be reached from its own dependencies?
        n = stack.pop()
        if n == num:
            raise _bad_field("depends_on", "cycle", "These dependencies would form a cycle.")
        if n not in seen:
            seen.add(n)
            stack.extend(graph.get(n, []))
    p_ = par
    while p_ is not None:                                  # parents never form a cycle either
        if p_ == num:
            raise _bad_field("parent", "cycle", "This parent would form a cycle.")
        p_ = existing[p_]["parent"] if p_ in existing else None
    return deps, par


def new_checklist(texts: list[str], old: list[dict]) -> list[dict]:
    """Items get ids 1..n in order. An item whose text was already on the list keeps its `done`."""
    if any(not t.strip() or len(t) > MAX_ITEM for t in texts):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       details={"fields": [{"loc": ["body", "checklist"], "type": "string_length"}]})
    done = {i["text"]: i["done"] for i in old}
    return [{"id": n, "text": t, "done": done.get(t, False)} for n, t in enumerate(texts, 1)]


def _member_principal(c, sid: str, handle: str):
    row = c.execute("SELECT p.id, p.handle, m.role FROM principal p JOIN member m ON m.principal_id = p.id "
                    "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (handle, sid)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such member.")
    return row


@router.post("/sessions/{s}/tasks", status_code=201)
def create_task(s: str, body: TaskCreate, key: str = Depends(idempotency_key),
                recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                runtime: str | None = Depends(runtime_label),
                p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return create(database, p, s, body, key, recovery, runtime)


def create(database: db.Database, p: tokens.Principal, s: str, body: TaskCreate, key: str, recovery, runtime):
    """The one implementation of task creation (API and dashboard)."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        assignee = _member_principal(c, sid, body.assignee) if body.assignee else None
        reviewer = _member_principal(c, sid, body.reviewer) if body.reviewer else None
        if body.from_proposal is not None and not c.execute(
                "SELECT 1 FROM event WHERE session_id=? AND seq=? AND type='proposal'", (sid, body.from_proposal)).fetchone():
            raise ApiError(404, "not_found", "No such proposal in this session.")
        tx.check_recovery(recovery)
        payload = {**body.model_dump(), "assignee": assignee["id"] if assignee else None}
        replay = tx.replay(key, "POST /sessions/{s}/tasks", payload, status=201)
        if replay is not None:
            return replay
        _work_divider(tx)
        _state(tx, "task.create")
        num = tx.sess["next_task_num"]
        c.execute("UPDATE session SET next_task_num=next_task_num+1 WHERE id=?", (sid,))
        now, tid = db.utcnow(), ids.new_ulid()
        items = new_checklist(body.checklist, [])
        deps, parent = check_division(c, sid, num, body.depends_on, body.parent)
        c.execute("INSERT INTO task(session_id,num,id,title,scope,deliverable,assignee_id,status,version,gen,stale_after_s,"
                  "created_by,created_at,updated_at,checklist,depends_on,parent,priority,reviewer_id) "
                  "VALUES (?,?,?,?,?,?,?,'open',1,0,?,?,?,?,?,?,?,?,?)",
                  (sid, num, tid, body.title, body.scope, body.deliverable, assignee["id"] if assignee else None,
                   body.stale_after_s, p.id, now, now, json.dumps(items), json.dumps(deps), parent, body.priority,
                   reviewer["id"] if reviewer else None))
        _, text = tx.event("task.created", task_num=num, assignee=assignee["id"] if assignee else None,
                           reply_to=body.from_proposal,
                           data={"title": body.title, "deliverable": body.deliverable, "scope": body.scope,
                                 "assignee": assignee["handle"] if assignee else None, "stale_after_s": body.stale_after_s,
                                 **({"checklist": [i["text"] for i in items]} if items else {}),
                                 **({"depends_on": ", ".join(f"T{d}" for d in deps)} if deps else {}),
                                 **({"parent": f"T{parent}"} if parent else {}),
                                 **({"priority": body.priority} if body.priority != "normal" else {}),
                                 **({"reviewer": reviewer["handle"]} if reviewer else {}),
                                 "from_proposal": body.from_proposal},
                           receipt=lambda seq: {"task": num, "id": tid, "status": "open", "version": 1, "gen": 0, "seq": seq})
    return respond(text, 201)


@router.get("/sessions/{s}/tasks")
def list_tasks(s: str, request: Request, status: str | None = Query(None, max_length=80),
               assignee: str | None = Query(None, max_length=80),
               mine: bool = Query(False), stale: bool | None = Query(None),
               format: Literal["json", "text"] = Query("json"),
               p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        where, args = ["session_id = ?"], [sess["id"]]
        if status:
            wanted = [x for x in status.split(",") if x in ("open", "in_progress", "blocked", "review", "done", "cancelled")]
            where.append(f"status IN ({','.join('?' * len(wanted)) or 'NULL'})"); args += wanted
        if assignee:
            where.append("assignee_id = (SELECT id FROM principal WHERE handle=?)"); args.append(assignee)
        if mine:
            where.append("(claimant_id = ? OR assignee_id = ?)"); args += [p.id, p.id]
        rows = c.execute(f"SELECT * FROM task WHERE {' AND '.join(where)} ORDER BY num", args).fetchall()
        views = [task_view(c, t, sess) for t in rows]
    if stale is not None:
        views = [v for v in views if v["stale"] == stale]
    result = {"session": sess["handle"], "tasks": views}
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        return PlainTextResponse(textviews.tasks_text(result))
    return result


@router.get("/sessions/{s}/tasks/next")
def next_tasks(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
               p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """A suggestion, never an assignment: open tasks that are ready (every dependency done) and assigned to
    the caller or to nobody, most urgent first, then oldest."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        rows = c.execute("SELECT * FROM task WHERE session_id=? AND status='open' AND (assignee_id IS NULL OR assignee_id=?)",
                         (sess["id"], p.id)).fetchall()
        views = [v for v in (task_view(c, t, sess) for t in rows) if v["ready"]]
    views.sort(key=lambda v: (PRIORITY_RANK[v["priority"]], v["created_at"], v["task"]))
    result = {"session": sess["handle"], "tasks": views}
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        return PlainTextResponse(textviews.tasks_text(result, heading="ready for you"))
    return result


@router.get("/sessions/{s}/tasks/{t}")
def show_task(s: str, t: str, request: Request, format: Literal["json", "text"] = Query("json"),
              p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    try:
        num = parse_task(t)
    except ValueError:
        raise ApiError(404, "not_found", "No such task in this session.") from None
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        task = _load(c, sess["id"], num)
        view = task_view(c, task, sess)
        evs = inbox.views(c, c.execute("SELECT * FROM (SELECT * FROM event WHERE session_id=? AND task_num=? "
                                       "ORDER BY seq DESC LIMIT 20) ORDER BY seq", (sess["id"], num)))
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        lines = [textviews.task_head(view), render.inline_line("title", view["title"]), render.inline_line("deliverable", view["deliverable"]),
                 *textviews.checklist_lines(view), *render.block_lines(view["scope"])]
        if view["summary"]:
            lines.append(render.inline_line("summary", view["summary"]))
        if view["submission"]:
            lines.append(render.inline_line("submission", f"#{view['submission']['seq']}"))
            if view["submission"]["commit"]:
                lines.append(render.inline_line("commit", view["submission"]["commit"]))
            for item in view["submission"]["tests"]:
                detail = f" ({item['detail']})" if item.get("detail") else ""
                lines.append(render.inline_line("test", f"{item['outcome']}: {item['command']}{detail}"))
        handoff = next((v for v in reversed(evs) if (v.get("data") or {}).get("handoff_fields")), None)
        if handoff is not None:                         # the next claimant reads this first
            lines.append(render.section(f"--- latest handoff #{handoff['seq']} by {handoff['actor']} ---"))
            lines += [render.inline_line(k, handoff["data"]["handoff_fields"][k])
                      for k in ("done", "next", "gotchas") if handoff["data"]["handoff_fields"].get(k)]
        for v in evs:
            lines.extend(view_lines(v))
        return PlainTextResponse("\n".join(lines) + "\n")
    return {**view, "events": evs}


@router.get("/sessions/{s}/tasks/{t}/trail")
def show_task_trail(s: str, t: str, request: Request, format: Literal["json", "text"] = Query("json"),
                    p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        task = _load(c, sess["id"], _num(t))
        trail = task_trail(c, sess["id"], task["num"])
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        lines = [render.section(f"--- review trail · T{task['num']} · {len(trail)} events ---")]
        for item in trail:
            lines.append(render.section(f"--- #{item['seq']} · {item['type']} · {item['by']} ---"))
            if item["detail"]:
                lines.append(render.inline_line("detail", item["detail"]))
            for ref in item["refs"]:
                lines.append(render.ref_line(ref))
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"session": sess["handle"], "task": task["num"], "trail": trail}


class TaskPatch(StrictModel):
    if_version: StrictInt = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    scope: str | None = Field(default=None, min_length=1, max_length=16 * 1024)
    deliverable: str | None = Field(default=None, min_length=1, max_length=512)
    stale_after_s: StrictInt | None = Field(default=None, ge=300, le=14400)
    checklist: list[str] | None = Field(default=None, max_length=MAX_CHECKLIST)
    depends_on: list[int | str] | None = Field(default=None, max_length=MAX_DEPENDS)
    parent: int | str | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    reviewer: str | None = Field(default=None, pattern=PRINCIPAL_HANDLE_RE.pattern)
    # Ownership and state are never patchable. They are declared only so that sending
    # them yields the specific `field_not_patchable` answer instead of a generic extra-field error.
    assignee: Any = None
    status: Any = None
    claimant: Any = None
    gen: Any = None
    fence: Any = None
    version: Any = None


@router.patch("/sessions/{s}/tasks/{t}")
def patch_task(s: str, t: str, body: TaskPatch, key: str = Depends(idempotency_key),
               recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
               runtime: str | None = Depends(runtime_label),
               p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return patch(database, p, s, t, body, key, recovery, runtime)


def patch(database: db.Database, p: tokens.Principal, s: str, t: str, body: TaskPatch, key: str, recovery, runtime):
    """The one implementation of task PATCH (API and dashboard)."""
    sent = body.model_fields_set
    bad = [f for f in NOT_PATCHABLE if f in sent]
    if bad:
        raise ApiError(422, "field_not_patchable", "Ownership and state cannot be changed with PATCH.",
                       hint="Use reassign, cancel or reopen.", details={"fields": [{"loc": ["body", f], "type": "not_patchable"} for f in bad]})
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        special = ("checklist", "depends_on", "parent", "reviewer")
        if "reviewer" in sent:
            rid = _member_principal(c, sid, body.reviewer)["id"] if body.reviewer else None
            changes_reviewer = rid if rid != task["reviewer_id"] else _UNCHANGED
        else:
            changes_reviewer = _UNCHANGED
        changes = {f: getattr(body, f) for f in PATCHABLE if f not in special and f in sent
                   and getattr(body, f) is not None and getattr(body, f) != task[f]}
        if "checklist" in sent:
            items = new_checklist(body.checklist or [], checklist_of(task))
            if items != checklist_of(task):
                changes["checklist"] = items
        if "depends_on" in sent or "parent" in sent:
            deps, par = check_division(c, sid, task["num"],
                                       body.depends_on if "depends_on" in sent else depends_of(task),
                                       body.parent if "parent" in sent else task["parent"])
            if deps != depends_of(task):
                changes["depends_on"] = deps
            if par != task["parent"]:
                changes["parent"] = par
        if changes_reviewer is not _UNCHANGED:
            changes["reviewer_id"] = changes_reviewer
        payload = {"if_version": body.if_version, **{f: getattr(body, f) for f in PATCHABLE if f in sent}}
        replay = _recovery_and_replay(tx, recovery, key, "PATCH /sessions/{s}/tasks/{t}", task, payload)
        if replay is not None:
            return replay
        _work_divider(tx)
        _state(tx, "task.coord")
        if task["status"] not in NON_TERMINAL:
            raise ApiError(409, "invalid_transition", "Finished tasks cannot be edited; reopen first.")
        _check_version(task, body.if_version)
        if p.kind == "agent" and task["parent"] is None and "parent" in changes:
            raise ApiError(403, "forbidden", "Only a human coordinator may reparent a root task.")
        if not changes:
            return respond(json.dumps(_result(task["num"], task["status"], task["gen"], task["version"], None),
                                      separators=(",", ":")), 200)
        version = task["version"] + 1
        cols = {k: (json.dumps(v) if k in ("checklist", "depends_on") else v) for k, v in changes.items()}
        _update(c, sid, task["num"], version=version, **cols)     # never gen, fence or last_activity_at
        shown = {k: ([i["text"] for i in v] if k == "checklist" else
                     ", ".join(f"T{d}" for d in v) or "none" if k == "depends_on" else
                     (f"T{v}" if v else "none") if k == "parent" else v) for k, v in changes.items() if k != "reviewer_id"}
        if "reviewer_id" in changes:
            shown["reviewer"] = inbox.handle_map(c, [changes["reviewer_id"]]).get(changes["reviewer_id"]) or "none"
        _, text = tx.event("task.edited", task_num=task["num"], claimant=task["claimant_id"], assignee=task["assignee_id"],
                           data={**shown, "gen": task["gen"]},
                           receipt=lambda seq: _result(task["num"], task["status"], task["gen"], version, seq))
    return respond(text, 200)


def _num(t: str) -> int:
    try:
        return parse_task(t)
    except ValueError:
        raise ApiError(404, "not_found", "No such task in this session.") from None


# --- claim and reclaim (mint fences) ---------------------------------------------------------------

class ClaimIn(StrictModel):
    pass


class AcknowledgeIn(StrictModel):
    pass


@router.post("/sessions/{s}/tasks/{t}/acknowledge")
def acknowledge_route(s: str, t: str, body: AcknowledgeIn, key: str = Depends(idempotency_key),
                      recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                      runtime: str | None = Depends(runtime_label),
                      p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return acknowledge(database, p, s, t, body, key, recovery, runtime)


def acknowledge(database: db.Database, p: tokens.Principal, s: str, t: str, body: AcknowledgeIn,
                key: str, recovery, runtime):
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        replay = _recovery_and_replay(tx, recovery, key, "POST /sessions/{s}/tasks/{t}/acknowledge", task, {})
        if replay is not None:
            return replay
        if task["assignee_id"] != p.id or tx.me["role"] == "observer":
            raise ApiError(403, "forbidden", "Only the assigned participant can acknowledge this task.")
        _state(tx, "task.acknowledge")
        if task["status"] != "open" or assignment_view(c, sid, task)["assignment_stage"] != "assigned":
            raise ApiError(409, "invalid_transition", "This assignment is not awaiting acknowledgment.")
        version = task["version"] + 1
        _update(c, sid, task["num"], version=version)
        _, text = tx.event("task.acknowledged", task_num=task["num"], assignee=p.id,
                           receipt=lambda seq: _result(task["num"], "open", task["gen"], version, seq))
    return respond(text, 200)


@router.post("/sessions/{s}/tasks/{t}/claim", status_code=201)
def claim(s: str, t: str, body: ClaimIn, key: str = Depends(idempotency_key),
          recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
          runtime: str | None = Depends(runtime_label),
          p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        replay = _recovery_and_replay(tx, recovery, key, "POST /sessions/{s}/tasks/{t}/claim", task, {},
                                      bind_runtime=True, status=201)
        if replay is not None:
            return replay
        if tx.me["role"] not in ("member", "coordinator"):
            raise ApiError(403, "forbidden", "Observers cannot claim tasks.")
        _state(tx, "task.claim")
        if task["status"] in ACTIVE:
            raise ApiError(409, "task_already_claimed", "Someone already holds this task.",
                           details={"claimant": inbox.handle_map(c, [task["claimant_id"]]).get(task["claimant_id"]),
                                    "gen": task["gen"]})
        if task["status"] != "open" or (task["assignee_id"] and task["assignee_id"] != p.id):
            raise ApiError(409, "task_not_claimable", "This task cannot be claimed by you now.",
                           hint="It is assigned to someone else, or not open.")
        deps, ready = dependency_view(c, sid, task)
        if not ready:
            raise ApiError(409, "task_waiting", "This task depends on tasks that are not done yet.",
                           hint="Work on what it needs first, or ask a human to change its dependencies.",
                           details={"needs": [f"T{d['task']}" for d in deps if d["status"] != "done"]})
        gen, version = task["gen"] + 1, task["version"] + 1
        fence, secret = new_fence(task["num"], gen)
        now = db.utcnow()
        _update(c, sid, task["num"], status="in_progress", gen=gen, version=version, fence_secret=secret,
                claimant_id=p.id, claimant_runtime=runtime, last_activity_at=now)
        _, text = tx.event("task.claimed", task_num=task["num"], data={"gen": gen},
                           receipt=lambda seq: _result(task["num"], "in_progress", gen, version, seq, fence=fence))
    return respond(text, 201)


class ReclaimIn(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/sessions/{s}/tasks/{t}/reclaim")
def reclaim(s: str, t: str, body: ReclaimIn, key: str = Depends(idempotency_key),
            recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
            runtime: str | None = Depends(runtime_label),
            p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Same principal (a new runtime after a crash or context reset), or a coordinator on its
    behalf. A coordinator's reclaim kills the old fence but gets no fence itself."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        replay = _recovery_and_replay(tx, recovery, key, "POST /sessions/{s}/tasks/{t}/reclaim", task,
                                      body.model_dump(), bind_runtime=True)
        if replay is not None:
            return replay
        own = task["claimant_id"] == p.id
        if not own and tx.me["role"] != "coordinator":
            raise ApiError(403, "forbidden", "Only the claimant or a coordinator may reclaim.")
        _state(tx, "task.reclaim")
        if task["status"] not in ACTIVE:
            raise ApiError(409, "invalid_transition", "Only an active claim can be reclaimed.")
        gen, version = task["gen"] + 1, task["version"] + 1
        fence, secret = new_fence(task["num"], gen)
        _update(c, sid, task["num"], gen=gen, version=version, fence_secret=secret,
                claimant_runtime=runtime if own else task["claimant_runtime"], last_activity_at=db.utcnow())
        _, text = tx.event("task.reclaimed", task_num=task["num"], body=body.reason,
                           data={"gen": gen, "on_behalf_of": None if own else
                                 inbox.handle_map(c, [task["claimant_id"]]).get(task["claimant_id"])},
                           receipt=lambda seq: _result(task["num"], task["status"], gen, version, seq,
                                                       fence=fence if own else None))
    return respond(text, 200)


# --- fenced claimant operations ------------------------------------------------------------------------

class NoteIn(StrictModel):
    fence: str = Field(max_length=32)
    note: str = Field(min_length=1, max_length=16 * 1024)
    effort_min: StrictInt | None = Field(default=None, ge=1, le=1440)     # minutes of work, self-reported


class HandoffFields(StrictModel):
    done: str | None = Field(default=None, min_length=1, max_length=1024)
    next: str | None = Field(default=None, min_length=1, max_length=1024)
    gotchas: str | None = Field(default=None, min_length=1, max_length=1024)


class ReasonIn(StrictModel):
    fence: str = Field(max_length=32)
    reason: str = Field(min_length=1, max_length=16 * 1024)
    waiting_on: str | None = Field(default=None, max_length=70)     # "#12" (question), "T3" (task) or "@alice"


class CheckIn(StrictModel):
    fence: str = Field(max_length=32)
    item: StrictInt = Field(ge=1, le=MAX_CHECKLIST)
    done: bool = True


class UnblockIn(StrictModel):
    fence: str = Field(max_length=32)
    reason: str | None = Field(default=None, min_length=1, max_length=16 * 1024)


class SubmitIn(StrictModel):
    fence: str = Field(max_length=32)
    summary: str = Field(min_length=1, max_length=1024)
    refs: list[Ref] = Field(default_factory=list, max_length=MAX_REFS)
    handoff_fields: HandoffFields | None = None
    commit: str | None = Field(default=None, pattern=COMMIT_RE)
    tests: list["TestEvidence"] = Field(default_factory=list, max_length=MAX_TEST_EVIDENCE)
    incomplete_checklist_reason: str | None = Field(default=None, min_length=1, max_length=1024)


class TestEvidence(StrictModel):
    command: str = Field(min_length=1, max_length=1024)
    outcome: Literal["pass", "fail", "skipped"]
    detail: str | None = Field(default=None, min_length=1, max_length=2048)


class ReleaseIn(StrictModel):
    fence: str | None = Field(default=None, max_length=32)     # absent: a human coordinator releases
    handoff: str | None = Field(default=None, min_length=1, max_length=16 * 1024)
    handoff_fields: HandoffFields | None = None


def _check_fence(c, task, p: tokens.Principal, fence: str | None) -> None:
    m = FENCE_RE.match(fence or "")
    ok = (m is not None and task["fence_secret"] is not None and task["claimant_id"] == p.id
          and int(m.group(1)) == task["num"] and int(m.group(2)) == task["gen"]
          and hmac.compare_digest(m.group(3).encode(), task["fence_secret"].encode()))
    if not ok:
        raise _fence_mismatch(c, task)


def _fenced(op: str, s: str, t: str, body, key, recovery, runtime, p, database) -> Any:
    froms, to, bump, refresh, ends = FENCED[op]
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        payload = body.model_dump(exclude_none=True)
        if "refs" in payload:
            payload["refs"] = [r.model_dump() for r in body.refs]
        if payload.get("handoff_fields") is not None:
            payload["handoff_fields"] = {k: v for k, v in payload["handoff_fields"].items() if v is not None}
        replay = _recovery_and_replay(tx, recovery, key, f"POST /sessions/{{s}}/tasks/{{t}}/{op}", task, payload)
        if replay is not None:
            return replay
        coordinator_release = op == "release" and body.fence is None
        if coordinator_release:
            _human_coordinator(tx)
        else:
            _check_fence(c, task, p, body.fence)
        _state(tx, "task.progress" if op in ("note", "check", "block", "unblock") else "task.finish")
        if task["status"] not in froms:
            raise ApiError(409, "invalid_transition", f"Cannot {op} a task that is {task['status']}.")
        if op == "submit" and (not any(ch.isalnum() for ch in body.summary)
                               or body.summary.strip().casefold() in {"n/a", "none", "tbd", "todo"}):
            raise ApiError(422, "summary_required", "Describe what was delivered and where to find the evidence.")
        items, waited = checklist_of(task), None
        if op == "submit":
            incomplete = [item["id"] for item in items if not item["done"]]
            if incomplete and body.incomplete_checklist_reason is None:
                raise ApiError(409, "checklist_incomplete", "Complete the task checklist before submitting.",
                               hint="Tick the remaining items, or record why an incomplete submission is necessary.",
                               details={"unchecked_items": incomplete})
        if op == "check":
            item = next((i for i in items if i["id"] == body.item), None)
            if item is None:
                raise ApiError(422, "checklist_item_unknown", "This task has no such checklist item.",
                               details={"items": len(items)})
            item["done"] = body.done
        if op == "block" and body.waiting_on is not None:
            waited = _waiting_on(c, sid, body.waiting_on)
        new_status = to or task["status"]
        gen = task["gen"] + (1 if bump else 0)
        version = task["version"] + (1 if new_status != task["status"] or bump else 0)
        cols: dict = {"status": new_status, "gen": gen, "version": version}
        if refresh:
            cols["last_activity_at"] = db.utcnow()
        if ends:
            cols["fence_secret"] = None
        if op == "release":
            cols.update(claimant_id=None, claimant_runtime=None)
        if op == "submit":
            cols["summary"] = body.summary
        if op == "check":
            cols["checklist"] = json.dumps(items)
        if new_status != "blocked":
            cols["waiting_on"] = None
        elif op == "block":
            cols["waiting_on"] = json.dumps(waited) if waited else None
        _update(c, sid, task["num"], **cols)
        data: dict = {"gen": gen}
        text_body = None
        if op == "note":
            text_body = body.note
            if body.effort_min:
                data["effort_min"] = body.effort_min
        elif op in ("block", "unblock"):
            data["reason"] = body.reason
            if waited:
                data["waiting_on"] = waited["label"]
        elif op == "check":
            data.update(item=item["id"], done=item["done"], progress=f"{sum(1 for i in items if i['done'])}/{len(items)}")
            data["title"] = item["text"]
        elif op == "submit":
            data["summary"] = body.summary
            if body.refs:
                data["refs"] = [r.model_dump(exclude_none=True) for r in body.refs]
            if body.commit:
                data["commit"] = body.commit
            if body.tests:
                data["tests"] = [item.model_dump(exclude_none=True) for item in body.tests]
            if body.incomplete_checklist_reason:
                data["incomplete_checklist_reason"] = body.incomplete_checklist_reason
        elif op == "release":
            data["handoff"] = body.handoff
        if op in ("submit", "release") and body.handoff_fields is not None:
            fields = body.handoff_fields.model_dump(exclude_none=True)
            if fields:
                data["handoff_fields"] = fields
        _, text = tx.event(EVENT[op], task_num=task["num"], body=text_body, data=data, claimant=task["claimant_id"],
                           waited=waited["ref"] if waited and waited["kind"] == "member" else None,
                           receipt=lambda seq: _result(task["num"], new_status, gen, version, seq))
    return respond(text, 200)


def _waiting_on(c, sid: str, ref: str) -> dict:
    """Resolve `#12` / `T3` / `@alice` against this session; unknown targets are refused (not echoed)."""
    m = WAITING_RE.match(ref)
    bad = ApiError(422, "invalid_request", "The request failed validation.",
                   hint="waiting_on is #<question seq>, T<task number> or @<member handle> in this session.",
                   details={"fields": [{"loc": ["body", "waiting_on"], "type": "unknown_target"}]})
    if m is None:
        raise bad
    if m["seq"]:
        seq = int(m["seq"])
        if not c.execute("SELECT 1 FROM event WHERE session_id=? AND seq=? AND type IN ('question','approval')",
                         (sid, seq)).fetchone():
            raise bad
        return {"kind": "question", "ref": seq, "label": f"#{seq}"}
    if m["task"]:
        num = int(m["task"])
        if not c.execute("SELECT 1 FROM task WHERE session_id=? AND num=?", (sid, num)).fetchone():
            raise bad
        return {"kind": "task", "ref": num, "label": f"T{num}"}
    row = c.execute("SELECT p.id, p.handle FROM principal p JOIN member m ON m.principal_id=p.id "
                    "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (m["handle"], sid)).fetchone()
    if row is None:
        raise bad
    return {"kind": "member", "ref": row["id"], "label": f"@{row['handle']}"}


def _fenced_route(op: str, model):
    def route(s: str, t: str, body: model, key: str = Depends(idempotency_key),           # type: ignore[valid-type]
              recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
              runtime: str | None = Depends(runtime_label),
              p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
        return _fenced(op, s, t, body, key, recovery, runtime, p, database)
    route.__name__ = f"task_{op}"
    # This module uses postponed annotations; give FastAPI the class itself, not the string "model".
    route.__annotations__["body"] = model
    router.post(f"/sessions/{{s}}/tasks/{{t}}/{op}")(route)


for _op, _model in (("note", NoteIn), ("check", CheckIn), ("block", ReasonIn), ("unblock", UnblockIn), ("submit", SubmitIn),
                    ("release", ReleaseIn)):
    _fenced_route(_op, _model)


# --- peer review ---------------------------------------------------------------------------------------

class ReviewIn(StrictModel):
    verdict: Literal["approve", "changes"]
    submission_seq: StrictInt = Field(ge=1)
    commit: str | None = Field(default=None, pattern=COMMIT_RE)
    notes: str | None = Field(default=None, min_length=1, max_length=16 * 1024)


@router.post("/sessions/{s}/tasks/{t}/review")
def review_task(s: str, t: str, body: ReviewIn, key: str = Depends(idempotency_key),
                recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                runtime: str | None = Depends(runtime_label),
                p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """The reviewer a human named gives a verdict on submitted work. Advice only: accepting or returning
    the task stays with a human coordinator."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        replay = _recovery_and_replay(tx, recovery, key, "POST /sessions/{s}/tasks/{t}/review", task,
                                      body.model_dump(exclude_none=True))
        if replay is not None:
            return replay
        if task["reviewer_id"] != p.id:
            raise ApiError(403, "forbidden", "Only the reviewer named on this task may review it.")
        _state(tx, "task.finish")
        if task["status"] != "review":
            raise ApiError(409, "invalid_transition", f"Cannot review a task that is {task['status']}.")
        submitted = latest_submission(c, sid, task["num"])
        if submitted is None or body.submission_seq != submitted["seq"]:
            raise ApiError(409, "submission_mismatch", "The review does not name the current submission.",
                           hint="Reload the task and review its latest task.submitted event.",
                           details={"current_submission_seq": submitted["seq"] if submitted else None})
        if body.commit != submitted["commit"]:
            raise ApiError(409, "submission_mismatch", "The review does not name the submitted Git commit.",
                           hint="Use the exact full commit recorded on the current submission.",
                           details={"current_submission_seq": submitted["seq"], "commit": submitted["commit"]})
        review_data = {"verdict": body.verdict, "submission_seq": submitted["seq"],
                       **({"commit": submitted["commit"]} if submitted["commit"] else {})}
        _, text = tx.event("task.reviewed", task_num=task["num"], body=body.notes, data=review_data,
                           claimant=task["claimant_id"],
                           receipt=lambda seq: _result(task["num"], task["status"], task["gen"], task["version"], seq,
                                                       verdict=body.verdict))
    return respond(text, 200)


# --- coordinator operations ------------------------------------------------------------------------

class CoordIn(StrictModel):
    if_version: StrictInt | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=16 * 1024)


class ReturnIn(StrictModel):
    if_version: StrictInt | None = Field(default=None, ge=1)
    submission_seq: StrictInt | None = Field(default=None, ge=1)
    feedback: str = Field(min_length=1, max_length=16 * 1024)          # required on return


class ReassignIn(StrictModel):
    if_version: StrictInt | None = Field(default=None, ge=1)
    to: str = Field(pattern=PRINCIPAL_HANDLE_RE.pattern)
    reason: str | None = Field(default=None, min_length=1, max_length=16 * 1024)


def _coord(op: str, s: str, t: str, body, key, recovery, runtime, p, database) -> Any:
    froms, to, bump = COORD[op]
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        task = _load(c, sid, _num(t))
        target = _member_principal(c, sid, body.to) if op == "reassign" else None
        payload = body.model_dump(exclude_none=True)
        if target is not None:
            payload["to"] = target["id"]
        replay = _recovery_and_replay(tx, recovery, key, f"POST /sessions/{{s}}/tasks/{{t}}/{op}", task, payload)
        if replay is not None:
            return replay
        agent_outcome = False
        if op == "reassign":
            _work_divider(tx)
        else:
            agent_outcome = _outcome_coordinator(tx, task)
        _state(tx, "task.coord")
        if task["status"] not in froms:
            raise ApiError(409, "invalid_transition", f"Cannot {op} a task that is {task['status']}.")
        if body.if_version is None:
            raise ApiError(409, "version_required", "Name the task version you are changing.",
                           hint="Reload the task and retry with its current version.",
                           details={"current_version": task["version"]})
        _check_version(task, body.if_version)
        if op == "return":
            submitted = latest_submission(c, sid, task["num"])
            if submitted is None or body.submission_seq != submitted["seq"]:
                raise ApiError(409, "submission_mismatch", "The return does not name the current submission.",
                               hint="Reload the task and return its latest task.submitted event.",
                               details={"current_submission_seq": submitted["seq"] if submitted else None})
        if agent_outcome and op == "accept":
            review = review_view(c, sid, task)["review"]
            if (task["reviewer_id"] is None or task["reviewer_id"] == task["claimant_id"]
                    or review is None or review["verdict"] != "approve"):
                raise ApiError(409, "peer_review_required",
                               "An independent named reviewer must approve this child task before an agent accepts it.")
        if target is not None and target["role"] == "observer":
            raise ApiError(409, "invalid_transition", "Observers cannot be assigned tasks.")
        gen, version = task["gen"] + (1 if bump else 0), task["version"] + 1
        cols: dict = {"status": to, "gen": gen, "version": version}
        previous_claimant = task["claimant_id"]
        new_assignee = task["assignee_id"]
        if op in ("reassign", "cancel"):
            cols.update(fence_secret=None, claimant_id=None, claimant_runtime=None)
        if op == "reassign":
            new_assignee = cols["assignee_id"] = target["id"]
        if op == "return":   # back to open, assigned to the previous claimant
            new_assignee = cols["assignee_id"] = previous_claimant
            cols.update(claimant_id=None, claimant_runtime=None)
        if op == "reopen":
            cols.update(claimant_id=None, claimant_runtime=None)
        _update(c, sid, task["num"], **cols)
        data: dict = {"gen": gen}
        if op == "return":
            data["feedback"] = body.feedback
            data["submission_seq"] = body.submission_seq
        elif body.reason:
            data["reason"] = body.reason
        if op == "reassign":
            data["to"] = target["handle"]
        extra = {}
        kids = children_of(c, sid, task["num"]) if op == "accept" else None
        if kids and kids["done"] < kids["total"]:          # allowed: humans decide; the receipt says so
            extra["warning"] = f"{kids['total'] - kids['done']} child task(s) not done"
        _, text = tx.event(EVENT[op], task_num=task["num"], data=data, claimant=previous_claimant,
                           previous_claimant=previous_claimant, assignee=new_assignee,
                           receipt=lambda seq: _result(task["num"], to, gen, version, seq, **extra))
    return respond(text, 200)


def _coord_route(op: str, model):
    def route(s: str, t: str, body: model, key: str = Depends(idempotency_key),           # type: ignore[valid-type]
              recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
              runtime: str | None = Depends(runtime_label),
              p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
        return _coord(op, s, t, body, key, recovery, runtime, p, database)
    route.__name__ = f"task_{op}"
    # This module uses postponed annotations; give FastAPI the class itself, not the string "model".
    route.__annotations__["body"] = model
    router.post(f"/sessions/{{s}}/tasks/{{t}}/{op}")(route)


for _op, _model in (("accept", CoordIn), ("return", ReturnIn), ("reassign", ReassignIn), ("cancel", CoordIn),
                    ("reopen", CoordIn)):
    _coord_route(_op, _model)


def coordinator_op(op: str, database: db.Database, p: tokens.Principal, s: str, t: str, body, key: str, recovery):
    """accept / return / reassign / cancel / reopen, or a fence-less coordinator release, for the
    dashboard: the same code paths as the API routes."""
    if op == "release":
        return _fenced("release", s, t, body, key, recovery, None, p, database)
    return _coord(op, s, t, body, key, recovery, None, p, database)
