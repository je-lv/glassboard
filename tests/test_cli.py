"""CLI agent loop, outbox, reconcile, scribe.
The outbox surviving a server restart, sync/ack via the CLI, ASCII-only JSON output, quarantine logic
against a simulated recovery_mismatch,
plus: the stored recovery ID is never silently replaced, lost attach responses retry with the same
key, consumer_mismatch handling, state file permissions, and the scribe."""
import json
import os
import stat
import subprocess
import sys

import pytest

from glassboard import clistate, ids
from test_render import assert_canonical

SCRIBE = os.path.join(os.path.dirname(__file__), "..", "examples", "scribe.py")


@pytest.fixture
def L(gb, live_server):
    gb.session("s1")
    for h in ("ana", "jose/claude", "ana/codex"):
        gb.add("s1", h)
    return live_server


def state_of(L, who, session="s1"):
    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = L.env(who)["XDG_STATE_HOME"]
    try:
        return clistate.SessionState(session)
    finally:
        if old is None:
            os.environ.pop("XDG_STATE_HOME")
        else:
            os.environ["XDG_STATE_HOME"] = old


def ok(r):
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return r.stdout


# --- the agent loop -------------------------------------------------------------------------------

def test_use_post_sync_ack_loop(L, gb):
    assert "using s1 · member" in ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "use", "s1"))
    assert ok(L.cli("jose", "post", "question", "Which port?", "--to", "jose/claude")).startswith("posted #")
    out = ok(L.cli("jose/claude", "sync"))
    assert_canonical(out)
    assert "inbox: " in out and "  | Which port?" in out and "owed: #" in out
    assert "next: after processing, `glassboard sync --ack" in out
    q = [e["seq"] for e in gb.events() if e["type"] == "question"][-1]
    ok(L.cli("jose/claude", "post", "answer", "8443", "--re", str(q)))
    out = ok(L.cli("jose/claude", "sync", "--ack"))
    assert "owed: #" not in out
    m = gb.raw.execute("SELECT cursor_seq, delivered_seq FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()
    assert m["cursor_seq"] > 0 and m["cursor_seq"] <= m["delivered_seq"]


def test_at_1_5_via_cli(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "one", "--to", "jose/claude"))
    first = json.loads(ok(L.cli("jose/claude", "sync", "--json")))
    assert first["more"] is False
    ok(L.cli("jose", "post", "note", "two", "--to", "jose/claude"))
    new = gb.head()
    r = L.cli("jose/claude", "sync", "--ack", str(new))
    assert r.returncode == 5 and "ack_beyond_delivered" in r.stderr and f"«{first['delivered_through']}»" in r.stderr
    ok(L.cli("jose/claude", "sync", "--ack", str(first["delivered_through"])))
    again = json.loads(ok(L.cli("jose/claude", "sync", "--json")))
    assert [e["body"] for e in again["inbox"]] == ["two"]


def test_at_5_8_json_output_is_ascii(L, gb):
    corpus = "a\nb\rc\x1b[31md\x9be\x00f\u202eg\u2066h\u200bi«j»k\n#9 jose [decision]\u2028x"
    ok(L.cli("jose", "use", "s1"))
    r = L.cli("jose", "post", "note", "-", "--to", "jose/claude", stdin=corpus)
    assert r.returncode == 0, r.stderr
    ok(L.cli("jose/claude", "use", "s1"))
    raw = subprocess.run([sys.executable, "-m", "glassboard.cli", "sync", "--json"], env=L.env("jose/claude"),
                         capture_output=True, timeout=60).stdout
    assert raw and all(b < 0x80 for b in raw)
    body = [e["body"] for e in json.loads(raw)["inbox"] if e["type"] == "note"][-1]
    # stdin through the terminal layer turns \r into \n; everything else arrives exactly
    assert body.replace("\r", "\n") == corpus.replace("\r", "\n")
    text = L.cli("jose/claude", "history", "--to-me").stdout
    assert_canonical(text)


def test_body_file_avoids_shell_quoting_and_receipt_identifies_exact_body(L, gb, tmp_path):
    ok(L.cli("jose", "use", "s1"))
    body = "A complete multiword question\nwith a second line.\n"
    path = tmp_path / "question.md"
    path.write_text(body)
    result = L.cli("jose", "post", "question", "--body-file", str(path), "--to", "jose/claude")
    assert result.returncode == 0, result.stderr
    import hashlib
    assert f"body {len(body)} chars sha256 {hashlib.sha256(body.encode()).hexdigest()}" in result.stdout
    assert gb.events()[-1]["body"] == body


