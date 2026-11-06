"""Topic channels inside a session (B55, ERRATA E43).

A channel is a named subset of the session's members with a topic. It decides whose inbox a message
reaches and who is woken; it never hides anything: channel messages stay in the one session log,
which every member reads (history, dashboard, export). Rules:
- Humans and coordinators (Session Managers and Task Managers) create, change and archive channels;
  agents propose one in a message. Every change is one event, routed to the channel's members.
- Each member sets its own mode: `follow` (every message), `mentions` (only messages addressed to it
  with `to`, or replies to its own messages) or `mute` (only messages addressed to it). Questions,
  approvals and holds addressed to a member always reach it: they are addressed by definition.
- A message with `channel` and no `to` reaches the channel's audience under those modes; `to` inside a
  channel narrows further and must name channel members. Agents post only in channels they belong to
  (and, for a post-only channel, only when listed as posters); humans may post in any channel.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field, StrictBool

from . import db, render, tokens
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import require_member, require_state

router = APIRouter(prefix="/api/v1")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
NAME_PATTERN = NAME_RE.pattern
MODES = ("follow", "mentions", "mute")
MAX_MEMBERS = 64


def load(c, sid: str, name: str):
    ch = c.execute("SELECT * FROM channel WHERE session_id=? AND name=?", (sid, name)).fetchone()
    if ch is None:
        raise ApiError(404, "not_found", "No such channel in this session.")
    return ch


def members(c, sid: str, name: str) -> list:
    return c.execute("SELECT cm.*, p.handle, p.kind FROM channel_member cm JOIN principal p ON p.id=cm.principal_id "
                     "JOIN member m ON m.session_id=cm.session_id AND m.principal_id=cm.principal_id AND m.left_at IS NULL "
                     "WHERE cm.session_id=? AND cm.name=? ORDER BY p.handle", (sid, name)).fetchall()


def audience(c, sid: str, name: str, actor_id: str, reply_to: int | None) -> list[str]:
    """Who a channel message without `to` reaches: followers, plus a mentions/mute member whose own
    message is being answered (a reply is addressed to its author)."""
    replied = None
    if reply_to is not None:
        r = c.execute("SELECT actor_id FROM event WHERE session_id=? AND seq=?", (sid, reply_to)).fetchone()
        replied = r["actor_id"] if r else None
    out = []
    for m in members(c, sid, name):
        if m["principal_id"] == actor_id:
            continue
        if m["mode"] == "follow" or (m["mode"] == "mentions" and m["principal_id"] == replied):
            out.append(m["principal_id"])
    return out


def check_post(c, sid: str, p: tokens.Principal, name: str, to_ids: list[str]) -> None:
    """Refuse a channel post that the rules above do not allow. Never echoes the channel name."""
    ch = load(c, sid, name)
    if ch["archived_at"]:
        raise ApiError(409, "channel_archived", "This channel is archived; post in the session or another channel.")
    mem = {m["principal_id"]: m for m in members(c, sid, name)}
    if p.kind != "human":
        me = mem.get(p.id)
        if me is None:
            raise ApiError(403, "forbidden", "Only members of this channel may post in it.",
                           hint="A human or a coordinator adds members to a channel.")
        if ch["post_policy"] == "listed" and not me["can_post"]:
            raise ApiError(403, "forbidden", "This channel is post-only for its listed posters.")
    outside = [i for i in to_ids if i not in mem]
    if outside:
        raise ApiError(422, "recipient_not_in_channel", "Inside a channel, `to` names channel members only.",
                       hint="Add them to the channel first, or post without the channel.",
                       details={"count": len(outside)})


def view(c, sid: str, ch, viewer_id: str | None = None) -> dict:
    ms = members(c, sid, ch["name"])
    mine = next((m["mode"] for m in ms if m["principal_id"] == viewer_id), None)
    return {"name": ch["name"], "topic": ch["topic"], "post_policy": ch["post_policy"], "archived": bool(ch["archived_at"]),
            "members": [{"handle": m["handle"], "kind": m["kind"], "mode": m["mode"], "can_post": bool(m["can_post"])}
                        for m in ms],
            "my_mode": mine}


def list_view(c, sess, viewer_id: str) -> dict:
    rows = c.execute("SELECT * FROM channel WHERE session_id=? ORDER BY archived_at IS NOT NULL, name", (sess["id"],)).fetchall()
    return {"session": sess["handle"], "channels": [view(c, sess["id"], r, viewer_id) for r in rows]}


def list_text(r: dict) -> str:
    lines = [render.section(f"--- channels · {r['session']} · {len(r['channels'])} ---")]
    for ch in r["channels"]:
        state = " · archived" if ch["archived"] else ""
        policy = " · post-only" if ch["post_policy"] == "listed" else ""
        mine = f" · you: {ch['my_mode']}" if ch["my_mode"] else " · you: not a member"
        lines.append(render.section(f"--- #{ch['name']} · {len(ch['members'])} member(s){policy}{state}{mine} ---"))
        if ch["topic"]:
            lines.append(render.inline_line("topic", ch["topic"]))
        lines.append(render.inline_line("members", ", ".join(
            f"@{m['handle']} ({m['mode']}{'' if m['can_post'] or ch['post_policy'] == 'all' else ', reads'})"
            for m in ch["members"]) or "none"))
    return "\n".join(lines) + "\n"


def _manager(tx) -> None:
    if tx.p.kind not in ("human", "agent") or (tx.p.kind == "agent" and tx.me["role"] != "coordinator") \
            or (tx.p.kind == "human" and tx.me["role"] not in ("coordinator", "member")):
        raise ApiError(403, "forbidden", "Only humans and coordinators create or change channels.",
                       hint="Agents can propose a channel in a message.")


def _member_ids(c, sid: str, handles: list[str]) -> list:
    rows, bad = [], 0
    for h in dict.fromkeys(handles):
        r = c.execute("SELECT p.id, p.handle FROM principal p JOIN member m ON m.principal_id=p.id "
                      "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (h, sid)).fetchone()
        rows.append(r) if r else None
        bad += r is None
    if bad:
        raise ApiError(422, "recipient_not_member", "Some handles are not members of this session.",
                       hint="Check them with `glassboard members list`.", details={"count": bad})
    return rows


class ChannelIn(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    topic: str | None = Field(default=None, min_length=1, max_length=200)
    members: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)
    post_policy: Literal["all", "listed"] = "all"
    posters: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)


class ChannelChange(StrictModel):
    topic: str | None = Field(default=None, min_length=1, max_length=200)
    add: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)
    remove: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)
    post_policy: Literal["all", "listed"] | None = None
    posters: list[str] | None = Field(default=None, max_length=MAX_MEMBERS)
    archive: StrictBool = False


class ModeIn(StrictModel):
    mode: Literal["follow", "mentions", "mute"]


@router.get("/sessions/{s}/channels")
def list_channels(s: str, request: Request, format: Literal["json", "text"] = Query("json"),
                  p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """The session's channels: topic, members and their modes, and your own mode."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        r = list_view(c, sess, p.id)
    if format == "text" or "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(list_text(r))
    return r


