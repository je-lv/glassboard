"""Release readiness: the README-linked demo runs, every documented command parses, every
relative link resolves, every configuration variable is documented, package metadata is consistent,
and the deploy files pass the real syntax checkers (scripts/deploy-check.sh)."""
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib

import pytest

import glassboard
from glassboard.cli import build_parser

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = pathlib.Path(sys.executable).parent
GUIDES = ["README.md", "docs/how-it-works.md", "docs/usage.md", "docs/deploy.md", "docs/onboarding.md",
          "docs/rehearsal.md"]
ADMIN_COMMANDS = {"init", "add-human", "add-agent", "mint-token", "revoke-token", "deactivate", "revoke-admin",
                  "revoke-web-sessions", "set-admin-password", "backup", "restore", "setup-tls"}


def port_free(port: int) -> bool:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)       # like uvicorn: TIME_WAIT is not "busy"
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.skipif(shutil.which("curl") is None, reason="the demo uses curl")
def test_documented_coordination_demo_runs(tmp_path):
    if not port_free(8000):
        pytest.skip("port 8000 (used by the demo) is busy")
    assert "examples/coordination-demo.sh" in (ROOT / "README.md").read_text()
    env = {**os.environ, "PATH": f"{BIN}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "GLASSBOARD_DEMO_DIR": str(tmp_path / "demo")}
    for k in ("GLASSBOARD_DB", "GLASSBOARD_MCP", "GLASSBOARD_INSECURE_HTTP", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(ROOT / "examples" / "coordination-demo.sh")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-2000:])
    out = r.stdout
    q = re.search(r"#(\d+) \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z alice -> alice/helper \[question\]", out).group(1)
    assert f"[answer] re:#{q}" in out                                     # the answer replies to the question
    assert "[task.accepted] T1" in out and "  | Two of three answers drafted." in out
    assert "Traceback" not in r.stderr


def documented_commands(text: str) -> list[tuple[str, bool]]:
    """(command, complete). Code-block lines are complete commands; inline `glassboard …` mentions
    in prose may be fragments ("`glassboard agent add`"), so only their subcommand is checked."""
    cmds = [(c, False) for c in re.findall(r"`((?:glassboard|glassboard-admin) [^`]+)`", text)]
    for lang, block in re.findall(r"```(\w*)\n(.*?)```", text, re.S):   # pair every fence (e.g. mermaid)
        if lang not in ("", "bash", "sh"):
            continue
        block = re.sub(r"\\\n\s*", " ", block)                          # line continuations
        for ln in block.splitlines():
            m = re.match(r"\s*(?:sudo\s+)?((?:glassboard|glassboard-admin) .*)$", ln)
            if m:
                cmds.append((m.group(1).split(" #")[0].strip(), True))
    return cmds


def _clean(c: str) -> list[str]:
    c = re.sub(r"\s*\[[^\]]*\]", "", c).replace("…", "x").replace("<", "").replace(">", "")
    return shlex.split(c)


@pytest.mark.parametrize("doc", GUIDES)
def test_every_documented_command_parses(doc):
    parser = build_parser()
    choices = next(a for a in parser._actions if a.dest == "cmd").choices
    cmds = documented_commands((ROOT / doc).read_text())
    assert cmds, doc
    for c, complete in cmds:
        try:
            argv = _clean(c)
        except ValueError:
            raise AssertionError(f"{doc}: cannot split `{c}`")
        words = [a for a in argv[1:] if not a.startswith("-")]
        if argv[0] == "glassboard-admin":
            values = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--db", "--journal")}
            sub = next((w for w in words if w not in values), None)
            assert sub is None or sub in ADMIN_COMMANDS or sub in ("x", "command"), f"{doc}: `{c}`"
            continue
        if not words or words[0] in ("run",) or "|" in c:
            continue
        assert words[0] in choices, f"{doc}: `{c}` names no glassboard command"
        if complete:
            try:
                parser.parse_args(argv[1:])
            except SystemExit as e:
                raise AssertionError(f"{doc}: `{c}` does not parse") from e


@pytest.mark.parametrize("doc", GUIDES + ["docs/docker.md", "docs/protocol.md", "integrations/claude-skill/README.md"])
def test_relative_links_resolve(doc):
    text = (ROOT / doc).read_text()
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        assert ((ROOT / doc).parent / target).exists(), f"{doc}: broken link {target}"


