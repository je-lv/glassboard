"""Canonical text views built from JSON views: one event, one sync response.

Stdlib only, so the participant CLI renders exactly the text the server would (one rendering
for humans and agents). All lines come from render.py.
"""
from __future__ import annotations

from . import render


def view_lines(v: dict) -> list[str]:
    """Canonical text of one event view (render.event_lines applies every text-rendering rule)."""
    data = dict(v["data"] or {})
    if v["refs"]:
        data["refs"] = v["refs"]
    return render.event_lines({"seq": v["seq"], "ts": v["ts"], "actor": v["actor"], "runtime": v["runtime"],
                               "type": v["type"], "broadcast": v["to"] == "all",
                               "recipients": [] if v["to"] == "all" else v["to"], "task_num": v["task"],
                               "reply_to": v["re"], "body": v["body"], "data": data})


def sync_text(r: dict, runtime: str | None) -> str:
    """Sync text rendering; every line is a Section, Header, Block, Inline or Ref line."""
    me = r["me"] + (f"@{runtime}" if runtime else "")
    lines = [render.section(f"@{me} · {r['session']} ({r['state']}, epoch {r['epoch']}, recovery {r['recovery_id']}) "
                            f"· consumer {r['consumer']} · acked #{r['cursor']} · delivered #{r['delivered_through']}"),
             render.NOTE_LINE,
             render.section("claims: " + (", ".join(_claim(x) for x in r["claims"]) or "none"))]
    for o in r["owed"]:
        what = "approval request" if o.get("type") == "approval" else "question"
        lines.append(render.section(f"owed: #{o['seq']} {what} from {o['from']} (unresolved, {o['age_s'] // 60}m)"))
    if r["owed_total"] > len(r["owed"]):
        lines.append(render.section(f"owed: … {r['owed_total'] - len(r['owed'])} more"))
    cp, n = r.get("checkpoint"), r.get("checkpoint_n")
    if cp:
        lines.append(render.section(f"checkpoint: #{cp['n']} saved {cp['saved_at'][11:16]}Z (included in full)"))
        lines.extend(checkpoint_lines(cp))
    elif n:
        lines.append(render.section(f"checkpoint: #{n} saved; `glassboard checkpoint show` for details"))
    lines.append(render.section(f"inbox: {len(r['inbox'])} new" + (" (more waiting)" if r["more"] else "")))
    for v in r["inbox"]:
        lines.extend(view_lines(v))
    if r["inbox"]:
        lines.append(render.section(f"next: after processing, `glassboard sync --ack {r['delivered_through']}`"))
    return "\n".join(lines)


def _claim(x: dict) -> str:
    """`T7 in_progress g3 (activity 4m ago)`, `stale` added when it is."""
    age = x.get("activity_age_s")
    act = f" (activity {age // 60}m ago{', stale' if x.get('stale') else ''})" if age is not None else ""
    prog = f" {x['progress']['done']}/{x['progress']['total']}" if x.get("progress") else ""
    wait = f" waiting on {_waiting(x['waiting_on'])}" if x.get("waiting_on") else ""
    return f"T{x['task']} {x['status']} g{x['gen']}{prog}{wait}{act}"


def checkpoint_lines(cp: dict) -> list[str]:
    """Canonical lines for an included checkpoint, under `checkpoint:` in sync text."""
    lines = [render.inline_line("summary", cp["summary"]), render.inline_line("next_action", cp["next_action"])]
    if cp["blockers"]:
        lines.append(render.inline_line("blockers", cp["blockers"]))
    if cp["task_nums"]:
        lines.append(render.inline_line("tasks", ", ".join(f"T{n}" for n in cp["task_nums"])))
    lines += [render.ref_line(r) for r in cp["refs"]]
    lines += render.block_lines(cp["body"] or "")
    if cp["truncated"]:
        lines.append(render.inline_line("truncated", "body cut at 4 KiB; see `glassboard checkpoint show`"))
    return lines


def task_head(v: dict) -> str:
    """The Section line that heads one task (task page, task list, MCP `board_tasks`)."""
    return render.section(f"--- T{v['task']} · {v['status']} · g{v['gen']} · v{v['version']}"
                          + (f" · claimant {v['claimant']}" if v["claimant"] else "")
                          + (f" · assignee {v['assignee']}" if v["assignee"] else "")
                          + (f" · assignment {v['assignment_stage']}" if v.get("assignment_stage") in ("assigned", "acknowledged", "working") else "")
                          + (" · acknowledgment overdue" if v.get("ack_overdue") else "")
                          + (f" · {v['priority']}" if v.get("priority") not in (None, "normal") else "")
                          + (f" · {v['progress']['done']}/{v['progress']['total']} done" if v.get("progress") else "")
                          + (f" · children {v['children']['done']}/{v['children']['total']} done" if v.get("children") else "")
                          + (f" · parent T{v['parent']}" if v.get("parent") else "")
                          + (f" · reviewer {v['reviewer']}" if v.get("reviewer") else "")
                          + (f" · submission #{v['submission']['seq']}" if v.get("submission") else "")
                          + (f" · review: {v['review']['verdict']}" if v.get("review") else "")
                          + (" · needs " + ", ".join(f"T{d['task']}" for d in v["depends_on"] if d["status"] != "done")
                             if v.get("depends_on") and not v.get("ready", True) else "")
                          + (f" · waiting on {_waiting(v['waiting_on'])}" if v.get("waiting_on") else "")
                          + (" · stale" if v["stale"] else "") + " ---")


def _waiting(w: dict) -> str:
    """`#12 (resolved)`, `T3 (open)`, `@alice`. Labels are server-built from validated values."""
    state = "" if w.get("resolved") is None else (" (resolved)" if w["resolved"] else " (open)")
    return f"{w['label']}{state}"


def checklist_lines(v: dict) -> list[str]:
    return [render.inline_line("done" if i["done"] else "todo", f"{i['id']}. {i['text']}") for i in v.get("checklist") or ()]


def tasks_text(r: dict, heading: str = "") -> str:
    """GET /tasks?format=text, `glassboard task list`, MCP `board_tasks`; `/tasks/next` with a heading."""
    lines = [render.section(f"--- {len(r['tasks'])} task(s) {heading + ' ' if heading else ''}in {r['session']} ---")]
    for v in r["tasks"]:
        lines += [task_head(v), render.inline_line("title", v["title"])]
    return "\n".join(lines) + "\n"


def checkpoint_text(cp: dict) -> str:
    """GET /checkpoints?format=text, `glassboard checkpoint show`, MCP `board_checkpoint show`."""
    lines = [render.section(f"checkpoint: #{cp['n']} of @{cp['principal']} saved {cp['saved_at'][:16]}Z "
                            f"· seen #{cp['last_seen_seq']}"), *checkpoint_lines(cp)]
    return "\n".join(lines) + "\n"


def whoami_text(w: dict) -> str:
    """GET /whoami?format=text, `glassboard whoami`/`login`, MCP `board_whoami`. One Section line per
    session with its current recovery ID (server values only; the title is participant text)."""
    owner = f", owner {w['owner']}" if w["owner"] else ""
    lines = [render.section(f"@{w['handle']} ({w['kind']}{owner}) · scope {w['scope']} · credential ID {w['credential_id']}")]
    for s in w["sessions"]:
        lines.append(render.section(f"--- session {s['session']} · {s['state']} · {s['role']} · epoch {s['epoch']} "
                                    f"· recovery {s['recovery_id']} ---"))
    return "\n".join(lines) + "\n"
