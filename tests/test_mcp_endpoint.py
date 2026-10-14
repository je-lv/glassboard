"""MCP adapter.

Runs in two environments:
- `.venv` (the core environment, no `mcp` package): only the "absent" tests run; the rest skip.
- `.venv-mcp` (requirements/mcp.lock on top): everything runs.
The live client probes (Claude Code, Codex calling board_whoami) are run by hand, not here.
"""
import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import tomllib

import pytest
from starlette.testclient import TestClient

from glassboard import mcp_adapter
from glassboard.app import ConfigError, create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAS_MCP = importlib.util.find_spec("mcp") is not None
needs_mcp = pytest.mark.skipif(not HAS_MCP, reason="optional mcp extra not installed (run in .venv-mcp)")
RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


def _lock_names(name):
    with open(os.path.join(ROOT, "requirements", name)) as f:
        return {line.split("==")[0] for line in f if line[:1].isalpha()}


# --- the core install never pulls the mcp package -------------------------------------------------

def test_at_7_1_mcp_only_in_its_own_lock():
    for lock in ("server.lock", "dev.lock", "build.lock"):
        assert not {"mcp", "mcp-types"} & _lock_names(lock), lock
    assert "mcp" in _lock_names("mcp.lock")
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        extras = tomllib.load(f)["project"]["optional-dependencies"]
    assert [d for d in extras["mcp"] if d.startswith("mcp")] and not any(
        d.startswith("mcp") for k, v in extras.items() if k != "mcp" for d in v)


