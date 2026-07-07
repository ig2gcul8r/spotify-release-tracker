# Spotify 新譜トラッカー

フォロー中アーティストの新譜を毎日自動チェックし、

- 📅 **Googleカレンダーに自動表示**(ICSカレンダー購読)
- 📧 **新譜が出たらメール通知**(Gmail)

する完全無料の自動化システムです。サーバー不要、GitHub Actionsで動きます。

---

## セットアップ手順(所要時間 約20分)

### Step 1: Spotify Developer アプリ登録

1. https://developer.spotify.com/dashboard にSpotifyアカウントでログイン
2. **Create app** をクリック
   - App name: `Release Tracker`(任意)
   - Redirect URIs: `http://127.0.0.1:8888/callback` ← **正確にこの通り**
   - Which API/SDKs: Web API にチェック
3. 作成後、**Settings** から `Client ID` と `Client Secret` を控える

### Step 2: Refresh Token の取得(Mac miniで一度だけ実行)

```bash
pip3 install requests
python3 get_refresh_token.py
```

- Client ID / Client Secret を入力するとブラウザが開くので、Spotifyで承認
- ターミナルに表示された `SPOTIFY_REFRESH_TOKEN` を控える
- ⚠️ このトークンは絶対に他人に見せない・リポジトリにコミットしないこと

### Step 3: GitHubリポジトリ作成

1. GitHubで**Privateリポジトリ**を新規作成(例: `spotify-release-tracker`)
2. このフォルダの中身をすべてpush:

```bash
cd spotify-release-tracker
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<あなたのユーザー名>/spotify-release-tracker.git
git push -u origin main
```

### Step 4: GitHub Secrets の登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録:

| Secret名 | 値 |
|---|---|
| `SPOTIFY_CLIENT_ID` | Step 1で取得 |
| `SPOTIFY_CLIENT_SECRET` | Step 1で取得 |
| `SPOTIFY_REFRESH_TOKEN` | Step 2で取得 |
| `GMAIL_ADDRESS` | 送信元Gmailアドレス |
| `GMAIL_APP_PASSWORD` | Gmailアプリパスワード(下記参照) |
| `NOTIFY_TO` | 通知先アドレス(省略時はGMAIL_ADDRESSと同じ) |

**Gmailアプリパスワードの取得方法:**
1. Googleアカウントで2段階認証を有効化
2. https://myaccount.google.com/apppasswords でアプリパスワードを生成
3. 表示された16文字を `GMAIL_APP_PASSWORD` に登録

### Step 5: 初回実行

1. リポジトリの **Actions** タブ → **Check Spotify Releases** → **Run workflow** で手動実行
2. 初回はベースライン記録のみで通知は飛びません(過去90日分がカレンダーに登録されます)
3. 以降、毎日 日本時間9:00 に自動チェック → 新譜があればメール通知

### Step 6: Googleカレンダーに購読登録

1. 初回実行後、リポジトリに `docs/releases.ics` が生成されます
2. そのファイルの **Raw URL** をコピー:
   `https://raw.githubusercontent.com/<ユーザー名>/spotify-release-tracker/main/docs/releases.ics`
   - Privateリポジトリの場合はGoogleカレンダーから読めないため、方法A: リポジトリをPublicにする(Secretsは安全なまま)、または方法B: GitHub Pagesを有効化(Settings → Pages → main / docs フォルダ)して `https://<ユーザー名>.github.io/spotify-release-tracker/releases.ics` を使う
3. Googleカレンダー → 左メニュー「他のカレンダー」の **+** → **URLで追加** → 上記URLを貼り付け
4. 「Spotify 新譜リリース」カレンダーとして表示されます(Google側の同期は数時間〜1日周期)

---

## カスタマイズ

`check_releases.py` 冒頭の定数で調整できます:

- `LOOKBACK_DAYS = 90` — カレンダーに表示する過去日数
- `MAX_ALBUMS_PER_ARTIST = 10` — アーティストごとの確認件数
- `MARKET = "JP"` — 対象マーケット

実行時刻の変更は `.github/workflows/check_releases.yml` の cron を編集
(UTC表記。日本時間 = UTC + 9時間)。

## 仕組み

```
GitHub Actions (毎日9:00 JST)
  └─ check_releases.py
       ├─ Spotify API: フォロー中アーティスト全取得
       ├─ 各アーティストの最新リリース取得
       ├─ seen_releases.json と照合 → 新譜検知
       ├─ docs/releases.ics 再生成 → commit & push
       │    └─ Googleカレンダーが購読URLから自動同期
       └─ 新譜があれば Gmail SMTP でメール通知
```
