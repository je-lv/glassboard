"""B36 (owner rule 2026-11-04, ERRATA E32): API, CLI and dashboard parity, and the agent reference."""
import json
import os
import pathlib
import sys

import pytest

from test_cli import L, ok  # noqa: F401  (fixture)
from test_dashboard import login

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import parity  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    os.environ.setdefault("GLASSBOARD_SWEEP_INTERVAL_S", "0")
    return parity.check(str(tmp_path_factory.mktemp("parity")))


def test_b36_every_api_route_has_cli_dashboard_or_a_recorded_reason(result):
    assert result["problems"] == [], "\n".join(result["problems"])
    assert len(parity.POLICY) >= 74


def test_b36_generated_matrix_and_reference_are_current(result):
    for key, text in parity.generated(result).items():
        assert parity.TARGETS[key].read_text(encoding="utf-8") == text, \
            f"{parity.TARGETS[key]} is stale: run python tools/parity.py --write"


def test_b36_reference_covers_every_route_and_cli_command(result):
    ref = parity.TARGETS["reference"].read_text(encoding="utf-8")
    for method, path in parity.POLICY:
        assert f"### {method} {path}" in ref
    for name in ("sync", "export", "invite", "reference", "task", "checkpoint", "wake"):
        assert f"`glassboard {name}" in ref or f"glassboard {name} " in ref


def test_b36_cli_prints_the_reference_and_the_dashboard_serves_it(L, gb):  # noqa: F811
    printed = ok(L.cli("jose", "reference"))
    assert printed == (ROOT / "src" / "glassboard" / "agent_reference.md").read_text(encoding="utf-8")
    page = gb.client.get("/docs/agent-reference")
    assert page.status_code == 200 and page.text == printed
    assert page.headers["content-type"].startswith("text/plain")
    assert 'href="/docs/agent-reference"' in gb.client.get("/docs").text


def test_b36_sync_include_checkpoint_from_the_cli(L, gb):  # noqa: F811
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))
    ok(L.cli("jose/claude", "checkpoint", "save", "--summary", "Halfway through T1", "--next", "Write the tests"))
    out = ok(L.cli("jose/claude", "sync", "--include-checkpoint"))
    assert "Halfway through T1" in out and "Write the tests" in out


def test_b36_export_and_invite_from_the_cli(L, gb, tmp_path):  # noqa: F811
    ok(L.cli("jose", "use", "s1"))
    ok(L.cli("jose", "post", "note", "Exported line"))
    assert "Exported line" in ok(L.cli("jose", "export"))
    out_file = tmp_path / "s1.jsonl"
    ok(L.cli("jose", "export", "--format", "jsonl", "--out", str(out_file)))
    first = json.loads(out_file.read_text().splitlines()[0])
    assert first["session"] == "s1"
    out = ok(L.cli("jose", "invite", "helper"))
    assert "@jose/helper invited to s1" in out and "one-time code (shown once):" in out
    assert "glassboard join http" in out


def test_b36_agent_view_shows_the_latest_checkpoint(L, gb):  # noqa: F811
    ok(L.cli("jose/claude", "use", "s1"))
    ok(L.cli("jose/claude", "sync"))
    ok(L.cli("jose/claude", "checkpoint", "save", "--summary", "Parsed the inputs", "--next", "Render the table"))
    page = login(gb).get("/s/s1/agent?as=jose/claude").text
    assert "Latest checkpoint" in page and "Parsed the inputs" in page and "1 saved" in page
    other = login(gb, "ana").get("/s/s1/agent?as=jose/claude").text
    assert "Only Session Managers read other members" in other and "Parsed the inputs" not in other
