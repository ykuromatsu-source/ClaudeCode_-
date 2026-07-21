# SNS自動運用システム 運用マニュアル

大濠うなぎのInstagram投稿を、テーマ入力 → キャプション/ビジュアル仕様の自動生成 →
キュー管理 → 定時自動配信まで一気通貫で担う自動化システムです。実際のAPI呼び出し
（Instagram投稿・Adobe Firefly/Gemini生成）を行わない**モックモード**を標準搭載して
おり、認証情報が無い状態でも安全に全機能を検証できます。

## 構成ファイル

```
★ClaudeCode/
├── .env.example                    # 環境変数テンプレート（Git管理対象）
├── .env                             # 実際の認証情報（Git管理対象外・要作成）
├── data/
│   ├── assets/                      # 宣材写真置き場
│   │   ├── manifest.json            # アセットのメタデータ（後述）
│   │   └── *.jpg / *.png            # 実際の宣材写真
│   ├── output/
│   │   └── scheduler.log            # スケジューラーの実行ログ
│   └── sns_queue.json               # 投稿キュー（生成・状態管理の中心データ）
└── scripts/
    ├── sns_orchestrator.py          # テーマ→投稿案の生成・キュー管理CLI
    ├── instagram_poster.py          # Instagram Graph API 投稿モジュール
    └── scheduler.py                 # 定時自動配信スケジューラー
```

## 1. セットアップ

### 1-1. 環境変数（Instagram Graph API認証情報）

```bash
cp .env.example .env
```

`.env` を開き、以下を設定します（Meta for Developers の Instagram Graph API 設定から取得）。

| 変数 | 内容 |
|---|---|
| `INSTAGRAM_ACCOUNT_ID` | InstagramビジネスアカウントのユーザーID |
| `INSTAGRAM_ACCESS_TOKEN` | 長期アクセストークン |
| `META_GRAPH_API_VERSION` | Graph APIバージョン（既定 `v20.0`） |

> **`.env` は空欄のままでも問題ありません。** `INSTAGRAM_ACCESS_TOKEN` が未設定の間は、
> 投稿系のすべてのコマンド（`--post-queued` / `scheduler.py`）が自動的に**モックモード**
> （実HTTPリクエストを送信せず、送信予定のペイロードをログ出力するだけ）で動作します。
> 本番投入時は `.env` に実際の値を設定するだけで、コードの変更は一切不要です。

### 1-2. Python依存ライブラリ

- 標準ライブラリのみでコア機能（キュー生成・管理・モック投稿・スケジューラー）が完結します。
- 実API呼び出し（`INSTAGRAM_ACCESS_TOKEN` 設定時）には `httpx` が必要です: `pip install httpx`
- 宣材写真のプレースホルダー生成・縦横比自動検出には `Pillow` を推奨します: `pip install Pillow`
  （未インストールでも他の機能はすべて動作します）

## 2. 本番の宣材写真を配置する（C機能）

### 2-1. 最短手順（メタデータを気にしない場合）

`.jpg` / `.png` の写真ファイルを `data/assets/` へコピーし、以下を実行するだけです。

```bash
python3 scripts/sns_orchestrator.py --seed-assets
```

`manifest.json` 未登録のファイルは自動的にスタブ登録されます（ファイル名からの
簡易的な `menu_name` 推定・実画像から検出した `orientation`）。**既存のマニフェスト
エントリ（過去に登録した本番写真の情報）は上書きされません**。デモ用のダミー3点
（`unagi_don.jpg` 等）も、`data/assets/` に既に実写真として同名ファイルを置いている
場合は上書きされません。

### 2-2. 精度を上げる（推奨）

自動照合（`--add-theme` でのビジュアル自動判定）の精度を上げたい場合は、
`--seed-assets` 実行後に `data/assets/manifest.json` を開き、該当エントリの
`genre` / `keywords` / `description` を埋めてください。

```json
{
  "file": "yakitori_moriawase.jpg",
  "menu_name": "焼き鳥盛り合わせ",
  "genre": "yakitori",
  "keywords": ["焼き鳥", "串", "炭火", "yakitori"],
  "orientation": "landscape",
  "description": "備長炭で炙った盛り合わせ。皮はパリッと、身はジューシー。"
}
```

| フィールド | 説明 |
|---|---|
| `file` | `data/assets/` 内のファイル名（完全一致） |
| `menu_name` | メニュー名（テーマ文字列との一致判定に使用） |
| `genre` | ジャンル（`unagi` / `sweets` / `drink` など。ハッシュタグ自動生成にも使用） |
| `keywords` | テーマ照合用キーワード（日本語・英語どちらでも可） |
| `orientation` | `landscape` / `portrait` / `square`（`--seed-assets`で自動検出済みなら編集不要） |
| `description` | AI画像/動画生成プロンプトの被写体描写に使われる詳細説明 |

