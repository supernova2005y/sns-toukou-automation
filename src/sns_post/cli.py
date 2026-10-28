"""コマンドライン。使い方は README.md を参照。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .errors import ApiError, ConfigError, PostFileError
from .instagram import InstagramClient, refresh_token
from .posts import load_posts
from .runner import run_due
from .x import XClient


def _cmd_check(args: argparse.Namespace) -> int:
    posts = load_posts(args.queue)
    print(f"{len(posts)} 件の予約投稿を読み込みました（書き方に問題なし）")
    for p in sorted(posts, key=lambda p: p.publish_at):
        done = ",".join(p.results) or "-"
        print(f"  {p.publish_at.isoformat()}  {','.join(p.targets)}  投稿ずみ:{done}  {p.path.name}")
    return 0


def _cmd_run_due(args: argparse.Namespace) -> int:
    posts = load_posts(args.queue)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    ig = x = None
    if args.publish:
        needed = {t for p in posts if p.publish_at <= now for t in p.pending_targets}
        if "instagram" in needed:
            ig = InstagramClient(config.instagram_from_env())
        if "x" in needed:
            x = XClient(config.x_from_env())
    else:
        print("下見モードです。実際に投稿するときは --publish をつけてください\n")
    for line in run_due(posts, now, args.publish, Path(args.done), ig, x):
        print(line)
    return 0


def _cmd_limit(_: argparse.Namespace) -> int:
    client = InstagramClient(config.instagram_from_env())
    print(json.dumps(client.publishing_limit(), ensure_ascii=False, indent=2))
    return 0


def _cmd_refresh_token(_: argparse.Namespace) -> int:
    import os

    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise ConfigError("環境変数が足りません: IG_ACCESS_TOKEN")
    result = refresh_token(token)
    print("新しいトークンを取得しました。.env の IG_ACCESS_TOKEN を書き換えてください（画面やログに残さないこと）")
    print(f"有効期間（秒）: {result.get('expires_in')}")
    print(result.get("access_token"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sns-post", description="Instagram と X への予約投稿（公式APIのみ）")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="予約投稿ファイルの書き方を確認する")
    c.add_argument("--queue", default="posts/queue")
    c.set_defaults(func=_cmd_check)

    r = sub.add_parser("run-due", help="時刻が来た投稿を実行する（既定は下見）")
    r.add_argument("--queue", default="posts/queue")
    r.add_argument("--done", default="posts/done")
    r.add_argument("--publish", action="store_true", help="実際に投稿する")
    r.add_argument("--now", help="現在時刻を指定（テスト用。タイムゾーンつきISO形式）")
    r.set_defaults(func=_cmd_run_due)

    sub.add_parser("limit", help="Instagram の投稿上限の使用状況").set_defaults(func=_cmd_limit)
    sub.add_parser("refresh-token", help="Instagram のトークンを更新する").set_defaults(func=_cmd_refresh_token)
    return p


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, PostFileError, ApiError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
