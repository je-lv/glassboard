"""Joining agents with invite codes, per-agent identities (profiles) on one machine, and orderly
access to shared resources (`lease take --wait`, `lease run`)."""
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from test_board import b  # noqa: F401  (fixture)
from test_tasks import api

CODE = re.compile(r"^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$")


def invite(gb, agent="helper", who="jose", key=True):
    return api(gb, "POST", "/invites", who, {"agent": agent}, key=key)


def join(gb, code):
    return gb.client.post("/api/v1/join", json={"code": code})


# --- invites -----------------------------------------------------------------------------------------

def test_invite_creates_the_humans_own_agent_adds_it_and_shows_the_code_once(b):
    r = invite(b, key="01JINVITEKEYAAAAAAAAAAAAAA")
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["agent"] == "jose/helper" and j["session"] == "s1" and j["created_agent"] is True
    assert CODE.match(j["code"]) and j["expires_at"].endswith("Z")
    ev = b.events()[-1]
    assert ev["type"] == "member.added" and ev["seq"] == j["seq"] and ev["data"]["handle"] == "jose/helper"
    # An idempotent replay never shows the code again.
    again = invite(b, key="01JINVITEKEYAAAAAAAAAAAAAA")
    assert again.headers.get("glassboard-replayed") == "true" and again.json()["code"] is None
    # The code is stored only as a hash: nowhere in the database, the event log or the audit trail.
    dump = "\n".join(b.raw.iterdump())
    assert j["code"] not in dump and j["code"].replace("-", "") not in dump


def test_inviting_an_existing_member_adds_no_event(b):
    before = len(b.events())
    r = invite(b, agent="claude")
    assert r.status_code == 201 and r.json()["created_agent"] is False and r.json()["seq"] is None
    assert CODE.match(r.json()["code"]) and len(b.events()) == before


def test_only_human_coordinators_invite_and_only_their_own_active_agents(b):
    assert invite(b, who="ana").status_code == 403                      # a member, not a coordinator
    assert invite(b, who="jose/claude").status_code == 403              # agents never invite
    b.raw.execute("UPDATE principal SET active=0 WHERE handle='jose/claude'")
    r = invite(b, agent="claude")
    assert r.status_code == 404 and r.json()["error"]["message"] == "No such agent of yours."
    for bad in ("Helper", "a/b", "", "x" * 33):
        assert invite(b, agent=bad).status_code == 422, bad
    b.session("s2", who="ana")
    r = b.req("POST", "/api/v1/sessions/s2/invites", "jose", json={"agent": "helper"}, rec=b.rec("s2"))
    assert r.status_code == 404                                          # not a member: the session stays hidden


# --- redeeming -----------------------------------------------------------------------------------------

def test_join_redeems_once_for_a_fresh_token_of_that_agent(b, caplog):
    caplog.set_level(logging.DEBUG)
    code = invite(b).json()["code"]
    r = join(b, code.lower().replace("-", " "))                          # forgiving about case and separators
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["handle"] == "jose/helper" and j["session"] == "s1" and j["recovery_id"] == b.rec()
    assert j["token"].startswith("gbt_")
    who = b.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {j['token']}"}).json()
    assert who["handle"] == "jose/helper"
    r = join(b, code)
    assert r.status_code == 404 and r.json()["error"]["code"] == "invite_invalid"
    assert code not in r.text and code.lower() not in r.text
    assert code not in caplog.text and j["token"] not in caplog.text
    audit = [x[0] for x in b.raw.execute("SELECT action FROM audit ORDER BY ts")]
    assert "invite.create" in audit and "invite.redeem" in audit


def test_expired_codes_archived_sessions_and_deactivated_agents_refuse_alike(b):
    codes = [invite(b, agent=f"a{i}").json()["code"] for i in range(3)]
    b.raw.execute("UPDATE invite SET expires_at='2000-01-01T00:00:00.000000Z' "
                  "WHERE agent_id=(SELECT id FROM principal WHERE handle='jose/a0')")
    b.raw.execute("UPDATE principal SET active=0 WHERE handle='jose/a1'")
    for c in codes[:2]:
        r = join(b, c)
        assert r.status_code == 404 and r.json()["error"]["code"] == "invite_invalid"
    b.raw.execute("UPDATE session SET state='archived' WHERE handle='s1'")
    assert join(b, codes[2]).status_code == 404


def test_bad_codes_count_against_the_auth_failure_limit(b):
    code = invite(b).json()["code"]
    for _ in range(10):
        assert join(b, "AAAA-AAAA-AAAA").status_code == 404
    r = join(b, code)                                                    # even a correct code waits now
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


