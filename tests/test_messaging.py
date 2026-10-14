"""Messages, questions, sync and consumers, history, export,
secrets only where minted, one event per message, recipient order; agent post rate, loop flag,
storage cap."""
import json
import re

import pytest

from glassboard import events, ids
from test_render import assert_canonical

SECRET_RE = re.compile(r"T\d+g\d+-[a-z2-7]{10}|C\d+-[a-z2-7]{10}")


@pytest.fixture
def m(gb):
    gb.admin("add-agent", "jose", "claude-2")
    gb.tok["jose/claude-2"] = gb.mint("jose/claude-2")
    gb.session("s1")
    for h in ("ana", "jose/claude", "ana/codex", "jose/claude-2"):
        gb.add("s1", h)
    return gb


def post(gb, who, kind="note", body="hello", to=None, re_=None, key=True, **extra):
    j = {"kind": kind, "body": body, **({"to": to} if to is not None else {}), **({"re": re_} if re_ else {}), **extra}
    return gb.req("POST", "/api/v1/sessions/s1/messages", who, json=j, rec=gb.rec(), key=key)


def sync(gb, who, consumer=None, ack=None, takeover=None, limit=None, key=True, runtime=None, fmt=None):
    j = {}
    if consumer is not None:
        j["consumer"] = consumer
    if ack is not None:
        j["ack_through"] = ack
    if takeover is not None:
        j["takeover"] = takeover
    if limit is not None:
        j["limit"] = limit
    extra = {"X-Glassboard-Runtime": runtime} if runtime else None
    path = "/api/v1/sessions/s1/sync" + (f"?format={fmt}" if fmt else "")
    return gb.req("POST", path, who, json=j, rec=gb.rec(), key=key if consumer is None else None, extra=extra)


def member(gb, who):
    return gb.raw.execute("SELECT * FROM member WHERE principal_id=?", (gb.pid(who),)).fetchone()


def attach(gb, who, **kw):
    r = sync(gb, who, **kw)
    assert r.status_code == 200, r.text
    return r.json()


# --- sync and acknowledgement ---------------------------------------------------------------------

def test_at_1_1_get_sync_is_405(m):
    before = tuple(member(m, "jose/claude"))
    r = m.client.get("/api/v1/sessions/s1/sync", headers=m.headers("jose/claude", key=None))
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
    assert tuple(member(m, "jose/claude")) == before


@pytest.mark.parametrize("bad", ["head", "10", -1, 5.5, True, [5]])
def test_at_1_2_ack_through_must_be_a_plain_integer(m, bad):
    c = attach(m, "jose/claude")["consumer"]
    cur = member(m, "jose/claude")["cursor_seq"]
    r = m.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"consumer": c, "ack_through": bad},
              rec=m.rec(), key=None)
    assert r.status_code == 422
    assert member(m, "jose/claude")["cursor_seq"] == cur


def _fill_inbox(gb, n, to="jose/claude"):
    return [post(gb, "jose", to=[to], body=f"m{i}").json()["seq"] for i in range(n)]


def test_at_1_3_and_1_4_ack_only_up_to_delivered(m):
    c = attach(m, "jose/claude")["consumer"]
    seqs = _fill_inbox(m, 4)
    first = sync(m, "jose/claude", consumer=c, limit=2).json()
    assert first["more"] is True
    d = first["delivered_through"]
    assert [e["seq"] for e in first["inbox"]][-1] == d and d < m.head()
    cur = member(m, "jose/claude")["cursor_seq"]
    r = sync(m, "jose/claude", consumer=c, ack=m.head())                      # beyond delivered: refused
    assert r.status_code == 409 and r.json()["error"]["code"] == "ack_beyond_delivered"
    assert r.json()["error"]["details"] == {"delivered_through": d}
    assert member(m, "jose/claude")["cursor_seq"] == cur
    ok = sync(m, "jose/claude", consumer=c, ack=d, limit=2).json()           # up to delivered: accepted
    assert ok["cursor"] == d and all(e["seq"] > d for e in ok["inbox"])
    assert [e["seq"] for e in ok["inbox"]] == [x for x in seqs if x > d][:2]


