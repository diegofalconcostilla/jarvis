#!/usr/bin/env bash
# Step 2: Ollama as a memory-safe, localhost-only background service. Asks before installing anything.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
confirm() { read -r -p "$1 [y/N] " a; [[ "$a" == "y" || "$a" == "Y" ]]; }

if ! command -v ollama >/dev/null && [ ! -x /opt/homebrew/bin/ollama ]; then
  confirm "Ollama isn't installed. Install it with Homebrew (brew install ollama)?" || { echo "Skipped."; exit 0; }
  brew install ollama
fi

if lsof -nP -iTCP:11434 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "An Ollama server is already running on this Mac; reusing it (not installing a second service)."
  lsof -nP -iTCP:11434 -sTCP:LISTEN | awk 'NR>1 {print "  listening:", $9}'
  exit 0
fi

confirm "Install the Jarvis Ollama service (starts at login, localhost only, one model in memory at a time)?" \
  || { echo "Skipped."; exit 0; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
DEST="$HOME/Library/LaunchAgents/com.jarvis.ollama.plist"
sed -e "s#__HOME__#$HOME#g" -e "s#__OLLAMA__#$(command -v ollama || echo /opt/homebrew/bin/ollama)#g" \
  "$REPO/setup/ollama.plist.template" > "$DEST"
launchctl bootout "gui/$(id -u)/com.jarvis.ollama" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Ollama service loaded (com.jarvis.ollama). No models are installed by this step: use setup/03_pull_model.sh."
