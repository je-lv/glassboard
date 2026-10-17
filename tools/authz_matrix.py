#!/usr/bin/env python3
"""Authorization matrix for the Glassboard HTTP API (IDOR / cross-session access): every route ×
every actor class × own/foreign session, checked against a declared policy, with a Markdown report.

Usage:  python tools/authz_matrix.py [--out docs/authz-matrix.md]
Exit status 0 when every cell matches the policy and every route is classified, 1 otherwise.
tests/test_authz_matrix.py runs the same function, so CI fails on any drift.

How a response is classified (what authorization decided, not the business outcome):
  401 → "401"; 403 `forbidden` → "403"; 404 with the session-level message "Not found." → "404"
  (a non-member, or an unknown session: 404, not 403); anything else → "ok"
  (2xx, or a later business refusal such as 409 fence_mismatch or 422 validation).
Every request carries a valid body and existing referenced objects (see PREPARE), so an "ok" actor
reaches the operation itself. Actors that should be refused run before the allowed ones, so a
successful state change cannot hide a refusal.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from glassboard import admin, ids
from glassboard.app import create_app
from glassboard.limits import AuthFailureLimiter, PostRateLimiter

ACTORS = ("anon", "readonly", "foreign", "observer", "agent_member", "agent_coord", "human_member", "human_coord", "admin")
ALL_IN = ("readonly", "observer", "agent_member", "agent_coord", "human_member", "human_coord")      # members of `own`
WRITERS = ("observer", "agent_member", "agent_coord", "human_member", "human_coord")                # full-scope members

# Policy rules: actor → expected class.
def rule(ok=(), forbidden=(), member_404=True):
    def f(actor):
        if actor in ok:
            return "ok"
        if actor == "anon":
            return "401"
        if member_404 and actor in ("foreign", "admin"):
            return "404"
        return "403"
    f.ok = ok
    return f


PUBLIC = lambda actor: "ok"                                                          # noqa: E731
AUTHENTICATED = rule(ok=("readonly", "foreign", "observer", "agent_member", "agent_coord", "human_member",
                         "human_coord", "admin"), member_404=False)
HUMANS_FULL = rule(ok=("foreign", "human_member", "human_coord", "admin"), member_404=False)   # instance-level, humans
ADMIN_ONLY = rule(ok=("admin",), member_404=False)
SESSION_READ = rule(ok=ALL_IN)
SESSION_WRITE = rule(ok=WRITERS)                       # read-only tokens 403, non-members 404
POSTERS = rule(ok=("agent_member", "agent_coord", "human_member", "human_coord"))     # observers do not post or claim
HUMAN_COORD = rule(ok=("human_coord",))                # human-only authority
DIVIDERS = rule(ok=("human_coord", "agent_coord"))     # dividing work: human coordinators and the orchestrator agent (E23)
RESOLVERS = rule(ok=("agent_member", "agent_coord", "human_coord"))     # recipient, coordinators, asker (human_coord)
RECLAIMERS = rule(ok=("agent_member", "agent_coord", "human_coord"))   # the claimant (agent_member) or a coordinator

POLICY = {
    ("GET", "/health/live"): PUBLIC,
    ("GET", "/api/v1/discovery"): PUBLIC,
    ("GET", "/api/v1/protocol"): PUBLIC,
    ("GET", "/ca.crt"): PUBLIC,
    ("POST", "/api/v1/join"): PUBLIC,                       # redeems an invite code; any bad code is 404 invite_invalid                                              # a public certificate
    ("GET", "/api/v1/whoami"): AUTHENTICATED,
    ("GET", "/api/v1/openapi.json"): AUTHENTICATED,
    ("GET", "/metrics"): ADMIN_ONLY,
    ("GET", "/api/v1/sessions"): AUTHENTICATED,
    ("GET", "/api/v1/roles"): AUTHENTICATED,
    ("GET", "/api/v1/roles/{name}/revisions"): AUTHENTICATED,
    ("PUT", "/api/v1/roles/{name}"): ADMIN_ONLY,
    ("GET", "/api/v1/sessions/{s}/job-roles"): SESSION_READ,
    ("POST", "/api/v1/sessions/{s}/job-roles"): POSTERS,
    ("POST", "/api/v1/sessions"): HUMANS_FULL,
    ("POST", "/api/v1/me/agents"): HUMANS_FULL,
    ("POST", "/api/v1/me/tokens"): HUMANS_FULL,
    # Own tokens only. Another human gets the object hidden (404 "…of yours"): the IDOR case.
    ("DELETE", "/api/v1/me/tokens/{credential_id}"): lambda a: {"anon": "401", "human_member": "ok", "human_coord": "hidden",
                                                                "foreign": "hidden", "admin": "hidden"}.get(a, "403"),
    ("GET", "/api/v1/sessions/{s}/members"): SESSION_READ,
    ("POST", "/api/v1/sessions/{s}/members"): HUMAN_COORD,
    ("POST", "/api/v1/sessions/{s}/invites"): HUMAN_COORD,      # humans invite their own agents
    ("DELETE", "/api/v1/sessions/{s}/members/{handle:path}"): HUMAN_COORD,
    ("POST", "/api/v1/sessions/{s}/state"): HUMAN_COORD,
    ("POST", "/api/v1/sessions/{s}/messages"): POSTERS,
    ("POST", "/api/v1/sessions/{s}/messages/preflight"): POSTERS,
    ("POST", "/api/v1/sessions/{s}/questions/{seq}/resolve"): RESOLVERS,
    ("POST", "/api/v1/sessions/{s}/sync"): SESSION_WRITE,
    ("GET", "/api/v1/sessions/{s}/events"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/export"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/tasks"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/tasks/{t}"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/tasks/{t}/trail"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/tasks/next"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/artifacts"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/artifacts/{artifact_id}"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/artifacts/{artifact_id}/content"): SESSION_READ,
    ("POST", "/api/v1/sessions/{s}/artifacts"): POSTERS,
    ("POST", "/api/v1/sessions/{s}/tasks"): DIVIDERS,
    ("PATCH", "/api/v1/sessions/{s}/tasks/{t}"): DIVIDERS,
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/claim"): POSTERS,
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/acknowledge"): rule(ok=("agent_member",)),
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reclaim"): RECLAIMERS,
    **{("POST", f"/api/v1/sessions/{{s}}/tasks/{{t}}/{op}"): SESSION_WRITE                # the fence decides (409)
       for op in ("note", "check", "block", "unblock", "submit", "release")},
    **{("POST", f"/api/v1/sessions/{{s}}/tasks/{{t}}/{op}"): HUMAN_COORD
       for op in ("accept", "return", "cancel", "reopen")},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reassign"): DIVIDERS,
    ("GET", "/api/v1/sessions/{s}/board"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/digest"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/leases"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/flow"): SESSION_READ,
    ("POST", "/api/v1/sessions/{s}/leases"): POSTERS,
    ("POST", "/api/v1/sessions/{s}/leases/release"): rule(ok=("agent_member", "human_coord")),   # holder or human coordinator
    ("POST", "/api/v1/sessions/{s}/approvals/{seq}"): rule(ok=("human_member", "human_coord")),  # named human or coordinator
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/review"): rule(ok=("agent_member",)),              # the named reviewer only
    ("GET", "/api/v1/sessions/{s}/decisions"): SESSION_READ,
    ("GET", "/api/v1/sessions/{s}/standup"): SESSION_READ,
    ("GET", "/api/v1/me/attention"): AUTHENTICATED,                      # only the caller's own items
    ("GET", "/api/v1/me/overview"): AUTHENTICATED,
    ("GET", "/api/v1/sessions/{s}/checkpoints"): SESSION_READ,          # own checkpoint (404 "No such checkpoint." is ok)
    ("POST", "/api/v1/sessions/{s}/checkpoints"): SESSION_WRITE,
}
# Routes this matrix does not cover, each with the reason and where it is tested instead.
DELEGATED = {
    "/health/ready": "loopback-only probe, tests/test_authz_matrix.py",
    "/docs": "public product guide with no board data: tests/test_dashboard.py",
    "/login": "dashboard, cookie auth: tests/test_dashboard.py",
    "/admin/login": "dashboard admin password authentication: tests/test_dashboard.py",
    "/admin": "dashboard, full-scope admin cookie: tests/test_dashboard.py",
    "/admin/password": "dashboard admin password change: tests/test_dashboard.py",
    "/admin/s/{s}": "dashboard admin read-only session inspection: tests/test_dashboard.py",
    "/admin/external/{index}/s/{s}": "dashboard admin read-only separate-board inspection: tests/test_dashboard.py",
    "/admin/access/create": "dashboard admin access mutation: experimental, browser authorization checks pending",
    "/admin/access/change": "dashboard admin access mutation: experimental, browser authorization checks pending",
    "/admin/access/password": "dashboard admin password reset: experimental, browser authorization checks pending",
    "/admin/credentials/create": "dashboard admin token mint: experimental, browser authorization checks pending",
    "/admin/credentials/revoke": "dashboard admin token revoke: experimental, browser authorization checks pending",
    "/admin/settings/boards": "dashboard separate-board settings: experimental, browser authorization checks pending",
    "/admin/settings/run-evidence": "dashboard private run-evidence settings: tests/test_conversations_runs.py",
    "/admin/conversations": "dashboard admin conversation explorer: tests/test_conversations_runs.py",
    "/admin/runs": "dashboard private run-evidence explorer: tests/test_conversations_runs.py",
    "/admin/runs/{root_index}/{round_name}": "dashboard private run detail: tests/test_conversations_runs.py",
    "/admin/runs/{root_index}/{round_name}/files/{filename}": "dashboard captured file viewer: tests/test_conversations_runs.py",
    "/admin/backups/create": "dashboard encrypted backup: experimental, browser authorization checks pending",
    "/admin/backups/{name}": "dashboard admin backup download: experimental, browser authorization checks pending",
    "/admin/workspaces/archive": "dashboard admin verified workspace export: tests/test_workspace_cleanup.py",
    "/admin/workspace-archives/{archive_id}": "dashboard admin verified workspace archive download: tests/test_workspace_cleanup.py",
    "/admin/workspaces/purge": "dashboard admin guarded workspace purge: tests/test_workspace_cleanup.py",
    "/attention/{s}/{seq}/dismiss": "dashboard human-coordinator proposal reply: experimental, browser authorization checks pending",
    "/logout": "dashboard, cookie auth: tests/test_dashboard.py",
    "/": "dashboard, cookie auth: tests/test_dashboard.py",
    "/attention-count": "dashboard, cookie auth (a count only): tests/test_comm.py",
    "/new-session": "dashboard, cookie auth, humans: tests/test_dashboard.py",
    "/agents": "dashboard, cookie auth, humans (my agents): tests/test_join.py",
    "/agents/token": "dashboard, cookie auth, owned token mint: tests/test_dashboard.py",
    "/owner": "dashboard, cookie auth, full-scope admin only: tests/test_dashboard.py",
    "/owner/s/{s}/join": "dashboard, cookie auth, explicit admin join: tests/test_dashboard.py",
    "/owner/roles/{name}": "dashboard, cookie auth, role prompt edits: tests/test_roles.py",
    "/owner/roles": "dashboard, cookie auth, new role prompt: tests/test_roles.py",
    "/s/{s}/team": "dashboard team management, cookie auth: tests/test_dashboard.py",
}
DASHBOARD_PREFIX = "/s/"                                    # cookie-authenticated pages: covered by the dashboard tests

BODIES = {
    ("POST", "/api/v1/sessions"): lambda ctx: {"handle": f"s{ids.new_ulid()[-8:].lower().replace('0', 'a')}", "title": "t"},
    ("POST", "/api/v1/me/agents"): lambda ctx: {"name": f"a{ids.new_ulid()[-6:].lower()}"},
    ("POST", "/api/v1/me/tokens"): lambda ctx: {},
    ("PUT", "/api/v1/roles/{name}"): lambda ctx: {"prompt": "Coordinate architecture review.", "if_version": 1},
    ("POST", "/api/v1/sessions/{s}/job-roles"): lambda ctx: {"handle": "jo/agentm", "role": "architect"},
    ("POST", "/api/v1/sessions/{s}/members"): lambda ctx: {"handle": "nobody/extra", "role": "member"},
    ("POST", "/api/v1/sessions/{s}/state"): lambda ctx: {"state": "paused", "reason": "r"},
    ("POST", "/api/v1/sessions/{s}/messages"): lambda ctx: {"kind": "note", "body": "hello"},
    ("POST", "/api/v1/sessions/{s}/messages/preflight"): lambda ctx: {"kind": "note", "body": "hello"},
    ("POST", "/api/v1/sessions/{s}/questions/{seq}/resolve"): lambda ctx: {"resolution": "declined"},
    ("POST", "/api/v1/sessions/{s}/sync"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/tasks"): lambda ctx: {"title": "t", "scope": "s", "deliverable": "d"},
    ("PATCH", "/api/v1/sessions/{s}/tasks/{t}"): lambda ctx: {"if_version": ctx["version"], "title": "renamed"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/claim"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/acknowledge"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reclaim"): lambda ctx: {"reason": "r"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/note"): lambda ctx: {"fence": ctx["fence"], "note": "n"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/check"): lambda ctx: {"fence": ctx["fence"], "item": 1},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/block"): lambda ctx: {"fence": ctx["fence"], "reason": "r"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/unblock"): lambda ctx: {"fence": ctx["fence"]},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/submit"): lambda ctx: {"fence": ctx["fence"], "summary": "s"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/release"): lambda ctx: {"fence": ctx["fence"]},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/accept"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/return"): lambda ctx: {"feedback": "f"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reassign"): lambda ctx: {"to": "jo/agentm"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/cancel"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reopen"): lambda ctx: {},
    ("POST", "/api/v1/sessions/{s}/checkpoints"): lambda ctx: {"summary": "s", "next_action": "n"},
    ("POST", "/api/v1/sessions/{s}/leases"): lambda ctx: {"resource": ctx["resource"]},
    ("POST", "/api/v1/sessions/{s}/invites"): lambda ctx: {"agent": f"i{ids.new_ulid()[-8:].lower().replace('0', 'a')}"},
    ("POST", "/api/v1/join"): lambda ctx: {"code": "AAAA-AAAA-AAAA"},
    ("POST", "/api/v1/sessions/{s}/leases/release"): lambda ctx: {"resource": ctx["resource"]},
    ("POST", "/api/v1/sessions/{s}/approvals/{seq}"): lambda ctx: {"decision": "grant"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/review"): lambda ctx: {"verdict": "approve", "submission_seq": ctx["submission_seq"]},
    ("POST", "/api/v1/sessions/{s}/artifacts"): lambda ctx: {},
}


class Instance:
    """A fresh board: humans jo (coordinator), mo (member), ad (instance admin, member of nothing),
    fo (member of `other` only); agents jo/agentm (member), jo/agentc (coordinator), jo/obs
    (observer); a read-only token for mo. Sessions `own` and `other`."""

    def __init__(self, tmp: str):
        self.db = os.path.join(tmp, "m.db")
        self._admin("add-human", "jo")
        self._admin("add-human", "mo")
        self._admin("add-human", "fo")
        self._admin("add-human", "ad", "--admin")
        self._admin("add-human", "nobody")
        for n in ("agentm", "agentc", "obs"):
            self._admin("add-agent", "jo", n)
        self._admin("add-agent", "nobody", "extra")
        self.tok = {"human_coord": self._mint("jo"), "human_member": self._mint("mo"), "foreign": self._mint("fo"),
                    "admin": self._mint("ad"), "agent_member": self._mint("jo/agentm"), "agent_coord": self._mint("jo/agentc"),
                    "observer": self._mint("jo/obs"), "readonly": self._mint("mo", "--scope", "read")}
        self.app = create_app(self.db, allowed_hosts=["testserver"], sweep_interval_s=0)
        # The matrix measures authorization, not rate limits (tested on their own): many
        # anonymous calls from one address would otherwise trip the 10-failures/min limiter.
        self.app.state.auth_limiter = AuthFailureLimiter(limit=10**9)
        self.app.state.post_limiter = PostRateLimiter(burst=10**9)
        self.c = TestClient(self.app, raise_server_exceptions=False)
        self.c.__enter__()  # reuse one portal/thread pool for the matrix's many requests
        self.req("POST", "/api/v1/sessions", "human_coord", {"handle": "own", "title": "own"})
        self.req("POST", "/api/v1/sessions", "foreign", {"handle": "other", "title": "other"})
        for handle, role in (("mo", "member"), ("jo/agentm", "member"), ("jo/agentc", "coordinator"), ("jo/obs", "observer")):
            self.req("POST", "/api/v1/sessions/own/members", "human_coord", {"handle": handle, "role": role}, s="own")

    def _admin(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            admin.main(["--db", self.db, *args])
        return out.getvalue().strip()

    def _mint(self, handle, *extra):
        return self._admin("mint-token", handle, *extra)

    def recovery(self, s):
        for x in self.c.get("/api/v1/sessions", headers={"Authorization": f"Bearer {self.tok['human_coord']}"}).json()["sessions"]:
            if x["session"] == s:
                return x["recovery_id"]
        return "0" * 16

    def req(self, method, path, actor, body=None, s="own"):
        h = {"Idempotency-Key": ids.new_ulid(), "X-Glassboard-Recovery": self.recovery(s)}
        if actor != "anon":
            h["Authorization"] = f"Bearer {self.tok[actor]}"
        return self.c.request(method, path, headers=h, json=body)

    def prepare(self, method: str, path: str) -> dict:
        """Objects the route refers to, made by the right principals; returns path values + context."""
        ctx: dict = {"s": "own"}
        # Each route starts from an active `own` session (the /state route's allowed cell pauses it).
        own = [x for x in self.c.get("/api/v1/sessions", headers={"Authorization": f"Bearer {self.tok['human_coord']}"})
               .json()["sessions"] if x["session"] == "own"][0]
        if own["state"] != "active":
            self.req("POST", "/api/v1/sessions/own/state", "human_coord", {"state": "active", "reason": "matrix"})
        ctx["resource"] = f"res-{ids.new_ulid()[-8:].lower()}"
        if path.endswith("/leases/release"):
            self.req("POST", "/api/v1/sessions/own/leases", "agent_member", {"resource": ctx["resource"]})
        if "{t}" in path:
            extra = {"reviewer": "jo/agentm"} if path.endswith("/review") else (
                {"assignee": "jo/agentm"} if path.endswith("/acknowledge") else {})
            t = self.req("POST", "/api/v1/sessions/own/tasks", "human_coord",
                         {"title": "t", "scope": "s", "deliverable": "d", **extra}).json()["task"]
            ctx["t"] = str(t)
            if path.endswith(("/note", "/block", "/unblock", "/submit", "/release", "/reclaim")):
                ctx["fence"] = self.req("POST", f"/api/v1/sessions/own/tasks/{t}/claim", "agent_member", {}).json()["fence"]
                if path.endswith("/unblock"):
                    self.req("POST", f"/api/v1/sessions/own/tasks/{t}/block", "agent_member", {"fence": ctx["fence"], "reason": "r"})
            if path.endswith(("/accept", "/return", "/review")):
                f = self.req("POST", f"/api/v1/sessions/own/tasks/{t}/claim", "agent_member", {}).json()["fence"]
                submitted = self.req("POST", f"/api/v1/sessions/own/tasks/{t}/submit", "agent_member", {"fence": f, "summary": "s"})
                ctx["submission_seq"] = submitted.json()["seq"]
            if path.endswith("/reopen"):
                self.req("POST", f"/api/v1/sessions/own/tasks/{t}/cancel", "human_coord", {})
            ctx["version"] = self.req("GET", f"/api/v1/sessions/own/tasks/{t}", "human_coord").json()["version"]
            ctx.setdefault("fence", f"T{t}g1-aaaaaaaaaa")
        if "{seq}" in path and "/approvals/" in path:
            ctx["seq"] = str(self.req("POST", "/api/v1/sessions/own/messages", "agent_member",
                                      {"kind": "approval", "body": "may I?", "to": ["mo"]}).json()["seq"])
        elif "{seq}" in path:
            ctx["seq"] = str(self.req("POST", "/api/v1/sessions/own/messages", "human_coord",
                                      {"kind": "question", "body": "q?", "to": ["jo/agentm"]}).json()["seq"])
        if "{handle:path}" in path:
            self.req("POST", "/api/v1/sessions/own/members", "human_coord", {"handle": "nobody/extra"})
            ctx["handle:path"] = "nobody/extra"
        if "{credential_id}" in path:
            ctx["credential_id"] = self.req("POST", "/api/v1/me/tokens", "human_member", {}).json()["id"]
        if "{name}" in path:
            ctx["name"] = "architect"
        if "{artifact_id}" in path:
            made = self.req("POST", "/api/v1/sessions/own/artifacts", "agent_member", {})
            ctx["artifact_id"] = made.json()["artifact"]
        return ctx


def fill(path: str, ctx: dict, s: str) -> str:
    out = path.replace("{s}", s)
    for k in ("t", "seq", "handle:path", "credential_id", "name", "artifact_id"):
        if k in ctx:
            out = out.replace("{" + k + "}", ctx[k])
    return out


def classify(r) -> str:
    if r.status_code == 401:
        return "401"
    try:
        e = r.json().get("error", {})
    except Exception:
        e = {}
    if r.status_code == 403 and e.get("code") == "forbidden":
        return "403"
    if r.status_code == 404 and e.get("message") == "Not found.":
        return "404"
    if r.status_code == 404 and "of yours" in (e.get("message") or ""):
        return "hidden"                     # someone else's object, not revealed (IDOR protection)
    return "ok"


def api_routes(app):
    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
    return list(walk(app.routes))


def run(tmp: str | None = None) -> dict:
    tmpdir = tmp or tempfile.mkdtemp(prefix="authz-")
    inst = Instance(tmpdir)
    routes = sorted({(m, r.path) for r in api_routes(inst.app) for m in r.methods})
    unclassified, cells = [], []
    for method, path in routes:
        if path.startswith(DASHBOARD_PREFIX) or path in DELEGATED:
            continue
        policy = POLICY.get((method, path))
        if policy is None:
            unclassified.append(f"{method} {path}")
            continue
        sessions_dim = ("own", "other") if "{s}" in path else ("-",)
        for sdim in sessions_dim:
            ctx = inst.prepare(method, path)
            # Refused actors first, allowed ones last (a state change must not hide a refusal).
            ordered = sorted(ACTORS, key=lambda a: policy(a) == "ok")
            for actor in ordered:
                if sdim != "other":
                    want = policy(actor)
                elif actor == "anon":
                    want = "401"
                elif actor == "readonly" and policy("readonly") == "403":
                    # A read-only token is refused on every write route before membership is looked
                    # at (require_full_scope). The answer depends only on the token, so it is the
                    # same for a foreign and an unknown session: no existence oracle.
                    want = "403"
                else:
                    want = "404"
                if sdim == "other" and actor == "foreign":
                    want = "skip"                       # `other` is foreign's own session: covered by the own-session rows
                if want == "skip":
                    continue
                body_fn = BODIES.get((method, path))
                body = body_fn(ctx) if body_fn else None
                if method == "POST" and path == "/api/v1/sessions/{s}/job-roles" and actor == "human_member":
                    body["handle"] = "mo"  # a member may set their own descriptive job role
                r = inst.req(method, fill(path, ctx, "own" if sdim != "other" else "other"), actor, body,
                             s="own" if sdim != "other" else "other")
                got = classify(r)
                cells.append({"method": method, "path": path, "session": sdim, "actor": actor, "want": want,
                              "got": got, "status": r.status_code,
                              "code": (r.json().get("error", {}) or {}).get("code") if r.headers.get("content-type", "").startswith("application/json") else None})
    inst.c.__exit__(None, None, None)
    bad = [c for c in cells if c["want"] != c["got"]]
    return {"cells": cells, "violations": bad, "unclassified": unclassified,
            "routes": len({(c["method"], c["path"]) for c in cells}),
            "delegated": [p for _, p in routes if p in DELEGATED or p.startswith(DASHBOARD_PREFIX)]}


def report(result: dict) -> str:
    mark = {"ok": "ok", "401": "401", "403": "403", "404": "404", "hidden": "hidden"}
    lines = ["# Glassboard authorization matrix", "",
             "Generated by `tools/authz_matrix.py`. Each cell is what authorization decided:",
             "`ok` (the operation ran or failed on its own merits), `401`, `403`, `404` (not a member / unknown",
             "session), or `hidden` (someone else's object answered as not found). A cell marked **✗** differs",
             "from the declared policy.", "",
             f"Routes checked: {result['routes']} · cells: {len(result['cells'])} · violations: {len(result['violations'])}"
             f" · unclassified routes: {len(result['unclassified'])}", ""]
    for sdim, title in (("own", "Own session (or no session)"), ("other", "A session the caller is not a member of")):
        rows = [c for c in result["cells"] if c["session"] in ((sdim, "-") if sdim == "own" else (sdim,))]
        if not rows:
            continue
        actors = [a for a in ACTORS if any(c["actor"] == a for c in rows)]
        lines += [f"## {title}", "", "| Route | " + " | ".join(actors) + " |", "|---|" + "---|" * len(actors)]
        for key in sorted({(c["method"], c["path"]) for c in rows}, key=lambda k: (k[1], k[0])):
            cell = {c["actor"]: c for c in rows if (c["method"], c["path"]) == key}
            vals = []
            for a in actors:
                c = cell.get(a)
                vals.append("" if c is None else (mark[c["got"]] + ("" if c["got"] == c["want"] else f" **✗** (want {c['want']})")))
            lines.append(f"| `{key[0]} {key[1]}` | " + " | ".join(vals) + " |")
        lines.append("")
    lines += ["## Not in this matrix", "", *[f"- `{p}`: {DELEGATED.get(p, 'dashboard, cookie auth: tests/test_dashboard.py, test_session_states.py, test_dashboard_tasks.py')}"
                                            for p in sorted(set(result["delegated"]))], ""]
    if result["unclassified"]:
        lines += ["## Unclassified routes (add them to POLICY)", "", *[f"- `{x}`" for x in result["unclassified"]], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write the Markdown report here")
    a = ap.parse_args(argv)
    os.environ.setdefault("GLASSBOARD_SWEEP_INTERVAL_S", "0")
    result = run()
    text = report(result)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    print(f"routes {result['routes']} · cells {len(result['cells'])} · violations {len(result['violations'])} · "
          f"unclassified {len(result['unclassified'])}")
    for v in result["violations"]:
        print(f"VIOLATION {v['method']} {v['path']} [{v['session']}] {v['actor']}: want {v['want']}, got {v['got']} "
              f"(HTTP {v['status']} {v['code']})")
    return 0 if not result["violations"] and not result["unclassified"] else 1


if __name__ == "__main__":
    sys.exit(main())
