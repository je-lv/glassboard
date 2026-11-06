"""Claude Code channels adapter (B32, ERRATA E40): push a fixed notice into an open Claude Code session
that sits idle at the prompt, where no hook fires. Stdlib only.

`glassboard claude-channel serve` is a minimal MCP stdio server that declares the `claude/channel`
capability (research preview in Claude Code). It arms a wake supervisor for the agent (a visible
`wake.armed`), waits on the non-consuming wake status, and for each new generation records
`wake.dispatched` and sends `notifications/claude/channel` with a fixed notice: never board content.
The agent then reads through `glassboard sync` and answers with `glassboard wake done`; a notice left
unanswered for 15 minutes pauses the supervisor visibly. Quiet hours (E38) hold notices back. When
Claude Code exits (stdin closes) the supervisor disarms visibly.

Refused, silently for the client but with a reason on stderr: a client not started through
`glassboard run` (its permission mode is unknown), or one started with a mode that would let the
notice act unattended (`--dangerously-skip-permissions`, `--permission-mode bypassPermissions|auto`).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from . import __version__, wakerun

NOTICE = ("New Glassboard work may be waiting for you (session {session}, through event #{generation}). "
          "Run `glassboard sync`, handle what it shows under your existing instructions, then run "
          "`glassboard wake done`.")
UNANSWERED_PAUSE_S = 900
DEBOUNCE_S = 3
UNATTENDED_ENV = "GLASSBOARD_CLIENT_UNATTENDED"
UNATTENDED_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
UNATTENDED_MODES = ("bypassPermissions", "auto")
SERVER = "glassboard"


def unattended(cmd: list[str]) -> bool:
    """Would this Claude Code command line let a pushed notice act without a human approving anything?"""
    for i, arg in enumerate(cmd[1:], 1):
        flag, _, value = arg.partition("=")
        if flag in UNATTENDED_FLAGS:
            return True
        if flag == "--permission-mode":
            value = value or (cmd[i + 1] if i + 1 < len(cmd) else "")
            if value in UNATTENDED_MODES:
                return True
    return False


def mcp_config(command: str = "glassboard") -> dict:
    return {"mcpServers": {SERVER: {"command": command, "args": ["claude-channel", "serve"]}}}


def install(project: str) -> tuple[str, bool]:
    """Merge the server into PROJECT/.mcp.json (other servers kept). Returns (path, changed)."""
    path = os.path.join(project, ".mcp.json")
    current = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        current = json.loads(text) if text.strip() else {}
        if not isinstance(current, dict) or not isinstance(current.get("mcpServers", {}), dict):
            raise ValueError(".mcp.json is not an object with an mcpServers object")
    servers = current.setdefault("mcpServers", {})
    if servers.get(SERVER) == mcp_config()["mcpServers"][SERVER]:
        return path, False
    servers[SERVER] = mcp_config()["mcpServers"][SERVER]
    tmp = path + ".glassboard-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path, True


class Server:
    def __init__(self, cli, a, out=None, inp=None):
        self.cli, self.a = cli, a
        self.out, self.inp = out or sys.stdout, inp or sys.stdin
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ctx = None

    # --- MCP (JSON-RPC 2.0, one message per line) --------------------------------------------
    def send(self, msg: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self.out.flush()

    def handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            self.send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": SERVER, "version": __version__},
                "instructions": ("Glassboard sends a short fixed notice on this channel when board work may be "
                                 "waiting for you. It never contains board content. Read the board with "
                                 "`glassboard sync`, act under the Glassboard protocol, then `glassboard wake done`.")}})
        elif method == "notifications/initialized":
            threading.Thread(target=self.poll, name="glassboard-channel", daemon=True).start()
        elif method in ("tools/list", "resources/list", "prompts/list"):
            key = method.split("/")[0]
            self.send({"jsonrpc": "2.0", "id": mid, "result": {key: []}})
        elif method == "ping":
            self.send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})

    def notify(self, session: str, generation: int) -> None:
        self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                   "params": {"content": NOTICE.format(session=session, generation=generation),
                              "meta": {"session": session, "generation": str(generation)}}})

    # --- the supervisor -------------------------------------------------------------------------
    def refusal(self) -> str | None:
        if not os.environ.get("GLASSBOARD_RUNTIME") or UNATTENDED_ENV not in os.environ:
            return "start Claude Code through `glassboard run` so its permission mode is known"
        if os.environ[UNATTENDED_ENV] == "1":
            return "Claude Code runs in a mode that approves on its own; a pushed notice would act unattended"
        return None

    def poll(self) -> None:
        why = self.refusal()
        if why:
            wakerun._log(f"channel refused: {why}")
            return
        agent_runtime = os.environ["GLASSBOARD_RUNTIME"]
        os.environ["GLASSBOARD_RUNTIME"] = ("c-" + agent_runtime)[:16]      # the adapter's own label
        try:
            self.ctx = ctx = self.cli.open_session(self.a)
            r = wakerun.arm(ctx, "claude")
            wakerun._log(f"channel armed · fence W{r['wake_gen']} · #{r['seq']}")
        except Exception as e:                                             # noqa: BLE001
            wakerun._log(f"channel not armed: {getattr(e, 'kind', e.__class__.__name__)}")
            return
        last = None
        while not self.stop.is_set():
            try:
                s = wakerun._status(ctx, wait=25, after=last)
                sup = s["supervisor"]
                if sup["state"] == "none" or s["session_state"] == "archived":
                    wakerun._log("channel stopped: the supervisor fence was released or the session archived")
                    return
                last = s["generation"]
                q = s.get("quiet")
                if (not s["pending"] or sup["state"] == "paused" or s["session_state"] == "paused"
                        or (q and q.get("blocks_dispatch"))):
                    continue
                if sup["outstanding"] is not None:
                    since = datetime.strptime(sup["dispatched_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - since).total_seconds() > UNANSWERED_PAUSE_S:
                        wakerun.control(ctx, "pause", "the open session did not answer the channel notice in 15 minutes")
                    continue
                time.sleep(DEBOUNCE_S)                                     # let a burst coalesce
                s = wakerun._status(ctx)
                if not s["pending"]:
                    continue
                d = wakerun._op(ctx, "dispatch", {"fence": wakerun._fence(ctx), "generation": s["generation"]},
                                "wake dispatch")
                self.notify(s["session"], s["generation"])
                wakerun._log(f"channel notice for generation {s['generation']} · #{d['seq']}")
            except Exception as e:                                         # noqa: BLE001 - keep serving MCP
                wakerun._log(f"channel: {getattr(e, 'kind', e.__class__.__name__)}; retrying in 30 s")
                self.stop.wait(30)

    def serve(self) -> int:
        try:
            for line in self.inp:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.handle(msg)
        finally:
            self.stop.set()
            if self.ctx is not None:
                try:
                    wakerun.control(self.ctx, "disarm")
                except Exception:                                          # noqa: BLE001
                    pass
        return 0


def main(a, module) -> int:
    wakerun.bind(module)
    if a.action == "install":
        project = os.path.abspath(a.project or ".")
        try:
            path, changed = install(project)
        except (ValueError, OSError) as e:
            raise module.CliError(module.EXIT_CONFIG, "channel_not_installed", str(e))
        print(("added the glassboard channel server to " if changed else "the glassboard channel server is already in ")
              + path)
        print("start Claude Code with: glassboard run -- claude --dangerously-load-development-channels server:glassboard")
        print("channels are a Claude Code research preview: interactive sessions only, claude.ai login; "
              "Team and Enterprise organizations must enable them")
        return module.EXIT_OK
    return Server(module, a).serve()
