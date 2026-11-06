"""The session "Now" page and navigation badges (ERRATA E42): what needs the viewer in this session,
ranked, each item with the one action that resolves it. Built only from views the board already
computes (attention items, blockers, task verification); nothing new is recorded."""
from __future__ import annotations

from . import api_blockers, api_board, render
from .api_tasks import review_view, verification_view

TONE_RANK = {"danger": 0, "attention": 1, "review": 2, "info": 3}
# attention kind -> (tone, action label)
KIND = {
    "runtime_exited": ("danger", "Check the agent"), "stale": ("danger", "Check task"), "loop": ("danger", "Read the thread"),
    "approval": ("attention", "Decide"), "hold": ("attention", "Decide"), "owed": ("attention", "Answer"),
    "standdown": ("attention", "Release work, then answer"), "waiting_on_you": ("attention", "Respond"),
    "declined": ("attention", "Reassign or cancel"), "unacknowledged": ("attention", "Check task"),
    "wake_held": ("attention", "Check the agent"),
    "review": ("review", "Review"), "review_requested": ("review", "Review"),
    "proposal": ("info", "Create tasks"),
}


def _href(sh: str, i: dict) -> str:
    if i.get("task"):
        return f"/s/{sh}/t/{i['task']}"
    if i["kind"] in ("approval", "hold"):
        return "/"                                     # decided from the home page forms
    if i["kind"] == "proposal":
        return f"/s/{sh}/new-task?from={i['seq']}"
    if i["kind"] in ("runtime_exited", "wake_held") and i.get("from"):
        return f"/s/{sh}/agent?as={i['from']}"
    return f"/s/{sh}?f=all#e{i['seq']}"


def items(c, sess, p) -> list[dict]:
    sh = sess["handle"]
    out: list[dict] = []
    for i in api_board.attention(c, p)["items"]:
        if i["session"] != sh:
            continue
        tone, action = KIND.get(i["kind"], ("info", "Open"))
        where = f"T{i['task']}" if i.get("task") else f"#{i['seq']}"
        title = f"{api_board._ATTN[i['kind']].capitalize()} · {where}" + (f" from @{i['from']}" if i.get("from") else "")
        detail = i.get("title") or i.get("text") or ""
        out.append({"tone": tone, "title": title, "detail": render.inline(detail, 140) if detail else None,
                    "action": action, "href": _href(sh, i), "age_s": i.get("age_s", 0)})
    for b in api_blockers.blockers(c, sess)["blockers"][:3]:
        if not b["holds"]:
            continue
        if b["kind"] in ("question", "approval"):
            title, href = f"#{b['seq']} {b['kind']} holds up {len(b['holds'])} task(s)", f"/s/{sh}?f=all#e{b['seq']}"
        elif b["kind"] == "member":
            title, href = f"@{b['handle']} is waited on by {len(b['holds'])} task(s)", f"/s/{sh}/agent?as={b['handle']}"
        else:
            title, href = f"T{b['task']} holds up {len(b['holds'])} task(s)", f"/s/{sh}/t/{b['task']}"
        out.append({"tone": "attention", "title": title, "detail": "Blocks " + ", ".join(f"T{h['task']}" for h in b["holds"]),
                    "action": "Unblock", "href": href, "age_s": 0})
    for t in c.execute("SELECT * FROM task WHERE session_id=? AND status='review' ORDER BY num", (sess["id"],)):
        v = verification_view(c, sess["id"], t, review_view(c, sess["id"], t))
        gaps = [g for g in (v or {}).get("gaps", []) if not g.startswith("waiting for")]
        if gaps:
            out.append({"tone": "review", "title": f"T{t['num']} in review with gaps", "detail": "; ".join(gaps),
                        "action": "Check evidence", "href": f"/s/{sh}/t/{t['num']}", "age_s": 0})
    out.sort(key=lambda x: (TONE_RANK[x["tone"]], -x["age_s"]))
    return out


def badges(c, sess, p) -> dict:
    """Counts shown on the session tabs: Now = items needing you, Board = blocker roots, Conversations =
    questions and requests you owe here."""
    from . import inbox
    _, owed = inbox.owed(c, sess["id"], p.id, show=0)
    return {"now": len(items(c, sess, p)), "board": api_blockers.blockers(c, sess)["count"], "conversations": owed}
