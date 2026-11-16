"""X の文面の下書き（生成AI）。本物の API は呼ばず、偽のモデル・偽の SDK クライアントを使う。"""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sns_post import cli
from sns_post.draft import draft_x_text, missing_urls
from sns_post.errors import ApiError, ConfigError, PostFileError
from sns_post.llm import ClaudeModel, GeminiModel, LLMConfig, llm_configured, llm_from_env
from sns_post.posts import parse_post, x_weighted_length
from sns_post.web import make_handler

JST = timezone(timedelta(hours=9))


class FakeModel:
    """用意した返事を順に返し、渡された指示を記録する。"""

    label = "偽のモデル"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def generate(self, system, prompt):
        self.prompts.append((system, prompt))
        return self.replies.pop(0)


CAPTION = "新作のケーキが入りました。11/20（金）から販売します。詳しくは https://example.com/menu をご覧ください"


# --- 下書きの作り方 ---


def test_short_enough_on_first_try():
    model = FakeModel(["11/20（金）から新作ケーキを販売します https://example.com/menu"])
    d = draft_x_text(CAPTION, model)
    assert d.tries == 1
    assert d.length == x_weighted_length(d.text) <= 280
    assert d.warnings == []
    assert d.model == "偽のモデル"
    system, prompt = model.prompts[0]
    assert "事実" in system and CAPTION in prompt and "280" in prompt


def test_retries_with_the_length_when_too_long():
    long = "あ" * 150  # 300 と数える
    model = FakeModel([long, "短くしました https://example.com/menu"])
    d = draft_x_text(CAPTION, model)
    assert d.tries == 2
    _, second = model.prompts[1]
    assert "300" in second and long in second  # 何文字だったかと前の案を伝えて作り直させる


def test_gives_up_after_three_tries():
    model = FakeModel(["あ" * 150] * 3)
    with pytest.raises(ApiError, match="収まりませんでした"):
        draft_x_text(CAPTION, model)
    assert len(model.prompts) == 3


def test_warns_when_url_is_dropped():
    d = draft_x_text(CAPTION, FakeModel(["11/20 から新作ケーキを販売します"]))
    assert d.warnings == ["本文の URL が下書きにありません: https://example.com/menu"]


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("「新作が出ました」", "新作が出ました"),
        ('"新作が出ました"', "新作が出ました"),
        ("```\n新作が出ました\n```", "新作が出ました"),
        ("  新作が出ました\n", "新作が出ました"),
        ("「新作」と「限定」が出ました", "「新作」と「限定」が出ました"),  # 文中のかぎかっこは残す
    ],
)
def test_strips_wrapping(raw, cleaned):
    assert draft_x_text(CAPTION, FakeModel([raw])).text == cleaned


def test_empty_caption_is_rejected():
    with pytest.raises(ValueError, match="本文が空"):
        draft_x_text("   ", FakeModel([]))


def test_missing_urls():
    assert missing_urls("a https://x.example/1 b https://x.example/2", "https://x.example/2") == ["https://x.example/1"]


# --- 設定（.env） ---


@pytest.fixture
def clean_env(monkeypatch):
    for name in ["LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_MODEL", "CLAUDE_MODEL"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_config_defaults_to_gemini_and_hides_key(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "SECRET-KEY")
    c = llm_from_env()
    assert (c.provider, c.model) == ("gemini", "gemini-3.8-flash")
    assert "SECRET-KEY" not in repr(c)
    assert llm_configured()


def test_config_claude_with_model_override(clean_env):
    clean_env.setenv("LLM_PROVIDER", "Claude")
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    clean_env.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    c = llm_from_env()
    assert (c.provider, c.model) == ("claude", "claude-sonnet-5-5")


def test_config_errors(clean_env):
    assert not llm_configured()
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        llm_from_env()
    clean_env.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        llm_from_env()


# --- SDK の呼び方（偽の SDK クライアント） ---


class FakeGeminiClient:
    def __init__(self, text):
        self.calls = []
        self.models = self
        self._text = text

    def generate_content(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self._text)


def test_gemini_model_passes_system_and_model():
    client = FakeGeminiClient("  下書き  ")
    m = GeminiModel(LLMConfig("gemini", "gemini-3.8-flash", "k"), client=client)
    assert m.generate("指示", "本文") == "下書き"
    call = client.calls[0]
    assert call["model"] == "gemini-3.8-flash" and call["contents"] == "本文"
    config = call["config"]
    system = config["system_instruction"] if isinstance(config, dict) else config.system_instruction
    assert system == "指示"
    assert m.label == "Gemini（gemini-3.8-flash）"


def test_gemini_empty_reply_is_an_error():
    m = GeminiModel(LLMConfig("gemini", "g", "k"), client=FakeGeminiClient(None))
    with pytest.raises(ApiError, match="返ってきませんでした"):
        m.generate("s", "p")


class FakeClaudeClient:
    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=self)
        self._response = response

    def create(self, **kw):
        self.calls.append(kw)
        return self._response


