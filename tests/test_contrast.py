"""UI/UX analysis 2026-11-05, phase 1 (findings F04, F05): the colour tokens meet WCAG contrast in both
themes. Text tokens need 4.5:1 and control borders 3:1 on every background they sit on; state text
also on its own 9% tint (how `.tag.st` renders)."""
import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).parents[1] / "src" / "glassboard" / "static" / "app.css").read_text(encoding="utf-8")
TEXT = ("fg", "fg-2", "muted", "accent-ink", "st-open", "st-progress", "st-blocked", "st-review", "st-done",
        "st-cancelled", "danger", "event-session", "event-member", "event-system")
STATES = ("st-open", "st-progress", "st-blocked", "st-review", "st-done", "st-cancelled", "danger")
BACKGROUNDS = ("surface", "bg", "surface-2")


def _tokens(text: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", text))


def themes() -> dict:
    start = CSS.index(":root {")
    light = _tokens(CSS[start:CSS.index("\n}", start)])
    d = CSS.index("@media (prefers-color-scheme: dark)")
    dark = dict(light, **_tokens(CSS[d:CSS.index("\n  }", d)]))
    return {"light": light, "dark": dark}


def _rgb(h):
    return [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]


def _lum(rgb):
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def tint(color, base, amount=0.09):
    return [c * amount + b * (1 - amount) for c, b in zip(_rgb(color), _rgb(base))]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_reach_4_5_on_every_background(theme):
    t = themes()[theme]
    bad = [f"--{k} on --{bg}: {ratio(_rgb(t[k]), _rgb(t[bg])):.2f}" for k in TEXT for bg in BACKGROUNDS
           if ratio(_rgb(t[k]), _rgb(t[bg])) < 4.5]
    assert bad == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_state_text_on_its_own_tint_reaches_4_5(theme):
    t = themes()[theme]
    bad = [k for k in STATES if ratio(_rgb(t[k]), tint(t[k], t["surface"])) < 4.5]
    assert bad == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_control_border_reaches_3_on_every_background(theme):
    t = themes()[theme]
    assert all(ratio(_rgb(t["control-border"]), _rgb(t[bg])) >= 3 for bg in BACKGROUNDS)


def test_controls_use_the_control_border_and_small_teal_text_uses_the_ink():
    assert "input, select, textarea { width: 100%; padding: .5rem .5rem; border: 1px solid var(--control-border);" in CSS
    assert "button.secondary, button.small, header button { background: var(--surface); color: var(--fg); " \
           "border-color: var(--control-border); }" in CSS
    teal_text = [ln for ln in CSS.splitlines() if re.search(r"(?<![-\w])color: var\(--accent\)", ln)]
    assert all(ln.startswith((".brand-name-accent", ".button.primary")) for ln in teal_text), teal_text
