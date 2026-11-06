"""B54 (ERRATA E37): every event an agent posts records what it had been delivered and had acknowledged."""
import json

from glassboard import inbox
from test_messaging import attach, m, member, post, sync  # noqa: F401  (fixture and helpers)
from test_render import assert_canonical


def last(gb):
    return gb.raw.execute("SELECT * FROM event ORDER BY rowid DESC LIMIT 1").fetchone()


def test_b54_agent_events_record_delivered_and_acked_at_that_moment(m):
    for i in range(3):
        post(m, "ana", body=f"news {i}")
    first = attach(m, "jose/claude")                       # delivers #.. through the head, acks nothing
    consumer = first["consumer"]
    post(m, "jose/claude", body="answering without an ack")
    row, mem = last(m), member(m, "jose/claude")
    assert (row["read_delivered"], row["read_acked"]) == (first["delivered_through"], first["cursor"])
    second = sync(m, "jose/claude", consumer=consumer, ack=first["delivered_through"]).json()
    post(m, "ana", body="later news")                      # not delivered to jose/claude yet
    post(m, "jose/claude", body="after the ack")
    row, mem = last(m), member(m, "jose/claude")
    assert row["read_delivered"] == mem["delivered_seq"] == second["delivered_through"]
    assert row["read_acked"] == mem["cursor_seq"] == first["delivered_through"]
    assert row["read_delivered"] < row["seq"] - 1          # the newer post by ana was not read


def test_b54_human_and_board_events_are_not_stamped(m):
    post(m, "ana", body="from a human")
    row = last(m)
    assert row["read_delivered"] is None and row["read_acked"] is None
    v = m.req("GET", "/api/v1/sessions/s1/events", "ana", key=None).json()["events"][-1]
    assert v["read"] is None
    text = m.req("GET", "/api/v1/sessions/s1/events?format=text", "ana", key=None).text
    assert "had read through" not in text.split("[note]")[-1]


def test_b54_timeline_history_and_export_carry_it_without_secrets(m):
    post(m, "ana", body="news")
    got = attach(m, "jose/claude")
    post(m, "jose/claude", body="reply")
    text = m.req("GET", "/api/v1/sessions/s1/events?format=text", "ana", key=None).text
    assert_canonical(text)
    want = f"  · had read through: «#{got['delivered_through']} (acknowledged #{got['cursor']})»"
    assert want in text.splitlines()
    v = m.req("GET", "/api/v1/sessions/s1/events", "ana", key=None).json()["events"][-1]
    assert v["read"] == {"delivered": got["delivered_through"], "acked": got["cursor"]}
    jl = m.req("GET", "/api/v1/sessions/s1/export?format=jsonl", "ana", key=None).text.strip().splitlines()
    assert json.loads(jl[-1])["read"] == v["read"]
    md = m.req("GET", "/api/v1/sessions/s1/export?format=md", "ana", key=None).text
    assert want in md
    assert set(v["read"]) == {"delivered", "acked"} and all(isinstance(x, int) for x in v["read"].values())


def test_b54_earlier_agent_events_show_as_unknown(m):
    agent = m.pid("jose/claude")
    row = m.raw.execute("SELECT ? AS actor_id, NULL AS read_delivered, NULL AS read_acked", (agent,)).fetchone()
    assert inbox.read_view(row, {agent}) == "unknown"
    assert inbox.read_view(row, set()) is None              # a human or the board: nothing to show
    from glassboard import render
    lines = render.event_lines({"seq": 9, "ts": "2026-11-06T10:00:00.000000Z", "actor": "jose/claude", "type": "note",
                                "body": "old", "data": None, "read": "unknown"})
    assert "  · had read through: «unknown»" in lines


def test_b54_migration_adds_nullable_columns(m):
    cols = {r[1]: r for r in m.raw.execute("PRAGMA table_info(event)")}
    assert cols["read_delivered"][3] == 0 and cols["read_acked"][3] == 0     # nullable: earlier rows stay NULL
