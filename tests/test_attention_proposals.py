"""Proposal attention stays actionable and records an explicit human dismissal."""
from glassboard import ids
from test_dashboard import csrf_of, login


def _proposals(gb):
    return [item for item in gb.req("GET", "/api/v1/me/attention", "jose", key=None).json()["items"]
            if item["kind"] == "proposal"]


def test_proposal_dismissal_is_a_visible_reply_and_requires_session_manager(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana")
    proposal = gb.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
                      json={"kind": "proposal", "body": "Split the build into two tasks."}, rec=gb.rec())
    assert proposal.status_code == 201
    seq = proposal.json()["seq"]
    assert [item["seq"] for item in _proposals(gb)] == [seq]
    member = login(gb, "ana")
    denied = member.post(f"/attention/s1/{seq}/dismiss", data={"csrf": csrf_of(member.get("/").text),
                                                                "op": ids.new_ulid()})
    assert denied.status_code == 403
    owner = login(gb)
    page = owner.get("/")
    assert f'aria-label="Dismiss proposal {seq} in s1"' in page.text
    dismissed = owner.post(f"/attention/s1/{seq}/dismiss", data={"csrf": csrf_of(page.text),
                                                                 "op": ids.new_ulid()})
    assert dismissed.status_code == 303 and dismissed.headers["location"] == "/"
    last = gb.events()[-1]
    assert last["type"] == "note" and last["reply_to"] == seq
    assert "Dismissed from the human attention queue" in last["body"]
    assert _proposals(gb) == []


def test_completed_root_removes_old_proposals_but_new_ones_still_appear(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    first = gb.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
                   json={"kind": "proposal", "body": "A build plan."}, rec=gb.rec())
    assert first.status_code == 201 and len(_proposals(gb)) == 1
    created = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
                     json={"title": "Build", "scope": "Make it", "deliverable": "Artifact"}, rec=gb.rec())
    task = created.json()["task"]
    claim = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/claim", "jose/claude", json={}, rec=gb.rec())
    gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/submit", "jose/claude",
           json={"fence": claim.json()["fence"], "summary": "Artifact produced and checked."}, rec=gb.rec())
    current = gb.req("GET", f"/api/v1/sessions/s1/tasks/{task}", "jose", key=None).json()
    accepted = gb.req("POST", f"/api/v1/sessions/s1/tasks/{task}/accept", "jose",
                      json={"if_version": current["version"]}, rec=gb.rec())
    assert accepted.status_code == 200 and _proposals(gb) == []
    later = gb.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
                   json={"kind": "proposal", "body": "A follow-up plan."}, rec=gb.rec())
    assert later.status_code == 201
    assert [item["seq"] for item in _proposals(gb)] == [later.json()["seq"]]
