#!/usr/bin/env python3
"""Recovery drills D1-D8: concurrent claims, a killed agent, a server killed mid-request, a second
process, staleness, pause, backup and restore on a separate instance, offline work.

Runs each drill against a throwaway local board: its own database in a temporary directory,
uvicorn on 127.0.0.1 (plain http on loopback only, as the CLI allows), and the real participant
CLI (`python -m glassboard.cli`) as the agents, each principal with its own HOME. Agents are
simulated by CLI processes; the drills check what the BOARD and the CLI guarantee. Use the checks
here as the expected results when you rehearse the same situations with real agents.

    python scripts/drills/drills.py               # D1-D4, D6-D8 (about a minute)
    python scripts/drills/drills.py --slow        # also D5 (staleness needs the 300 s minimum)
    python scripts/drills/drills.py D3 D7b        # only these
    python scripts/drills/drills.py --keep        # keep the temp directory (board.db, logs, homes)

Exit status = number of failed checks. Every check prints PASS/FAIL with its evidence line.
Nothing here touches anything outside the temporary directory; no network beyond 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

PY = sys.executable
SESSION = "drills"
HUMANS = ("jose", "ana")
AGENTS = ("jose/claude", "ana/codex")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, bool, str]] = []

    def check(self, drill: str, what: str, ok: bool, evidence: str = "") -> bool:
        self.rows.append((drill, what, bool(ok), evidence))
        flag = "PASS" if ok else "FAIL"
        print(f"{flag} {drill}: {what}" + (f"  [{evidence}]" if evidence else ""), flush=True)
        return bool(ok)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if not r[2])


class Board:
    """A local Glassboard instance on its own files; can be killed (-9), restarted, restored."""

    def __init__(self, root: str, name: str = "board", sweep_s: float = 60.0):
        self.root, self.name, self.sweep_s = root, name, sweep_s
        self.db = os.path.join(root, f"{name}.db")
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log = open(os.path.join(root, f"{name}.log"), "ab")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def admin(self, *args: str) -> str:
        r = subprocess.run([PY, "-m", "glassboard.admin", "--db", self.db, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"glassboard-admin {args[0]} failed: {r.stderr.strip()[-600:]}")
        return r.stdout

    def start(self) -> "Board":
        env = {**os.environ, "GLASSBOARD_DB": self.db, "GLASSBOARD_ALLOWED_HOSTS": "127.0.0.1",
               "GLASSBOARD_SWEEP_INTERVAL_S": str(self.sweep_s)}
        env.pop("GLASSBOARD_MCP", None)
        self.proc = subprocess.Popen([PY, "-m", "uvicorn", "glassboard.serve:app", "--host", "127.0.0.1",
                                      "--port", str(self.port), "--workers", "1", "--log-level", "warning"],
                                     env=env, stdout=self.log, stderr=self.log)
        for _ in range(400):
            try:
                urllib.request.urlopen(self.url + "/health/live", timeout=1)
                return self
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"board {self.name} exited at startup (see {self.log.name})")
                time.sleep(0.05)
        raise RuntimeError("board did not come up")

    def kill9(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill9()


class Principal:
    """One principal: a token file and a HOME for the CLI (its config, credentials, state)."""

    def __init__(self, root: str, board: Board, handle: str):
        self.handle, self.board = handle, board
        self.home = os.path.join(root, "home-" + handle.replace("/", "-"))
        os.makedirs(self.home, exist_ok=True)
        self.token_file = os.path.join(self.home, "token")
        board.admin("mint-token", handle, "--out", self.token_file)
        with open(self.token_file) as f:
            self.token = f.read().strip()

    def env(self, runtime: str | None = None) -> dict:
        e = {"PATH": os.environ.get("PATH", ""), "HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, "cfg"),
             "XDG_STATE_HOME": os.path.join(self.home, "state")}
        if runtime:
            e["GLASSBOARD_RUNTIME"] = runtime
        return e

    def login(self, url: str) -> None:
        r = self.cli("login", "--url", url, "--token-file", self.token_file)
        if r.returncode != 0:
            raise RuntimeError(f"login {self.handle}: {r.stderr.strip()[:300]}")

    def cli(self, *args: str, runtime: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([PY, "-m", "glassboard.cli", *args], env=self.env(runtime), capture_output=True,
                              text=True, timeout=timeout)

    def popen(self, *args: str, runtime: str | None = None) -> subprocess.Popen:
        return subprocess.Popen([PY, "-m", "glassboard.cli", *args], env=self.env(runtime),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def api(board: Board, who: Principal, method: str, path: str, body=None, rec: str | None = None,
        key: str | None = None, runtime: str | None = None, raw: bool = False):
    """One HTTP call; returns (status, lower-cased headers, parsed JSON or text). Never raises on HTTP errors."""
    h = {"Authorization": f"Bearer {who.token}"}
    if method != "GET":
        h["Idempotency-Key"] = key or uuid.uuid4().hex
        h["Content-Type"] = "application/json"
    if rec:
        h["X-Glassboard-Recovery"] = rec
    if runtime:
        h["X-Glassboard-Runtime"] = runtime
    data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
    req = urllib.request.Request(board.url + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, headers, payload = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, headers, payload = e.code, dict(e.headers), e.read()
    headers = {k.lower(): v for k, v in headers.items()}
    if raw:
        return status, headers, payload
    try:
        return status, headers, json.loads(payload)
    except ValueError:
        return status, headers, payload.decode("utf-8", "replace")


class Drill:
    """A fresh board and session per drill, so drills never depend on each other."""

    def __init__(self, root: str, report: Report, name: str, sweep_s: float = 60.0, stale_after_s: int = 1800):
        self.root = os.path.join(root, name)
        os.makedirs(self.root)
        self.report, self.name = report, name
        self.board = Board(self.root, sweep_s=sweep_s)
        for h in HUMANS:
            self.board.admin("add-human", h)
        for h in AGENTS:
            owner, agent = h.split("/")
            self.board.admin("add-agent", owner, agent)
        self.p = {h: Principal(self.root, self.board, h) for h in (*HUMANS, *AGENTS)}
        self.board.start()
        self.call("jose", "POST", "/api/v1/sessions", {"handle": SESSION, "title": f"Drill {name}",
                                                      "stale_after_s": stale_after_s}, expect=201)
        self.call("jose", "POST", f"/api/v1/sessions/{SESSION}/members", {"handle": "ana", "role": "coordinator"},
                  rec=self.rec(), expect=200)
        for h in AGENTS:
            self.call("jose", "POST", f"/api/v1/sessions/{SESSION}/members", {"handle": h, "role": "member"},
                      rec=self.rec(), expect=200)
        for h in (*HUMANS, *AGENTS):
            self.p[h].login(self.board.url)
            r = self.cli(h, "use", SESSION)
            if r.returncode != 0:
                raise RuntimeError(f"use {h}: {r.stderr.strip()[:300]}")

    # helpers -------------------------------------------------------------------------------------
    def check(self, what, ok, evidence=""):
        return self.report.check(self.name, what, ok, evidence)

    def call(self, who, method, path, body=None, rec=None, key=None, runtime=None, expect=None, raw=False):
        st, h, b = api(self.board, self.p[who], method, path, body, rec, key, runtime, raw)
        if expect is not None and st != expect:
            raise RuntimeError(f"{method} {path} as {who}: HTTP {st} (expected {expect}): {str(b)[:300]}")
        return st, h, b

    def cli(self, who, *args, runtime=None, timeout=60):
        return self.p[who].cli(*args, runtime=runtime, timeout=timeout)

    def rec(self) -> str:
        _, _, b = self.call("jose", "GET", "/api/v1/sessions", expect=200)
        return next(s["recovery_id"] for s in b["sessions"] if s["session"] == SESSION)

    def events(self, types=None) -> list[dict]:
        out, after = [], 0
        while True:
            _, _, b = self.call("jose", "GET", f"/api/v1/sessions/{SESSION}/events?after={after}&limit=100", expect=200)
            out += b["events"]
            if len(b["events"]) < 100:
                break
            after = b["events"][-1]["seq"]
        return [e for e in out if types is None or e["type"] in types]

    def task(self, title="Drill task", assignee=None, **extra) -> int:
        body = {"title": title, "scope": "Update docs/onboarding.md", "deliverable": "a short note", **extra}
        if assignee:
            body["assignee"] = assignee
        _, _, b = self.call("jose", "POST", f"/api/v1/sessions/{SESSION}/tasks", body, rec=self.rec(), expect=201)
        return b["task"]

    def close(self):
        self.board.stop()


def err_code(r: subprocess.CompletedProcess) -> str:
    """The `<code>` of the CLI's canonical error line `ERROR <code>: <message>` (or "")."""
    for line in (r.stderr + r.stdout).splitlines():
        if line.startswith("ERROR ") and ":" in line:
            return line[6:].split(":", 1)[0]
    return ""


