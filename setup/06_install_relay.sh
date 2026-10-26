#!/usr/bin/env bash
# Step 6 (optional): let AI sessions ASK Jarvis to do things (agenda check, local actions) without using Jarvis's
# private permissions themselves. Jarvis does the work outside the session and answers on Telegram. Asks first.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# --yes: the owner typed the confirmation into the command itself (shells without a keyboard prompt)
if [[ "${1:-}" == "--yes" ]]; then a=y
else read -r -p "Install the Jarvis request relay (runs owner requests, answers on Telegram)? [y/N] " a || a=""; fi
[[ "$a" == "y" || "$a" == "Y" ]] || { echo "Skipped (run it again and answer y, or add --yes)."; exit 0; }
mkdir -p -m 700 "$HOME/.jarvis/requests/inbox" "$HOME/.jarvis/requests/done"
DEST="$HOME/Library/LaunchAgents/com.jarvis.relay.plist"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/setup/relay.plist.template" > "$DEST"
launchctl bootout "gui/$(id -u)/com.jarvis.relay" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed com.jarvis.relay."