def test_at_1_5_events_after_the_snapshot_cannot_be_acked(m):
    c = attach(m, "jose/claude")["consumer"]
    _fill_inbox(m, 2)
    s = sync(m, "jose/claude", consumer=c).json()
    assert s["more"] is False and s["delivered_through"] == m.head()
    old_head = m.head()
    new = post(m, "jose", to=["jose/claude"]).json()["seq"]                    # commits after delivery
    r = sync(m, "jose/claude", consumer=c, ack=new)
    assert r.status_code == 409 and r.json()["error"]["details"]["delivered_through"] == old_head
    ok = sync(m, "jose/claude", consumer=c, ack=old_head).json()
    assert [e["seq"] for e in ok["inbox"]] == [new]


def test_at_1_6_unacked_items_are_redelivered(m):
    c = attach(m, "jose/claude")["consumer"]
    seqs = _fill_inbox(m, 3)
    a = sync(m, "jose/claude", consumer=c).json()
    b = sync(m, "jose/claude", consumer=c).json()
    got = [e["seq"] for e in a["inbox"]]
    assert set(seqs) <= set(got) and got == [e["seq"] for e in b["inbox"]]


def test_at_1_7_old_ack_is_a_no_op(m):
    c = attach(m, "jose/claude")["consumer"]
    _fill_inbox(m, 3)
    s = sync(m, "jose/claude", consumer=c).json()
    sync(m, "jose/claude", consumer=c, ack=s["delivered_through"])
    r = sync(m, "jose/claude", consumer=c, ack=1)
    assert r.status_code == 200 and r.json()["cursor"] == s["delivered_through"]


def test_at_1_8_peeking_never_moves_cursor_or_delivery(m):
    attach(m, "jose/claude")
    _fill_inbox(m, 3)
    before = (member(m, "jose/claude")["cursor_seq"], member(m, "jose/claude")["delivered_seq"])
    for _ in range(3):
        r = m.req("GET", "/api/v1/sessions/s1/events?to=me&after=0", "jose/claude", key=None)
        assert r.status_code == 200 and len(r.json()["events"]) >= 3
    assert (member(m, "jose/claude")["cursor_seq"], member(m, "jose/claude")["delivered_seq"]) == before


# --- single consumer --------------------------------------------------------------------------

def test_at_2_1_attach(m):
    seqs = _fill_inbox(m, 3)                       # routed items waiting before the first sync
    head0 = m.head()
    r = sync(m, "jose/claude", runtime="a1f")
    j = r.json()
    assert re.fullmatch(r"C1-[a-z2-7]{10}", j["consumer"]) and j["consumer_gen"] == 1
    ev = m.events()[-1]
    assert ev["type"] == "consumer.attached" and ev["seq"] == head0 + 1 and ev["runtime"] == "a1f"
    assert set(seqs) <= {e["seq"] for e in j["inbox"]}
    # Cursor 5, head 9 → delivered_through 9 (the head the page was scanned against;
    # the attach event itself comes after it and is never in the caller's own inbox).
    assert j["delivered_through"] == member(m, "jose/claude")["delivered_seq"] == head0 == m.head() - 1
    assert member(m, "jose/claude")["consumer_runtime"] == "a1f"


def test_at_2_2_second_runtime_without_token_conflicts(m):
    attach(m, "jose/claude", runtime="a1f")
    before, head = tuple(member(m, "jose/claude")), m.head()
    r = sync(m, "jose/claude", runtime="b77")
    assert r.status_code == 409 and r.json()["error"]["code"] == "consumer_conflict"
    d = r.json()["error"]["details"]
    assert d["runtime"] == "a1f" and d["consumer_gen"] == 1 and d["consumer_since"]
    assert tuple(member(m, "jose/claude")) == before and m.head() == head


def test_at_2_3_forged_token_is_a_mismatch(m):
    c = attach(m, "jose/claude")["consumer"]
    before = tuple(member(m, "jose/claude"))
    forged = c[:3] + ("a" if c[3] != "a" else "b") + c[4:]
    for tok in (forged, "C9-aaaaaaaaaa"):
        r = sync(m, "jose/claude", consumer=tok, ack=1)
        assert r.status_code == 409 and r.json()["error"]["code"] == "consumer_mismatch"
    assert tuple(member(m, "jose/claude")) == before


