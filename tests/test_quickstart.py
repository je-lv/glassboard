"""`glassboard-admin quickstart`: a private loopback board, a first session and join codes in one command."""
import os
import re
import socket
import subprocess
import sys


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_quickstart_starts_a_board_with_join_codes_and_stops(tmp_path):
    d, port = tmp_path / "qs", free_port()
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    admin = [sys.executable, "-m", "glassboard.admin"]
    try:
        r = subprocess.run([*admin, "quickstart", "--dir", str(d), "--port", str(port), "--human", "Ana Dev",
                            "--agents", "claude,codex"], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stderr == ""                                            # setup chatter stays quiet
        assert f"Glassboard is running at http://127.0.0.1:{port}/" in r.stdout
        assert "tokens/ana-dev" in r.stdout and "gbt_" not in r.stdout   # a path to the token, never the token
        codes = re.findall(rf"glassboard join http://127.0.0.1:{port} (\S+) -- (claude|codex)", r.stdout)
        assert [c for _, c in codes] == ["claude", "codex"]
        assert oct(os.stat(d / "tokens" / "ana-dev").st_mode & 0o777) == "0o600"
        work = tmp_path / "work"
        work.mkdir()
        j = subprocess.run([sys.executable, "-m", "glassboard.cli", "join", f"http://127.0.0.1:{port}", codes[0][0], "--here"],
                           env=env, cwd=work, capture_output=True, text=True, timeout=60)
        assert j.returncode == 0, j.stderr
        w = subprocess.run([sys.executable, "-m", "glassboard.cli", "whoami"], env=env, cwd=work,
                           capture_output=True, text=True, timeout=60)
        assert "ana-dev/claude" in w.stdout and "quickstart" in w.stdout
        again = subprocess.run([*admin, "quickstart", "--dir", str(d), "--port", str(port)], env=env,
                               capture_output=True, text=True, timeout=60)
        assert again.returncode == 0 and "Team > Add an agent" in again.stdout   # reuse: no new codes
    finally:
        s = subprocess.run([*admin, "quickstart", "--dir", str(d), "--stop"], env=env, capture_output=True, text=True)
    assert "stopped the quickstart board" in s.stdout
