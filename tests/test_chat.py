"""B58 (ERRATA E46): the Chat tab. A read-only view over the one session log for humans who watch the
agents talk and step in: messages oldest first, task and system events folded into markers, replies
under what they answer, a composer that posts through the existing dashboard Post form."""
import re

import pytest

from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_dashboard import csrf_of, login
from test_tasks import api, claim
from test_topic_channels import channel, post


def msg(gb, who, kind, body, **extra):
    r = api(gb, "POST", "/messages", who, {"kind": kind, "body": body, **extra})
    assert r.status_code == 201, r.text
    return r.json()["seq"]


def chat(c, query=""):
    r = c.get("/s/s1/chat" + query)
    assert r.status_code == 200, r.text
    return r.text


def stream_part(html):
    return html.split('id="chat-stream"', 1)[1].split("</section>", 1)[0]


def message(html, seq):
    m = re.search(rf'<li class="ev chat-msg[^"]*" id="m{seq}".*?</li>', html, re.S)
    assert m, seq
    return m.group(0)


@pytest.mark.parametrize("kind", ["note", "question", "answer", "update", "proposal", "blocker", "approval"])
def test_b58_each_message_kind_renders_with_its_kind_label_and_author(b, kind):
    extra = {"to": ["jose"]} if kind == "approval" else {}
    seq = msg(b, "jose/claude", kind, f"A {kind} body", **extra)
    li = message(chat(login(b)), seq)
    assert f'data-event="{kind}"' in li and f'<span class="ev-kind">{kind}</span>' in li
    assert 'class="actor-badge actor-claude"' in li and f"A {kind} body" in li
    assert f"#{seq} · " in li


def test_b58_decision_renders_and_only_human_coordinators_get_the_kind(b):
    seq = msg(b, "jose", "decision", "We ship on Friday")
    html = chat(login(b))
    assert '<span class="ev-kind">decision</span>' in message(html, seq)
    assert "<option>decision</option>" in html                         # jose: human Session Manager
    assert "<option>decision</option>" not in chat(login(b, "ana"))     # ana: human Contributor


def test_b58_messages_oldest_first_and_task_events_fold_into_markers(b):
    first = msg(b, "jose/claude", "note", "First")
    n = mk(b)
    claim(b, n)
    last = msg(b, "ana", "note", "Last")
    html = chat(login(b))
    part = stream_part(html)
    between = part[part.index(f'id="m{first}"'):part.index(f'id="m{last}"')]
    assert part.index('class="chat-marker"') < part.index(f'id="m{first}"')      # session set-up before it
    marker = re.search(r'<li class="chat-marker">.*?</li>', between, re.S).group(0)
    assert "2 task or system events" in marker and f"T{n} task.created by @jose" in marker
    assert f"T{n} task.claimed by @jose/claude" in marker
    assert re.search(r'href="/s/s1\?before=\d+#e\d+"', marker)                # opens the Timeline there
    assert "task.created" not in chat(login(b), "?events=0").split('id="chat-stream"', 1)[1].split("</section>")[0]
    assert 'id="chat-end"' in part


def test_b58_replies_sit_under_their_message_with_a_quote(b):
    q = msg(b, "jose/claude", "question", "Which port?\nDetails follow", to=["jose"])
    html = chat(login(b))
    assert 'class="ev-state ev-owed">you owe an answer' in message(html, q)
    a = msg(b, "jose", "answer", "Use 9090.", re=q, to=["jose/claude"])
    html = chat(login(b))
    li = message(html, a)
    assert "chat-reply" in li.split(">", 1)[0]
    assert f'href="#m{q}"' in li and "reply to @jose/claude" in li and "«Which port?»" in li
    assert 'class="ev-state ev-answered">answered' in message(html, q)


def test_b58_reply_and_ask_links_set_the_reply_target(b):
    q = msg(b, "jose/claude", "question", "Ready?", to=["jose"])
    c = login(b)
    li = message(chat(c), q)
    assert f'href="/s/s1/chat?reply={q}#composer" data-chat-reply' in li
    assert f'href="/s/s1/chat?ask={q}#composer" data-chat-ask' in li
    html = chat(c, f"?reply={q}")
    assert f'name="re" value="{q}" data-chat-re' in html and f"@jose/claude #{q}" in html
    assert "<option selected>answer</option>" in html and 'value="jose/claude" placeholder' in html
    html = chat(c, f"?ask={q}")
    assert "<option selected>question</option>" in html and f'name="re" value="{q}"' in html
    html = chat(c)
    assert 'name="re" value="" data-chat-re' in html and "data-chat-chip hidden" in html


