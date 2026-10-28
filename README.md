# sns-toukou-automation

Instagram と X に、**公式APIだけ**で予約投稿するための小さなツール。
Markdown で書いた投稿ファイルを `posts/queue/` に置くと、時刻が来たものを投稿します。

## できること / やらないこと

| できる | やらない |
|---|---|
| 予約投稿（Instagram の画像、X のテキスト） | 自動いいね・自動フォロー・自動DM |
| 1つのファイルから Instagram と X へ同時投稿 | Cookie を使う非公式ツール |
| 下見（dry-run）が既定。`--publish` をつけたときだけ投稿 | 他人のパスワードを預かること |
| 途中で失敗しても二重投稿しない | |

## 使い方

```bash
pip install -e ".[dev]"          # 初回だけ
cp .env.example .env             # 認証情報を入れる（手順は docs/セットアップ.md）

# 1. 投稿ファイルを書く（examples/ のサンプルを参考に）
cp "examples/2026-11-01_1200_サンプル.md" posts/queue/

sns-post check                   # 書き方の確認
sns-post run-due                 # 下見：どれが投稿されるかを表示するだけ
sns-post run-due --publish       # 実際に投稿（時刻が来たものだけ）
sns-post limit                   # Instagram の投稿上限の使用状況
sns-post refresh-token           # Instagram のトークン更新（有効60日）
pytest                           # テスト
```

投稿ファイルの形：

```markdown
---
publish_at: 2026-11-01T12:00:00+09:00   # タイムゾーンつきで書く
targets: [instagram, x]
image_url: https://example.com/photo.jpg  # Instagram には必須（JPEG・公開URL）
x_text: Xだけ文面を変えたいとき（任意）
---
本文。Instagram のキャプションになる。
```

- 投稿に成功した先は、ファイルの `results:` に記録されます。全部の先に投稿できたら `posts/done/` に移ります
- Instagram は24時間で100投稿まで（カルーセルは1投稿）。確認は `sns-post limit`
- X は従量課金。通常の投稿 約0.015ドル、リンク付き 約0.20ドル。下見で目安が出ます

## 作ってあるもの / まだないもの

- [x] 投稿ファイルの読み込みと書き方チェック
- [x] Instagram（画像1枚）と X（テキスト）の投稿
- [x] 下見モード、二重投稿の防止、テスト（20件）
- [ ] 実際のアカウントでの動作確認（**未確認**。テストはAPIの代わりに偽物を使っています）
- [ ] 画像の置き場所（Instagram は公開URLが必要）
- [ ] 定期実行（PCのタスクスケジューラ、GitHub Actions など）
- [ ] Instagram の複数枚投稿・動画・リール・ストーリーズ
- [ ] 投稿文の下書き作成の補助

## 注意

- `.env` は GitHub に上げない（`.gitignore` 済み）。トークンをチャットやログに貼らない
- APIのバージョン・上限・料金は変わるので、使う前に公式ドキュメントで確認する（`docs/セットアップ.md` に出典）
- 最初は、自分のテスト用アカウントだけで試す
