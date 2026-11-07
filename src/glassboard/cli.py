"""glassboard: participant CLI. Standard library only; never disables TLS verification."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, clistate, hooks, ids, livesession, profiles, render, runtime, textviews, tlsboot
from .render import error_lines, inline, r2

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_CONN, EXIT_HTTP, EXIT_CONFIG = 0, 2, 3, 4, 5, 6
EXIT_RECOVERY = 7          # the board was restored: stop writing, run `glassboard reconcile`
LAST_UNREAD: str | None = None   # inbox news from the last write (Glassboard-Inbox-Unread)


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "glassboard")


def _paths():
    d = config_dir()
    return d, os.path.join(d, "config.json"), os.path.join(d, "credentials")


class CliError(Exception):
    def __init__(self, code: int, kind: str, message: str, details: dict | None = None,
                 status: int | None = None, retryable: bool = False):
        self.code, self.kind, self.message, self.details = code, kind, message, details or {}
        self.status, self.retryable = status, retryable


def load_config() -> dict:
    _, cfg, _ = _paths()
    if not os.path.exists(cfg):
        raise CliError(EXIT_CONFIG, "not_configured", "Run `glassboard login` first.")
    with open(cfg) as f:
        return json.load(f)


def load_token() -> tuple[str, str]:
    """Credentials file first (works in clients that strip *TOKEN* env vars), then env."""
    _, _, cred = _paths()
    if os.path.exists(cred):
        st = os.stat(cred)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CliError(EXIT_CONFIG, "insecure_credentials",
                           f"{cred} is readable by group/others; run: chmod 600 {cred}")
        with open(cred) as f:
            return f.read().strip(), "file"
    tok = os.environ.get("GLASSBOARD_TOKEN", "").strip()
    if tok:
        return tok, "env"
    raise CliError(EXIT_CONFIG, "no_credentials", "No credentials file and GLASSBOARD_TOKEN is not set.")


def ssl_context(ca_file: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


PLAIN_WARNING = ("warning: plain HTTP, no TLS (board started with GLASSBOARD_INSECURE_HTTP=1): anyone on the LAN "
                 "can read your token and messages.")
_warned = False


def is_loopback_http(url: str) -> bool:
    """Plain http to this machine only. Parsed, not prefix-matched: `http://localhost.example` is NOT loopback."""
    u = urllib.parse.urlsplit(url)
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")


def request(cfg: dict, method: str, path: str, token: str | None = None, body=None, accept_text=False,
            extra_headers: dict | None = None, raw_body: bytes | None = None, return_bytes: bool = False,
            timeout: float = 30):
    global _warned
    base = cfg["url"].rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise CliError(EXIT_CONFIG, "invalid_config", "The configured board URL must be an http or https URL without credentials.")
    url = base + path
    if parsed.scheme != "https" and not is_loopback_http(url):
        if not cfg.get("insecure_http"):
            raise CliError(EXIT_USAGE, "insecure_url", "Use https:// (plain http needs `glassboard login --insecure-http`).")
        if not _warned:
            print(PLAIN_WARNING, file=sys.stderr)
            _warned = True
    headers = {"User-Agent": f"glassboard-cli/{__version__}", "Accept": "text/plain" if accept_text else "application/json",
               **(extra_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    label, _ = runtime.resolve()
    if label:
        headers["X-Glassboard-Runtime"] = label
    if body is not None and raw_body is not None:
        raise ValueError("body and raw_body are mutually exclusive")
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = ssl_context(cfg.get("ca_file")) if parsed.scheme == "https" else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:  # nosec B310 - scheme allowlisted above
            payload = resp.read()
            global LAST_UNREAD
            LAST_UNREAD = resp.headers.get("Glassboard-Inbox-Unread")
            if return_bytes:
                return resp.status, payload
            raw = payload.decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)["error"]
        except Exception:
            # Canonical text errors: "ERROR <code>: <message>" on the first line.
            first = raw.split("\n", 1)[0]
            if first.startswith("ERROR ") and ": " in first:
                code_, _, msg = first[6:].partition(": ")
                err = {"code": code_, "message": msg}
            else:
                err = {"code": "http_error", "message": f"HTTP {e.code}"}
        code = EXIT_AUTH if e.code == 401 else EXIT_HTTP
        if err.get("code") == "recovery_mismatch":
            code = EXIT_RECOVERY
        raise CliError(code, str(err.get("code", "http_error")), str(err.get("message", "")),
                       {k: v for k, v in err.items() if k in ("hint", "request_id", "details")},
                       status=e.code, retryable=bool(err.get("retryable")) or e.code >= 500 or e.code == 429)
    except urllib.error.URLError as e:
        reason = e.reason
        kind = "tls_error" if isinstance(reason, ssl.SSLError) else "connection_error"
        raise CliError(EXIT_CONN, kind, str(reason), retryable=kind == "connection_error")
    except (TimeoutError, ConnectionError) as e:        # raised outside URLError on reads
        raise CliError(EXIT_CONN, "connection_error", str(e), retryable=True)


def _print_error(e: CliError) -> None:
    """Text errors: `ERROR <code>: <message>`, `hint: …`, then one Inline line per detail.
    Server strings are untrusted here: the code is reduced to [a-z_], message and hint to one line."""
    def one_line(t) -> str:
        return " ".join(r2(str(t)).split())[:300]
    code = "".join(ch for ch in str(e.kind).lower() if ch.isalnum() or ch == "_")[:64] or "error"
    details = dict(e.details.get("details") or {}) if isinstance(e.details.get("details"), dict) else {}
    details.update({k: v for k, v in e.details.items() if k not in ("hint", "request_id", "details")})
    lines = error_lines(code, one_line(e.message) or "Request failed.",
                        one_line(e.details["hint"]) if e.details.get("hint") else None, details,
                        one_line(e.details["request_id"]) if e.details.get("request_id") else None)
    print("\n".join(lines), file=sys.stderr)


def cmd_login(a) -> int:
    d, cfg_path, cred_path = _paths()
    os.makedirs(d, mode=0o700, exist_ok=True)
    https = a.url.startswith("https://")
    if not https and not is_loopback_http(a.url) and not (a.insecure_http and a.url.startswith("http://")):
        raise CliError(EXIT_USAGE, "insecure_url", "Use https:// (plain http only for loopback, or with --insecure-http "
                                                   "when the board runs in plain-HTTP mode).")
    if a.insecure_http and (https or a.ca_file or a.fetch_ca):
        raise CliError(EXIT_USAGE, "usage", "--insecure-http is for http:// boards only, without --ca-file/--fetch-ca.")
    if a.fetch_ca and (not https or a.ca_file):
        raise CliError(EXIT_USAGE, "usage", "--fetch-ca needs an https:// URL and replaces --ca-file.")
    if a.ca_fingerprint and not a.fetch_ca:
        raise CliError(EXIT_USAGE, "usage", "--ca-fingerprint goes with --fetch-ca.")
    if a.token_file:
        with open(a.token_file) as f:
            token = f.read().strip()
    elif a.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        raise CliError(EXIT_USAGE, "no_token", "Provide --token-file or --token-stdin.")
    ca_file = os.path.abspath(a.ca_file) if a.ca_file else None
    if a.fetch_ca:
        ca_file = _fetch_ca(a, d)
    cfg = {"url": a.url, "ca_file": ca_file}
    if a.insecure_http and not is_loopback_http(a.url):
        cfg["insecure_http"] = True
    status, raw = request(cfg, "GET", "/api/v1/whoami?format=text", token=token, accept_text=True)
    match = re.match(r"^@([a-z0-9-]+(?:/[a-z0-9-]+)?) ", raw)
    if match is None:
        raise CliError(EXIT_HTTP, "invalid_response", "The board returned an invalid identity response.")
    handle = match.group(1)
    selected = os.environ.get("GLASSBOARD_PROFILE")
    if selected and selected != profiles.name_for(handle):
        raise CliError(EXIT_CONFIG, "profile_identity_mismatch",
                       "The authenticated identity does not match the selected profile.",
                       {"hint": f"Profile {selected} expects @{selected.replace('-', '/', 1)}; token is for @{handle}. "
                                "Use the right token or choose a different --as profile."})
    cfg["principal"] = handle
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(cred_path, 0o600)
    sys.stdout.write(r2(raw, allow_newline=True))
    return EXIT_OK


def _fetch_ca(a, config_dir: str) -> str:
    """Download the board's root certificate and pin it by fingerprint (tlsboot.py explains why
    the one unverified download is safe). Without --ca-fingerprint a human must confirm on a terminal."""
    try:
        want = tlsboot.normalize(a.ca_fingerprint) if a.ca_fingerprint else None
    except tlsboot.TlsBootError as e:           # checked before any network access
        raise CliError(EXIT_USAGE, "usage", str(e)) from None
    try:
        pem = tlsboot.fetch_ca(a.url)
        got = tlsboot.fingerprint(pem)
    except tlsboot.TlsBootError as e:
        raise CliError(EXIT_CONN, "ca_fetch_failed", str(e)) from None
    if want is None:
        if a.token_stdin or not sys.stdin.isatty():
            raise CliError(EXIT_USAGE, "ca_fingerprint_required",
                           "Pass --ca-fingerprint (from your board admin) or run this on a terminal to confirm it.")
        print(f"board CA fingerprint: {got}")
        if input("Does it match the fingerprint your board admin gave you? [y/N] ").strip().lower() not in ("y", "yes"):  # nosec B322 - Python 3 only
            raise CliError(EXIT_USAGE, "ca_not_confirmed", "Not confirmed; nothing was saved.")
    elif want != got:
        raise CliError(EXIT_CONN, "ca_fingerprint_mismatch",
                       "The certificate the board sent does not match the fingerprint you were given. Nothing was "
                       "saved and no token was sent. Ask your board admin before trying again.")
    path = os.path.join(config_dir, "ca.crt")
    with open(path, "w") as f:
        f.write(pem)
    os.chmod(path, 0o644)                   # a public certificate
    print(f"pinned the board CA ({got})", file=sys.stderr)
    return path


def redeem(cfg: dict, code: str, local: bool = False) -> dict:
    """Redeem a join code with the board named by `cfg` (url, ca_file, insecure_http): store the agent's
    profile (in ./.glassboard with `local`), activate it for this process and its children, and fetch the
    session's recovery ID. Returns the join response; its token is written to the profile only (E1)."""
    _, raw = request(cfg, "POST", "/api/v1/join", body={"code": code})
    j = json.loads(raw)
    name = profiles.name_for(j["handle"])
    if local:
        profiles.use_local(name, os.getcwd())
    profiles.activate(name)
    d, cfg_path, cred_path = _paths()
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump({**cfg, "session": j["session"], "principal": j["handle"]}, f, indent=2)
    fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(j["token"] + "\n")
    os.chmod(cred_path, 0o600)
    s = _fetch_session(load_config(), j["token"], j["session"])
    clistate.SessionState(s["session"]).save(_session_record(s))
    print(f"joined {r2(j['session'])} as @{r2(j['handle'])} (profile {name})")
    return {k: v for k, v in j.items() if k != "token"}


def cmd_join(a) -> int:
    """Redeem an invite code: this agent's own identity (a profile), joined to the session, bound to the
    calling client process (or `--here`, or the client launched after `--`). No token is ever pasted."""
    if a.insecure_http and a.url.startswith("https://"):
        raise CliError(EXIT_USAGE, "usage", "--insecure-http is for http:// boards only.")
    if getattr(a, "local", False) and not a.here:
        raise CliError(EXIT_USAGE, "usage", "--local goes with --here (the profile lives in this directory).")
    if not a.url.startswith("https://") and not is_loopback_http(a.url) and not a.insecure_http:
        raise CliError(EXIT_USAGE, "insecure_url", "Use https:// (plain http only for loopback, or with --insecure-http).")
    staging = os.path.join(os.getcwd(), profiles.LOCAL_DIR) if getattr(a, "local", False) \
        else os.path.join(profiles.ORIG_CONFIG, "glassboard")
    if a.fetch_ca:                                   # only a fetched CA needs the staging directory
        os.makedirs(staging, mode=0o700, exist_ok=True)
    ca_file = os.path.abspath(a.ca_file) if a.ca_file else (_fetch_ca(a, staging) if a.fetch_ca else None)
    cfg = {"url": a.url, "ca_file": ca_file}
    if a.insecure_http and not is_loopback_http(a.url):
        cfg["insecure_http"] = True
    j = redeem(cfg, a.code, local=getattr(a, "local", False))
    name = profiles.name_for(j["handle"])
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if cmd:                                                  # launch the client as this identity
        if a.here:                                           # the folder keeps its identity after the client exits
            print(f"identity file: {profiles.write_pointer(name, local=getattr(a, 'local', False))} (no secret inside)")
        print(f"starting {r2(cmd[0])} as @{r2(j['handle'])}…", file=sys.stderr)
        livesession.hold_for_client()                         # a wake supervisor sees this client as open (E31)
        runtime.run(_with_compaction(cmd, getattr(a, "compact_at", None)))
        return EXIT_OK
    if a.here:
        if getattr(a, "local", False):
            print(f"identity file: {profiles.write_pointer(name, local=True)}; config, credential and state live in "
                  f"./{profiles.LOCAL_DIR} (git-ignored), so commands never write outside this directory")
        else:
            print(f"identity file: {profiles.write_pointer(name)} (no secret inside); commands run below this directory use it")
    else:
        bound = profiles.bind(name)
        if bound:
            print(f"identity bound to this {r2(bound[1])} process: its glassboard commands now act as @{r2(j['handle'])}")
        else:
            print(f"identity file: {profiles.write_pointer(name)} (no agent client process found above this shell)")
    print(f"profile config: {profiles.profile_root(name)}")
    print("Keep that profile directory and its state subdirectory persistent. The workspace pointer alone cannot restore credentials.")
    if os.path.exists("/.dockerenv") and not getattr(a, "local", False):   # --local: state is in the (mounted) workspace
        print("warning: this appears to be a container; mount persistent config and state storage before it exits.",
              file=sys.stderr)
    print("next: glassboard doctor && glassboard sync     (then read docs: glassboard protocol)")
    return EXIT_OK


SKILL_HOMES = {"claude": (".claude", "skills"), "codex": (".codex", "skills")}


def cmd_skill(a) -> int:
    """Print the packaged agent skill, or install it where Claude Code and Codex look for skills."""
    from importlib import resources
    text = (resources.files("glassboard") / "skill" / "SKILL.md").read_text(encoding="utf-8")
    if a.action == "show":
        sys.stdout.write(text)
        return EXIT_OK
    targets = list(SKILL_HOMES) if a.client == "all" else [a.client]
    for t in targets:
        d = os.path.join(a.home or os.path.expanduser("~"), *SKILL_HOMES[t], "glassboard")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"installed the glassboard skill for {t}: {os.path.join(d, 'SKILL.md')}")
    print("Agents started after this can be told: \"join my Glassboard session with code …\"")
    return EXIT_OK