# --- D1 -------------------------------------------------------------------------------------------

def d1(d: Drill):
    """Two principals claim the same task simultaneously: one 201, one task_already_claimed."""
    n = d.task("Shared task")
    procs = [d.p[h].popen("task", "claim", f"T{n}", runtime=f"{h.split('/')[1][:3]}1") for h in AGENTS]
    results = [(p.wait(timeout=60), *p.communicate()) for p in procs]
    ok = [r for r in results if r[0] == 0]
    lost = [r for r in results if r[0] != 0]
    codes = [err_code(subprocess.CompletedProcess([], r[0], r[1], r[2])) for r in lost]
    d.check("exactly one claim succeeds", len(ok) == 1, f"exit codes {[r[0] for r in results]}")
    d.check("the other gets task_already_claimed", codes == ["task_already_claimed"], f"codes {codes}")
    claimed = d.events({"task.claimed"})
    d.check("exactly one task.claimed event", len(claimed) == 1, f"{len(claimed)} event(s)")


# --- D2 -------------------------------------------------------------------------------------------

def _inbox_seqs(r: subprocess.CompletedProcess) -> list[int]:
    return [e["seq"] for e in json.loads(r.stdout)["inbox"]]


def _wait_until(pred, timeout=15.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def d2(d: Drill):
    """An agent syncs and is killed before acking: redelivery; ack beyond delivered -> 409; no duplicate posts."""
    _, _, m = d.call("ana", "POST", f"/api/v1/sessions/{SESSION}/messages",
                     {"kind": "question", "body": "Which section of the onboarding doc is unclear?", "to": ["jose/claude"]},
                     rec=d.rec(), expect=201)
    r1 = d.cli("jose/claude", "sync", "--json", runtime="cl1")
    d.check("first sync delivers the message (not acked)", r1.returncode == 0 and m["seq"] in _inbox_seqs(r1),
            f"exit {r1.returncode}")
    waiting = d.p["jose/claude"].popen("sync", "--wait", "20", runtime="cl1")
    time.sleep(1.0)
    waiting.send_signal(signal.SIGKILL)                      # killed mid long-poll, before any ack
    waiting.wait(timeout=10)
    r2 = d.cli("jose/claude", "sync", "--json", runtime="cl1")
    d.check("after the kill, the next sync redelivers it (at-least-once)",
            r2.returncode == 0 and m["seq"] in _inbox_seqs(r2), f"exit {r2.returncode}")
    delivered = json.loads(r2.stdout)["delivered_through"]
    r3 = d.cli("jose/claude", "sync", "--ack", str(delivered + 50), runtime="cl1")
    d.check("ack beyond the delivered boundary is refused (409 ack_beyond_delivered)",
            r3.returncode == 5 and err_code(r3) == "ack_beyond_delivered", f"exit {r3.returncode} {err_code(r3)}")
    r4 = d.cli("jose/claude", "sync", "--ack", "--json", runtime="cl1")
    r5 = d.cli("jose/claude", "sync", "--json", runtime="cl1")
    d.check("after a valid ack the message is not redelivered",
            r4.returncode == 0 and r5.returncode == 0 and m["seq"] not in _inbox_seqs(r5), f"exits {r4.returncode} {r5.returncode}")

    # No duplicate posts: the answer's response is lost (agent killed while the server is frozen), the
    # restarted agent's next command resends the queued operation with the SAME op ID.
    body = "Section 3 (login) needs the CA step first."
    os.kill(d.board.proc.pid, signal.SIGSTOP)
    try:
        poster = d.p["jose/claude"].popen("post", "answer", body, "--re", str(m["seq"]), runtime="cl1")
        outbox = os.path.join(d.p["jose/claude"].home, "state")
        _wait_until(lambda: any("outbox" in f for _, _, fs in os.walk(outbox) for f in fs), timeout=10)
        time.sleep(0.5)                                       # the request is on the wire, no response yet
        poster.send_signal(signal.SIGKILL)
        poster.wait(timeout=10)
    finally:
        os.kill(d.board.proc.pid, signal.SIGCONT)
    # The restarted agent is a new runtime: it takes the inbox over explicitly; every command first
    # flushes the outbox, so the answer is resent with its original op ID.
    r6 = d.cli("jose/claude", "sync", "--takeover", "previous runtime was killed", runtime="cl2")
    answers = [e for e in d.events({"answer"}) if e.get("body") == body]
    d.check("the resent answer exists exactly once (no duplicate post)", r6.returncode == 0 and len(answers) == 1,
            f"exit {r6.returncode}, {len(answers)} answer(s)")
    rob = d.cli("jose/claude", "outbox")
    d.check("outbox is empty afterwards", rob.returncode == 0 and rob.stdout.startswith("outbox: 0 pending"),
            rob.stdout.strip().splitlines()[0] if rob.stdout else rob.stderr.strip()[:120])


# --- D3 -------------------------------------------------------------------------------------------

def d3(d: Drill):
    """kill -9 the server between commit and response: the retry with the same op returns the identical receipt."""
    path = f"/api/v1/sessions/{SESSION}/messages"
    msg = {"kind": "update", "body": "Draft of section 2 is ready for review."}
    key = uuid.uuid4().hex
    st1, h1, b1 = d.call("jose/claude", "POST", path, msg, rec=d.rec(), key=key, raw=True)
    d.board.kill9()                                           # (a) crash right after the commit
    d.board.start()
    st2, h2, b2 = d.call("jose/claude", "POST", path, msg, rec=d.rec(), key=key, raw=True)
    d.check("(a) retry after kill -9 returns the byte-identical receipt",
            (st1, b1) == (st2, b2) and st1 == 201, f"HTTP {st1}/{st2}, {len(b1)}/{len(b2)} bytes")
    d.check("(a) the retry carries Glassboard-Replayed: true", h2.get("glassboard-replayed") == "true"
            and "glassboard-replayed" not in h1, f"{h2.get('glassboard-replayed')!r}")

    # (b) the response never reaches the client: freeze the server, send, give up, let it commit, kill -9.
    msg2 = {"kind": "update", "body": "Section 4 has a new example."}
    key2 = uuid.uuid4().hex
    rec = d.rec()
    os.kill(d.board.proc.pid, signal.SIGSTOP)
    try:
        lost = subprocess.Popen([PY, "-c", "import json,sys,urllib.request as u\n"
                                 "r=u.Request(sys.argv[1],data=sys.argv[2].encode(),method='POST',headers=json.loads(sys.argv[3]))\n"
                                 "u.urlopen(r,timeout=60)",
                                 d.board.url + path, json.dumps(msg2),
                                 json.dumps({"Authorization": f"Bearer {d.p['jose/claude'].token}", "Idempotency-Key": key2,
                                             "X-Glassboard-Recovery": rec, "Content-Type": "application/json"})],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        lost.kill()                                           # the client gave up: no response received
        lost.wait(timeout=10)
    finally:
        os.kill(d.board.proc.pid, signal.SIGCONT)
    committed = _wait_until(lambda: any(e.get("body") == msg2["body"] for e in d.events({"update"})), timeout=10)
    d.board.kill9()
    d.board.start()
    st3, h3, b3 = d.call("jose/claude", "POST", path, msg2, rec=rec, key=key2, raw=True)
    st4, h4, b4 = d.call("jose/claude", "POST", path, msg2, rec=rec, key=key2, raw=True)
    n = len([e for e in d.events({"update"}) if e.get("body") == msg2["body"]])
    d.check("(b) the server committed although the client never got the response", committed)
    d.check("(b) retry after kill -9 is a replay (201, Glassboard-Replayed: true), stable bytes",
            st3 == st4 == 201 and h3.get("glassboard-replayed") == "true" and b3 == b4, f"HTTP {st3}/{st4}")
    d.check("(b) exactly one event for the operation", n == 1, f"{n} event(s)")


# --- D4 -------------------------------------------------------------------------------------------

def d4(d: Drill):
    """A second process for jose/claude: consumer_conflict until explicit takeover; fences independent."""
    n = d.task("Review the protocol doc", assignee="jose/claude")
    A, B = "cl1", "cl2"
    ra = d.cli("jose/claude", "sync", runtime=A)
    rc = d.cli("jose/claude", "task", "claim", f"T{n}", runtime=A)
    d.check("runtime A attaches and claims", ra.returncode == 0 and rc.returncode == 0, f"exits {ra.returncode} {rc.returncode}")
    rb = d.cli("jose/claude", "sync", runtime=B)
    d.check("runtime B gets consumer_conflict", rb.returncode == 5 and err_code(rb) == "consumer_conflict",
            f"exit {rb.returncode} {err_code(rb)}")
    rt = d.cli("jose/claude", "sync", "--takeover", "runtime A stopped answering", runtime=B)
    d.check("explicit takeover by B succeeds", rt.returncode == 0, f"exit {rt.returncode} {err_code(rt)}")
    ra2 = d.cli("jose/claude", "sync", runtime=A)
    d.check("old runtime A now gets consumer_mismatch", ra2.returncode == 5 and err_code(ra2) == "consumer_mismatch",
            f"exit {ra2.returncode} {err_code(ra2)}")
    rn = d.cli("jose/claude", "task", "note", f"T{n}", "Still reading section 1.", runtime=A)
    d.check("the task fence is still A's until reclaim (A's note succeeds)", rn.returncode == 0, f"exit {rn.returncode} {err_code(rn)}")
    rr = d.cli("jose/claude", "task", "reclaim", f"T{n}", "runtime A replaced", runtime=B)
    d.check("B reclaims the task", rr.returncode == 0, f"exit {rr.returncode} {err_code(rr)}")
    rn2 = d.cli("jose/claude", "task", "note", f"T{n}", "Another note from A.", runtime=A)
    d.check("A's old fence is refused (fence_mismatch)", rn2.returncode == 5 and err_code(rn2) == "fence_mismatch",
            f"exit {rn2.returncode} {err_code(rn2)}")
    types = [e["type"] for e in d.events({"consumer.attached", "consumer.takeover", "task.claimed", "task.reclaimed"})]
    d.check("all of it is visible in the log", types == ["consumer.attached", "task.claimed", "consumer.takeover", "task.reclaimed"],
            ", ".join(types))
    hist = d.cli("jose", "history")
    secret_like = [w for w in (hist.stdout + rt.stdout + rr.stdout).split() if
                   __import__("re").fullmatch(r"(T\d+g\d+|C\d+)-[a-z2-7]{10}", w.strip("«»,.()"))]
    d.check("no fence or consumer secret printed by the CLI", not secret_like,
            f"{len(secret_like)} found" + (f" (shape {secret_like[0].split('-')[0]}-...)" if secret_like else ""))


# --- D5 (slow) ------------------------------------------------------------------------------------

def d5(d: Drill):
    """Claimant idle past the threshold: one task.stale; coordinator reassigns; old fence rejected."""
    n = d.task("Write the FAQ", assignee="jose/claude", stale_after_s=300)
    rc = d.cli("jose/claude", "task", "claim", f"T{n}", runtime="cl1")
    d.check("claimed", rc.returncode == 0, f"exit {rc.returncode}")
    t0 = time.monotonic()
    seen = _wait_until(lambda: d.events({"task.stale"}), timeout=420, step=5)
    d.check("task.stale appears after the 300 s threshold", seen and time.monotonic() - t0 >= 295,
            f"after {time.monotonic() - t0:.0f} s")
    time.sleep(3 * d.board.sweep_s)
    stale = d.events({"task.stale"})
    d.check("exactly one task.stale for the idle period", len(stale) == 1, f"{len(stale)} event(s)")
    rr = d.cli("jose", "task", "reassign", f"T{n}", "--to", "ana/codex")
    d.check("coordinator reassigns", rr.returncode == 0, f"exit {rr.returncode} {err_code(rr)}")
    rn = d.cli("jose/claude", "task", "note", f"T{n}", "Back now.", runtime="cl1")
    d.check("the old claimant's fence is rejected", rn.returncode != 0 and err_code(rn) in ("fence_mismatch", "no_fence"),
            f"exit {rn.returncode} {err_code(rn)}")


# --- D6 -------------------------------------------------------------------------------------------

def d6(d: Drill):
    """Pause mid-task: claim/note/reclaim rejected; checkpoint, release, post, sync allowed."""
    n = d.task("Edit the glossary", assignee="jose/claude")
    n2 = d.task("Spare task")
    d.cli("jose/claude", "sync", runtime="cl1")
    rc = d.cli("jose/claude", "task", "claim", f"T{n}", runtime="cl1")
    rp = d.cli("jose", "session", "pause", "lunch break")
    d.check("claimed, then the coordinator pauses", rc.returncode == 0 and rp.returncode == 0,
            f"exits {rc.returncode} {rp.returncode} {err_code(rp)}")
    for label, args, rt in (("claim", ("task", "claim", f"T{n2}"), "cl1"),
                            ("note", ("task", "note", f"T{n}", "Halfway."), "cl1"),
                            ("reclaim", ("task", "reclaim", f"T{n}", "new runtime"), "cl2")):
        r = d.cli("jose/claude", *args, runtime=rt)
        d.check(f"{label} rejected (session_paused)", r.returncode == 5 and err_code(r) == "session_paused",
                f"exit {r.returncode} {err_code(r)}")
    for label, args in (("checkpoint", ("checkpoint", "save", "--summary", "Glossary half done", "--next", "Finish G-Z")),
                        ("post", ("post", "update", "Paused at letter F.")),
                        ("sync", ("sync",)),
                        ("release", ("task", "release", f"T{n}", "Paused; handing back."))):
        r = d.cli("jose/claude", *args, runtime="cl1")
        d.check(f"{label} allowed while paused", r.returncode == 0, f"exit {r.returncode} {err_code(r)}")


# --- D7 / D7b -------------------------------------------------------------------------------------

def _restore_elsewhere(d: Drill, backup: str) -> Board:
    # A separate instance = its own directory (the serving lock is per directory: backup.serving_lock_path).
    where = os.path.join(d.root, "restored")
    os.makedirs(where)
    other = Board(where, name="restored", sweep_s=d.board.sweep_s)
    jcopy = backup + ".journal"           # exists only once some access reduction was journaled
    other.admin("restore", backup, *(("--journal-from", jcopy) if os.path.exists(jcopy) else ()))
    return other.start()


def d7(d: Drill):
    """Backup, 5 more events, restore on a separate instance."""
    n = d.task("Proofread section 5", assignee="jose/claude")
    d.cli("jose/claude", "sync", runtime="cl1")
    d.cli("jose/claude", "task", "claim", f"T{n}", runtime="cl1")
    old_rec = d.rec()
    backup = os.path.join(d.root, "drill.backup")
    d.board.admin("backup", backup, "--no-encrypt")
    lost = [f"Post-backup update {i}" for i in range(1, 6)]
    for body in lost:
        d.call("jose/claude", "POST", f"/api/v1/sessions/{SESSION}/messages", {"kind": "update", "body": body},
               rec=old_rec, expect=201)
    other = _restore_elsewhere(d, backup)
    try:
        d.board, original = other, d.board                     # all helpers now talk to the restored instance
        _, _, b = d.call("jose", "GET", "/api/v1/sessions", expect=200)
        s = next(x for x in b["sessions"] if x["session"] == SESSION)
        d.check("new recovery ID", s["recovery_id"] != old_rec, f"{old_rec} -> {s['recovery_id']}")
        d.check("session paused", s["state"] == "paused", s["state"])
        _, _, t = d.call("jose", "GET", f"/api/v1/sessions/{SESSION}/tasks/{n}", expect=200)
        d.check("task fence cleared (gen +1, claimant kept)", t["gen"] == 2 and t["status"] == "in_progress",
                f"gen {t['gen']} {t['status']}")
        d.p["jose/claude"].login(other.url)                   # the agent's laptop now reaches the restored board
        rn = d.cli("jose/claude", "task", "note", f"T{n}", "Continuing.", "-s", SESSION, runtime="cl1")
        d.check("the client stops: recovery_mismatch (exit 7)", rn.returncode == 7 and err_code(rn) == "recovery_mismatch",
                f"exit {rn.returncode} {err_code(rn)}")
        restored = d.events({"system.restored"})
        d.check("one system.restored event", len(restored) == 1, f"{len(restored)}")
        bodies = {e.get("body") for e in d.events()}
        d.check("the 5 post-backup events are absent", not bodies & set(lost), f"{len(bodies & set(lost))} present")
        st, _, j = d.call("jose/claude", "POST", f"/api/v1/sessions/{SESSION}/sync", {}, rec=s["recovery_id"], runtime="cl9")
        d.check("inbox consumers cleared (a fresh attach succeeds without takeover)", st == 200 and j.get("consumer"),
                f"HTTP {st}")
    finally:
        other.stop()
        d.board = original


def d7b(d: Drill):
    """A client with outbox entries meets the restored instance: recovery_mismatch -> quarantine -> reconcile."""
    n = d.task("Fix the typo list", assignee="jose/claude")
    d.cli("jose/claude", "task", "claim", f"T{n}", runtime="cl1")
    backup = os.path.join(d.root, "drill.backup")
    d.board.admin("backup", backup, "--no-encrypt")
    d.board.stop()                                             # the board goes away; the agent keeps working offline
    q1 = d.cli("jose/claude", "post", "update", "Typo list: 4 fixed.", runtime="cl1")
    q2 = d.cli("jose/claude", "task", "note", f"T{n}", "Two typos left.", runtime="cl1")
    d.check("offline commands queue (exit 4)", q1.returncode == 4 and q2.returncode == 4, f"exits {q1.returncode} {q2.returncode}")
    other = _restore_elsewhere(d, backup)
    try:
        d.board, original = other, d.board
        for h in ("jose", "jose/claude"):
            d.p[h].login(other.url)
        rs = d.cli("jose/claude", "sync", "-s", SESSION, runtime="cl1")
        d.check("recovery_mismatch, exit 7", rs.returncode == 7 and err_code(rs) == "recovery_mismatch",
                f"exit {rs.returncode} {err_code(rs)}")
        rec = d.cli("jose/claude", "reconcile", "-s", SESSION, runtime="cl1")
        ops = [ln.split("«", 1)[1].split()[0] for ln in rec.stdout.splitlines() if ln.startswith("  · op: «")]
        d.check("reconcile lists both queued operations in quarantine", rec.returncode == 0 and len(ops) == 2,
                f"{len(ops)} op(s)")
        post_op, note_op = (ops + ["?", "?"])[:2]
        rr = d.cli("jose/claude", "reconcile", "-s", SESSION, "--resend", post_op, runtime="cl1")
        d.check("--resend of the post applies it under the new recovery ID", rr.returncode == 0,
                f"exit {rr.returncode} {err_code(rr)}")
        rr2 = d.cli("jose/claude", "reconcile", "-s", SESSION, "--resend", post_op, runtime="cl1")
        n_posts = len([e for e in d.events({"update"}) if e.get("body") == "Typo list: 4 fixed."])
        d.check("exactly one copy on the board", n_posts == 1 and rr2.returncode != 0, f"{n_posts} copies")
        rf = d.cli("jose/claude", "reconcile", "-s", SESSION, "--resend", note_op, runtime="cl1")
        d.check("the fenced note is refused (fence_dead; reclaim first)", rf.returncode == 2 and err_code(rf) == "fence_dead",
                f"exit {rf.returncode} {err_code(rf)}")
    finally:
        other.stop()
        d.board = original


# --- D8 -------------------------------------------------------------------------------------------

def d8(d: Drill, offline_s: float = 3.0):
    """Laptop offline (no restore): the outbox replays in order with the same recovery ID."""
    rec_before = d.rec()
    d.board.stop()
    bodies = [f"Offline note {i}: section {i} reviewed." for i in (1, 2, 3)]
    codes = [d.cli("jose/claude", "post", "note", b, runtime="cl1").returncode for b in bodies]
    ob = d.cli("jose/claude", "outbox")
    d.check("three posts queued while offline (exit 4)", codes == [4, 4, 4] and "outbox: 3 pending" in ob.stdout,
            f"exits {codes}; {ob.stdout.strip().splitlines()[0] if ob.stdout else ''}")
    time.sleep(offline_s)
    d.board.start()
    rs = d.cli("jose/claude", "sync", runtime="cl1")
    notes = [e for e in d.events({"note"}) if e.get("body") in bodies]
    d.check("the next command flushes the outbox", rs.returncode == 0, f"exit {rs.returncode} {err_code(rs)}")
    d.check("replayed in order, once each", [e["body"] for e in notes] == bodies, f"{len(notes)} note(s)")
    d.check("same recovery ID (no restore happened)", d.rec() == rec_before)
    ob2 = d.cli("jose/claude", "outbox")
    d.check("outbox empty", ob2.stdout.startswith("outbox: 0 pending"), ob2.stdout.strip().splitlines()[0] if ob2.stdout else "")


DRILLS = {"D1": d1, "D2": d2, "D3": d3, "D4": d4, "D5": d5, "D6": d6, "D7": d7, "D7B": d7b, "D8": d8}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Glassboard recovery drills D1-D8 on a throwaway local board")
    ap.add_argument("drills", nargs="*", help="drill IDs (default: all; D5 only with --slow)")
    ap.add_argument("--slow", action="store_true", help="include D5 (about 6 minutes)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    a = ap.parse_args(argv)
    names = [x.upper() for x in a.drills] or [k for k in DRILLS if k != "D5" or a.slow]
    unknown = [x for x in names if x not in DRILLS]
    if unknown:
        ap.error(f"unknown drill(s): {', '.join(unknown)}")
    root = tempfile.mkdtemp(prefix="gb-drills-")
    report = Report()
    try:
        for name in names:
            d = None
            try:
                d = Drill(root, report, name, **SETUP.get(name, {}))
                DRILLS[name](d)
            except Exception as e:                      # a crashed drill is a failed drill, with the reason
                report.check(name, "drill ran to completion", False, f"{type(e).__name__}: {str(e)[:300]}")
            finally:
                if d:
                    d.close()
    finally:
        if a.keep:
            print(f"kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    total = len(report.rows)
    print(f"drills: {total - report.failures}/{total} checks passed ({', '.join(names)})")
    return report.failures


SETUP: dict[str, dict] = {"D5": {"sweep_s": 5.0}}

if __name__ == "__main__":
    sys.exit(main())
