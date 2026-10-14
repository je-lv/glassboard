import sqlite3

from glassboard import admin


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_discovery_is_public_and_has_no_session_data(board):
    r = board["client"].get("/api/v1/discovery")
    assert r.status_code == 200
    j = r.json()
    assert j["api_version"] == "v1" and "whoami" in j["features"]
    assert "sessions" not in j


def test_whoami_requires_bearer(board):
    r = board["client"].get("/api/v1/whoami")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"]["code"] == "unauthenticated"


def test_whoami_valid_agent_token(board):
    tok = board["mint"]("jose/claude")
    r = board["client"].get("/api/v1/whoami", headers=auth(tok))
    assert r.status_code == 200
    j = r.json()
    assert j["handle"] == "jose/claude" and j["kind"] == "agent" and j["owner"] == "jose"
    t = board["client"].get("/api/v1/whoami?format=text", headers=auth(tok))
    assert t.text.startswith("@jose/claude (agent, owner jose)")
    assert f"credential ID {j['credential_id']}" in t.text
    assert f"credential {j['credential_id']}" not in t.text


def test_malformed_wrong_secret_unknown_id(board):
    tok = board["mint"]("jose/claude")
    pre, cid, sec = tok.split("_", 2)
    bad = [
        "garbage", f"gbt_{cid}_{'A' * 43}", f"gbt_aaaaaaaaaa_{sec}", tok + "x",
        f"Basic {tok}",
    ]
    for b in bad:
        h = {"Authorization": b} if b.startswith("Basic") else auth(b)
        r = board["client"].get("/api/v1/whoami", headers=h)
        assert r.status_code == 401, b


def test_revoked_expired_deactivated(board, capsys):
    tok = board["mint"]("jose/claude")
    cid = tok.split("_")[1]
    assert board["client"].get("/api/v1/whoami", headers=auth(tok)).status_code == 200
    admin.main(["--db", board["db"], "revoke-token", cid])
    assert board["client"].get("/api/v1/whoami", headers=auth(tok)).status_code == 401

    exp = board["mint"]("jose/claude", "--expires-days", "0")
    assert board["client"].get("/api/v1/whoami", headers=auth(exp)).status_code == 401

    live = board["mint"]("jose/claude")
    admin.main(["--db", board["db"], "deactivate", "jose"])      # owner deactivated
    capsys.readouterr()
    assert board["client"].get("/api/v1/whoami", headers=auth(live)).status_code == 401


def test_secret_never_stored(board):
    tok = board["mint"]("jose/claude")
    secret = tok.split("_", 2)[2].encode()
    for suffix in ("", "-wal"):
        try:
            data = open(board["db"] + suffix, "rb").read()
        except FileNotFoundError:
            continue
        assert secret not in data
    row = sqlite3.connect(board["db"]).execute("SELECT length(secret_sha256) FROM credential").fetchone()
    assert row[0] == 32


def test_untrusted_host_rejected(board):
    r = board["client"].get("/api/v1/discovery", headers={"Host": "evil.example"})
    assert r.status_code == 400
    r = board["client"].get("/api/v1/discovery", headers={"Host": "board.lan"})
    assert r.status_code == 200


def test_last_used_is_bookkeeping(board):
    tok = board["mint"]("jose/claude")
    board["client"].get("/api/v1/whoami", headers=auth(tok))
    v = sqlite3.connect(board["db"]).execute("SELECT last_used_at FROM credential").fetchone()[0]
    assert v and v.endswith("Z")