def cmd_whoami(a) -> int:
    cfg = load_config()
    tok, _ = load_token()
    if a.json:
        _, raw = request(cfg, "GET", "/api/v1/whoami", token=tok)
        result = json.loads(raw)
        _warn_profile_identity(cfg, result["handle"])
        print(json.dumps(result, ensure_ascii=True))
    else:
        _, raw = request(cfg, "GET", "/api/v1/whoami?format=text", token=tok, accept_text=True)
        match = re.match(r"^@([a-z0-9-]+(?:/[a-z0-9-]+)?) ", raw)
        if match:
            _warn_profile_identity(cfg, match.group(1))
        sys.stdout.write(r2(raw, allow_newline=True))
    return EXIT_OK


def _warn_profile_identity(cfg: dict, handle: str) -> None:
    expected = cfg.get("principal")
    selected = os.environ.get("GLASSBOARD_PROFILE")
    if expected and expected != handle:
        print(f"warning: configured profile identity @{r2(expected)} authenticated as @{r2(handle)}; run `glassboard doctor`.",
              file=sys.stderr)
    if selected and profiles.name_for(handle) != selected:
        print(f"warning: profile {r2(selected)} authenticated as @{r2(handle)}; run `glassboard doctor`.", file=sys.stderr)


def cmd_doctor(a) -> int:
    """Check the selected profile, workspace pointer, live principal and writable state without exposing secrets."""
    name = getattr(a, "resolved_profile", None)
    source = getattr(a, "profile_source", "default")
    pointed = profiles.pointer()
    report = {"profile": name, "profile_source": source,
              "pointer": pointed[1] if pointed else None, "pointer_profile": pointed[0] if pointed else None}
    problems = []
    if name and not profiles.exists(name):
        problems.append("selected profile is missing (the workspace pointer does not contain credentials)")
        report.update(status="failed", problems=problems, profile_root=profiles.profile_root(name))
    else:
        cfg, tok = _authed()
        _, raw = request(cfg, "GET", "/api/v1/whoami", token=tok)
        live = json.loads(raw)
        expected = cfg.get("principal")
        report.update(live_handle=live["handle"], configured_handle=expected,
                      config_root=config_dir(), state_root=clistate.state_root())
        if expected and expected != live["handle"]:
            problems.append("configured identity does not match the live credential")
        if name and profiles.name_for(live["handle"]) != name:
            problems.append("profile name does not match the live credential")
        if pointed and name and pointed[0] != name:
            problems.append("workspace pointer selects a different profile")
        for label, path in (("config", config_dir()), ("state", clistate.state_root())):
            parent = path if os.path.exists(path) else os.path.dirname(path)
            if not os.access(parent, os.W_OK):
                problems.append(f"{label} root is not writable")
        report.update(status="failed" if problems else "ok", problems=problems)
    if a.json:
        _print_json(report)
    else:
        for key, value in report.items():
            if key != "problems":
                print(f"{key}: {value}")
        for problem in problems:
            print(f"problem: {r2(problem)}")
    return EXIT_CONFIG if problems else EXIT_OK


def cmd_protocol(a) -> int:
    cfg = load_config()
    sys.stdout.write(r2(_call(cfg, None, "GET", "/api/v1/protocol", accept_text=True), allow_newline=True))
    return EXIT_OK


def cmd_discovery(a) -> int:
    cfg = load_config()
    _, raw = request(cfg, "GET", "/api/v1/discovery")
    print(json.dumps(json.loads(raw), ensure_ascii=True, indent=None if a.json else 2))
    return EXIT_OK


def cmd_env_probe(a) -> int:
    """Report what this process can see. Never prints secret values."""
    label, source = runtime.resolve()
    _, cfg_path, cred_path = _paths()
    tok_env = os.environ.get("GLASSBOARD_TOKEN")
    report = {
        "cli_version": __version__, "python": platform.python_version(), "platform": sys.platform,
        "env.GLASSBOARD_RUNTIME": os.environ.get("GLASSBOARD_RUNTIME"),
        "env.GLASSBOARD_TOKEN_present": bool(tok_env),
        "runtime.resolved": label, "runtime.source": source,
        "profile": os.environ.get("GLASSBOARD_PROFILE"), "profile.source": os.environ.get("GLASSBOARD_PROFILE_SOURCE"),
        "config_present": os.path.exists(cfg_path),
        "credentials_file_present": os.path.exists(cred_path),
        "credentials_file_mode": oct(os.stat(cred_path).st_mode & 0o777) if os.path.exists(cred_path) else None,
        "ppid": os.getppid(),
    }
    if a.json:
        print(json.dumps(report, ensure_ascii=True))
    else:
        for k, v in report.items():
            print(f"{k}: {v}")
    return EXIT_OK




# --- account-level commands ----------------------------------------------------------------

def _authed():
    cfg = load_config()
    tok, _ = load_token()
    return cfg, tok


def _call(cfg, tok, method, path, body=None, headers=None, accept_text=False):
    _, raw = request(cfg, method, path, token=tok, body=body, extra_headers=headers, accept_text=accept_text)
    if accept_text:
        return raw
    return json.loads(raw) if raw else {}


def _call_bytes(cfg, tok, method, path, *, raw_body: bytes | None = None, headers=None) -> bytes:
    _, raw = request(cfg, method, path, token=tok, raw_body=raw_body, extra_headers=headers, return_bytes=True)
    return raw


def _quote(path_part: str) -> str:
    return urllib.parse.quote(path_part, safe="/")


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=True))          # JSON output is pure ASCII


def cmd_session_create(a) -> int:
    cfg, tok = _authed()
    body = {"handle": a.handle, "title": a.title}
    if a.autonomous:
        body["autonomous"] = True
    if a.stale_after is not None:
        body["stale_after_s"] = a.stale_after
    r = _call(cfg, tok, "POST", "/api/v1/sessions", body, {"Idempotency-Key": ids.new_ulid()})
    clistate.SessionState(r["session"]).save(_session_record(r))
    if a.json:
        _print_json(r)
    else:
        print(f"session {r2(r['session'])} created · you are coordinator · recovery {r2(r['recovery_id'])}")
        if r["autonomous"]:
            print("  · autonomous: agent coordinators may complete peer-reviewed child tasks")
        print(f"  · title: {inline(r['title'])}")
    return EXIT_OK


def cmd_session_state(a) -> int:
    """pause → paused, resume → active (also unarchives), archive → archived."""
    target = {"pause": "paused", "resume": "active", "archive": "archived"}[a.action]
    ctx = open_session(a)
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/state", {"state": target, "reason": a.reason}, f"session {a.action}")
    print(f"{r2(r['session'])}: {r2(r['from'])} → {r2(r['state'])} · #{r['seq']}")
    return EXIT_OK


def cmd_sessions(a) -> int:
    cfg, tok = _authed()
    r = _call(cfg, tok, "GET", "/api/v1/sessions")
    if a.json:
        _print_json(r)
        return EXIT_OK
    if not r["sessions"]:
        print("no sessions")
    for s in r["sessions"]:
        print(f"{r2(s['session'])} · {r2(s['role'])} · {r2(s['state'])}"
              + (" · autonomous" if s["autonomous"] else "")
              + f" · epoch {s['epoch']} · recovery {r2(s['recovery_id'])} · head #{s['head']}")
        print(f"  · title: {inline(s['title'])}")
    return EXIT_OK


def cmd_agent(a) -> int:
    cfg, tok = _authed()
    r = _call(cfg, tok, "POST", "/api/v1/me/agents", {"name": a.name, "kind": a.kind}, {"Idempotency-Key": ids.new_ulid()})
    print(f"@{r2(r['handle'])} ({r2(r['kind'])}) registered; mint a token with: glassboard token new {r2(r['handle'])} --out FILE")
    return EXIT_OK


def cmd_token(a) -> int:
    cfg, tok = _authed()
    key = {"Idempotency-Key": ids.new_ulid()}
    if a.action == "revoke":
        if not a.target:
            raise CliError(EXIT_USAGE, "usage", "usage: glassboard token revoke CREDENTIAL_ID")
        r = _call(cfg, tok, "DELETE", f"/api/v1/me/tokens/{_quote(a.target)}", headers=key)
        print(f"credential {r2(r['id'])} revoked")
        return EXIT_OK
    body = {"scope": a.scope}
    if a.target:
        body["principal"] = a.target
    if a.expires_days is not None:
        body["expires_days"] = a.expires_days
    if a.label:
        body["label"] = a.label
    if a.out:
        # Create the file before asking the server, so a bad path never costs a shown-once secret.
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            r = _call(cfg, tok, "POST", "/api/v1/me/tokens", body, key)
        except BaseException:
            os.close(fd)
            os.unlink(a.out)
            raise
        with os.fdopen(fd, "w") as f:
            f.write(r["secret"] + "\n")
        print(f"credential {r2(r['id'])} for @{r2(r['principal'])} written to {a.out} (0600)", file=sys.stderr)
    else:
        r = _call(cfg, tok, "POST", "/api/v1/me/tokens", body, key)
        print(r["secret"])
        print(f"credential {r2(r['id'])} for @{r2(r['principal'])}: shown once; store it in a 0600 file.", file=sys.stderr)
    return EXIT_OK


def cmd_role(a) -> int:
    """Read or edit the same versioned prompt catalog as the Owner browser page."""
    if a.action == "assignments":
        ctx = open_session(a, allow_reconcile=True)
        result = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/job-roles")
        if a.json:
            _print_json(result)
        else:
            for item in result["assignments"]:
                print(f"@{r2(item['handle'])} · {r2(item['permission'])} · "
                      f"{r2(item['job_role'] or 'none')}"
                      + (f" v{item['job_role_version']}" if item['job_role_version'] else ""))
        return EXIT_OK
    if a.action in ("assign", "clear"):
        if not a.to or (a.action == "assign" and not a.name):
            raise CliError(EXIT_USAGE, "usage", "role assign NAME --to HANDLE (or role clear --to HANDLE)")
        ctx = open_session(a)
        with ctx.state.locked():
            result = send_op(ctx, "POST", f"{ctx.path}/job-roles",
                             {"handle": a.to, "role": a.name if a.action == "assign" else None},
                             f"role {a.action}")
        print(f"@{r2(result['handle'])} job role {r2(result['role'] or 'none')}"
              + (f" v{result['version']}" if result["version"] else "")
              + (f" · #{result['seq']}" if result["seq"] else ""))
        return EXIT_OK
    cfg, tok = _authed()
    roles = _call(cfg, tok, "GET", "/api/v1/roles")["roles"]
    if a.action == "list":
        if a.json:
            _print_json({"roles": roles})
        else:
            for role in roles:
                print(f"{role['name']} v{role['version']} · updated {role['updated_at'][:19]}Z by @{role['updated_by']}")
        return EXIT_OK
    if not a.name:
        raise CliError(EXIT_USAGE, "usage", f"role {a.action} needs a role name")
    selected = next((r for r in roles if r["name"] == a.name), None)
    if a.action == "history":
        revisions = _call(cfg, tok, "GET", f"/api/v1/roles/{_quote(a.name)}/revisions")
        if a.json:
            _print_json(revisions)
        else:
            for revision in revisions["revisions"]:
                print(f"{revision['name']} v{revision['version']} · {revision['updated_at'][:19]}Z by @{revision['updated_by']}")
                print(r2(revision["prompt"], allow_newline=True))
        return EXIT_OK
    if a.action == "show":
        if selected is None:
            raise CliError(EXIT_USAGE, "not_found", "No such job role.")
        if a.json:
            _print_json(selected)
        else:
            print(f"{selected['name']} v{selected['version']} · @{selected['updated_by']}")
            print(r2(selected["prompt"], allow_newline=True))
        return EXIT_OK
    if not a.prompt_file:
        raise CliError(EXIT_USAGE, "usage", "role set needs --prompt-file FILE")
    with open(a.prompt_file, encoding="utf-8") as stream:
        prompt = stream.read()
    version = selected["version"] if selected else 0
    result = _call(cfg, tok, "PUT", f"/api/v1/roles/{_quote(a.name)}",
                   {"prompt": prompt, "if_version": version}, {"Idempotency-Key": ids.new_ulid()})
    print(f"{r2(result['name'])} prompt saved as v{result['version']}")
    return EXIT_OK


# --- session-scoped commands: stored recovery ID, outbox, quarantine --------

def _session_record(s: dict) -> dict:
    return {"session": s["session"], "id": s["id"], "recovery_id": s["recovery_id"], "epoch": s["epoch"],
            "needs_reconcile": False, "old_recovery_id": None}


class Ctx:
    def __init__(self, cfg, tok, state: clistate.SessionState, info: dict, label: str | None):
        self.cfg, self.tok, self.state, self.info, self.label = cfg, tok, state, info, label

    @property
    def path(self) -> str:
        return f"/api/v1/sessions/{_quote(self.info['session'])}"


def _session_name(a, cfg) -> str:
    name = getattr(a, "session", None) or getattr(a, "global_session", None) or cfg.get("session")
    if not name:
        raise CliError(EXIT_USAGE, "no_session", "Name a session with -s SESSION or run `glassboard use SESSION`.")
    if not clistate.SESSION_ARG_RE.match(name):
        raise CliError(EXIT_USAGE, "usage", "Session names are lowercase handles (or session IDs).")
    return name


def _fetch_session(cfg, tok, name) -> dict:
    for s in _call(cfg, tok, "GET", "/api/v1/sessions")["sessions"]:
        if s["session"] == name or s["id"] == name:
            return s
    raise CliError(EXIT_HTTP, "not_found", "No such session among yours.")


def open_session(a, allow_reconcile: bool = False) -> Ctx:
    """The session's stored state; on first use the recovery ID is fetched and stored. A stored
    recovery ID is never replaced here."""
    cfg, tok = _authed()
    name = _session_name(a, cfg)
    state = clistate.SessionState(name)
    info = state.load()
    if info is None:
        info = _session_record(_fetch_session(cfg, tok, name))
        state.save(info)
        if info["session"] != name:                       # named by ID: keep state under the handle
            state = clistate.SessionState(info["session"])
            state.save(info)
    if info.get("needs_reconcile") and not allow_reconcile:
        raise CliError(EXIT_RECOVERY, "needs_reconcile",
                       "The board was restored; this session's pending operations are quarantined.",
                       {"hint": "Stop and tell your human. Then: glassboard reconcile"})
    label, _ = runtime.resolve()
    return Ctx(cfg, tok, state, info, label)


