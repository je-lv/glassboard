"""B58 (ERRATA E46): the Chat view, a read-only projection of one session log for a human who watches
the agents talk and steps in from the same page.

Nothing here writes: the composer posts through the dashboard Post form (`POST /s/{s}/post`), so every
rule of a post (CSRF, recovery ID, idempotency, validation, preflight) is the one the Timeline uses.
Message text reaches the page only through the canonical rendering (`event_card`: R2/R3 applied);
task and system events are folded into one-line markers that link to the Timeline, where every event
stays visible."""
from __future__ import annotations

from . import inbox, render

# Events people write to each other: shown as chat messages. Everything else becomes a marker.
MESSAGE_TYPES = ("question", "answer", "update", "proposal", "blocker", "note", "decision", "approval",
                 "question.resolved", "approval.granted", "approval.denied",
                 "hold", "hold.decided", "hold.expired", "standdown.requested", "standdown.done", "standdown.lifted")
# Messages that can leave somebody owing an answer (inbox.obligation_holders knows how each one closes).
OWED_TYPES = ("question", "approval", "hold", "standdown.requested")
# Bookkeeping that a marker counts but does not name.
QUIET = ("consumer.", "wake.", "context.", "runtime.", "lease.", "checkpoint.")
LIMIT = 200               # newest messages on one page
OLD_OWED_LIMIT = 200      # older still-owed requests kept in view
MARKER_LABELS = 3
CHAT_SKIP_LINES = (render.INLINE + "channel:", render.INLINE + "had read through:")


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _first_line(body: str | None) -> str:
    first = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    return render.inline(first, 90)[1:-1] if first else ""


def _marker_label(row, handles: dict) -> str:
    who = handles.get(row["actor_id"], "?")
    task = f"T{row['task_num']} " if row["task_num"] is not None else ""
    return f"{task}{row['type']} by @{who}"


