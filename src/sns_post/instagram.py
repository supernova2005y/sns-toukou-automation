"""Instagram の公式 API で画像を投稿する。

接続先は IG_LOGIN で切り替える（unified: graph.facebook.com／instagram: graph.instagram.com）。

流れ：コンテナ作成 → 準備完了を待つ → 公開。
複数枚（カルーセル）：1枚ずつ is_carousel_item のコンテナ → まとめる CAROUSEL のコンテナ（キャプションはこちら）→ 公開。
画像は、Meta のサーバーが取りに来られる公開URL（JPEG）に置いておく必要がある。
"""
from __future__ import annotations

import time
from typing import Callable

import requests

from .config import IG_HOSTS, InstagramConfig
from .errors import ApiError


class InstagramClient:
    def __init__(
        self,
        config: InstagramConfig,
        session: requests.Session | None = None,
        poll_interval: float = 2.0,
        poll_timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sleep = sleep

    def _url(self, path: str) -> str:
        return f"{self.config.host}/{self.config.api_version}/{path}"

    def _headers(self) -> dict[str, str]:
        # トークンはURLではなくヘッダーで渡す（エラー表示やログに残りにくい）
        return {"Authorization": f"Bearer {self.config.access_token}"}

    @staticmethod
    def _check(resp: requests.Response) -> dict:
        if not resp.ok:
            raise ApiError(f"Instagram API {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def create_image_container(self, image_url: str, caption: str) -> str:
        resp = self.session.post(
            self._url(f"{self.config.user_id}/media"),
            headers=self._headers(),
            data={"image_url": image_url, "caption": caption},
            timeout=30,
        )
        return self._check(resp)["id"]

    def create_carousel_item(self, image_url: str) -> str:
        """カルーセルの1枚。キャプションはつけない（公式：カルーセルの各画像では使えない）。"""
        resp = self.session.post(
            self._url(f"{self.config.user_id}/media"),
            headers=self._headers(),
            data={"image_url": image_url, "is_carousel_item": "true"},
            timeout=30,
        )
        return self._check(resp)["id"]

    def create_carousel_container(self, children: list[str], caption: str) -> str:
        resp = self.session.post(
            self._url(f"{self.config.user_id}/media"),
            headers=self._headers(),
            data={"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
            timeout=30,
        )
        return self._check(resp)["id"]

    def wait_until_ready(self, container_id: str) -> None:
        waited = 0.0
        while True:
            resp = self.session.get(
                self._url(container_id),
                headers=self._headers(),
                params={"fields": "status_code"},
                timeout=30,
            )
            status = self._check(resp).get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise ApiError(f"Instagram のコンテナが {status} になりました: {container_id}")
            if waited >= self.poll_timeout:
                raise ApiError(f"Instagram のコンテナの準備が間に合いませんでした（{status}）")
            self._sleep(self.poll_interval)
            waited += self.poll_interval

    def publish_container(self, container_id: str) -> str:
        resp = self.session.post(
            self._url(f"{self.config.user_id}/media_publish"),
            headers=self._headers(),
            data={"creation_id": container_id},
            timeout=30,
        )
        return self._check(resp)["id"]

    def post_image(self, image_url: str, caption: str) -> str:
        """画像1枚を投稿し、公開されたメディアのIDを返す。"""
        container_id = self.create_image_container(image_url, caption)
        self.wait_until_ready(container_id)
        return self.publish_container(container_id)

    def post_carousel(self, image_urls: list[str], caption: str) -> str:
        """複数枚（2〜20枚）を1つの投稿にし、公開されたメディアのIDを返す。

        途中で失敗しても、公開（media_publish）まで進んでいなければ投稿はされない。
        作りかけのコンテナは公開されないまま、時間がたつと期限切れになる。
        """
        children = []
        for url in image_urls:
            item = self.create_carousel_item(url)
            self.wait_until_ready(item)
            children.append(item)
        container_id = self.create_carousel_container(children, caption)
        self.wait_until_ready(container_id)
        return self.publish_container(container_id)

    def publishing_limit(self) -> dict:
        """24時間あたりの投稿上限の使用状況（上限は公式の最新値を優先）。"""
        resp = self.session.get(
            self._url(f"{self.config.user_id}/content_publishing_limit"),
            headers=self._headers(),
            params={"fields": "quota_usage,config"},
            timeout=30,
        )
        return self._check(resp)


def refresh_token(token: str, session: requests.Session | None = None) -> dict:
    """旧 Instagram ログイン方式の長期トークン（有効60日）を更新する。
    取得後60日以内、かつ24時間以上経ってから。Unified Login のトークン（期限なし）には使わない。"""
    s = session or requests.Session()
    resp = s.get(
        f"{IG_HOSTS['instagram']}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    )
    return InstagramClient._check(resp)
