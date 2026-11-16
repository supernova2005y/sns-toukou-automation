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
from .runner import NOTHING_DUE, run_due
from .x import XClient


def _cmd_check(args: argparse.Namespace) -> int:
    posts = load_posts(args.queue)
    print(f"{len(posts)} 件の予約投稿を読み込みました（書き方に問題なし）")
    for p in sorted(posts, key=lambda p: p.publish_at):
        done = ",".join(p.results) or "-"
        print(f"  {p.publish_at.isoformat()}  {','.join(p.targets)}  投稿ずみ:{done}  {p.path.name}")
    return 0


def _append_log(path: str, lines: list[str], now: datetime) -> None:
    """定期実行の記録。画面がないので、何が起きたかをファイルに残す（本文・トークンは書かない）。"""
    lines = [line for line in lines if line != NOTHING_DUE]  # 何もない回は書かない（記録が埋もれないように）
    if not lines:
        return
    log = Path(path)
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone().isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{stamp} {line.splitlines()[0]}\n")


def _cmd_run_due(args: argparse.Namespace) -> int:
    if not args.log:
        return _run_due(args)
    try:
        return _run_due(args)
    except (ConfigError, PostFileError, ApiError) as e:
        _append_log(args.log, [f"[エラー] {e}"], datetime.now(timezone.utc))
        raise


def _run_due(args: argparse.Namespace) -> int:
    posts = load_posts(args.queue)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    ig = x = store = None
    if args.publish:
        needed = {t for p in posts if p.publish_at <= now for t in p.pending_targets}
        if "instagram" in needed:
            ig = InstagramClient(config.instagram_from_env())
            if any(p.needs_store for p in posts):
                from .storage import store_from_env

                store = store_from_env()
        if "x" in needed:
            x = XClient(config.x_from_env())
    else:
        print("下見モードです。実際に投稿するときは --publish をつけてください\n")
    if args.publish:
        from .publisher import BusyError, publish_lock

        try:
            with publish_lock(Path(args.queue).parent):
                posts = load_posts(args.queue)  # ロックを取ってから読み直す（画面で投稿済みのものを除くため）
                lines = run_due(posts, now, True, Path(args.done), ig, x, store)
        except BusyError as e:
            raise ApiError(str(e)) from e
    else:
        lines = run_due(posts, now, False, Path(args.done), ig, x)
    for line in lines:
        print(line)
    if args.log:
        _append_log(args.log, lines, now)
    return 0


def _cmd_draft_x(args: argparse.Namespace, model=None) -> int:
    from .draft import draft_x_text
    from .posts import parse_post, save_post

    path = Path(args.file)
    if not path.exists() and not path.is_absolute():
        path = Path(args.queue) / args.file  # 予約のファイル名だけでもよい
    if not path.exists():
        raise PostFileError(f"投稿ファイルが見つかりません: {args.file}")
    post = parse_post(path)
    if "x" not in post.targets:
        raise PostFileError(f"{path.name}: targets に x がないので、X の文面は要りません")
    if post.results:
        # 予約画面と同じ決まり：一部でも投稿済みの予約は書き換えない（記録の食い違いを防ぐ）
        raise PostFileError(f"{path.name}: 一部の投稿先に投稿済みなので、文面は変えません")
    before = path.read_bytes()
    if model is None:
        from .llm import model_from_env

        model = model_from_env()
    print(f"{model.label} で X 用の文面の下書きを作ります（投稿はしません）\n")
    draft = draft_x_text(post.text, model)
    print(draft.text)
    print(f"\n--- X の文字数 {draft.length}/280（{draft.tries} 回目で収まりました）")
    for w in draft.warnings:
        print(f"注意: {w}")
    print("日時・場所・値段などが本文と同じかは、自分の目で確かめてください")
    if not args.write:
        print("\nまだファイルには書いていません。使うときは --write をつけてもう一度（x_text に書き込みます）")
        return 0
    from .publisher import BusyError, publish_lock

    # 下書きを作っているあいだに投稿・変更されていたら書かない（投稿中の目印を取ってから確かめる）
    try:
        with publish_lock(path.parent.parent):
            if not path.exists() or path.read_bytes() != before:
                raise PostFileError(f"{path.name}: 下書きを作っているあいだにファイルが変わったので、書き込みませんでした")
            if post.x_text:
                print(f"\nいまの x_text を置き換えます（前の文面: {post.x_text}）")
            post.x_text = draft.text
            save_post(post)
    except BusyError as e:
        raise ApiError(str(e)) from e
    print(f"{path.name} の x_text に書き込みました")
    return 0


def _cmd_limit(_: argparse.Namespace) -> int:
    client = InstagramClient(config.instagram_from_env())
    print(json.dumps(client.publishing_limit(), ensure_ascii=False, indent=2))
    return 0


def _cmd_refresh_token(args: argparse.Namespace) -> int:
    import os

    login = (os.environ.get("IG_LOGIN") or "unified").strip().lower()
    if login != "instagram":
        print("Unified Login のトークン（システムユーザートークン）は期限がないので、更新は不要です")
        return 0
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
    serve(Path(args.queue), Path(args.done), args.host, args.port, publish=args.publish)
    return 0