def test_at_2_4_takeover_resets_delivery_and_redelivers(m):
    c1 = attach(m, "jose/claude", runtime="a1f")["consumer"]
    seqs = _fill_inbox(m, 3)
    s = sync(m, "jose/claude", consumer=c1).json()
    assert member(m, "jose/claude")["delivered_seq"] == s["delivered_through"] > member(m, "jose/claude")["cursor_seq"]
    t = sync(m, "jose/claude", takeover={"reason": "restarted after crash"}, runtime="b77").json()
    assert re.fullmatch(r"C2-[a-z2-7]{10}", t["consumer"])
    assert set(seqs) <= {e["seq"] for e in t["inbox"]}                    # redelivered to the new consumer
    ev = [e for e in m.events() if e["type"] == "consumer.takeover"][-1]
    assert ev["body"] == "restarted after crash" and ev["runtime"] == "b77"
    assert m.recipients("s1", ev["seq"]) == ["jose"]                       # owner (+ coordinators: jose)


def test_at_2_5_old_consumer_after_takeover_cannot_ack(m):
    c1 = attach(m, "jose/claude")["consumer"]
    _fill_inbox(m, 2)
    s = sync(m, "jose/claude", consumer=c1).json()
    sync(m, "jose/claude", takeover={"reason": "new laptop"})
    cur = member(m, "jose/claude")["cursor_seq"]
    r = sync(m, "jose/claude", consumer=c1, ack=s["delivered_through"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "consumer_mismatch"
    assert member(m, "jose/claude")["cursor_seq"] == cur


@pytest.mark.parametrize("tk", [{}, {"reason": ""}, {"reason": None}])
def test_at_2_6_takeover_needs_a_reason(m, tk):
    attach(m, "jose/claude")
    assert sync(m, "jose/claude", takeover=tk).status_code == 422


def test_at_2_7_lost_attach_response_replays_the_receipt(m):
    k = ids.new_ulid()
    a = sync(m, "jose/claude", key=k, runtime="a1f")
    b = sync(m, "jose/claude", key=k, runtime="a1f")
    assert a.status_code == b.status_code == 200
    assert a.json()["consumer"] == b.json()["consumer"] and b.headers["glassboard-replayed"] == "true"
    assert sum(1 for e in m.events() if e["type"] == "consumer.attached") == 1
    c = sync(m, "jose/claude", key=k, runtime="b77")           # attach binds the runtime
    assert c.status_code == 409 and c.json()["error"]["code"] == "idempotency_key_reused"


def test_at_2_8_independent_agents_are_independent_principals(m):
    a = attach(m, "jose/claude")["consumer"]
    b = attach(m, "jose/claude-2")["consumer"]
    post(m, "jose", to=["jose/claude"], body="only for claude")
    ra = sync(m, "jose/claude", consumer=a).json()
    rb = sync(m, "jose/claude-2", consumer=b).json()
    assert any(e["body"] == "only for claude" for e in ra["inbox"])
    assert not any(e["body"] == "only for claude" for e in rb["inbox"])


def test_at_2_10_attach_with_ack_is_refused(m):
    r = m.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={"ack_through": 3}, rec=m.rec())
    assert r.status_code == 422 and member(m, "jose/claude")["consumer_secret"] is None


def test_sync_requires_key_to_attach_and_recovery_always(m):
    assert m.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={}, rec=m.rec(), key=None).status_code == 422
    assert m.req("POST", "/api/v1/sessions/s1/sync", "jose/claude", json={}).status_code == 428
    ro = m.mint("jose/claude", "--scope", "read")
    assert m.client.post("/api/v1/sessions/s1/sync", json={}, headers={"Authorization": f"Bearer {ro}",
                         "X-Glassboard-Recovery": m.rec(), "Idempotency-Key": ids.new_ulid()}).status_code == 403


# --- questions and obligations -----------------------------------------------------------------

def owed(gb, who):
    c = gb.raw.execute("SELECT consumer_secret, consumer_gen FROM member WHERE principal_id=?", (gb.pid(who),)).fetchone()
    if c["consumer_secret"] is None:
        j = attach(gb, who)
    else:
        j = sync(gb, who, consumer=f"C{c['consumer_gen']}-{c['consumer_secret']}").json()
    return [o["seq"] for o in j["owed"]], j["owed_total"]


