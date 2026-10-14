"""Recovery drills D1-D8, scripted in scripts/drills/drills.py.

The script runs each drill on its own throwaway local board, with the real CLI as the agents.
D5 (staleness) needs the 300 s minimum threshold, so it runs only with GLASSBOARD_SLOW=1.
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "drills", "drills.py")


def run(*args, timeout):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    total = re.search(r"drills: (\d+)/(\d+) checks passed", r.stdout)
    return r, total


def test_drills_d1_to_d8_except_d5():
    r, total = run(timeout=600)
    fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
    assert r.returncode == 0 and not fails, (fails, r.stderr[-2000:])
    assert total and total.group(1) == total.group(2) and int(total.group(2)) >= 45
    for d in ("D1", "D2", "D3", "D4", "D6", "D7", "D7B", "D8"):
        assert f"PASS {d}:" in r.stdout


@pytest.mark.skipif(os.environ.get("GLASSBOARD_SLOW") != "1", reason="D5 takes about 6 minutes (GLASSBOARD_SLOW=1)")
def test_drill_d5_staleness():
    r, total = run("D5", timeout=900)
    assert r.returncode == 0, r.stdout[-2000:]


def test_unknown_drill_is_a_usage_error():
    r, _ = run("D99", timeout=60)
    assert r.returncode == 2 and "unknown drill" in r.stderr
