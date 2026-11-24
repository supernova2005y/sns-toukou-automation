"""画面からの投稿（sns-post web --publish）のテスト。本物のAPIは呼ばない。"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from sns_post.errors import ApiError, ConfigError
from sns_post.posts import parse_post
from sns_post.publisher import LOCK_NAME, BusyError, Publisher, publish_lock
from sns_post.web import create_post, file_version, make_handler, serve

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=JST)


class FakeIG:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post_image(self, image_url, caption):
        self.calls.append((image_url, caption))
        if self.fail:
            raise ApiError("Instagram API 400: だめでした")
        return f"ig-{len(self.calls)}"


def make(tmp_path, ig=None):
    ig = ig or FakeIG()
    q, d = tmp_path / "posts" / "queue", tmp_path / "posts" / "done"
    pub = Publisher(q, d, clients=lambda targets: (ig, None))
    return pub, ig, q, d


def new_post(q, when="2026-11-11T20:00:00+09:00", targets=("instagram",)):
    return create_post(
        {"text": "新作です", "targets": list(targets), "image_url": "https://example.com/a.jpg", "publish_at": when},
        q,
        NOW,
    )


def test_publish_now_posts_before_the_time_and_moves_to_done(tmp_path):
    pub, ig, q, d = make(tmp_path)
    post = new_post(q)
    lines = pub.publish_now(post.path, NOW)
    assert ig.calls == [("https://example.com/a.jpg", "新作です")]
    assert any(line.startswith("[投稿]") for line in lines)
    moved = d / post.path.name
    assert moved.exists() and not post.path.exists()
    assert parse_post(moved).results == {"instagram": "ig-1"}
    assert pub.log[0]["line"].startswith("[完了]")
    assert not (tmp_path / "posts" / LOCK_NAME).exists()


def test_publish_now_twice_does_not_double_post(tmp_path):
    pub, ig, q, _ = make(tmp_path)
    post = new_post(q)
    pub.publish_now(post.path, NOW)
    with pytest.raises(LookupError):
        pub.publish_now(post.path, NOW)
    assert len(ig.calls) == 1


def test_failed_post_stays_in_queue_without_results(tmp_path):
    pub, ig, q, _ = make(tmp_path, FakeIG(fail=True))
    post = new_post(q)
    lines = pub.publish_now(post.path, NOW)
    assert any(line.startswith("[失敗]") for line in lines)
    assert post.path.exists() and parse_post(post.path).results == {}


def test_publish_due_only_posts_what_is_due(tmp_path):
    pub, ig, q, d = make(tmp_path)
    soon = new_post(q, "2026-11-08T12:30:00+09:00")
    later = new_post(q, "2026-11-09T12:00:00+09:00")
    assert pub.publish_due(NOW) == []  # まだ時刻が来ていない
    pub.publish_due(NOW + timedelta(hours=1))
    assert len(ig.calls) == 1
    assert (d / soon.path.name).exists() and later.path.exists()


def test_publish_due_skips_when_another_process_is_posting(tmp_path):
    pub, ig, q, _ = make(tmp_path)
    new_post(q, "2026-11-08T12:30:00+09:00")
    with publish_lock(tmp_path / "posts"):
        assert pub.publish_due(NOW + timedelta(hours=1)) == []
    assert ig.calls == []


def test_publish_now_refuses_while_locked(tmp_path):
    pub, ig, q, _ = make(tmp_path)
    post = new_post(q)
    with publish_lock(tmp_path / "posts"):
        with pytest.raises(BusyError):
            pub.publish_now(post.path, NOW)
    assert ig.calls == []


def test_lock_is_kept_fresh_while_publishing(tmp_path):
    # 長い投稿（カルーセル20枚など）の最中に、ほかの処理から「古いロック」とみなされないこと
    posts = tmp_path / "posts"
    lock = posts / LOCK_NAME
    with publish_lock(posts, heartbeat=0.05):
        old = time.time() - 3600
        os.utime(lock, (old, old))  # 長く投稿している状態をまねる
        deadline = time.time() + 5
        while lock.stat().st_mtime <= old + 1 and time.time() < deadline:
            time.sleep(0.02)
        assert time.time() - lock.stat().st_mtime < 60
        with pytest.raises(BusyError):  # 新しいままなので、ほかの処理は引き継がない
            with publish_lock(posts):
                pass
    assert not lock.exists()


def test_stale_lock_is_taken_over(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    lock = posts / LOCK_NAME
    lock.write_text("123")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with publish_lock(posts):
        assert lock.exists()
    assert not lock.exists()


def test_serve_refuses_publish_on_open_host(tmp_path):
    with pytest.raises(ConfigError):
        serve(tmp_path / "q", tmp_path / "d", host="0.0.0.0", publish=True)


# --- 画面（HTTP）から ---


def _start(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _call(url, method, path, body=None):
    req = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def publishing(tmp_path):
    pub, ig, q, d = make(tmp_path)
    srv, url = _start(make_handler(q, d, clock=lambda: NOW, publisher=pub))
    yield url, ig, q, d
    srv.shutdown()
    srv.server_close()


def test_http_publish_round_trip(publishing):
    url, ig, q, d = publishing
    post = new_post(q)
    status, data = _call(url, "GET", "/api/posts")
    assert data["publish_enabled"] is True
    status, data = _call(url, "POST", f"/api/posts/{post.path.name}/publish", {"version": file_version(post.path)})
    assert status == 200 and data["ok"] is True
    assert len(ig.calls) == 1 and (d / post.path.name).exists()
    status, data = _call(url, "GET", "/api/log")
    assert any("[投稿]" in e["line"] for e in data["log"])


def test_http_publish_needs_current_version(publishing):
    url, ig, q, _ = publishing
    post = new_post(q)
    status, data = _call(url, "POST", f"/api/posts/{post.path.name}/publish", {"version": "old"})
    assert status == 409 and ig.calls == []


def test_http_publish_is_refused_in_preview_mode(tmp_path):
    q, d = tmp_path / "posts" / "queue", tmp_path / "posts" / "done"
    post = new_post(q)
    srv, url = _start(make_handler(q, d, clock=lambda: NOW))
    try:
        status, data = _call(url, "GET", "/api/posts")
        assert data["publish_enabled"] is False
        status, data = _call(url, "POST", f"/api/posts/{post.path.name}/publish", {"version": file_version(post.path)})
        assert status == 403 and "下見モード" in data["error"]
        assert post.path.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_run_due_publish_respects_the_lock(tmp_path, monkeypatch, capsys):
    from sns_post import cli

    q = tmp_path / "posts" / "queue"
    new_post(q, "2026-11-08T12:30:00+09:00", targets=("x",))
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(k, "dummy")
    monkeypatch.setattr(cli, "XClient", lambda cfg: object())
    with publish_lock(tmp_path / "posts"):
        code = cli.main(["run-due", "--queue", str(q), "--done", str(tmp_path / "posts" / "done"),
                         "--publish", "--now", "2026-11-08T13:00:00+09:00"])
    assert code == 1
    assert "投稿中" in capsys.readouterr().err


# --- 認証情報が足りないときの知らせ ---

ALL_CRED = ["IG_USER_ID", "IG_ACCESS_TOKEN", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]


def test_missing_credentials_lists_names_only(monkeypatch):
    from sns_post.config import missing_credentials

    for n in ALL_CRED:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("IG_USER_ID", "fake-user")
    assert missing_credentials() == {
        "instagram": ["IG_ACCESS_TOKEN"],
        "x": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"],
    }
    monkeypatch.setenv("IG_ACCESS_TOKEN", "fake-token")
    result = missing_credentials()
    assert "instagram" not in result
    assert "fake-token" not in json.dumps(result)  # 値は返さない


def test_screen_shows_missing_credentials_only_in_publish_mode(tmp_path):
    pub, ig, q, d = make(tmp_path)
    missing = {"instagram": ["IG_ACCESS_TOKEN"]}
    srv, url = _start(make_handler(q, d, clock=lambda: NOW, publisher=pub, missing_credentials=missing))
    try:
        _, data = _call(url, "GET", "/api/posts")
        assert data["missing_credentials"] == missing
    finally:
        srv.shutdown()
        srv.server_close()
    srv, url = _start(make_handler(q, d, clock=lambda: NOW, missing_credentials=missing))
    try:
        _, data = _call(url, "GET", "/api/posts")
        assert data["missing_credentials"] == {}  # 下見モードでは認証情報を使わない
    finally:
        srv.shutdown()
        srv.server_close()


def test_credential_warnings_text():
    from sns_post.web import credential_warnings

    assert credential_warnings({}) == []
    text = "\n".join(credential_warnings({"instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN"]}))
    assert "Instagram: .env に IG_USER_ID, IG_ACCESS_TOKEN がありません" in text
    assert "起動し直して" in text