def test_at_8_2_an_answer_resolves_only_its_author(m):
    q = post(m, "jose", kind="question", to=["ana", "jose/claude"], body="where?").json()["seq"]
    post(m, "ana", kind="answer", re_=q, body="here")
    assert owed(m, "ana") == ([], 0) and owed(m, "jose/claude") == ([q], 1)


def test_at_8_3_a_clarifying_question_does_not_resolve(m):
    q = post(m, "jose", kind="question", to=["ana"]).json()["seq"]
    post(m, "ana", kind="question", re_=q, to=["jose"], body="which one?")
    post(m, "ana", kind="update", re_=q, body="looking")
    assert owed(m, "ana") == ([q], 1)


def test_at_8_4_recipient_declines_once(m):
    q = post(m, "jose", kind="question", to=["ana", "jose/claude"]).json()["seq"]
    r = m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "ana", json={"resolution": "declined", "note": "not mine"},
              rec=m.rec())
    assert r.status_code == 200 and r.json()["scope"] == "self"
    ev = m.events()[-1]
    assert ev["type"] == "question.resolved" and ev["reply_to"] == q and ev["data"]["scope"] == "self"
    assert m.recipients("s1", ev["seq"]) == ["jose"]                   # routed to the asker
    assert owed(m, "ana") == ([], 0) and owed(m, "jose/claude") == ([q], 1)
    again = m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "ana", json={"resolution": "declined"}, rec=m.rec())
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_resolved"


def test_at_8_5_asker_resolves_for_all(m):
    q = post(m, "ana", kind="question", to=["jose/claude", "ana/codex"]).json()["seq"]
    r = m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "ana", json={"resolution": "obsolete"}, rec=m.rec())
    assert r.status_code == 200 and r.json()["scope"] == "all"
    res = [e for e in m.events() if e["type"] == "question.resolved"][-1]
    assert m.recipients("s1", res["seq"]) == ["ana/codex", "jose/claude"]     # recipients (asker is the actor)
    assert owed(m, "jose/claude") == ([], 0) and owed(m, "ana/codex") == ([], 0)
    assert m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "jose", json={"resolution": "obsolete"},
                 rec=m.rec()).status_code == 409
    other = m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "jose/claude-2", json={"resolution": "obsolete"},
                  rec=m.rec())
    assert other.status_code == 403                                   # neither recipient, asker nor coordinator


def test_at_8_6_acking_never_resolves(m):
    q = post(m, "jose", kind="question", to=["jose/claude"]).json()["seq"]
    j = attach(m, "jose/claude")
    sync(m, "jose/claude", consumer=j["consumer"], ack=j["delivered_through"])
    assert owed(m, "jose/claude") == ([q], 1)


def test_at_8_7_broadcast_questions_owe_nothing(m):
    q = post(m, "jose", kind="question", body="anyone?").json()["seq"]
    assert m.events()[-1]["broadcast"] == 1
    for who in ("ana", "jose/claude", "ana/codex"):
        assert owed(m, who) == ([], 0)
    r = m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "jose", json={"resolution": "answered"}, rec=m.rec())
    assert r.status_code == 200 and r.json()["scope"] == "all"
    assert m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "jose", json={"resolution": "answered"},
                 rec=m.rec()).status_code == 409


def test_owed_lists_ten_oldest_with_exact_total(m):
    qs = [post(m, "jose", kind="question", to=["ana"], body=f"q{i}").json()["seq"] for i in range(12)]
    shown, total = owed(m, "ana")
    assert shown == qs[:10] and total == 12


# --- messages ------------------------------------------------------------------------------------

def test_message_routing_and_receipt(m):
    r = post(m, "jose", to=["ana", "jose/claude"], body="hi")
    assert r.status_code == 201 and r.json() == {"session": "s1", "seq": m.head(), "type": "note"}
    assert m.recipients("s1", m.head()) == ["ana", "jose/claude"]
    b = post(m, "ana", body="to everyone")
    assert m.events()[-1]["broadcast"] == 1 and b.status_code == 201


