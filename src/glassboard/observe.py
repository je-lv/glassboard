"""Event-backed Observe model. Never reads current task state into past snapshots."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from . import inbox

STATUS = {
    "task.created": "open", "task.claimed": "in_progress",
    "task.blocked": "blocked", "task.unblocked": "in_progress", "task.submitted": "review",
    "task.released": "open", "task.accepted": "done", "task.returned": "open",
    "task.reassigned": "open", "task.reopened": "open", "task.cancelled": "cancelled",
}
BOOKMARKS = {"task.submitted", "task.reviewed", "task.returned", "task.accepted", "task.blocked",
             "task.stale", "blocker", "decision", "approval.granted", "approval.denied",
             "session.paused", "session.resumed", "task.reassigned"}
MECHANICS = {"lease.taken", "lease.released", "lease.expired", "consumer.attached", "consumer.takeover"}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _task_event(tasks: dict[int, dict], event: dict) -> None:
    n, kind, data = event["task"], event["type"], event["data"] or {}
    if n is None:
        return
    if kind == "task.created":
        tasks[n] = {"num": n, "title": data.get("title") or f"Task T{n}",
                    "status": "open", "assignee": data.get("assignee"), "claimant": None,
                    "reviewer": data.get("reviewer"), "created_seq": event["seq"],
                    "status_seq": event["seq"], "status_since": event["ts"],
                    "claims": 0, "returns": 0, "review": None, "summary": None}
        return
    if n not in tasks:
        return
    task = tasks[n]
    if kind == "task.edited":
        for field in ("title", "reviewer"):
            if field in data:
                task[field] = None if field == "reviewer" and data[field] == "none" else data[field]
    if kind in STATUS:
        task["status"] = STATUS[kind]
        task["status_seq"] = event["seq"]
        task["status_since"] = event["ts"]
    if kind == "task.claimed":
        task["claimant"] = event["actor"]
        task["claims"] += 1
    elif kind == "task.reclaimed":
        # Reclaim rotates a fence and leaves both the claimant and status as they were.
        # A coordinator may perform it on the claimant's behalf.
        task["claims"] += 1
    elif kind in ("task.reassigned", "task.released", "task.cancelled", "task.reopened"):
        task["claimant"] = None
        if kind == "task.reassigned":
            task["assignee"] = data.get("to") or data.get("assignee") or (event["to"][0] if isinstance(event["to"], list) and event["to"] else None)
    elif kind == "task.returned":
        task["returns"] += 1
        task["assignee"] = task["claimant"]
        task["claimant"] = None
    elif kind == "task.submitted":
        task["summary"] = data.get("summary")
        task["review"] = None
    elif kind == "task.reviewed":
        task["review"] = {"seq": event["seq"], "by": event["actor"], "verdict": data.get("verdict")}


def replay(events: list[dict], at: int) -> dict:
    tasks: dict[int, dict] = {}
    before: dict | None = None
    selected = None
    open_blockers: dict[int, dict] = {}
    for event in events:
        if event["seq"] > at:
            break
        if event["seq"] == at:
            before = deepcopy(tasks.get(event["task"])) if event["task"] is not None else None
            selected = event
        _task_event(tasks, event)
        if event["type"] == "blocker" and isinstance(event["to"], list):
            open_blockers[event["seq"]] = event
        if event.get("re"):
            open_blockers.pop(event.get("re"), None)
        if event["type"] in ("task.returned", "task.accepted", "task.cancelled", "task.reopened"):
            for seq, blocker in list(open_blockers.items()):
                if blocker["task"] == event["task"]:
                    del open_blockers[seq]
    after = tasks.get(selected["task"]) if selected and selected["task"] is not None else None
    changes = []
    if selected and selected["task"] is not None:
        if before is None and after is not None:
            changes.append(f"T{after['num']} created · open")
        elif before is not None and after is not None:
            for field, label in (("status", "Status"), ("assignee", "Assignee"),
                                 ("claimant", "Claimant"), ("reviewer", "Reviewer")):
                if before[field] != after[field]:
                    changes.append(f"T{after['num']} {label.lower()}: {before[field] or 'none'} → {after[field] or 'none'}")
    if selected and not changes:
        changes.append("No task status change recorded at this event.")
    pending_reviews = [t for t in tasks.values() if t["status"] == "review" and t["reviewer"] and not t["review"]]
    blockers = [b for b in open_blockers.values() if any("/" not in h for h in b["to"])]
    return {"tasks": sorted(tasks.values(), key=lambda t: t["num"]), "selected": selected,
            "changes": changes, "pending_reviews": pending_reviews, "human_blockers": blockers,
            "at_ts": selected["ts"] if selected else None}


def strip(events: list[dict], at: int) -> dict:
    if not events:
        return {"height": 75, "lanes": [], "marks": [], "bookmarks": [], "start": None, "end": None}
    actors = list(dict.fromkeys(e["actor"] for e in events))
    actors.sort(key=lambda a: ("/" in a, a))
    named_colors = ("claude", "codex", "grok", "glm")
    fallback_colors = ("rose", "gold", "teal", "lime", "coral", "indigo")
    used: set[str] = set()

    def actor_color(handle: str) -> str:
        if "/" not in handle:
            return "human"
        leaf = handle.rsplit("/", 1)[-1].lower()
        for color in named_colors:
            if color in leaf and color not in used:
                used.add(color)
                return color
        for color in fallback_colors:
            if color not in used:
                used.add(color)
                return color
        return fallback_colors[sum(handle.encode("utf-8")) % len(fallback_colors)]

    lanes = [{"handle": a, "y": 41 + i * 35, "human": "/" not in a,
              "color": actor_color(a)} for i, a in enumerate(actors)]
    ymap = {x["handle"]: x["y"] for x in lanes}
    colors = {x["handle"]: x["color"] for x in lanes}
    start, end = _time(events[0]["ts"]), _time(events[-1]["ts"])
    total = max(1.0, (end-start).total_seconds())
    marks, bookmarks = [], []
    for e in events:
        if e["type"] in MECHANICS and e["seq"] != at:
            continue
        x = 130 + 740 * ((_time(e["ts"]) - start).total_seconds() / total)
        key = e["type"] in BOOKMARKS
        mark = {"seq": e["seq"], "x": round(x, 2), "y": ymap[e["actor"]],
                "actor": e["actor"], "type": e["type"], "ts": e["ts"],
                "key": key, "selected": e["seq"] == at, "future": e["seq"] > at,
                "color": colors[e["actor"]],
                "human": "/" not in e["actor"]}
        marks.append(mark)
        if key:
            bookmarks.append(mark)
    cursor = next((m["x"] for m in marks if m["selected"]), None)
    return {"height": 70 + len(lanes) * 35, "lanes": lanes, "marks": marks, "cursor_x": cursor,
            "bookmarks": bookmarks, "start": events[0]["ts"], "end": events[-1]["ts"]}


def model(c, sess, at: int) -> dict:
    rows = c.execute("SELECT * FROM event WHERE session_id=? ORDER BY seq", (sess["id"],)).fetchall()
    events = inbox.views(c, rows)
    state = replay(events, at)
    selected = state["selected"]
    state["selected_lines"] = [{"text": line, "cls": "hdr" if i == 0 else "blk"}
                               for i, line in enumerate(render_event(selected))] if selected else []
    state["strip"] = strip(events, at)
    state["previous"] = at - 1 if at > 1 else None
    state["next"] = at + 1 if at < sess["head_seq"] else None
    state["bookmarks"] = state["strip"]["bookmarks"]
    state["head"] = sess["head_seq"]
    state["previous_bookmark"] = next((b["seq"] for b in reversed(state["bookmarks"]) if b["seq"] < at), None)
    state["next_bookmark"] = next((b["seq"] for b in state["bookmarks"] if b["seq"] > at), None)
    if state["at_ts"]:
        for task in state["tasks"]:
            task["status_minutes"] = max(0, int((_time(state["at_ts"]) - _time(task["status_since"])).total_seconds() / 60))
    return state


def render_event(event: dict) -> list[str]:
    if event is None:
        return []
    from .textviews import view_lines
    return view_lines(event)