def test_b58_composer_posts_through_the_post_form_and_returns_to_chat(b):
    c = login(b)
    html = chat(c)
    form = html.split('class="chat-composer"', 1)[1].split("</form>", 1)[0]
    for field in ('name="csrf"', 'name="recovery"', 'name="op"', 'name="return_to" value="chat"',
                  'formaction="/s/s1/preflight"', 'list="chat-members"'):
        assert field in form, field
    assert '<option value="jose/claude">@jose/claude</option>' in html
    data = {"csrf": csrf_of(html), "recovery": b.rec(), "op": "chat-op-0001", "return_to": "chat",
            "kind": "note", "to": "@jose/claude", "body": "From the chat"}
    r = c.post("/s/s1/post", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/s/s1/chat?ok=posted#chat-end"
    ev = b.events()[-1]
    assert ev["type"] == "note" and ev["body"] == "From the chat" and b.recipients("s1", ev["seq"]) == ["jose/claude"]
    r = c.post("/s/s1/post", data={**data, "op": "chat-op-0002", "to": "nobody-here"})
    assert r.status_code == 303 and r.headers["location"] == "/s/s1/chat?err=recipient_not_member"
    r = c.post("/s/s1/post", data={**data, "op": "chat-op-0003", "csrf": "wrong"})
    assert r.status_code == 403


def test_b58_channel_filter_keeps_owed_questions_in_view(b):
    channel(b, members=("jose", "jose/claude", "ana/codex"))
    inside = post(b, "jose/claude", "Header spacing", channel="design").json()["seq"]
    outside = msg(b, "ana/codex", "note", "Unrelated")
    owed = msg(b, "ana/codex", "question", "Which licence?", to=["jose"])
    c = login(b)
    html = chat(c, "?channel=design")
    part = stream_part(html)
    assert f'id="m{inside}"' in part and f'id="m{outside}"' not in part
    assert f'id="m{owed}"' in part and "Still open, from the whole session" in message(html, owed)
    assert "chat-marker" not in part
    assert 'class="chip on" href="/s/s1/chat?channel=design"' in html
    assert '<option value="design" selected>#design</option>' in html
    assert '<input type="hidden" name="return_channel" value="design">' in html
    r = c.post("/s/s1/post", data={"csrf": csrf_of(html), "recovery": b.rec(), "op": "chat-op-0004", "return_to": "chat",
                                   "return_channel": "design", "kind": "note", "channel": "design", "body": "In the channel"})
    assert r.headers["location"] == "/s/s1/chat?channel=design&ok=posted#chat-end"
    assert b.events()[-1]["channel"] == "design"
    msg(b, "jose", "answer", "MIT", re=owed, to=["ana/codex"])
    assert f'id="m{owed}"' not in stream_part(chat(c, "?channel=design"))   # answered: no longer pinned
    assert f'id="m{inside}"' in stream_part(chat(c, "?channel=nope"))         # unknown channel: whole session


def test_b58_no_raw_hidden_characters_reach_the_html(b):
    q = msg(b, "jose/claude", "question", "ok\U000e0041\u202eevil", to=["jose"])
    msg(b, "jose", "answer", "fine\u2066x", re=q, to=["jose/claude"])
    c = login(b)
    for html in (chat(c), c.get("/s/s1/chat/stream").text):
        assert "⟨U+E0041⟩⟨U+202E⟩" in html and "⟨U+2066⟩" in html
        assert not re.search("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]", html)


def test_b58_viewers_see_the_stream_without_a_composer(b):
    b.add("s1", "ana", role="observer")
    msg(b, "jose/claude", "note", "Visible to viewers")
    html = chat(login(b, "ana"))
    assert "Visible to viewers" in html
    assert 'class="chat-composer"' not in html and "data-chat-reply" not in html
    assert "You can read this conversation" in html


def test_b58_non_members_get_404(b):
    assert b.admin("add-human", "bob")[0] == 0
    b.tok["bob"] = b.mint("bob")
    c = login(b, "bob")
    assert c.get("/s/s1/chat").status_code == 404
    assert c.get("/s/s1/chat/stream").status_code == 404


def test_b58_stream_fragment_is_marked_for_the_refresh(b):
    msg(b, "jose/claude", "note", "Live")
    r = login(b).get("/s/s1/chat/stream")
    assert r.status_code == 200 and r.headers["x-glassboard-fragment"] == "chat"
    assert "Live" in r.text and "<script" not in r.text and "<form" not in r.text


def test_b58_quick_actions_stand_down_only_for_those_allowed(b):
    seq = msg(b, "ana/codex", "note", "Working on it")
    li = message(chat(login(b)), seq)                                   # jose: human Session Manager
    assert 'href="/s/s1/agent?as=ana/codex#standdown"' in li and "Ask @" in li
    b.add("s1", "ana", role="member")
    own = msg(b, "jose/claude", "note", "Mine")
    html = chat(login(b, "ana"))                                        # ana: Contributor, owns ana/codex only
    assert 'href="/s/s1/agent?as=ana/codex#standdown"' in message(html, seq)
    assert "#standdown" not in message(html, own)
    assert 'id="standdown"' in login(b).get("/s/s1/agent?as=ana/codex").text


def test_b58_chat_tab_is_second_with_its_number_key(b):
    html = chat(login(b))
    tabs = re.findall(r'<a class="tab[^"]*" href="/s/s1([^"]*)" data-key="(\d)"', html)
    assert tabs[:3] == [("/now", "1"), ("/chat", "2"), ("", "3")]
    assert 'aria-current="page"><span class="tab-key" aria-hidden="true">2</span>Chat' in html
    items = login(b).get("/s/s1/jump.json").json()["items"]
    assert {"kind": "view", "label": "Chat", "href": "/s/s1/chat"} in items
