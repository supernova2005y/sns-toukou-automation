"""予約画面（ローカルのWebサーバー）。

`sns-post web` で起動し、ブラウザで開く。標準ライブラリだけで動く。

- 既定（`sns-post web`）では、投稿ファイルを `posts/queue/` に書き出す・書き換える・取り消すだけで、**投稿はしない**
- `sns-post web --publish` のときだけ、「今すぐ投稿」と、時刻が来た予約の自動投稿（1分ごと）をする。
  投稿の手順は `run-due --publish` と同じ（publisher.py）
- 取り消した予約は消さずに `posts/cancelled/` へ移す（間違えても戻せるように）
- どこか1つにでも投稿済みの予約は、編集も取り消しもできない（二重投稿・記録の食い違いを防ぐため）
- 既定では自分のPC（127.0.0.1）からしか開けない。認証がないので、外に公開しない
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import ApiError, ConfigError, PostFileError
from .posts import VALID_TARGETS, Post, _parse_time, parse_post, save_post, validate
from .publisher import BusyError, Publisher, start_auto
from .storage import MAX_IMAGE_BYTES

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
        "editable": status in ("scheduled", "overdue") and not post.results,
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


def _build_post(data: dict, now: datetime) -> Post:
    """画面から送られた内容を検査して Post にする（ファイルにはまだ書かない）。"""
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
    image_key = str(data.get("image_key") or "").strip() or None
    if image_url and image_key:
        image_key = None  # URL を入れたら、そちらを使う（runner と同じ優先順位）
    x_text = str(data.get("x_text") or "").strip() or None
    targets = list(dict.fromkeys(targets))
    post = Post(
        path=placeholder,
        publish_at=publish_at,
        targets=targets,
        text=text,
        image_url=image_url if "instagram" in targets else None,
        image_key=image_key if "instagram" in targets else None,
        x_text=x_text if "x" in targets else None,
    )
    validate(post)
    return post


def _free_path(queue: Path, publish_at: datetime) -> Path:
    stem = publish_at.strftime("%Y-%m-%d_%H%M")
    path = queue / f"{stem}.md"
    n = 2
    while path.exists():
        path = queue / f"{stem}_{n}.md"
        n += 1
    return path


def file_version(path: Path) -> str:
    """ファイルの中身から作る目印。画面で開いたあとに書き換わっていないかの確認に使う。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def create_post(data: dict, queue: Path, now: datetime) -> Post:
    """画面から送られた内容を検査して、投稿ファイルを1つ書き出す。"""
    post = _build_post(data, now)
    queue.mkdir(parents=True, exist_ok=True)
    post.path = _free_path(queue, post.publish_at)
    save_post(post)
    return post


def _queue_path(name: str, queue: Path) -> Path:
    """予約中フォルダの中のファイルだけを返す（../ などでほかの場所を触らせない）。"""
    if not isinstance(name, str) or not name.endswith(".md") or name.startswith(("_", ".")) or "/" in name or "\\" in name:
        raise LookupError("その予約は見つかりません")
    path = queue / name
    if path.resolve().parent != queue.resolve() or not path.is_file():
        raise LookupError("その予約は見つかりません")
    return path


def _find_editable(name: str, queue: Path, version: object) -> Post:
    """予約中のファイルを名前で探す。投稿済みの先があるもの・開いたあとに変わったものは断る。"""
    path = _queue_path(name, queue)
    if version != file_version(path):
        raise PostFileError("ほかの場所で予約が変わりました。画面を読み込み直してください")
    post = parse_post(path)
    if post.results:
        raise PostFileError("この予約は一部の投稿先に投稿済みなので、変更・取り消しはできません")
    return post


def read_post(name: str, queue: Path) -> dict:
    """編集画面に出す内容。"""
    path = _queue_path(name, queue)
    post = parse_post(path)
    return {
        "name": name,
        "version": file_version(path),
        "publish_at": post.publish_at.isoformat(),
        "targets": post.targets,
        "text": post.text,
        "image_url": post.image_url,
        "image_key": post.image_key,
        "x_text": post.x_text,
        "editable": not post.results,
    }


def update_post(name: str, data: dict, queue: Path, now: datetime) -> Post:
    """予約を書き換える。日時が変わったらファイル名も変える。"""
    old = _find_editable(name, queue, data.get("version") if isinstance(data, dict) else None)
    post = _build_post(data, now)
    same_time = old.publish_at == post.publish_at
    post.path = old.path if same_time else _free_path(queue, post.publish_at)
    save_post(post)
    if post.path != old.path:
        old.path.unlink()
    return post


def cancel_post(name: str, version: object, queue: Path, cancelled: Path) -> Path:
    """予約を取り消す。消さずに cancelled/ へ移す。"""
    post = _find_editable(name, queue, version)
    cancelled.mkdir(parents=True, exist_ok=True)
    dest = cancelled / post.path.name
    n = 2
    while dest.exists():
        dest = cancelled / f"{post.path.stem}_{n}.md"
        n += 1
    post.path.rename(dest)
    return dest


def _index_html() -> bytes:
    return resources.files("sns_post").joinpath("static/index.html").read_bytes()


def publish_post(name: str, version: object, queue: Path, publisher: Publisher | None, now: datetime) -> dict:
    """画面の「今すぐ投稿」。--publish で起動していなければ断る。"""
    if publisher is None:
        raise PermissionError("いまは下見モードです。投稿するには sns-post web --publish で起動し直してください")
    path = _queue_path(name, queue)
    if version != file_version(path):
        raise PostFileError("ほかの場所で予約が変わりました。画面を読み込み直してください")
    lines = publisher.publish_now(path, now)
    ok = bool(lines) and not any(line.startswith(("[失敗]", "[スキップ]")) for line in lines)
    return {"ok": ok, "log": lines}


