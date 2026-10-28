"""環境変数（.env）からの設定読み込み。トークンはここ以外に書かない。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


def load_env(path: str | Path = ".env") -> None:
    """.env があれば読み込む。すでに設定済みの環境変数は上書きしない。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require(names: list[str]) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError("環境変数が足りません: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


@dataclass(frozen=True)
class InstagramConfig:
    user_id: str
    access_token: str = field(repr=False)  # repr に出さない（ログに漏れないように）
    api_version: str = "v23.0"


@dataclass(frozen=True)
class XConfig:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    access_token_secret: str = field(repr=False)


def instagram_from_env() -> InstagramConfig:
    v = _require(["IG_USER_ID", "IG_ACCESS_TOKEN"])
    return InstagramConfig(
        user_id=v["IG_USER_ID"],
        access_token=v["IG_ACCESS_TOKEN"],
        api_version=os.environ.get("IG_API_VERSION") or "v23.0",
    )


def x_from_env() -> XConfig:
    v = _require(["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"])
    return XConfig(
        api_key=v["X_API_KEY"],
        api_secret=v["X_API_SECRET"],
        access_token=v["X_ACCESS_TOKEN"],
        access_token_secret=v["X_ACCESS_TOKEN_SECRET"],
    )