def test_at_7_1_core_modules_do_not_import_mcp():
    code = ("import sys, glassboard.app, glassboard.cli, glassboard.admin, glassboard.mcp_adapter;"
            "print(sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, "GLASSBOARD_MCP": ""})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


# --- /mcp absent unless installed AND enabled -----------------------------------------------------

def test_at_7_3_no_mcp_route_unless_enabled(gb, monkeypatch):
    monkeypatch.delenv("GLASSBOARD_MCP", raising=False)
    r = gb.client.post("/mcp", headers={**RPC_HEADERS, **gb.headers("jose", key=False)}, json=rpc("tools/list"))
    assert r.status_code == 404
    d = gb.client.get("/api/v1/discovery").json()
    assert "mcp" not in d["features"] and "mcp_sync_wait_max_s" not in d["limits"]


def test_at_7_3_enabled_without_the_package_refuses_to_start(gb, monkeypatch):
    monkeypatch.setattr(mcp_adapter, "available", lambda: False)
    gb.stop()
    with pytest.raises(ConfigError, match="optional `mcp` package"):
        create_app(gb.db, allowed_hosts=["testserver"], mcp=True)
    monkeypatch.setenv("GLASSBOARD_MCP", "1")
    with pytest.raises(ConfigError):
        create_app(gb.db, allowed_hosts=["testserver"])


# --- with the extra installed -----------------------------------------------------------------------

@pytest.fixture
def mcp_client(gb):
    gb.stop()
    app = create_app(gb.db, allowed_hosts=["testserver", "board.lan"], mcp=True)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _call(c, gb, who, method="tools/call", params=None, extra=None):
    h = {**RPC_HEADERS, "Authorization": f"Bearer {gb.tok.get(who, who)}", **(extra or {})}
    return c.post("/mcp", headers=h, json=rpc(method, params))


@needs_mcp
def test_board_whoami_matches_http_whoami_per_principal(gb, mcp_client):
    for who in ("jose", "jose/claude", "ana/codex"):
        r = _call(mcp_client, gb, who, params={"name": "board_whoami", "arguments": {}})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")   # json_response
        res = r.json()["result"]
        http = mcp_client.get("/api/v1/whoami?format=text", headers={"Authorization": f"Bearer {gb.tok[who]}"}).text
        assert res["isError"] is False and res["content"] == [{"type": "text", "text": http}]
        assert http.startswith(f"@{who} (")


@needs_mcp
@pytest.mark.parametrize("auth", [None, "Basic abc", "Bearer gbt_aaaaaaaaaa_" + "A" * 43])
def test_mcp_requires_a_valid_bearer(gb, mcp_client, auth):
    h = {**RPC_HEADERS, **({"Authorization": auth} if auth else {})}
    r = mcp_client.post("/mcp", headers=h, json=rpc("tools/call", {"name": "board_whoami", "arguments": {}}))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    assert r.headers["www-authenticate"].startswith("Bearer")


@needs_mcp
def test_revoked_and_read_only_tokens(gb, mcp_client):
    cred = mcp_client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {gb.tok['ana/codex']}"}).json()["credential_id"]
    read_tok = gb.mint("jose/claude", "--scope", "read")
    r = _call(mcp_client, gb, read_tok, params={"name": "board_whoami", "arguments": {}})
    assert r.status_code == 200 and "scope read" in r.json()["result"]["content"][0]["text"]
    gb.admin("revoke-token", cred)
    assert _call(mcp_client, gb, "ana/codex", params={"name": "board_whoami", "arguments": {}}).status_code == 401


@needs_mcp
def test_mcp_auth_failures_share_the_http_limiter(gb, mcp_client):
    for _ in range(10):
        assert _call(mcp_client, gb, "gbt_bad").status_code == 401
    r = _call(mcp_client, gb, "jose", params={"name": "board_whoami", "arguments": {}})
    assert r.status_code == 429 and "retry-after" in r.headers
    assert mcp_client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {gb.tok['jose']}"}).status_code == 429


@needs_mcp
@pytest.mark.parametrize("origin,ok", [
    (None, True), ("https://board.lan", True), ("https://board.lan:8443", True), ("https://testserver", True),
    ("http://board.lan:8443", False), ("https://evil.example", False), ("https://board.lan.evil.example", False),
    ("https://board.lan:8443/x", False), ("null", False),
])
def test_origin_allow_list(gb, mcp_client, origin, ok):
    r = _call(mcp_client, gb, "jose", params={"name": "board_whoami", "arguments": {}},
              extra={"Origin": origin} if origin else None)
    if ok:
        assert r.status_code == 200
    else:
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        assert origin not in r.text                                           # errors never echo input


@needs_mcp
def test_host_allow_list(gb, mcp_client):
    r = mcp_client.post("/mcp", headers={**RPC_HEADERS, "Authorization": f"Bearer {gb.tok['jose']}", "Host": "rebind.example"},
                        json=rpc("tools/list"))
    assert r.status_code == 400 and "rebind.example" not in r.text
    r = mcp_client.post("/mcp", headers={**RPC_HEADERS, "Authorization": f"Bearer {gb.tok['jose']}", "Host": "board.lan:8443"},
                        json=rpc("tools/list"))
    assert r.status_code == 200


@needs_mcp
def test_body_limit_applies_to_mcp(gb, mcp_client):
    big = rpc("tools/call", {"name": "board_whoami", "arguments": {"x": "a" * 70000}})
    r = mcp_client.post(
        "/mcp", headers={**RPC_HEADERS, "Authorization": f"Bearer {gb.tok['jose']}"}, content=json.dumps(big))
    assert r.status_code == 413


@needs_mcp
def test_discovery_reports_mcp_and_its_wait_cap(gb, mcp_client):
    d = mcp_client.get("/api/v1/discovery").json()
    assert "mcp" in d["features"] and d["limits"]["mcp_sync_wait_max_s"] == mcp_adapter.MCP_SYNC_WAIT_MAX_S
    assert mcp_adapter.MCP_SYNC_WAIT_MAX_S < d["limits"]["sync_wait_max_s"]


@needs_mcp
def test_logs_hold_no_credentials(gb, mcp_client, caplog):
    caplog.set_level(logging.DEBUG)
    _call(mcp_client, gb, "jose/claude", params={"name": "board_whoami", "arguments": {}})
    _call(mcp_client, gb, "jose", extra={"Origin": "https://evil.example"})
    secret = gb.tok["jose/claude"].split("_", 2)[2]
    assert secret not in caplog.text and "Bearer" not in caplog.text and "evil.example" not in caplog.text
    assert "route=/mcp" in caplog.text


# --- the SDK's own client over a real socket (what Claude Code / Codex do, minus TLS) ---------------

@needs_mcp
def test_sdk_client_calls_board_whoami_over_http(gb, tmp_path):
    import httpx2
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from conftest import free_port

    gb.stop()
    port = free_port()
    app = create_app(gb.db, allowed_hosts=["127.0.0.1"], mcp=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.025)

    async def go(who):
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {gb.tok[who]}"}) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    res = await s.call_tool("board_whoami", {})
                    return [x.name for x in tools.tools], res.content[0].text

    try:
        for who in ("jose/claude", "ana"):
            names, text = asyncio.run(go(who))
            assert "board_whoami" in names and text.startswith(f"@{who} (")     # the full list: test_b18
    finally:
        server.should_exit = True
        t.join(timeout=10)
