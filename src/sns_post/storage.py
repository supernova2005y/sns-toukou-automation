"""画像の置き場所（クラウドストレージ）。

- Instagram は、公開されている JPEG の URL からしか画像を取れない（Meta のサーバーが取りに来る）
- 画像は非公開のバケットに置き、投稿する瞬間だけの期限つきURL（署名付きURL）を作って Meta に渡す
- 保存先は差し替えられるよう Protocol にしてある。いまの実装は S3 互換（Cloudflare R2・AWS S3 など）
- boto3 は使うときだけ読み込む（`pip install -e ".[storage]"`）。テストでは偽のクライアントを渡す
"""
from __future__ import annotations

import uuid
from typing import Protocol

from .config import StorageConfig
from .errors import ApiError, ConfigError, PostFileError

# アプリ側の安全のための上限。Instagram 側の上限は公式で確認していない（未確認）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_EXPIRES = 3600  # 署名付きURLの有効秒数。Meta が署名付きURLで取れるかは実アカウントで未確認


class ImageStore(Protocol):
    def upload(self, data: bytes) -> str:
        """画像を保存して、保存先の名前（キー）を返す。"""

    def signed_url(self, key: str, expires: int = DEFAULT_EXPIRES) -> str:
        """キーから、期限つきの読み取りURLを作る。"""


def check_jpeg(data: bytes) -> None:
    """JPEG かどうかを中身で確かめる（拡張子は信用しない）。"""
    if not data:
        raise PostFileError("画像が空です")
    if len(data) > MAX_IMAGE_BYTES:
        raise PostFileError(f"画像が大きすぎます（{len(data) // 1024 // 1024}MB。上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB）")
    if not data.startswith(b"\xff\xd8\xff"):
        raise PostFileError("JPEG の画像だけ使えます（Instagram の投稿は JPEG のみ）")


class S3ImageStore:
    def __init__(self, cfg: StorageConfig, client=None) -> None:
        self.cfg = cfg
        if client is None:
            try:
                import boto3
            except ImportError as e:
                raise ConfigError('boto3 が入っていません。pip install -e ".[storage]" を実行してください') from e
            client = boto3.client(
                "s3",
                endpoint_url=cfg.endpoint_url,
                region_name=cfg.region,
                aws_access_key_id=cfg.access_key_id,
                aws_secret_access_key=cfg.secret_access_key,
            )
        self.client = client

    def upload(self, data: bytes) -> str:
        check_jpeg(data)
        key = f"images/{uuid.uuid4().hex}.jpg"  # 元のファイル名は使わない（名前から中身が分からないように）
        try:
            self.client.put_object(Bucket=self.cfg.bucket, Key=key, Body=data, ContentType="image/jpeg")
        except Exception as e:  # botocore の例外をまとめる。鍵は文面に入らない
            raise ApiError(f"画像を保存できませんでした: {type(e).__name__}") from e
        return key

    def signed_url(self, key: str, expires: int = DEFAULT_EXPIRES) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.cfg.bucket, "Key": key}, ExpiresIn=expires
            )
        except Exception as e:
            raise ApiError(f"画像のURLを作れませんでした: {type(e).__name__}") from e


def store_from_env() -> S3ImageStore | None:
    from .config import storage_from_env

    cfg = storage_from_env()
    return S3ImageStore(cfg) if cfg else None
