"""Versioned, owner-editable job prompts do not grant session permissions."""
import json
import re

from fastapi.testclient import TestClient

from glassboard import ids
from glassboard.dashboard import LOGIN_COOKIE


def test_default_roles_are_readable_but_only_admin_can_edit(gb):
    rows = gb.req("GET", "/api/v1/roles", "jose").json()["roles"]
    assert {r["name"] for r in rows} == {"lead", "architect", "security", "builder", "integrator", "verifier"}
    assert all(r["version"] == 1 and r["prompt"] for r in rows)
    body = {"prompt": "Review proposed architecture before implementation.", "if_version": 1}
    assert gb.req("PUT", "/api/v1/roles/architect", "jose", json=body).status_code == 403
    assert gb.req("PUT", "/api/v1/roles/architect", "jose/claude", json=body).status_code == 403
    assert gb.admin("add-human", "owner", "--admin")[0] == 0
    gb.tok["owner"] = gb.mint("owner")
    key = ids.new_ulid()
    changed = gb.req("PUT", "/api/v1/roles/architect", "owner", json=body, key=key)
    assert changed.status_code == 200 and changed.json()["version"] == 2
    replay = gb.req("PUT", "/api/v1/roles/architect", "owner", json=body, key=key)
    assert replay.status_code == 200 and replay.headers["glassboard-replayed"] == "true"
    assert gb.req("PUT", "/api/v1/roles/architect", "owner", json=body).status_code == 409
    assert gb.raw.execute("SELECT count(*) FROM role_template_revision WHERE name='architect'").fetchone()[0] == 2
    revisions = gb.req("GET", "/api/v1/roles/architect/revisions", "jose").json()["revisions"]
    assert [r["version"] for r in revisions] == [2, 1]
    assert revisions[0]["prompt"] == body["prompt"]
    assert gb.raw.execute("SELECT count(*) FROM audit WHERE action='role_template.update'").fetchone()[0] == 1


def test_role_prompt_refuses_token_shape_and_hidden_controls(gb):
    assert gb.admin("add-human", "owner", "--admin")[0] == 0
    gb.tok["owner"] = gb.mint("owner")
    for prompt in ("gbt_aaaaaaaaaa_" + "A" * 43, "Architect\u202e hidden"):
        r = gb.req("PUT", "/api/v1/roles/architect", "owner",
                   json={"prompt": prompt, "if_version": 1})
        assert r.status_code == 422
    assert gb.raw.execute("SELECT version FROM role_template WHERE name='architect'").fetchone()[0] == 1


def test_owner_can_edit_role_prompt_in_browser(gb):
    assert gb.admin("add-human", "owner", "--admin")[0] == 0
    gb.tok["owner"] = gb.mint("owner")
    c = TestClient(gb.app, base_url="https://testserver", raise_server_exceptions=False, follow_redirects=False)
    c.get("/login")
    assert c.post("/login", data={"csrf": c.cookies[LOGIN_COOKIE], "token": gb.tok["owner"]}).status_code == 303
    page = c.get("/owner?tab=roles")
    assert page.status_code == 200 and "Job role prompts" in page.text
    form = re.search(r'<form method="post" action="/owner/roles/architect".*?</form>', page.text, re.S).group()
    values = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', form))
    values["prompt"] = "Approve documented design before dependent work."
    assert c.post("/owner/roles/architect", data=values).status_code == 303
    assert gb.raw.execute("SELECT version,prompt FROM role_template WHERE name='architect'").fetchone()[0] == 2
    refreshed = c.get("/owner?tab=roles").text
    assert "Approve documented design" in refreshed
    architect = re.search(r'<form method="post" action="/owner/roles/architect".*?</form>', refreshed, re.S).group()
    assert "Revision history · 2" in refreshed
    assert "Define the system contract" in refreshed and "Approve documented design" in architect
    created = c.post("/owner/roles", data={"csrf": values["csrf"], "op": ids.new_ulid(),
                                           "if_version": "0", "name": "educator",
                                           "prompt": "Explain the model and its limits."})
    assert created.status_code == 303
    assert "educator" in c.get("/owner?tab=roles").text


