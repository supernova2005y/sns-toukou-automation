import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from sns_post.errors import PostFileError
from sns_post.posts import parse_post
from sns_post.web import create_post, list_posts, make_handler

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=JST)

GOOD = {
    "text": "新作が入りました",
    "targets": ["instagram", "x"],
    "image_url": "https://example.com/a.jpg",
    "publish_at": "2026-11-11T20:00:00+09:00",
}


def test_create_post_writes_a_file_run_due_can_read(tmp_path):
    post = create_post(dict(GOOD), tmp_path / "queue", NOW)
    assert post.path.name == "2026-11-11_2000.md"
    again = parse_post(post.path)  # run-due と同じ読み込みで読める
    assert again.targets == ["instagram", "x"]
    assert again.text == "新作が入りました"
    assert again.results == {}


def test_same_time_gets_a_new_name(tmp_path):
    q = tmp_path / "queue"
    a = create_post(dict(GOOD), q, NOW)
    b = create_post(dict(GOOD), q, NOW)
    assert a.path != b.path and b.path.name == "2026-11-11_2000_2.md"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"publish_at": "2026-11-01T20:00:00+09:00"}, "過ぎています"),
        ({"publish_at": "2026-11-11T20:00:00"}, "タイムゾーン"),
        ({"targets": []}, "投稿先"),
        ({"targets": ["facebook"]}, "投稿先"),
        ({"text": "  "}, "本文"),
        ({"image_url": "http://example.com/a.jpg"}, "https"),
        ({"targets": ["x"], "text": "あ" * 141}, "282/280"),
    ],
)
def test_create_post_rejects_bad_input(tmp_path, change, message):
    with pytest.raises(PostFileError, match=message):
        create_post({**GOOD, **change}, tmp_path / "queue", NOW)
    assert not (tmp_path / "queue").exists() or not list((tmp_path / "queue").glob("*.md"))


def test_list_posts_statuses(tmp_path):
    q, d = tmp_path / "queue", tmp_path / "done"
    create_post(dict(GOOD), q, NOW)
    create_post({**GOOD, "publish_at": "2026-11-09T09:00:00+09:00"}, q, NOW)
    done = create_post({**GOOD, "publish_at": "2026-11-10T09:00:00+09:00"}, q, NOW)
    d.mkdir()
    done.path.rename(d / done.path.name)
    (q / "broken.md").write_text("設定なし", encoding="utf-8")

    later = datetime(2026, 11, 10, 0, 0, tzinfo=JST)
    items = list_posts(q, d, later)
    by_name = {i["name"]: i["status"] for i in items}
    assert by_name["2026-11-09_0900.md"] == "overdue"  # 時刻を過ぎたのに残っている
    assert by_name["2026-11-11_2000.md"] == "scheduled"
    assert by_name["2026-11-10_0900.md"] == "done"
    assert by_name["broken.md"] == "error"
    assert items[-1]["status"] == "done"  # 済みは最後


@pytest.fixture
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "queue", tmp_path / "done", clock=lambda: NOW))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()
    srv.server_close()


def _post(url, body, headers=None):
    req = urllib.request.Request(
        url + "/api/posts",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_page_and_round_trip(server):
    url, tmp = server
    with urllib.request.urlopen(url + "/") as r:
        assert "予約投稿" in r.read().decode()
    status, data = _post(url, GOOD)
    assert status == 201 and data["post"]["name"] == "2026-11-11_2000.md"
    with urllib.request.urlopen(url + "/api/posts") as r:
        posts = json.loads(r.read())["posts"]
    assert [p["status"] for p in posts] == ["scheduled"]


def test_http_rejects_other_sites_and_non_json(server):
    url, tmp = server
    status, _ = _post(url, GOOD, {"Origin": "https://evil.example"})
    assert status == 403
    req = urllib.request.Request(url + "/api/posts", data=b"text=a", headers={"Content-Type": "text/plain"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 415
    assert not (tmp / "queue").exists()


def test_http_error_message_is_readable(server):
    url, _ = server
    status, data = _post(url, {**GOOD, "publish_at": "2026-11-01T20:00:00+09:00"})
    assert status == 400 and "過ぎています" in data["error"] and ".md" not in data["error"]