@router.post("/sessions/{s}/channels", status_code=201)
def create_channel(s: str, body: ChannelIn, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Create a channel with a topic and members (humans and coordinators). The creator is a member."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/channels", body.model_dump(), status=201)
        if replay is not None:
            return replay
        _manager(tx)
        require_state(tx.sess, "message")
        if c.execute("SELECT 1 FROM channel WHERE session_id=? AND name=?", (sid, body.name)).fetchone():
            raise ApiError(409, "channel_exists", "A channel with this name already exists in this session.")
        rows = _member_ids(c, sid, [*body.members, p.handle])
        posters = {r["id"] for r in _member_ids(c, sid, body.posters)} | {p.id}
        c.execute("INSERT INTO channel(session_id,name,topic,post_policy,created_by,created_at) VALUES (?,?,?,?,?,?)",
                  (sid, body.name, body.topic, body.post_policy, p.id, db.utcnow()))
        for r in rows:
            c.execute("INSERT INTO channel_member(session_id,name,principal_id,can_post) VALUES (?,?,?,?)",
                      (sid, body.name, r["id"], int(body.post_policy == "all" or r["id"] in posters)))
        data = {"title": body.name, "members": ", ".join("@" + r["handle"] for r in rows)}
        if body.post_policy == "listed":
            data["status"] = "post-only: " + ", ".join("@" + r["handle"] for r in rows if r["id"] in posters)
        _, text = tx.event("channel.created", body=body.topic, data=data, channel=body.name,
                           to=[r["id"] for r in rows],
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "channel": body.name,
                                                "members": len(rows)})
    return respond(text, 201)


