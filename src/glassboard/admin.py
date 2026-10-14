"""glassboard-admin: local instance administration on the VM (direct DB access).

Instance-level operations write one `audit` row each and no session event.
The audit actor is the `board` system principal; the OS user who ran the command is recorded in
`data.os_user`. Access reductions (revoke-token, deactivate, revoke-admin) are
appended to security.journal and fsynced before the database change commits. Every
invocation first replays the journal, like the server does at startup.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import backup as backup_mod
from . import admin_auth, db, ids, journal, tlsboot, tokens


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:            # no passwd entry (some containers)
        return f"uid:{os.getuid()}"


def _audit(c, action, target, data, op_id=None):
    c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id) VALUES (?,?,?,?,?,?,?)",
              (db.new_id(), db.utcnow(), ids.BOARD_PRINCIPAL_ID, action, target,
               json.dumps({**data, "os_user": _os_user()}), op_id))


def _seg(s: str) -> str:
    if not tokens.HANDLE_SEG_RE.match(s):
        sys.exit(f"invalid handle segment {s!r}: must match ^[a-z0-9-]{{1,32}}$")
    return s


def _principal(c, handle):
    p = c.execute("SELECT id, kind, active, is_admin FROM principal WHERE handle=?", (handle,)).fetchone()
    if not p:
        raise SystemExit(f"no principal {handle!r}")
    if p["kind"] == "system":
        raise SystemExit("system principals cannot be changed with glassboard-admin")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="glassboard-admin")
    ap.add_argument("--db", default=os.environ.get("GLASSBOARD_DB", "./var/glassboard.db"))
    ap.add_argument("--journal", help="security.journal path (default: GLASSBOARD_JOURNAL, else next to the database)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    h = sub.add_parser("add-human"); h.add_argument("handle"); h.add_argument("--admin", action="store_true"); h.add_argument("--display-name")
    a = sub.add_parser("add-agent"); a.add_argument("owner"); a.add_argument("name"); a.add_argument("--kind", choices=["agent", "client"], default="agent"); a.add_argument("--display-name")
    t = sub.add_parser("mint-token"); t.add_argument("handle"); t.add_argument("--scope", choices=["full", "read"], default="full"); t.add_argument("--expires-days", type=int); t.add_argument("--label"); t.add_argument("--out", help="write token to this file with mode 0600 instead of stdout")
    r = sub.add_parser("revoke-token"); r.add_argument("credential_id")
    d = sub.add_parser("deactivate"); d.add_argument("handle")
    ra = sub.add_parser("revoke-admin"); ra.add_argument("handle")
    rw = sub.add_parser("revoke-web-sessions"); rw.add_argument("handle")
    pw = sub.add_parser("set-admin-password", help="read a new admin password from stdin")
    pw.add_argument("handle")
    bk = sub.add_parser("backup"); bk.add_argument("out")
    bk.add_argument("--age-recipient", action="append", default=[], help="age recipient (repeatable)")
    bk.add_argument("--no-encrypt", action="store_true", help="write a plaintext backup (holds token hashes and receipts)")
    rs = sub.add_parser("restore"); rs.add_argument("backup_file")
    rs.add_argument("--age-identity", help="age identity file for an encrypted backup")
    rs.add_argument("--journal-from", help="off-host copy of security.journal, after a total host loss")
    st = sub.add_parser("setup-tls", help="publish the board CA and print the join command")
    st.add_argument("--host", default=os.environ.get("GLASSBOARD_HOST", "board.lan"))
    st.add_argument("--port", type=int, default=int(os.environ.get("GLASSBOARD_TLS_PORT", "8443")))
    st.add_argument("--ca", help="Caddy's root.crt (default: look in Caddy's usual data directories)")
    args = ap.parse_args(argv)

    if args.cmd == "setup-tls":
        return _setup_tls(args)
    jpath = args.journal or journal.default_path(args.db)
    if args.cmd == "restore":
        # Before anything opens the database: restore needs the instance stopped and swaps the file.
        try:
            r = backup_mod.restore(args.db, args.backup_file, jpath, identity=args.age_identity,
                                   journal_source=args.journal_from)
        except (backup_mod.RestoreError, journal.JournalError) as e:
            print(f"glassboard-admin: restore refused: {e}", file=sys.stderr)
            return 5
        print(f"restored backup taken {r['backup_taken_at']}; journal entries re-applied: {r['journal_entries_reapplied']}")
        for x in r["per_session"]:
            print(f"  {x['session']}: epoch {x['epoch']} · {x['state']} · backup head #{x['backup_head']} · "
                  f"system.restored #{x['restored_event']}")
        print("Sessions are paused; any coordinator may resume them after reconciling.")
        return 0
    database = db.Database(args.db)
    try:
        database.migrate()
        journal.replay(database, jpath)
        if args.cmd == "init":
            print(f"initialised {args.db}")
            return 0
        if args.cmd == "backup":
            try:
                r = backup_mod.backup(database, args.out, jpath, args.age_recipient, args.no_encrypt)
            except backup_mod.RestoreError as e:
                print(f"glassboard-admin: backup refused: {e}", file=sys.stderr)
                return 5
            print(f"backup {r['out']} ({'encrypted' if r['encrypted'] else 'PLAINTEXT'}) taken {r['taken_at']}, "
                  f"{r['sessions']} session(s); journal copy {r['journal_copy']}: store both off-host")
            return 0
        return _run(database, jpath, args)
    except journal.JournalError as e:
        print(f"glassboard-admin: refusing to continue: {e}", file=sys.stderr)
        return 3
    except journal.JournalWriteError as e:
        if e.uncertain:
            print(f"glassboard-admin: security.journal write failed, outcome uncertain: {e}", file=sys.stderr)
        else:
            print(f"glassboard-admin: security.journal write failed, nothing changed: {e}", file=sys.stderr)
        return 4
    finally:
        database.close()


CADDY_ROOTS = ("{xdg}/caddy/pki/authorities/local/root.crt",
               "{home}/.local/share/caddy/pki/authorities/local/root.crt",
               "/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt",
               "/var/lib/caddy/pki/authorities/local/root.crt")


def _setup_tls(args) -> int:
    """Caddy creates its internal CA the first time it starts. This copies that root certificate
    to where the board publishes it (`GET /ca.crt`), then prints its fingerprint and the exact join
    command for participants. Only the public certificate is ever copied, never Caddy's private key."""
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    candidates = [args.ca] if args.ca else [c.format(xdg=xdg, home=home) for c in CADDY_ROOTS]
    src = next((c for c in candidates if c and os.path.isfile(c)), None)
    if src is None:
        print("glassboard-admin: no Caddy root certificate found. Start Caddy once with deploy/Caddyfile (it creates "
              "its internal CA then), or pass --ca PATH/root.crt.", file=sys.stderr)
        return 6
    try:
        with open(src, encoding="ascii") as f:
            pem = tlsboot.check_ca_pem(f.read(tlsboot.MAX_CA_BYTES + 1))
        fp = tlsboot.fingerprint(pem)
    except (OSError, UnicodeDecodeError, tlsboot.TlsBootError) as e:
        print(f"glassboard-admin: setup-tls refused: {e}", file=sys.stderr)
        return 5
    dest = os.environ.get("GLASSBOARD_CA_FILE") or os.path.join(os.path.dirname(os.path.abspath(args.db)), "ca.crt")
    if os.path.abspath(src) != os.path.abspath(dest):
        with open(dest, "w") as f:
            f.write(pem)
    os.chmod(dest, 0o644)
    url = f"https://{args.host}:{args.port}"
    print(f"published {dest} (served at {url}/ca.crt)")
    print(f"board CA fingerprint: {fp}")
    print("Give each participant their token file and this command (the fingerprint is what makes it safe):")
    print(f"  glassboard login --url {url} --fetch-ca --ca-fingerprint {fp} --token-file <their token file>")
    return 0


