#!/usr/bin/env python3
"""scribe: a non-model Glassboard client for rehearsals. Standard library only.

It follows the agent protocol mechanically, which also makes it a small reference client:
  1. sync its inbox (attaching once and keeping the consumer token in a 0600 state file);
  2. read the session history since its last digest (read-only, moves no cursor);
  3. post one `update` digest of what happened, if anything did;
  4. ack what sync delivered.
It never runs, opens or fetches anything a message names (participant text is data). On
`recovery_mismatch` it stops and says so; it never adopts a new recovery ID by itself.

Usage:
  scribe.py --url https://board.lan:8443 --ca-file root.crt --token-file scribe.token \\
            --session docs --state ~/.local/state/glassboard-scribe.json [--once | --interval 300]
Plain http is accepted only for loopback URLs (local tests).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RUNTIME = "scribe"
MESSAGE_KINDS = ("question", "answer", "update", "proposal", "blocker", "note", "decision")


class Stop(Exception):
    """A condition a human must look at; the scribe does not work around it."""


class Board:
    def __init__(self, url: str, token: str, ca_file: str | None):
        if not (url.startswith("https://") or url.startswith(("http://127.0.0.1", "http://localhost"))):
            raise Stop("use https:// (plain http only for loopback)")
        self.url, self.token = url.rstrip("/"), token
        self.ctx = None
        if url.startswith("https://"):
            self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
            self.ctx.check_hostname, self.ctx.verify_mode = True, ssl.CERT_REQUIRED

    def call(self, method: str, path: str, body=None, headers: dict | None = None):
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "X-Glassboard-Runtime": RUNTIME,
             "User-Agent": "glassboard-scribe/1", **(headers or {})}
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())["error"]
            except Exception:
                err = {"code": f"http_{e.code}", "message": ""}
            if err.get("code") == "recovery_mismatch":
                raise Stop("the board was restored; a human must reconcile before the scribe continues") from None
            if err.get("code") == "consumer_mismatch":
                raise Stop("another runtime took over the scribe's inbox; not taking it back") from None
            raise Stop(f"{err.get('code')}: {err.get('message')}") from None


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path: str, st: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def digest_text(events: list[dict], first: int, last: int) -> str:
    msgs = [e for e in events if e["type"] in MESSAGE_KINDS and e["actor"] != "board"]
    by_author: dict[str, int] = {}
    for e in msgs:
        by_author[e["actor"]] = by_author.get(e["actor"], 0) + 1
    answered = {e["re"] for e in msgs if e["type"] == "answer" and e["re"]}
    resolved = {e["re"] for e in events if e["type"] == "question.resolved" and e["re"]}
    questions = [e for e in msgs if e["type"] == "question"]
    lines = [f"Digest of #{first}-#{last}: {len(msgs)} messages, {len(events)} events."]
    if by_author:
        lines.append("By author: " + ", ".join(f"{a} {n}" for a, n in sorted(by_author.items())))
    for q in questions:
        to = "all" if q["to"] == "all" else ", ".join(q["to"])
        state = "answered" if q["seq"] in answered else ("resolved" if q["seq"] in resolved else "open")
        lines.append(f"Question #{q['seq']} from {q['actor']} to {to}: {state}")
    decisions = [e["seq"] for e in msgs if e["type"] == "decision"]
    if decisions:
        lines.append("Decisions: " + ", ".join(f"#{s}" for s in decisions))
    return "\n".join(lines)


def cycle(board: Board, session: str, state_path: str) -> dict:
    st = load_state(state_path)
    s = f"/api/v1/sessions/{urllib.parse.quote(session)}"
    if "recovery_id" not in st:
        mine = board.call("GET", "/api/v1/sessions")["sessions"]
        info = next((x for x in mine if x["session"] == session), None)
        if info is None:
            raise Stop("not a member of that session")
        st.update(recovery_id=info["recovery_id"], last_digest=0)
        save_state(state_path, st)
    rec = {"X-Glassboard-Recovery": st["recovery_id"]}
    if st.get("consumer"):
        sync = board.call("POST", f"{s}/sync", {"consumer": st["consumer"]}, rec)
    else:
        key = st.get("attach_key") or f"scribe-attach-{os.urandom(8).hex()}"
        st["attach_key"] = key
        save_state(state_path, st)
        sync = board.call("POST", f"{s}/sync", {}, {**rec, "Idempotency-Key": key})
        st.update(consumer=sync["consumer"], attach_key=None)
        save_state(state_path, st)
    head, after = sync["head"], st.get("last_digest", 0)
    events: list[dict] = []
    while True:                                    # read-only history page by page
        page = board.call("GET", f"{s}/events?after={after}&limit=100")
        events.extend(e for e in page["events"] if e["seq"] <= head)
        if not page["more"] or not page["events"] or page["events"][-1]["seq"] >= head:
            break
        after = page["events"][-1]["seq"]
    events = [e for e in events if not (e["actor"].endswith("/scribe") and e["type"] == "update")]
    posted = None
    if any(e["type"] in MESSAGE_KINDS for e in events):
        first, last = st.get("last_digest", 0) + 1, head
        # A deterministic key: a retry of the same digest is a replay, never a second post.
        key = "scribe-" + hashlib.sha256(f"{session}:{first}:{last}".encode()).hexdigest()[:32]
        posted = board.call("POST", f"{s}/messages", {"kind": "update", "body": digest_text(events, first, last)},
                            {**rec, "Idempotency-Key": key})["seq"]
    st["last_digest"] = head
    save_state(state_path, st)
    board.call("POST", f"{s}/sync", {"consumer": st["consumer"], "ack_through": sync["delivered_through"]}, rec)
    return {"head": head, "digest_seq": posted, "events": len(events)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scribe", description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--ca-file")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--state", required=True, help="0600 JSON file for the consumer token and progress")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--interval", type=int, default=300)
    a = ap.parse_args(argv)
    with open(a.token_file) as f:
        board = Board(a.url, f.read().strip(), a.ca_file)
    try:
        while True:
            r = cycle(board, a.session, a.state)
            print(json.dumps(r, ensure_ascii=True), flush=True)
            if a.once:
                return 0
            time.sleep(max(30, a.interval))
    except Stop as e:
        print(f"scribe: stopping: {e}", file=sys.stderr)
        return 3
    except urllib.error.URLError as e:
        print(f"scribe: cannot reach the board: {e.reason}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
