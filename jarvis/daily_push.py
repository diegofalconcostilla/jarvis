"""Daily push (launchd com.jarvis.daily-push, 20:00): commit + push the day's work in every listed repo, but only after
validating that nothing sensitive is in it.

Repos are listed in ~/.jarvis/daily_push.json ({"repos": [...], "personal": [...]}, private, never in git).
Checks on everything staged (new/changed files, added lines):
  - private files by name (.env, keys, consent/agenda stores, databases)
  - secret patterns (bot tokens, API keys, GitHub/AWS/Google keys, private key blocks)
  - the ACTUAL secret values from the owner's .env files (exact match anywhere in the added text)
  - personal data: the owner's emails/IDs listed in "personal", private-network IPs; in PUBLIC repos any real email
A repo with a finding is NOT committed (staging undone) and the owner gets one Telegram message: file + reason, never
the value. Clean repos are committed ("daily: <date>") and pushed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from . import guard
from .reminders import send

CONF = Path.home() / ".jarvis" / "daily_push.json"
ENV_FILES = [Path.home() / ".jarvis" / ".env"]
BLOCKED_NAMES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|id_(rsa|ed25519)\w*|consent\.json|agenda\.json|"
                           r".*\.(db|sqlite3?)|reminders_sent\.json)$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SAFE_EMAIL = re.compile(r"(noreply|no-reply|example\.(com|org)|users\.noreply\.github\.com|anthropic\.com)", re.I)
PRIVATE_IP = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
ATTRIBUTION = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _git(repo: Path, *args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)


def _secret_values(extra_env: list[Path]) -> list[str]:
    vals = []
    for f in ENV_FILES + extra_env:
        if f.exists():
            for line in f.read_text(errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    v = line.split("=", 1)[1].strip().strip('"')
                    if len(v) >= 8:
                        vals.append(v)
    return vals


def scan(repo: Path, public: bool, personal: list[str]) -> list[str]:
    """Findings in what's staged (file: reason). Empty = safe to commit."""
    staged = [f for f in _git(repo, "diff", "--cached", "--name-only").stdout.splitlines() if f]
    added = "\n".join(l[1:] for l in _git(repo, "diff", "--cached", "-U0").stdout.splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    secrets = _secret_values([repo / ".env"])
    findings = [f"{f}: private file (never committed)" for f in staged if BLOCKED_NAMES.search(f)]
    for f in staged:
        p = repo / f
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        text = p.read_text(errors="replace")
        for kind in guard.find_secrets(text):
            findings.append(f"{f}: looks like a {kind}")
        if any(v in text for v in secrets):
            findings.append(f"{f}: contains one of your actual secret values (.env)")
    for p in personal:
        if p and p.lower() in added.lower():
            findings.append(f"added lines contain your personal info ({p[:3]}…)")
    if PRIVATE_IP.search(added):
        findings.append("added lines contain a private network IP address")
    if public:
        emails = {m for m in EMAIL.findall(added) if not SAFE_EMAIL.search(m)}
        if emails:
            findings.append(f"public repo: {len(emails)} real email address(es) in added lines")
    return sorted(set(findings))


def run():
    conf = json.loads(CONF.read_text()) if CONF.exists() else {"repos": [], "personal": []}
    today, report = date.today().isoformat(), []
    for entry in conf["repos"]:
        repo = Path(entry["path"]).expanduser()
        if not (repo / ".git").exists():
            continue
        # The incubator ships its day build at 20:00: wait (up to an hour) for running jobs before staging.
        if entry.get("wait_for_jobs"):
            db = repo / "state" / "incubator.db"
            for _ in range(60):
                busy = subprocess.run(["sqlite3", str(db), "select count(*) from jobs where status='running';"],
                                      capture_output=True, text=True).stdout.strip()
                if busy in ("", "0"):
                    break
                time.sleep(60)
        _git(repo, "add", "-A")
        if not _git(repo, "diff", "--cached", "--name-only").stdout.strip():
            if _git(repo, "rev-list", "--count", "@{u}..HEAD").stdout.strip() not in ("", "0"):
                _git(repo, "push", "-q")
            continue
        findings = scan(repo, entry.get("public", False), conf.get("personal", []))
        if findings:
            _git(repo, "reset", "-q")
            report.append(f"🚫 {repo.name}: NOT pushed\n" + "\n".join(f"  • {x}" for x in findings[:8]))
            continue
        n = len(_git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
        _git(repo, "commit", "-q", "-m", f"daily: {today}\n\n{n} file(s) changed; sensitive-data scan passed.\n\n{ATTRIBUTION}")
        push = _git(repo, "push", "-q", timeout=180)
        if push.returncode != 0:
            report.append(f"⚠️ {repo.name}: committed but push failed (retried tomorrow)")
    if report:  # only bother the owner when something was blocked or failed
        send("🗂 Daily push (20:00)\n" + "\n".join(report))
    return report


if __name__ == "__main__":
    for line in run() or ["all repos pushed, scan clean"]:
        print(line)
