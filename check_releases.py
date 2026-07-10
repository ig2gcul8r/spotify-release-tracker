#!/usr/bin/env python3
"""
Spotify新譜トラッカー
フォロー中アーティストの新リリースを検知し、
- releases.ics (Googleカレンダー購読用) を更新
- 新譜があればメール通知
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

import requests

# ========== 設定 ==========
STATE_FILE = Path("seen_releases.json")
ICS_FILE = Path("docs/releases.ics")
LOOKBACK_DAYS = 90          # 初回実行時・ICSに含める過去日数
MAX_ALBUMS_PER_ARTIST = 10  # アーティストごとに確認する最新リリース数
MARKET = "JP"

# MusicBrainz(未来のリリース予定の取得)
MB_LOOKAHEAD_DAYS = 365
MB_USER_AGENT = (
    "spotify-release-tracker/1.0 "
    "(https://github.com/ig2gcul8r/spotify-release-tracker)"
)

SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

# メール通知(未設定なら通知スキップ)
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_ADDRESS)


# ========== Spotify API ==========
class RateLimitAbort(Exception):
    """レート制限ペナルティ中(長時間のRetry-After)を示す例外"""
    pass


def get_access_token() -> str:
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": SPOTIFY_REFRESH_TOKEN,
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(url: str, token: str, params: dict | None = None) -> dict:
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:  # rate limit
            wait = int(resp.headers.get("Retry-After", "5"))
            if wait > 300:  # 5分超の待機指示 = ペナルティ中。今回は諦める
                raise RateLimitAbort(
                    f"Retry-After={wait}s. Aborting this run."
                )
            print(f"  Rate limited. Waiting {wait}s...")
            time.sleep(wait + 1)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after retries: {url}")


def get_followed_artists(token: str) -> list[dict]:
    artists = []
    url = "https://api.spotify.com/v1/me/following"
    params = {"type": "artist", "limit": 50}
    while True:
        data = api_get(url, token, params)["artists"]
        artists.extend(data["items"])
        after = data.get("cursors", {}).get("after")
        if not after:
            break
        params["after"] = after
    return artists


def get_recent_releases(token: str, artist: dict) -> list[dict]:
    data = api_get(
        f"https://api.spotify.com/v1/artists/{artist['id']}/albums",
        token,
        params={
            "include_groups": "album,single",
            "limit": MAX_ALBUMS_PER_ARTIST,
            "market": MARKET,
        },
    )
    releases = []
    for album in data.get("items", []):
        releases.append(
            {
                "id": album["id"],
                "name": album["name"],
                "artist": artist["name"],
                "type": album["album_type"],
                "release_date": album["release_date"],
                "release_date_precision": album["release_date_precision"],
                "url": album["external_urls"]["spotify"],
                "total_tracks": album.get("total_tracks", 0),
            }
        )
    return releases


# ========== MusicBrainz(リリース予定) ==========
def mb_get_upcoming(artist_name: str) -> list[dict]:
    """MusicBrainzからアナウンス済みの未来リリースを取得"""
    today = datetime.now().strftime("%Y-%m-%d")
    horizon = (
        datetime.now() + timedelta(days=MB_LOOKAHEAD_DAYS)
    ).strftime("%Y-%m-%d")
    query = (
        f'artistname:"{artist_name}" '
        f"AND date:[{today} TO {horizon}] AND status:official"
    )
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/release",
            params={"query": query, "fmt": "json", "limit": 20},
            headers={"User-Agent": MB_USER_AGENT},
            timeout=30,
        )
        if resp.status_code == 503:  # MB側の混雑。今回はスキップ
            time.sleep(2)
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  MusicBrainz error: {e}")
        return []

    results = []
    seen_titles = set()
    for rel in data.get("releases", []):
        credit = (rel.get("artist-credit") or [{}])[0].get("name", "")
        if credit.casefold() != artist_name.casefold():
            continue  # 同名別アーティスト対策: 完全一致のみ採用
        date = rel.get("date", "")
        if not date or date <= today:
            continue
        title = rel.get("title", "")
        if not title or title.casefold() in seen_titles:
            continue  # 複数エディションの重複排除
        seen_titles.add(title.casefold())
        ptype = (rel.get("release-group") or {}).get("primary-type") or "album"
        precision = {10: "day", 7: "month", 4: "year"}.get(len(date), "day")
        results.append(
            {
                "id": f"mb_{rel['id']}",
                "name": title,
                "artist": artist_name,
                "type": ptype.lower(),
                "release_date": date,
                "release_date_precision": precision,
                "url": f"https://musicbrainz.org/release/{rel['id']}",
                "total_tracks": rel.get("track-count") or 0,
                "upcoming": True,
            }
        )
    return results



def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": [], "releases": {}, "first_run_done": False, "cycle_checked": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_release_date(release: dict) -> datetime | None:
    d = release["release_date"]
    precision = release["release_date_precision"]
    try:
        if precision == "day":
            return datetime.strptime(d, "%Y-%m-%d")
        if precision == "month":
            return datetime.strptime(d, "%Y-%m")
        return datetime.strptime(d, "%Y")
    except ValueError:
        return None


# ========== ICS生成 ==========
def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def generate_ics(releases: dict) -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//spotify-release-tracker//JP",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Spotify 新譜リリース",
        "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for rid, r in releases.items():
        dt = parse_release_date(r)
        if dt is None:
            continue
        date_str = dt.strftime("%Y%m%d")
        end_str = (dt + timedelta(days=1)).strftime("%Y%m%d")
        type_label = {"album": "アルバム", "single": "シングル", "ep": "EP"}.get(
            r["type"], r["type"]
        )
        if r.get("upcoming"):
            summary = escape_ics(f"📅 {r['artist']} — {r['name']}(予定)")
            description = escape_ics(
                f"リリース予定 / {type_label}\n{r['url']}"
            )
        else:
            summary = escape_ics(f"🎵 {r['artist']} — {r['name']}")
            description = escape_ics(
                f"{type_label} / {r['total_tracks']}曲\n{r['url']}"
            )
        lines += [
            "BEGIN:VEVENT",
            f"UID:{rid}@spotify-release-tracker",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{date_str}",
            f"DTEND;VALUE=DATE:{end_str}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"URL:{r['url']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ========== メール通知 ==========
def send_email(new_releases: list[dict]) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("Gmail credentials not set. Skipping email notification.")
        return

    released = [r for r in new_releases if not r.get("upcoming")]
    upcoming = [r for r in new_releases if r.get("upcoming")]

    body_lines = []
    if released:
        body_lines.append("🎵 新譜がリリースされました:\n")
        for r in released:
            type_label = {"album": "アルバム", "single": "シングル", "ep": "EP"}.get(
                r["type"], r["type"]
            )
            body_lines.append(
                f"● {r['artist']} — {r['name']}\n"
                f"   {type_label} / リリース日: {r['release_date']}\n"
                f"   {r['url']}\n"
            )
    if upcoming:
        body_lines.append("\n📅 今後のリリース予定が発表されました:\n")
        for r in upcoming:
            type_label = {"album": "アルバム", "single": "シングル", "ep": "EP"}.get(
                r["type"], r["type"]
            )
            body_lines.append(
                f"● {r['artist']} — {r['name']}\n"
                f"   {type_label} / 予定日: {r['release_date']}\n"
                f"   {r['url']}\n"
            )
    body_lines.append("\n※Googleカレンダーにも自動反映されます(購読設定済みの場合)")

    subject_parts = []
    if released:
        subject_parts.append(f"新譜{len(released)}件")
    if upcoming:
        subject_parts.append(f"リリース予定{len(upcoming)}件")
    msg = MIMEText("\n".join(body_lines), "plain", "utf-8")
    msg["Subject"] = Header(f"🎵 {' / '.join(subject_parts)}", "utf-8")
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    print(f"Email sent to {NOTIFY_TO}")


# ========== メイン ==========
def main() -> None:
    print("Getting access token...")
    token = get_access_token()

    print("Fetching followed artists...")
    artists = get_followed_artists(token)
    print(f"  {len(artists)} artists found.")

    state = load_state()
    seen_ids = set(state["seen_ids"])
    notifications_active = state.get("first_run_done", False)
    cycle_checked = set(state.get("cycle_checked", []))
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    # 前回中断した続きから再開(チェックポイント)
    remaining = [a for a in artists if a["id"] not in cycle_checked]
    if not remaining:  # 全員チェック済み → 新しい周回を開始
        cycle_checked = set()
        remaining = artists
    print(
        f"  Checking {len(remaining)} artists this run "
        f"({len(cycle_checked)} already done this cycle)."
    )

    new_releases: list[dict] = []
    aborted = False

    for i, artist in enumerate(remaining, 1):
        print(f"[{i}/{len(remaining)}] {artist['name']}")
        try:
            releases = get_recent_releases(token, artist)
        except RateLimitAbort as e:
            print(f"Rate limit penalty active: {e}")
            print("Saving progress. Will resume from here on next run.")
            aborted = True
            break
        except Exception as e:
            print(f"  Error: {e}")
            continue

        cycle_checked.add(artist["id"])
        spotify_keys = set()
        for r in releases:
            spotify_keys.add((r["artist"].casefold(), r["name"].casefold()))
            dt = parse_release_date(r)
            if dt is None or dt < cutoff:
                continue
            state["releases"][r["id"]] = r
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                if notifications_active:
                    new_releases.append(r)

        # MusicBrainzでリリース予定をチェック
        for u in mb_get_upcoming(artist["name"]):
            key = (u["artist"].casefold(), u["name"].casefold())
            if key in spotify_keys:
                continue  # Spotify側に既にある(=リリース済み)ものは除外
            state["releases"][u["id"]] = u
            if u["id"] not in seen_ids:
                seen_ids.add(u["id"])
                if notifications_active:
                    new_releases.append(u)

        time.sleep(1.0)  # rate limitに優しく

    # 一巡完了の判定
    cycle_complete = not aborted and len(cycle_checked) >= len(artists)
    if cycle_complete:
        state["first_run_done"] = True  # 初回一巡完了 → 以降は通知有効
        cycle_checked = set()  # 次回から新しい周回
        print("Cycle complete.")

    state["cycle_checked"] = sorted(cycle_checked)

    # 掃除: 通常リリースは90日より古いものを、予定は日付が過ぎたものを削除
    # (予定日が来ればSpotify側のチェックが実物を拾うため)
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    pruned = {}
    for rid, r in state["releases"].items():
        d = parse_release_date(r)
        if d is None:
            continue
        if r.get("upcoming"):
            if d >= today_dt:
                pruned[rid] = r
        elif d >= cutoff:
            pruned[rid] = r
    state["releases"] = pruned
    state["seen_ids"] = sorted(seen_ids)
    save_state(state)

    ICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICS_FILE.write_text(generate_ics(state["releases"]), encoding="utf-8")
    print(f"ICS written: {ICS_FILE} ({len(state['releases'])} events)")

    if new_releases:
        print(f"{len(new_releases)} new release(s) found!")
        for r in new_releases:
            print(f"  - {r['artist']}: {r['name']} ({r['release_date']})")
        send_email(new_releases)
    elif not notifications_active:
        print("Baseline still being recorded. Notifications start "
              "after the first full cycle completes.")
    else:
        print("No new releases.")

    if aborted:
        print("NOTE: Run ended early due to rate limit penalty. "
              "Progress was saved and will resume next run.")


if __name__ == "__main__":
    sys.exit(main())
