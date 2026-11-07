"""ERRATA E44: task replay, the task's recorded events as a sequence between members."""
from glassboard import replay
from test_board import b, mk  # noqa: F401  (fixture and helpers)
from test_dashboard import login
from test_tasks import api, claim

SHA = "c" * 40


def test_replay_draws_routed_events_and_includes_replies(b):
    n = mk(b)
    fence = claim(b, n).json()["fence"]
    q = api(b, "POST", "/messages", "jose/claude", {"kind": "question", "body": "Which tone?", "to": ["jose"], "task": f"T{n}"}).json()["seq"]
    a = api(b, "POST", "/messages", "jose", {"kind": "answer", "body": "Plain and short", "re": q}).json()["seq"]   # no task
    api(b, "POST", f"/tasks/{n}/submit", "jose/claude", {"fence": fence, "summary": "Wrote it; see docs/faq.md", "commit": SHA})
    r = replay.replay(b.raw, b.raw.execute("SELECT id FROM session WHERE handle='s1'").fetchone()[0], n)
    seqs = [s["seq"] for s in r["steps"]]
    assert q in seqs and a in seqs                              # the answer joins through reply_to
    assert [l["handle"] for l in r["lanes"]][:2] == ["jose", "jose/claude"]
    claimed = next(s for s in r["steps"] if s["type"] == "task.claimed")
    assert claimed["self"] and not claimed["x_targets"]          # routed to nobody: a dot on its own lane
    asked = next(s for s in r["steps"] if s["seq"] == q)
    assert asked["to"] == ["jose"] and len(asked["x_targets"]) == 1 and asked["cat"] == "question"


def test_replay_page_works_without_script_and_links_back(b):
    n = mk(b)
    claim(b, n)
    c = login(b, "jose")
    page = c.get(f"/s/s1/t/{n}/replay").text
    assert 'class="card replay" data-replay' in page and 'class="rp-svg"' in page and 'class="rp-list"' in page
    assert "data-rp-controls hidden" in page                    # controls appear only with the script
    assert '<script src="/static/replay.js"></script>' in page and " style=" not in page
    assert f'href="/s/s1/t/{n}/replay"' in c.get(f"/s/s1/t/{n}").text
    assert c.get("/s/s1/t/999/replay").status_code == 404
    js = c.get("/static/replay.js").text
    assert "innerHTML" not in js


def _page(b):
    n = mk(b)
    claim(b, n)
    return n, login(b, "jose")


def test_b68_controls_have_fixed_buttons_play_toggle_and_position(b):
    n, c = _page(b)
    page = c.get(f"/s/s1/t/{n}/replay").text
    assert 'data-replay data-rp-total="' in page
    for name, label in (("back", "Previous step"), ("play", "Play"), ("step", "Next step"), ("reset", "Back to the first step")):
        assert f'data-rp="{name}" aria-label="{label}"' in page
    assert page.count('data-rp="play"') == 1                     # Play and Pause are the same button
    assert 'class="rp-pos" data-rp-pos aria-live="polite"></span>' in page   # filled by the script: "Step N of M"
    assert '<p class="rp-now" data-rp-now hidden></p>' in page    # the step text: one fixed line under the graph
    css = c.get("/static/app.css").text
    assert ".rp-buttons button { width: 4.5rem;" in css and ".rp-speed button { width: 4.25rem;" in css
    assert ".rp-pos { flex: none; width: 15ch;" in css and "white-space: nowrap" in css
    now = css[css.index(".rp-now {"):].split("}", 1)[0]
    assert "height: 1.5rem" in now and "text-overflow: ellipsis" in now and "white-space: nowrap" in now


def test_b68_speed_control_slow_normal_fast(b):
    n, c = _page(b)
    page = c.get(f"/s/s1/t/{n}/replay").text
    assert 'class="rp-speed" role="group" aria-label="Speed" data-rp-speed' in page
    assert 'data-rp-ms="1600" data-rp-speed-name="slow" aria-pressed="false"' in page
    assert 'data-rp-ms="900" data-rp-speed-name="normal" aria-pressed="true"' in page
    assert 'data-rp-ms="450" data-rp-speed-name="fast" aria-pressed="false"' in page
    js = c.get("/static/replay.js").text
    assert "prefers-reduced-motion: reduce" in js and '(reduced ? "slow" : "normal")' in js


def test_b68_graph_stays_in_view_and_only_inner_boxes_scroll(b):
    n, c = _page(b)
    page = c.get(f"/s/s1/t/{n}/replay").text
    assert 'class="rp-stage" data-rp-stage' in page
    assert 'class="rp-scroll" data-rp-diagram tabindex="0"' in page
    assert 'class="rp-list" data-rp-list tabindex="0"' in page
    assert 'class="rp-svg rp-svg-head"' in page                  # lane names stay on top of the diagram box
    js = c.get("/static/replay.js").text
    assert "scrollIntoView" not in js and "window.scroll" not in js and "scrollTo(" not in js
    assert "container.scrollTop" in js                           # only the boxes' own scrollTop moves
    for ev in ('"wheel"', '"touchmove"', '"pointerdown"'):
        assert f"addEventListener({ev}, " in js                    # a user scroll stops the auto-follow
    assert "follow = true;" in js.split("function play()", 1)[1].split("function move", 1)[0]   # Play resumes it
    css = c.get("/static/app.css").text
    assert ".rp-stage { position: sticky;" in css and ".rp-js .rp-list { max-height: 14rem; overflow-y: auto;" in css
    assert ".rp-svg-head { position: sticky; top: 0;" in css


def test_b68_keyboard_space_arrows_escape_without_clashing(b):
    n, c = _page(b)
    js = c.get("/static/replay.js").text
    for key in ('e.key === " "', '"ArrowRight"', '"ArrowLeft"', '"Escape"'):
        assert key in js
    assert "typing(e.target)" in js and 'dialog[open]' in js
    keys = c.get("/static/keys.js").text
    assert "ArrowRight" not in keys and "ArrowLeft" not in keys and '" "' not in keys and "Escape" not in keys
    assert 'data-rp-keys hidden' in c.get(f"/s/s1/t/{n}/replay").text
