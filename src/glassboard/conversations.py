"""Human-readable conversation projections over the append-only event log.

The canonical timeline remains the audit record.  This module is deliberately a
read-only projection: it groups replies and renders participant-authored fields
without changing or hiding the underlying event sequence.
"""
from __future__ import annotations

import json
import sqlite3

from . import inbox, render

MESSAGE_TYPES = (
    "question", "answer", "update", "proposal", "blocker", "note", "decision", "approval",
    "question.resolved", "approval.granted", "approval.denied",
)
MESSAGE_TYPE_SET = frozenset(MESSAGE_TYPES)
PAGE_SIZE = 200
BODY_PREVIEW_CHARS = 800
BODY_PREVIEW_LINES = 12


def _actor_style(handle: str) -> tuple[str, str, str]:
    agent = handle.rsplit("/", 1)[-1] if "/" in handle else None
    known = {"codex": ("C", "Codex"), "claude": ("Cl", "Claude"), "grok": ("G", "Grok"),
             "glm": ("GL", "GLM")}
    mark, name = known.get(agent, (agent[:2].upper(), agent) if agent else ("H", handle))
    return mark, name, agent if agent in known else ("agent" if agent else "human")


def _data_fields(data: dict | None) -> list[dict]:
    fields = []
    for key, value in (data or {}).items():
        if key == "refs" or value is None:
            continue
        if isinstance(value, str):
            text = render.r2(value, allow_newline=True)
        else:
            text = render.r2(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2), allow_newline=True)
        fields.append({"key": render.r2(str(key)), "value": text})
    return fields


def _body_view(value: str) -> tuple[str, str, bool]:
    body = render.r2(value, allow_newline=True)
    lines = body.splitlines()
    long = len(body) > BODY_PREVIEW_CHARS or len(lines) > BODY_PREVIEW_LINES
    if not long:
        return body, body, False
    preview = "\n".join(lines[:BODY_PREVIEW_LINES])[:BODY_PREVIEW_CHARS].rstrip()
    return body, preview + "…", True


def _item(view: dict, session: str | None = None) -> dict:
    mark, name, style = _actor_style(view["actor"])
    to = [view["to"]] if isinstance(view["to"], str) else list(view["to"] or ())
    body, preview, body_long = _body_view(view.get("body") or "")
    return {
        "seq": view["seq"], "ts": view["ts"], "actor": view["actor"], "runtime": view.get("runtime"),
        "type": view["type"], "to": to, "task": view.get("task"), "re": view.get("re"),
        "body": body, "body_preview": preview, "body_long": body_long,
        "fields": _data_fields(view.get("data")),
        "refs": [{"label": render.r2(str(ref.get("label", ""))),
                  "loc": render.r2(str(ref.get("loc", ""))),
                  "access": render.r2(str(ref.get("access", "")))} for ref in view.get("refs", ())],
        "actor_mark": mark, "actor_name": name, "actor_style": style, "children": [], "depth": 0,
        "session": session,
    }


def _thread_projection(items: list[dict], selected: int | None = None,
                       reply: int | None = None,
                       parents: dict[tuple[str | None, int], int | None] | None = None) -> dict:
    """Group messages by reply root without recursively nesting their HTML."""
    def key(item):
        return item["session"], item["seq"]

    by_key = {key(item): item for item in items}
    parents = parents or {item_key: item["re"] for item_key, item in by_key.items()}
    grouped: dict[tuple[str | None, int], list[dict]] = {}
    for item in items:
        current_key, seen = key(item), set()
        while parents.get(current_key) is not None:
            parent_key = (current_key[0], parents[current_key])
            if parent_key in seen or parent_key not in parents:
                break
            seen.add(parent_key)
            current_key = parent_key
        item["thread"] = current_key[1]
        grouped.setdefault(current_key, []).append(item)

    resolving = {"answer", "question.resolved", "approval.granted", "approval.denied"}
    threads = []
    for (session, root_seq), messages in grouped.items():
        resolved = {message["re"] for message in messages if message["type"] in resolving}
        attention = sum(message["type"] in {"question", "approval"} and message["seq"] not in resolved
                        for message in messages)
        root, latest = messages[0], messages[-1]
        threads.append({"id": root_seq, "session": session, "root": root, "latest": latest,
                        "messages": messages, "count": len(messages),
                        "participants": sorted({message["actor"] for message in messages}),
                        "attention": attention,
                        "task": next((message["task"] for message in reversed(messages) if message["task"]), None),
                        "sort": latest.get("global_id", latest["seq"])})
    threads.sort(key=lambda thread: thread["sort"], reverse=True)
    selected_thread = next((thread for thread in threads if thread["id"] == selected), None)
    selected_messages = selected_thread["messages"] if selected_thread else []
    reply_item = next((item for item in selected_messages if item["seq"] == reply), None)
    return {"threads": threads, "selected_thread": selected_thread,
            "selected_messages": selected_messages, "reply_item": reply_item}


