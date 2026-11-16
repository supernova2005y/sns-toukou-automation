"""X 用の文面（x_text）の下書きを、生成AIに作らせる。

困りごと：Instagram の本文をそのまま X に出すと、日本語は1文字=2文字分と数えるので、
141〜280文字の日本語でも投稿の直前に失敗する。毎回人が手で縮めるのは手間。

やること：
- 本文から、X の重みつき280文字に収まる文面の下書きを作る
- 数えるのは生成AIではなくこのツール（x_weighted_length）。長すぎたら、何文字だったかを伝えて作り直させる
- 本文の URL が消えていたら知らせる（日時・場所・値段などの事実は人が見て確かめる）
- 下書きは**投稿しない**。画面に出すか、--write のときだけファイルの x_text に書く
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ApiError
from .llm import TextModel
from .posts import X_TEXT_MAX, x_weighted_length

MAX_TRIES = 3
_URL = re.compile(r"https?://\S+", re.IGNORECASE)

SYSTEM = """あなたは、お店や個人の SNS 投稿を手伝う編集者です。
Instagram 用の本文を、X（旧Twitter）用の短い文面に直します。

守ること：
- 本文に書いてある事実（日時・場所・値段・商品名・URL）は変えない。本文にない事実を足さない
- URL は本文のまま、省略せずに残す
- ハッシュタグは多くても2つまで。なくてもよい
- 絵文字は本文で使っているものだけ、少なめに
- 本文と同じ言葉づかい（です・ます など）にする
- 出力は X に投稿する文面だけ。説明・前置き・かぎかっこ・引用符はつけない"""


@dataclass
class Draft:
    text: str
    length: int  # X の重みつき文字数
    tries: int  # 何回目で収まったか
    model: str  # どのモデルで作ったか
    warnings: list[str] = field(default_factory=list)


def _prompt(text: str, limit: int, previous: tuple[str, int] | None) -> str:
    lines = [
        f"X の文字数の上限は {limit} です（日本語・全角は1文字=2、半角英数字は1、URL は長さにかかわらず23と数えます）。",
        f"日本語だけなら、だいたい {limit // 2 - 10} 文字までにしてください。",
        "",
        "Instagram の本文：",
        text,
    ]
    if previous:
        draft, n = previous
        lines += ["", f"前の案は {n} で、上限の {limit} を超えました。もっと短くしてください。前の案：", draft]
    return "\n".join(lines)


def _clean(raw: str) -> str:
    """前後の空白・コードブロック・全体を囲む引用符を外す。"""
    s = raw.strip()
    s = re.sub(r"^```[a-zA-Z]*\n?|```$", "", s).strip()
    for a, b in (("「", "」"), ('"', '"'), ("“", "”"), ("'", "'")):
        inner = s[1:-1]
        if len(s) >= 2 and s.startswith(a) and s.endswith(b) and a not in inner and b not in inner:
            s = s[1:-1].strip()
    return s


def missing_urls(source: str, draft: str) -> list[str]:
    """本文にあって、下書きから消えた URL。"""
    return [u for u in _URL.findall(source) if u not in draft]


def draft_x_text(text: str, model: TextModel, limit: int = X_TEXT_MAX, tries: int = MAX_TRIES) -> Draft:
    if not text.strip():
        raise ValueError("本文が空なので、下書きを作れません")
    previous: tuple[str, int] | None = None
    for i in range(1, tries + 1):
        draft = _clean(model.generate(SYSTEM, _prompt(text, limit, previous)))
        if not draft:
            raise ApiError("生成AIから空の文面が返ってきました")
        n = x_weighted_length(draft)
        if n <= limit:
            warnings = [f"本文の URL が下書きにありません: {u}" for u in missing_urls(text, draft)]
            return Draft(text=draft, length=n, tries=i, model=model.label, warnings=warnings)
        previous = (draft, n)
    assert previous is not None
    raise ApiError(f"{tries} 回作り直しても {limit} 文字に収まりませんでした（最後の案は {previous[1]}）。手で縮めてください")
