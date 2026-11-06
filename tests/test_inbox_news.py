"""Inbox news on every reply: successful session writes carry Glassboard-Inbox-Unread; the CLI shows
it on stderr; receipts stay byte-identical."""
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_tasks import api


def test_writes_say_how_many_events_wait_unread(b):
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "one", "to": ["jose/claude"]})
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "two"})
    r = api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "mine"})
    unread = b.req("GET", "/api/v1/sessions/s1/brief", "jose/claude", key=None).json()["inbox"]["unacked"]
    assert r.status_code == 201 and r.headers["glassboard-inbox-unread"] == str(unread) and unread >= 2
    assert "inbox" not in r.text                                      # a header, never the receipt body
    assert "glassboard-inbox-unread" not in b.req("GET", "/api/v1/sessions/s1/events", "jose/claude", key=None).headers
    bad = api(b, "POST", "/messages", "jose/claude", {"kind": "nope", "body": "x"})
    assert bad.status_code == 422 and "glassboard-inbox-unread" not in bad.headers


def test_a_replay_keeps_the_receipt_byte_identical(b):
    first = api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "x"}, key="op-news-0001")
    api(b, "POST", "/messages", "ana", {"kind": "note", "body": "later"})
    again = api(b, "POST", "/messages", "jose/claude", {"kind": "note", "body": "x"}, key="op-news-0001")
    assert again.content == first.content and again.headers.get("glassboard-replayed") == "true"
    assert again.headers["glassboard-inbox-unread"] != first.headers["glassboard-inbox-unread"]


def test_cli_prints_the_news_on_stderr_only(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "post", "note", "for claude", "--to", "jose/claude"))
    r = L.cli("jose/claude", "post", "note", "hello")
    assert r.returncode == 0 and r.stdout.startswith("posted #") and "inbox" not in r.stdout
    assert "unread · glassboard sync to read them" in r.stderr
    ok(L.cli("jose/claude", "sync"))
    ok(L.cli("jose/claude", "sync", "--ack"))
    r = L.cli("jose/claude", "post", "note", "caught up")
    assert r.returncode == 0 and "inbox" not in r.stderr                  # nothing waiting: no line
