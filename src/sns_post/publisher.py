"""予約画面から投稿するための部品。

- `sns-post web --publish` で起動したときだけ使う（既定の予約画面は投稿しない）
- 投稿の手順そのものは runner.run_due と同じ（成功した先はその都度記録 → 二重投稿しない）
- 画面の「今すぐ投稿」・自動投稿・コマンドの `run-due --publish` が同時に動かないよう、
  ロックファイル（posts/.publish.lock）で1つずつ投稿する
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from . import config
from .errors import ConfigError, PostFileError
from .instagram import InstagramClient
from .posts import Post, load_posts, parse_post
from .runner import run_due

LOCK_NAME = ".publish.lock"
STALE_SECONDS = 15 * 60  # これより古いロックは、前の処理が落ちて残ったものとみなす


class BusyError(Exception):
    """ほかの場所で投稿中。"""


@contextmanager
def publish_lock(posts_dir: Path) -> Iterator[None]:
    """投稿中の目印。同じPCの別のプロセス（コマンド・画面）とも重ならないようにする。"""
    posts_dir.mkdir(parents=True, exist_ok=True)
    lock = posts_dir / LOCK_NAME
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_SECONDS:
                lock.unlink(missing_ok=True)
                continue
            raise BusyError("ほかの場所（画面かコマンド）で投稿中です。少し待ってからやり直してください")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        break
    else:
        raise BusyError("投稿中の目印を作れませんでした。少し待ってからやり直してください")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _default_clients(targets: set[str]):
    """必要な投稿先のクライアントを .env から作る。設定がなければ None（runner が「スキップ」と書く）。"""
    from .x import XClient

    ig = x = None
    if "instagram" in targets:
        try:
            ig = InstagramClient(config.instagram_from_env())
        except ConfigError:
            ig = None
    if "x" in targets:
        try:
            x = XClient(config.x_from_env())
        except ConfigError:
            x = None
    return ig, x


class Publisher:
    def __init__(
        self,
        queue: Path,
        done: Path,
        clients: Callable[[set[str]], tuple] = _default_clients,
        history: int = 50,
        image_store=None,
    ) -> None:
        self.queue = queue
        self.done = done
        self.clients = clients
        self._image_store = image_store
        self._store_loaded = image_store is not None
        self.log: deque[dict] = deque(maxlen=history)  # 画面に出す最近の記録
        self._lock = threading.Lock()

    def _record(self, lines: list[str], now: datetime) -> None:
        for line in lines:
            self.log.appendleft({"at": now.isoformat(), "line": line})

    def _store(self):
        """画像の置き場所。STORAGE_* がなければ None。必要になったときに1回だけ作る。"""
        if not self._store_loaded:
            from .storage import store_from_env

            self._image_store = store_from_env()
            self._store_loaded = True
        return self._image_store

    def _run(self, posts: list[Post], now: datetime) -> list[str]:
        needed = {t for p in posts if p.publish_at <= now for t in p.pending_targets}
        if not needed:
            return []
        ig, x = self.clients(needed)
        store = self._store() if any(p.needs_store for p in posts) else None
        return run_due(posts, now, True, self.done, ig, x, store)

    def publish_now(self, path: Path, now: datetime) -> list[str]:
        """1件を、予約の時刻を待たずに投稿する（投稿済みの先は飛ばす）。"""
        with self._lock, publish_lock(self.queue.parent):
            if not path.is_file():  # ロック待ちの間に、ほかで投稿済みになった
                raise LookupError("その予約は見つかりません（投稿済みかもしれません）")
            post = parse_post(path)
            lines = self._run([post], max(now, post.publish_at))
            self._record(lines, now)
            return lines

    def publish_due(self, now: datetime) -> list[str]:
        """時刻が来た予約をまとめて投稿する（自動投稿用）。"""
        try:
            with self._lock, publish_lock(self.queue.parent):
                lines = self._run(load_posts(self.queue), now)
        except BusyError:
            return []  # 次の回にやる
        except PostFileError as e:
            lines = [f"[自動投稿を止めました] 読めない予約ファイルがあります: {e}"]
        self._record(lines, now)
        return lines


def start_auto(publisher: Publisher, clock: Callable[[], datetime], interval: float = 60.0) -> threading.Event:
    """interval 秒ごとに、時刻が来た予約を投稿する。返した Event を set すると止まる。"""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                publisher.publish_due(clock())
            except Exception as e:  # 1回の失敗で自動投稿全体を止めない
                publisher._record([f"[エラー] 自動投稿: {e}"], clock())
            stop.wait(interval)

    threading.Thread(target=loop, name="sns-post-auto", daemon=True).start()
    return stop
