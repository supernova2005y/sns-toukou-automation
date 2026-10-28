"""X の公式 API（v2）でテキストを投稿する。

料金は従量課金（目安：通常の投稿 約0.015ドル、リンク付き 約0.20ドル）。
最新の料金は公式で確認すること。
"""
from __future__ import annotations

import re

import requests
from requests_oauthlib import OAuth1

from .config import XConfig
from .errors import ApiError

ENDPOINT = "https://api.x.com/2/tweets"
_LINK = re.compile(r"https?://", re.IGNORECASE)

COST_PLAIN_USD = 0.015
COST_WITH_LINK_USD = 0.20


def has_link(text: str) -> bool:
    return bool(_LINK.search(text))


def estimate_cost_usd(text: str) -> float:
    return COST_WITH_LINK_USD if has_link(text) else COST_PLAIN_USD


class XClient:
    def __init__(self, config: XConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def post_text(self, text: str) -> str:
        """投稿し、ポストのIDを返す。"""
        auth = OAuth1(
            self.config.api_key,
            self.config.api_secret,
            self.config.access_token,
            self.config.access_token_secret,
        )
        resp = self.session.post(ENDPOINT, json={"text": text}, auth=auth, timeout=30)
        if not resp.ok:
            raise ApiError(f"X API {resp.status_code}: {resp.text[:500]}")
        return resp.json()["data"]["id"]
