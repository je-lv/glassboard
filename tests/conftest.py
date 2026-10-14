import os
import subprocess
import sys

import pytest
from typing import Literal

from fastapi import Depends
from pydantic import Field
from fastapi.testclient import TestClient

from glassboard import admin, tokens
from glassboard.app import create_app
from glassboard.deps import authenticate
from glassboard.models import StrictModel

PROBE_PATH = "/api/v1/_test/probe"


class Probe(StrictModel):
    """Body shape for the test-only probe route (replaces the removed /_spike/validate route)."""
    kind: Literal["question", "answer"]
    body: str = Field(max_length=16384)
    to: list[str] = Field(max_length=20)


def add_probe_route(app):
    """Mount a body route on the real app, so validation tests exercise the production exception
    handlers, middleware and authentication. It exists only in tests."""
    @app.post(PROBE_PATH, status_code=204)
    def probe(p: Probe, who: tokens.Principal = Depends(authenticate)):
        return None


@pytest.fixture
def board(tmp_path, capsys):
    dbp = str(tmp_path / "b.db")
    admin.main(["--db", dbp, "add-human", "jose"])
    admin.main(["--db", dbp, "add-agent", "jose", "claude"])
    capsys.readouterr()
    def mint(handle, *extra):
        admin.main(["--db", dbp, "mint-token", handle, *extra])
        return capsys.readouterr().out.strip()
    app = create_app(dbp, allowed_hosts=["testserver", "board.lan"])
    add_probe_route(app)
    client = TestClient(app, raise_server_exceptions=False)
    return {"db": dbp, "client": client, "mint": mint, "app": app}


# --- shared board fixture ------------------------------------------------------------------------
import json as _json
import sqlite3 as _sqlite3

from glassboard import ids as _ids


class Board:
    """A fresh instance with humans jose, ana and agents jose/claude, ana/codex (one token each)."""

    PEOPLE = ("jose", "ana")
    AGENTS = (("jose", "claude"), ("ana", "codex"))

    def __init__(self, tmp_path, capsys):
        self.tmp, self.capsys = tmp_path, capsys
        self.db = str(tmp_path / "b.db")
        for h in self.PEOPLE:
            self.admin("add-human", h)
        for o, n in self.AGENTS:
            self.admin("add-agent", o, n)
        self.app = create_app(self.db, allowed_hosts=["testserver"])
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.tok = {h: self.mint(h) for h in [*self.PEOPLE, *(f"{o}/{n}" for o, n in self.AGENTS)]}
        self.raw = _sqlite3.connect(self.db, isolation_level=None)
        self.raw.row_factory = _sqlite3.Row

    def admin(self, *args):
        rc = admin.main(["--db", self.db, *args])
        out = self.capsys.readouterr()
        return rc, out

    def mint(self, handle, *extra):
        return self.admin("mint-token", handle, *extra)[1].out.strip()

    def headers(self, who, rec=None, key=True, extra=None):
        h = {"Authorization": f"Bearer {self.tok.get(who, who)}"}
        if key is True:
            h["Idempotency-Key"] = _ids.new_ulid()
        elif key:
            h["Idempotency-Key"] = key
        if rec:
            h["X-Glassboard-Recovery"] = rec
        return {**h, **(extra or {})}

    def req(self, method, path, who, json=None, rec=None, key=True, extra=None):
        return self.client.request(method, path, headers=self.headers(who, rec, key, extra), json=json)

    def session(self, handle="s1", who="jose", title="Write the docs"):
        r = self.req("POST", "/api/v1/sessions", who, json={"handle": handle, "title": title})
        assert r.status_code == 201, r.text
        return r.json()

    def rec(self, s="s1"):
        return self.raw.execute("SELECT recovery_id FROM session WHERE handle=?", (s,)).fetchone()[0]

    def add(self, s, handle, role="member", who="jose"):
        r = self.req("POST", f"/api/v1/sessions/{s}/members", who, json={"handle": handle, "role": role}, rec=self.rec(s))
        assert r.status_code == 200, r.text
        return r.json()

    def pid(self, handle):
        return self.raw.execute("SELECT id FROM principal WHERE handle=?", (handle,)).fetchone()[0]

    def events(self, s="s1"):
        rows = self.raw.execute("SELECT e.* FROM event e JOIN session s ON s.id=e.session_id WHERE s.handle=? "
                                "ORDER BY e.seq", (s,)).fetchall()
        return [{**dict(r), "data": _json.loads(r["data"]) if r["data"] else None} for r in rows]

    def recipients(self, s, seq):
        return sorted(r[0] for r in self.raw.execute(
            "SELECT p.handle FROM event_recipient er JOIN principal p ON p.id=er.principal_id "
            "JOIN session s ON s.id=er.session_id WHERE s.handle=? AND er.seq=?", (s, seq)))

    def head(self, s="s1"):
        return self.raw.execute("SELECT head_seq FROM session WHERE handle=?", (s,)).fetchone()[0]

    def stop(self):
        """Shut the app down (releases the serving lock, closes the database), like stopping the service."""
        self.app.state.shutdown()

    def restart(self):
        """A new app process on the same files (startup replay runs again)."""
        self.stop()
        self.app = create_app(self.db, allowed_hosts=["testserver"])
        self.client = TestClient(self.app, raise_server_exceptions=False)


