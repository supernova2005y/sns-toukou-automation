from __future__ import annotations


class ConfigError(Exception):
    """設定（環境変数）が足りない。"""


class ApiError(Exception):
    """SNS側のAPIがエラーを返した。"""


class PostFileError(Exception):
    """投稿ファイルの書き方に問題がある。"""
