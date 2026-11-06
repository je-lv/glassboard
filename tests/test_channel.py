"""B32 Claude Code channels adapter (ERRATA E40): a fixed notice pushed into an idle open session,
recorded as wake events, refused when the client could act unattended."""
import io
import json
import subprocess
import sys
import time

from glassboard import channel
from test_cli import L, ok  # noqa: F401  (fixture)


def test_b32_unattended_modes_are_detected():
    assert channel.unattended(["claude", "--dangerously-skip-permissions"])
    assert channel.unattended(["claude", "--permission-mode", "bypassPermissions"])
    assert channel.unattended(["claude", "--permission-mode=auto"])
    assert not channel.unattended(["claude", "--permission-mode", "default"])
    assert not channel.unattended(["claude", "--dangerously-load-development-channels", "server:glassboard"])


def test_b32_install_merges_into_mcp_json_once(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    path, changed = channel.install(str(tmp_path))
    cfg = json.loads(open(path).read())
    assert changed and cfg["mcpServers"]["other"] == {"command": "x"}
    assert cfg["mcpServers"]["glassboard"] == {"command": "glassboard", "args": ["claude-channel", "serve"]}
    assert channel.install(str(tmp_path)) == (path, False)


def handshake(lines):
    out = io.StringIO()
    srv = channel.Server(cli=None, a=None, out=out, inp=io.StringIO("".join(json.dumps(m) + "\n" for m in lines)))
    for m in lines:
        if m.get("method") != "notifications/initialized":
            srv.handle(m)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_b32_mcp_handshake_declares_the_channel_and_no_tools():
    got = handshake([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "x"}}])
    assert got[0]["result"]["capabilities"] == {"experimental": {"claude/channel": {}}}
    assert got[0]["result"]["protocolVersion"] == "2025-06-18"
    assert got[1]["result"] == {"tools": []} and got[2]["result"] == {}
    assert got[3]["error"]["code"] == -32601


def test_b32_refused_without_glassboard_run_or_when_unattended(monkeypatch):
    srv = channel.Server(cli=None, a=None)
    monkeypatch.delenv("GLASSBOARD_RUNTIME", raising=False)
    monkeypatch.delenv(channel.UNATTENDED_ENV, raising=False)
    assert "glassboard run" in srv.refusal()
    monkeypatch.setenv("GLASSBOARD_RUNTIME", "rt1")
    monkeypatch.setenv(channel.UNATTENDED_ENV, "1")
    assert "unattended" in srv.refusal()
    monkeypatch.setenv(channel.UNATTENDED_ENV, "0")
    assert srv.refusal() is None


def test_b32_notice_is_fixed_text_recorded_as_a_wake_and_disarmed_on_exit(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "use", "s1"))
    env = {**L.env("jose/claude"), "GLASSBOARD_RUNTIME": "rt1", channel.UNATTENDED_ENV: "0"}
    p = subprocess.Popen([sys.executable, "-m", "glassboard.cli", "claude-channel", "serve"], env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        assert json.loads(p.stdout.readline())["id"] == 1
        deadline = time.time() + 20
        while time.time() < deadline and not any(e["type"] == "wake.armed" for e in gb.events()):
            time.sleep(0.2)
        ok(L.cli("jose", "post", "note", "SECRET-PLAN-TEXT for the agent", "--to", "jose/claude"))
        line = p.stdout.readline()                    # blocks until the notice (debounce ~3 s)
        notice = json.loads(line)
        assert notice["method"] == "notifications/claude/channel"
        assert notice["params"]["content"].startswith("New Glassboard work may be waiting for you (session s1")
        assert "SECRET-PLAN-TEXT" not in line                                       # never board content
        types = [e["type"] for e in gb.events()]
        assert "wake.armed" in types and "wake.dispatched" in types
        r = L.cli("jose/claude", "wake", "done", extra_env={"GLASSBOARD_RUNTIME": "rt1"})
        assert r.returncode == 0 and r.stdout.startswith("inspected generation"), r.stderr
    finally:
        p.stdin.close()
        p.wait(timeout=20)
    assert gb.events()[-1]["type"] == "wake.disarmed"
