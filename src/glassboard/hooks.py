"""Client hook recipes and the compaction threshold (B40, ERRATA E35). Stdlib only.

Hooks are opt-in. `glassboard hooks install --for claude|codex` adds two command hooks to the client's
own settings file, and nothing else:

- SessionStart (source compact, resume or clear) runs `glassboard hook session-start`, which prints the
  resume brief as `hookSpecificOutput.additionalContext` (same shape for Claude Code and Codex).
- PostCompact runs `glassboard hook compacted`, which records one `context.compacted` event.

With `--deliver` (B53) PostToolUse runs `glassboard hook deliver`: new inbox events for this runtime,
bounded, as additionalContext, acknowledged only after they were printed. With `--hold` the Stop hook
keeps a turn going once while events wait (never under bypassPermissions, never twice in a row).

Every hook exits 0 and prints nothing when the board cannot be reached, so a hook never breaks the
client. Hooks never block compaction: a block during context-limit recovery fails the request.

The compaction threshold is client configuration that `glassboard run` sets for the client it starts:
Claude Code `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (a percentage of the auto-compact window; it can only lower
the client's own threshold) and Codex `-c model_post_turn_compact_threshold_percent=N` (compaction after
a final response, i.e. at a turn boundary). Order: `glassboard run --compact-at`, then the agent's own
setting (`glassboard compaction set`), then the board's (`GLASSBOARD_COMPACT_AT`, in discovery), then
DEFAULT_COMPACT_AT [owner, 2026-11-05: 75-80% by default, configurable per board, agent and run]."""
from __future__ import annotations

import json
import os
import time

DEFAULT_COMPACT_AT = 75
CLIENTS = ("claude", "codex")
MARK = "glassboard hook "                    # how an installed handler is recognised (and never duplicated)
CLAUDE_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
CODEX_KEY = "model_post_turn_compact_threshold_percent"


def recipe(client: str, deliver: bool = False, hold: bool = False) -> dict:
    """The hooks object for one client's settings file. `deliver` (B53) adds PostToolUse delivery of new
    board events; `hold` adds the opt-in Stop hook that keeps a turn going once while events wait."""
    def handler(verb: str) -> dict:
        return {"type": "command", "command": f"glassboard hook {verb} --client {client}", "timeout": 15}
    out = {"SessionStart": [{"matcher": "compact|resume|clear" if client == "claude" else "^(compact|resume|clear)$",
                             "hooks": [handler("session-start")]}],
           "PostCompact": [{"hooks": [handler("compacted")]}]}
    if deliver or hold:
        out["PostToolUse"] = [{"matcher": "*" if client == "claude" else ".*", "hooks": [handler("deliver")]}]
    if hold:
        out["Stop"] = [{"hooks": [handler("stop")]}]
    return out


def settings_path(client: str, project: str | None = None, home: str | None = None) -> str:
    """Claude Code: settings.json (user ~/.claude, or a project's .claude). Codex: hooks.json (~/.codex or
    a project's .codex)."""
    base = project if project else (home or os.path.expanduser("~"))
    return os.path.join(base, ".claude", "settings.json") if client == "claude" else \
        os.path.join(base, ".codex", "hooks.json")


def merge(existing: dict, client: str, deliver: bool = False, hold: bool = False) -> tuple[dict, bool]:
    """Add the recipe's handlers that are not there yet. Other hooks and settings are kept as they are."""
    out = json.loads(json.dumps(existing))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the settings file has a 'hooks' value that is not an object")
    changed = False
    for event, groups in recipe(client, deliver, hold).items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ValueError(f"the settings file's hooks.{event} is not a list")
        have = {h.get("command", "").split(" --client")[0] for g in current if isinstance(g, dict)
                for h in g.get("hooks", []) if isinstance(h, dict) and h.get("command", "").startswith(MARK)}
        for g in groups:
            if g["hooks"][0]["command"].split(" --client")[0] not in have:
                current.append(g)
                changed = True
    return out, changed


def install(client: str, project: str | None = None, home: str | None = None, deliver: bool = False,
            hold: bool = False) -> tuple[str, bool, str | None]:
    """Merge the recipe into the client's settings file. Returns (path, changed, backup path)."""
    path = settings_path(client, project, home)
    existing = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        existing = json.loads(text) if text.strip() else {}
        if not isinstance(existing, dict):
            raise ValueError("the settings file is not a JSON object")
    merged, changed = merge(existing, client, deliver, hold)
    if not changed:
        return path, False, None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = f"{path}.glassboard-backup-{time.strftime('%Y%m%dT%H%M%S')}"
        with open(path, "rb") as src, open(backup, "xb") as dst:
            dst.write(src.read())
    tmp = f"{path}.glassboard-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path, True, backup


# --- compaction threshold -----------------------------------------------------------------------

def parse_compact_at(value) -> int | None | str:
    """'off' → 'off'; 1..100 → int; None/'' → None (not set here). Raises ValueError otherwise."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() == "off":
        return "off"
    n = int(str(value).strip().rstrip("%"))
    if not 1 <= n <= 100:
        raise ValueError("compaction percent is 1-100 or off")
    return n


def resolve(run_flag=None, agent=None, board=None) -> tuple[int | str, str]:
    """(value, source): the first one set of run flag, agent setting, board setting, default."""
    for value, source in ((run_flag, "run"), (agent, "agent"), (board, "board")):
        parsed = parse_compact_at(value)
        if parsed is not None:
            return parsed, source
    return DEFAULT_COMPACT_AT, "default"


def client_of(cmd: list[str]) -> str | None:
    name = os.path.basename(cmd[0]) if cmd else ""
    return name if name in CLIENTS else None


def apply(cmd: list[str], value, env: dict) -> tuple[list[str], str | None]:
    """The client command line and environment with the threshold set, and a one-line note (None when
    nothing was set: another client, 'off', or the user already set it themselves)."""
    client = client_of(cmd)
    if client is None or value == "off":
        return cmd, None
    if client == "claude":
        if env.get(CLAUDE_ENV):
            return cmd, None
        env[CLAUDE_ENV] = str(value)
        return cmd, f"claude compacts at {value}% of its auto-compact window ({CLAUDE_ENV})"
    if any(CODEX_KEY in a for a in cmd[1:]):          # the user set it on the command line already
        return cmd, None
    return [cmd[0], "-c", f"{CODEX_KEY}={value}", *cmd[1:]], \
        f"codex compacts after a turn at {value}% of its context window ({CODEX_KEY})"