@router.post("/sessions/{s}/channels/{name}")
def change_channel(s: str, name: str, body: ChannelChange, key: str = Depends(idempotency_key),
                   recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                   runtime: str | None = Depends(runtime_label),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Change a channel's topic, members or posting rule, or archive it (humans and coordinators)."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        if not NAME_RE.match(name):
            raise ApiError(404, "not_found", "No such channel in this session.")
        ch = load(c, sid, name)
        replay = tx.replay(key, "POST /sessions/{s}/channels/{name}", body.model_dump(),
                           target={"type": "channel", "id": name})
        if replay is not None:
            return replay
        _manager(tx)
        require_state(tx.sess, "message")
        if ch["archived_at"]:
            raise ApiError(409, "channel_archived", "This channel is archived.")
        before = [m["principal_id"] for m in members(c, sid, name)]
        added = _member_ids(c, sid, body.add)
        removed = _member_ids(c, sid, body.remove)
        changes = []
        if body.topic is not None and body.topic != ch["topic"]:
            c.execute("UPDATE channel SET topic=? WHERE session_id=? AND name=?", (body.topic, sid, name))
            changes.append("topic")
        for r in added:
            if r["id"] not in before:
                c.execute("INSERT INTO channel_member(session_id,name,principal_id,can_post) VALUES (?,?,?,?)",
                          (sid, name, r["id"], int((body.post_policy or ch["post_policy"]) == "all")))
                changes.append("+@" + r["handle"])
        for r in removed:
            if r["id"] in before:
                c.execute("DELETE FROM channel_member WHERE session_id=? AND name=? AND principal_id=?", (sid, name, r["id"]))
                changes.append("-@" + r["handle"])
        if body.post_policy is not None and body.post_policy != ch["post_policy"]:
            c.execute("UPDATE channel SET post_policy=? WHERE session_id=? AND name=?", (body.post_policy, sid, name))
            changes.append(f"posting: {body.post_policy}")
        if body.posters is not None:
            ids = {r["id"] for r in _member_ids(c, sid, body.posters)}
            c.execute("UPDATE channel_member SET can_post=CASE WHEN principal_id IN (%s) THEN 1 ELSE 0 END "
                      "WHERE session_id=? AND name=?" % (",".join("?" * len(ids)) or "NULL"), (*ids, sid, name))
            changes.append("posters")
        if body.archive:
            c.execute("UPDATE channel SET archived_at=? WHERE session_id=? AND name=?", (db.utcnow(), sid, name))
        if not changes and not body.archive:
            raise ApiError(409, "no_change", "Nothing to change.")
        reach = sorted(set(before) | {r["id"] for r in added})
        etype = "channel.archived" if body.archive else "channel.changed"
        data = {"title": name, **({"status": ", ".join(changes)} if changes else {})}
        _, text = tx.event(etype, body=body.topic if "topic" in changes else None, data=data, channel=name, to=reach,
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "channel": name,
                                                "archived": body.archive, "changes": changes})
    return respond(text, 200)


@router.post("/sessions/{s}/channels/{name}/mode")
def set_mode(s: str, name: str, body: ModeIn, key: str = Depends(idempotency_key),
             recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
             runtime: str | None = Depends(runtime_label),
             p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A channel member chooses follow, mentions or mute for itself. Recorded, routed to nobody."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        if not NAME_RE.match(name):
            raise ApiError(404, "not_found", "No such channel in this session.")
        load(c, sid, name)
        replay = tx.replay(key, "POST /sessions/{s}/channels/{name}/mode", body.model_dump(),
                           target={"type": "channel", "id": name})
        if replay is not None:
            return replay
        require_state(tx.sess, "message")
        row = c.execute("SELECT mode FROM channel_member WHERE session_id=? AND name=? AND principal_id=?",
                        (sid, name, p.id)).fetchone()
        if row is None:
            raise ApiError(409, "not_a_channel_member", "You are not a member of this channel.")
        c.execute("UPDATE channel_member SET mode=? WHERE session_id=? AND name=? AND principal_id=?",
                  (body.mode, sid, name, p.id))
        _, text = tx.event("channel.mode", data={"title": name, "status": body.mode}, channel=name,
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "channel": name,
                                                "mode": body.mode})
    return respond(text, 200)
