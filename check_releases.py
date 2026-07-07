#!/usr/bin/env python3
"""
Spotify新譜トラッカー
フォロー中アーティストの新リリースを検知し、
- releases.ics (Googleカレンダー購読用) を更新
- 新譜があればメール通知
"""

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


# ========== 状態管理 ==========
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": [], "releases": {}, "first_run_done": False}


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
        type_label = {"album": "アルバム", "single": "シングル"}.get(r["type"], r["type"])
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

    body_lines = ["フォロー中アーティストの新譜が見つかりました:\n"]
    for r in new_releases:
        type_label = {"album": "アルバム", "single": "シングル"}.get(r["type"], r["type"])
        body_lines.append(
            f"● {r['artist']} — {r['name']}\n"
            f"   {type_label} / リリース日: {r['release_date']}\n"
            f"   {r['url']}\n"
        )
    body_lines.append("\n※Googleカレンダーにも自動反映されます(購読設定済みの場合)")

    msg = MIMEText("\n".join(body_lines), "plain", "utf-8")
    msg["Subject"] = Header(
        f"🎵 新譜リリース通知 ({len(new_releases)}件)", "utf-8"
    )
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
    first_run = not state.get("first_run_done", False)
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    new_releases: list[dict] = []

    for i, artist in enumerate(artists, 1):
        print(f"[{i}/{len(artists)}] {artist['name']}")
        try:
            releases = get_recent_releases(token, artist)
        except RateLimitAbort as e:
            print(f"Rate limit penalty active: {e}")
            print("State not saved. Will retry on next scheduled run.")
            sys.exit(1)
        except Exception as e:
            print(f"  Error: {e}")
            continue

        for r in releases:
            dt = parse_release_date(r)
            if dt is None or dt < cutoff:
                continue
            state["releases"][r["id"]] = r
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                if not first_run:
                    new_releases.append(r)
        time.sleep(1.0)  # rate limitに優しく(363組なら約7分)

    # 古いリリースをICS/stateから掃除(表示対象外)
    state["releases"] = {
        rid: r
        for rid, r in state["releases"].items()
        if (d := parse_release_date(r)) and d >= cutoff
    }
    state["seen_ids"] = sorted(seen_ids)
    state["first_run_done"] = True
    save_state(state)

    ICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICS_FILE.write_text(generate_ics(state["releases"]), encoding="utf-8")
    print(f"ICS written: {ICS_FILE} ({len(state['releases'])} events)")

    if first_run:
        print("First run: baseline recorded. No notifications sent.")
    elif new_releases:
        print(f"{len(new_releases)} new release(s) found!")
        for r in new_releases:
            print(f"  - {r['artist']}: {r['name']} ({r['release_date']})")
        send_email(new_releases)
    else:
        print("No new releases.")


if __name__ == "__main__":
    sys.exit(main())
