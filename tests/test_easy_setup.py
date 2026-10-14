"""Easier setup.

(1) Easier TLS: `GET /ca.crt` publishes the board's root certificate; `glassboard-admin setup-tls`
    prints its fingerprint and the join command; `glassboard login --fetch-ca --ca-fingerprint`
    pins it. https stays verified everywhere.
(2) Plain-HTTP mode: GLASSBOARD_INSECURE_HTTP=1 on the server, `--insecure-http` on the client;
    the app enforces LAN-only itself and warns in discovery, the dashboard and every CLI command.
"""
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

from glassboard import admin, tlsboot
from glassboard.app import create_app

from conftest import free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not installed")


def make_ca(tmp, name="ca"):
    """A throwaway CA and a server certificate for localhost/127.0.0.1 (openssl CLI)."""
    d = tmp / name
    d.mkdir()
    run = lambda *a: subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)      # noqa: E731
    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key",
        "-out", "ca.crt", "-days", "2", "-subj", "/CN=test board CA")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "srv.key",
        "-out", "srv.csr", "-subj", "/CN=localhost")
    (d / "ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.crt",
        "-days", "2", "-extfile", "ext")
    return d


class Uvicorn:
    def __init__(self, app, host="127.0.0.1", ssl_dir=None):
        import uvicorn
        self.port = free_port()
        kw = {"ssl_certfile": str(ssl_dir / "srv.crt"), "ssl_keyfile": str(ssl_dir / "srv.key")} if ssl_dir else {}
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=self.port, log_level="warning", **kw))
        self.t = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.t.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.025)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.t.join(timeout=10)


def cli(env, *args, stdin=None):
    return subprocess.run([sys.executable, "-m", "glassboard.cli", *args], env=env, capture_output=True, text=True,
                          timeout=60, input=stdin)


def home(tmp, who="jose-claude"):
    h = tmp / f"home-{who}"
    h.mkdir(exist_ok=True)
    return {"PATH": os.environ["PATH"], "HOME": str(h), "XDG_CONFIG_HOME": str(h / "cfg"), "XDG_STATE_HOME": str(h / "st")}


# --- tlsboot ---------------------------------------------------------------------------------------

@needs_openssl
def test_fingerprint_matches_openssl(tmp_path):
    d = make_ca(tmp_path)
    pem = (d / "ca.crt").read_text()
    ours = tlsboot.fingerprint(pem)
    theirs = subprocess.run(["openssl", "x509", "-in", "ca.crt", "-noout", "-fingerprint", "-sha256"], cwd=d,
                            capture_output=True, text=True, check=True).stdout.strip().split("=", 1)[1]
    assert ours == "SHA256:" + theirs.upper()
    assert tlsboot.normalize(theirs.replace(":", "").lower()) == ours
    assert tlsboot.normalize("sha256" + theirs.replace(":", "").lower()) == ours == tlsboot.normalize(ours)
    with pytest.raises(tlsboot.TlsBootError):
        tlsboot.normalize("SHA256:" + theirs[:-3])


@needs_openssl
def test_check_ca_pem_refuses_keys_and_junk(tmp_path):
    d = make_ca(tmp_path)
    for bad in ((d / "ca.key").read_text(), (d / "ca.crt").read_text() + (d / "ca.key").read_text(),
                "hello", (d / "ca.crt").read_text() * 2):
        with pytest.raises(tlsboot.TlsBootError):
            tlsboot.check_ca_pem(bad)


def test_only_tlsboot_ever_skips_verification():
    """The bootstrap download is the one unverified connection, and it is pinned by fingerprint."""
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
            if re.search(r"CERT_NONE|check_hostname\s*=\s*False|verify\s*=\s*False", p.read_text())]
    assert hits == ["src/glassboard/tlsboot.py"]


# --- /ca.crt and setup-tls --------------------------------------------------------------------------

@needs_openssl
def test_ca_crt_is_public_and_never_a_key(gb, tmp_path):
    assert gb.client.get("/ca.crt").status_code == 404                       # nothing published yet
    d = make_ca(tmp_path)
    target = pathlib.Path(gb.app.state.ca_file)
    target.write_text((d / "ca.key").read_text())                            # misconfiguration: a key
    r = gb.client.get("/ca.crt")
    assert r.status_code == 404 and "PRIVATE" not in r.text
    target.write_text((d / "ca.crt").read_text())
    r = gb.client.get("/ca.crt")                                             # no token needed
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-pem-file")
    assert tlsboot.fingerprint(r.text) == tlsboot.fingerprint((d / "ca.crt").read_text())


