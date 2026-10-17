import json
import re

from fastapi.testclient import TestClient

from glassboard import ids
from glassboard.dashboard import LOGIN_COOKIE


def _login(gb, who="jose"):
    client = TestClient(gb.app, base_url="https://testserver", raise_server_exceptions=False,
                        follow_redirects=False)
    client.get("/login")
    response = client.post("/login", data={"csrf": client.cookies[LOGIN_COOKIE], "token": gb.tok[who]})
    assert response.status_code == 303
    return client


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_conversation_view_threads_replies_and_posts_back_to_it(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    question = gb.req("POST", "/api/v1/sessions/s1/messages", "ana",
                      json={"kind": "question", "body": "Need <b>proof</b>?", "to": ["jose"]},
                      rec=gb.rec()).json()["seq"]
    gb.req("POST", "/api/v1/sessions/s1/messages", "jose",
           json={"kind": "answer", "body": "Yes.", "re": question}, rec=gb.rec())
    client = _login(gb)
    response = client.get("/s/s1/conversations")
    assert response.status_code == 200
    assert "Need &lt;b&gt;proof&lt;/b&gt;?" in response.text and "thread-card" in response.text
    assert "Need <b>proof</b>?" not in response.text
    assert "message-replies" not in response.text and response.text.count("reply-composer") == 0
    filtered = client.get("/s/s1/conversations", params={"author": "jose"})
    assert f"mode=thread&amp;thread={question}" in filtered.text
    response = client.get("/s/s1/conversations", params={"mode": "thread", "thread": question})
    assert response.status_code == 200 and "focused-thread" in response.text and "Yes." in response.text
    assert response.text.count('class="message-card') == 2 and "message-replies" not in response.text
    response = client.get("/s/s1/conversations", params={"mode": "thread", "thread": question,
                                                          "reply": question})
    assert response.status_code == 200 and response.text.count('id="reply-composer"') == 1
    response = client.post("/s/s1/post", data={"csrf": _csrf(response.text), "recovery": gb.rec(),
                           "op": ids.new_ulid(), "re": str(question), "return_to": "conversations",
                           "return_thread": str(question),
                           "kind": "note", "to": "ana", "body": "Recorded."})
    assert response.status_code == 303
    assert response.headers["location"] == f"/s/s1/conversations?mode=thread&thread={question}#message-{question}"


def test_long_conversation_message_is_previewed_then_expandable(gb):
    gb.session("s1")
    body = "\n".join(f"line {n} " + ("x" * 70) for n in range(20)) + " UNIQUE-END"
    seq = gb.req("POST", "/api/v1/sessions/s1/messages", "jose",
                 json={"kind": "proposal", "body": body}, rec=gb.rec()).json()["seq"]
    client = _login(gb)
    overview = client.get("/s/s1/conversations")
    assert "UNIQUE-END" not in overview.text
    focused = client.get("/s/s1/conversations", params={"mode": "thread", "thread": seq})
    assert 'class="long-message"' in focused.text and "Show full message" in focused.text
    assert "UNIQUE-END" in focused.text


def test_admin_conversations_searches_all_workspaces_but_requires_visible_membership_to_reply(gb):
    gb.session("alpha")
    gb.session("beta", who="ana")
    gb.req("POST", "/api/v1/sessions/beta/messages", "ana",
           json={"kind": "note", "body": "BETA-ONLY"}, rec=gb.rec("beta"))
    gb.raw.execute("UPDATE principal SET is_admin=1 WHERE handle='jose'")
    client = _login(gb)
    response = client.get("/admin/conversations")
    assert response.status_code == 200 and "BETA-ONLY" in response.text and "beta" in response.text
    assert "All-workspace mode is read-only" in response.text and "Post reply" not in response.text
    response = client.get("/admin/conversations", params={"session": "beta"})
    assert response.status_code == 200 and "Join it visibly" in response.text and "Post reply" not in response.text
    assert "/admin/conversations?session=beta&amp;mode=thread&amp;thread=" in response.text


def test_agent_run_browser_is_admin_only_and_does_not_traverse(gb, tmp_path):
    evidence = tmp_path / "evidence"
    run = evidence / "codex-round01"
    run.mkdir(parents=True)
    (run / "prompt.txt").write_text("Do the work.\n")
    (run / "stdout.jsonl").write_text(json.dumps({"type": "reasoning", "text": "summary"}) + "\n")
    (run / "stderr.bin").write_bytes(b"diagnostic\xff\n")
    (run / "native-01.jsonl").write_text('{"type":"tool","name":"shell"}\n')
    (run / "record.json").write_text(json.dumps({
        "agent": "codex", "round": 1, "status": "complete", "started_at": "2026-10-17T00:00:00Z",
        "ended_at": "2026-10-17T00:01:00Z", "exit_code": 0, "json_events": 1,
        "native_files": [{"copy": "native-01.jsonl", "status": "copied"}],
    }))
    gb.app.state.admin_run_evidence_roots = [str(evidence)]
    ordinary = _login(gb)
    assert ordinary.get("/admin/runs").status_code == 404
    gb.raw.execute("UPDATE principal SET is_admin=1 WHERE handle='jose'")
    admin = _login(gb)
    listing = admin.get("/admin/runs")
    assert listing.status_code == 200 and "codex-round01" in listing.text
    detail = admin.get("/admin/runs/0/codex-round01")
    assert detail.status_code == 200 and "provider reasoning output" in detail.text
    stderr = admin.get("/admin/runs/0/codex-round01/files/stderr.bin")
    assert stderr.status_code == 200 and "diagnostic" in stderr.text
    assert admin.get("/admin/runs/0/codex-round01/files/secrets.txt").status_code == 404
