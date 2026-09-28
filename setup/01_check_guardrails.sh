#!/usr/bin/env bash
# Step 1: audit before anything else. Fails if any API-billing key is set (env, launchd, shell profiles).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m jarvis check "$@"