def _cmd_schedule(args: argparse.Namespace, run=None) -> int:
    from . import schedule

    run = run or schedule.run_command
    windows = schedule.is_windows()
    if args.remove or args.status:
        if not windows:
            print("Windows 以外では、crontab -e で該当の行を確かめる・消してください")
            return 0
        result = run(schedule.windows_delete_args() if args.remove else schedule.windows_query_args())
        if result.returncode != 0:
            print(f"見つからないか、操作できませんでした: {(result.stderr or result.stdout).strip()}")
            return 1
        print(f"定期実行（{schedule.TASK_NAME}）を消しました" if args.remove else result.stdout.strip())
        return 0

    publish = not args.dry_run
    command = schedule.task_command(Path.cwd(), publish=publish, windows=windows)
    what = "時刻が来た予約を実際に投稿します" if publish else "下見だけします（投稿しない。ログに [下見] が出るかを確かめる用）"
    print(f"{args.every}分ごとに、{what}")
    if not windows:
        print("crontab -e で、次の1行を足してください（自動では書き込みません）:")
        print(schedule.cron_line(command, args.every))
        return 0
    create = schedule.windows_create_args(command, args.every)
    print(f"  動かすコマンド: {create[create.index('/TR') + 1]}")
    print(f"  結果の記録: {Path.cwd() / schedule.DEFAULT_LOG}")
    if not args.install:
        print("まだ登録していません。登録するときは --install をつけてください")
        return 0
    result = run(create)
    if result.returncode != 0:
        print(f"登録できませんでした: {(result.stderr or result.stdout).strip()}")
        return 1
    print(f"タスクスケジューラに「{schedule.TASK_NAME}」を登録しました。消すときは sns-post schedule --remove")
    print("PCが動いていてログインしている間だけ動きます")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sns-post", description="Instagram と X への予約投稿（公式APIのみ）")
    p.add_argument(
        "-C", dest="workdir", metavar="フォルダ",
        help="このフォルダで動かす（.env と posts/ の場所）。タスクスケジューラなど、ほかの場所から呼ぶとき用",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="予約投稿ファイルの書き方を確認する")
    c.add_argument("--queue", default="posts/queue")
    c.set_defaults(func=_cmd_check)

    r = sub.add_parser("run-due", help="時刻が来た投稿を実行する（既定は下見）")
    r.add_argument("--queue", default="posts/queue")
    r.add_argument("--done", default="posts/done")
    r.add_argument("--publish", action="store_true", help="実際に投稿する")
    r.add_argument("--now", help="現在時刻を指定（テスト用。タイムゾーンつきISO形式）")
    r.add_argument("--log", metavar="ファイル", help="結果を日時つきで追記する（定期実行用。例 posts/run.log）")
    r.set_defaults(func=_cmd_run_due)

    w = sub.add_parser("web", help="予約画面をブラウザで開けるようにする（--publish なしなら投稿はしない）")
    w.add_argument("--queue", default="posts/queue")
    w.add_argument("--done", default="posts/done")
    w.add_argument("--host", default="127.0.0.1", help="既定は自分のPCだけ。スマホから開くときは 0.0.0.0（認証なしに注意）")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--publish", action="store_true", help="画面から投稿する（今すぐ投稿・時刻が来たら自動投稿）。自分のPCで開くときだけ")
    w.set_defaults(func=_cmd_web)

    s = sub.add_parser("schedule", help="定期実行（N分ごとに run-due --publish）を登録する。既定は表示だけ")
    s.add_argument("--every", type=int, default=10, help="何分ごとに動かすか（既定 10）")
    s.add_argument("--dry-run", action="store_true", help="--publish なし（下見だけ）で登録する。まず試すとき用")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="実際に登録する（Windows のタスクスケジューラ）")
    g.add_argument("--remove", action="store_true", help="登録を消す")
    g.add_argument("--status", action="store_true", help="登録の状態を見る")
    s.set_defaults(func=_cmd_schedule)

    d = sub.add_parser("draft-x", help="生成AIで X 用の文面（x_text）の下書きを作る。投稿はしない")
    d.add_argument("file", help="投稿ファイル（パス、または posts/queue の中のファイル名）")
    d.add_argument("--queue", default="posts/queue")
    d.add_argument("--write", action="store_true", help="下書きをファイルの x_text に書き込む")
    d.set_defaults(func=_cmd_draft_x)

    sub.add_parser("limit", help="Instagram の投稿上限の使用状況").set_defaults(func=_cmd_limit)
    t = sub.add_parser("refresh-token", help="Instagram のトークンを更新し、.env に書き込む（旧 Instagram ログイン方式のみ）")
    t.add_argument("--env", default=".env", help="書き込む .env のパス")
    t.set_defaults(func=_cmd_refresh_token)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workdir:
        import os

        try:
            os.chdir(args.workdir)
        except OSError as e:
            print(f"エラー: -C のフォルダに移れません: {args.workdir}（{e.strerror}）", file=sys.stderr)
            return 1
    config.load_env()
    try:
        return args.func(args)
    except (ConfigError, PostFileError, ApiError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
