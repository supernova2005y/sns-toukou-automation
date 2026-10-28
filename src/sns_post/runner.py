"""時刻が来た投稿を実行する。

- 既定は下見（dry-run）。publish=True のときだけ実際に投稿する
- 投稿に成功した先は、その都度ファイルに記録する。途中で失敗しても、再実行で二重投稿しない
- すべての先に投稿できたら done/ に移す
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .errors import ApiError
from .posts import Post, save_post
from .x import estimate_cost_usd


class _InstagramLike(Protocol):
    def post_image(self, image_url: str, caption: str) -> str: ...


class _XLike(Protocol):
    def post_text(self, text: str) -> str: ...


def run_due(
    posts: list[Post],
    now: datetime,
    publish: bool,
    done_dir: Path,
    instagram: _InstagramLike | None = None,
    x: _XLike | None = None,
) -> list[str]:
    log: list[str] = []
    for post in sorted(posts, key=lambda p: p.publish_at):
        if post.publish_at > now:
            continue
        for target in post.pending_targets:
            label = f"{post.path.name} → {target}"
            if not publish:
                extra = f"（X料金の目安 約{estimate_cost_usd(post.text_for('x')):.3f}ドル）" if target == "x" else ""
                log.append(f"[下見] {label}{extra}\n    {post.text_for(target)[:60]!r}")
                continue
            client = instagram if target == "instagram" else x
            if client is None:
                log.append(f"[スキップ] {label}: 認証情報が設定されていません")
                continue
            try:
                if target == "instagram":
                    media_id = client.post_image(post.image_url or "", post.text_for(target))
                else:
                    media_id = client.post_text(post.text_for(target))
            except ApiError as e:
                log.append(f"[失敗] {label}: {e}")
                continue
            post.results[target] = media_id
            save_post(post)  # 成功した先は、すぐ記録する
            log.append(f"[投稿] {label} id={media_id}")
        if publish and not post.pending_targets:
            done_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(post.path), str(done_dir / post.path.name))
            log.append(f"[完了] {post.path.name} を done/ に移しました")
    if not log:
        log.append("時刻が来た投稿はありません")
    return log
