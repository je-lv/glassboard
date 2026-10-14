"""Admin browser controls enforce scope, CSRF, confirmation and durable audit records."""
import re

from glassboard import db
from test_dashboard import csrf_of, login


def _owner(gb):
    assert gb.admin("add-human", "owner", "--admin")[0] == 0
    gb.tok["owner"] = gb.mint("owner")
    return login(gb, "owner")


def test_admin_access_and_token_lifecycle_from_browser(gb):
    owner = _owner(gb)
    assert login(gb, "jose").get("/admin?tab=access").status_code == 404
    access = owner.get("/admin?tab=access")
    assert access.status_code == 200 and "Add human or agent" in access.text
    csrf = csrf_of(access.text)
    create = {"kind": "human", "name": "reviewer", "csrf": csrf}
    assert owner.post("/admin/access/create", data={k: v for k, v in create.items() if k != "csrf"}).status_code == 403
    assert owner.post("/admin/access/create", data=create).status_code == 303
    assert gb.raw.execute("SELECT kind,active FROM principal WHERE handle='reviewer'").fetchone()[:] == ("human", 1)
    assert owner.post("/admin/access/change", data={"csrf": csrf, "handle": "owner",
                                                    "confirm": "owner", "action": "deactivate"}).status_code == 409
    assert owner.post("/admin/access/change", data={"csrf": csrf, "handle": "reviewer",
                                                    "confirm": "wrong", "action": "deactivate"}).status_code == 422
    assert gb.raw.execute("SELECT active FROM principal WHERE handle='reviewer'").fetchone()[0] == 1

    issued = owner.post("/admin/credentials/create", data={"csrf": csrf, "handle": "reviewer",
                                                           "scope": "read", "days": "30", "label": "tablet"})
    assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
    token = re.search(r"gbt_[a-z2-7]{10}_[A-Za-z0-9_-]{43}", issued.text).group()
    credential_id = token.split("_")[1]
    assert token not in owner.get("/admin?tab=credentials").text
    assert owner.post("/admin/credentials/revoke", data={"csrf": csrf, "credential": credential_id}).status_code == 422
    revoked = owner.post("/admin/credentials/revoke", data={"csrf": csrf, "credential": credential_id,
                                                              "confirm": "1"})
    assert revoked.status_code == 303
    assert gb.raw.execute("SELECT revoked_at FROM credential WHERE id=?", (credential_id,)).fetchone()[0]
    assert gb.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    permanent = owner.post("/admin/credentials/create", data={"csrf": csrf, "handle": "reviewer",
                                                              "scope": "read", "days": "", "label": "offline reader"})
    assert permanent.status_code == 200 and "No expiry" in permanent.text
    assert gb.raw.execute("SELECT expires_at FROM credential WHERE label='offline reader'").fetchone()[0] is None
    actions = [r[0] for r in gb.raw.execute("SELECT action FROM audit ORDER BY ts")]
    assert "principal.create" in actions and "credential.create" in actions and "credential.revoke" in actions


def test_admin_can_register_only_a_readable_separate_board(gb, tmp_path):
    owner = _owner(gb)
    csrf = csrf_of(owner.get("/admin?tab=settings").text)
    bad = owner.post("/admin/settings/boards", data={"csrf": csrf, "action": "add", "path": "relative.db"})
    assert bad.status_code == 422
    external_path = tmp_path / "external.db"
    external = db.Database(str(external_path))
    external.migrate()
    external.close()
    added = owner.post("/admin/settings/boards", data={"csrf": csrf, "action": "add", "path": str(external_path)})
    assert added.status_code == 303
    assert gb.app.state.admin_external_dbs == [str(external_path)]
    assert owner.get("/admin?tab=settings").text.count(str(external_path)) >= 1
    assert owner.post("/admin/settings/boards", data={"csrf": csrf, "action": "remove", "index": "0"}).status_code == 422
    assert owner.post("/admin/settings/boards", data={"csrf": csrf, "action": "remove", "index": "0",
                                                      "confirm": "1"}).status_code == 303
    assert gb.app.state.admin_external_dbs == []
    assert [r[0] for r in gb.raw.execute("SELECT action FROM audit WHERE action='admin_external_boards.update'")] == \
        ["admin_external_boards.update", "admin_external_boards.update"]