### 2-3. 動作確認

```bash
python3 scripts/sns_orchestrator.py --add-theme "焼き鳥盛り合わせフェア"
```
生成結果の `visual: asset_edit / asset: yakitori_moriawase.jpg` が表示されれば、
新しい宣材写真が正しく認識・照合されています。該当写真が無いテーマの場合は
`ai_image`（Gemini向けプロンプト生成）へ自動フォールバックします。

## 3. 投稿案の生成・キュー管理

```bash
# テーマを1件クイック追加（ジャンル・投稿予定日時は自動推定/現在時刻）
python3 scripts/sns_orchestrator.py --add-theme "今週末の限定 夏野菜カレー"

# 投稿予定日時・媒体・動画化を明示指定
python3 scripts/sns_orchestrator.py --add-theme "土用の丑の日フェア" \
    --scheduled-at 2026-07-24T11:30:00+09:00 --video

# キューの一覧確認（ID/ステータス/プラットフォーム/テーマ/Visual Type）
python3 scripts/sns_orchestrator.py --list-queue
```

## 4. 手動一括投稿（即時配信）

```bash
# モック実行（実際には投稿しない・ペイロード確認のみ）
python3 scripts/sns_orchestrator.py --post-queued --dry-run

# 実際に投稿（.envに認証情報が設定されている場合のみ実配信、未設定なら自動モック）
python3 scripts/sns_orchestrator.py --post-queued
```
`status: queued` の全項目を順次投稿し、成功した項目のみ `status: posted` ＋
`posted_at` へ更新します（失敗した項目は次回再試行できるよう `queued` のまま残ります）。

## 5. 定時自動配信スケジューラー（B機能）

`scheduler.py` は `data/sns_queue.json` を監視し、`status: queued` かつ
`scheduled_at` が到来した項目だけを自動抽出して配信します（未到来の予約投稿は
スキップされ、次回以降のサイクルで自動的に処理されます）。

### 5-1. 単発チェックモード（`--run-once`）— cron / launchd 向け

外部スケジューラー（cronやmacOSの`launchd`）から定期起動する場合に使います。

```bash
# モック（動作確認）
python3 scripts/scheduler.py --run-once --dry-run

# 実運用（15分おきに起動する crontab の例）
*/15 * * * * cd /path/to/★ClaudeCode && /usr/bin/python3 scripts/scheduler.py --run-once >> data/output/cron.log 2>&1
```

### 5-2. 常駐ループモード（既定）

スクリプト自身が指定間隔で待機し続けます（`tmux`/`screen`や`launchd`常駐設定、
バックグラウンドプロセスとしての起動を想定）。

```bash
# 60分間隔で常駐監視（既定）
python3 scripts/scheduler.py

# 15分間隔・モードで常駐監視
python3 scripts/scheduler.py --interval-minutes 15

# Ctrl+C（SIGINT）またはSIGTERMで安全に終了します
```

### 5-3. ログ

実行ログはコンソールと `data/output/scheduler.log` の両方へ出力されます。
1回の点検サイクルで例外が発生してもスケジューラー自体はクラッシュせず、
エラー内容をログに記録した上で次回のサイクルへ継続します。

## 6. モックモードと実運用の切り替え早見表

| 状況 | `--post-queued` / `scheduler.py` の挙動 |
|---|---|
| `.env` に `INSTAGRAM_ACCESS_TOKEN` 未設定 | 自動的にモックモード（実HTTPなし） |
| `--dry-run` を明示指定 | トークン設定の有無に関わらず常にモックモード |
| `.env` に有効なトークンを設定 かつ `--dry-run` 無し | 実際にInstagramへ投稿 |

## 7. 制約・注意事項

- 実際の画像/動画生成（Adobe Firefly Veo 3.1 / Gemini）とCDNアップロードは本システムの
  スコープ外です。`ai_image` / `ai_video` の投稿URLは検証用のプレースホルダー
  （`https://cdn.example.com/...`）を合成しています。本番投入時は、生成・ホスティング
  済みの実URLを `visual_spec` に反映する処理を別途追加してください。
- アクセストークン等の機密情報はログ・コード・Gitのいずれにも出力・保存されません
  （`.env` は `.gitignore` で除外済み、ペイロードログは常にトークンを除去してから出力）。
- 既存スキル `~/.claude/skills/insta-food-buzz/` は読み取り専用参照のみで、
  一切変更されません。
