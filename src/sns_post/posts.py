"""投稿ファイル（Markdown + 先頭のYAML）の読み書き。

例：
---
publish_at: 2026-11-01T12:00:00+09:00   # タイムゾーンつきで書く
targets: [instagram, x]
image_url: https://example.com/photo.jpg  # Instagram に投稿するときは必須（JPEG・公開URL）
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
    x_text: str | None = None
    results: dict[str, str] = field(default_factory=dict)  # 投稿済みの先 → 投稿ID

    @property
    def pending_targets(self) -> list[str]:
        return [t for t in self.targets if t not in self.results]

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


def parse_post(path: str | Path) -> Post:
    path = Path(path)
    meta, body = _split(path.read_text(encoding="utf-8"), path)

    targets = meta.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PostFileError(f"{path.name}: targets に投稿先を書いてください（instagram / x）")
    bad = [t for t in targets if t not in VALID_TARGETS]
    if bad:
        raise PostFileError(f"{path.name}: 未対応の投稿先です: {bad}")

    post = Post(
        path=path,
        publish_at=_parse_time(meta.get("publish_at"), path),
        targets=list(targets),
        text=body,
        image_url=meta.get("image_url"),
        x_text=meta.get("x_text"),
        results=dict(meta.get("results") or {}),
    )
    validate(post)
    return post


def validate(post: Post) -> None:
    name = post.path.name
    if not post.text and "instagram" in post.targets:
        raise PostFileError(f"{name}: 本文が空です")
    if "instagram" in post.targets:
        if not post.image_url or not str(post.image_url).startswith("https://"):
            raise PostFileError(f"{name}: Instagram には https の image_url が必要です")
        host = urlparse(str(post.image_url)).hostname or ""
        if "://" in str(post.image_url)[len("https://"):] or "." not in host:
            raise PostFileError(f"{name}: image_url の形が正しくありません（https:// が重なっていないか確認）: {post.image_url}")
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