@needs_openssl
def test_setup_tls_publishes_and_prints_the_join_command(gb, tmp_path, capsys):
    d = make_ca(tmp_path)
    rc = admin.main(["--db", gb.db, "setup-tls", "--host", "board.lan", "--ca", str(d / "ca.crt")])
    out = capsys.readouterr().out
    fp = tlsboot.fingerprint((d / "ca.crt").read_text())
    assert rc == 0 and fp in out
    assert f"glassboard login --url https://board.lan:8443 --fetch-ca --ca-fingerprint {fp} --token-file" in out
    assert gb.client.get("/ca.crt").status_code == 200
    rc = admin.main(["--db", gb.db, "setup-tls", "--ca", str(d / "ca.key")])
    assert rc == 5 and "private key" in capsys.readouterr().err
    rc = admin.main(["--db", gb.db, "setup-tls", "--ca", str(tmp_path / "missing.crt")])
    assert rc == 6 and "Start Caddy once" in capsys.readouterr().err


# --- login --fetch-ca over real TLS -------------------------------------------------------------------

@needs_openssl
def test_fetch_ca_pins_by_fingerprint(gb, tmp_path):
    d = make_ca(tmp_path)
    pathlib.Path(gb.app.state.ca_file).write_text((d / "ca.crt").read_text())
    fp = tlsboot.fingerprint((d / "ca.crt").read_text())
    tok = tmp_path / "tok"
    tok.write_text(gb.tok["jose/claude"] + "\n")
    gb.stop()
    with Uvicorn(create_app(gb.db, allowed_hosts=["localhost"]), ssl_dir=d) as srv:
        url = f"https://localhost:{srv.port}"
        env = home(tmp_path)
        cfgdir = pathlib.Path(env["XDG_CONFIG_HOME"]) / "glassboard"
        # without the CA the verified connection fails: proves https is really verified
        r = cli(env, "login", "--url", url, "--token-file", str(tok))
        assert r.returncode == 4 and "tls_error" in r.stderr
        # wrong fingerprint: nothing saved, no token sent
        wrong = "SHA256:" + ":".join(["AB"] * 32)
        r = cli(env, "login", "--url", url, "--fetch-ca", "--ca-fingerprint", wrong, "--token-file", str(tok))
        assert r.returncode == 4 and "ca_fingerprint_mismatch" in r.stderr
        assert not (cfgdir / "ca.crt").exists() and not (cfgdir / "credentials").exists()
        # no fingerprint and no terminal: refused
        r = cli(env, "login", "--url", url, "--fetch-ca", "--token-file", str(tok))
        assert r.returncode == 2 and "ca_fingerprint_required" in r.stderr
        # right fingerprint: pinned, logged in, later commands verify against the pinned root
        r = cli(env, "login", "--url", url, "--fetch-ca", "--ca-fingerprint", fp.lower().replace(":", ""),
                "--token-file", str(tok))
        assert r.returncode == 0 and r.stdout.startswith("@jose/claude (agent, owner jose)"), r.stderr
        assert (cfgdir / "ca.crt").read_text() == (d / "ca.crt").read_text()
        assert json.loads((cfgdir / "config.json").read_text())["ca_file"] == str(cfgdir / "ca.crt")
        assert cli(env, "whoami").returncode == 0
    # a different CA on the same URL: the pinned root refuses it (no silent re-trust)
    d2 = make_ca(tmp_path, "other")
    with Uvicorn(create_app(gb.db, allowed_hosts=["localhost"]), ssl_dir=d2) as srv2:
        cfg = json.loads((cfgdir / "config.json").read_text())
        cfg["url"] = f"https://localhost:{srv2.port}"
        (cfgdir / "config.json").write_text(json.dumps(cfg))
        r = cli(env, "whoami")
        assert r.returncode == 4 and "tls_error" in r.stderr


@needs_openssl
def test_fetch_ca_refuses_http_and_conflicting_flags(tmp_path):
    env = home(tmp_path)
    for args in (["--url", "http://board.lan:8080", "--fetch-ca"],
                 ["--url", "https://board.lan:8443", "--fetch-ca", "--ca-file", "x.crt"],
                 ["--url", "https://board.lan:8443", "--ca-fingerprint", "SHA256:00"]):
        r = cli(env, "login", *args, "--token-stdin", stdin="gbt_x\n")
        assert r.returncode == 2, (args, r.stderr)


# --- plain-HTTP mode ----------------------------------------------------------------------------------

@pytest.fixture
def plain(gb):
    gb.stop()
    return create_app(gb.db, allowed_hosts=["testserver", "127.0.0.2"], plain_http=True)