@pytest.fixture
def gb(tmp_path, capsys):
    board = Board(tmp_path, capsys)
    try:
        yield board
    finally:
        board.client.close()
        board.raw.close()
        if not board.app.state.db._closed:
            board.stop()


def api_routes(app_or_routes):
    """Every APIRoute, including those inside included routers. FastAPI 0.142 nests
    `include_router` results as `_IncludedRouter` objects instead of flattening them into
    `app.routes`, so walking `app.routes` alone silently misses every router route."""
    from fastapi.routing import APIRoute
    routes = getattr(app_or_routes, "routes", app_or_routes)
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            assert not getattr(r.include_context, "prefix", ""), "walker does not handle include_router(prefix=...)"
            yield from api_routes(r.original_router.routes)


# A valid body for every session route that takes one. Route walkers (non-member → 404, missing
# recovery → 428, ...) fail when a body route has no entry, so new routes are never skipped silently.
VALID_BODIES = {
    ("POST", "/api/v1/sessions/{s}/members"): {"handle": "ana"},
    ("POST", "/api/v1/sessions/{s}/job-roles"): {"handle": "jose", "role": "architect"},
    ("POST", "/api/v1/sessions/{s}/messages"): {"kind": "note", "body": "hello"},
    ("POST", "/api/v1/sessions/{s}/messages/preflight"): {"kind": "note", "body": "hello"},
    ("POST", "/api/v1/sessions/{s}/questions/{seq}/resolve"): {"resolution": "declined"},
    ("POST", "/api/v1/sessions/{s}/sync"): {},
    ("POST", "/api/v1/sessions/{s}/tasks"): {"title": "t", "scope": "s", "deliverable": "d"},
    ("PATCH", "/api/v1/sessions/{s}/tasks/{t}"): {"if_version": 1, "title": "t2"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/claim"): {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/acknowledge"): {},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reclaim"): {"reason": "r"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/note"): {"fence": "T1g1-aaaaaaaaaa", "note": "n"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/check"): {"fence": "T1g1-aaaaaaaaaa", "item": 1},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/review"): {"verdict": "approve", "submission_seq": 1},
    ("POST", "/api/v1/sessions/{s}/approvals/{seq}"): {"decision": "grant"},
    ("POST", "/api/v1/sessions/{s}/leases"): {"resource": "docs/faq.md"},
    ("POST", "/api/v1/sessions/{s}/invites"): {"agent": "helper"},
    ("POST", "/api/v1/join"): {"code": "AAAA-AAAA-AAAA"},
    ("POST", "/api/v1/sessions/{s}/leases/release"): {"resource": "docs/faq.md"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/block"): {"fence": "T1g1-aaaaaaaaaa", "reason": "r"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/unblock"): {"fence": "T1g1-aaaaaaaaaa"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/submit"): {"fence": "T1g1-aaaaaaaaaa", "summary": "done"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/release"): {"fence": "T1g1-aaaaaaaaaa"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/accept"): {"if_version": 1},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/return"): {"if_version": 1, "submission_seq": 1, "feedback": "f"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reassign"): {"if_version": 1, "to": "ana"},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/cancel"): {"if_version": 1},
    ("POST", "/api/v1/sessions/{s}/tasks/{t}/reopen"): {"if_version": 1},
    ("POST", "/api/v1/sessions/{s}/checkpoints"): {"summary": "s", "next_action": "n"},
    ("POST", "/api/v1/sessions/{s}/state"): {"state": "paused", "reason": "r"},
}


