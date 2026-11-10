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


# --- 編集・取り消し ---
from sns_post.posts import save_post  # noqa: E402
from sns_post.web import cancel_post, file_version, read_post, update_post  # noqa: E402


def _version(post):
    return file_version(post.path)


def test_update_keeps_name_when_time_is_same(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    new = update_post(post.path.name, {**GOOD, "text": "書き直しました", "version": _version(post)}, q, NOW)
    assert new.path == post.path
    assert parse_post(new.path).text == "書き直しました"


def test_update_renames_when_time_changes(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    new = update_post(post.path.name, {**GOOD, "publish_at": "2026-11-12T09:30:00+09:00", "version": _version(post)}, q, NOW)
    assert new.path.name == "2026-11-12_0930.md"
    assert not post.path.exists()
    assert [p.name for p in q.glob("*.md")] == ["2026-11-12_0930.md"]


def test_update_can_move_an_overdue_post_to_the_future(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    later = datetime(2026, 11, 12, 0, 0, tzinfo=JST)  # 予約時刻を過ぎた
    new = update_post(post.path.name, {**GOOD, "publish_at": "2026-11-13T20:00:00+09:00", "version": _version(post)}, q, later)
    assert new.publish_at.day == 13


def test_update_drops_image_when_instagram_is_removed(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    new = update_post(post.path.name, {**GOOD, "targets": ["x"], "version": _version(post)}, q, NOW)
    assert parse_post(new.path).image_url is None


def test_update_refuses_when_file_changed_since_opened(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    old_version = _version(post)
    post.text = "ほかの場所で書き換え"
    save_post(post)
    with pytest.raises(PostFileError, match="読み込み直して"):
        update_post(post.path.name, {**GOOD, "version": old_version}, q, NOW)
    assert parse_post(post.path).text == "ほかの場所で書き換え"


def test_partly_posted_cannot_be_edited_or_cancelled(tmp_path):
    q = tmp_path / "queue"
    post = create_post(dict(GOOD), q, NOW)
    post.results = {"instagram": "123"}
    save_post(post)
    v = _version(post)
    with pytest.raises(PostFileError, match="投稿済み"):
        update_post(post.path.name, {**GOOD, "version": v}, q, NOW)
    with pytest.raises(PostFileError, match="投稿済み"):
        cancel_post(post.path.name, v, q, tmp_path / "cancelled")
    assert parse_post(post.path).results == {"instagram": "123"}
    assert read_post(post.path.name, q)["editable"] is False


def test_cancel_moves_file_instead_of_deleting(tmp_path):
    q, c = tmp_path / "queue", tmp_path / "cancelled"
    post = create_post(dict(GOOD), q, NOW)
    dest = cancel_post(post.path.name, _version(post), q, c)
    assert not post.path.exists() and dest.exists()
    assert list_posts(q, tmp_path / "done", NOW) == []


@pytest.mark.parametrize("name", ["../secret.md", "..%2Fsecret.md", "_template.md", ".hidden.md", "a.txt", "nothing.md"])
def test_names_outside_the_queue_are_not_found(tmp_path, name):
    q = tmp_path / "queue"
    q.mkdir()
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    with pytest.raises(LookupError):
        read_post(name, q)
    with pytest.raises(LookupError):
        cancel_post(name, "x", q, tmp_path / "cancelled")
    assert (tmp_path / "secret.md").exists()


def _call(url, method, path, body=None, headers=None):
    req = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_edit_and_cancel_round_trip(server):
    url, tmp = server
    _, created = _post(url, GOOD)
    name = created["post"]["name"]
    assert created["post"]["editable"] is True

    status, got = _call(url, "GET", f"/api/posts/{name}")
    assert status == 200 and got["post"]["text"] == "新作が入りました"
    v = got["post"]["version"]

    status, edited = _call(url, "PUT", f"/api/posts/{name}", {**GOOD, "text": "変更", "version": v})
    assert status == 200 and edited["post"]["excerpt"] == "変更"

    status, _ = _call(url, "POST", f"/api/posts/{name}/cancel", {"version": v})
    assert status == 409  # 古い目印では取り消せない

    v2 = _call(url, "GET", f"/api/posts/{name}")[1]["post"]["version"]
    status, _ = _call(url, "POST", f"/api/posts/{name}/cancel", {"version": v2}, {"Origin": "https://evil.example"})
    assert status == 403
    status, data = _call(url, "POST", f"/api/posts/{name}/cancel", {"version": v2})
    assert status == 200 and (tmp / "cancelled" / data["cancelled"]).exists()

    status, _ = _call(url, "GET", f"/api/posts/{name}")
    assert status == 404


# --- 画像を上げる（偽の置き場所を使う。本物のバケットは呼ばない） ---
class _FakeStore:
    def __init__(self):
        self.saved = []

    def upload(self, data):
        from sns_post.storage import check_jpeg

        check_jpeg(data)
        self.saved.append(data)
        return "images/" + "a" * 32 + ".jpg"

    def signed_url(self, key, expires=3600):
        return "https://signed.example.com/" + key


def _serve(tmp_path, store):
    h = make_handler(tmp_path / "queue", tmp_path / "done", lambda: NOW, image_store=store)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post_image(base, body, ctype="image/jpeg", headers=None):
    req = urllib.request.Request(base + "/api/images", data=body, method="POST", headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_upload_image_returns_key_and_reservation_uses_it(tmp_path):
    store = _FakeStore()
    srv, base = _serve(tmp_path, store)
    try:
        status, data = _post_image(base, b"\xff\xd8\xff\xe0" + b"0" * 10)
        assert status == 201 and data["image_key"].startswith("images/")
        body = {k: v for k, v in GOOD.items() if k != "image_url"} | {"image_key": data["image_key"]}
        req = urllib.request.Request(base + "/api/posts", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 201
    finally:
        srv.shutdown()
    saved = parse_post(next((tmp_path / "queue").glob("*.md")))
    assert saved.image_key == data["image_key"] and saved.image_url is None


@pytest.mark.parametrize(
    "body, ctype, headers, code",
    [
        (b"GIF89a....", "image/jpeg", None, 400),  # 中身が JPEG ではない
        (b"\xff\xd8\xff\xe0", "image/png", None, 415),
        (b"\xff\xd8\xff\xe0", "image/jpeg", {"Origin": "http://evil.example.com"}, 403),
    ],
)
def test_upload_image_rejects_bad_requests(tmp_path, body, ctype, headers, code):
    srv, base = _serve(tmp_path, _FakeStore())
    try:
        status, _ = _post_image(base, body, ctype, headers)
    finally:
        srv.shutdown()
    assert status == code


def test_upload_image_without_store_is_refused(tmp_path):
    srv, base = _serve(tmp_path, None)
    try:
        status, data = _post_image(base, b"\xff\xd8\xff\xe0")
    finally:
        srv.shutdown()
    assert status == 403 and "STORAGE_" in data["error"]
