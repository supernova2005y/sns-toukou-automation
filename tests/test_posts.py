from datetime import datetime, timedelta, timezone

import pytest

from sns_post.errors import PostFileError
from sns_post.posts import load_posts, parse_post, save_post

JST = timezone(timedelta(hours=9))

GOOD = """---
publish_at: 2026-11-01T12:00:00+09:00
targets: [instagram, x]
image_url: https://example.com/a.jpg
---
こんにちは #テスト
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_good(tmp_path):
    post = parse_post(write(tmp_path, "a.md", GOOD))
    assert post.publish_at == datetime(2026, 11, 1, 12, 0, tzinfo=JST)
    assert post.targets == ["instagram", "x"]
    assert post.text == "こんにちは #テスト"
    assert post.pending_targets == ["instagram", "x"]


def test_timezone_required(tmp_path):
    p = write(tmp_path, "a.md", GOOD.replace("+09:00", ""))
    with pytest.raises(PostFileError, match="タイムゾーン"):
        parse_post(p)


def test_instagram_needs_https_image(tmp_path):
    p = write(tmp_path, "a.md", GOOD.replace("https://example.com/a.jpg", "http://example.com/a.jpg"))
    with pytest.raises(PostFileError, match="image_url"):
        parse_post(p)


def test_x_only_needs_no_image(tmp_path):
    text = GOOD.replace("[instagram, x]", "[x]").replace("image_url: https://example.com/a.jpg\n", "")
    assert parse_post(write(tmp_path, "a.md", text)).targets == ["x"]


def test_unknown_target(tmp_path):
    with pytest.raises(PostFileError, match="未対応"):
        parse_post(write(tmp_path, "a.md", GOOD.replace("[instagram, x]", "[tiktok]")))


def test_x_text_too_long(tmp_path):
    text = GOOD.replace("こんにちは #テスト", "あ" * 281).replace("[instagram, x]", "[x]")
    with pytest.raises(PostFileError, match="280"):
        parse_post(write(tmp_path, "a.md", text))


def test_x_text_override_allows_short_x(tmp_path):
    text = GOOD.replace("こんにちは #テスト", "あ" * 300).replace(
        "image_url: https://example.com/a.jpg", "image_url: https://example.com/a.jpg\nx_text: 短い文面"
    )
    post = parse_post(write(tmp_path, "a.md", text))
    assert post.text_for("x") == "短い文面"
    assert post.text_for("instagram") == "あ" * 300


def test_too_many_hashtags(tmp_path):
    tags = " ".join(f"#t{i}" for i in range(31))
    with pytest.raises(PostFileError, match="ハッシュタグ"):
        parse_post(write(tmp_path, "a.md", GOOD.replace("こんにちは #テスト", tags)))


def test_save_roundtrip_keeps_results(tmp_path):
    post = parse_post(write(tmp_path, "a.md", GOOD))
    post.results["instagram"] = "123"
    save_post(post)
    again = parse_post(post.path)
    assert again.results == {"instagram": "123"}
    assert again.pending_targets == ["x"]
    assert again.publish_at == post.publish_at
    assert again.text == post.text


def test_load_posts_skips_underscore_files(tmp_path):
    write(tmp_path, "a.md", GOOD)
    write(tmp_path, "_memo.md", "not a post")
    assert [p.path.name for p in load_posts(tmp_path)] == ["a.md"]