def test_every_configuration_variable_is_documented():
    used = set()
    for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "deploy").iterdir()):
        used |= set(re.findall(r"GLASSBOARD_[A-Z_]*[A-Z]", p.read_text()))
    deploy = (ROOT / "docs" / "deploy.md").read_text()
    missing = sorted(v for v in used if f"`{v}`" not in deploy)
    assert not missing, missing


def test_package_metadata_is_consistent():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert meta["version"] == glassboard.__version__ != "0.0.1.dev0"
    assert meta["license"] == "Apache-2.0" and (ROOT / "LICENSE").read_text().lstrip().startswith("Apache License")
    assert "jinja2" in " ".join(meta["optional-dependencies"]["server"])          # the dashboard needs it
    assert meta["dependencies"] == []                                              # the CLI is stdlib-only
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts) == {"glassboard", "glassboard-admin", "glassboard-rehearsal"}
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["glassboard"]
    for pattern, folder in (("sql/*.sql", "sql"), ("templates/*.html", "templates"), ("static/*.css", "static"),
                            ("static/*.js", "static")):
        assert pattern in data and any((ROOT / "src" / "glassboard" / folder).glob(pattern.split("/")[1]))


def test_deploy_files_pass_the_syntax_checkers():
    r = subprocess.run([str(ROOT / "scripts" / "deploy-check.sh")], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "CADDY": os.environ.get("CADDY", "")})
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout
    if r.stdout.count("PASS") == 0:
        pytest.skip("no checker available here: " + r.stdout)


def test_tls_harness_has_no_undeclared_bc_dependency():
    script = (ROOT / "scripts" / "e2e-verify.sh").read_text()
    assert not re.search(r"\bbc\b", script)
    assert "date +%s%3N" in script


@pytest.mark.skipif(shutil.which("curl") is None, reason="dev-board.sh uses curl")
def test_dev_board_launcher_starts_a_ready_board(tmp_path):
    """scripts/dev-board.sh: people, agents, tokens, the shared `dev` session, and join instructions."""
    import time as _t
    port = None
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # A different global uvicorn ahead of the Glassboard environment must not be selected. This
    # reproduces the failure where a global server could not import the project package.
    decoy = tmp_path / "decoy-bin"
    decoy.mkdir()
    (decoy / "uvicorn").write_text("#!/bin/sh\necho wrong-uvicorn >&2\nexit 97\n")
    (decoy / "uvicorn").chmod(0o755)
    env = {**os.environ, "PATH": f"{decoy}:{BIN}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    for k in ("GLASSBOARD_DB", "GLASSBOARD_MCP", "GLASSBOARD_INSECURE_HTTP", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        env.pop(k, None)
    proc = subprocess.Popen([str(ROOT / "scripts" / "dev-board.sh"), "--dir", str(tmp_path / "b"), "--port", str(port),
                             "alice:claude,codex", "bob:codex"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    out = ""
    try:
        deadline = _t.time() + 60
        while "Ctrl-C stops" not in out and _t.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            out += line
        assert "Ctrl-C stops" in out, out
        for f in ("alice", "bob", "alice-claude", "alice-codex", "bob-codex"):
            p = tmp_path / "b" / "tokens" / f
            assert p.exists() and oct(p.stat().st_mode & 0o777) == "0o600"
        assert f"glassboard login --url http://127.0.0.1:{port} --token-file" in out and "glassboard run -- codex" in out
        a = {**env, "XDG_CONFIG_HOME": str(tmp_path / "a"), "XDG_STATE_HOME": str(tmp_path / "a")}
        run = lambda *args: subprocess.run([str(BIN / "glassboard"), *args], env=a, capture_output=True, text=True)  # noqa: E731
        assert run("login", "--url", f"http://127.0.0.1:{port}", "--token-file", str(tmp_path / "b" / "tokens" / "bob-codex")).returncode == 0
        assert run("use", "dev").returncode == 0
        members = run("members", "list", "dev").stdout
        assert "@alice (human) · coordinator" in members and "@bob/codex (agent) · member" in members
    finally:
        import signal as _sig
        os.killpg(proc.pid, _sig.SIGTERM)
        proc.wait(timeout=10)
