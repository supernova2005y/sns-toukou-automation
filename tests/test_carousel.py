"""Instagram の複数枚（カルーセル）投稿。API は偽の session / client で確かめる。"""
from datetime import datetime, timedelta, timezone

import pytest

from sns_post.errors import ApiError, PostFileError
from sns_post.posts import parse_post, save_post
from sns_post.runner import run_due
from tests.test_clients import FakeResponse, client

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 1, 13, 0, tzinfo=JST)


def write(tmp_path, images_yaml, extra=""):
    q = tmp_path / "queue"
    q.mkdir(exist_ok=True)
    path = q / "a.md"
    path.write_text(
        f"---\npublish_at: 2026-11-01T12:00:00+09:00\ntargets: [instagram]\n{images_yaml}\n{extra}---\n本文です\n",
        encoding="utf-8",
    )
    return path


IMAGES = "images:\n- https://example.com/1.jpg\n- images/two.jpg\n- https://example.com/3.jpg\n"


# --- 投稿ファイル ---

def test_images_are_read_and_written_back(tmp_path):
    path = write(tmp_path, IMAGES)
    post = parse_post(path)
    assert post.is_carousel and post.images == ["https://example.com/1.jpg", "images/two.jpg", "https://example.com/3.jpg"]
    assert post.needs_store  # image_key が混ざっている
    post.results["instagram"] = "ig-1"
    save_post(post)
    again = parse_post(path)
    assert again.images == post.images and again.results == {"instagram": "ig-1"}


@pytest.mark.parametrize(
    "images_yaml, extra, message",
    [
        ("images:\n- https://example.com/1.jpg\n", "", "2〜20 枚"),
        ("images:\n" + "".join(f"- https://example.com/{i}.jpg\n" for i in range(21)), "", "2〜20 枚"),
        (IMAGES, "image_url: https://example.com/a.jpg\n", "一緒に書けません"),
        ("images:\n- https://example.com/1.jpg\n- http://example.com/2.jpg\n", "", "https://"),
        ("images:\n- https://example.com/1.jpg\n- ../secret.jpg\n", "", "image_key の形"),
        ("images: https://example.com/1.jpg\n", "", "1行ずつ"),
    ],
)
def test_bad_images_are_rejected(tmp_path, images_yaml, extra, message):
    path = write(tmp_path, images_yaml, extra)
    with pytest.raises(PostFileError, match=message):
        parse_post(path)


def test_single_image_post_does_not_need_store_unless_key(tmp_path):
    path = write(tmp_path, "image_url: https://example.com/a.jpg\n")
    post = parse_post(path)
    assert not post.is_carousel and not post.needs_store


# --- Instagram クライアント ---