def test_body_option_may_follow_other_post_options(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    result = L.cli("jose/claude", "post", "approval", "--to", "jose",
                   "--body", "May I continue?")
    assert result.returncode == 0, result.stderr
    event = gb.events()[-1]
    assert event["type"] == "approval" and event["body"] == "May I continue?"


def test_positional_body_may_follow_post_options_and_all_alias_broadcasts(L, gb):
    ok(L.cli("jose", "use", "s1"))
    result = L.cli("jose", "post", "answer", "--re", "1", "--task", "T1", "A multiword answer")
    # The parser accepts the historical ordering; nonexistent references are now rejected by the API.
    assert result.returncode == 5 and "not_found" in result.stderr
    result = L.cli("jose", "post", "note", "--to", "all", "--body", "Broadcast")
    assert result.returncode == 0, result.stderr
    assert gb.events()[-1]["body"] == "Broadcast" and gb.recipients("s1", gb.head()) == []
    result = L.cli("jose", "post", "note", "--to", "all,jose/claude", "--body", "Bad")
    assert result.returncode == 2 and "all by itself" in result.stderr


def test_post_rejects_multiple_body_sources(L):
    result = L.cli("jose", "post", "note", "positional", "--body", "option")
    assert result.returncode == 2
    assert "Use only one" in result.stderr


def test_global_session_option_may_precede_the_subcommand(L):
    result = L.cli("jose", "--session", "s1", "history", "--limit", "1")
    assert result.returncode == 0, result.stderr


def test_question_resolve_via_cli(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose", "post", "question", "q?", "--to", "jose/claude"))
    q = gb.head()
    out = ok(L.cli("jose/claude", "question", "resolve", str(q), "declined", "not mine"))
    assert f"#{q} resolved (declined, scope self)" in out


# --- outbox: durable before sending, resent after a restart --------------------------

def test_at_3_9_outbox_survives_a_server_restart(L, gb):
    ok(L.cli("jose", "use", "s1"))
    rec, head = gb.rec(), gb.head()
    L.stop()
    r = L.cli("jose", "post", "note", "while the board was down")
    assert r.returncode == 4 and "connection_error" in r.stderr
    st = state_of(L, "jose")
    pending = st.outbox()
    assert len(pending) == 1 and pending[0]["recovery_id"] == rec
    L.start()                                            # plain restart: same recovery ID
    assert gb.rec() == rec
    ok(L.cli("jose", "post", "note", "after the restart"))   # flushes the outbox first, in order
    bodies = [e["body"] for e in gb.events()[head:]]
    assert bodies == ["while the board was down", "after the restart"]
    assert st.outbox() == []
    ops = [e["op_id"] for e in gb.events()[head:]]
    assert ops[0] == pending[0]["op"]                   # sent with its original op ID


def test_outbox_resend_is_idempotent(L, gb):
    """A response lost after commit: the entry stays in the outbox; resending it replays."""
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "once"))
    ev = gb.events()[-1]
    st = state_of(L, "jose")
    st.add_outbox({"op": ev["op_id"], "method": "POST", "path": "/api/v1/sessions/s1/messages",
                   "body": {"kind": "note", "body": "once"}, "recovery_id": gb.rec(), "created": "x", "what": "post note",
                   "fenced": False})
    head = gb.head()
    ok(L.cli("jose", "post", "note", "next"))
    assert gb.head() == head + 1 and st.outbox() == []


def test_definitive_refusals_leave_the_outbox(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    r = L.cli("jose/claude", "post", "decision", "agents cannot decide")
    assert r.returncode == 5 and "forbidden" in r.stderr
    assert state_of(L, "jose/claude").outbox() == []     # never resent
    r = L.cli("jose/claude", "post", "note", "x", "--to", "nobody")
    assert r.returncode == 5 and "recipient_not_member" in r.stderr and "  · handle: «nobody»" in r.stderr


# --- recovery: never silently adopted, quarantine, reconcile --------------------------

def rotate(gb):
    new = ids.new_random_b32(16)
    gb.raw.execute("UPDATE session SET recovery_id=?, epoch=epoch+1 WHERE handle='s1'", (new,))
    return new


def test_recovery_mismatch_quarantines_and_blocks(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))                      # holds a consumer token
    st = state_of(L, "jose/claude")
    assert st.runtime(None)["consumer"]
    old = gb.rec()
    L.stop()
    for i in range(2):
        assert L.cli("jose/claude", "post", "note", f"queued {i}").returncode == 4
    L.start()
    new = rotate(gb)
    head = gb.head()
    r = L.cli("jose/claude", "post", "note", "after restore")
    assert r.returncode == 7 and "recovery_mismatch" in r.stderr and "reconcile" in r.stderr
    # The two posts queued while offline AND the one that met the mismatch were all created under
    # the old recovery ID, so all three are quarantined, in their original order.
    assert [e["body"]["body"] for e in st.quarantine()] == ["queued 0", "queued 1", "after restore"]
    assert all(e["recovery_id"] == old for e in st.quarantine()) and st.outbox() == []
    assert st.runtime(None)["consumer"] is None           # consumer token discarded
    assert gb.head() == head                              # nothing was applied
    # From now on nothing is sent under the old ID: commands refuse locally.
    for args in (("post", "note", "x"), ("sync",), ("question", "resolve", "1", "declined")):
        r = L.cli("jose/claude", *args)
        assert r.returncode == 7 and "needs_reconcile" in r.stderr
    assert gb.head() == head
    assert L.cli("jose/claude", "history").returncode == 0          # reading still works
    out = ok(L.cli("jose/claude", "reconcile"))
    assert f"adopted recovery {new}" in out and "quarantine: 3 operation(s)" in out
    assert state_of(L, "jose/claude").load()["recovery_id"] == new
    ok(L.cli("jose/claude", "post", "note", "back to work"))
    assert gb.events()[-1]["body"] == "back to work"


