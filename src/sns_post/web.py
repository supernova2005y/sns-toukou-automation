"""予約画面（ローカルのWebサーバー）。

`sns-post web` で起動し、ブラウザで開く。標準ライブラリだけで動く。

- 画面からは投稿ファイルを `posts/queue/` に書き出すだけ。**ここから投稿はしない**
  （実際の投稿は今までどおり `sns-post run-due --publish`）
- 既定では自分のPC（127.0.0.1）からしか開けない。認証がないので、外に公開しない
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from .errors import PostFileError
from .posts import VALID_TARGETS, Post, _parse_time, parse_post, save_post, validate

MAX_BODY = 64 * 1024
EXCERPT_LEN = 40


def _summary(post: Post, status: str) -> dict:
    return {
        "name": post.path.name,
        "status": status,
        "publish_at": post.publish_at.isoformat(),
        "targets": post.targets,
        "excerpt": post.text.replace("\n", " ")[:EXCERPT_LEN],
        "image_url": post.image_url,
        "posted": sorted(post.results),
    }


def list_posts(queue: Path, done: Path, now: datetime) -> list[dict]:
    """一覧に出す投稿。予約中（時刻を過ぎたものは overdue）→ 済み の順。読めないファイルは error で返す。"""
    pending: list[dict] = []
    finished: list[dict] = []
    for directory, is_done in ((queue, False), (done, True)):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                post = parse_post(path)
            except PostFileError as e:
                pending.append({"name": path.name, "status": "error", "error": str(e)})
                continue
            if is_done or not post.pending_targets:
                finished.append(_summary(post, "done"))
            elif post.publish_at <= now:
                pending.append(_summary(post, "overdue"))
            else:
                pending.append(_summary(post, "scheduled"))
    pending.sort(key=lambda p: p.get("publish_at", ""))
    finished.sort(key=lambda p: p["publish_at"], reverse=True)
    return pending + finished


def create_post(data: dict, queue: Path, now: datetime) -> Post:
    """画面から送られた内容を検査して、投稿ファイルを1つ書き出す。"""
    if not isinstance(data, dict):
        raise PostFileError("送られた内容の形が正しくありません")
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets or any(t not in VALID_TARGETS for t in targets):
        raise PostFileError("投稿先（Instagram / X）を1つ以上選んでください")
    text = str(data.get("text") or "").strip()
    if not text:
        raise PostFileError("本文を入力してください")
    placeholder = Path("新しい投稿.md")
    publish_at = _parse_time(data.get("publish_at"), placeholder)
    if publish_at <= now:
        raise PostFileError("投稿する日時が過ぎています。これから先の日時にしてください")
    image_url = str(data.get("image_url") or "").strip() or None
    x_text = str(data.get("x_text") or "").strip() or None

    post = Post(
        path=placeholder,
        publish_at=publish_at,
        targets=list(dict.fromkeys(targets)),
        text=text,
        image_url=image_url,
        x_text=x_text,
    )
    validate(post)

    queue.mkdir(parents=True, exist_ok=True)
    stem = publish_at.strftime("%Y-%m-%d_%H%M")
    path = queue / f"{stem}.md"
    n = 2
    while path.exists():
        path = queue / f"{stem}_{n}.md"
        n += 1
    post.path = path
    save_post(post)
    return post


def _index_html() -> bytes:
    return resources.files("sns_post").joinpath("static/index.html").read_bytes()


def make_handler(queue: Path, done: Path, clock=lambda: datetime.now(timezone.utc)):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sns-post"

        def log_message(self, fmt, *args):  # 本文などを端末に出さない
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, _index_html(), "text/html; charset=utf-8")
            elif path == "/api/posts":
                self._json(HTTPStatus.OK, {"posts": list_posts(queue, done, clock())})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/posts":
                self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})
                return
            # 別のサイトから勝手に予約を書き込まれないよう、JSON と同じ出どころからの送信だけ受け付ける
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON で送ってください"})
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "この画面以外からは予約できません"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "内容が大きすぎます"})
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                post = create_post(data, queue, clock())
            except (ValueError, PostFileError) as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e).replace("新しい投稿.md: ", "")})
                return
            self._json(HTTPStatus.CREATED, {"post": _summary(post, "scheduled")})

    return Handler


def serve(queue: Path, done: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(queue, done))
    shown = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"予約画面: http://{shown}:{server.server_address[1]}/  （止めるときは Ctrl+C）")
    print("ここから投稿はしません。投稿は sns-post run-due --publish で行います")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n止めました")
    finally:
        server.server_close()