def route_has_body(app, method, path):
    return any(r.dependant.body_params for r in api_routes(app) if r.path == path and method in r.methods)


def session_routes(app, methods=("GET", "POST", "PATCH", "DELETE", "PUT")):
    """Session routes of the bearer-token API. Dashboard routes (/s/...) use cookies and have
    their own tests (test_dashboard.py)."""
    for r in api_routes(app):
        if "{s}" in r.path and r.path.startswith("/api/"):
            for m in sorted(r.methods):
                if m in methods:
                    yield m, r.path


def fill_path(path, s="s1", **params):
    url = path.replace("{s}", s)
    for name, val in {"handle:path": "ana", "handle": "ana", "t": "1", "seq": "1", "id": "x", **params}.items():
        url = url.replace("{" + name + "}", val)
    return url


def prepare_path(gb, method, path) -> dict:
    """Create whatever a route's path refers to, so walkers reach the check they test instead of
    an earlier 404 for a missing referenced object (checks run in a fixed order). Needs jose
    (coordinator) and ana as members of s1."""
    if "/tasks/{t}" in path:
        r = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose", json={"title": "t", "scope": "s", "deliverable": "d"},
                   rec=gb.rec())
        assert r.status_code == 201, r.text
        return {"t": str(r.json()["task"])}
    if path.endswith("/questions/{seq}/resolve"):
        r = gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "question", "body": "q?", "to": ["ana"]},
                   rec=gb.rec())
        assert r.status_code == 201, r.text
        return {"seq": str(r.json()["seq"])}
    if path.endswith("/approvals/{seq}"):
        r = gb.req("POST", "/api/v1/sessions/s1/messages", "ana", json={"kind": "approval", "body": "may I?", "to": ["jose"]},
                   rec=gb.rec())
        assert r.status_code == 201, r.text
        return {"seq": str(r.json()["seq"])}
    return {}


# --- a live uvicorn + real CLI subprocesses --------------------------------------------
import os as _os
import socket as _socket
import subprocess as _subprocess
import sys as _sys
import threading as _threading
import time as _time


def free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    """uvicorn on 127.0.0.1:<port> over the Board's database; can stop and start again on the same
    port and files (a server restart). `cli(who, *args)` runs the real CLI in a subprocess
    with its own HOME/XDG dirs per principal, logged in over plain http (loopback only)."""

    def __init__(self, gb, tmp_path):
        self.gb, self.tmp, self.port, self.homes = gb, tmp_path, free_port(), {}
        self.server = self.thread = None

    def start(self):
        import uvicorn
        app = create_app(self.gb.db, allowed_hosts=["127.0.0.1"])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = _threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            _time.sleep(0.025)
        raise RuntimeError("uvicorn did not start")

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.thread.join(timeout=10)
            self.server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def env(self, who):
        if who not in self.homes:
            home = self.tmp / f"home-{who.replace('/', '-')}"
            home.mkdir()
            env = {"PATH": _os.environ["PATH"], "HOME": str(home), "XDG_CONFIG_HOME": str(home / "cfg"),
                   "XDG_STATE_HOME": str(home / "state")}
            tf = home / "tok"
            tf.write_text(self.gb.tok[who] + "\n")
            r = _subprocess.run([_sys.executable, "-m", "glassboard.cli", "login", "--url", self.url,
                                 "--token-file", str(tf)], env=env, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            self.homes[who] = env
        return self.homes[who]

    def cli(self, who, *args, stdin=None, extra_env=None):
        return _subprocess.run([_sys.executable, "-m", "glassboard.cli", *args], env={**self.env(who), **(extra_env or {})},
                               capture_output=True, text=True, timeout=60, input=stdin)


@pytest.fixture
def live_server(gb, tmp_path):
    srv = LiveServer(gb, tmp_path).start()
    yield srv
    srv.stop()
