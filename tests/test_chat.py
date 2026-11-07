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
    # B67: a channel view shows only its own messages; open requests from elsewhere fold into one line that names
    # the ones the viewer owes
    assert f'id="m{owed}"' not in part
    line = part.split('class="chat-elsewhere"', 1)[1].split("</p>", 1)[0]
    assert "1 open request elsewhere" in line and f"you owe: " in line and f"#m{owed}" in line and "«Which licence?»" in line
    assert "chat-marker" not in part
    assert 'class="chip on" href="/s/s1/chat?channel=design"' in html
    assert '<option value="design" selected>#design</option>' in html
    assert '<input type="hidden" name="return_channel" value="design">' in html
    r = c.post("/s/s1/post", data={"csrf": csrf_of(html), "recovery": b.rec(), "op": "chat-op-0004", "return_to": "chat",
                                   "return_channel": "design", "kind": "note", "channel": "design", "body": "In the channel"})
    assert r.headers["location"] == "/s/s1/chat?channel=design&ok=posted#chat-end"
    assert b.events()[-1]["channel"] == "design"
    msg(b, "jose", "answer", "MIT", re=owed, to=["ana/codex"])
    assert "chat-elsewhere" not in stream_part(chat(c, "?channel=design"))  # answered: nothing open elsewhere
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


def test_0_1_7_side_panel_lists_the_team_with_access_and_presence(b):
    html = chat(login(b))
    side = html.split('class="chat-side"', 1)[1]
    for handle in ("@jose", "@ana", "@jose/claude"):
        assert handle in side
    assert "Session Manager" in side and "Contributor" in side
    assert side.index("@ana") < side.index("@jose/claude")             # humans before agents
    assert 'data-chat-to-member="jose/claude"' in side and 'data-chat-to-member="jose"' not in side   # never yourself


def test_0_1_7_to_prefill_accepts_members_only(b):
    assert 'value="jose/claude" placeholder' in chat(login(b), "?to=jose/claude")
    assert 'value="nobody" placeholder' not in chat(login(b), "?to=nobody")
    assert 'value="jose" placeholder' not in chat(login(b), "?to=jose")        # not to yourself


def test_0_1_7_new_channel_from_chat_returns_to_chat(b):
    c = login(b)
    html = chat(c)
    assert "+ New channel" in html
    r = c.post("/s/s1/channels", data={"csrf": csrf_of(html), "op": "chat-newch-0001", "return_to": "chat",
                                       "name": "design", "topic": "Layout", "members": "jose/claude"})
    assert r.status_code == 303 and r.headers["location"].startswith("/s/s1/chat")   # with the success flash
    assert "#design" in chat(c)
    r = c.post("/s/s1/channels", data={"csrf": csrf_of(html), "op": "chat-newch-0002", "return_to": "chat",
                                       "name": "design", "members": ""})
    assert r.status_code == 303 and r.headers["location"].startswith("/s/s1/chat?err=")


def test_0_1_7_viewers_get_no_new_channel_form(b):
    b.add("s1", "ana", role="observer")
    assert "+ New channel" not in chat(login(b, "ana"))


def _list(html):
    return html.split("data-chat-channels-box>", 1)[1].split("</div>", 1)[0]


def test_b67_channel_list_new_channel_first_then_whole_session_then_channels(b):
    channel(b, members=("jose", "jose/claude", "ana/codex"))
    post(b, "jose/claude", "Header spacing", channel="design")
    c = login(b)
    html = chat(c, "?channel=design")
    side = html.split('class="chat-side"', 1)[1]
    assert side.index("+ New channel") < side.index("Whole session") < side.index("#</span>design")
    rows = _list(html)
    assert 'class="chat-ch on unread"' in rows and 'href="/s/s1/chat?channel=design" aria-current="page"' in rows
    assert 'title="1 unread">1</span>' in rows                         # from the viewer's own read position
    for m in ("follow", "mentions", "mute"):
        assert f'name="mode" value="{m}"' in rows
    assert '<input type="hidden" name="return_to" value="chat">' in rows
    # the list also rides inside the refresh fragment, so counts stay live
    part = c.get("/s/s1/chat/stream?channel=design").text
    assert "<template data-chat-channels-live>" in part and 'title="1 unread"' in part


def test_b67_own_posts_do_not_count_and_mode_returns_to_chat(b):
    channel(b, members=("jose", "jose/claude"))
    post(b, "jose", "Mine", channel="design")
    c = login(b)
    html = chat(c)
    assert "unread" not in _list(html).split("#</span>design", 1)[1].split("</li>", 1)[0]
    form = _list(html).split('action="/s/s1/channels/design/mode"', 1)[1].split("</form>", 1)[0]
    op = form.split('name="op" value="', 1)[1].split('"', 1)[0]
    r = c.post("/s/s1/channels/design/mode", data={"csrf": csrf_of(html), "op": op, "mode": "mute", "return_to": "chat",
                                                    "return_channel": "design"})
    assert r.status_code == 303 and r.headers["location"].startswith("/s/s1/chat?channel=design")
    assert 'class="chat-ch muted"' in _list(chat(c))


def test_b66_chat_is_a_fixed_frame_page(b):
    c = login(b)
    html = chat(c)
    assert 'class="page-wide page-chat"' in html or "page-wide page-chat" in html
    assert 'class="chat-bar"' in html and 'id="chat-stream"' in html
