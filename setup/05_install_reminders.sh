#!/usr/bin/env bash
# Step 5 (optional): daily agenda reminders on Telegram (03:30; weekly overview on Mondays). Asks first.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
read -r -p "Install the daily agenda reminders (Telegram, 03:30)? [y/N] " a
[[ "$a" == "y" || "$a" == "Y" ]] || { echo "Skipped."; exit 0; }
[ -f "$HOME/.jarvis/.env" ] || echo "Note: put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in ~/.jarvis/.env (chmod 600)."
DEST="$HOME/Library/LaunchAgents/com.jarvis.reminders.plist"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/setup/reminders.plist.template" > "$DEST"
launchctl bootout "gui/$(id -u)/com.jarvis.reminders" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed com.jarvis.reminders (daily 03:30)."
