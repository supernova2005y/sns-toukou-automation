from datetime import datetime, timedelta, timezone

from sns_post.errors import ApiError
from sns_post.posts import load_posts
from sns_post.runner import run_due

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 1, 13, 0, tzinfo=JST)

POST = """---
publish_at: {when}
targets: [instagram, x]
image_url: https://example.com/a.jpg
---
本文
"""


class FakeIG:
    def __init__(self):
        self.calls = 0

    def post_image(self, image_url, caption):
        self.calls += 1
        return "ig-1"


class FlakyX:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def post_text(self, text):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ApiError("X API 500: boom")
        return "x-1"


def setup(tmp_path, when="2026-11-01T12:00:00+09:00"):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "a.md").write_text(POST.format(when=when), encoding="utf-8")
    return q, tmp_path / "done"


def test_dry_run_posts_nothing(tmp_path):
    q, done = setup(tmp_path)
    ig, x = FakeIG(), FlakyX(0)
    log = run_due(load_posts(q), NOW, False, done, ig, x)
    assert ig.calls == 0 and x.calls == 0
    assert any("下見" in line for line in log)
    assert (q / "a.md").exists()


def test_future_post_is_not_run(tmp_path):
    q, done = setup(tmp_path, when="2026-11-02T12:00:00+09:00")
    ig, x = FakeIG(), FlakyX(0)
    log = run_due(load_posts(q), NOW, True, done, ig, x)
    assert ig.calls == 0 and x.calls == 0
    assert log == ["時刻が来た投稿はありません"]


def test_publish_moves_to_done(tmp_path):
    q, done = setup(tmp_path)
    run_due(load_posts(q), NOW, True, done, FakeIG(), FlakyX(0))
    assert not (q / "a.md").exists()
    assert (done / "a.md").exists()


def test_partial_failure_does_not_repost_on_retry(tmp_path):
    q, done = setup(tmp_path)
    ig, x = FakeIG(), FlakyX(1)  # Xは1回目だけ失敗

    first = run_due(load_posts(q), NOW, True, done, ig, x)
    assert any("[失敗]" in line for line in first)
    assert (q / "a.md").exists()  # まだ done に移らない
    assert ig.calls == 1

    second = run_due(load_posts(q), NOW, True, done, ig, x)
    assert ig.calls == 1  # Instagram は再投稿しない
    assert x.calls == 2
    assert (done / "a.md").exists()
    assert any("[完了]" in line for line in second)


def test_missing_client_is_skipped(tmp_path):
    q, done = setup(tmp_path)
    log = run_due(load_posts(q), NOW, True, done, None, None)
    assert sum("[スキップ]" in line for line in log) == 2
    assert (q / "a.md").exists()