def _on_recovery_mismatch(ctx: Ctx, old: str) -> CliError:
    moved = ctx.state.quarantine_recovery(old)
    ctx.state.discard_runtime_secrets()
    ctx.info.update(needs_reconcile=True, old_recovery_id=old)
    ctx.state.save(ctx.info)
    return CliError(EXIT_RECOVERY, "recovery_mismatch",
                    "The board was restored. Nothing more is sent under the old recovery ID.",
                    {"hint": "Stop and tell your human. Then: glassboard reconcile",
                     "quarantined": len(moved)})


def deliver(ctx: Ctx, entry: dict) -> dict:
    """Send one outbox entry with its own op ID and recovery ID. Removes it when the board has
    answered definitively (success, replay, or a non-retryable refusal)."""
    headers = {"Idempotency-Key": entry["op"], "X-Glassboard-Recovery": entry["recovery_id"]}
    try:
        _, raw = request(ctx.cfg, entry["method"], entry["path"], token=ctx.tok, body=entry.get("body"),
                         extra_headers=headers)
    except CliError as e:
        if e.kind == "recovery_mismatch":
            raise _on_recovery_mismatch(ctx, entry["recovery_id"]) from None
        if e.kind == "fence_mismatch" and entry.get("task") is not None:
            _set_fence(ctx, entry, None)                    # protocol rule 8: that fence is dead
        if not e.retryable:
            ctx.state.remove_outbox(entry["op"])          # refused for good: never resend it
        raise
    ctx.state.remove_outbox(entry["op"])
    result = json.loads(raw) if raw else {}
    effect = entry.get("fence_effect")
    if effect == "store" and result.get("fence"):
        _set_fence(ctx, entry, result["fence"])           # a claim/reclaim delivered by any later flush
    elif effect == "drop":
        _set_fence(ctx, entry, None)                      # submit/release ended the claim
    elif effect in ("wake_store", "wake_drop"):           # the wake supervisor's dispatch fence (E30)
        label = entry.get("runtime", ctx.label)
        rt = ctx.state.runtime(label)
        if effect == "wake_store" and result.get("fence"):
            rt["wake_fence"] = result["fence"]
        elif effect == "wake_drop":
            rt.pop("wake_fence", None)
        ctx.state.save_runtime(label, rt)
    return result


def _set_fence(ctx: "Ctx", entry: dict, fence: str | None) -> None:
    label = entry.get("runtime", ctx.label)
    rt = ctx.state.runtime(label)
    fences = rt.setdefault("fences", {})
    if fence is None:
        fences.pop(str(entry["task"]), None)
    else:
        fences[str(entry["task"])] = fence
    ctx.state.save_runtime(label, rt)


def flush(ctx: Ctx) -> int:
    """Send pending outbox entries in order (same op IDs, same recovery ID)."""
    sent = 0
    for entry in ctx.state.outbox():
        deliver(ctx, entry)
        sent += 1
    return sent


def send_op(ctx: Ctx, method: str, path: str, body, what: str, fenced: bool = False,
            task: int | None = None, fence_effect: str | None = None) -> dict:
    """Queue first (durable before anything is sent), then send the whole outbox in order, so an
    offline period queues every operation and a later command replays them in order."""
    entry = {"op": ids.new_ulid(), "method": method, "path": path, "body": body,
             "recovery_id": ctx.info["recovery_id"], "created": _now(), "what": what, "fenced": fenced,
             "task": task, "fence_effect": fence_effect, "runtime": ctx.label}
    ctx.state.add_outbox(entry)
    result = None
    try:
        for queued in ctx.state.outbox():
            r = deliver(ctx, queued)
            if queued["op"] == entry["op"]:
                result = r
    except CliError as e:
        if e.retryable and any(q["op"] == entry["op"] for q in ctx.state.outbox()):
            e.details["hint"] = "Queued in the outbox; the next glassboard command sends it (same operation ID)."
        raise
    return result


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_use(a) -> int:
    cfg, tok = _authed()
    name = _session_name(a, cfg)
    s = _fetch_session(cfg, tok, name)
    state = clistate.SessionState(s["session"])
    stored = state.load()
    if stored and stored["recovery_id"] != s["recovery_id"]:
        raise CliError(EXIT_RECOVERY, "recovery_mismatch", "The board was restored since you last used this session.",
                       {"hint": "Run: glassboard reconcile -s " + s["session"]})
    if not stored:
        state.save(_session_record(s))
    _, cfg_path, _ = _paths()
    cfg["session"] = s["session"]
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"using {r2(s['session'])} · {r2(s['role'])} · recovery {r2(s['recovery_id'])}")
    return EXIT_OK


def _parse_draft(text: str) -> dict:
    parts = [x.strip() for x in text.split("|")]
    if len(parts) not in (3, 4, 5) or not all(parts[:3]):
        raise CliError(EXIT_USAGE, "usage", '--draft "title|scope|deliverable[|assignee[|reviewer]]"')
    d = {"title": parts[0], "scope": parts[1], "deliverable": parts[2]}
    for i, field in ((3, "assignee"), (4, "reviewer")):        # B70: an empty field is left out
        if len(parts) > i and parts[i].lstrip("@"):
            d[field] = parts[i].lstrip("@")
    return d


def _task_refs(values) -> list[str] | None:
    """--depends-on T3 --depends-on T4,T5 → ["T3", "T4", "T5"]; "none" clears."""
    if values is None:
        return None
    out = [x.strip() for v in values for x in v.split(",") if x.strip()]
    return [] if out == ["none"] else out


def _division(a, body: dict) -> None:
    deps = _task_refs(a.depends_on)
    if deps is not None:
        body["depends_on"] = deps
    if a.parent is not None:
        body["parent"] = None if a.parent == "none" else a.parent
    if a.priority:
        body["priority"] = a.priority
    if a.reviewer is not None:
        body["reviewer"] = None if a.reviewer == "none" else a.reviewer


def _post_message(a) -> dict:
    standup = {k: v for k, v in (("done", a.done), ("doing", a.doing), ("blocked", a.blocked)) if v}
    body_sources = sum(value is not None for value in (a.body, a.body_option, a.body_file))
    if body_sources > 1:
        raise CliError(EXIT_USAGE, "usage", "Use only one of the message argument, --body, or --body-file.")
    if body_sources == 0 and not standup:
        raise CliError(EXIT_USAGE, "usage",
                       'usage: glassboard post KIND "<text>" (or --body "<text>")')
    if a.body_file is not None:
        if a.body_file == "-":
            body = sys.stdin.read()
        else:
            try:
                with open(a.body_file, encoding="utf-8") as stream:
                    body = stream.read()
            except OSError as e:
                raise CliError(EXIT_USAGE, "body_file_unreadable", "The message body file could not be read.") from e
    else:
        selected = a.body_option if a.body_option is not None else a.body
        body = sys.stdin.read() if selected == "-" else (selected or "Stand-up update.")
    msg = {"kind": a.kind, "body": body}
    if standup:
        msg["standup"] = standup
    if a.to:
        recipients = [h.strip() for part in a.to for h in part.split(",") if h.strip()]
        if recipients == ["all"]:
            recipients = []
        elif "all" in recipients:
            raise CliError(EXIT_USAGE, "usage", "Use --to all by itself, or list specific handles.")
        if recipients:
            msg["to"] = recipients
    if a.re is not None:
        msg["re"] = a.re
    if a.task:
        msg["task"] = a.task
    if getattr(a, "channel", None):
        msg["channel"] = a.channel.lstrip("#")
    if a.ref:
        msg["refs"] = [_parse_ref(x) for x in a.ref]
    if a.draft:
        msg["drafts"] = [_parse_draft(x) for x in a.draft]
    return msg


def cmd_preflight(a) -> int:
    msg = _post_message(a)
    ctx = open_session(a)
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/messages/preflight", msg, f"preflight {a.kind}")
    print(json.dumps(r, ensure_ascii=True, sort_keys=True))
    return EXIT_OK if r.get("status") == "ready" else EXIT_USAGE


def cmd_post(a) -> int:
    msg = _post_message(a)
    ctx = open_session(a)
    with ctx.state.locked():
        if a.check:
            pf = send_op(ctx, "POST", f"{ctx.path}/messages/preflight", msg, f"preflight {a.kind}")
            if pf.get("status") != "ready":
                print(json.dumps(pf, ensure_ascii=True, sort_keys=True))
                return EXIT_USAGE
            msg["preflight_seq"] = pf["seq"]
        elif a.preflight_seq is not None:
            msg["preflight_seq"] = a.preflight_seq
        r = send_op(ctx, "POST", f"{ctx.path}/messages", msg, f"post {a.kind}")
    encoded = msg["body"].encode("utf-8")
    where = r2(r['session']) + (f" #{r2(msg['channel'])}" if msg.get("channel") else "")
    print(f"posted #{r['seq']} [{r2(r['type'])}] in {where} · body {len(msg['body'])} chars "
          f"sha256 {hashlib.sha256(encoded).hexdigest()}")
    return EXIT_OK


def _parse_ref(spec: str) -> dict:
    """LABEL|LOC|ACCESS[|SHA256]"""
    parts = spec.split("|")
    if len(parts) not in (3, 4):
        raise CliError(EXIT_USAGE, "usage", "--ref takes LABEL|LOC|ACCESS[|SHA256]")
    ref = {"label": parts[0], "loc": parts[1], "access": parts[2]}
    if len(parts) == 4:
        ref["sha256"] = parts[3]
    return ref


def _parse_test_evidence(spec: str) -> dict:
    """OUTCOME|COMMAND[|DETAIL]"""
    parts = spec.split("|", 2)
    if len(parts) not in (2, 3) or parts[0] not in ("pass", "fail", "skipped") or not parts[1]:
        raise CliError(EXIT_USAGE, "usage", "--test takes OUTCOME|COMMAND[|DETAIL]")
    item = {"outcome": parts[0], "command": parts[1]}
    if len(parts) == 3 and parts[2]:
        item["detail"] = parts[2]
    return item


def cmd_question(a) -> int:
    body = {"resolution": a.resolution}
    if a.note:
        body["note"] = a.note
    ctx = open_session(a)
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/questions/{int(a.seq)}/resolve", body, f"resolve #{a.seq}")
    print(f"#{r['question']} resolved ({r2(r['resolution'])}, scope {r2(r['scope'])}) · #{r['seq']}")
    return EXIT_OK


def cmd_sync(a) -> int:
    ctx = open_session(a)
    with ctx.state.locked():
        flush(ctx)
        rt = ctx.state.runtime(ctx.label)
        body: dict = {"limit": a.limit}
        if getattr(a, "include_checkpoint", False):
            body["include_checkpoint"] = True                 # after a compaction: your checkpoint in full
        if a.wait:
            body["wait"] = max(0, min(25, a.wait))         # long-poll, at most 25 s
        headers = {"X-Glassboard-Recovery": ctx.info["recovery_id"]}
        minting = False
        if a.takeover is not None:
            minting = True
            body["takeover"] = {"reason": a.takeover}
        elif rt["consumer"]:
            body["consumer"] = rt["consumer"]
            if a.ack is not None:
                if a.ack == "stored":
                    if rt["delivered_through"] is None:
                        raise CliError(EXIT_USAGE, "nothing_to_ack", "Nothing was delivered to this runtime yet; sync first.")
                    body["ack_through"] = rt["delivered_through"]
                else:
                    body["ack_through"] = int(a.ack)
        else:
            minting = True
            if a.ack is not None:
                raise CliError(EXIT_USAGE, "not_attached", "This runtime has no inbox consumer yet; run `glassboard sync` first.")
        if minting:
            # Kept until the board answers, so a lost response is retried with the same key.
            rt["pending_attach"] = rt.get("pending_attach") or ids.new_ulid()
            ctx.state.save_runtime(ctx.label, rt)
            headers["Idempotency-Key"] = rt["pending_attach"]
        try:
            _, raw = request(ctx.cfg, "POST", f"{ctx.path}/sync", token=ctx.tok, body=body, extra_headers=headers)
        except CliError as e:
            if e.kind == "recovery_mismatch":
                raise _on_recovery_mismatch(ctx, ctx.info["recovery_id"]) from None
            if e.kind == "consumer_mismatch":
                rt.update(consumer=None, delivered_through=None)
                ctx.state.save_runtime(ctx.label, rt)
                e.details["hint"] = ("Another runtime took over this inbox. Do not take it back unless your human "
                                     "confirms the other one is gone: glassboard sync --takeover \"<reason>\"")
            if e.kind == "forbidden":
                e.details["hint"] = ("Read-only viewers do not own an inbox cursor. Use `glassboard digest`, "
                                     "`glassboard history`, and `glassboard artifact list` for passive inspection.")
            if minting and not e.retryable:
                rt["pending_attach"] = None
                ctx.state.save_runtime(ctx.label, rt)
            raise
        j = json.loads(raw)
        if minting:
            rt.update(consumer=j["consumer"], pending_attach=None)
        rt["delivered_through"] = j["delivered_through"]
        ctx.state.save_runtime(ctx.label, rt)
    if j.get("consumer"):
        # Like fences, the consumer secret lives only in the 0600 runtime file: an agent's
        # terminal output ends up in its transcript, so the CLI shows the non-secret prefix `C<gen>`.
        j = {**j, "consumer": str(j["consumer"]).split("-", 1)[0]}
    if a.json:
        _print_json(j)
    else:
        sys.stdout.write(textviews.sync_text(j, ctx.label) + "\n")
    return EXIT_OK


def cmd_watch(a) -> int:
    """Long-poll repeatedly for a runner. Delivery remains at-least-once until another command acks it."""
    print("warning: watch owns this identity's inbox consumer; do not run another watcher for the same identity.",
          file=sys.stderr)
    last = None
    while True:
        a.wait = a.wait or 20
        a.ack = None
        a.takeover = None
        a.json = True
        ctx = open_session(a)
        with ctx.state.locked():
            flush(ctx)
            rt = ctx.state.runtime(ctx.label)
            body = {"limit": a.limit, "wait": max(1, min(25, a.wait))}
            headers = {"X-Glassboard-Recovery": ctx.info["recovery_id"]}
            minting = not rt["consumer"]
            if minting:
                rt["pending_attach"] = rt.get("pending_attach") or ids.new_ulid()
                ctx.state.save_runtime(ctx.label, rt)
                headers["Idempotency-Key"] = rt["pending_attach"]
            else:
                body["consumer"] = rt["consumer"]
            try:
                _, raw = request(ctx.cfg, "POST", f"{ctx.path}/sync", token=ctx.tok, body=body,
                                 extra_headers=headers)
            except CliError as e:
                if e.kind == "recovery_mismatch":
                    raise _on_recovery_mismatch(ctx, ctx.info["recovery_id"]) from None
                raise
            packet = json.loads(raw)
            if minting:
                rt.update(consumer=packet["consumer"], pending_attach=None)
            rt["delivered_through"] = packet["delivered_through"]
            ctx.state.save_runtime(ctx.label, rt)
        marker = (packet["delivered_through"], tuple(e["seq"] for e in packet.get("inbox", [])))
        if marker != last or a.heartbeats:
            shown = {**packet, "consumer": str(packet["consumer"]).split("-", 1)[0]}
            _print_json(shown)
            sys.stdout.flush()
            last = marker
        time.sleep(max(0.1, a.interval))


