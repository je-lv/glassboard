"""Protocol doc, AGENTS.md/CLAUDE.md snippets, the Glassboard Claude Skill. The acceptance test
("fresh Claude Code and Codex sessions onboard using only the docs") needs live agents (run by hand); these
tests keep the docs true to the code."""
import pathlib
import re
import shlex

import pytest

from glassboard.cli import build_parser
from glassboard.protocol import PROTOCOL_TEXT

ROOT = pathlib.Path(__file__).parents[1]
DOCS = {
    "onboarding": ROOT / "docs" / "onboarding.md",
    "claude": ROOT / "docs" / "snippets" / "CLAUDE.md",
    "agents": ROOT / "docs" / "snippets" / "AGENTS.md",
    "skill": ROOT / "integrations" / "claude-skill" / "glassboard" / "SKILL.md",
}


def test_published_protocol_is_the_served_protocol():
    assert (ROOT / "docs" / "protocol.md").read_text() == PROTOCOL_TEXT


def commands(text: str) -> list[str]:
    """Every `glassboard …` command in backticks or code blocks."""
    found = re.findall(r"`(glassboard [^`]+)`", text)
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S):
        found += [ln.split("#")[0].strip() for ln in block.splitlines() if ln.strip().startswith("glassboard ")]
    return [c for c in found if c]


@pytest.mark.parametrize("name", list(DOCS))
def test_every_documented_command_parses(name):
    parser = build_parser()
    cmds = commands(DOCS[name].read_text())
    assert cmds, name
    for c in cmds:
        c = re.sub(r"\s*\[[^\]]*\]", "", c)                     # optional parts in [brackets]
        c = c.replace("…", "x").replace("<", "").replace(">", "")
        argv = shlex.split(c)[1:]
        if argv[:1] == ["run"]:
            continue                                              # `run -- <client>` execs the client
        if argv[-1:] == ["--"] or argv[:1] == ["--version"]:
            continue
        try:
            parser.parse_args(argv)
        except SystemExit as e:
            raise AssertionError(f"{name}: `{c}` does not parse") from e


def test_onboarding_states_what_the_board_cannot_guarantee():
    t = DOCS["onboarding"].read_text()
    # the board's guarantees and their limits, stated verbatim in the onboarding doc
    assert ("It prevents conflicting *board* state. It cannot stop two agents doing the same work outside the\n"
            "board, cannot make a model obey quoted-content conventions, and \"last seen\" proves connectivity,\nnot work.") in t
    assert "Self-hosting the board does not make model processing local" in t


@pytest.mark.parametrize("name", ["onboarding", "agents"])
def test_codex_setup_follows_e7(name):
    t = DOCS[name].read_text()
    assert 'sandbox_mode = "workspace-write"' in t and "network_access = true" in t


@pytest.mark.parametrize("name", list(DOCS))
def test_docs_carry_the_e6_rules_and_no_old_names(name):
    t = DOCS[name].read_text()
    assert "agentboard" not in t.lower() and "agent board" not in t.lower()
    if name != "onboarding":
        for phrase in ("never", "task=T#", "approval prompts", "proposal", "recovery_mismatch", "fence_mismatch"):
            assert phrase in t, (name, phrase)


@pytest.mark.parametrize("name", list(DOCS))
def test_docs_stay_general_purpose(name):
    """No domain-specific vocabulary or examples."""
    t = DOCS[name].read_text().lower()
    for word in ("pentest", "penetration", "exploit", "vulnerab", " cve", "red team", "payload", "attack"):
        assert word not in t, (name, word)


def test_skill_has_valid_frontmatter():
    t = DOCS["skill"].read_text()
    m = re.match(r"^---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n", t)
    assert m and m.group(1) == "glassboard" and 40 < len(m.group(2)) < 1024
