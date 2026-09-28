"""Guardrails that keep Jarvis free to run and private.

1. No API-billed models: if any provider API key is visible to a process, Claude Code (and others) may bill per token
   instead of using the subscription. Jarvis refuses to run model calls while one is set.
2. No secrets in git: a scanner the pre-commit hook runs on staged files.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Variables that switch a tool to pay-per-token API billing (or leak a paid credential to a child process).
API_KEY_VARS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY", "COHERE_API_KEY", "HF_TOKEN",
]

SECRET_PATTERNS = {
    "Anthropic key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "OpenAI-style key": r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}",
    "Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Google API key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
    "Private key block": r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
}


def api_keys_in_env(env=None) -> list[str]:
    """Names of billing-related variables that are set (non-empty) in this process's environment."""
    env = os.environ if env is None else env
    return [k for k in API_KEY_VARS if env.get(k, "").strip()]


def api_keys_in_launchd() -> list[str]:
    """Same check for launchd's user environment (what scheduled jobs inherit). macOS only; [] elsewhere."""
    found = []
    for k in API_KEY_VARS:
        try:
            out = subprocess.run(["launchctl", "getenv", k], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.strip():
            found.append(k)
    return found


def api_keys_in_profiles(home: Path | None = None) -> list[str]:
    """Shell profiles that export a billing-related variable."""
    home = home or Path.home()
    hits = []
    for name in (".zshrc", ".zprofile", ".zshenv", ".bash_profile", ".bashrc", ".profile"):
        f = home / name
        if f.exists():
            text = f.read_text(errors="replace")
            for k in API_KEY_VARS:
                if re.search(rf"^\s*(export\s+)?{k}\s*=\s*\S", text, re.M):
                    hits.append(f"{name}: {k}")
    return hits


def assert_no_api_billing():
    """Raise if anything could make a model call bill an API account."""
    problems = api_keys_in_env()
    if problems:
        raise PermissionError(
            f"Refusing to call a model: {', '.join(problems)} is set, which can switch to pay-per-token API billing. "
            "Unset it (Jarvis only uses the Claude subscription and local Ollama).")


def find_secrets(text: str) -> list[str]:
    return [name for name, pat in SECRET_PATTERNS.items() if re.search(pat, text)]
