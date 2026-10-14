"""Recorded draft checks and the forensic record of unsent messages."""
import json

from test_dashboard import csrf_of, login


def test_preflight_records_safe_draft_without_delivery_and_binds_send(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    path = "/api/v1/sessions/s1/messages/preflight"
    draft = {"kind": "note", "body": "Work is ready for review.", "to": ["ana"]}
    head = gb.head()
    key = "preflight-safe-draft-001"
    checked = gb.req("POST", path, "jose", json=draft, rec=gb.rec(), key=key)
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert (result["status"], result["seq"], result["redacted"]) == ("ready", head + 1, False)
    event = gb.events()[-1]
    assert event["type"] == "message.preflight" and json.loads(event["body"]) == draft
    assert gb.recipients("s1", result["seq"]) == []
    assert gb.req("POST", path, "jose", json=draft, rec=gb.rec(), key=key).content == checked.content
    assert gb.head() == head + 1

    changed = gb.req("POST", "/api/v1/sessions/s1/messages", "jose",
                     json={**draft, "body": "Different text", "preflight_seq": result["seq"]}, rec=gb.rec())
    assert changed.status_code == 422 and changed.json()["error"]["code"] == "invalid_preflight"
    assert gb.head() == head + 1
    sent = gb.req("POST", "/api/v1/sessions/s1/messages", "jose",
                  json={**draft, "preflight_seq": result["seq"]}, rec=gb.rec())
    assert sent.status_code == 201 and gb.head() == head + 2
    assert gb.events()[-1]["data"]["preflight_seq"] == result["seq"]


def test_secret_draft_is_rejected_and_absent_from_record_and_response(gb):
    gb.session("s1")
    secret = gb.tok["jose"]
    checked = gb.req("POST", "/api/v1/sessions/s1/messages/preflight", "jose",
                     json={"kind": "note", "body": f"Do not publish {secret}"}, rec=gb.rec())
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert result["status"] == "rejected" and result["redacted"] is True
    assert result["issues"][0]["code"] == "secret_in_content"
    event = gb.events()[-1]
    assert event["body"] is None and "draft_sha256" not in event["data"]
    assert secret not in checked.text and secret not in str(event)


def test_browser_trace_exports_exact_preflight_subset_and_is_member_only(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="observer")
    draft = {"kind": "note", "body": "An unsent diagnostic."}
    checked = gb.req("POST", "/api/v1/sessions/s1/messages/preflight", "jose",
                     json=draft, rec=gb.rec())
    assert checked.status_code == 200
    c = login(gb)
    main = c.get("/s/s1")
    trace = c.get("/s/s1/trace")
    complete = c.get("/s/s1/record.jsonl")
    subset = c.get("/s/s1/record.jsonl?only=trace")
    assert main.status_code == trace.status_code == complete.status_code == subset.status_code == 200
    assert "An unsent diagnostic." not in main.text
    assert "An unsent diagnostic." in trace.text
    full_rows = [json.loads(line) for line in complete.text.splitlines()]
    trace_rows = [json.loads(line) for line in subset.text.splitlines()]
    assert trace_rows[0]["subset"] == "trace"
    assert trace_rows[1:] == [row for row in full_rows[1:] if row["type"] == "message.preflight"]
    assert c.get("/s/s1/record.jsonl?only=bad").status_code == 400
    assert login(gb, "ana").get("/s/s1/trace").status_code == 200
    gb.session("hidden", who="ana")
    for path in ("/s/hidden/trace", "/s/hidden/record.jsonl"):
        assert c.get(path).status_code == 404


def test_browser_preflight_escapes_attack_text_and_withholds_secret(gb):
    gb.session("s1")
    c = login(gb)
    csrf = csrf_of(c.get("/s/s1").text)
    dangerous = '<img src=x onerror=alert(1)>'
    checked = c.post("/s/s1/preflight", data={"csrf": csrf, "recovery": gb.rec(),
                                                  "op": "browser-preflight-xss-01", "kind": "note",
                                                  "body": dangerous})
    assert checked.status_code == 200 and "Ready" in checked.text
    trace = c.get("/s/s1/trace").text
    assert dangerous not in trace and "&lt;img" in trace
    secret = gb.tok["jose"]
    rejected = c.post("/s/s1/preflight", data={"csrf": csrf, "recovery": gb.rec(),
                                                   "op": "browser-preflight-secret-01", "kind": "note",
                                                   "body": secret})
    assert rejected.status_code == 200 and "Rejected" in rejected.text
    assert secret not in rejected.text and secret not in c.get("/s/s1/trace").text
