"""Validation errors return field paths and types only, never echo client input."""
import json

from conftest import PROBE_PATH

PAYLOAD = "PWN\x1b[31m<script>alert(1)</script>\n#999 jose -> all [decision]"
MARK = "PWN"


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def assert_no_echo(r):
    assert MARK not in r.text and "<script>" not in r.text and "\x1b" not in r.text and "#999" not in r.text
    e = r.json()["error"]
    assert e["code"] == "invalid_request"
    for f in e["details"]["fields"]:
        assert set(f) == {"loc", "type"}


def test_query_literal_not_echoed(board):
    tok = board["mint"]("jose/claude")
    from urllib.parse import quote
    r = board["client"].get("/api/v1/whoami?format=" + quote(PAYLOAD), headers=auth(tok))
    assert r.status_code == 422
    assert_no_echo(r)
    assert r.json()["error"]["details"]["fields"][0]["loc"] == ["query", "format"]


def test_body_wrong_types_not_echoed(board):
    tok = board["mint"]("jose/claude")
    for body in ({"kind": PAYLOAD, "body": "x", "to": []},
                 {"kind": "question", "body": 5, "to": [PAYLOAD]},
                 {"kind": "question", "body": PAYLOAD * 5000, "to": []},
                 {"kind": "question", "body": "x", "to": [], PAYLOAD: 1},
                 {"kind": "question", "body": "x", "to": [], "pwned_key_name": 1}):
        r = board["client"].post(PROBE_PATH, headers=auth(tok), json=body)
        assert r.status_code in (422, 413), body
        if r.status_code == 422:
            assert_no_echo(r)
            assert "pwned_key_name" not in r.text


def test_malformed_json_not_echoed(board):
    tok = board["mint"]("jose/claude")
    raw = '{"kind": "' + PAYLOAD.replace("\n", "\\n").replace("\x1b", "\\u001b") + '", oops'
    r = board["client"].post(PROBE_PATH, headers={**auth(tok), "Content-Type": "application/json"}, content=raw)
    assert r.status_code == 422
    assert_no_echo(r)


def test_default_fastapi_handler_would_have_echoed(board):
    """Regression guard: proves the custom handler is doing work (stock FastAPI leaks input)."""
    from fastapi import FastAPI, Query
    from fastapi.testclient import TestClient
    from typing import Literal
    stock = FastAPI()

    @stock.get("/x")
    def x(format: Literal["json", "text"] = Query("json")):
        return {}
    r = TestClient(stock).get("/x", params={"format": PAYLOAD})
    assert MARK in r.text


def test_404_405_do_not_echo_path(board):
    r = board["client"].get("/api/v1/" + MARK + "%3Cscript%3E")
    assert r.status_code == 404 and MARK not in r.text
    assert r.json()["error"]["code"] == "not_found"
    r = board["client"].delete("/api/v1/discovery")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_oversized_body_413(board):
    tok = board["mint"]("jose/claude")
    r = board["client"].post(PROBE_PATH, headers=auth(tok), content=b"x" * 70000,
                             )
    assert r.status_code == 413


def test_spike_route_removed(tmp_path, monkeypatch):
    """The old /_spike/validate route is gone; its old opt-in variable no longer brings it back."""
    from glassboard.app import create_app
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GLASSBOARD_SPIKE_ROUTES", "1")
    c = TestClient(create_app(str(tmp_path / "x.db"), allowed_hosts=["testserver"]))
    assert c.post("/api/v1/_spike/validate", json={}).status_code == 404
    assert c.post(PROBE_PATH, json={}).status_code == 404       # the test probe is not in the app
