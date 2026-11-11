#!/usr/bin/env bash
# Step 7 (optional): home monitoring through Home Assistant (read-only, every 5 minutes). Asks first.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cat <<'EOF'
Jarvis will READ your Home Assistant sensors every 5 minutes (it never switches or unlocks anything),
keep 7 days of changes in ~/.jarvis/home/, and let the local model decide what's worth a Telegram message.
Before this works you need:
  1. In Home Assistant: a non-admin user "jarvis" -> its profile -> Security -> Long-lived access token.
  2. In ~/.jarvis/.env (chmod 600):  HA_URL=http://homeassistant.local:8123  and  HA_TOKEN=<the token>
  3. Your consent, at this terminal:  python3 -m jarvis consent grant home
EOF
read -r -p "Install the home check (launchd, every 5 minutes)? [y/N] " a
[[ "$a" == "y" || "$a" == "Y" ]] || { echo "Skipped."; exit 0; }
grep -q '^HA_TOKEN=' "$HOME/.jarvis/.env" 2>/dev/null || echo "Note: HA_TOKEN isn't in ~/.jarvis/.env yet (step 2)."
(cd "$REPO" && /usr/bin/python3 -m jarvis home init)
DEST="$HOME/Library/LaunchAgents/com.jarvis.home.plist"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/setup/home.plist.template" > "$DEST"
launchctl bootout "gui/$(id -u)/com.jarvis.home" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed com.jarvis.home (every 5 minutes). Test now: python3 -m jarvis home check"
echo "Log: ~/Library/Logs/jarvis-home.log"