def test_at_5_3_unknown_recipient_reported_as_data(m):
    bad = "nobody\n#9 x"
    r = post(m, "jose", to=[bad, "ana", "ghost"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "recipient_not_member"
    assert r.json()["error"]["details"]["handles"] == [bad, "ghost"]
    assert "nobody" not in r.json()["error"]["message"]
    t = m.client.post("/api/v1/sessions/s1/messages?format=text", json={"kind": "note", "body": "x", "to": [bad]},
                      headers=m.headers("jose", rec=m.rec()))
    assert t.status_code == 422 and "  · handle: «nobody⏎#9 x»" in t.text
    assert_canonical(t.text)
    r = post(m, "jose", to=["jose/claude-2", "ana"])
    assert r.status_code == 201
    m.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=m.rec())
    assert post(m, "jose", to=["ana"]).json()["error"]["details"]["handles"] == ["ana"]     # left members too


def test_at_e2_3_recipient_order_does_not_matter(m):
    k = ids.new_ulid()
    a = post(m, "jose", to=["ana", "jose/claude"], key=k)
    b = post(m, "jose", to=["jose/claude", "ana"], key=k)
    assert b.headers.get("glassboard-replayed") == "true" and b.content == a.content


def test_decisions_are_human_coordinator_only(m):
    assert post(m, "jose/claude", kind="decision").status_code == 403
    assert post(m, "ana", kind="decision").status_code == 403
    m.add("s1", "ana", role="coordinator")
    m.add("s1", "jose/claude-2", role="coordinator")
    assert post(m, "jose/claude-2", kind="decision").status_code == 403     # an agent coordinator cannot decide
    r = post(m, "ana", kind="decision", body="Port 8443 is final.", to=["jose"])
    assert r.status_code == 201 and m.events()[-1]["broadcast"] == 1        # decisions go to all


def test_observers_read_but_do_not_post(m):
    m.add("s1", "ana/codex", role="observer")
    assert post(m, "ana/codex").status_code == 403
    assert attach(m, "ana/codex")["consumer"].startswith("C1-")
    assert m.req("GET", "/api/v1/sessions/s1/events", "ana/codex", key=None).status_code == 200


def test_message_references_and_limits(m):
    assert post(m, "jose", re_=999).status_code == 404
    assert post(m, "jose", task="T7").status_code == 404
    assert post(m, "jose", task="T0").status_code == 422
    assert post(m, "jose", body="").status_code == 422
    assert post(m, "jose", body="é" * 8193).status_code == 422          # 16 386 bytes > 16 KiB
    assert post(m, "jose", to=[f"x{i}" for i in range(21)]).status_code == 422
    assert post(m, "jose", kind="gossip").status_code == 422
    sha = "a" * 64
    r = post(m, "jose", refs=[{"label": "draft", "loc": "docs/x.md@3f9e2c1", "access": "owner_only", "sha256": sha}])
    assert r.status_code == 201
    v = m.req("GET", "/api/v1/sessions/s1/events?after=%d" % (m.head() - 1), "jose", key=None).json()["events"][0]
    assert v["refs"] == [{"label": "draft", "loc": "docs/x.md@3f9e2c1", "access": "owner_only", "sha256": sha}]
    assert post(m, "jose", refs=[{"label": "d", "loc": "l", "access": "shared", "sha256": "ABC"}]).status_code == 422
    assert post(m, "jose", refs=[{"label": "d", "loc": "l", "access": "shared"}] * 11).status_code == 422


def test_r7_json_is_ascii(m):
    post(m, "jose", body="bidi \u202e and é and \x1b[31m", to=["ana"])
    attach(m, "ana")
    for r in (m.req("GET", "/api/v1/sessions/s1/events", "ana", key=None),
              sync(m, "ana", consumer=f"C1-{member(m, 'ana')['consumer_secret']}"),
              m.req("GET", "/api/v1/sessions", "ana", key=None)):
        assert all(b < 0x80 for b in r.content), r.content[:200]
    j = m.req("GET", "/api/v1/sessions/s1/events", "ana", key=None).json()
    note = [e for e in j["events"] if e["type"] == "note"][-1]
    assert note["body"] == "bidi \u202e and \u00e9 and \x1b[31m"                # exact text, escaped in transit


# --- one event per message operation ------------------------------------------------------------

def test_at_8_1_message_rows(m):
    rows = [("post", lambda: post(m, "jose"), 1), ("attach", lambda: attach(m, "ana"), 1),
            ("plain sync", lambda: sync(m, "ana", consumer=f"C1-{member(m, 'ana')['consumer_secret']}"), 0),
            ("ack", lambda: sync(m, "ana", consumer=f"C1-{member(m, 'ana')['consumer_secret']}", ack=1), 0),
            ("peek", lambda: m.req("GET", "/api/v1/sessions/s1/events?to=me", "ana", key=None), 0),
            ("export", lambda: m.req("GET", "/api/v1/sessions/s1/export", "ana", key=None), 0),
            ("question", lambda: post(m, "jose", kind="question", to=["ana"]), 1)]
    for name, op, delta in rows:
        h = m.head()
        op()
        assert m.head() - h == delta, name
    q = m.head()
    h = m.head()
    m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "ana", json={"resolution": "declined"}, rec=m.rec())
    assert m.head() - h == 1