def make_handler(
    queue: Path,
    done: Path,
    clock=lambda: datetime.now(timezone.utc),
    cancelled: Path | None = None,
    publisher: Publisher | None = None,
    image_store=None,
):
    cancelled = cancelled or queue.parent / "cancelled"

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

        def _route(self) -> tuple[str | None, str | None]:
            """/api/posts → (None, None)、/api/posts/<name> → (name, None)、/api/posts/<name>/cancel → (name, "cancel")"""
            parts = urlsplit(self.path).path.split("/")
            if parts[:3] != ["", "api", "posts"]:
                return "", None
            rest = [unquote(p) for p in parts[3:] if p]
            if not rest:
                return None, None
            if len(rest) == 1:
                return rest[0], None
            if len(rest) == 2:
                return rest[0], rest[1]
            return "", None

        def _read_json(self) -> dict | None:
            """書き込みの受け付け。別のサイトから勝手に書き込まれないよう、JSON と同じ出どころだけ通す。"""
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON で送ってください"})
                return None
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "この画面以外からは変更できません"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "内容が大きすぎます"})
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "送られた内容の形が正しくありません"})
                return None

        def _run(self, action) -> None:
            try:
                status, obj = action()
            except LookupError as e:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'\"")})
                return
            except PermissionError as e:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(e)})
                return
            except ApiError as e:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
                return
            except BusyError as e:
                self._json(HTTPStatus.CONFLICT, {"error": str(e)})
                return
            except (ValueError, PostFileError) as e:
                msg = str(e).replace("新しい投稿.md: ", "")
                conflict = "変わりました" in msg or "投稿済み" in msg
                self._json(HTTPStatus.CONFLICT if conflict else HTTPStatus.BAD_REQUEST, {"error": msg})
                return
            self._json(status, obj)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, _index_html(), "text/html; charset=utf-8")
                return
            if path == "/api/log":
                self._json(HTTPStatus.OK, {"log": list(publisher.log) if publisher else []})
                return
            name, sub = self._route()
            if name is None and sub is None:
                self._json(
                    HTTPStatus.OK,
                    {"posts": list_posts(queue, done, clock()), "publish_enabled": publisher is not None, "upload_enabled": image_store is not None},
                )
            elif name and sub is None:
                self._run(lambda: (HTTPStatus.OK, {"post": read_post(name, queue)}))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})

        def _upload_image(self) -> None:
            """画像（JPEG の中身そのもの）を置き場所に上げて、キーを返す。"""
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "image/jpeg":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JPEG の画像だけ使えます"})
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "この画面以外からは画像を上げられません"})
                return
            if image_store is None:
                self._json(HTTPStatus.FORBIDDEN, {"error": "画像の置き場所が設定されていません（.env の STORAGE_*。手順は docs/セットアップ.md）"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_IMAGE_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"画像は {MAX_IMAGE_BYTES // 1024 // 1024}MB までです"})
                return
            data = self.rfile.read(length)
            self._run(lambda: (HTTPStatus.CREATED, {"image_key": image_store.upload(data)}))

        def do_POST(self):
            if urlsplit(self.path).path == "/api/images":
                self._upload_image()
                return
            name, sub = self._route()
            if not (name is None and sub is None) and not (name and sub in ("cancel", "publish")):
                self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})
                return
            data = self._read_json()
            if data is None:
                return
            if name is None:
                self._run(lambda: (HTTPStatus.CREATED, {"post": _summary(create_post(data, queue, clock()), "scheduled")}))
                return
            version = data.get("version") if isinstance(data, dict) else None
            if sub == "publish":
                self._run(lambda: (HTTPStatus.OK, publish_post(name, version, queue, publisher, clock())))
            else:
                self._run(lambda: (HTTPStatus.OK, {"cancelled": cancel_post(name, version, queue, cancelled).name}))

        def do_PUT(self):
            name, sub = self._route()
            if not name or sub is not None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})
                return
            data = self._read_json()
            if data is None:
                return
            self._run(lambda: (HTTPStatus.OK, {"post": _summary(update_post(name, data, queue, clock()), "scheduled")}))

    return Handler


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def serve(
    queue: Path,
    done: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    publish: bool = False,
    interval: float = 60.0,
) -> None:
    if publish and host not in LOCAL_HOSTS:
        # 認証のない画面から、同じネットワークの誰でも投稿できてしまうため
        raise ConfigError("--publish は自分のPC（--host 127.0.0.1）で開くときだけ使えます")
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    from .storage import store_from_env

    image_store = store_from_env()  # STORAGE_* がなければ None（画像は URL を手入力する）
    publisher = Publisher(queue, done, image_store=image_store) if publish else None
    server = ThreadingHTTPServer((host, port), make_handler(queue, done, clock, publisher=publisher, image_store=image_store))
    shown = "localhost" if host in LOCAL_HOSTS else host
    print(f"予約画面: http://{shown}:{server.server_address[1]}/  （止めるときは Ctrl+C）")
    stop = None
    if publisher:
        stop = start_auto(publisher, clock, interval)
        print(f"投稿モード: 画面の「今すぐ投稿」と、時刻が来た予約の自動投稿（{int(interval)}秒ごと）をします")
        print("この画面を止めると、自動投稿も止まります")
    else:
        print("下見モード: ここから投稿はしません（投稿するときは sns-post web --publish）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n止めました")
    finally:
        if stop:
            stop.set()
        server.server_close()
