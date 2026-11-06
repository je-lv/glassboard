"""Actor badges for the dashboard (B35, owner fix F4): one helper for every view.

A badge shows who acted: humans get "H"; agents whose name contains a known client family
(claude, codex, grok, glm) get that family's mark and color, so `tlachia-claude` reads as a Claude
agent; other agents get "A" and a stable color from a fixed palette, never the muted grey that
blended into the timeline."""
from __future__ import annotations

import hashlib
import re

FAMILIES = {"claude": ("Cl", "Claude"), "codex": ("C", "Codex"), "grok": ("G", "Grok"), "glm": ("GL", "GLM")}
PALETTE = ("rose", "gold", "teal", "lime", "coral", "indigo")


def family(leaf: str) -> str | None:
    """The client family named in an agent name, by whole token first, then by substring."""
    tokens = re.split(r"[-_.]", leaf.lower())
    for name in FAMILIES:
        if name in tokens:
            return name
    for name in FAMILIES:
        if name in leaf.lower():
            return name
    return None


RUN_TITLE = "One run of this agent's client process (its runtime label), not a member or a mention."


def run_label(handle: str, runtime: str | None) -> str | None:
    """Owner request 2026-11-05: a runtime reads as "grok · run 53xp", never as "@53xp"."""
    if not runtime:
        return None
    leaf = handle.rsplit("/", 1)[-1]
    return f"{family(leaf) or leaf} · run {runtime}"


def badge(handle: str) -> tuple[str, str, str]:
    """(mark, name, style) for a principal handle."""
    if "/" not in handle:
        return "H", handle, "human"
    leaf = handle.rsplit("/", 1)[-1]
    fam = family(leaf)
    if fam:
        mark, label = FAMILIES[fam]
        return mark, (label if leaf == fam else leaf), fam
    digest = hashlib.sha256(handle.encode("utf-8")).digest()
    return "A", leaf, PALETTE[digest[0] % len(PALETTE)]
