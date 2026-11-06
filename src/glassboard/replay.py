"""Task replay (ERRATA E44): the recorded events of one task, drawn as a sequence between members.

Columns are the members who acted or were reached; each step is one recorded event: an arrow from its
author to the members its routing reached (event_recipient rows), a dashed line across all lanes for a
broadcast, or a dot on the author's lane when it reached nobody (a claim, a note). Answers and other
replies to the task's events are included. Read-only; nothing is inferred from timing.
"""
from __future__ import annotations

from . import inbox, render

MAX_STEPS = 200
MAX_LANES = 10
SKIP = ("consumer.", "wake.", "context.", "message.preflight", "runtime.", "lease.", "checkpoint.", "channel.mode")
CATEGORY = {
    "task.created": "member", "task.edited": "member", "task.reassigned": "member",
    "task.acknowledged": "progress", "task.claimed": "progress", "task.note": "progress", "task.checked": "progress",
    "task.unblocked": "progress", "task.reclaimed": "progress",
    "task.submitted": "review", "task.reviewed": "review", "task.returned": "review",
    "task.accepted": "done", "task.blocked": "warn", "task.cancelled": "muted", "task.declined": "warn",
    "task.stale": "danger", "task.released": "member", "task.reopened": "member",
    "question": "question", "approval": "question", "hold": "question",
    "answer": "message", "question.resolved": "message", "approval.granted": "done", "approval.denied": "danger",
    "hold.decided": "message", "hold.expired": "message", "decision": "decision", "blocker": "warn",
}
COL, TOP, ROW, LEFT = 160, 64, 40, 100


def _events(c, sid: str, num: int) -> list:
    rows = {r["seq"]: r for r in c.execute("SELECT * FROM event WHERE session_id=? AND task_num=? ORDER BY seq", (sid, num))}
    frontier = set(rows)
    for _ in range(5):                                   # replies to the task's events, a few levels deep
        if not frontier:
            break
        q = ",".join("?" * len(frontier))
        new = {r["seq"]: r for r in c.execute(f"SELECT * FROM event WHERE session_id=? AND reply_to IN ({q})",
                                              (sid, *frontier)) if r["seq"] not in rows}
        rows.update(new)
        frontier = set(new)
    return [rows[s] for s in sorted(rows) if not rows[s]["type"].startswith(SKIP)][:MAX_STEPS]


def replay(c, sid: str, num: int) -> dict:
    views = inbox.views(c, _events(c, sid, num))
    lanes: list[str] = []

    def lane(h: str) -> None:
        if h not in lanes and len(lanes) < MAX_LANES:
            lanes.append(h)
    for v in views:
        lane(v["actor"])
        if v["to"] != "all":
            for h in v["to"]:
                lane(h)
    steps = []
    for i, v in enumerate(views):
        y = TOP + i * ROW
        x0 = LEFT + lanes.index(v["actor"]) * COL if v["actor"] in lanes else LEFT
        targets = [] if v["to"] == "all" else [h for h in v["to"] if h in lanes and h != v["actor"]]
        data = v.get("data") or {}
        first = next((ln for ln in (v.get("body") or "").splitlines() if ln.strip()), "")
        detail = first or data.get("summary") or data.get("feedback") or data.get("reason") or data.get("title") or ""
        steps.append({"n": i + 1, "seq": v["seq"], "ts": v["ts"], "type": v["type"], "actor": v["actor"],
                      "to": v["to"], "cat": CATEGORY.get(v["type"], "system"), "y": y, "x0": x0,
                      "x_targets": [LEFT + lanes.index(h) * COL for h in targets],
                      "broadcast": v["to"] == "all", "self": v["to"] != "all" and not targets,
                      "detail": render.inline(detail, 140) if detail else None})
    width = LEFT + max(len(lanes) - 1, 1) * COL + 190          # room for a label right of the last lane
    height = TOP + max(len(steps), 1) * ROW + 16
    return {"lanes": [{"handle": h, "x": LEFT + i * COL} for i, h in enumerate(lanes)], "steps": steps,
            "width": width, "height": height, "truncated": len(steps) >= MAX_STEPS}
