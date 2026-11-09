"""定期実行の登録（`sns-post schedule`）。

- Windows はタスクスケジューラ（schtasks）に「N分ごとに run-due --publish」を登録する
- それ以外（Mac・Linux）は cron に書く1行を表示するだけ（自動では書き込まない）
- 既定は表示だけ。登録するのは `--install` のとき
- 同時に動いたときの二重投稿は、run-due --publish 側のロック（publisher.publish_lock）で防ぐ
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from .errors import ConfigError

TASK_NAME = "sns-post"
DEFAULT_LOG = "posts/run.log"
# schtasks の /TR の長さ。公式は「262文字を超えない」。1文字余裕をみる（出典は docs/セットアップ.md）
SCHTASKS_TR_MAX = 261


def is_windows() -> bool:
    return os.name == "nt"


def _python_for_task(executable: str, windows: bool) -> str:
    """Windows では、実行のたびに黒い画面が出ない pythonw.exe を使う（あれば）。"""
    if windows:
        exe = Path(executable)
        quiet = exe.with_name("pythonw.exe")
        if exe.name.lower() == "python.exe" and quiet.exists():
            return str(quiet)
    return executable


def task_command(workdir: Path, *, publish: bool = True, executable: str | None = None,
                 windows: bool | None = None, log: str = DEFAULT_LOG) -> list[str]:
    """定期実行で動かすコマンド。publish=False なら下見だけ（登録して試す用）。"""
    windows = is_windows() if windows is None else windows
    python = _python_for_task(executable or sys.executable, windows)
    cmd = [python, "-m", "sns_post.cli", "-C", str(workdir), "run-due"]
    if publish:
        cmd.append("--publish")
    return cmd + ["--log", log.replace("/", "\\") if windows else log]


def _check_every(every: int) -> None:
    if not 1 <= every <= 1439:
        raise ConfigError("--every は 1〜1439（分）にしてください")


def windows_create_args(command: list[str], every: int) -> list[str]:
    _check_every(every)
    tr = subprocess.list2cmdline(command)
    if len(tr) > SCHTASKS_TR_MAX:
        raise ConfigError(
            f"登録するコマンドが長すぎます（{len(tr)}/{SCHTASKS_TR_MAX}文字）。"
            "フォルダを短い場所（例 C:\\sns-post）に移してください"
        )
    return ["schtasks", "/Create", "/SC", "MINUTE", "/MO", str(every), "/TN", TASK_NAME, "/TR", tr, "/F"]


def windows_delete_args() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def windows_query_args() -> list[str]:
    return ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"]


def cron_line(command: list[str], every: int) -> str:
    _check_every(every)
    if every < 60:
        when = f"*/{every} * * * *"
    elif every % 60 == 0 and 24 % (every // 60) == 0:
        when = f"0 */{every // 60} * * *"
    else:
        raise ConfigError("cron では 60分以上の間隔は 60・120・180… のように、24時間を割り切れる時間にしてください")
    return f"{when} {shlex.join(command)}"


def run_command(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
