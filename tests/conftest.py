"""テストで共通に使う偽物。pytest がこのファイルを自動で読み込むので、テストファイルからの import はいらない。"""
import pytest

from sns_post.config import InstagramConfig
from sns_post.instagram import InstagramClient

# 偽のトークン。URL に出ていないかを確かめるための目印
FAKE_TOKEN = "SECRET"
INSTAGRAM_CONFIG = InstagramConfig(user_id="42", access_token=FAKE_TOKEN, api_version="v23.0", login="instagram")


class FakeResponse:
    """requests の返事のふりをする。"""

    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """呼ばれたURLと渡された値を記録し、用意した返事を順に返す。本物の API には繋がない。"""

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


def _to_response(item):
    """dict は成功の返事、(status_code, dict) は失敗の返事にする。"""
    if isinstance(item, tuple):
        status_code, payload = item
        return FakeResponse(payload, ok=False, status_code=status_code)
    return FakeResponse(item)


@pytest.fixture
def instagram_client():
    """偽の session を使う InstagramClient を作る関数を渡す。

    使い方: ``client, session = instagram_client([{"id": "c1"}, (400, {"error": "bad"})])``
    返事は並べた順に1つずつ返る。session.calls に、呼ばれた (method, url, kw) が残る。
    """

    def make(responses, config=INSTAGRAM_CONFIG):
        session = FakeSession(_to_response(r) for r in responses)
        return InstagramClient(config, session=session, sleep=lambda _: None), session

    return make
