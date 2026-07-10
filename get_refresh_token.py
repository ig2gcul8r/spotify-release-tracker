#!/usr/bin/env python3
"""
初回セットアップ用: Spotifyのrefresh tokenを取得するスクリプト。
ローカルPC(Mac mini)で一度だけ実行します。

事前準備:
1. https://developer.spotify.com/dashboard でアプリを作成
2. アプリ設定の Redirect URIs に http://127.0.0.1:8888/callback を追加
3. 下記の環境変数を設定するか、実行時に入力

実行:
    pip install requests
    python get_refresh_token.py
"""

import base64
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-follow-read playlist-modify-private"

client_id = os.environ.get("SPOTIFY_CLIENT_ID") or input("Client ID: ").strip()
client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or input("Client Secret: ").strip()

state = secrets.token_urlsafe(16)
auth_code_holder = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if params.get("state", [""])[0] != state:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch")
            return
        auth_code_holder["code"] = params.get("code", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "認証完了!このタブは閉じてOKです。".encode("utf-8")
        )

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler)
threading.Thread(target=server.handle_request, daemon=True).start()

auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
    {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
    }
)
print("ブラウザで認証ページを開きます...")
webbrowser.open(auth_url)
print(f"開かない場合はこのURLへ: {auth_url}")

# コールバック待ち
import time

while "code" not in auth_code_holder:
    time.sleep(0.5)

print("認証コードを取得。トークンに交換中...")
auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
resp = requests.post(
    "https://accounts.spotify.com/api/token",
    data={
        "grant_type": "authorization_code",
        "code": auth_code_holder["code"],
        "redirect_uri": REDIRECT_URI,
    },
    headers={"Authorization": f"Basic {auth_header}"},
    timeout=30,
)
resp.raise_for_status()
tokens = resp.json()

print("\n" + "=" * 60)
print("SPOTIFY_REFRESH_TOKEN (GitHub Secretsに登録してください):")
print("=" * 60)
print(tokens["refresh_token"])
print("=" * 60)
print("\n※このトークンは他人に見せないでください。")
