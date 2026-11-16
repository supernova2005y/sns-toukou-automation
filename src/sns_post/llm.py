"""生成AI（LLM）を呼ぶ部分。Gemini と Claude を差し替えられる。

- どちらも公式の SDK を使う（pip install -e ".[llm]"）。SDK は使うときだけ読み込む
- API キーは .env だけに置く（GEMINI_API_KEY / ANTHROPIC_API_KEY）。設定クラスの repr には出さない
- モデル名・料金は変わるので、使う前に公式ドキュメントで確かめる（docs/セットアップ.md に出典）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from .errors import ApiError, ConfigError

PROVIDERS = ("gemini", "claude")
DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "claude": "claude-opus-5-5",
}
KEY_NAMES = {
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}
MODEL_NAMES = {
    "gemini": "GEMINI_MODEL",
    "claude": "CLAUDE_MODEL",
}
MAX_OUTPUT_TOKENS = 16000  # 下書きは短いが、考える分（thinking）もここに含まれるので小さくしない


class TextModel(Protocol):
    """文章を1回つくるもの。テストでは偽物に差し替える。"""

    label: str  # 画面やログに出す「どのモデルか」（キーは含めない）

    def generate(self, system: str, prompt: str) -> str: ...


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str = field(repr=False)  # repr に出さない（ログに漏れないように）


def llm_configured() -> bool:
    """下書きに使える設定があるか（キーの値は見ない・返さない）。"""
    provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    return provider in PROVIDERS and bool(os.environ.get(KEY_NAMES[provider]))


def llm_from_env() -> LLMConfig:
    provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER は {' か '.join(PROVIDERS)} にしてください（今: {provider}）")
    key_name = KEY_NAMES[provider]
    key = os.environ.get(key_name)
    if not key:
        raise ConfigError(f"環境変数が足りません: {key_name}（下書きを使うときだけ必要。手順は docs/セットアップ.md）")
    model = (os.environ.get(MODEL_NAMES[provider]) or "").strip() or DEFAULT_MODELS[provider]
    return LLMConfig(provider=provider, model=model, api_key=key)


def _sdk_missing(provider: str) -> ConfigError:
    return ConfigError(f"{provider} の SDK が入っていません。pip install -e \".[llm]\" を実行してください")


class GeminiModel:
    """Google の Gemini API（google-genai SDK）。"""

    def __init__(self, config: LLMConfig, client=None) -> None:
        self.config = config
        self.label = f"Gemini（{config.model}）"
        if client is None:
            try:
                from google import genai
            except ImportError as e:
                raise _sdk_missing("Gemini") from e
            client = genai.Client(api_key=config.api_key)
        self.client = client

    def generate(self, system: str, prompt: str) -> str:
        try:
            from google.genai import errors as genai_errors
            from google.genai import types

            config = types.GenerateContentConfig(system_instruction=system, max_output_tokens=MAX_OUTPUT_TOKENS)
            sdk_errors: tuple[type[BaseException], ...] = (genai_errors.APIError,)
        except ImportError:  # テストの偽クライアントは SDK なしで動く
            config = {"system_instruction": system, "max_output_tokens": MAX_OUTPUT_TOKENS}
            sdk_errors = ()
        try:
            resp = self.client.models.generate_content(model=self.config.model, contents=prompt, config=config)
        except sdk_errors as e:
            raise ApiError(f"Gemini API: {str(e)[:300]}") from e
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            raise ApiError("Gemini API: 文章が返ってきませんでした（安全のための判定で止められた可能性があります）")
        return text


class ClaudeModel:
    """Anthropic の Claude API（anthropic SDK）。

    断られたとき（stop_reason が refusal）は、サーバー側で別のモデルに引き継ぐ設定（fallbacks）をつけている。
    """

    def __init__(self, config: LLMConfig, client=None) -> None:
        self.config = config
        self.label = f"Claude（{config.model}）"
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise _sdk_missing("Claude") from e
            client = anthropic.Anthropic(api_key=config.api_key)
        self.client = client

    def generate(self, system: str, prompt: str) -> str:
        try:
            import anthropic

            sdk_errors: tuple[type[BaseException], ...] = (anthropic.APIStatusError, anthropic.APIConnectionError)
        except ImportError:
            sdk_errors = ()
        try:
            resp = self.client.beta.messages.create(
                model=self.config.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except sdk_errors as e:
            raise ApiError(f"Claude API: {str(e)[:300]}") from e
        if resp.stop_reason == "refusal":
            raise ApiError("Claude API: この内容では下書きを作れませんでした（断られました）")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not text:
            raise ApiError("Claude API: 文章が返ってきませんでした")
        return text


def model_from_env() -> TextModel:
    config = llm_from_env()
    return GeminiModel(config) if config.provider == "gemini" else ClaudeModel(config)
