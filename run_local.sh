#!/bin/zsh
# Mac mini launchd用: 毎朝実行し、状態をgit pushする
set -e
cd "$(dirname "$0")"

set -a
source .env
set +a

/usr/bin/git pull --rebase origin main

/usr/bin/python3 -u check_releases.py

/usr/bin/git add seen_releases.json docs/releases.ics
if ! /usr/bin/git diff --cached --quiet; then
    /usr/bin/git commit -m "Update releases $(date +%Y-%m-%d)"
    /usr/bin/git push origin main
fi