# --- secrets only where minted -------------------------------------------------------------------

def test_at_e1_1_response_scanner(m):
    """Runs a whole flow and scans every response body and header. Only attach/takeover (and
    their replays) may contain a consumer secret."""
    seen = []
    orig = m.client.request

    def rec(method, url, *a, **k):
        r = orig(method, url, *a, **k)
        body = k.get("json") or {}
        minting = url.split("?")[0].endswith("/sync") and method == "POST" and "consumer" not in body
        seen.append((method, url, minting, r))
        return r
    m.client.request = rec
    k = ids.new_ulid()
    c = sync(m, "jose/claude", key=k).json()["consumer"]
    sync(m, "jose/claude", key=k)                                        # replay of a minting op
    post(m, "jose", kind="question", to=["jose/claude"])
    sync(m, "jose/claude", consumer=c)
    sync(m, "jose/claude", consumer=c, fmt="text")
    sync(m, "jose/claude")                                               # conflict
    sync(m, "jose/claude", consumer="C1-aaaaaaaaaa")                     # mismatch
    c2 = sync(m, "jose/claude", takeover={"reason": "r"}).json()["consumer"]
    sync(m, "jose/claude", consumer=c)                                   # old token
    m.req("GET", "/api/v1/sessions/s1/events", "jose", key=None)
    m.req("GET", "/api/v1/sessions/s1/events?format=text", "jose", key=None)
    m.req("GET", "/api/v1/sessions/s1/export?format=md", "jose", key=None)
    m.req("GET", "/api/v1/sessions/s1/export?format=jsonl", "jose", key=None)
    m.req("GET", "/api/v1/sessions/s1/members", "jose", key=None)
    m.req("GET", "/api/v1/sessions", "jose/claude", key=None)
    m.req("GET", "/api/v1/whoami", "jose/claude", key=None)
    m.client.request = orig
    assert len(seen) >= 15 and c2 != c
    for method, url, minting, r in seen:
        text = r.text + " ".join(f"{a}: {b}" for a, b in r.headers.items())
        if minting:
            continue
        assert not SECRET_RE.search(text), (method, url, r.status_code, text[:300])


def test_at_e1_2_views_show_only_the_prefix(m):
    c = attach(m, "jose/claude")["consumer"]
    secret = c.split("-")[1]
    j = sync(m, "jose/claude", consumer=c).json()
    assert j["consumer"] == "C1"
    t = sync(m, "jose/claude", consumer=c, fmt="text").text
    assert "consumer C1 ·" in t and secret not in t
    members = m.req("GET", "/api/v1/sessions/s1/members", "jose", key=None).json()["members"]
    assert {x["handle"]: x["consumer"] for x in members}["jose/claude"] == "C1"
    for path in ("/api/v1/sessions/s1/events", "/api/v1/sessions/s1/export?format=jsonl", "/api/v1/sessions/s1/export"):
        assert secret not in m.req("GET", path, "jose", key=None).text


def test_at_e1_3_consumer_errors_carry_no_secrets(m):
    c = attach(m, "jose/claude")["consumer"]
    for r in (sync(m, "jose/claude"), sync(m, "jose/claude", consumer="C1-aaaaaaaaaa")):
        assert r.status_code == 409 and not SECRET_RE.search(r.text) and c.split("-")[1] not in r.text


