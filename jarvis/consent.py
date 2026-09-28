"""Consent registry: Jarvis touches nothing private unless the owner granted that scope.

Grants live in ~/.jarvis/consent.json (outside the repo, never committed). Only a human at an interactive terminal can
grant a scope (`python -m jarvis consent grant <scope>` asks y/N); code, scripts and AI sessions can only check.

Scopes are plain names, e.g. "email", "calendar", "contacts", "messages", "browser-history", "photos",
"files:~/Documents", "model-install:<name>". Anything not listed here counts as NOT granted.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

STORE = Path.home() / ".jarvis" / "consent.json"

# Scopes that always need explicit consent. Code must call require() before touching any of them.
PRIVATE_SCOPES = ["email", "calendar", "contacts", "messages", "browser-history", "photos", "location",
                  "keychain", "files", "microphone", "camera", "payments"]


class ConsentRequired(PermissionError):
    pass


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def granted(scope: str) -> bool:
    return scope in _load()


def require(scope: str, why: str = ""):
    """Call before touching private data or installing anything. Raises unless the owner granted it."""
    if not granted(scope):
        raise ConsentRequired(
            f"'{scope}' needs the owner's consent{f' ({why})' if why else ''}. "
            f"Ask them to run: python -m jarvis consent grant {scope}")


def grant(scope: str, note: str = "") -> bool:
    """Interactive only: refuses when not attached to a terminal (so no script or model can grant itself)."""
    if not sys.stdin.isatty():
        print("Consent can only be granted by a person at an interactive terminal.", file=sys.stderr)
        return False
    answer = input(f"Allow Jarvis to access '{scope}'? {note} [y/N] ").strip().lower()
    if answer != "y":
        print("Not granted.")
        return False
    data = _load()
    data[scope] = {"granted_at": datetime.now().isoformat(timespec="seconds"), "note": note}
    STORE.parent.mkdir(mode=0o700, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=1))
    STORE.chmod(0o600)
    print(f"Granted '{scope}'. Revoke any time: python -m jarvis consent revoke {scope}")
    return True


def revoke(scope: str):
    data = _load()
    if data.pop(scope, None) is not None:
        STORE.write_text(json.dumps(data, indent=1))
        print(f"Revoked '{scope}'.")
    else:
        print(f"'{scope}' wasn't granted.")


def listing() -> dict:
    return _load()