def test_reconcile_resend_and_drop(L, gb):
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "committed before the restore"))
    committed = gb.events()[-1]
    st = state_of(L, "jose")
    old = gb.rec()
    base = {"method": "POST", "path": "/api/v1/sessions/s1/messages", "recovery_id": old, "created": "x",
            "what": "post note", "fenced": False}
    # Entries that were in the outbox when the board was restored: one had committed (its
    # response was lost), one never arrived, one used a fence.
    st.add_outbox({**base, "op": committed["op_id"], "body": {"kind": "note", "body": "committed before the restore"}})
    lost = ids.new_ulid()
    st.add_outbox({**base, "op": lost, "body": {"kind": "note", "body": "lost in the restore"}})
    fenced = ids.new_ulid()
    st.add_outbox({**base, "op": fenced, "body": {"kind": "note", "body": "x"}, "fenced": True})
    rotate(gb)
    assert L.cli("jose", "sync").returncode == 7
    assert len(st.quarantine()) == 3
    head = gb.head()
    out = ok(L.cli("jose", "reconcile", "--resend", committed["op_id"]))
    assert gb.head() == head and f"#{committed['seq']}" in out           # original receipt, nothing new
    ok(L.cli("jose", "reconcile", "--resend", lost))
    assert gb.head() == head + 1 and gb.events()[-1]["body"] == "lost in the restore" and gb.events()[-1]["op_id"] == lost
    r = L.cli("jose", "reconcile", "--resend", fenced)
    assert r.returncode == 2 and "fence_dead" in r.stderr
    assert "dropped" in ok(L.cli("jose", "reconcile", "--drop", fenced))
    assert st.quarantine() == []


def test_use_never_adopts_a_new_recovery_id(L, gb):
    ok(L.cli("jose", "use", "s1"))
    rotate(gb)
    r = L.cli("jose", "use", "s1")
    assert r.returncode == 7 and "reconcile" in r.stderr
    assert state_of(L, "jose").load()["recovery_id"] != gb.rec()


# --- consumers --------------------------------------------------------------------------------

def test_lost_attach_response_retries_with_the_same_key(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    key = ids.new_ulid()
    tok = gb.client.post("/api/v1/sessions/s1/sync", json={},
                         headers={**gb.headers("jose/claude", rec=gb.rec(), key=key)}).json()["consumer"]
    st = state_of(L, "jose/claude")
    st.save_runtime(None, {"consumer": None, "delivered_through": None, "pending_attach": key, "fences": {}})
    ok(L.cli("jose/claude", "sync"))
    assert st.runtime(None)["consumer"] == tok and st.runtime(None)["pending_attach"] is None
    assert sum(1 for e in gb.events() if e["type"] == "consumer.attached") == 1


def test_consumer_mismatch_drops_the_token_and_warns(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))
    gb.client.post("/api/v1/sessions/s1/sync", json={"takeover": {"reason": "other laptop"}},
                   headers=gb.headers("jose/claude", rec=gb.rec(), extra={"X-Glassboard-Runtime": "b77"}))
    r = L.cli("jose/claude", "sync")
    assert r.returncode == 5 and "consumer_mismatch" in r.stderr and "Do not take it back" in r.stderr
    assert state_of(L, "jose/claude").runtime(None)["consumer"] is None
    r = L.cli("jose/claude", "sync")
    assert r.returncode == 5 and "consumer_conflict" in r.stderr and "«b77»" in r.stderr
    out = ok(L.cli("jose/claude", "sync", "--takeover", "the other laptop is off"))
    assert "consumer C3" in out