def test_post_carousel_flow():
    c, s = client(
        [
            FakeResponse({"id": "item-1"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "item-2"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "carousel-1"}),
            FakeResponse({"status_code": "IN_PROGRESS"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-9"}),
        ]
    )
    assert c.post_carousel(["https://example.com/1.jpg", "https://example.com/2.jpg"], "キャプション") == "media-9"
    posts = [kw["data"] for method, _, kw in s.calls if method == "POST"]
    # 1枚ずつのコンテナにはキャプションをつけない
    assert posts[0] == {"image_url": "https://example.com/1.jpg", "is_carousel_item": "true"}
    assert posts[1] == {"image_url": "https://example.com/2.jpg", "is_carousel_item": "true"}
    assert posts[2] == {"media_type": "CAROUSEL", "children": "item-1,item-2", "caption": "キャプション"}
    assert posts[3] == {"creation_id": "carousel-1"}
    for _, url, kw in s.calls:
        assert "SECRET" not in url


def test_post_carousel_stops_before_publish_when_an_item_fails():
    c, s = client([FakeResponse({"id": "item-1"}), FakeResponse({"status_code": "ERROR"})])
    with pytest.raises(ApiError, match="ERROR"):
        c.post_carousel(["https://example.com/1.jpg", "https://example.com/2.jpg"], "x")
    assert not any(url.endswith("/media_publish") for _, url, _ in s.calls)


# --- 投稿の流れ（runner） ---

class FakeIG:
    def __init__(self, fail=False):
        self.carousels = []
        self.singles = 0
        self.fail = fail

    def post_image(self, image_url, caption):
        self.singles += 1
        return "ig-single"

    def post_carousel(self, image_urls, caption):
        self.carousels.append(list(image_urls))
        if self.fail:
            raise ApiError("Instagram API 400: だめでした")
        return "ig-carousel"


class FakeStore:
    def signed_url(self, key, expires=3600):
        return f"https://signed.example.com/{key}"


def test_runner_posts_carousel_with_signed_urls_and_records(tmp_path):
    path = write(tmp_path, IMAGES)
    ig = FakeIG()
    done = tmp_path / "done"
    lines = run_due([parse_post(path)], NOW, True, done, ig, None, FakeStore())
    assert ig.singles == 0
    assert ig.carousels == [["https://example.com/1.jpg", "https://signed.example.com/images/two.jpg", "https://example.com/3.jpg"]]
    assert parse_post(done / "a.md").results == {"instagram": "ig-carousel"}
    assert any("[投稿]" in line for line in lines)


def test_runner_carousel_with_key_but_no_store_is_not_posted(tmp_path):
    path = write(tmp_path, IMAGES)
    ig = FakeIG()
    lines = run_due([parse_post(path)], NOW, True, tmp_path / "done", ig, None, None)
    assert ig.carousels == [] and path.exists()
    assert any("[失敗]" in line and "STORAGE" in line for line in lines)


def test_runner_failed_carousel_stays_in_queue(tmp_path):
    path = write(tmp_path, "images:\n- https://example.com/1.jpg\n- https://example.com/2.jpg\n")
    ig = FakeIG(fail=True)
    run_due([parse_post(path)], NOW, True, tmp_path / "done", ig, None, None)
    assert path.exists() and parse_post(path).results == {}


def test_dry_run_shows_number_of_images(tmp_path):
    path = write(tmp_path, IMAGES)
    lines = run_due([parse_post(path)], NOW, False, tmp_path / "done")
    assert any("[下見]" in line and "3枚" in line for line in lines)


# --- 予約画面 ---

def test_screen_saves_several_images_as_carousel(tmp_path):
    from sns_post.web import create_post, list_posts, read_post

    q = tmp_path / "queue"
    data = {
        "text": "新作です",
        "targets": ["instagram"],
        "images": ["https://example.com/1.jpg", " images/two.jpg ", ""],
        "publish_at": "2026-11-02T12:00:00+09:00",
    }
    post = create_post(data, q, NOW)
    saved = parse_post(post.path)
    assert saved.images == ["https://example.com/1.jpg", "images/two.jpg"] and not saved.image_url
    assert read_post(post.path.name, q)["images"] == saved.images
    item = list_posts(q, tmp_path / "done", NOW)[0]
    assert item["image_count"] == 2 and item["image_url"] == "https://example.com/1.jpg"


def test_screen_one_image_in_list_is_a_normal_post(tmp_path):
    from sns_post.web import create_post

    data = {"text": "x", "targets": ["instagram"], "images": ["images/one.jpg"], "publish_at": "2026-11-02T12:00:00+09:00"}
    post = parse_post(create_post(data, tmp_path / "queue", NOW).path)
    assert post.images == [] and post.image_key == "images/one.jpg"


def test_screen_rejects_too_many_images(tmp_path):
    from sns_post.web import create_post

    data = {
        "text": "x",
        "targets": ["instagram"],
        "images": [f"https://example.com/{i}.jpg" for i in range(21)],
        "publish_at": "2026-11-02T12:00:00+09:00",
    }
    with pytest.raises(PostFileError, match="2〜20 枚"):
        create_post(data, tmp_path / "queue", NOW)


def test_screen_drops_images_when_instagram_is_not_a_target(tmp_path):
    from sns_post.web import create_post

    data = {"text": "x", "targets": ["x"], "images": ["https://example.com/1.jpg", "https://example.com/2.jpg"], "publish_at": "2026-11-02T12:00:00+09:00"}
    post = parse_post(create_post(data, tmp_path / "queue", NOW).path)
    assert post.images == []