def test_at_4_5_export_and_history_hold_no_receipts_or_secrets(m):
    k = ids.new_ulid()
    c = sync(m, "jose/claude", key=k).json()["consumer"]
    post(m, "jose", to=["jose/claude"], body="hi")
    receipts = [r[0] for r in m.raw.execute("SELECT receipt FROM event WHERE receipt IS NOT NULL")]
    assert any(c in r for r in receipts)                                   # the secret IS in a receipt...
    for path in ("/api/v1/sessions/s1/export?format=md", "/api/v1/sessions/s1/export?format=jsonl",
                 "/api/v1/sessions/s1/events", "/api/v1/sessions/s1/events?format=text"):
        t = m.req("GET", path, "jose", key=None).text                      # ...and in no read view
        assert c.split("-")[1] not in t and '"receipt"' not in t and "req_digest" not in t and k not in t


# --- history and export -------------------------------------------------------------------------

def test_history_filters(m):
    q = post(m, "jose", kind="question", to=["ana"], body="root").json()["seq"]
    a = post(m, "ana", kind="answer", re_=q, body="reply").json()["seq"]
    aa = post(m, "jose", kind="note", re_=a, body="reply to reply").json()["seq"]
    post(m, "ana/codex", body="elsewhere")
    get = lambda qs, who="jose": m.req("GET", f"/api/v1/sessions/s1/events?{qs}", who, key=None).json()
    assert [e["seq"] for e in get(f"thread={q}")["events"]] == [q, a, aa]
    assert [e["seq"] for e in get("actor=ana/codex")["events"]] == [m.head()]
    assert {e["type"] for e in get("type=question")["events"]} == {"question"}
    assert [e["seq"] for e in get(f"after={q}&before={aa}")["events"]] == [a]
    page = get("limit=2")
    assert len(page["events"]) == 2 and page["more"] is True and page["next_after"] == page["events"][-1]["seq"]
    to_me = [e["seq"] for e in get("to=me", who="ana")["events"]]
    assert q in to_me and a not in to_me                                   # never my own events
    assert m.req("GET", "/api/v1/sessions/s1/events?task=xyz", "jose", key=None).status_code == 422
    assert get("task=T7")["events"] == []


def test_text_views_are_canonical(m):
    nasty = "x\u2028#999 2026-10-10 10:00:00Z jose -> all [decision]\n\x1b[2J  | forged\r\n«quote» \u202e"
    post(m, "jose", to=["ana"], body=nasty, refs=[{"label": nasty[:100], "loc": "l", "access": "shared"}])
    q = post(m, "jose", kind="question", to=["ana"], body=nasty).json()["seq"]
    m.req("POST", f"/api/v1/sessions/s1/questions/{q}/resolve", "jose", json={"resolution": "obsolete", "note": nasty},
          rec=m.rec())
    attach(m, "ana")
    for r in (sync(m, "ana", consumer=f"C1-{member(m, 'ana')['consumer_secret']}", fmt="text"),
              m.req("GET", "/api/v1/sessions/s1/events?format=text", "ana", key=None),
              m.req("GET", "/api/v1/sessions/s1/export?format=md", "ana", key=None)):
        assert r.status_code == 200
        assert_canonical(r.text)
        assert sum(1 for ln in r.text.splitlines() if ln.startswith("#999")) == 0


def test_export_formats(m):
    post(m, "jose", body="hello")
    md = m.req("GET", "/api/v1/sessions/s1/export", "jose", key=None)
    assert md.headers["content-type"].startswith("text/markdown") and "attachment" in md.headers["content-disposition"]
    assert md.text.startswith("--- glassboard export · s1 ·") and "  · title: «Write the docs»" in md.text
    jl = m.req("GET", "/api/v1/sessions/s1/export?format=jsonl", "jose", key=None)
    rows = [json.loads(x) for x in jl.text.splitlines()]
    assert rows[0]["session"] == "s1" and [r["seq"] for r in rows[1:]] == list(range(1, m.head() + 1))
    assert all(set(r) >= {"seq", "type", "actor", "to"} and "receipt" not in r for r in rows[1:])


# --- rate limit, loop flag, storage cap -------------------------------------------------------