def _claude_response(stop_reason="end_turn", texts=("下書き",)):
    blocks = [SimpleNamespace(type="thinking", thinking="")] + [SimpleNamespace(type="text", text=t) for t in texts]
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def test_claude_model_reads_text_blocks_and_sets_fallback():
    client = FakeClaudeClient(_claude_response(texts=("前半", "後半")))
    m = ClaudeModel(LLMConfig("claude", "claude-opus-5-5", "k"), client=client)
    assert m.generate("指示", "本文") == "前半後半"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == "指示"
    assert call["messages"] == [{"role": "user", "content": "本文"}]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in call  # Opus 5.5 は thinking を止められない（指定しない）


def test_claude_refusal_is_an_error():
    m = ClaudeModel(LLMConfig("claude", "c", "k"), client=FakeClaudeClient(_claude_response("refusal", ())))
    with pytest.raises(ApiError, match="断られました"):
        m.generate("s", "p")


# --- コマンド（sns-post draft-x） ---

POST_FILE = """---
publish_at: 2026-11-20T12:00:00+09:00
targets: [instagram, x]
image_url: https://example.com/a.jpg
---
{body}
"""


def _write(tmp_path, body=CAPTION, extra=""):
    q = tmp_path / "posts" / "queue"
    q.mkdir(parents=True)
    p = q / "2026-11-20_1200.md"
    p.write_text(POST_FILE.format(body=body).replace("---\n" + body, extra + "---\n" + body), encoding="utf-8")
    return p


def _args(path, write=False):
    return cli.build_parser().parse_args(["draft-x", str(path)] + (["--write"] if write else []))


def test_cli_shows_draft_without_writing(tmp_path, capsys):
    p = _write(tmp_path)
    before = p.read_text(encoding="utf-8")
    assert cli._cmd_draft_x(_args(p), FakeModel(["新作ケーキ 11/20 から https://example.com/menu"])) == 0
    out = capsys.readouterr().out
    assert "新作ケーキ" in out and "投稿はしません" in out and "--write" in out
    assert p.read_text(encoding="utf-8") == before


def test_cli_write_saves_x_text_and_keeps_the_rest(tmp_path):
    p = _write(tmp_path)
    cli._cmd_draft_x(_args(p, write=True), FakeModel(["新作ケーキ 11/20 から https://example.com/menu"]))
    post = parse_post(p)
    assert post.x_text == "新作ケーキ 11/20 から https://example.com/menu"
    assert post.text == CAPTION and post.image_url == "https://example.com/a.jpg" and post.results == {}


def test_cli_refuses_when_already_partly_posted(tmp_path):
    p = _write(tmp_path, extra="results:\n  instagram: '123'\n")
    with pytest.raises(PostFileError, match="投稿済み"):
        cli._cmd_draft_x(_args(p, write=True), FakeModel(["x"]))


def test_cli_refuses_without_x_target(tmp_path):
    p = _write(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("[instagram, x]", "[instagram]"), encoding="utf-8")
    with pytest.raises(PostFileError, match="targets に x"):
        cli._cmd_draft_x(_args(p), FakeModel(["x"]))


def test_cli_does_not_write_if_file_changed_meanwhile(tmp_path):
    p = _write(tmp_path)

    class ChangingModel(FakeModel):
        def generate(self, system, prompt):
            p.write_text(p.read_text(encoding="utf-8").replace("新作", "限定"), encoding="utf-8")  # 別の場所で書き換わった
            return "新作ケーキ https://example.com/menu"

    with pytest.raises(PostFileError, match="変わったので"):
        cli._cmd_draft_x(_args(p, write=True), ChangingModel([]))
    assert parse_post(p).x_text is None


def test_cli_accepts_a_name_in_the_queue(tmp_path, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli._cmd_draft_x(_args(p.name), FakeModel(["新作 https://example.com/menu"])) == 0


# --- 予約画面（/api/draft-x） ---


@pytest.fixture
def server(tmp_path):
    started = []

    def start(drafter):
        q = tmp_path / "queue"
        handler = make_handler(q, tmp_path / "done", lambda: datetime(2026, 11, 16, tzinfo=JST), drafter=drafter)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _post_json(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_web_draft_returns_text_and_writes_nothing(server, tmp_path):
    base = server(FakeModel(["新作 https://example.com/menu"]))
    with urllib.request.urlopen(base + "/api/posts") as r:
        assert json.loads(r.read())["draft_enabled"] is True
    status, data = _post_json(base + "/api/draft-x", {"text": CAPTION})
    assert status == 200
    assert data["x_text"] == "新作 https://example.com/menu" and data["length"] <= 280 and data["model"] == "偽のモデル"
    assert not (tmp_path / "queue").exists()  # ファイルは作らない


def test_web_draft_disabled_without_llm(server):
    base = server(None)
    with urllib.request.urlopen(base + "/api/posts") as r:
        assert json.loads(r.read())["draft_enabled"] is False
    status, data = _post_json(base + "/api/draft-x", {"text": CAPTION})
    assert status == 403 and "LLM_PROVIDER" in data["error"]


def test_web_draft_errors(server):
    base = server(FakeModel(["あ" * 150] * 3))
    assert _post_json(base + "/api/draft-x", {"text": ""})[0] == 400
    status, data = _post_json(base + "/api/draft-x", {"text": CAPTION})
    assert status == 502 and "収まりませんでした" in data["error"]

