"""定期実行の登録（sns-post schedule）のテスト。schtasks は本物を呼ばず、偽物に差し替える。"""
import subprocess
from pathlib import Path

import pytest

from sns_post import cli, schedule
from sns_post.errors import ConfigError


@pytest.fixture(autouse=True)
def keep_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class FakeRun:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, stdout="ok", stderr="ng")


def _args(*extra):
    return cli.build_parser().parse_args(["schedule", *extra])


def test_task_command_runs_in_folder_publishes_and_logs():
    cmd = schedule.task_command(Path("/shop"), executable="python", windows=False)
    assert cmd == ["python", "-m", "sns_post.cli", "-C", str(Path("/shop")),
                   "run-due", "--publish", "--log", "posts/run.log"]


def test_task_command_dry_run_has_no_publish():
    cmd = schedule.task_command(Path("/shop"), publish=False, executable="python", windows=False)
    assert "--publish" not in cmd and "run-due" in cmd


def test_task_command_uses_pythonw_on_windows(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    cmd = schedule.task_command(Path("C:/shop"), executable=str(tmp_path / "python.exe"), windows=True)
    assert cmd[0] == str(tmp_path / "pythonw.exe")
    assert cmd[-1] == "posts\\run.log"


def test_task_command_parses_back_into_the_cli():
    """登録するコマンドが、そのまま sns-post の引数として読めること。"""
    cmd = schedule.task_command(Path("/shop"), executable="python", windows=False)
    args = cli.build_parser().parse_args(cmd[3:])
    assert args.workdir == str(Path("/shop")) and args.command == "run-due"
    assert args.publish and args.log == "posts/run.log"


def test_windows_create_args():
    args = schedule.windows_create_args(["py", "-m", "sns_post.cli", "run-due", "--publish"], 15)
    assert args[:2] == ["schtasks", "/Create"]
    assert args[args.index("/SC") + 1] == "MINUTE" and args[args.index("/MO") + 1] == "15"
    assert args[args.index("/TN") + 1] == schedule.TASK_NAME
    assert "--publish" in args[args.index("/TR") + 1]


def test_windows_create_args_quotes_paths_with_spaces():
    args = schedule.windows_create_args([r"C:\My Python\pythonw.exe", "-C", r"C:\お店 用"], 10)
    assert args[args.index("/TR") + 1] == r'"C:\My Python\pythonw.exe" -C "C:\お店 用"'


def test_windows_create_args_rejects_long_command_and_bad_interval():
    with pytest.raises(ConfigError, match="長すぎます"):
        schedule.windows_create_args(["x" * 300], 10)
    with pytest.raises(ConfigError):
        schedule.windows_create_args(["py"], 0)
    with pytest.raises(ConfigError):
        schedule.windows_create_args(["py"], 1440)


def test_cron_line():
    assert schedule.cron_line(["py", "a b"], 10) == "*/10 * * * * py 'a b'"
    assert schedule.cron_line(["py"], 120) == "0 */2 * * * py"
    with pytest.raises(ConfigError):
        schedule.cron_line(["py"], 90)


def test_shows_by_default_and_registers_only_with_install(monkeypatch, capsys):
    monkeypatch.setattr(schedule, "is_windows", lambda: True)
    run = FakeRun()
    assert cli._cmd_schedule(_args(), run) == 0
    assert run.calls == []  # 既定は表示だけ
    assert "まだ登録していません" in capsys.readouterr().out

    assert cli._cmd_schedule(_args("--install", "--every", "5"), run) == 0
    create = run.calls[0]
    assert create[:2] == ["schtasks", "/Create"]
    assert create[create.index("/MO") + 1] == "5"
    assert "--publish" in create[create.index("/TR") + 1]


def test_install_dry_run_registers_without_publish(monkeypatch):
    monkeypatch.setattr(schedule, "is_windows", lambda: True)
    run = FakeRun()
    assert cli._cmd_schedule(_args("--install", "--dry-run"), run) == 0
    assert "--publish" not in run.calls[0][run.calls[0].index("/TR") + 1]


def test_remove_status_and_failure(monkeypatch):
    monkeypatch.setattr(schedule, "is_windows", lambda: True)
    run = FakeRun()
    assert cli._cmd_schedule(_args("--remove"), run) == 0
    assert run.calls[0][:2] == ["schtasks", "/Delete"]
    assert cli._cmd_schedule(_args("--status"), run) == 0
    assert run.calls[1][:2] == ["schtasks", "/Query"]
    assert cli._cmd_schedule(_args("--install"), FakeRun(returncode=1)) == 1


def test_mac_linux_only_prints_cron(monkeypatch, capsys):
    monkeypatch.setattr(schedule, "is_windows", lambda: False)
    run = FakeRun()
    assert cli._cmd_schedule(_args("--install"), run) == 0
    assert run.calls == []
    assert "*/10 * * * *" in capsys.readouterr().out