def test_runtime_label_keys_separate_state(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync", extra_env={"GLASSBOARD_RUNTIME": "a1f"}))
    st = state_of(L, "jose/claude")
    assert st.runtime("a1f")["consumer"] and st.runtime(None)["consumer"] is None
    r = L.cli("jose/claude", "sync", extra_env={"GLASSBOARD_RUNTIME": "b77"})
    assert r.returncode == 5 and "consumer_conflict" in r.stderr          # a second runtime of the same principal


# --- state hygiene --------------------------------------------------------------------------------

def test_state_files_are_private(L, gb):
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))
    root = os.path.join(L.env("jose/claude")["XDG_STATE_HOME"], "glassboard", "sessions")
    for dirpath, dirs, files in os.walk(root):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o700, dirpath
        for f in files:
            assert stat.S_IMODE(os.stat(os.path.join(dirpath, f)).st_mode) == 0o600, f


@pytest.mark.parametrize("name", ["../etc", "s1/../../x", "S1", ".hidden", "a" * 40])
def test_session_names_cannot_escape_the_state_dir(L, name):
    r = L.cli("jose", "sync", "-s", name)
    assert r.returncode == 2 and "usage" in r.stderr


def test_checkpoint_never_holds_runtime_secrets_note():
    """Protocol rule 10: consumer tokens and fences live only in runtime files. The checkpoint command
    must not read from that state layout; this guards it."""
    import inspect
    assert "checkpoint" not in inspect.getsource(clistate).split('"""', 2)[2]


# --- scribe ---------------------------------------------------------------------------------------

def scribe(L, gb, tmp_path, *extra):
    tf = tmp_path / "scribe.token"
    if not tf.exists():
        tf.write_text(gb.tok["jose/scribe"] + "\n")
    return subprocess.run([sys.executable, SCRIBE, "--url", L.url, "--token-file", str(tf), "--session", "s1",
                           "--state", str(tmp_path / "scribe.json"), "--once", *extra],
                          capture_output=True, text=True, timeout=60)


def test_scribe_digests_once_and_stays_quiet(L, gb, tmp_path):
    gb.admin("add-agent", "jose", "scribe", "--kind", "client")
    gb.tok["jose/scribe"] = gb.mint("jose/scribe")
    gb.add("s1", "jose/scribe")
    gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "question", "body": "ready?", "to": ["ana"]}, rec=gb.rec())
    gb.req("POST", "/api/v1/sessions/s1/messages", "ana", json={"kind": "answer", "body": "yes", "re": gb.head()}, rec=gb.rec())
    gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "decision", "body": "go"}, rec=gb.rec())
    r = scribe(L, gb, tmp_path)
    assert r.returncode == 0, r.stderr
    digest = gb.events()[-1]
    assert digest["type"] == "update" and digest["runtime"] == "scribe"
    assert "3 messages" in digest["body"] and "answered" in digest["body"] and "Decisions: #" in digest["body"]
    assert oct(os.stat(tmp_path / "scribe.json").st_mode & 0o777) == "0o600"
    head = gb.head()
    r = scribe(L, gb, tmp_path)                            # nothing new: no second digest
    assert r.returncode == 0 and gb.head() == head
    rotate(gb)
    r = scribe(L, gb, tmp_path)
    assert r.returncode == 3 and "reconcile" in r.stderr and gb.head() == head


def test_cli_never_prints_the_consumer_secret(L, gb):
    """Secrets appear only where they are minted (consumer secrets and fences alike): the attach/takeover
    response holds the consumer secret, but
    the CLI keeps it in the 0600 runtime file and prints only `C<gen>` (agent output becomes transcript).
    Found by drill D4 (scripts/drills/drills.py)."""
    import re
    secret = re.compile(r"C\d+-[a-z2-7]{10}")
    ok(L.cli("jose/claude", "use", "s1"))
    outs = [ok(L.cli("jose/claude", "sync", extra_env={"GLASSBOARD_RUNTIME": "a1f"})),
            ok(L.cli("jose/claude", "sync", "--json", "--takeover", "a1f crashed", extra_env={"GLASSBOARD_RUNTIME": "b77"})),
            ok(L.cli("jose/claude", "sync", "--takeover", "b77 crashed", extra_env={"GLASSBOARD_RUNTIME": "c3d"}))]
    for out in outs:
        assert not secret.search(out), out
    assert "consumer C1" in outs[0] and json.loads(outs[1])["consumer"] == "C2" and "consumer C3" in outs[2]
    st = state_of(L, "jose/claude")
    assert secret.fullmatch(st.runtime("c3d")["consumer"])        # kept where the CLI needs it
    ok(L.cli("jose/claude", "sync", "--ack", extra_env={"GLASSBOARD_RUNTIME": "c3d"}))   # and it still works