def cmd_history(a) -> int:
    """Without --after: the latest --limit events (before --before, if given). With --after: oldest
    first from there. A partial window always ends with a line saying so and how to page (B72)."""
    ctx = open_session(a, allow_reconcile=True)
    q: dict = {"limit": a.limit}
    if a.after is None:
        q["latest"] = "true"
    for k in ("after", "before", "task", "thread", "actor", "type", "channel"):
        if getattr(a, k) is not None:
            q[k] = getattr(a, k)
    if a.to_me:
        q["to"] = "me"
    if not a.json:
        q["format"] = "text"
    path = f"{ctx.path}/events?{urllib.parse.urlencode(q)}"
    if a.json:
        j = _call(ctx.cfg, ctx.tok, "GET", path)
        _print_json(j)
        if j.get("truncated"):                       # JSON stays pure on stdout; the notice goes to stderr
            print(render.reword_partial(render.partial_line(j), "cli"), file=sys.stderr)
    else:
        text = r2(_call(ctx.cfg, ctx.tok, "GET", path, accept_text=True), allow_newline=True)
        sys.stdout.write(render.reword_partial(text, "cli"))
    return EXIT_OK


def cmd_outbox(a) -> int:
    ctx = open_session(a, allow_reconcile=True)
    out, q = ctx.state.outbox(), ctx.state.quarantine()
    print(f"outbox: {len(out)} pending · quarantine: {len(q)}")
    for label, entries in (("pending", out), ("quarantined", q)):
        for e in entries:
            print(f"  · {label}: {inline(e['op'] + ' ' + e['what'] + ' (created ' + e['created'] + ')')}")
    return EXIT_OK


def cmd_reconcile(a) -> int:
    """Show what was quarantined, the restore notice and the board's view; adopt the board's
    current recovery ID (an explicit, human-run step); resend or drop quarantined entries."""
    ctx = open_session(a, allow_reconcile=True)
    with ctx.state.locked():
        current = _fetch_session(ctx.cfg, ctx.tok, ctx.info["session"])
        if current["recovery_id"] != ctx.info["recovery_id"] or ctx.info.get("needs_reconcile"):
            old = ctx.info["recovery_id"]
            ctx.state.quarantine_recovery(old)
            if not ctx.info.get("needs_reconcile"):
                ctx.state.discard_runtime_secrets()
            ctx.info.update(recovery_id=current["recovery_id"], epoch=current["epoch"], needs_reconcile=False,
                            old_recovery_id=old if old != current["recovery_id"] else ctx.info.get("old_recovery_id"))
            ctx.state.save(ctx.info)
            print(f"adopted recovery {r2(current['recovery_id'])} (epoch {current['epoch']}); previous {r2(old)}")
        if a.drop:
            hit = ctx.state.remove_quarantine(a.drop)
            print(f"dropped {inline(a.drop)}" if hit else f"not in quarantine: {inline(a.drop)}")
            return EXIT_OK if hit else EXIT_USAGE
        if a.resend:
            entry = next((e for e in ctx.state.quarantine() if e["op"] == a.resend), None)
            if entry is None:
                raise CliError(EXIT_USAGE, "not_quarantined", "No such quarantined operation.")
            if entry.get("fenced"):
                raise CliError(EXIT_USAGE, "fence_dead", "This operation used a fence that the restore invalidated.",
                               {"hint": "Reclaim the task first: glassboard task reclaim T# \"<reason>\""})
            resend = {**entry, "recovery_id": ctx.info["recovery_id"]}       # original op ID, new recovery ID
            ctx.state.remove_quarantine(entry["op"])
            ctx.state.add_outbox(resend)
            r = deliver(ctx, resend)
            print(f"resent {inline(entry['op'])}: #{r.get('seq')}")
            return EXIT_OK
    restored = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/events?type=system.restored&limit=100")["events"]
    print(f"@{r2(current['session'])} · {r2(current['state'])} · epoch {current['epoch']} · recovery {r2(current['recovery_id'])}")
    for line in (textviews.view_lines(restored[-1]) if restored else []):
        print(line)
    q = ctx.state.quarantine()
    print(f"quarantine: {len(q)} operation(s)")
    for e in q:
        print(f"  · op: {inline(e['op'] + ' ' + e['what'] + ' (created ' + e['created'] + ', recovery ' + e['recovery_id'] + ')')}")
    if q:
        print("next: glassboard reconcile --resend OP  |  glassboard reconcile --drop OP")
    return EXIT_OK


def cmd_members(a) -> int:
    if a.action == "list":
        ctx = open_session(a, allow_reconcile=True)
        r = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/members")
        if a.json:
            _print_json(r)
            return EXIT_OK
        for m in r["members"]:
            cons = f" · consumer {r2(m['consumer'])}" if m["consumer"] else ""
            print(f"@{r2(m['handle'])} ({r2(m['kind'])}) · {r2(m['role'])} · joined #{m['joined_seq']}{cons}")
        return EXIT_OK
    if not a.handle:
        raise CliError(EXIT_USAGE, "usage", f"usage: glassboard members {a.action} SESSION HANDLE")
    ctx = open_session(a)
    with ctx.state.locked():
        if a.action == "add":
            r = send_op(ctx, "POST", f"{ctx.path}/members", {"handle": a.handle, "role": a.role}, f"members add {a.handle}")
            what = {"added": "added", "role_changed": "role changed", "none": "unchanged"}[r["change"]]
            print(f"@{r2(r['member'])} {what} · {r2(r['role'])}" + (f" · #{r['seq']}" if r["seq"] else ""))
        else:
            r = send_op(ctx, "DELETE", f"{ctx.path}/members/{_quote(a.handle)}", None, f"members remove {a.handle}")
            print(f"@{r2(r['member'])} removed · #{r['seq']}")
    return EXIT_OK


# --- tasks ------------------------------------------------------------------------------------

def _task_num(t: str) -> int:
    n = t[1:] if t[:1] in ("T", "t") else t
    if not n.isdigit() or int(n) < 1:
        raise CliError(EXIT_USAGE, "usage", "Tasks are named like 7 or T7.")
    return int(n)


def _fence_for(ctx: Ctx, num: int) -> str:
    fence = ctx.state.runtime(ctx.label).get("fences", {}).get(str(num))
    if not fence:
        raise CliError(EXIT_USAGE, "no_fence", f"This runtime holds no fence for T{num}.",
                       {"hint": f'If the task is still yours (restart, context reset): glassboard task reclaim T{num} "<reason>"'})
    return fence


def _revision_of(ctx: Ctx, num: int, tp: str, supersedes: int | None) -> int | None:
    """ERRATA E48 (B71): `task submit` on a task in review revises its current submission. Without a live
    fence for the task the CLI names the current submission itself; `--supersedes SEQ` names it exactly."""
    if supersedes is None and ctx.state.runtime(ctx.label).get("fences", {}).get(str(num)):
        return None                                      # the claim is held: an ordinary fenced submit
    current = _call(ctx.cfg, ctx.tok, "GET", tp)
    if current["status"] != "review":
        if supersedes is not None:
            raise CliError(EXIT_USAGE, "usage", f"T{num} is {r2(current['status'])}; --supersedes revises a task in review.")
        return None                                      # _fence_for explains the missing fence
    return supersedes if supersedes is not None else (current.get("submission") or {}).get("seq")


