#!/usr/bin/env bash
# Step 4: Claude Code on your SUBSCRIPTION (Pro/Max). Never with an API key: that bills per token.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
from jarvis import guard
keys = guard.api_keys_in_env() + guard.api_keys_in_launchd() + guard.api_keys_in_profiles()
if keys:
    raise SystemExit(f"Stop: API-billing variables are set ({', '.join(keys)}). Remove them first; "
                     "Claude Code would use them instead of your subscription.")
PY
if ! command -v claude >/dev/null; then
  read -r -p "Claude Code isn't installed. Install it with Anthropic's official installer (claude.ai/install.sh)? [y/N] " a
  [[ "$a" == "y" || "$a" == "Y" ]] || { echo "Skipped."; exit 0; }
  curl -fsSL https://claude.ai/install.sh | bash
fi
cat <<'TXT'

Log in (once, interactive):
  1. Run:  claude
  2. Choose "Claude account with subscription" (Pro/Max). Do NOT choose "Anthropic Console / API usage billing".
  3. Finish the browser login, then type /exit.

Then verify it's on the subscription (one tiny call; Jarvis refuses to run if no subscription usage windows come back):
  python3 -m jarvis check --claude
TXT
