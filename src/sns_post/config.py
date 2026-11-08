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


def update_env(key: str, value: str, path: str | Path = ".env") -> None:
    """.env の1行（KEY=...）を書き換える。なければ末尾に足す。ほかの行はそのまま残す。"""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


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
    # unified: Instagram API with Unified Login（新しいアプリはこちら。graph.facebook.com）
    # instagram: 旧 Instagram ログイン方式（graph.instagram.com）
    login: str = "unified"

    @property
    def host(self) -> str:
        return IG_HOSTS[self.login]


IG_HOSTS = {
    "unified": "https://graph.facebook.com",
    "instagram": "https://graph.instagram.com",
}


@dataclass(frozen=True)
class XConfig:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    access_token_secret: str = field(repr=False)


def instagram_from_env() -> InstagramConfig:
    v = _require(["IG_USER_ID", "IG_ACCESS_TOKEN"])
    login = (os.environ.get("IG_LOGIN") or "unified").strip().lower()
    if login not in IG_HOSTS:
        raise ConfigError(f"IG_LOGIN は {' か '.join(IG_HOSTS)} にしてください（今: {login}）")
    return InstagramConfig(
        user_id=v["IG_USER_ID"],
        access_token=v["IG_ACCESS_TOKEN"],
        api_version=os.environ.get("IG_API_VERSION") or "v23.0",
        login=login,
    )


def x_from_env() -> XConfig:
    v = _require(["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"])
    return XConfig(
        api_key=v["X_API_KEY"],
        api_secret=v["X_API_SECRET"],
        access_token=v["X_ACCESS_TOKEN"],
        access_token_secret=v["X_ACCESS_TOKEN_SECRET"],
    )