def test_agent_post_rate_limit(m):
    clock = [0.0]
    from glassboard.limits import PostRateLimiter
    m.app.state.post_limiter = PostRateLimiter(clock=lambda: clock[0])
    codes = [post(m, "jose/claude").status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429
    r = post(m, "jose/claude")
    assert r.json()["error"]["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
    assert all(post(m, "jose").status_code == 201 for _ in range(15))        # humans are not limited
    clock[0] += 2.1                                                         # 30/min = one every 2 s
    assert post(m, "jose/claude").status_code == 201


def test_loop_flag_after_ten_agent_events_in_a_thread(m):
    clock = [0.0]
    from glassboard.limits import PostRateLimiter
    m.app.state.post_limiter = PostRateLimiter(burst=100, clock=lambda: clock[0])
    root = post(m, "jose/claude", kind="question", to=["ana/codex"]).json()["seq"]
    last = root
    for i in range(9):
        who = "ana/codex" if i % 2 == 0 else "jose/claude"
        last = post(m, who, re_=last, body=f"r{i}").json()["seq"]
    flags = [e for e in m.events() if e["actor_id"] == ids.BOARD_PRINCIPAL_ID]
    assert len(flags) == 1 and flags[0]["data"] == {"flag": "loop", "thread": root}
    assert flags[0]["reply_to"] == last and m.recipients("s1", flags[0]["seq"]) == ["jose"]
    last = post(m, "jose/claude", re_=last).json()["seq"]                    # 11th: the board note broke the run
    assert len([e for e in m.events() if e["actor_id"] == ids.BOARD_PRINCIPAL_ID]) == 1
    post(m, "jose", re_=last)                                               # a human in the thread
    assert len([e for e in m.events() if e["actor_id"] == ids.BOARD_PRINCIPAL_ID]) == 1


def test_storage_cap_and_warning(m, monkeypatch):
    head = m.head()
    monkeypatch.setattr(events, "MAX_EVENTS", head + 5)
    while m.head() < head + 4:
        assert post(m, "jose").status_code == 201
    warn = [e for e in m.events() if e["actor_id"] == ids.BOARD_PRINCIPAL_ID and e["data"] == {"flag": "storage"}]
    assert len(warn) == 1 and m.recipients("s1", warn[0]["seq"]) == ["jose"]
    while True:
        r = post(m, "jose")
        if r.status_code != 201:
            break
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    assert attach(m, "ana")["consumer"].startswith("C1-")                    # reading still works on a full session


def test_at_2_4_new_consumer_cannot_ack_what_only_the_old_one_saw(m):
    """On takeover, delivered_seq restarts at the cursor. Otherwise the new runtime could ack
    up to the old runtime's boundary and skip events it was never shown."""
    c1 = attach(m, "jose/claude")["consumer"]
    seqs = _fill_inbox(m, 4)
    old = sync(m, "jose/claude", consumer=c1).json()["delivered_through"]       # old runtime saw all 4
    t = sync(m, "jose/claude", takeover={"reason": "crash"}, limit=2).json()     # new runtime sees 2
    assert t["more"] is True and t["delivered_through"] < old
    c2 = t["consumer"]
    r = sync(m, "jose/claude", consumer=c2, ack=old)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ack_beyond_delivered"
    assert r.json()["error"]["details"]["delivered_through"] == t["delivered_through"]
    rest = sync(m, "jose/claude", consumer=c2, ack=t["delivered_through"]).json()
    assert set(seqs) - {e["seq"] for e in t["inbox"]} <= {e["seq"] for e in rest["inbox"]}


def test_attach_after_consumer_cleared_restarts_delivery(m):
    """The same rule for a plain attach after the consumer was cleared (removal and re-add,
    or a restore)."""
    c1 = attach(m, "jose/claude")["consumer"]
    _fill_inbox(m, 4)
    old = sync(m, "jose/claude", consumer=c1).json()["delivered_through"]
    m.raw.execute("UPDATE member SET consumer_secret=NULL WHERE principal_id=?", (m.pid("jose/claude"),))
    a = sync(m, "jose/claude", limit=1).json()
    assert a["delivered_through"] < old
    r = sync(m, "jose/claude", consumer=a["consumer"], ack=old)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ack_beyond_delivered"
