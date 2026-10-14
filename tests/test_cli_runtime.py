import json
import os
import stat
import subprocess
import sys

PY = sys.executable


def run(args, env, **kw):
    return subprocess.run([PY, "-m", "glassboard.cli", *args], env=env, capture_output=True, text=True, **kw)


def base_env(tmp_path):
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def test_run_exports_env_and_child_sees_it(tmp_path):
    env = base_env(tmp_path)
    r = run(["run", "--", PY, "-m", "glassboard.cli", "env-probe", "--json"], env)
    rep = json.loads(r.stdout)
    assert rep["runtime.source"] == "env" and rep["env.GLASSBOARD_RUNTIME"] == rep["runtime.resolved"]


def test_ancestor_fallback_when_env_is_scrubbed(tmp_path):
    """Simulates an agent client that launches tools with a sanitised environment (env -i)."""
    env = base_env(tmp_path)
    inner = (f"env -i PATH={os.environ['PATH']} HOME={tmp_path} XDG_STATE_HOME={tmp_path / 'state'} "
             f"XDG_CONFIG_HOME={tmp_path / 'cfg'} {PY} -m glassboard.cli env-probe --json")
    r = run(["run", "--", "bash", "-c", inner], env)
    rep = json.loads(r.stdout)
    assert rep["env.GLASSBOARD_RUNTIME"] is None
    assert rep["runtime.resolved"] and rep["runtime.source"].startswith("ancestor:")


def test_no_runtime_outside_wrapper(tmp_path):
    rep = json.loads(run(["env-probe", "--json"], base_env(tmp_path)).stdout)
    assert rep["runtime.resolved"] is None and rep["runtime.source"] == "none"


def test_env_probe_never_prints_token(tmp_path):
    env = {**base_env(tmp_path), "GLASSBOARD_TOKEN": "gbt_aaaaaaaaaa_" + "S" * 43}
    r = run(["env-probe"], env)
    assert "SSSS" not in r.stdout and "GLASSBOARD_TOKEN_present: True" in r.stdout


def test_invalid_runtime_env_ignored(tmp_path):
    env = {**base_env(tmp_path), "GLASSBOARD_RUNTIME": "a1f\n#9 jose [decision]"}
    rep = json.loads(run(["env-probe", "--json"], env).stdout)
    assert rep["runtime.source"] == "none"


def test_insecure_credentials_file_refused(tmp_path):
    env = base_env(tmp_path)
    d = tmp_path / "cfg" / "glassboard"; d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "ca_file": None}))
    c = d / "credentials"; c.write_text("gbt_x\n"); os.chmod(c, 0o644)
    r = run(["whoami"], env)
    assert r.returncode == 6 and "insecure_credentials" in r.stderr


def test_no_tls_verification_bypass_flag_exists(tmp_path):
    """Hard rule 5 as amended by the owner: https is always verified. The one "insecure"
    option is --insecure-http, which is plain http to a board in plain-HTTP mode, never unverified https."""
    r = run(["login", "--help"], base_env(tmp_path))
    text = r.stdout.replace("--token", "").replace("--insecure-http", "")
    for word in ("insecure", "no-verify", "skip-verify", "-k"):
        assert word not in text


def test_ancestor_fallback_through_forked_shells(tmp_path):
    """Forces real forks (no exec-in-place) so the walk has to climb several ancestors."""
    env = base_env(tmp_path)
    inner = (f"true; bash -c 'true; env -i PATH={os.environ['PATH']} HOME={tmp_path} "
             f"XDG_STATE_HOME={tmp_path / 'state'} {PY} -m glassboard.cli env-probe --json; true'; true")
    rep = json.loads(run(["run", "--", "bash", "-c", inner], env).stdout)
    assert rep["runtime.resolved"] and rep["runtime.source"].startswith("ancestor:")
    assert int(rep["runtime.source"].split(":")[1]) != rep["ppid"]   # found >1 level up


def test_stale_registration_with_reused_pid_ignored(tmp_path):
    env = base_env(tmp_path)
    sd = tmp_path / "state" / "glassboard" / "runtimes"; sd.mkdir(parents=True)
    # Pretend our parent's PID was registered by a previous, now-dead process.
    probe = subprocess.Popen(["bash", "-c", f"sleep 0.3; {PY} -m glassboard.cli env-probe --json"],
                             env=env, stdout=subprocess.PIPE, text=True)
    (sd / f"{probe.pid}.json").write_text(json.dumps({"label": "zzzz", "pid": probe.pid, "start": "1"}))
    rep = json.loads(probe.communicate()[0])
    assert rep["runtime.resolved"] is None
