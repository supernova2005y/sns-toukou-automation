import pytest

from sns_post import config
from sns_post.errors import ApiError, ConfigError, PostFileError
from sns_post.posts import load_posts
from sns_post.runner import run_due
from sns_post.storage import MAX_IMAGE_BYTES, S3ImageStore, check_jpeg

from datetime import datetime, timedelta, timezone

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 10
CFG = config.StorageConfig(bucket="b", access_key_id="AK", secret_access_key="SK", endpoint_url="https://e.example.com")


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail

    def put_object(self, Bucket, Key, Body, ContentType):
        if self.fail:
            raise RuntimeError("secret-looking SK detail")
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://e.example.com/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"


def test_upload_saves_jpeg_with_random_key():
    s3 = FakeS3()
    key = S3ImageStore(CFG, client=s3).upload(JPEG)
    assert key.startswith("images/") and key.endswith(".jpg")
    assert s3.objects[("b", key)] == (JPEG, "image/jpeg")


@pytest.mark.parametrize("data", [b"", b"GIF89a....", b"\x89PNG....", JPEG + b"0" * MAX_IMAGE_BYTES])
def test_check_jpeg_rejects(data):
    with pytest.raises(PostFileError):
        check_jpeg(data)


def test_upload_error_does_not_leak_details():
    with pytest.raises(ApiError) as e:
        S3ImageStore(CFG, client=FakeS3(fail=True)).upload(JPEG)
    assert "SK" not in str(e.value)


def test_signed_url():
    url = S3ImageStore(CFG, client=FakeS3()).signed_url("images/a.jpg", 600)
    assert url.endswith("/b/images/a.jpg?exp=600")


def test_config_repr_hides_secrets():
    assert "AK" not in repr(CFG) and "SK" not in repr(CFG)


def test_storage_from_env(monkeypatch):
    for k in ("STORAGE_BUCKET", "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY", "STORAGE_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    assert config.storage_from_env() is None
    monkeypatch.setenv("STORAGE_BUCKET", "b")
    with pytest.raises(ConfigError):
        config.storage_from_env()
    monkeypatch.setenv("STORAGE_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://insecure.example.com")
    with pytest.raises(ConfigError):
        config.storage_from_env()


JST = timezone(timedelta(hours=9))
POST = "---\npublish_at: 2026-11-01T12:00:00+09:00\ntargets: [instagram]\n{img}\n---\n本文\n"


class FakeIG:
    def __init__(self):
        self.urls = []

    def post_image(self, image_url, caption):
        self.urls.append(image_url)
        return "ig-1"


class FakeStore:
    def signed_url(self, key, expires=3600):
        return f"https://signed.example.com/{key}"


def _run(tmp_path, img, store):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "a.md").write_text(POST.format(img=img), encoding="utf-8")
    ig = FakeIG()
    lines = run_due(load_posts(q), datetime(2026, 11, 1, 13, tzinfo=JST), True, tmp_path / "done", ig, None, store)
    return ig, lines


def test_runner_uses_signed_url_for_image_key(tmp_path):
    ig, _ = _run(tmp_path, "image_key: images/a.jpg", FakeStore())
    assert ig.urls == ["https://signed.example.com/images/a.jpg"]


def test_runner_prefers_image_url(tmp_path):
    ig, _ = _run(tmp_path, "image_url: https://example.com/a.jpg\nimage_key: images/a.jpg", FakeStore())
    assert ig.urls == ["https://example.com/a.jpg"]


def test_runner_without_store_fails_without_posting(tmp_path):
    ig, lines = _run(tmp_path, "image_key: images/a.jpg", None)
    assert ig.urls == [] and any("STORAGE_" in l for l in lines)


@pytest.mark.parametrize("key", ["/etc/a.jpg", "../a.jpg", "images/a b.jpg"])
def test_bad_image_key_is_rejected(tmp_path, key):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "a.md").write_text(POST.format(img=f"image_key: '{key}'"), encoding="utf-8")
    with pytest.raises(PostFileError):
        load_posts(q)


def test_instagram_needs_url_or_key(tmp_path):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "a.md").write_text(POST.format(img="x_text: a"), encoding="utf-8")
    with pytest.raises(PostFileError):
        load_posts(q)
