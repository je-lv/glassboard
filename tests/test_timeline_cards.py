"""B35 (owner fixes F3, F4): timeline events as readable cards, and actor badges that stand out."""
import pytest

from glassboard import actors
from glassboard.dashboard import _paragraphs
from test_dashboard import d, login  # noqa: F401  (fixture)


@pytest.mark.parametrize("handle, mark, name, style", [
    ("jose", "H", "jose", "human"),
    ("jose/claude", "Cl", "Claude", "claude"),
    ("operator/tlachia-claude", "Cl", "tlachia-claude", "claude"),
    ("operator/circuitlab-r2-codex", "C", "circuitlab-r2-codex", "codex"),
    ("jelv/glm", "GL", "GLM", "glm"),
])
def test_b35_badge_family_from_the_agent_name(handle, mark, name, style):
    assert actors.badge(handle) == (mark, name, style)


def test_b35_generic_agents_get_a_stable_palette_color_not_grey():
    mark, name, style = actors.badge("operator/lead")
    assert mark == "A" and name == "lead" and style in actors.PALETTE
    assert actors.badge("operator/lead") == (mark, name, style)


def test_b35_paragraphs_reflow_prose_and_keep_structure():
    block = ["  | First line of a hard", "  | wrapped sentence.", "  |", "  | - item one", "  | - item two",
             "  |", "  | ```", "  |   code  kept", "  | ```"]
    paras = _paragraphs(block)
    assert [[s["text"] for s in p] for p in paras] == [
        ["First line of a hard wrapped sentence."], ["- item one", "- item two"], ["```", "  code  kept", "```"]]
    assert paras[2][1]["pre"] is True


def test_b35_question_answer_cards(d):  # noqa: F811
    q = d.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
              json={"kind": "question", "body": "Which port?\n\nDefault if unanswered: 8080.", "to": ["jose"]},
              rec=d.rec()).json()["seq"]
    open_q = login(d).get("/s/s1/timeline").text
    assert '<span class="ev-state ev-owed">awaiting answer</span>' in open_q
    assert d.admin("add-human", "bob")[0] == 0
    d.add("s1", "bob")
    d.req("POST", "/api/v1/sessions/s1/messages", "jose",
          json={"kind": "answer", "body": "Use 9090.", "re": q, "to": ["jose/claude", "ana", "bob"]},
          rec=d.rec())
    html = login(d).get("/s/s1/timeline").text
    question = html.split('data-event="question"', 1)[1].split('<div class="ev"', 1)[0]
    assert '<span class="ev-state ev-answered">answered</span>' in question
    assert "<p><span class=\"seg\">Which port?</span></p><p><span class=\"seg\">Default if unanswered: 8080.</span></p>" in question
    answer = html.split('data-event="answer"', 1)[1]
    assert f'thread={q}#message-{q}">re #{q}</a>' in answer and "«Which port?»" in answer
    assert "<summary>3 recipients</summary>" in answer
    assert "  | " not in answer.split("</div>", 3)[-1]          # no doubled line markers in the body


def test_b35_hidden_characters_stay_visible_in_cards(d):  # noqa: F811
    d.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
          json={"kind": "note", "body": "ok\U000e0041\u202e"}, rec=d.rec())
    html = login(d).get("/s/s1/timeline").text
    assert "⟨U+E0041⟩⟨U+202E⟩" in html and "\U000e0041" not in html and "\u202e" not in html
