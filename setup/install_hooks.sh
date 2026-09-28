#!/usr/bin/env bash
# Installs the secret-scanning pre-commit hook into this clone.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cp "$REPO/setup/hooks/pre-commit" "$REPO/.git/hooks/pre-commit"
chmod +x "$REPO/.git/hooks/pre-commit"
echo "pre-commit hook installed: commits containing secrets or .env files are blocked."
