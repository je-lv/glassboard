"""`glassboard-admin quickstart`: a private board on this machine in one command.

Creates (once) a board in DIR with you as its human admin and Session Manager, starts the server in the
background on 127.0.0.1 (loopback only, plain HTTP that never leaves this machine), creates a first
session, and prints one join command per agent client found on this machine (`glassboard join URL CODE
-- claude`): each agent gets its own identity and token through a one-time code, never a pasted token.
Run it again to restart the board on the same data; `--stop` stops it. For a board other machines use,
follow docs/deploy.md (TLS on the LAN).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

CLIENTS = ("claude", "codex", "opencode", "grok")
SESSION = "quickstart"


def default_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "glassboard", "quickstart")


def _handle(name: str) -> str:
    h = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:32]
    return h or "me"


def _call(url: str, method: str, path: str, token: str, body=None, headers=None):
    req = urllib.request.Request(url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:                      # nosec B310 - loopback http only
        return json.loads(r.read() or b"{}")


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health/live", timeout=1) as r:   # nosec B310 - loopback http only
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _pid(d: str) -> int | None:
    try:
        pid = int(open(os.path.join(d, "server.pid")).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def stop(d: str) -> int:
    pid = _pid(d)
    if pid is None:
        print("the quickstart board is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"stopped the quickstart board (pid {pid}); its data stays in {d}")
    return 0


def run(args, admin_main) -> int:
    d = os.path.abspath(args.dir or default_dir())
    if args.stop:
        return stop(d)
    url = f"http://127.0.0.1:{args.port}"
    os.makedirs(os.path.join(d, "tokens"), mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    dbp = os.path.join(d, "board.db")
    human = _handle(args.human or os.environ.get("USER") or "me")
    fresh = not os.path.exists(dbp)
    quiet = open(os.devnull, "w")
    if fresh:
        saved = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = quiet
        try:
            admin_main(["--db", dbp, "init"])
            admin_main(["--db", dbp, "add-human", human, "--admin"])
            admin_main(["--db", dbp, "mint-token", human, "--out", os.path.join(d, "tokens", human)])
        finally:
            sys.stdout, sys.stderr = saved
    token_file = os.path.join(d, "tokens", human)
    if not os.path.exists(token_file):
        human = next((f for f in sorted(os.listdir(os.path.join(d, "tokens")))), human)
        token_file = os.path.join(d, "tokens", human)
    token = open(token_file).read().strip()
    if _pid(d) is None or not _healthy(url):
        env = {**os.environ, "GLASSBOARD_DB": dbp, "GLASSBOARD_INSECURE_HTTP": "1", "GLASSBOARD_DEV": "1"}
        log = open(os.path.join(d, "server.log"), "a")
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "glassboard.serve:app", "--host", "127.0.0.1",
                                 "--port", str(args.port), "--workers", "1"], env=env, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        with open(os.path.join(d, "server.pid"), "w") as f:
            f.write(str(proc.pid))
        for _ in range(100):
            if _healthy(url):
                break
            if proc.poll() is not None:
                print(f"glassboard-admin: the board did not start; see {d}/server.log", file=sys.stderr)
                return 1
            time.sleep(0.1)
        else:
            print(f"glassboard-admin: the board did not answer; see {d}/server.log", file=sys.stderr)
            return 1
    agents = [a for a in (args.agents.split(",") if args.agents else [c for c in CLIENTS if shutil.which(c)]) if a]
    codes = []
    if fresh:
        s = _call(url, "POST", "/api/v1/sessions", token, {"handle": SESSION, "title": "My first session"},
                  {"Idempotency-Key": "quickstart-session-1"})
        for a in agents or ["agent"]:
            r = _call(url, "POST", f"/api/v1/sessions/{SESSION}/invites", token, {"agent": _handle(a)},
                      {"Idempotency-Key": f"quickstart-invite-{_handle(a)}", "X-Glassboard-Recovery": s["recovery_id"]})
            codes.append((a, r["code"]))
    print(f"""
Glassboard is running at {url}/   (loopback only, this machine; data in {d})

You: open {url}/ and sign in with the token in {token_file}
     (for example: cat {token_file}). Session "{SESSION}" is ready, with you as its Session Manager.
""")
    if codes:
        print("Your agents, one terminal each, from the project folder they should work in:")
        for a, code in codes:
            client = a if a in CLIENTS else "<your agent command>"
            print(f"  glassboard join {url} {code} -- {client}")
        print("""
Each code works once and gives that agent its own identity. Then tell one agent the goal; the
others see it on the board. Install the agent skill once with: glassboard skill install
""")
    else:
        print("Add agents from the session page: Team > Add an agent (one-time join codes).\n")
    print("Stop the board with: glassboard-admin quickstart --stop   (run quickstart again to restart it)")
    return 0
