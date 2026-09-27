# CLAUDE.md

このリポジトリで作業する際の前提知識をまとめたものです。

## プロジェクト概要

Spotifyでフォロー中のアーティストの新譜を毎日自動チェックし、以下を行うシステム。

- `docs/releases.ics` を更新 → Googleカレンダーが購読URLから同期
- 新譜・リリース予定を検知したら Gmail SMTP でメール通知
- リリース済みの新譜を Spotify プレイリスト「Release Tracker」へ自動追加

サーバー不要。GitHub Actions(毎日 UTC 0:00 = JST 9:00)で動く。
実体は単一スクリプト `check_releases.py` のみで、外部依存は `requests` だけ。

## ファイル構成

| パス | 役割 |
|---|---|
| `check_releases.py` | 本体。取得・検知・ICS生成・メール送信・プレイリスト同期をすべて担う |
| `seen_releases.json` | 永続状態(コミット対象)。ワークフローが毎回書き戻す |
| `docs/releases.ics` | 生成物(コミット対象)。Googleカレンダー購読用 |
| `get_refresh_token.py` | 初回のみローカル実行して `SPOTIFY_REFRESH_TOKEN` を取得 |
| `run_local.sh` | Mac mini の launchd から回す場合の代替実行系(`.env` を読む) |
| `.github/workflows/check_releases.yml` | スケジュール実行と `git commit && push` |
| `.claude/agents/log-checker.md` | Actions実行ログを要約報告する専用サブエージェント |

## コマンド

```bash
pip install requests

# ローカル実行(要: 環境変数 or .env)
python3 -u check_releases.py

# Mac mini 経路(.env を読み込み、実行後に main へ push)
./run_local.sh
```

必須の環境変数: `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `SPOTIFY_REFRESH_TOKEN`。
`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` が未設定ならメール通知はスキップされる(`NOTIFY_TO` 省略時は `GMAIL_ADDRESS` 宛)。
テストもリンタもなく、実行そのものが唯一の検証手段。
シークレットを伴うため CI 以外での通し実行は避け、変更箇所は読みで確認するのが基本。

## 状態ファイル (`seen_releases.json`) のスキーマ

| キー | 意味 |
|---|---|
| `seen_ids` | 既知リリースID。ここに入っていれば通知しない(リミックスも「無視するため」に登録される) |
| `releases` | ICSに出す現役リリース。`{id: release}`。90日より古い通常リリース・日付が過ぎた予定は毎回削除される |
| `first_run_done` | 初回一巡が終わったか。`false` の間は通知を出さずベースライン記録のみ |
| `cycle_checked` | 今周回でチェック済みのアーティストID(レート制限中断からの再開点) |
| `playlist_id` | 自動生成したプレイリストのID |
| `playlist_synced_ids` | プレイリストへ追加済みのリリースID |

この6キーは互いに依存しているので、片方だけ書き換えると通知漏れや重複通知になる。
手で編集するのは原則避ける。

## 設計上の要点(壊しやすい箇所)

- **レート制限の扱い**: 429 の `Retry-After` が300秒を超えたら `RateLimitAbort` を投げ、
  進捗を保存して**正常終了**する。失敗扱いにしないのが仕様。次回は `cycle_checked` の続きから再開する。
- **チェックポイント方式**: 全アーティスト(約360人)を1回では回しきれない前提。
  一巡し終えた時だけ `cycle_complete` となり `first_run_done` が立ち、`cycle_checked` がリセットされる。
- **通知の有効化条件**: `first_run_done` が `true` になるまで通知は飛ばない。
  状態ファイルを消すと全曲が「新譜」になり大量通知が発生する。
- **MusicBrainz は未来の予定専用**: 同名別アーティストを除くため artist-credit の完全一致のみ採用。
  503 や例外は空リストを返して黙ってスキップする(少数のエラーは正常)。
- **予定(📅)と実物(🎵)の重複排除**: Spotify側に実物が出たら
  `(artist, name)` の casefold 一致で予定エントリを削除する。この突き合わせが崩れると
  カレンダーに二重表示される。
- **リミックス除外**: 名前に "remix" を含むものは追跡せず、ID だけ `seen_ids` に入れて毎回スキップする。
- **プレイリスト同期の予算**: 1実行あたり `PLAYLIST_SYNC_BUDGET`(15)リクエストで打ち切り、
  残りは次回へ回す。アーティストチェック側の予算を食わないための上限。
- **ICS のエスケープ**: `escape_ics()` を通さずに文字列を埋め込むとカレンダーが壊れる。
  改行は `\r\n`、`DTSTART`/`DTEND` は VALUE=DATE の終日イベント。
- **API 呼び出しの間隔**: アーティスト1件ごとに `time.sleep(1.0)` を挟んでいる。ここを削ると429を誘発する。

## 調整ポイント

`check_releases.py` 冒頭の定数: `LOOKBACK_DAYS`(90)、`MAX_ALBUMS_PER_ARTIST`(10)、
`MARKET`("JP")、`MB_LOOKAHEAD_DAYS`(365)、`PLAYLIST_NAME`、`PLAYLIST_SYNC_BUDGET`。
実行時刻は `.github/workflows/check_releases.yml` の cron(UTC表記、JST = UTC+9)。

## 慣習

- コード内コメント・ログ・メール本文・カレンダー表記は日本語。ログの見出しは英語のまま。
- `print()` のみでログ出力。`log-checker` エージェントが特定の文字列
  (`N artists found` / `Cycle complete.` / `ICS written:` / `Rate limit penalty active:` など)を
  grep で拾うため、既存のログ文言は安易に変えない。
- 状態ファイルは `ensure_ascii=False, indent=2` で書く(差分を読めるようにするため)。
- 自動コミットのメッセージは `Update releases YYYY-MM-DD`。
- `.claude/` は `agents/` を除き gitignore 済み。
- シークレットは絶対にコミットしない。`.env` は gitignore 済み。