# --- the CLI: one identity per agent, chosen without environment juggling ------------------------------

def _cli(env, *args, cwd=None):
    return subprocess.run([sys.executable, "-m", "glassboard.cli", *args], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def _handle(out: str) -> str:
    return json.loads(out)["handle"]


def test_cli_join_binds_the_identity_to_the_agent_client_process(b, live_server, tmp_path):
    env = live_server.env("jose")                       # the human's own login on this machine
    code = invite(b).json()["code"]
    client = tmp_path / "fakeagent"                     # stands in for `claude` / `codex`
    shutil.copy("/bin/sh", client)
    py = f"{sys.executable} -m glassboard.cli"
    script = f"{py} join {live_server.url} {code} && {py} whoami --json && {py} env-probe --json"
    r = subprocess.run([str(client), "-c", script], env={**env, "GLASSBOARD_CLIENTS": "fakeagent"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "joined s1 as @jose/helper (profile jose-helper)"
    assert "identity bound to this fakeagent process" in lines[1]
    assert code not in r.stdout and "gbt_" not in r.stdout
    who, probe = json.loads(lines[-2]), json.loads(lines[-1])
    assert who["handle"] == "jose/helper"
    assert probe["profile"] == "jose-helper" and probe["profile.source"].startswith("bound to fakeagent process")
    # The human's own terminal is not below that client: it stays the human.
    assert _handle(_cli(env, "whoami", "--json").stdout) == "jose"
    assert _handle(_cli(env, "--as", "jose-helper", "whoami", "--json").stdout) == "jose/helper"
    wrong = tmp_path / "wrong.token"
    wrong.write_text(b.tok["ana"] + "\n")
    wrong.chmod(0o600)
    mismatch = _cli(env, "--as", "jose-helper", "login", "--url", live_server.url,
                    "--token-file", str(wrong))
    assert mismatch.returncode == 6 and "profile_identity_mismatch" in mismatch.stderr
    assert _handle(_cli(env, "--as", "jose-helper", "whoami", "--json").stdout) == "jose/helper"
    assert _cli(env, "--as", "../jose", "whoami").returncode != 0
    cred = tmp_path / "home-jose" / "cfg" / "glassboard" / "profiles" / "jose-helper" / "cfg" / "glassboard" / "credentials"
    assert oct(cred.stat().st_mode & 0o777) == "0o600"


def test_cli_join_here_writes_a_secret_free_pointer(b, live_server, tmp_path):
    env = live_server.env("jose")
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    r = _cli(env, "join", live_server.url, invite(b).json()["code"], "--here", cwd=work)
    assert r.returncode == 0, r.stderr
    assert (work / ".glassboard-profile").read_text() == "jose-helper\n"
    assert _handle(_cli(env, "whoami", "--json", cwd=work / "sub").stdout) == "jose/helper"
    doctor = _cli(env, "doctor", "--json", cwd=work / "sub")
    report = json.loads(doctor.stdout)
    assert doctor.returncode == 0 and report["status"] == "ok" and report["live_handle"] == "jose/helper"
    cfg = tmp_path / "home-jose" / "cfg" / "glassboard" / "profiles" / "jose-helper" / "cfg" / "glassboard" / "config.json"
    assert json.loads(cfg.read_text())["principal"] == "jose/helper"
    assert _handle(_cli(env, "whoami", "--json", cwd=tmp_path).stdout) == "jose"


def test_doctor_explains_a_dangling_workspace_pointer(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
           "XDG_STATE_HOME": str(tmp_path / "state")}
    work = tmp_path / "work"
    work.mkdir()
    (work / ".glassboard-profile").write_text("friend-codex\n")
    result = _cli(env, "doctor", cwd=work)
    assert result.returncode == 6
    assert "workspace pointer does not contain credentials" in result.stdout


def test_cli_join_then_launch_runs_the_client_as_the_agent(b, live_server, tmp_path):
    env = live_server.env("jose")
    inner = f"{sys.executable} -m glassboard.cli whoami --json"
    r = _cli(env, "join", live_server.url, invite(b).json()["code"], "--", "sh", "-c", inner, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert _handle(r.stdout.splitlines()[-1]) == "jose/helper"           # nested commands find the profile
    assert "starting sh as @jose/helper" in r.stderr


def test_cli_join_refuses_plain_http_to_a_remote_board(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "c"),
           "XDG_STATE_HOME": str(tmp_path / "s")}
    r = _cli(env, "join", "http://192.168.1.20:8080", "AAAA-AAAA-AAAA")
    assert r.returncode != 0 and "https" in r.stderr


# --- orderly access to shared resources -----------------------------------------------------------------

def test_lease_take_wait_queues_behind_the_holder(b, live_server):
    assert live_server.cli("jose/claude", "use", "s1").returncode == 0
    assert live_server.cli("ana/codex", "use", "s1").returncode == 0
    assert live_server.cli("jose/claude", "lease", "take", "docs/faq.md").returncode == 0
    r = live_server.cli("ana/codex", "lease", "take", "docs/faq.md")
    assert r.returncode != 0                                               # without --wait: refused at once
    p = subprocess.Popen([sys.executable, "-m", "glassboard.cli", "lease", "take", "docs/faq.md", "--wait", "30"],
                         env=live_server.env("ana/codex"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert "waiting for jose/claude to release it" in p.stderr.readline()
        assert live_server.cli("jose/claude", "lease", "release", "docs/faq.md").returncode == 0
        out, _ = p.communicate(timeout=30)
    finally:
        p.kill()
    assert p.returncode == 0 and out.startswith("lease taken")
    assert [e["type"] for e in b.events() if e["type"].startswith("lease")][-3:] == \
        ["lease.taken", "lease.released", "lease.taken"]


def test_lease_run_holds_the_lease_for_the_command_and_always_releases(b, live_server):
    assert live_server.cli("ana/codex", "use", "s1").returncode == 0
    probe = f"{sys.executable} -m glassboard.cli lease list; exit 3"
    r = live_server.cli("ana/codex", "lease", "run", "docs/faq.md", "--", "sh", "-c", probe)
    assert r.returncode == 3, r.stderr                                     # the command's own exit status
    assert "held by ana/codex" in r.stdout and "lease released" in r.stdout
    assert [e["type"] for e in b.events()][-2:] == ["lease.taken", "lease.released"]
    assert live_server.cli("ana/codex", "lease", "run", "docs/faq.md").returncode != 0    # no command: usage
    assert b.events()[-1]["type"] == "lease.released"                                     # and no lease taken


# --- the dashboard: humans add agents from the browser -------------------------------------------------

def _dash(gb, who="jose"):
    from test_dashboard import login
    return login(gb, who)


def test_dashboard_add_agent_shows_the_join_steps_once(b):
    from test_dashboard import csrf_of
    c = _dash(b)
    assert 'action="/s/s1/invite"' not in c.get("/s/s1").text
    html = c.get("/agents?tab=invitations").text
    assert 'action="/s/s1/invite"' in html and "Get a join code" in html
    form = {"csrf": csrf_of(html), "recovery": b.rec(), "op": "01JDASHINVITEAAAAAAAAAAAAA", "client": "claude", "agent": ""}
    r = c.post("/s/s1/invite", data=form)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    code = re.search(r'<code id="code" class="joincode">([A-Z2-7-]+)</code>', r.text).group(1)
    assert CODE.match(code) and "@jose/claude" in r.text                  # empty name = the client type
    assert f"glassboard join https://testserver {code}" in r.text and f"{code} -- claude" in r.text
    assert re.findall(r"<script[^>]*>", r.text) == ['<script src="/static/copy.js">', '<script src="/static/notify.js">']
    assert " style=" not in r.text and "onclick" not in r.text
    again = c.post("/s/s1/invite", data=form)                               # resubmitted form: never the code again
    assert again.status_code == 200 and code not in again.text and "already shown once" in again.text
    r = c.post("/s/s1/invite", data={**form, "op": "01JDASHINVITEBBBBBBBBBBBBBB", "client": "codex", "agent": "rev-1"})
    assert "@jose/rev-1" in r.text and "-- codex" in r.text and "network_access" in r.text
    assert "jose/rev-1" in [e["data"]["handle"] for e in b.events() if e["type"] == "member.added"]


def test_dashboard_add_agent_is_for_coordinators_with_valid_names(b):
    from test_dashboard import csrf_of
    c = _dash(b, "ana")
    html = c.get("/agents?tab=invitations").text
    assert 'action="/s/s1/invite"' not in html                              # members do not see the form
    r = c.post("/s/s1/invite", data={"csrf": csrf_of(html), "recovery": b.rec(), "op": "01JDASHINVITECCCCCCCCCCCCCC",
                                     "client": "claude", "agent": "x"})
    assert r.status_code == 303 and r.headers["location"].endswith("err=forbidden")
    c = _dash(b)
    html = c.get("/agents?tab=invitations").text
    r = c.post("/s/s1/invite", data={"csrf": csrf_of(html), "recovery": b.rec(), "op": "01JDASHINVITEDDDDDDDDDDDDDD",
                                     "client": "claude", "agent": "<script>"})
    assert r.status_code == 303 and r.headers["location"].endswith("err=invalid_request")
    r = c.post("/s/s1/invite", data={"recovery": b.rec(), "op": "01JDASHINVITEEEEEEEEEEEEEEE", "client": "claude"})
    assert r.status_code == 403                                             # no CSRF token


def test_options_may_sit_between_the_resource_and_the_command():
    from glassboard.cli import build_parser
    a = build_parser().parse_args(["--as", "jose-helper", "lease", "run", "f.md", "--wait", "5", "--", "sh", "-c", "x --y"])
    assert (a.as_profile, a.resource, a.wait, a.command) == ("jose-helper", "f.md", 5, ["sh", "-c", "x --y"])
    a = build_parser().parse_args(["join", "https://b.lan:8443", "AAAA-AAAA-AAAA", "--fetch-ca", "--ca-fingerprint", "SHA256:x",
                                   "--", "claude", "--resume"])
    assert a.fetch_ca and a.command == ["claude", "--resume"]


def test_skill_is_packaged_identical_and_installs_for_both_clients(tmp_path):
    import pathlib
    root = pathlib.Path(__file__).parents[1]
    packaged = (root / "src/glassboard/skill/SKILL.md").read_text()
    assert packaged == (root / "integrations/claude-skill/glassboard/SKILL.md").read_text()
    assert "glassboard join" in packaged and "Task Manager" in packaged and "--wait 600" in packaged
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    r = _cli(env, "skill", "install")
    assert r.returncode == 0, r.stderr
    for d in (".claude", ".codex"):
        assert (tmp_path / d / "skills" / "glassboard" / "SKILL.md").read_text() == packaged
    assert _cli(env, "skill", "show").stdout == packaged


def test_my_agents_page_creates_agents_and_revokes_tokens(b):
    from test_dashboard import csrf_of
    c = _dash(b)
    html = c.get("/agents?tab=tokens").text
    assert "@jose/claude" in html and "My agents" in html
    cid = b.tok["jose/claude"].split("_")[1]
    assert f"<code>{cid}</code>" in html and b.tok["jose/claude"] not in html          # ids only, never secrets
    form = {"csrf": csrf_of(html), "op": "01JMYAGENTSAAAAAAAAAAAAAAA", "action": "create", "name": "reviewer"}
    assert c.post("/agents", data=form).headers["location"] == "/agents"
    assert "@jose/reviewer" in c.get("/agents").text
    r = c.post("/agents", data={**form, "op": "01JMYAGENTSBBBBBBBBBBBBBBB"})
    assert r.headers["location"].endswith("err=handle_taken")
    r = c.post("/agents", data={"csrf": form["csrf"], "op": "01JMYAGENTSCCCCCCCCCCCCCCC", "action": "revoke", "credential": cid})
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    assert b.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {b.tok['jose/claude']}"}).status_code == 401
    # someone else's credential is not revealed, and a malformed id never reaches the database
    ana_cid = b.tok["ana/codex"].split("_")[1]
    for bad in (ana_cid, "../../x", "<script>"):
        r = c.post("/agents", data={"csrf": form["csrf"], "op": "01JMYAGENTSDDDDDDDDDDDDDDD" + bad[:2].upper(),
                                    "action": "revoke", "credential": bad})
        assert r.headers["location"].endswith("err=not_found"), bad
    assert b.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {b.tok['ana/codex']}"}).status_code == 200


def test_plan_the_work_asks_the_task_manager_for_a_breakdown(b):
    import html as _html
    from test_dashboard import csrf_of
    c = _dash(b)
    page = c.get("/s/s1").text
    assert "asks the agents for a task breakdown" in page                  # no Task Manager yet: all agents
    api(b, "POST", "/members", "jose", {"handle": "ana/codex", "role": "coordinator"})
    page = c.get("/s/s1").text
    assert "asks the Task Manager for a task breakdown" in page and "Access: Task Manager</span>" in page
    form = re.search(r'<form method="post" action="/s/s1/post" class="plan">(.*?)</form>', page, re.S).group(1)
    data = {m[0]: _html.unescape(m[1]) for m in re.findall(r'name="(\w+)" value="([^"]*)"', form)}
    assert data["to"] == "ana/codex" and data["kind"] == "question"
    assert c.post("/s/s1/post", data=data).status_code == 303
    ev = b.events()[-1]
    assert ev["type"] == "question" and "--draft" in ev["body"] and b.recipients("s1", ev["seq"]) == ["ana/codex"]