def test_plain_mode_is_lan_only(gb, plain):
    for addr, ok in (("192.168.1.20", True), ("10.1.2.3", True), ("127.0.0.1", True), ("172.20.0.1", True),
                     ("8.8.8.8", False), ("172.32.0.1", False), ("100.64.0.1", False), ("testclient", False)):
        c = TestClient(plain, client=(addr, 40000))
        r = c.get("/api/v1/whoami", headers={"Authorization": f"Bearer {gb.tok['jose']}"})
        assert (r.status_code == 200) == ok, (addr, r.status_code)
        if not ok:
            assert r.status_code == 403 and r.json()["error"]["code"] == "lan_only"
        assert c.get("/health/live").status_code == (200 if ok else 403)


def test_plain_mode_warns_and_uses_plain_cookies(gb, plain):
    c = TestClient(plain, client=("192.168.1.20", 40000))
    d = c.get("/api/v1/discovery").json()
    assert d["transport"] == "plain-http" and "without TLS" in d["warning"]
    r = c.get("/login")
    assert "NOT ENCRYPTED" in r.text
    assert 'id="dismiss-plain-http"' in r.text and 'aria-label="Dismiss connection warning"' in r.text
    assert '<script src="/static/banner.js"></script>' in r.text
    assert r.text.index('/static/banner-preload.js') < r.text.index('/static/app.css')
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("gb_login=") and "secure" not in cookie.lower() and "httponly" in cookie.lower()
    nonce = cookie.split(";")[0].split("=", 1)[1]
    r = c.post("/login", data={"token": gb.tok["jose"], "csrf": nonce}, follow_redirects=False)
    assert r.status_code == 303 and "gb_session=" in r.headers["set-cookie"]
    assert "NOT ENCRYPTED" in c.get("/").text                                # every page, logged in too


def test_tls_mode_unchanged(gb):
    r = gb.client.get("/login")
    assert "NOT ENCRYPTED" not in r.text and r.headers["set-cookie"].startswith("__Host-gb_login=")
    assert "secure" in r.headers["set-cookie"].lower()
    assert "transport" not in gb.client.get("/api/v1/discovery").json()
    c = TestClient(gb.app, client=("8.8.8.8", 40000))                        # behind Caddy: Caddy guards
    assert c.get("/health/live").status_code == 200


def test_cli_plain_http_needs_the_flag_and_warns(gb, tmp_path, plain):
    """127.0.0.2 is on the loopback interface but not literal loopback for the CLI, like a LAN address."""
    tok = tmp_path / "tok"
    tok.write_text(gb.tok["jose/claude"] + "\n")
    with Uvicorn(plain, host="127.0.0.2") as srv:
        url = f"http://127.0.0.2:{srv.port}"
        env = home(tmp_path)
        r = cli(env, "login", "--url", url, "--token-file", str(tok))
        assert r.returncode == 2 and "insecure_url" in r.stderr and "--insecure-http" in r.stderr
        r = cli(env, "login", "--url", url, "--insecure-http", "--token-file", str(tok))
        assert r.returncode == 0 and r.stdout.startswith("@jose/claude") and "plain HTTP, no TLS" in r.stderr
        r = cli(env, "whoami")
        assert r.returncode == 0 and r.stderr.count("plain HTTP, no TLS") == 1
        r = cli(env, "login", "--url", "https://localhost:1", "--insecure-http", "--token-file", str(tok))
        assert r.returncode == 2                                            # https never goes unverified


def test_cli_loopback_check_is_parsed_not_prefix_matched(tmp_path):
    """Previously, `http://localhost.example` passed the loopback prefix check and was used in plain."""
    from glassboard.cli import is_loopback_http
    assert is_loopback_http("http://localhost:8080") and is_loopback_http("http://127.0.0.1:1/x")
    for url in ("http://localhost.example:80", "http://127.0.0.1.nip.io", "https://localhost", "http://localhostx"):
        assert not is_loopback_http(url), url
    r = cli(home(tmp_path), "login", "--url", "http://localhost.example:80", "--token-stdin", stdin="gbt_x\n")
    assert r.returncode == 2 and "insecure_url" in r.stderr


def test_client_request_refuses_non_http_configured_schemes():
    from glassboard.cli import CliError, request
    with pytest.raises(CliError, match="http or https"):
        request({"url": "file:///etc/passwd", "insecure_http": True}, "GET", "", token="secret")
    with pytest.raises(tlsboot.TlsBootError):
        tlsboot.fetch_ca("https://user:password@board.lan")
