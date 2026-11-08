import pytest

from sns_post.config import InstagramConfig
from sns_post.errors import ApiError
from sns_post.instagram import InstagramClient
from sns_post.x import estimate_cost_usd, has_link


class FakeResponse:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """呼ばれたURLと渡された値を記録し、用意した返事を順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


CONFIG = InstagramConfig(user_id="42", access_token="SECRET", api_version="v23.0", login="instagram")


def client(responses):
    s = FakeSession(responses)
    return InstagramClient(CONFIG, session=s, sleep=lambda _: None), s


def test_post_image_flow_and_token_in_header_not_url():
    c, s = client(
        [
            FakeResponse({"id": "container-1"}),
            FakeResponse({"status_code": "IN_PROGRESS"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-1"}),
        ]
    )
    assert c.post_image("https://example.com/a.jpg", "hello") == "media-1"
    urls = [u for _, u, _ in s.calls]
    assert urls[0] == "https://graph.instagram.com/v23.0/42/media"
    assert urls[-1] == "https://graph.instagram.com/v23.0/42/media_publish"
    for _, url, kw in s.calls:
        assert "SECRET" not in url
        assert kw["headers"]["Authorization"] == "Bearer SECRET"
    assert s.calls[0][2]["data"] == {"image_url": "https://example.com/a.jpg", "caption": "hello"}
    assert s.calls[-1][2]["data"] == {"creation_id": "container-1"}


def test_container_error_raises():
    c, _ = client([FakeResponse({"id": "c"}), FakeResponse({"status_code": "ERROR"})])
    with pytest.raises(ApiError, match="ERROR"):
        c.post_image("https://example.com/a.jpg", "x")


def test_api_error_message_has_status():
    c, _ = client([FakeResponse({"error": "bad"}, ok=False, status_code=400)])
    with pytest.raises(ApiError, match="400"):
        c.create_image_container("https://example.com/a.jpg", "x")


def test_config_repr_hides_token():
    assert "SECRET" not in repr(CONFIG)


def test_x_cost_estimate():
    assert not has_link("こんにちは")
    assert has_link("詳細は https://example.com")
    assert estimate_cost_usd("こんにちは") == 0.015
    assert estimate_cost_usd("https://example.com") == 0.20


def test_update_env_replaces_only_that_line(tmp_path, monkeypatch):
    from sns_post.config import update_env

    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("IG_USER_ID=42\nIG_ACCESS_TOKEN=OLD\nX_API_KEY=k\n", encoding="utf-8")
    update_env("IG_ACCESS_TOKEN", "NEW", env)
    assert env.read_text(encoding="utf-8") == "IG_USER_ID=42\nIG_ACCESS_TOKEN=NEW\nX_API_KEY=k\n"


def test_refresh_token_command_does_not_print_token(tmp_path, monkeypatch, capsys):
    from sns_post import cli

    env = tmp_path / ".env"
    env.write_text("IG_ACCESS_TOKEN=OLD\n", encoding="utf-8")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "OLD")
    monkeypatch.setenv("IG_LOGIN", "instagram")
    monkeypatch.setattr(cli, "refresh_token", lambda t: {"access_token": "BRAND-NEW", "expires_in": 5184000})
    assert cli.main(["refresh-token", "--env", str(env)]) == 0
    out = capsys.readouterr().out
    assert "BRAND-NEW" not in out
    assert "約60日" in out
    assert "IG_ACCESS_TOKEN=BRAND-NEW" in env.read_text(encoding="utf-8")


def test_unified_login_uses_graph_facebook_com():
    cfg = InstagramConfig(user_id="42", access_token="SECRET", api_version="v23.0")
    assert cfg.login == "unified"
    s = FakeSession([FakeResponse({"id": "c1"}), FakeResponse({"status_code": "FINISHED"}), FakeResponse({"id": "m1"})])
    client = InstagramClient(cfg, session=s, sleep=lambda _: None)
    assert client.post_image("https://example.com/a.jpg", "hi") == "m1"
    urls = [c[1] for c in s.calls]
    assert all(u.startswith("https://graph.facebook.com/v23.0/") for u in urls)
    assert all("SECRET" not in u for u in urls)


def test_instagram_from_env_login(monkeypatch):
    from sns_post.config import instagram_from_env
    from sns_post.errors import ConfigError

    monkeypatch.setenv("IG_USER_ID", "42")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "SECRET")
    monkeypatch.delenv("IG_LOGIN", raising=False)
    assert instagram_from_env().host == "https://graph.facebook.com"
    monkeypatch.setenv("IG_LOGIN", "Instagram")
    assert instagram_from_env().host == "https://graph.instagram.com"
    monkeypatch.setenv("IG_LOGIN", "page")
    with pytest.raises(ConfigError):
        instagram_from_env()


def test_refresh_token_skipped_for_unified(tmp_path, monkeypatch, capsys):
    from sns_post import cli

    env = tmp_path / ".env"
    env.write_text("IG_ACCESS_TOKEN=OLD\n", encoding="utf-8")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "OLD")
    monkeypatch.delenv("IG_LOGIN", raising=False)

    def boom(_):
        raise AssertionError("呼ばれてはいけない")

    monkeypatch.setattr(cli, "refresh_token", boom)
    assert cli.main(["refresh-token", "--env", str(env)]) == 0
    assert "不要" in capsys.readouterr().out
    assert env.read_text(encoding="utf-8") == "IG_ACCESS_TOKEN=OLD\n"
