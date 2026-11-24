"""投稿ファイル（Markdown + 先頭のYAML）の読み書き。

例：
---
publish_at: 2026-11-01T12:00:00+09:00   # タイムゾーンつきで書く
targets: [instagram, x]
image_url: https://example.com/photo.jpg  # Instagram には image_url か image_key のどちらかが必須（JPEG）
image_key: images/xxxx.jpg                # 画像の置き場所（STORAGE_*）に上げた画像。投稿の直前に期限つきURLにする
images: [https://example.com/1.jpg, images/yyyy.jpg]  # 複数枚（カルーセル、2〜20枚）。URL と image_key を混ぜてよい
x_text: Xだけ文面を変えたいときに書く（任意）
---
本文（Instagram のキャプション。x_text がなければ X もこの文面）
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .errors import PostFileError

VALID_TARGETS = ("instagram", "x")
IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30
IG_CAROUSEL_MIN = 2   # 1枚なら image_url / image_key を使う（公式に下限の記載はない。このツールの決まり）
IG_CAROUSEL_MAX = 20  # 公式：カルーセルは20枚まで（docs/セットアップ.md の出典）
X_TEXT_MAX = 280  # Xの「重みつき文字数」の上限。数え方は x_weighted_length()

# X の数え方（twitter-text v3 の設定）。この範囲の文字は1、ほか（日本語・絵文字など）は2と数える
_X_WEIGHT1_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
_X_URL = re.compile(r"https?://\S+", re.IGNORECASE)
X_URL_LENGTH = 23  # URL は長さにかかわらず23文字と数える


def x_weighted_length(text: str) -> int:
    """X の重みつき文字数（目安）。日本語は1文字=2、URLは23。

    絵文字の結合（ZWJ）は厳密には1つの絵文字=2だが、ここでは文字ごとに数えるので多めに出る（安全側）。
    """
    text = unicodedata.normalize("NFC", text)
    urls = _X_URL.findall(text)
    rest = _X_URL.sub("", text)
    total = X_URL_LENGTH * len(urls)
    for ch in rest:
        cp = ord(ch)
        total += 1 if any(lo <= cp <= hi for lo, hi in _X_WEIGHT1_RANGES) else 2
    return total


@dataclass
class Post:
    path: Path
    publish_at: datetime
    targets: list[str]
    text: str
    image_url: str | None = None
    image_key: str | None = None
    x_text: str | None = None
    images: list[str] = field(default_factory=list)  # 複数枚（カルーセル）。各要素は https のURLか image_key
    results: dict[str, str] = field(default_factory=dict)  # 投稿済みの先 → 投稿ID

    @property
    def pending_targets(self) -> list[str]:
        return [t for t in self.targets if t not in self.results]

    @property
    def is_carousel(self) -> bool:
        return bool(self.images)

    @property
    def needs_store(self) -> bool:
        """画像の置き場所（image_key → 期限つきURL）が要るか。"""
        if self.images:
            return any(not is_image_url(v) for v in self.images)
        return bool(self.image_key and not self.image_url)

    def text_for(self, target: str) -> str:
        if target == "x" and self.x_text:
            return self.x_text
        return self.text


def _parse_time(value: object, path: Path) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as e:
            raise PostFileError(f"{path.name}: publish_at の形式が正しくありません: {value}") from e
    else:
        raise PostFileError(f"{path.name}: publish_at がありません")
    if dt.tzinfo is None:
        raise PostFileError(f"{path.name}: publish_at にタイムゾーンをつけてください（例 +09:00）")
    return dt


def _split(raw: str, path: Path) -> tuple[dict, str]:
    if not raw.startswith("---"):
        raise PostFileError(f"{path.name}: 先頭に --- で囲んだ設定がありません")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise PostFileError(f"{path.name}: 設定の終わりの --- がありません")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise PostFileError(f"{path.name}: 設定の書き方が正しくありません")
    return meta, parts[2].strip()


def _parse_images(value: object, path: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PostFileError(f"{path.name}: images は画像のURLか image_key を1行ずつ並べてください（- https://… の形）")
    return [v.strip() for v in value]


def is_image_url(value: str) -> bool:
    """images の1つが URL か（違えば image_key として扱う）。"""
    return "://" in value


def _check_image_url(name: str, url: str) -> None:
    if not url.startswith("https://"):
        raise PostFileError(f"{name}: 画像のURLは https:// で始めてください: {url}")
    host = urlparse(url).hostname or ""
    if "://" in url[len("https://"):] or "." not in host:
        raise PostFileError(f"{name}: image_url の形が正しくありません（https:// が重なっていないか確認）: {url}")


def _check_image_key(name: str, key: str) -> None:
    if key.startswith("/") or ".." in key or not re.fullmatch(r"[A-Za-z0-9._/-]+", key):
        raise PostFileError(f"{name}: image_key の形が正しくありません: {key}")


def parse_post(path: str | Path) -> Post:
    path = Path(path)
    meta, body = _split(path.read_text(encoding="utf-8"), path)

    targets = meta.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PostFileError(f"{path.name}: targets に投稿先を書いてください（instagram / x）")
    bad = [t for t in targets if t not in VALID_TARGETS]
    if bad:
        raise PostFileError(f"{path.name}: 未対応の投稿先です: {bad}")
    dup = sorted({t for t in targets if targets.count(t) > 1})
    if dup:  # 重なっていると、同じ先に2回投稿してしまう
        raise PostFileError(f"{path.name}: 投稿先が重複しています: {dup}")

    post = Post(
        path=path,
        publish_at=_parse_time(meta.get("publish_at"), path),
        targets=list(targets),
        text=body,
        image_url=meta.get("image_url"),
        image_key=meta.get("image_key"),
        x_text=meta.get("x_text"),
        images=_parse_images(meta.get("images"), path),
        results=dict(meta.get("results") or {}),
    )
    validate(post)
    return post


def validate(post: Post) -> None:
    name = post.path.name
    if not post.text and "instagram" in post.targets:
        raise PostFileError(f"{name}: 本文が空です")
    if "instagram" in post.targets and post.images:
        if post.image_url or post.image_key:
            raise PostFileError(f"{name}: images（複数枚）と image_url / image_key は一緒に書けません。どちらか一方にしてください")
        if not IG_CAROUSEL_MIN <= len(post.images) <= IG_CAROUSEL_MAX:
            raise PostFileError(
                f"{name}: images は {IG_CAROUSEL_MIN}〜{IG_CAROUSEL_MAX} 枚にしてください（今 {len(post.images)} 枚。1枚なら image_url を使います）"
            )
        for v in post.images:
            if is_image_url(v):
                _check_image_url(name, v)
            else:
                _check_image_key(name, v)
    elif "instagram" in post.targets:
        if post.image_key:
            _check_image_key(name, str(post.image_key))
        if not post.image_key and (not post.image_url or not str(post.image_url).startswith("https://")):
            raise PostFileError(f"{name}: Instagram には https の image_url か、画像の置き場所の image_key が必要です")
        if post.image_url:
            _check_image_url(name, str(post.image_url))
    if "instagram" in post.targets:
        if len(post.text) > IG_CAPTION_MAX:
            raise PostFileError(f"{name}: キャプションが {IG_CAPTION_MAX} 文字を超えています")
        if len(re.findall(r"#\S+", post.text)) > IG_HASHTAG_MAX:
            raise PostFileError(f"{name}: ハッシュタグが {IG_HASHTAG_MAX} 個を超えています")
    if "x" in post.targets:
        n = x_weighted_length(post.text_for("x"))
        if n > X_TEXT_MAX:
            raise PostFileError(
                f"{name}: Xの文面が長すぎます（{n}/{X_TEXT_MAX}。日本語は1文字=2、URLは23で数えます。x_text で短くできます）"
            )


def save_post(post: Post) -> None:
    """投稿ずみの先（results）をファイルに書き戻す。再実行で二重投稿しないための記録。"""
    meta: dict = {
        "publish_at": post.publish_at,
        "targets": post.targets,
    }
    if post.image_url:
        meta["image_url"] = post.image_url
    if post.image_key:
        meta["image_key"] = post.image_key
    if post.images:
        meta["images"] = post.images
    if post.x_text:
        meta["x_text"] = post.x_text
    if post.results:
        meta["results"] = post.results
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    post.path.write_text(f"---\n{front}\n---\n{post.text}\n", encoding="utf-8")


def load_posts(directory: str | Path) -> list[Post]:
    d = Path(directory)
    if not d.exists():
        return []
    return [parse_post(p) for p in sorted(d.glob("*.md")) if not p.name.startswith("_")]
