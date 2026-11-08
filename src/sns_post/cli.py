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


def _cmd_refresh_token(args: argparse.Namespace) -> int:
    import os

    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise ConfigError("環境変数が足りません: IG_ACCESS_TOKEN")
    result = refresh_token(token)
    new_token = result.get("access_token")
    if not new_token:
        raise ApiError("新しいトークンが返ってきませんでした")
    # トークンは画面に出さず、.env に直接書き込む（ログや画面共有に残さないため）
    config.update_env("IG_ACCESS_TOKEN", new_token, args.env)
    days = int(result.get("expires_in") or 0) // 86400
    print(f"新しいトークンを {args.env} に書き込みました（有効 約{days}日）")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    if args.host not in ("127.0.0.1", "localhost"):
        print("注意: この画面には認証がありません。同じネットワークの人なら誰でも予約を書き込めます\n")
    serve(Path(args.queue), Path(args.done), args.host, args.port)
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

    w = sub.add_parser("web", help="予約画面をブラウザで開けるようにする（投稿はしない）")
    w.add_argument("--queue", default="posts/queue")
    w.add_argument("--done", default="posts/done")
    w.add_argument("--host", default="127.0.0.1", help="既定は自分のPCだけ。スマホから開くときは 0.0.0.0（認証なしに注意）")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=_cmd_web)

    sub.add_parser("limit", help="Instagram の投稿上限の使用状況").set_defaults(func=_cmd_limit)
    t = sub.add_parser("refresh-token", help="Instagram のトークンを更新し、.env に書き込む")
    t.add_argument("--env", default=".env", help="書き込む .env のパス")
    t.set_defaults(func=_cmd_refresh_token)
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