def stream(c, sess, viewer, me, channel: str | None = None, show_events: bool = True) -> dict:
    """The page model: `items` oldest first (messages and markers), plus whether older messages exist."""
    from .dashboard import event_card
    sid = sess["id"]
    types = _marks(len(MESSAGE_TYPES))
    where, args = f"session_id=? AND type IN ({types})", [sid, *MESSAGE_TYPES]
    if channel:
        where += " AND channel=?"; args.append(channel)
    rows = c.execute(f"SELECT * FROM event WHERE {where} ORDER BY seq DESC LIMIT {LIMIT + 1}", args).fetchall()
    more = len(rows) > LIMIT
    rows = list(reversed(rows[:LIMIT]))
    low = rows[0]["seq"] if rows and more else 0
    # Owed requests stay visible. The whole-session view pins the ones older than this page. A channel view
    # shows only that channel's messages (B67): its own older owed requests are pinned, and open requests from
    # elsewhere fold into one line that names the ones the viewer owes.
    shown = {r["seq"] for r in rows}
    cand_where, cand_args = f"session_id=? AND type IN ({_marks(len(OWED_TYPES))})", [sid, *OWED_TYPES]
    if not channel:
        cand_where += " AND seq < ?"; cand_args.append(low)
    extra, elsewhere_rows = [], []
    for r in c.execute(f"SELECT * FROM event WHERE {cand_where} ORDER BY seq DESC LIMIT {OLD_OWED_LIMIT}", cand_args):
        if r["seq"] in shown:
            continue
        holders = inbox.obligation_holders(c, sid, r["seq"])
        if not holders:
            continue
        if channel and r["channel"] != channel:
            elsewhere_rows.append((r, holders))
        else:
            extra.append(r)
    rows = sorted(rows + extra, key=lambda r: r["seq"])
    pinned = {r["seq"] for r in extra}
    elsewhere = None
    if elsewhere_rows:
        mine = []
        for r, holders in sorted(elsewhere_rows, key=lambda x: x[0]["seq"]):
            if viewer.p.id in holders:
                v = inbox.views(c, [r])[0]
                mine.append({"seq": v["seq"], "actor": v["actor"], "type": v["type"], "channel": v["channel"],
                             "text": _first_line(v["body"])})
        elsewhere = {"count": len(elsewhere_rows), "mine": mine}

    views = inbox.views(c, rows)
    actor_ids = {r["actor_id"] for r in rows}
    people = {}
    if actor_ids:
        for p in c.execute(f"SELECT id, handle, kind, owner_id FROM principal WHERE id IN ({_marks(len(actor_ids))})",
                           list(actor_ids)):
            people[p["handle"]] = p
    # Quotes of the messages being replied to (they may be older than this page).
    refs = sorted({v["re"] for v in views if v["re"]})
    quotes = {}
    if refs:
        for r in inbox.views(c, c.execute(f"SELECT * FROM event WHERE session_id=? AND seq IN ({_marks(len(refs))})",
                                          [sid, *refs]).fetchall()):
            quotes[r["seq"]] = {"seq": r["seq"], "actor": r["actor"], "type": r["type"], "text": _first_line(r["body"])}
    is_human = viewer.p.kind == "human"
    coordinator = me["role"] == "coordinator"
    on_page = {v["seq"] for v in views}
    for q in quotes.values():
        q["on_page"] = q["seq"] in on_page
    messages = []
    for v, row in zip(views, rows):
        card = event_card(v)
        # The channel shows as a tag; the canonical channel line would repeat it. The author's read
        # position (B54) stays on the Timeline card [P]: in a chat it reads as clutter on every message.
        card["body"] = [b for b in card["body"]
                        if not (b["kind"] == "line" and b["text"].startswith(CHAT_SKIP_LINES))]
        holders = []
        if v["type"] in OWED_TYPES:
            ids = inbox.obligation_holders(c, sid, v["seq"])
            if ids:
                holders = sorted(inbox.handle_map(c, ids).values())
        author = people.get(v["actor"])
        agent = author is not None and author["kind"] == "agent"
        messages.append({
            "seq": v["seq"], "card": card, "type": v["type"], "actor": v["actor"], "channel": v["channel"],
            "re": v["re"], "quote": quotes.get(v["re"]) if v["re"] else None,
            "owed": v["type"] in OWED_TYPES, "holders": holders, "i_owe": viewer.p.handle in holders,
            "pinned": v["seq"] in pinned,
            "other_channel": bool(channel) and v["channel"] != channel,
            "time": card["head"]["ts"][11:16] if card["head"] else "",
            "agent": agent,
            "may_stand_down": agent and is_human and (coordinator or author["owner_id"] == viewer.p.id),
            "decide_hold": v["type"] == "hold" and viewer.p.handle in holders,
        })

    items = [{"kind": "message", **m} for m in messages]
    if show_events and not channel and messages:
        items = _with_markers(c, sid, items, low)
    return {"items": items, "more": more, "count": len(messages), "elsewhere": elsewhere}


def _with_markers(c, sid: str, items: list[dict], low: int) -> list[dict]:
    """Fold each run of task and system events between two messages into one marker line."""
    types = _marks(len(MESSAGE_TYPES))
    rows = c.execute(f"SELECT seq, type, actor_id, task_num FROM event WHERE session_id=? AND seq > ? "
                     f"AND type NOT IN ({types}) AND type != 'message.preflight' ORDER BY seq",
                     [sid, low, *MESSAGE_TYPES]).fetchall()
    if not rows:
        return items
    handles = inbox.handle_map(c, list({r["actor_id"] for r in rows}))
    out, i = [], 0

    def marker(run):
        named = [r for r in run if not r["type"].startswith(QUIET)]
        labels = [_marker_label(r, handles) for r in named[:MARKER_LABELS]]
        return {"kind": "marker", "first": run[0]["seq"], "last": run[-1]["seq"], "count": len(run),
                "labels": labels, "rest": len(run) - len(labels)}

    for item in items:
        run = []
        while i < len(rows) and rows[i]["seq"] < item["seq"]:
            run.append(rows[i]); i += 1
        if run:
            out.append(marker(run))
        out.append(item)
    if i < len(rows):
        out.append(marker(rows[i:]))
    return out


def reply_target(c, sess, seq: int | None) -> dict | None:
    """The message a no-JavaScript Reply or Ask link points at (`?reply=N`, `?ask=N`)."""
    if seq is None:
        return None
    row = c.execute(f"SELECT * FROM event WHERE session_id=? AND seq=? AND type IN ({_marks(len(MESSAGE_TYPES))})",
                    [sess["id"], seq, *MESSAGE_TYPES]).fetchone()
    if row is None:
        return None
    v = inbox.views(c, [row])[0]
    return {"seq": v["seq"], "actor": v["actor"], "type": v["type"]}