def page(conn: sqlite3.Connection, sess, *, author: str = "", kind: str = "", query: str = "",
         task: int | None = None, include_system: bool = False, before: int | None = None,
         thread: int | None = None, reply: int | None = None, limit: int = PAGE_SIZE) -> dict:
    """Return one newest page in readable order plus a reply tree.

    Filtering is applied before pagination. A reply whose parent falls outside the
    page becomes a visible root but retains its parent sequence link.
    """
    where = ["e.session_id=:s", "e.type != 'message.preflight'"]
    args: dict[str, object] = {"s": sess["id"]}
    if not include_system:
        names = ",".join(f":m{i}" for i, _ in enumerate(MESSAGE_TYPES))
        where.append(f"e.type IN ({names})")
        args.update({f"m{i}": value for i, value in enumerate(MESSAGE_TYPES)})
    if author:
        where.append("p.handle=:author"); args["author"] = author
    if kind:
        where.append("e.type=:kind"); args["kind"] = kind
    if task is not None:
        where.append("e.task_num=:task"); args["task"] = task
    if query:
        where.append("(instr(lower(coalesce(e.body,'')),lower(:q))>0 OR "
                     "instr(lower(coalesce(e.data,'')),lower(:q))>0)")
        args["q"] = query[:200]
    if before is not None:
        where.append("e.seq<:before"); args["before"] = before
    rows = conn.execute(
        f"SELECT e.* FROM event e JOIN principal p ON p.id=e.actor_id WHERE {' AND '.join(where)} "
        "ORDER BY e.seq DESC LIMIT :limit", {**args, "limit": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_item(v) for v in inbox.views(conn, reversed(rows))]
    eligible_where = "" if include_system else " AND type IN ({})".format(
        ",".join("?" for _ in MESSAGE_TYPES))
    eligible_args = (sess["id"], *MESSAGE_TYPES) if not include_system else (sess["id"],)
    parents = {(None, row["seq"]): row["reply_to"] for row in conn.execute(
        "SELECT seq,reply_to FROM event WHERE session_id=? AND type!='message.preflight'" + eligible_where,
        eligible_args)}
    projection = _thread_projection(items, thread, reply, parents)
    authors = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.handle FROM event e JOIN principal p ON p.id=e.actor_id "
        "WHERE e.session_id=? ORDER BY p.handle", (sess["id"],))]
    return {"items": items, **projection, "more": more,
            "older_before": rows[-1]["seq"] if more and rows else None,
            "authors": authors, "kinds": sorted(MESSAGE_TYPE_SET | ({r[0] for r in conn.execute(
                "SELECT DISTINCT type FROM event WHERE session_id=?", (sess["id"],))} if include_system else set()))}


def global_page(conn: sqlite3.Connection, *, author: str = "", kind: str = "", query: str = "",
                task: int | None = None, include_system: bool = False, before: int | None = None,
                thread: int | None = None, reply: int | None = None, limit: int = PAGE_SIZE) -> dict:
    """Return an admin-wide chronological projection across all workspaces.

    Global pagination uses SQLite's event row id because event sequence numbers are
    scoped to a workspace. Reply relationships are likewise keyed by workspace.
    """
    where = ["e.type != 'message.preflight'"]
    args: dict[str, object] = {}
    if not include_system:
        names = ",".join(f":m{i}" for i, _ in enumerate(MESSAGE_TYPES))
        where.append(f"e.type IN ({names})")
        args.update({f"m{i}": value for i, value in enumerate(MESSAGE_TYPES)})
    if author:
        where.append("p.handle=:author"); args["author"] = author
    if kind:
        where.append("e.type=:kind"); args["kind"] = kind
    if task is not None:
        where.append("e.task_num=:task"); args["task"] = task
    if query:
        where.append("(instr(lower(coalesce(e.body,'')),lower(:q))>0 OR "
                     "instr(lower(coalesce(e.data,'')),lower(:q))>0)")
        args["q"] = query[:200]
    if before is not None:
        where.append("e.rowid<:before"); args["before"] = before
    rows = conn.execute(
        f"SELECT e.*, e.rowid AS global_id, s.handle AS session_handle FROM event e "
        f"JOIN session s ON s.id=e.session_id JOIN principal p ON p.id=e.actor_id "
        f"WHERE {' AND '.join(where)} ORDER BY e.rowid DESC LIMIT :limit",
        {**args, "limit": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    handles = inbox.handle_map(conn, [row["actor_id"] for row in rows])
    items = []
    for row in reversed(rows):
        item = _item(inbox.event_view(conn, row, handles), row["session_handle"])
        item["global_id"] = row["global_id"]
        items.append(item)
    sessions_in_page = sorted({item["session"] for item in items})
    parents = {}
    if sessions_in_page:
        session_marks = ",".join("?" for _ in sessions_in_page)
        type_clause = "" if include_system else " AND e.type IN ({})".format(
            ",".join("?" for _ in MESSAGE_TYPES))
        parent_rows = conn.execute(
            f"SELECT s.handle,e.seq,e.reply_to FROM event e JOIN session s ON s.id=e.session_id "
            f"WHERE s.handle IN ({session_marks}) AND e.type!='message.preflight'{type_clause}",
            (*sessions_in_page, *((MESSAGE_TYPES) if not include_system else ()))).fetchall()
        parents = {(row["handle"], row["seq"]): row["reply_to"] for row in parent_rows}
    projection = _thread_projection(items, thread, reply, parents)
    authors = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.handle FROM event e JOIN principal p ON p.id=e.actor_id ORDER BY p.handle")]
    kinds = sorted(MESSAGE_TYPE_SET | ({r[0] for r in conn.execute(
        "SELECT DISTINCT type FROM event")} if include_system else set()))
    return {"items": items, **projection, "more": more,
            "older_before": rows[-1]["global_id"] if more and rows else None,
            "authors": authors, "kinds": kinds}

