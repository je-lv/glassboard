"""B55 (ERRATA E43): topic channels inside a session. A channel decides whose inbox a message reaches;
it never hides anything: every channel message stays in the one session log."""
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import csrf_of, login
from test_render import assert_canonical
from test_tasks import api


def channel(gb, name="design", who="jose", members=("jose/claude", "ana/codex"), **extra):
    return api(gb, "POST", "/channels", who, {"name": name, "members": list(members), **extra})


def post(gb, who, body, channel=None, **extra):
    return api(gb, "POST", "/messages", who, {"kind": "note", "body": body, **({"channel": channel} if channel else {}), **extra})


def test_b55_a_channel_post_reaches_its_members_and_stays_in_the_log(b):
    r = channel(b, topic="Page layout and colours")
    assert r.status_code == 201, r.text
    created = b.events()[-1]
    assert created["type"] == "channel.created" and created["channel"] == "design"
    assert b.recipients("s1", created["seq"]) == ["ana/codex", "jose/claude"]
    seq = post(b, "jose/claude", "Header spacing is 24 px now", channel="design").json()["seq"]
    assert b.recipients("s1", seq) == ["ana/codex", "jose"]          # members; never broadcast
    assert b.raw.execute("SELECT broadcast FROM event WHERE seq=?", (seq,)).fetchone()[0] == 0
    # ana (a human, not a member) still reads it in the shared log
    hist = b.req("GET", "/api/v1/sessions/s1/events?format=text", "ana", key=None).text
    assert_canonical(hist)
    assert "  · channel: «#design»" in hist and "Header spacing is 24 px now" in hist
    only = b.req("GET", "/api/v1/sessions/s1/events?channel=design", "ana", key=None).json()["events"]
    assert {e["channel"] for e in only} == {"design"}


def test_b55_modes_follow_mentions_and_mute(b):
    channel(b, members=("jose/claude", "ana/codex", "ana"))
    api(b, "POST", "/channels/design/mode", "ana/codex", {"mode": "mute"})
    api(b, "POST", "/channels/design/mode", "ana", {"mode": "mentions"})
    assert b.events()[-1]["type"] == "channel.mode" and b.recipients("s1", b.events()[-1]["seq"]) == []
    seq = post(b, "jose/claude", "General update", channel="design").json()["seq"]
    assert b.recipients("s1", seq) == ["jose"]                         # follow only
    reply = post(b, "jose/claude", "To ana", channel="design", to=["ana"]).json()["seq"]
    assert b.recipients("s1", reply) == ["ana"]                        # addressed: always delivered
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Muted but asked?", "to": ["ana/codex"],
                                                    "channel": "design"}).json()["seq"]
    assert b.recipients("s1", q) == ["ana/codex"]                     # a question to a muted member still owes
    asked = post(b, "ana", "my point", channel="design").json()["seq"]
    back = post(b, "jose/claude", "replying", channel="design", re=asked).json()["seq"]
    assert "ana" in b.recipients("s1", back)                          # a reply reaches its author in mentions mode


def test_b55_who_may_post_and_manage(b):
    channel(b, members=("jose/claude",))
    assert post(b, "ana/codex", "not a member", channel="design").status_code == 403    # agents must be members
    assert post(b, "ana", "a human may post anywhere", channel="design").status_code == 201
    assert post(b, "jose/claude", "x", channel="design", to=["ana/codex"]).json()["error"]["code"] == "recipient_not_in_channel"
    assert channel(b, name="ops", who="jose/claude").status_code == 403                  # agents propose, not create
    assert channel(b, name="ops", who="ana/codex").status_code == 403
    assert channel(b, name="design").json()["error"]["code"] == "channel_exists"
    assert post(b, "jose/claude", "x", channel="nope").status_code == 404


def test_b55_post_only_channel_and_archive(b):
    channel(b, name="announce", members=("jose/claude", "ana/codex"), post_policy="listed", posters=["jose/claude"])
    assert post(b, "jose/claude", "Release at five", channel="announce").status_code == 201
    assert post(b, "ana/codex", "can I?", channel="announce").status_code == 403
    r = api(b, "POST", "/channels/announce", "jose", {"add": ["ana"], "topic": "Releases"})
    assert r.status_code == 200 and "+@ana" in r.json()["changes"]
    r = api(b, "POST", "/channels/announce", "jose", {"archive": True})
    assert r.status_code == 200 and b.events()[-1]["type"] == "channel.archived"
    assert post(b, "jose/claude", "late", channel="announce").json()["error"]["code"] == "channel_archived"
    listed = b.req("GET", "/api/v1/sessions/s1/channels?format=text", "ana", key=None).text
    assert_canonical(listed)
    assert "--- #announce · 4 member(s) · post-only · archived · you: follow ---" in listed


def test_b55_cli_and_dashboard(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose/claude", "use", "s1"))
    out = ok(L.cli("jose", "channel", "create", "design", "--topic", "Layout", "--member", "jose/claude"))
    assert out.startswith("#design created with 2 member(s)")
    assert ok(L.cli("jose/claude", "post", "note", "From the channel", "--channel", "design")).startswith("posted #")
    assert "  · channel: «#design»" in ok(L.cli("jose", "history", "--channel", "design"))
    assert ok(L.cli("jose/claude", "channel", "mode", "design", "mute")).startswith("#design: you now mute")
    assert "@jose/claude (mute)" in ok(L.cli("jose", "channel", "list"))
    c = login(gb, "jose")
    page = c.get("/s/s1").text
    assert 'id="channels"' in page and "#design" in page and '<option value="design">#design</option>' in page
    token = csrf_of(page)
    r = c.post("/s/s1/channels", data={"csrf": token, "name": "ops", "topic": "Deploys", "members": "jose/claude"},
               follow_redirects=False)
    assert r.status_code == 303 and gb.events()[-1]["type"] == "channel.created"
    r = c.post("/s/s1/post", data={"csrf": token, "kind": "note", "body": "Via the page", "channel": "ops", "recovery": gb.rec()},
               follow_redirects=False)
    assert r.status_code == 303 and gb.events()[-1]["channel"] == "ops"
    assert "Via the page" in c.get("/s/s1?f=all&channel=ops").text
