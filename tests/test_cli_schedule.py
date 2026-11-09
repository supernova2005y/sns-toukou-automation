"""定期実行（タスクスケジューラ・cron から run-due を呼ぶ）のためのオプションのテスト。本物のAPIは呼ばない。"""
import pytest

from sns_post import cli
from sns_post.posts import parse_post

POST = """---
publish_at: 2026-11-01T12:00:00+09:00
targets: [x]
---
ひみつの本文
"""


@pytest.fixture(autouse=True)
def keep_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # -C で移っても、テストのあとで元に戻る


class FakeX:
    def __init__(self, cfg=None):
        self.calls = 0

    def post_text(self, text):
        self.calls += 1
        return "x-1"


def setup_folder(tmp_path):
    root = tmp_path / "shop"
    (root / "posts" / "queue").mkdir(parents=True)
    (root / "posts" / "queue" / "a.md").write_text(POST, encoding="utf-8")
    return root


def test_dash_c_runs_in_that_folder_and_reads_its_env(tmp_path, monkeypatch):
    root = setup_folder(tmp_path)
    (root / ".env").write_text("X_API_KEY=k\nX_API_SECRET=s\nX_ACCESS_TOKEN=t\nX_ACCESS_TOKEN_SECRET=ts\n", encoding="utf-8")
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    made = []
    monkeypatch.setattr(cli, "XClient", lambda cfg: made.append(FakeX()) or made[-1])

    code = cli.main(["-C", str(root), "run-due", "--publish", "--now", "2026-11-01T13:00:00+09:00"])

    assert code == 0
    assert made and made[0].calls == 1
    assert (root / "posts" / "done" / "a.md").exists()
    assert parse_post(root / "posts" / "done" / "a.md").results == {"x": "x-1"}


def test_dash_c_missing_folder_is_an_error(tmp_path, capsys):
    assert cli.main(["-C", str(tmp_path / "nai"), "check"]) == 1
    assert "-C のフォルダ" in capsys.readouterr().err


def test_log_records_results_without_post_text(tmp_path, monkeypatch):
    root = setup_folder(tmp_path)
    monkeypatch.setenv("X_API_KEY", "k")
    monkeypatch.setenv("X_API_SECRET", "s")
    monkeypatch.setenv("X_ACCESS_TOKEN", "t")
    monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "ts")
    monkeypatch.setattr(cli, "XClient", lambda cfg: FakeX())

    args = ["-C", str(root), "run-due", "--publish", "--log", "posts/run.log", "--now", "2026-11-01T13:00:00+09:00"]
    assert cli.main(args) == 0
    assert cli.main(args) == 0  # 2回目は何もない → 記録に増やさない

    lines = (root / "posts" / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "[投稿] a.md → x id=x-1" in lines[0] and lines[0].startswith("2026-11-01T")
    assert "[完了]" in lines[1]
    assert "ひみつ" not in "\n".join(lines)


def test_dry_run_log_does_not_include_post_text(tmp_path):
    root = setup_folder(tmp_path)
    assert cli.main(["-C", str(root), "run-due", "--log", "posts/run.log", "--now", "2026-11-01T13:00:00+09:00"]) == 0
    text = (root / "posts" / "run.log").read_text(encoding="utf-8")
    assert "[下見] a.md → x" in text and "ひみつ" not in text
    assert (root / "posts" / "queue" / "a.md").exists()


def test_log_records_errors(tmp_path, monkeypatch):
    root = setup_folder(tmp_path)
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    args = ["-C", str(root), "run-due", "--publish", "--log", "posts/run.log", "--now", "2026-11-01T13:00:00+09:00"]
    assert cli.main(args) == 1
    assert "[エラー] 環境変数が足りません" in (root / "posts" / "run.log").read_text(encoding="utf-8")
