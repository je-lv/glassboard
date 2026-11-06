"""UI/UX analysis 2026-11-05, phase 2 (finding F03): buttons are neutral by default and each screen shows at
most one visible primary action. Primaries inside a closed dialog, a hidden panel or a closed <details>
belong to the screen that opens there, so they are not counted on the page behind it."""
from html.parser import HTMLParser

import pytest

from test_admin_console import _owner
from test_artifacts import upload
from test_board import mk
from test_dashboard import login
from test_tasks import api, claim

CONTAINERS = {"dialog", "details", "div", "section", "aside", "form", "nav", "main", "header", "ul", "li", "article"}


class Primaries(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.visible = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        concealed = any(c for _, c in self.stack)
        if "primary" in classes and tag in ("button", "a") and not concealed:
            self.visible.append(a.get("class"))
        if tag in CONTAINERS:
            hidden = ("hidden" in a or (tag == "dialog" and "open" not in a)
                      or (tag == "details" and "open" not in a))
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        if tag in CONTAINERS:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break


def visible_primaries(html: str) -> list:
    parser = Primaries()
    parser.feed(html)
    return parser.visible


def test_buttons_are_neutral_unless_marked_primary():
    import pathlib
    css = (pathlib.Path(__file__).parents[1] / "src/glassboard/static/app.css").read_text(encoding="utf-8")
    rule = css[css.index("\nbutton {"):css.index("}", css.index("\nbutton {"))]
    assert "background: var(--surface)" in rule and "var(--accent)" not in rule and "margin-top" not in rule
    assert "button.primary { border-color: var(--accent); background: var(--accent);" in css


@pytest.fixture
def seeded(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    n = mk(gb, checklist=["One"])
    claim(gb, n)
    api(gb, "POST", "/messages", "ana", {"kind": "question", "body": "Which format?", "to": ["jose"]})
    api(gb, "POST", "/messages", "jose/claude", {"kind": "approval", "body": "May I publish?", "to": ["jose"]})
    upload(gb, "jose/claude", b"final\n", name="FINAL.txt", purpose="deliverable")
    upload(gb, "jose/claude", b"log\n", name="run.log")
    return gb, n


def test_every_dashboard_page_shows_at_most_one_primary(seeded):
    gb, n = seeded
    c = login(gb)
    pages = ["/", "/agents", "/s/s1", "/s/s1/board", f"/s/s1/t/{n}", "/s/s1/conversations", "/s/s1/artifacts",
             "/s/s1/progress", "/s/s1/team", "/s/s1/new-task", "/s/s1/agent?as=jose/claude", "/docs"]
    checked = 0
    for path in pages:
        r = c.get(path)
        if r.status_code == 404:
            continue
        assert r.status_code == 200, path
        assert len(visible_primaries(r.text)) <= 1, (path, visible_primaries(r.text))
        checked += 1
    assert checked >= 9
    owner = _owner(gb)
    for tab in ("workspaces", "access", "credentials", "roles", "settings", "operations"):
        html = owner.get(f"/admin?tab={tab}").text
        assert len(visible_primaries(html)) <= 1, (tab, visible_primaries(html))
    for path in ("/login", "/admin/login"):
        assert len(visible_primaries(gb.client.get(path).text)) == 1, path