def test_role_cli_reads_and_updates_same_catalog(live_server, tmp_path):
    gb = live_server.gb
    assert gb.admin("add-human", "owner", "--admin")[0] == 0
    gb.tok["owner"] = gb.mint("owner")
    assert "architect v1" in live_server.cli("owner", "role", "list").stdout
    assert "Define the system contract" in live_server.cli("owner", "role", "show", "architect").stdout
    prompt = tmp_path / "role.txt"
    prompt.write_text("Review the design and approve it in the task record.\n")
    changed = live_server.cli("owner", "role", "set", "architect", "--prompt-file", str(prompt))
    assert changed.returncode == 0 and "v2" in changed.stdout
    assert gb.raw.execute("SELECT prompt FROM role_template WHERE name='architect'").fetchone()[0] == prompt.read_text()
    history = live_server.cli("owner", "role", "history", "architect", "--json")
    assert history.returncode == 0, history.stderr
    assert [row["version"] for row in json.loads(history.stdout)["revisions"]] == [2, 1]


def test_agents_can_self_organize_job_roles_without_gaining_permissions(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana")
    path = "/api/v1/sessions/s1/job-roles"
    body = {"handle": "jose/claude", "role": "architect"}
    key = ids.new_ulid()
    head = gb.head()
    chosen = gb.req("POST", path, "jose/claude", json=body, rec=gb.rec(), key=key)
    assert chosen.status_code == 200 and chosen.json()["version"] == 1
    assert gb.head() == head + 1
    assert gb.req("POST", path, "jose/claude", json=body, rec=gb.rec(), key=key).status_code == 200
    assert gb.head() == head + 1
    assignments = gb.req("GET", path, "ana").json()["assignments"]
    assignment = next(a for a in assignments if a["handle"] == "jose/claude")
    assert assignment["job_role"] == "architect" and "architecture" in assignment["effective_prompt"]
    assert gb.req("POST", path, "ana", json={"handle": "jose/claude", "role": "lead"}, rec=gb.rec()).status_code == 403
    assert gb.req("POST", path, "jose/claude", json={"handle": "ana", "role": "lead"}, rec=gb.rec()).status_code == 403
    assert gb.req("POST", "/api/v1/sessions/s1/tasks", "jose/claude",
                  json={"title": "x", "scope": "x", "deliverable": "x"}, rec=gb.rec()).status_code == 403
    assert gb.events()[-1]["type"] == "member.jobrole"


def test_coordinator_assigns_job_role_and_browser_shows_it(gb):
    gb.session("s1")
    gb.add("s1", "jose/claude")
    c = TestClient(gb.app, base_url="https://testserver", raise_server_exceptions=False, follow_redirects=False)
    c.get("/login")
    c.post("/login", data={"csrf": c.cookies[LOGIN_COOKIE], "token": gb.tok["jose"]})
    page = c.get("/s/s1")
    assert 'action="/s/s1/job-role"' not in page.text
    form = re.search(r'<form method="post" action="/s/s1/job-role".*?</form>',
                     c.get("/s/s1/team").text, re.S).group()
    values = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', form))
    values.update(handle="jose/claude", role="verifier")
    assert c.post("/s/s1/job-role", data=values).status_code == 303
    html = c.get("/s/s1").text
    assert "Project role: verifier" in html and ">v1</span>" in html
    assert gb.raw.execute("SELECT job_role FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()[0] == "verifier"


def test_role_assignment_cli(live_server):
    gb = live_server.gb
    gb.session("s1")
    gb.add("s1", "jose/claude")
    result = live_server.cli("jose", "role", "assign", "security", "--to", "jose/claude", "-s", "s1")
    assert result.returncode == 0 and "security v1" in result.stdout
    assert gb.raw.execute("SELECT job_role FROM member WHERE principal_id=?", (gb.pid("jose/claude"),)).fetchone()[0] == "security"
    exported = live_server.cli("jose", "role", "assignments", "-s", "s1", "--json")
    assert exported.returncode == 0, exported.stderr
    assignment = next(row for row in json.loads(exported.stdout)["assignments"]
                      if row["handle"] == "jose/claude")
    assert assignment["job_role"] == "security" and assignment["job_role_version"] == 1
