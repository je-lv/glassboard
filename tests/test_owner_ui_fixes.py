"""Owner inspection fixes F5-F7 (2026-11-05): toasts where the eye is, an easy-to-spot thread action,
and a readable Final deliverables list."""
import pathlib

from glassboard import dashboard
from test_artifacts import setup_board, upload
from test_dashboard import login

STATIC = pathlib.Path(dashboard.__file__).parent / "static"


def test_f5_toast_sits_top_centre_and_success_stays_eight_seconds():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = css[css.index(".toasts {"):css.index("}", css.index(".toasts {"))]
    assert "top:" in rule and "left: 50%" in rule and "bottom:" not in rule
    assert "8000" in (STATIC / "toast.js").read_text(encoding="utf-8")


def test_f5_browser_upload_lands_on_a_page_with_the_uploaded_toast(gb):
    js = (STATIC / "artifacts.js").read_text(encoding="utf-8")
    assert '"ok", "artifact_uploaded"' in js and "location.reload()" not in js
    setup_board(gb)
    page = login(gb).get("/s/s1/artifacts?ok=artifact_uploaded").text
    assert "File uploaded." in page


def test_f6_human_size_and_versions_grouped_newest_first():
    assert dashboard.human_size(1) == "1 byte" and dashboard.human_size(900) == "900 bytes"
    assert dashboard.human_size(14122) == "13.8 KB" and dashboard.human_size(5 * 1024 * 1024) == "5.0 MB"
    items = [{"name": "R.txt", "size": 20, "sha256": "b" * 64, "id": "new"},
             {"name": "W.whl", "size": 10, "sha256": "c" * 64, "id": "w"},
             {"name": "R.txt", "size": 10, "sha256": "a" * 64, "id": "old"}]
    groups = dashboard.artifact_groups(items)
    assert [g["latest"]["id"] for g in groups] == ["new", "w"]
    assert [e["id"] for e in groups[0]["earlier"]] == ["old"] and groups[1]["earlier"] == []
    assert groups[0]["latest"]["short_sha"] == "b" * 12


def test_f6_deliverables_show_latest_download_and_folded_earlier_versions(gb):
    setup_board(gb)
    first = upload(gb, "jose/claude", b"draft\n", name="FINAL.txt", purpose="deliverable").json()
    second = upload(gb, "jose/claude", b"final\n", name="FINAL.txt", purpose="deliverable").json()
    page = login(gb).get("/s/s1/artifacts").text
    start = page.index('class="card deliverables"')
    zone = page[start:page.index("<h2>Supporting evidence", start)]
    assert "latest of 2" in zone and "Earlier versions (1), superseded" in zone
    assert zone.index(second["artifact"]) < zone.index(first["artifact"])          # newest first
    assert '<li class="item">' in zone and ">Download</a>" in zone and "primary" not in zone
    assert "1 current, 1 earlier version." in zone
    assert f'id="hash-{second["artifact"]}"' in zone and second["sha256"] in zone      # full hash kept for Copy
    assert "data-tone" not in zone                                                   # teal is not a state
    assert page.index("upload-panel") < start                                        # one closed line above
    assert '<script src="/static/copy.js">' in page


def test_f7_thread_card_is_one_click_target_with_a_visible_cue(gb):
    gb.session()
    gb.add("s1", "jose/claude")
    gb.req("POST", "/api/v1/sessions/s1/messages", "jose", json={"kind": "note", "body": "Hello"}, rec=gb.rec())
    q = gb.req("POST", "/api/v1/sessions/s1/messages", "jose/claude",
               json={"kind": "question", "body": "Which one?", "to": ["jose"]}, rec=gb.rec()).json()["seq"]
    page = login(gb).get("/s/s1/conversations").text
    assert '<a class="item-title stretch"' in page and 'class="item-go" aria-hidden="true">Open →' in page
    assert "thread-open" not in page
    owed = page[page.index(f"Thread #{q}") - 400:page.index(f"Thread #{q}") + 1200]
    assert 'data-tone="attention"' in owed and '<a class="button primary"' in owed and ">Reply</a>" in owed
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".item-title.stretch::after" in css and "inset: 0" in css


def test_phase5_ago_and_size_filters_and_no_bare_hashes(gb):
    import re
    from glassboard.dashboard import _ago, _env
    assert _ago(None) == "never" and _ago(59) == "just now" and _ago(60) == "1m ago" and _ago(3599) == "59m ago"
    assert _ago(3600) == "1h 0m ago" and _ago(23 * 3600) == "23h ago" and _ago(3 * 86400) == "3d ago"
    assert _env.filters["ago_min"](32818) == "22d ago" and _env.filters["size"](14122) == "13.8 KB"
    setup_board(gb)
    task = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
                  json={"title": "Audit", "scope": "x", "deliverable": "report"}, rec=gb.rec()).json()["task"]
    art = upload(gb, "jose/claude", b"evidence\n", task=f"T{task}").json()
    page = login(gb).get(f"/s/s1/t/{task}").text
    assert f'id="hash-{art["artifact"]}"' in page and "bytes ·" not in page
    visible = re.sub(r'<code id="hash-[^"]+" class="hash-full">[0-9a-f]+</code>|title="[^"]*"', "", page)
    assert not re.search(r"\b[0-9a-f]{64}\b", visible)          # every SHA-256 goes through hash()