def _run(database: db.Database, jpath: str, args) -> int:
    with database.write() as c:
        if args.cmd == "add-human":
            pid = db.new_id()
            c.execute("INSERT INTO principal(id,handle,kind,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?)",
                      (pid, _seg(args.handle), "human", args.display_name or args.handle, int(args.admin), db.utcnow()))
            _audit(c, "principal.create", pid, {"handle": args.handle, "kind": "human", "is_admin": bool(args.admin)})
            print(args.handle)
        elif args.cmd == "add-agent":
            owner = c.execute("SELECT id FROM principal WHERE handle=? AND kind='human'", (args.owner,)).fetchone()
            if not owner:
                raise SystemExit(f"no human {args.owner!r}")
            handle = f"{_seg(args.owner)}/{_seg(args.name)}"
            pid = db.new_id()
            c.execute("INSERT INTO principal(id,handle,kind,owner_id,display_name,created_at) VALUES (?,?,?,?,?,?)",
                      (pid, handle, args.kind, owner["id"], args.display_name or handle, db.utcnow()))
            _audit(c, "principal.create", pid, {"handle": handle, "kind": args.kind})
            print(handle)
        elif args.cmd == "mint-token":
            p = _principal(c, args.handle)
            tok, cid, digest = tokens.new_token()
            exp = None
            if args.expires_days is not None:
                exp = (datetime.now(timezone.utc) + timedelta(days=args.expires_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            c.execute("INSERT INTO credential(id,principal_id,secret_sha256,scope,label,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                      (cid, p["id"], digest, args.scope, args.label, db.utcnow(), exp))
            _audit(c, "credential.create", cid, {"principal_id": p["id"], "scope": args.scope})
            if args.out:
                fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(tok + "\n")
                print(f"credential {cid} written to {args.out} (0600)", file=sys.stderr)
            else:
                print(tok)
                print(f"credential {cid}: shown once; store it in a 0600 file.", file=sys.stderr)
        elif args.cmd == "set-admin-password":
            p = _principal(c, args.handle)
            if p["kind"] != "human" or not p["active"] or not p["is_admin"]:
                raise SystemExit("password login requires an active human admin")
            password = getpass.getpass("New admin password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
            try:
                admin_auth.set_password(c, p["id"], password)
            except ValueError as e:
                raise SystemExit(str(e)) from None
            _audit(c, "admin_password.set", p["id"], {})
            print(f"admin password set for {args.handle}; earlier password sessions are invalid")
        # --- access reductions: validate, journal (fsync), apply, commit ------------
        elif args.cmd == "revoke-token":
            row = c.execute("SELECT revoked_at FROM credential WHERE id=?", (args.credential_id,)).fetchone()
            if row is None or row["revoked_at"]:
                print("already revoked or unknown")
                return 0
            e = journal.append(jpath, "credential.revoke", credential_id=args.credential_id)
            journal.apply_entry(c, e)
            _audit(c, "credential.revoke", args.credential_id, {"journal_n": e["n"]}, op_id=e["op_id"])
            print("revoked")
        elif args.cmd == "deactivate":
            p = _principal(c, args.handle)
            if not p["active"]:
                print("already inactive")
                return 0
            e = journal.append(jpath, "principal.deactivate", principal_id=p["id"])
            journal.apply_entry(c, e)
            _audit(c, "principal.deactivate", p["id"], {"journal_n": e["n"]}, op_id=e["op_id"])
            print("deactivated")
        elif args.cmd == "revoke-web-sessions":
            p = _principal(c, args.handle)
            live = c.execute("SELECT count(*) FROM web_session WHERE principal_id=? AND revoked_at IS NULL",
                             (p["id"],)).fetchone()[0]
            if not live:
                print("no live web sessions")
                return 0
            e = journal.append(jpath, "web_session.revoke_all", principal_id=p["id"])
            journal.apply_entry(c, e)
            _audit(c, "web_session.revoke_all", p["id"], {"journal_n": e["n"], "sessions": live}, op_id=e["op_id"])
            print(f"revoked {live} web session(s)")
        elif args.cmd == "revoke-admin":
            p = _principal(c, args.handle)
            if not p["is_admin"]:
                print("not an admin")
                return 0
            e = journal.append(jpath, "principal.admin_revoke", principal_id=p["id"])
            journal.apply_entry(c, e)
            _audit(c, "principal.admin_revoke", p["id"], {"journal_n": e["n"]}, op_id=e["op_id"])
            print("admin revoked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