def cmd_task(a) -> int:
    act = a.action
    if act == "next":
        ctx = open_session(a, allow_reconcile=True)
        if a.json:
            _print_json(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks/next"))
        else:
            sys.stdout.write(r2(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks/next?format=text", accept_text=True),
                                allow_newline=True))
        return EXIT_OK
    if act in ("list", "show", "trail"):
        ctx = open_session(a, allow_reconcile=True)
        if act == "list":
            q = {k: v for k, v in (("status", a.status), ("mine", "true" if a.mine else None)) if v}
            r = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks?{urllib.parse.urlencode(q)}")
            if a.json:
                _print_json(r)
                return EXIT_OK
            sys.stdout.write(textviews.tasks_text(r))
            return EXIT_OK
        num = _task_num(a.task)
        if act == "trail":
            suffix = "" if a.json else "?format=text"
            result = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks/{num}/trail{suffix}", accept_text=not a.json)
            if a.json:
                _print_json(result)
            else:
                sys.stdout.write(r2(result, allow_newline=True))
            return EXIT_OK
        if a.json:
            _print_json(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks/{num}"))
        else:
            sys.stdout.write(r2(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/tasks/{num}?format=text", accept_text=True),
                                allow_newline=True))
        return EXIT_OK
    ctx = open_session(a)
    with ctx.state.locked():
        if act == "create":
            drafts = [{}]
            if a.from_proposal is not None:
                drafts = _drafts_of(ctx, a.from_proposal, a.draft)
            for d in drafts:
                body = {"title": a.task or d.get("title"), "scope": a.scope or d.get("scope"),
                        "deliverable": a.deliverable or d.get("deliverable")}
                if not all(body.values()):
                    raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task create "<title>" --scope … --deliverable … '
                                                        "(or --from-proposal SEQ --draft N|all)")
                if a.assignee or d.get("assignee"):
                    body["assignee"] = a.assignee or d.get("assignee")
                if d.get("reviewer"):                    # B70: the draft's reviewer; --reviewer overrides
                    body["reviewer"] = d["reviewer"]
                if a.stale_after is not None:
                    body["stale_after_s"] = a.stale_after
                if a.check:
                    body["checklist"] = a.check
                if a.from_proposal is not None:
                    body["from_proposal"] = a.from_proposal
                _division(a, body)
                r = send_op(ctx, "POST", f"{ctx.path}/tasks", body, "task create")
                print(f"T{r['task']} created · open · #{r['seq']}")
            return EXIT_OK
        num = _task_num(a.task)
        tp = f"{ctx.path}/tasks/{num}"
        text = a.text
        revised = None
        if act == "edit":
            current = _call(ctx.cfg, ctx.tok, "GET", tp)
            body = {"if_version": current["version"]}
            for f, v in (("title", a.title), ("scope", a.scope), ("deliverable", a.deliverable), ("stale_after_s", a.stale_after),
                         ("checklist", a.check)):
                if v is not None:
                    body[f] = v
            _division(a, body)
            r = send_op(ctx, "PATCH", tp, body, f"task edit T{num}")
        elif act == "claim":
            r = send_op(ctx, "POST", f"{tp}/claim", {}, f"task claim T{num}", task=num, fence_effect="store")
        elif act == "acknowledge":
            r = send_op(ctx, "POST", f"{tp}/acknowledge", {}, f"task acknowledge T{num}")
        elif act == "reclaim":
            if not text:
                raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task reclaim T# "<reason>"')
            r = send_op(ctx, "POST", f"{tp}/reclaim", {"reason": text}, f"task reclaim T{num}", task=num, fence_effect="store")
        elif act == "review":
            if text not in ("approve", "changes"):
                raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task review T# approve|changes --submission SEQ [--commit SHA] [--notes "…"]')
            if a.submission is None:
                raise CliError(EXIT_USAGE, "usage", "task review requires --submission SEQ from the current task.submitted event")
            body = {"verdict": text, "submission_seq": a.submission}
            if a.commit:
                body["commit"] = a.commit
            if a.notes:
                body["notes"] = a.notes
            r = send_op(ctx, "POST", f"{tp}/review", body, f"task review T{num}")
        elif act == "check":
            current = _call(ctx.cfg, ctx.tok, "GET", tp)
            items = current.get("checklist") or []
            if not text:
                raise CliError(EXIT_USAGE, "usage", "usage: glassboard task check T# <item number or exact label> [--undo]")
            if text.isdigit():
                item_num = int(text)
            else:
                matches = [item for item in items if item["text"] == text]
                if len(matches) != 1:
                    choices = "; ".join(f"{item['id']}. {r2(item['text'])}" for item in items) or "none"
                    raise CliError(EXIT_USAGE, "checklist_item_unknown", "Use an item number or one exact checklist label.",
                                   {"valid_items": choices})
                item_num = matches[0]["id"]
            body = {"fence": _fence_for(ctx, num), "item": item_num, "done": not a.undo}
            r = send_op(ctx, "POST", f"{tp}/check", body, f"task check T{num}", fenced=True, task=num)
        elif act in ("note", "block", "unblock", "submit", "release") and not (act == "release" and a.as_coordinator):
            if act in ("note", "block", "submit") and not text:
                raise CliError(EXIT_USAGE, "usage", f'usage: glassboard task {act} T# "<text>"')
            supersedes = _revision_of(ctx, num, tp, a.supersedes) if act == "submit" else None
            body = {"supersedes": supersedes} if supersedes else {"fence": _fence_for(ctx, num)}
            key = {"note": "note", "block": "reason", "unblock": "reason", "submit": "summary", "release": "handoff"}[act]
            if text:
                body[key] = text
            if act == "submit" and a.ref:
                body["refs"] = [_parse_ref(x) for x in a.ref]
            if act == "submit" and a.commit:
                body["commit"] = a.commit
            if act == "submit" and a.test:
                body["tests"] = [_parse_test_evidence(x) for x in a.test]
            if act == "submit" and a.allow_incomplete_checklist:
                body["incomplete_checklist_reason"] = a.allow_incomplete_checklist
            if act == "note" and a.effort:
                body["effort_min"] = a.effort
            handoff = {k: v for k, v in (("done", a.done), ("next", a.next_step), ("gotchas", a.gotchas)) if v}
            if handoff and act in ("submit", "release"):
                body["handoff_fields"] = handoff
            if act == "block" and a.waiting_on:
                body["waiting_on"] = a.waiting_on
            r = send_op(ctx, "POST", f"{tp}/{act}", body, f"task {act} T{num}", fenced=not supersedes, task=num,
                        fence_effect="drop" if act in ("submit", "release") and not supersedes else None)
            revised = supersedes
        elif act == "decline":                               # B41: give the task back with a reason
            if not text:
                raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task decline T# "<reason>"')
            fence = ctx.state.runtime(ctx.label).get("fences", {}).get(str(num))
            body = {"reason": text, **({"fence": fence} if fence else {})}
            r = send_op(ctx, "POST", f"{tp}/decline", body, f"task decline T{num}", fenced=bool(fence), task=num,
                        fence_effect="drop" if fence else None)
        elif act == "release":                               # --as-coordinator: no fence
            r = send_op(ctx, "POST", f"{tp}/release", {"handoff": text} if text else {}, f"task release T{num}")
        elif act in ("accept", "return", "reassign", "cancel", "reopen"):
            current = _call(ctx.cfg, ctx.tok, "GET", tp)
            body = {"if_version": current["version"]}
            if act == "return":
                if not text:
                    raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task return T# "<feedback>"')
                body["feedback"] = text
                if not current.get("submission"):
                    raise CliError(EXIT_USAGE, "submission_required", "This task has no current submission to return.")
                body["submission_seq"] = current["submission"]["seq"]
            elif act == "reassign":
                if not a.to:
                    raise CliError(EXIT_USAGE, "usage", 'usage: glassboard task reassign T# --to HANDLE ["reason"]')
                body["to"] = a.to
                if text:
                    body["reason"] = text
            elif text:
                body["reason"] = text
            r = send_op(ctx, "POST", f"{tp}/{act}", body, f"task {act} T{num}")
        else:
            raise CliError(EXIT_USAGE, "usage", f"unknown task action {act}")
    fence = r.get("fence")
    shown = f" · fence T{r['task']} g{r['gen']} kept for this runtime" if fence else ""
    shown += f" · revision: supersedes #{revised}" if revised else ""
    print(f"T{r['task']} {r2(r['status'])} g{r['gen']} v{r['version']}" + (f" · #{r['seq']}" if r.get("seq") else "") + shown)
    return EXIT_OK


# --- checkpoints ------------------------------------------------------------------------------

def _drafts_of(ctx, seq: int, which: str | None) -> list[dict]:
    """The task drafts of proposal #seq: one (--draft N) or all (--draft all). Without --draft, the task
    is only linked to the proposal and its fields come from the command line."""
    if which is None:
        return [{}]
    evs = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/events?after={seq - 1}&limit=1")["events"]
    drafts = (evs[0].get("data") or {}).get("drafts") if evs and evs[0]["seq"] == seq and evs[0]["type"] == "proposal" else None
    if not drafts:
        raise CliError(EXIT_USAGE, "no_drafts", "That event is not a proposal with task drafts.")
    if which == "all":
        return drafts
    if not which.isdigit() or not 1 <= int(which) <= len(drafts):
        raise CliError(EXIT_USAGE, "usage", f"--draft is 1..{len(drafts)} or all")
    return [drafts[int(which) - 1]]


def _session_view(a, path: str, query: dict | None = None) -> int:
    ctx = open_session(a, allow_reconcile=True)
    q = {k: v for k, v in (query or {}).items() if v is not None}
    sep = "&" if q else ""
    qs = urllib.parse.urlencode(q)
    if a.json:
        _print_json(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/{path}" + (f"?{qs}" if qs else "")))
    else:
        sys.stdout.write(r2(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/{path}?{qs}{sep}format=text", accept_text=True),
                            allow_newline=True))
    return EXIT_OK


def _report_exit(label: str, code: int) -> None:
    """B37: the client of runtime `label` exited. Report it to every session where this runtime kept
    local state (an inbox consumer or task fences); the board records it only if work is still held."""
    root = clistate.state_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    try:
        cfg, tok = _authed()
    except CliError:
        return
    body = {"signal": -code} if code < 0 else {"exit_code": code}
    for name in names:
        try:
            state = clistate.SessionState(name)
        except ValueError:
            continue
        info, rt = state.load(), state.runtime(label)
        if not info or not (rt.get("consumer") or rt.get("fences")):
            continue
        headers = {"X-Glassboard-Recovery": info["recovery_id"], "X-Glassboard-Runtime": label,
                   "Idempotency-Key": ids.new_ulid()}
        try:
            r = _call(cfg, tok, "POST", f"/api/v1/sessions/{_quote(name)}/runtime/exited", body=body, headers=headers)
        except CliError as e:
            print(f"glassboard: {name}: exit not reported ({e.kind})", file=sys.stderr)
            continue
        if r.get("seq"):
            print(f"glassboard: {name}: reported that runtime {label} exited while holding work (#{r['seq']})",
                  file=sys.stderr)


def _board_compact_at(cfg) -> str | int | None:
    """The board's compaction setting from discovery; None when unset or unreachable."""
    try:
        _, raw = request(cfg, "GET", "/api/v1/discovery", timeout=5)
        return (json.loads(raw).get("compaction") or {}).get("at_percent")
    except Exception:
        return None


def _with_compaction(cmd: list[str], run_flag) -> list[str]:
    """B40: set the client's auto-compaction threshold (run flag, agent, board, default). B32: also tell the
    channel adapter whether this client may act unattended (it refuses to push then)."""
    if hooks.client_of(cmd) is None:
        return cmd
    if hooks.client_of(cmd) == "claude":
        from . import channel
        os.environ[channel.UNATTENDED_ENV] = "1" if channel.unattended(cmd) else "0"
    try:
        cfg = load_config()
    except (CliError, OSError, ValueError):
        cfg = {}
    try:
        board = _board_compact_at(cfg) if cfg.get("url") and run_flag is None and cfg.get("compact_at") is None else None
        value, source = hooks.resolve(run_flag, cfg.get("compact_at"), board)
    except ValueError as e:
        raise CliError(EXIT_USAGE, "usage", str(e))
    cmd, note = hooks.apply(cmd, value, os.environ)
    if note:
        print(f"glassboard: {note}; set by {source} (`glassboard compaction` to change)", file=sys.stderr)
    return cmd


def cmd_compaction(a) -> int:
    """B40: this agent's auto-compaction threshold for clients started with `glassboard run`."""
    cfg = load_config()
    if a.action == "set":
        if a.value is None:
            raise CliError(EXIT_USAGE, "usage", "usage: glassboard compaction set PERCENT|off|unset")
        if a.value == "unset":
            cfg.pop("compact_at", None)
        else:
            try:
                v = hooks.parse_compact_at(a.value)
            except ValueError as e:
                raise CliError(EXIT_USAGE, "usage", str(e))
            cfg["compact_at"] = v
        _, cfg_path, _ = _paths()
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
    board = _board_compact_at(cfg) if cfg.get("url") else None
    value, source = hooks.resolve(None, cfg.get("compact_at"), board)
    shown = "off (the client keeps its own threshold)" if value == "off" else f"{value}%"
    if a.json:
        _print_json({"compact_at": value, "source": source, "agent": cfg.get("compact_at"), "board": board,
                     "default": hooks.DEFAULT_COMPACT_AT})
    else:
        print(f"compaction: {shown} (from {source}; `glassboard run --compact-at N|off` overrides one run)")
    return EXIT_OK


DELIVER_CHARS = 6000                   # B53: bound on what one hook adds to the agent's context
UNATTENDED_MODES = ("bypassPermissions", "auto")   # B32 F3: never keep a turn going where nobody approves


def _deliverable(ctx) -> tuple[str, int] | None:
    """B53: new inbox events for this runtime as bounded canonical text, and the seq to acknowledge after
    printing. None when there is nothing, the runtime holds no inbox consumer (a hook never attaches or
    takes over), or the agent is in quiet hours (B56: delivery waits like a wake)."""
    rt = ctx.state.runtime(ctx.label)
    if not rt.get("consumer"):
        return None
    w = _call_quick(ctx, "GET", f"{ctx.path}/wake")
    q = (w or {}).get("quiet")
    if q and q.get("active") and q.get("blocks_dispatch", True):
        return None
    _, raw = request(ctx.cfg, "POST", f"{ctx.path}/sync", token=ctx.tok, body={"consumer": rt["consumer"], "limit": 50},
                     extra_headers={"X-Glassboard-Recovery": ctx.info["recovery_id"]}, timeout=8)
    j = json.loads(raw)
    rt["delivered_through"] = j["delivered_through"]
    ctx.state.save_runtime(ctx.label, rt)
    if not j["inbox"]:
        return None
    head = [render.section(f"note: glassboard delivered {{n}} new event(s) in session {j['session']} while you worked "
                           f"(the same text `glassboard sync` shows). They are acknowledged now; reply on the board "
                           f"if one needs you."), render.NOTE_LINE]
    body, last, n = [], None, 0
    for ev in j["inbox"]:
        lines = textviews.view_lines(ev)
        if body and sum(len(x) + 1 for x in head + body + lines) > DELIVER_CHARS - 200:
            break
        body.extend(lines)
        last, n = ev["seq"], n + 1
    if n < len(j["inbox"]) or j["more"]:
        body.append(render.section("note: more events are waiting: run `glassboard sync`."))
    head[0] = head[0].replace("{n}", str(n))
    text = "\n".join(head + body)
    return text[:DELIVER_CHARS], last


def _call_quick(ctx, method: str, path: str):
    try:
        _, raw = request(ctx.cfg, method, path, token=ctx.tok, timeout=5)
        return json.loads(raw) if raw else None
    except Exception:                       # noqa: BLE001 - the caller treats "unknown" as "no limit known"
        return None


def _hook_deliver(ctx, a, payload: dict, stop: bool) -> None:
    """PostToolUse (stop=False): add new events as context. Stop (stop=True, opt-in): keep the turn going
    once while events wait, never under an unattended permission mode and never twice in a row."""
    if stop and (payload.get("stop_hook_active") or payload.get("permission_mode") in UNATTENDED_MODES):
        return
    with ctx.state.locked():
        got = _deliverable(ctx)
        if got is None:
            return
        text, last = got
        if stop:
            out = {"decision": "block", "reason": text}
        else:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()                  # printed first; acknowledged only after (at-least-once)
        rt = ctx.state.runtime(ctx.label)
        request(ctx.cfg, "POST", f"{ctx.path}/sync", token=ctx.tok, body={"consumer": rt["consumer"], "ack_through": last,
                                                                          "limit": 1},
                extra_headers={"X-Glassboard-Recovery": ctx.info["recovery_id"]}, timeout=8)


def cmd_hook(a) -> int:
    """B40: called by a client hook. Prints only what the hook protocol expects, and nothing at all
    (exit 0) when anything fails, so a hook never breaks the client."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    try:
        ctx = open_session(a)
        if a.action in ("deliver", "stop"):
            _hook_deliver(ctx, a, payload, a.action == "stop")
        elif a.action == "session-start":
            _, text = request(ctx.cfg, "GET", f"{ctx.path}/brief?format=text", token=ctx.tok, accept_text=True,
                              timeout=8)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                             ensure_ascii=True))
        else:
            trigger = payload.get("trigger") if payload.get("trigger") in ("auto", "manual") else "unknown"
            request(ctx.cfg, "POST", f"{ctx.path}/context/compacted", token=ctx.tok,
                    body={"trigger": trigger, "client": a.client},
                    extra_headers={"Idempotency-Key": ids.new_ulid(), "X-Glassboard-Recovery": ctx.info["recovery_id"]},
                    timeout=8)
    except Exception:                       # noqa: BLE001 - a hook must never fail the client
        pass
    return EXIT_OK


def cmd_hooks(a) -> int:
    """B40: show or install the opt-in client hooks (resume brief after compaction; compaction events)."""
    targets = list(hooks.CLIENTS) if a.client == "all" else [a.client]
    if a.action == "show":
        for t in targets:
            print(f"# {t}: add to {hooks.settings_path(t, a.project, a.home)}")
            print(json.dumps({"hooks": hooks.recipe(t, a.deliver, a.hold)}, indent=2))
        return EXIT_OK
    for t in targets:
        try:
            path, changed, backup = hooks.install(t, a.project, a.home, a.deliver, a.hold)
        except (ValueError, OSError) as e:
            raise CliError(EXIT_CONFIG, "hooks_not_installed", f"{t}: {e}")
        if changed:
            print(f"installed the glassboard hooks for {t}: {path}" + (f" (previous file kept as {backup})" if backup else ""))
        else:
            print(f"the glassboard hooks for {t} are already in {path}")
        if t == "codex":
            print("codex: open /hooks in Codex once to review and trust them; Codex skips new hooks until trusted")
    return EXIT_OK


def cmd_export(a) -> int:
    """The whole session: canonical text (md, default) or one JSON event per line (jsonl)."""
    ctx = open_session(a, allow_reconcile=True)
    raw = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/export?format={a.format}", accept_text=True)
    if a.out:
        with open(a.out, "x", encoding="utf-8") as fh:
            fh.write(raw)
        print(f"exported {ctx.path.rsplit('/', 1)[-1]} to {a.out}")
    else:
        sys.stdout.write(r2(raw, allow_newline=True) if a.format == "md" else raw)
    return EXIT_OK


def cmd_invite(a) -> int:
    """Humans (Session Managers): a one-time join code for one of your agents."""
    ctx = open_session(a)
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/invites", {"agent": a.agent, "role": a.role}, f"invite {a.agent}")
    if a.json:
        _print_json(r)
        return EXIT_OK
    print(f"@{r2(r['agent'])} invited to {r2(r['session'])} · expires {r2(r['expires_at'])}")
    if r.get("code"):
        print(f"one-time code (shown once): {r2(r['code'])}")
        print(f"on the agent's machine: glassboard join {r2(ctx.cfg['url'])} {r2(r['code'])}")
    return EXIT_OK


def cmd_reference(a) -> int:
    """The agent reference: every capability with its API route, CLI command and dashboard page."""
    from importlib import resources
    sys.stdout.write(resources.files("glassboard").joinpath("agent_reference.md").read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_approval(a) -> int:
    ctx = open_session(a)
    body = {"decision": a.decision}
    if a.note:
        body["note"] = a.note
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/approvals/{a.seq}", body, f"approval {a.decision} #{a.seq}")
    print(f"approval #{a.seq} {'granted' if a.decision == 'grant' else 'denied'} · #{r['seq']}")
    return EXIT_OK


def _minutes(v: str) -> int:
    """30, 30m or 2h → minutes."""
    v = v.strip().lower()
    mult = 60 if v.endswith("h") else 1
    v = v.rstrip("hm")
    if not v.isdigit():
        raise argparse.ArgumentTypeError("a number of minutes, like 30, 30m or 2h")
    return int(v) * mult


def cmd_hold(a) -> int:
    """B41: raise an objection that never blocks (the deciders owe an answer before the deadline), or decide one."""
    ctx = open_session(a)
    if a.action == "raise":
        if not a.arg or not a.default or a.deadline is None:
            raise CliError(EXIT_USAGE, "usage", 'usage: glassboard hold raise "<what should wait, and why>" '
                                                '--default "<what happens if nobody decides>" --deadline 30m [--task T#]')
        body = {"body": a.arg, "default": a.default, "deadline_min": a.deadline}
        if a.task:
            body["task"] = _task_num(a.task)
        with ctx.state.locked():
            r = send_op(ctx, "POST", f"{ctx.path}/holds", body, "hold raise")
        print(f"hold #{r['seq']} raised · decide by {r2(r['deadline_at'])[:16]}Z, otherwise the stated default applies")
        return EXIT_OK
    if not (a.arg or "").lstrip("#").isdigit() or a.decision is None:
        raise CliError(EXIT_USAGE, "usage", 'usage: glassboard hold decide SEQ uphold|proceed [--note "…"]')
    seq = int(a.arg.lstrip("#"))
    body = {"decision": a.decision, **({"note": a.note} if a.note else {})}
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/holds/{seq}", body, f"hold decide #{seq}")
    print(f"hold #{seq} {'upheld' if a.decision == 'uphold' else 'overruled: proceed'} · #{r['seq']}")
    return EXIT_OK


def cmd_standdown(a) -> int:
    """B41: a human asks an agent to stop and hand off; the agent answers; a human lifts it."""
    ctx = open_session(a)
    if a.action == "request":
        if not a.arg or not a.text:
            raise CliError(EXIT_USAGE, "usage", 'usage: glassboard standdown request AGENT "<reason>"')
        with ctx.state.locked():
            r = send_op(ctx, "POST", f"{ctx.path}/standdowns", {"member": a.arg, "reason": a.text}, "standdown request")
        print(f"stand-down #{r['seq']} requested for @{r2(r['member'])}")
        return EXIT_OK
    if a.action == "lift":
        if not (a.arg or "").lstrip("#").isdigit():
            raise CliError(EXIT_USAGE, "usage", 'usage: glassboard standdown lift SEQ ["<note>"]')
        seq = int(a.arg.lstrip("#"))
        with ctx.state.locked():
            r = send_op(ctx, "POST", f"{ctx.path}/standdowns/{seq}/lift", {"note": a.text} if a.text else {},
                        f"standdown lift #{seq}")
        print(f"stand-down #{seq} lifted · #{r['seq']}")
        return EXIT_OK
    # done: release what this runtime holds, then answer. A checkpoint must already be saved.
    with ctx.state.locked():
        b = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/brief")
        sd = b.get("standdown")
        if not sd:
            raise CliError(EXIT_USAGE, "no_standdown", "Nobody asked you to stand down in this session.")
        if sd.get("done_seq"):
            print(f"stand-down #{sd['seq']} already answered at #{sd['done_seq']}; a human lifts it")
            return EXIT_OK
        handoff = a.text or f"Stand-down requested in #{sd['seq']}."
        fences = ctx.state.runtime(ctx.label).get("fences", {})
        for x in b.get("claims", []):
            if x["status"] not in ("in_progress", "blocked"):
                continue
            fence = fences.get(str(x["task"]))
            if not fence:
                raise CliError(EXIT_USAGE, "no_fence", f"This runtime holds no fence for T{x['task']}.",
                               {"hint": f'Reclaim it first (glassboard task reclaim T{x["task"]} "<reason>"), then retry.'})
            send_op(ctx, "POST", f"{ctx.path}/tasks/{x['task']}/release", {"fence": fence, "handoff": handoff},
                    f"task release T{x['task']}", fenced=True, task=x["task"], fence_effect="drop")
            print(f"T{x['task']} released")
        for lease in _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/leases").get("leases", []):
            if lease["holder"] == b["member"]:
                send_op(ctx, "POST", f"{ctx.path}/leases/release", {"resource": lease["resource"]}, "lease release")
                print(f"lease {r2(lease['resource'])} released")
        r = send_op(ctx, "POST", f"{ctx.path}/standdowns/{sd['seq']}/done", {"note": a.text} if a.text else {},
                    f"standdown done #{sd['seq']}")
    print(f"stand-down #{sd['seq']} answered · #{r['seq']} · a human lifts it when you may work again")
    return EXIT_OK


def _local_tz() -> str | None:
    """This machine's IANA time zone name, when it can be told (TZ, then /etc/localtime)."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz or tz == "UTC":
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "/zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else None


def cmd_quiet(a) -> int:
    """B56: quiet hours. Humans set them for themselves or for an agent they own or coordinate."""
    if a.action == "show":
        return _session_view(a, "status")
    ctx = open_session(a)
    me = _call(ctx.cfg, ctx.tok, "GET", "/api/v1/whoami")["handle"]
    body: dict = {"member": a.member or me}
    if a.action == "set":
        tz = a.tz or _local_tz()
        if not (a.start and a.end and tz):
            raise CliError(EXIT_USAGE, "usage", "usage: glassboard quiet set --from 22:00 --to 07:00 [--tz Europe/Madrid] "
                                                "[--for AGENT] [--urgent]")
        body.update(start=a.start, end=a.end, tz=tz, urgent=bool(a.urgent))
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/quiet-hours", body, "quiet " + a.action)
    print(f"@{r2(r['member'])} quiet hours: {r2(r['quiet'])}" + (" · human-caused wakes go through" if r["urgent"] else "")
          + f" · #{r['seq']}")
    return EXIT_OK


def cmd_lease(a) -> int:
    if a.action == "list":
        return _session_view(a, "leases")
    if not a.resource:
        raise CliError(EXIT_USAGE, "usage", f"usage: glassboard lease {a.action} RESOURCE")
    ctx = open_session(a)
    if a.action in ("take", "run"):
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if (a.action == "run") != bool(cmd):
            raise CliError(EXIT_USAGE, "usage", "usage: glassboard lease run RESOURCE [--wait S] -- COMMAND…")
        _take_lease(ctx, a.resource, a.note, a.wait)
        if a.action == "take":
            return EXIT_OK
        try:
            return subprocess.call(cmd)
        finally:
            _release_lease(ctx, a.resource)
    _release_lease(ctx, a.resource)
    return EXIT_OK


def _take_lease(ctx, resource: str, note: str | None, wait: int) -> None:
    """Take the lease, or with --wait queue behind its holder: retry until it is released (or expires)."""
    import time as _time
    deadline, told = _time.monotonic() + max(0, wait), False
    body = {"resource": resource, **({"note": note} if note else {})}
    while True:
        try:
            with ctx.state.locked():
                r = send_op(ctx, "POST", f"{ctx.path}/leases", body, "lease take")
            print(f"lease taken · #{r['seq']}")
            return
        except CliError as e:
            if e.kind != "resource_held":
                raise
            left = deadline - _time.monotonic()
            if left <= 0:
                raise
            if not told:
                holder = (e.details.get("details") or {}).get("holder", "someone")
                print(f"waiting for {r2(str(holder))} to release it (up to {wait}s)…", file=sys.stderr)
                told = True
            _time.sleep(min(2.0, left))


def _release_lease(ctx, resource: str) -> None:
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/leases/release", {"resource": resource}, "lease release")
    print(f"lease released · #{r['seq']}")


def cmd_board(a) -> int:
    ctx = open_session(a, allow_reconcile=True)
    if a.json:
        _print_json(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/board"))
    else:
        sys.stdout.write(r2(_call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/board?format=text", accept_text=True), allow_newline=True))
    return EXIT_OK


def cmd_artifact(a) -> int:
    ctx = open_session(a, allow_reconcile=a.action in ("list", "get"))
    base = f"{ctx.path}/artifacts"
    if a.action == "list":
        query = urllib.parse.urlencode({"task": a.task}) if a.task else ""
        result = _call(ctx.cfg, ctx.tok, "GET", base + ("?" + query if query else ""))
        if a.json:
            _print_json(result)
        elif not result["artifacts"]:
            print("no artifacts")
        else:
            for item in result["artifacts"]:
                task = f" · T{item['task']}" if item["task"] else ""
                purpose = " · deliverable" if item.get("purpose") == "deliverable" else ""
                print(f"{r2(item['id'])} · {r2(item['name'])} · {item['size']} bytes · {r2(item['sha256'])}{task}{purpose}")
        return EXIT_OK
    if a.action == "upload":
        path = os.path.abspath(a.value)
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                raise OSError("not a regular file")
            if st.st_size > 16 * 1024 * 1024:
                raise CliError(EXIT_USAGE, "payload_too_large", "Artifact exceeds the 16 MiB limit.")
            with open(path, "rb") as f:
                content = f.read()
        except CliError:
            raise
        except OSError as e:
            raise CliError(EXIT_USAGE, "file_error", f"Cannot read artifact: {e}") from None
        query = {"name": a.name or os.path.basename(path), "media_type": a.media_type,
                 "purpose": "deliverable" if a.deliverable else "evidence",
                 **({"description": a.description} if a.description else {}), **({"task": a.task} if a.task else {})}
        headers = {"Idempotency-Key": ids.new_ulid(), "X-Glassboard-Recovery": ctx.info["recovery_id"],
                   "Content-Type": "application/octet-stream"}
        raw = _call_bytes(ctx.cfg, ctx.tok, "POST", base + "?" + urllib.parse.urlencode(query),
                          raw_body=content, headers=headers)
        result = json.loads(raw)
        if a.json:
            _print_json(result)
        else:
            print(f"artifact {r2(result['artifact'])} uploaded · {result['size']} bytes · sha256 {r2(result['sha256'])} · #{result['seq']}")
        return EXIT_OK
    if not a.out:
        raise CliError(EXIT_USAGE, "usage", "artifact get requires --out PATH")
    meta = _call(ctx.cfg, ctx.tok, "GET", f"{base}/{_quote(a.value)}")
    content = _call_bytes(ctx.cfg, ctx.tok, "GET", f"{base}/{_quote(a.value)}/content")
    import hashlib
    if len(content) != meta["size"] or hashlib.sha256(content).hexdigest() != meta["sha256"]:
        raise CliError(EXIT_HTTP, "artifact_mismatch", "Downloaded artifact does not match its recorded size and SHA-256.")
    out = os.path.abspath(a.out)
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
    except OSError as e:
        raise CliError(EXIT_USAGE, "file_error", f"Cannot create output file: {e}") from None
    print(f"saved {r2(out)} · {len(content)} bytes · sha256 {r2(meta['sha256'])}")
    return EXIT_OK


def _me_view(name: str, a) -> int:
    cfg, tok = _authed()
    if a.json:
        _print_json(_call(cfg, tok, "GET", f"/api/v1/me/{name}"))
    else:
        sys.stdout.write(r2(_call(cfg, tok, "GET", f"/api/v1/me/{name}?format=text", accept_text=True), allow_newline=True))
    return EXIT_OK


def cmd_claude_channel(a) -> int:
    from . import channel
    return channel.main(a, sys.modules[__name__])


def cmd_topic_channel(a) -> int:
    """B55: topic channels. list for anyone; create/change/archive for humans and coordinators; mode for members."""
    if a.action == "list":
        return _session_view(a, "channels")
    if not a.name:
        raise CliError(EXIT_USAGE, "usage", f"usage: glassboard channel {a.action} NAME …")
    ctx = open_session(a)
    path = f"{ctx.path}/channels/{_quote(a.name)}"
    if a.action == "create":
        body = {"name": a.name, "members": a.member or [], **({"topic": a.topic} if a.topic else {})}
        if a.post_only:
            body.update(post_policy="listed", posters=a.poster or [])
        with ctx.state.locked():
            r = send_op(ctx, "POST", f"{ctx.path}/channels", body, f"channel create {a.name}")
        print(f"#{r2(r['channel'])} created with {r['members']} member(s) · #{r['seq']}")
        return EXIT_OK
    if a.action == "mode":
        if len(a.values) != 1 or a.values[0] not in ("follow", "mentions", "mute"):
            raise CliError(EXIT_USAGE, "usage", "usage: glassboard channel mode NAME follow|mentions|mute")
        with ctx.state.locked():
            r = send_op(ctx, "POST", f"{path}/mode", {"mode": a.values[0]}, f"channel mode {a.name}")
        print(f"#{r2(r['channel'])}: you now {r2(r['mode'])} · #{r['seq']}")
        return EXIT_OK
    body: dict = {}
    if a.action == "topic":
        if not a.values:
            raise CliError(EXIT_USAGE, "usage", 'usage: glassboard channel topic NAME "text"')
        body["topic"] = " ".join(a.values)
    elif a.action in ("add", "remove"):
        if not a.values:
            raise CliError(EXIT_USAGE, "usage", f"usage: glassboard channel {a.action} NAME HANDLE…")
        body[a.action] = a.values
    elif a.action == "posters":
        body.update(post_policy="listed" if a.values else "all", posters=a.values)
    elif a.action == "archive":
        body["archive"] = True
    with ctx.state.locked():
        r = send_op(ctx, "POST", path, body, f"channel {a.action} {a.name}")
    what = "archived" if r.get("archived") else ("changed: " + ", ".join(r2(x) for x in r.get("changes", [])))
    print(f"#{r2(r['channel'])} {what} · #{r['seq']}")
    return EXIT_OK


def cmd_team(a) -> int:
    from . import team
    return team.main(a, sys.modules[__name__])


def cmd_wake(a) -> int:
    from . import wakerun
    return wakerun.main(a, sys.modules[__name__])


def cmd_checkpoint(a) -> int:
    if a.action == "show":
        ctx = open_session(a, allow_reconcile=True)
        q = {"principal": a.principal or "me", "n": a.n or "latest"}
        r = _call(ctx.cfg, ctx.tok, "GET", f"{ctx.path}/checkpoints?{urllib.parse.urlencode(q)}")
        if a.json:
            _print_json(r)
            return EXIT_OK
        sys.stdout.write(textviews.checkpoint_text(r["checkpoint"]))
        return EXIT_OK
    if a.text and not a.summary:
        a.summary = a.text
    if not (a.summary and a.next):
        raise CliError(EXIT_USAGE, "usage", 'usage: glassboard checkpoint save "SUMMARY" --next "NEXT STEP" '
                                            '(or --summary "..." --next "...")')
    body = {"summary": a.summary, "next_action": a.next}
    if a.blockers:
        body["blockers"] = a.blockers
    if a.task:
        body["task_nums"] = [_task_num(x) for x in a.task]
    if a.ref:
        body["refs"] = [_parse_ref(x) for x in a.ref]
    if a.body:
        body["body"] = sys.stdin.read() if a.body == "-" else a.body
    ctx = open_session(a)
    with ctx.state.locked():
        r = send_op(ctx, "POST", f"{ctx.path}/checkpoints", body, "checkpoint save")
    print(f"checkpoint #{r['n']} saved · #{r['seq']}")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """`join … -- CLIENT…` and `lease run … -- COMMAND…`: everything after `--` is the command. Plain
    argparse binds the optional positional before it reaches `--` when options sit in between."""

    def parse_known_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        words = [w for i, w in enumerate(args) if not w.startswith("-") and (i == 0 or args[i - 1] != "--as")]
        if self.prog == "glassboard" and "--" in args and words[:1] in (["join"], ["lease"]):
            i = args.index("--")
            ns, rest = super().parse_known_args(args[:i], namespace)
            ns.command = args[i + 1:]
            return ns, rest
        return super().parse_known_args(args, namespace)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="glassboard")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--as", dest="as_profile", metavar="PROFILE", help="act as this joined identity (e.g. jose-claude)")
    ap.add_argument("-s", "--session", dest="global_session", metavar="SESSION",
                    help="session for any session-scoped command (may appear before the subcommand)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login"); lg.add_argument("--url", required=True); lg.add_argument("--ca-file")
    lg.add_argument("--fetch-ca", action="store_true", help="download the board CA and pin it by fingerprint")
    lg.add_argument("--ca-fingerprint", help="SHA256:… from your board admin (with --fetch-ca)")
    lg.add_argument("--insecure-http", action="store_true",
                    help="allow plain http:// to a board in plain-HTTP mode (NO TLS: tokens readable on the LAN)")
    g = lg.add_mutually_exclusive_group(); g.add_argument("--token-file"); g.add_argument("--token-stdin", action="store_true")
    jn = sub.add_parser("join", help="agents: redeem an invite code from your human")
    jn.add_argument("url"); jn.add_argument("code"); jn.add_argument("--ca-file")
    jn.add_argument("--fetch-ca", action="store_true"); jn.add_argument("--ca-fingerprint")
    jn.add_argument("--insecure-http", action="store_true")
    jn.add_argument("--here", action="store_true", help="identify by a pointer file in this directory instead")
    jn.add_argument("--local", action="store_true",
                    help="with --here: keep this identity's config, credential and state in ./.glassboard (for clients "
                         "sandboxed to their workspace)")
    jn.add_argument("--compact-at", metavar="PERCENT|off", help="with -- CLIENT: its auto-compaction threshold")
    jn.add_argument("command", nargs="*", help="-- CLIENT…: start the agent client as this identity")
    sub.add_parser("protocol", help="print the agent protocol")
    sk = sub.add_parser("skill", help="show or install the agent skill (Claude Code, Codex)")
    sk.add_argument("action", choices=["install", "show"])
    sk.add_argument("--for", dest="client", choices=["claude", "codex", "all"], default="all")
    sk.add_argument("--home", help=argparse.SUPPRESS)
    w = sub.add_parser("whoami"); w.add_argument("--json", action="store_true")
    dc = sub.add_parser("doctor", help="check profile, credential identity and persistent client state")
    dc.add_argument("--json", action="store_true")
    d = sub.add_parser("discovery"); d.add_argument("--json", action="store_true")
    e = sub.add_parser("env-probe"); e.add_argument("--json", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--compact-at", metavar="PERCENT|off",
                   help="this run's auto-compaction threshold for claude or codex (default: agent, board, 75)")
    r.add_argument("command", nargs=argparse.REMAINDER)
    cm = sub.add_parser("compaction", help="show or set this agent's auto-compaction threshold for `glassboard run`")
    cm.add_argument("action", choices=["show", "set"]); cm.add_argument("value", nargs="?", help="PERCENT, off or unset")
    cm.add_argument("--json", action="store_true")
    hs = sub.add_parser("hooks", help="show or install the opt-in client hooks (resume brief, compaction events)")
    hs.add_argument("action", choices=["show", "install"])
    hs.add_argument("--for", dest="client", choices=["claude", "codex", "all"], default="all")
    hs.add_argument("--project", metavar="DIR", help="install into DIR/.claude or DIR/.codex instead of your home")
    hs.add_argument("--deliver", action="store_true", help="also deliver new board events into the open session after each tool call")
    hs.add_argument("--hold", action="store_true", help="with --deliver: keep a finished turn going once while events wait (Stop hook)")
    hs.add_argument("--home", help=argparse.SUPPRESS)

    def sess(p):
        p.add_argument("-s", "--session", help="session handle (default: the one chosen with `use`)")
        return p

    se = sub.add_parser("session").add_subparsers(dest="action", required=True)
    for verb in ("pause", "resume", "archive"):
        sv = se.add_parser(verb); sv.add_argument("reason"); sv.add_argument("-s", "--session")
    sc = se.add_parser("create"); sc.add_argument("handle"); sc.add_argument("title")
    sc.add_argument("--stale-after", type=int, metavar="SECONDS"); sc.add_argument("--json", action="store_true")
    sc.add_argument("--autonomous", action="store_true", help="allow peer-reviewed child-task outcomes by agent coordinators")
    ss = sub.add_parser("sessions"); ss.add_argument("--json", action="store_true")
    us = sub.add_parser("use"); us.add_argument("session")
    # Shipped syntax: members ACTION SESSION [HANDLE]
    mb = sub.add_parser("members"); mb.add_argument("action", choices=["list", "add", "remove"])
    mb.add_argument("session"); mb.add_argument("handle", nargs="?")
    mb.add_argument("--role", choices=["coordinator", "member", "observer"], default="member")
    mb.add_argument("--json", action="store_true")
    ag = sub.add_parser("agent").add_subparsers(dest="action", required=True)
    aa = ag.add_parser("add"); aa.add_argument("name"); aa.add_argument("--kind", choices=["agent", "client"], default="agent")
    tk = sub.add_parser("token"); tk.add_argument("action", choices=["new", "revoke"]); tk.add_argument("target", nargs="?")
    tk.add_argument("--scope", choices=["full", "read"], default="full"); tk.add_argument("--expires-days", type=int)
    tk.add_argument("--label"); tk.add_argument("--out", help="write the token to this new file (mode 0600)")
    jr = sub.add_parser("role", help="list, show or edit job-role prompts")
    jr.add_argument("action", choices=["list", "show", "history", "set", "assign", "clear", "assignments"])
    jr.add_argument("name", nargs="?")
    jr.add_argument("--prompt-file", help="UTF-8 file used by role set")
    jr.add_argument("--to", help="session member whose job role is assigned")
    jr.add_argument("-s", "--session")
    jr.add_argument("--json", action="store_true")
    for verb in ("post", "preflight"):
        po = sess(sub.add_parser(verb)); po.add_argument("kind", choices=["question", "answer", "update", "proposal", "blocker", "note", "decision", "approval"], help="message kind; decision is human Session Managers only; approval must name humans with --to")
        po.add_argument("body", nargs="?", help='message text immediately after KIND, or "-" to read stdin (optional for a stand-up update)'); po.add_argument("--to", action="append", help="comma-separated handles (repeatable); omit it or use 'all' to broadcast")
        po.add_argument("--body", dest="body_option", help='message text; unlike the positional form, may appear after other options')
        po.add_argument("--body-file", metavar="PATH", help='read UTF-8 message text from PATH (or "-" for stdin)')
        po.add_argument("--re", type=int); po.add_argument("--task"); po.add_argument("--channel", metavar="NAME", help="post in a topic channel"); po.add_argument("--ref", action="append", metavar="LABEL|LOC|ACCESS[|SHA256]", help="reference; ACCESS is owner_only, shared, or unavailable")
        po.add_argument("--done", help="stand-up update: what got done"); po.add_argument("--doing", help="stand-up update: in progress")
        po.add_argument("--blocked", help="stand-up update: what blocks you")
        po.add_argument("--draft", action="append", metavar="TITLE|SCOPE|DELIVERABLE[|ASSIGNEE[|REVIEWER]]", help="proposal: a proposed task (repeat, at most 10)")
        if verb == "post":
            pf_options = po.add_mutually_exclusive_group()
            pf_options.add_argument("--check", action="store_true", help="record a preflight, then post only if ready")
            pf_options.add_argument("--preflight-seq", type=int, help="link to a ready preflight of the exact message")
    sy = sess(sub.add_parser("sync")); sy.add_argument("--ack", nargs="?", const="stored", metavar="N")
    sy.add_argument("--takeover", metavar="REASON"); sy.add_argument("--limit", type=int, default=50)
    sy.add_argument("--wait", type=int, default=0); sy.add_argument("--json", action="store_true")
    sy.add_argument("--include-checkpoint", action="store_true",
                    help="include your latest checkpoint in full (use after a context compaction)")
    wa = sess(sub.add_parser("watch", help="runner integration: continuously long-poll and emit JSON packets"))
    wa.add_argument("--wait", type=int, default=20, metavar="SECONDS")
    wa.add_argument("--limit", type=int, default=50)
    wa.add_argument("--interval", type=float, default=1.0, metavar="SECONDS")
    wa.add_argument("--heartbeats", action="store_true", help="also print unchanged timeout packets")
    hi = sess(sub.add_parser("history", help="read the log: the latest --limit events, or oldest first from --after"))
    hi.add_argument("--after", type=int, help="oldest first after this event number (0 = from the start)")
    hi.add_argument("--before", type=int, help="the latest --limit events before this event number")
    hi.add_argument("--task"); hi.add_argument("--thread", type=int); hi.add_argument("--actor"); hi.add_argument("--type")
    hi.add_argument("--channel", metavar="NAME")
    hi.add_argument("--to-me", action="store_true"); hi.add_argument("--limit", type=int, default=50); hi.add_argument("--json", action="store_true")
    qu = sess(sub.add_parser("question")); qu.add_argument("action", choices=["resolve"]); qu.add_argument("seq", type=int)
    qu.add_argument("resolution", choices=["declined", "obsolete", "answered"]); qu.add_argument("note", nargs="?")
    rc = sess(sub.add_parser("reconcile")); grp = rc.add_mutually_exclusive_group()
    grp.add_argument("--resend", metavar="OP"); grp.add_argument("--drop", metavar="OP")
    sess(sub.add_parser("outbox"))
    cp = sess(sub.add_parser("checkpoint")); cp.add_argument("action", choices=["save", "show"])
    cp.add_argument("text", nargs="?", help="save: the summary (same as --summary)")
    cp.add_argument("--summary"); cp.add_argument("--next"); cp.add_argument("--blockers")
    cp.add_argument("--task", action="append", metavar="T#"); cp.add_argument("--ref", action="append", metavar="LABEL|LOC|ACCESS[|SHA256]", help="reference; ACCESS is owner_only, shared, or unavailable")
    cp.add_argument("--body", help='longer handoff text, or "-" for stdin'); cp.add_argument("--principal")
    cp.add_argument("--n"); cp.add_argument("--json", action="store_true")
    tk2 = sess(sub.add_parser("task"))
    tk2.add_argument("action", choices=["list", "next", "show", "trail", "create", "review", "edit", "acknowledge", "claim", "note", "check", "block", "unblock", "submit",
                                        "release", "decline", "reclaim", "accept", "return", "reassign", "cancel", "reopen"])
    tk2.add_argument("task", nargs="?", help="T# (for create: the title)")
    tk2.add_argument("text", nargs="?", help="note / reason / summary / handoff / feedback")
    tk2.add_argument("--scope"); tk2.add_argument("--deliverable"); tk2.add_argument("--assignee"); tk2.add_argument("--title")
    tk2.add_argument("--stale-after", type=int, metavar="SECONDS"); tk2.add_argument("--to")
    tk2.add_argument("--status"); tk2.add_argument("--mine", action="store_true"); tk2.add_argument("--json", action="store_true")
    tk2.add_argument("--ref", action="append", metavar="LABEL|LOC|ACCESS[|SHA256]", help="reference; ACCESS is owner_only, shared, or unavailable")
    tk2.add_argument("--as-coordinator", action="store_true", help="release without a fence (human coordinator)")
    tk2.add_argument("--check", action="append", metavar="ITEM", help="checklist item (create/edit; repeat; edit replaces the list)")
    tk2.add_argument("--undo", action="store_true", help="task check: mark the item not done")
    tk2.add_argument("--waiting-on", metavar="#SEQ|T#|@HANDLE", help="task block: what the task waits on")
    tk2.add_argument("--depends-on", action="append", metavar="T#[,T#]", help="create/edit: tasks this one needs ('none' clears)")
    tk2.add_argument("--parent", metavar="T#", help="create/edit: parent task ('none' clears)")
    tk2.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    tk2.add_argument("--from-proposal", type=int, metavar="SEQ", help="create: link (and with --draft, fill) from a proposal")
    tk2.add_argument("--draft", metavar="N|all", help="create: use draft N of the proposal, or all of them")
    tk2.add_argument("--reviewer", metavar="HANDLE", help="create/edit: the member who reviews submitted work ('none' clears)")
    tk2.add_argument("--notes", help="task review: what to change, or why it is good")
    tk2.add_argument("--submission", type=int, metavar="SEQ", help="task review: exact task.submitted event number")
    tk2.add_argument("--supersedes", type=int, metavar="SEQ",
                     help="task submit on a task in review: the submission this revision replaces (default: the current one)")
    tk2.add_argument("--commit", metavar="FULL_SHA", help="submit/review: exact 40- or 64-character Git commit")
    tk2.add_argument("--test", action="append", metavar="OUTCOME|COMMAND[|DETAIL]",
                     help="task submit: structured test evidence (repeatable)")
    tk2.add_argument("--allow-incomplete-checklist", metavar="REASON",
                     help="task submit: record why unchecked acceptance items remain")
    tk2.add_argument("--effort", type=int, metavar="MINUTES", help="task note: minutes of work since the last note")
    tk2.add_argument("--done", help="submit/release handoff: what is done"); tk2.add_argument("--gotchas", help="handoff: pitfalls")
    tk2.add_argument("--next", dest="next_step", help="submit/release handoff: what comes next")
    bo = sess(sub.add_parser("board", help="the session's tasks as a kanban board")); bo.add_argument("--json", action="store_true")
    ar = sess(sub.add_parser("artifact", help="upload, list or download immutable evidence"))
    ar.add_argument("action", choices=["upload", "list", "get"]); ar.add_argument("value", nargs="?", help="file path or artifact ID")
    ar.add_argument("--out", help="get: create this output file"); ar.add_argument("--name", help="upload: display file name")
    ar.add_argument("--media-type", default="application/octet-stream"); ar.add_argument("--description")
    ar.add_argument("--task", metavar="T#"); ar.add_argument("--deliverable", action="store_true",
                    help="upload: place the immutable file in the session delivery area")
    ar.add_argument("--json", action="store_true")
    dg = sess(sub.add_parser("digest", help="what changed since your last ack (or --after N)"))
    dg.add_argument("--after", type=int); dg.add_argument("--json", action="store_true")
    de = sess(sub.add_parser("decisions", help="every decision in the session")); de.add_argument("--json", action="store_true")
    ex = sess(sub.add_parser("export", help="the whole session as canonical text (md) or JSON lines"))
    ex.add_argument("--format", choices=["md", "jsonl"], default="md"); ex.add_argument("--out", help="write to a new file")
    iv = sess(sub.add_parser("invite", help="Session Managers: one-time join code for one of your agents"))
    iv.add_argument("agent", help="the agent's name under you (alice/<agent>)")
    iv.add_argument("--role", choices=["member", "coordinator", "observer"], default="member")
    iv.add_argument("--json", action="store_true")
    sub.add_parser("reference", help="print the agent reference (every capability: API, CLI, dashboard)")
    su = sess(sub.add_parser("standup", help="each member's latest stand-up update")); su.add_argument("--json", action="store_true")
    br = sess(sub.add_parser("brief", help="resume brief: live claims, owed, inbox, conflicts, checkpoint, decisions"))
    br.add_argument("--for", dest="member", help="coordinators: another member's brief"); br.add_argument("--json", action="store_true")
    ch = sess(sub.add_parser("claude-channel", help="Claude Code channels (research preview): push a fixed notice into an idle open session"))
    ch.add_argument("action", choices=["install", "serve"])
    ch.add_argument("--project", metavar="DIR", help="install: the project whose .mcp.json gets the server (default: here)")
    tc = sess(sub.add_parser("channel", help="topic channels in this session: list, create, change, archive, your mode"))
    tc.add_argument("action", choices=["list", "create", "topic", "add", "remove", "posters", "archive", "mode"])
    tc.add_argument("name", nargs="?", help="channel name (lowercase letters, digits, dashes)")
    tc.add_argument("values", nargs="*", help="topic: the text; add/remove/posters: handles; mode: follow|mentions|mute")
    tc.add_argument("--topic"); tc.add_argument("--member", action="append", metavar="HANDLE", help="create: a member (repeat)")
    tc.add_argument("--post-only", action="store_true", help="create: only --poster handles (and you) may post")
    tc.add_argument("--poster", action="append", metavar="HANDLE"); tc.add_argument("--json", action="store_true")
    hk = sess(sub.add_parser("hook", help="called by client hooks (see `glassboard hooks`); always exits 0"))
    hk.add_argument("action", choices=["session-start", "compacted", "deliver", "stop"])
    hk.add_argument("--client", choices=["claude", "codex", "other"], default="other")
    se = sess(sub.add_parser("search", help="find messages, titles, summaries and tasks in this session"))
    se.add_argument("query", nargs="+"); se.add_argument("--limit", type=int); se.add_argument("--json", action="store_true")
    bl = sess(sub.add_parser("blockers", help="what holds up the most work: root causes of waiting tasks, biggest first"))
    bl.add_argument("--json", action="store_true")
    st = sess(sub.add_parser("status", help="one row per agent: derived state and why, claims, owed, leases, last event, checkpoint"))
    st.add_argument("--json", action="store_true")
    ho = sess(sub.add_parser("hold", help="raise an objection with a default and a deadline (never blocks), or decide one"))
    ho.add_argument("action", choices=["raise", "decide"])
    ho.add_argument("arg", nargs="?", help="raise: what should wait, and why; decide: the hold's SEQ")
    ho.add_argument("decision", nargs="?", choices=["uphold", "proceed"], help="decide: uphold the hold or proceed")
    ho.add_argument("--default", help="raise: what happens if nobody decides in time")
    ho.add_argument("--deadline", type=_minutes, metavar="MINUTES", help="raise: 5-1440 minutes (30, 30m, 2h)")
    ho.add_argument("--task", metavar="T#"); ho.add_argument("--note", help="decide: a note for the record")
    sd = sess(sub.add_parser("standdown", help="humans ask an agent to stop and hand off; the agent answers with done"))
    sd.add_argument("action", choices=["request", "done", "lift"])
    sd.add_argument("arg", nargs="?", help="request: the agent's handle; lift: the request SEQ; done: (none)")
    sd.add_argument("text", nargs="?", help="request: reason; done: handoff note; lift: note")
    qh = sess(sub.add_parser("quiet", help="quiet hours: no wakes for an agent, no notifications for you, in a daily window"))
    qh.add_argument("action", choices=["show", "set", "clear"])
    qh.add_argument("--for", dest="member", metavar="MEMBER", help="set/clear: an agent you own or coordinate (default: you)")
    qh.add_argument("--from", dest="start", metavar="HH:MM"); qh.add_argument("--to", dest="end", metavar="HH:MM")
    qh.add_argument("--tz", metavar="ZONE", help="IANA time zone (default: this machine's)")
    qh.add_argument("--urgent", action="store_true", help="set: let wakes a human caused through (messages, decisions)")
    qh.add_argument("--json", action="store_true")
    apv = sess(sub.add_parser("approval", help="humans: grant or deny an approval request"))
    apv.add_argument("decision", choices=["grant", "deny"]); apv.add_argument("seq", type=int); apv.add_argument("note", nargs="?")
    le = sess(sub.add_parser("lease", help="advisory claims on a resource (path, document, service)"))
    le.add_argument("action", choices=["take", "release", "list", "run"]); le.add_argument("resource", nargs="?")
    le.add_argument("--note"); le.add_argument("--json", action="store_true")
    le.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="take/run: wait in line until it is free")
    le.add_argument("command", nargs="*", help="run: -- COMMAND… to run while holding the lease")
    wk = sess(sub.add_parser("wake", help="wake idle agents: status, done, and the human-armed supervisor (run)"))
    wk.add_argument("action", choices=["status", "done", "run", "arm", "pause", "resume", "stop", "disarm"])
    wk.add_argument("reason", nargs="?", help="pause/resume/stop: why")
    wk.add_argument("--json", action="store_true"); wk.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    wk.add_argument("--generation", type=int, help="done: the generation you inspected (a supervisor sets it for you)")
    wk.add_argument("--client", choices=["claude", "codex", "grok", "opencode", "script"])
    wk.add_argument("--session-id", help="run: the client's exact session ID to resume")
    wk.add_argument("--agent-runtime", help="run: the runtime label the agent uses (its inbox consumer)")
    wk.add_argument("--takeover", metavar="REASON", help="run/arm: replace a supervisor your human confirmed is gone")
    wk.add_argument("--cwd", help="run: working directory for the client")
    wk.add_argument("--script", help="run: the program the script adapter starts")
    wk.add_argument("--attach", metavar="URL", help="run: OpenCode server URL to resume through")
    wk.add_argument("--prompt-file", help="run: a fixed prompt file (yours, not writable by others)")
    wk.add_argument("--max-turns", type=int, help="run: per-wake turn cap where the client has one")
    wk.add_argument("--debounce", type=float, default=30.0, metavar="SECONDS", help="run: let bursts coalesce")
    wk.add_argument("--max-per-hour", type=int, default=6, help="run: wake budget per hour (then stop)")
    wk.add_argument("--idle-stops", type=int, default=3, help="run: stop after this many wakes without progress")
    wk.add_argument("--max-dispatches", type=int, default=0, help="run: stop after this many dispatches (0: no limit)")
    wk.add_argument("--headless-only", action="store_true",
                    help="run: you attest this session is never opened in a terminal (for clients with no open-session signal)")
    wk.add_argument("--recheck", type=float, default=10.0, metavar="SECONDS",
                    help="run: while a wake is held (session open or unknown), check again this often")
    wk.add_argument("command", nargs="*", help="run: -- extra client arguments (escalating flags are refused)")
    tm = sub.add_parser("team", help="Session Managers: start scripts that start each agent already joined").add_subparsers(
        dest="action", required=True)
    for verb, text in (("up", "write one start script per agent into its folder (nothing changes on the board)"),
                       ("start", "what a start script runs: fresh join code, join this folder, hooks, client, wake")):
        tp = tm.add_parser(verb, help=text)
        tp.add_argument("session_name", metavar="SESSION")
        if verb == "up":
            tp.add_argument("pairs", nargs="+", metavar="AGENT:FOLDER", help="an agent name under you and its folder")
            tp.add_argument("--client", default="claude", metavar="claude|codex|grok|opencode|CMD",
                            help="the client each script starts (default claude); a command line is allowed")
            tp.add_argument("--force", action="store_true", help="replace existing start scripts")
        else:
            tp.add_argument("agent", help="the agent's name under you")
            tp.add_argument("--compact-at", metavar="PERCENT|off", help="the client's auto-compaction threshold")
            tp.add_argument("command", nargs="*", help="-- CLIENT…: the client to start as the agent")
        tp.add_argument("--role", choices=["member", "coordinator", "observer"], default="member")
        tp.add_argument("--local", action="store_true", help="keep the agent's config, credential and state in FOLDER/.glassboard")
        tp.add_argument("--no-hooks", action="store_true", help="do not install the delivery hooks (Claude Code, Codex)")
        tp.add_argument("--no-hold", action="store_true", help="delivery hooks without the Stop hook that keeps a turn going once")
        tp.add_argument("--no-wake", action="store_true", help="do not offer the wake supervisor when the client exits")
    fl = sess(sub.add_parser("flow", help="cycle times, blocked time, review turnaround, throughput")); fl.add_argument("--json", action="store_true")
    at = sub.add_parser("attention", help="what needs you, across your sessions"); at.add_argument("--json", action="store_true")
    ov = sub.add_parser("overview", help="one line per session"); ov.add_argument("--json", action="store_true")
    return ap


def _main(argv=None) -> int:
    ap = build_parser()
    a, extra = ap.parse_known_args(argv)
    # argparse subparsers do not support parse_intermixed_args. Preserve the documented positional
    # message form when a quoted body follows options, e.g. `post answer --re 11 "text"`.
    if extra and a.cmd in ("post", "preflight") and a.body is None and len(extra) == 1 and not extra[0].startswith("-"):
        a.body = extra[0]
    elif extra and a.cmd == "wake" and a.action == "run":
        a.command = [*a.command, *extra]               # client arguments after `--`; refused flags are checked in run
    elif extra and a.cmd == "team" and a.action == "start" and extra[0] == "--":
        a.command = [*a.command, *extra]               # the client command line after `--`, after the options
    elif extra:
        ap.error("unrecognized arguments: " + " ".join(extra))
    if (a.cmd == "task" and a.action not in ("list", "next") and not a.task
            and not (a.action == "create" and a.from_proposal is not None and a.draft)):
        ap.error(f"task {a.action} needs a task (or, for create, a title)")
    if a.cmd == "task" and a.action == "create" and not (a.scope and a.deliverable) and not (a.from_proposal is not None and a.draft):
        ap.error("task create needs --scope and --deliverable")
    if a.cmd == "artifact" and a.action in ("upload", "get") and not a.value:
        ap.error(f"artifact {a.action} needs {'a file path' if a.action == 'upload' else 'an artifact ID'}")
    if getattr(a, "ack", None) not in (None, "stored") and not str(a.ack).isdigit():
        ap.error("--ack takes no value or a non-negative integer")
    if a.cmd not in ("join", "skill", "team"):              # team: the human's own login (team.use_human)
        name, source = profiles.resolve(a.as_profile)
        a.resolved_profile, a.profile_source = name, source
        if name:
            if not profiles.NAME_RE.match(name) or not profiles.exists(name):
                if a.cmd == "doctor":
                    return cmd_doctor(a)
                print(f"glassboard: no joined identity by that name ({source}); run `glassboard join` first",
                      file=sys.stderr)
                return EXIT_CONFIG
            profiles.activate(name)
            os.environ["GLASSBOARD_PROFILE_SOURCE"] = source
    try:
        if a.cmd == "run":
            cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
            if not cmd:
                raise CliError(EXIT_USAGE, "usage", "usage: glassboard run -- <command> [args...]")
            livesession.hold_for_client()                     # a wake supervisor sees this client as open (E31)
            return runtime.run(_with_compaction(cmd, a.compact_at), on_exit=_report_exit)
        if a.cmd == "session":
            return cmd_session_create(a) if a.action == "create" else cmd_session_state(a)
        return _with_inbox_news({"login": cmd_login, "whoami": cmd_whoami, "doctor": cmd_doctor,
                "discovery": cmd_discovery, "env-probe": cmd_env_probe,
                "sessions": cmd_sessions, "use": cmd_use, "members": cmd_members, "agent": cmd_agent,
                "token": cmd_token, "role": cmd_role, "post": cmd_post, "preflight": cmd_preflight,
                "sync": cmd_sync, "watch": cmd_watch, "wake": cmd_wake, "history": cmd_history, "board": cmd_board, "artifact": cmd_artifact,
                "digest": lambda a: _session_view(a, "digest", {"after": a.after}),
                "decisions": lambda a: _session_view(a, "decisions"), "standup": lambda a: _session_view(a, "standup"), "status": lambda a: _session_view(a, "status"),
                "blockers": lambda a: _session_view(a, "blockers"),
                "search": lambda a: _session_view(a, "search", {"q": " ".join(a.query), "limit": a.limit}),
                "brief": lambda a: _session_view(a, "brief", {"principal": a.member}),
                "hook": cmd_hook, "hooks": cmd_hooks, "claude-channel": cmd_claude_channel, "channel": cmd_topic_channel, "compaction": cmd_compaction,
                "approval": cmd_approval, "hold": cmd_hold, "standdown": cmd_standdown, "quiet": cmd_quiet, "lease": cmd_lease, "flow": lambda a: _session_view(a, "flow"),
                "attention": lambda a: _me_view("attention", a), "overview": lambda a: _me_view("overview", a),
                "question": cmd_question, "reconcile": cmd_reconcile, "outbox": cmd_outbox, "task": cmd_task,
                "checkpoint": cmd_checkpoint, "join": cmd_join, "protocol": cmd_protocol, "skill": cmd_skill,
                "export": cmd_export, "invite": cmd_invite, "reference": cmd_reference, "team": cmd_team}[a.cmd](a), a)
    except CliError as e:
        _print_error(e)
        return e.code
    except ValueError as e:                 # e.g. an invalid session name reaching clistate
        _print_error(CliError(EXIT_USAGE, "usage", str(e)))
        return EXIT_USAGE


NO_NEWS = ("hook", "claude-channel", "wake", "sync", "watch", "hooks", "run", "join", "team")


def _with_inbox_news(rc: int, a) -> int:
    """After a successful write, one line on stderr when unread events wait (from the board's
    Glassboard-Inbox-Unread header); stdout stays exactly the command's own output."""
    if rc == EXIT_OK and LAST_UNREAD and LAST_UNREAD != "0" and getattr(a, "cmd", None) not in NO_NEWS:
        sys.stdout.flush()                   # the command's own output comes first, also when both are captured
        print(f"inbox: {r2(LAST_UNREAD)} unread · glassboard sync to read them", file=sys.stderr)
    return rc


def main(argv=None) -> int:
    try:
        return _main(argv)
    except OSError as e:                    # local state or config (network errors are CliErrors already)
        if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        _print_error(CliError(EXIT_CONFIG, "state_unwritable", f"Cannot write {e.filename or 'local CLI state'}.",
                              {"hint": "Inside a sandbox that only lets you write in your workspace, ask your human for "
                                       "a new join code and join from the workspace with `glassboard join URL CODE "
                                       "--here --local`: everything then lives in ./.glassboard. Otherwise make the CLI "
                                       "state and config directories writable."}))
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
