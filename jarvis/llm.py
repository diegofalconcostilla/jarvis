"""The two brains, token-optimized.

- ollama(): local models do the bulk (drafting, summarizing, classifying, embeddings). Free, private, offline.
- claude(): short judgment calls through Claude Code logged in with the SUBSCRIPTION (`claude -p`), never an API key.
  Each call replaces Claude Code's large agent system prompt and disables tools/MCP, which cuts input from ~20k tokens
  to a few hundred. The plan's own usage report (5-hour and 7-day windows) is saved after every call and gates the next.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import urllib.error
import urllib.request

from . import guard

OLLAMA_URL = os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
STATE = Path.home() / ".jarvis"
USAGE_FILE = STATE / "claude_usage.json"

# Claude usage budget: leave most of the subscription for the owner's own use.
FIVE_HOUR_MAX = float(os.environ.get("JARVIS_FIVE_HOUR_MAX", "0.5"))
SEVEN_DAY_MAX = float(os.environ.get("JARVIS_SEVEN_DAY_MAX", "0.6"))
WEEK_PACE_MARGIN = 0.15
WEEK = 7 * 86400


class ClaudeUnavailable(Exception):
    pass


# --- Ollama --------------------------------------------------------------------------
# Standard library only (no third-party HTTP client): fewer dependencies, less supply-chain risk.

def _http(path: str, body: dict | None = None, timeout: int = 900) -> dict:
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def installed_models() -> list[str]:
    return [m["name"] for m in _http("/api/tags", timeout=10).get("models", [])]


def ollama(prompt: str, model: str, *, system: str | None = None, temperature: float = 0.3,
           num_ctx: int = 8192, json_mode: bool = False) -> str:
    """One local generation. Only models already installed (installing needs consent: setup/03_pull_model.sh)."""
    guard.assert_no_api_billing()
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx}}
    if system:
        body["system"] = system
    if json_mode:
        body["format"] = "json"
    try:
        return _http("/api/generate", body)["response"].strip()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"model {model} isn't installed; install it only with the owner's consent") from e
        raise


# --- Claude (subscription only) ------------------------------------------------------

def usage() -> dict | None:
    try:
        return json.loads(USAGE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _util(window: dict | None) -> float:
    if not window or (window.get("resetsAt") or 0) <= time.time():
        return 0.0
    return float(window.get("utilization") or 0.0)


def usage_problem() -> str | None:
    u = usage()
    if not u:
        return None
    if u.get("status") == "rejected" and (u.get("resetsAt") or 0) > time.time():
        return "subscription limit reached"
    five, week = _util(u.get("five_hour")), _util(u.get("seven_day"))
    if five >= FIVE_HOUR_MAX:
        return f"5-hour window at {five:.0%} (Jarvis limit {FIVE_HOUR_MAX:.0%})"
    if week > 0:
        elapsed = 1.0 - max(0.0, u["seven_day"]["resetsAt"] - time.time()) / WEEK
        allowed = min(SEVEN_DAY_MAX, elapsed + WEEK_PACE_MARGIN)
        if week >= allowed:
            return f"7-day window at {week:.0%}, ahead of pace (allowed {allowed:.0%})"
    return None


def claude(prompt: str, *, system: str = "Answer concisely. Follow the requested format exactly.",
           model: str = "sonnet", timeout: int = 300) -> str:
    """One short, tool-less Claude call on the subscription. Raises ClaudeUnavailable when over budget."""
    guard.assert_no_api_billing()  # an API key in the environment would switch Claude Code to per-token billing
    problem = usage_problem()
    if problem:
        raise ClaudeUnavailable(problem)
    cwd = STATE / "claude_cwd"  # empty dir: Claude Code picks up no project files as context
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in guard.API_KEY_VARS}
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--max-turns", "1", "--model", model,
           "--system-prompt", system, "--tools", "", "--strict-mcp-config"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise ClaudeUnavailable(str(e)) from e
    result = None
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "rate_limit_event" and msg.get("rate_limit_info"):
            info = msg["rate_limit_info"]
            w = info.get("unifiedWindows") or {}
            if not w:  # no subscription windows reported: not a subscription login -> stop, don't risk billing
                raise ClaudeUnavailable("Claude Code isn't logged in with a subscription (no usage windows reported)")
            USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps({"ts": time.time(), "status": info.get("status"),
                                              "resetsAt": info.get("resetsAt"),
                                              "five_hour": w.get("five_hour"), "seven_day": w.get("seven_day")}))
        elif msg.get("type") == "result":
            result = msg
    if not result or result.get("is_error") or proc.returncode != 0:
        raise ClaudeUnavailable(str((result or {}).get("result") or proc.stderr)[:300])
    return result["result"]


def ask(prompt: str, local_model: str, *, allow_claude: bool = False) -> tuple[str, str]:
    """Ollama first. Claude only when explicitly allowed AND within budget. Returns (answer, source)."""
    if allow_claude:
        try:
            return claude(prompt), "claude"
        except ClaudeUnavailable:
            pass
    return ollama(prompt, local_model), "ollama"
