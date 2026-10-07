"""python -m jarvis <command>

  check [--claude]        audit: no API keys anywhere, Ollama local-only, models, Claude CLI (+1 tiny call with --claude)
  ask "question"          answer with the local model (JARVIS_MODEL); --claude lets Claude answer when within budget
  consent list|grant X|revoke X
  scan FILE...            look for secrets (the pre-commit hook runs this)
  google-login            at the Mac: approve read-only Gmail + Calendar access (opens the browser)
  agenda [days]           show the stored activities for the next N days (default 14)
  agenda-scan             scan new email + calendar now (scheduled daily 03:00; needs consent: email / calendar)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import consent, guard, llm


def check(with_claude: bool) -> int:
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'OK' if good else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    report("no API-billing keys in this shell", not guard.api_keys_in_env(), ", ".join(guard.api_keys_in_env()))
    report("no API-billing keys in launchd", not guard.api_keys_in_launchd(), ", ".join(guard.api_keys_in_launchd()))
    report("no API-billing keys in shell profiles", not guard.api_keys_in_profiles(),
           "; ".join(guard.api_keys_in_profiles()))
    try:
        models = llm.installed_models()
        report("Ollama reachable on 127.0.0.1", True, f"{len(models)} model(s): {', '.join(models) or 'none'}")
    except Exception as e:  # noqa: BLE001
        report("Ollama reachable on 127.0.0.1", False, str(e)[:120])
    host = os.environ.get("OLLAMA_HOST", "")
    listen = subprocess.run("lsof -nP -iTCP:11434 -sTCP:LISTEN", shell=True, capture_output=True, text=True).stdout
    report("Ollama listens on localhost only", "*:11434" not in listen and "0.0.0.0" not in host,
           "exposed to the network" if "*:11434" in listen else "")
    model = os.environ.get("JARVIS_MODEL")
    report("JARVIS_MODEL set", bool(model), model or "export JARVIS_MODEL=<an installed Ollama model>")
    report("Claude Code CLI installed", shutil.which("claude"), shutil.which("claude") or "see setup/04_setup_claude.sh")
    if with_claude:
        try:
            reply = llm.claude("Reply with exactly: ok")
            u = llm.usage() or {}
            report("Claude on the subscription", "ok" in reply.lower(),
                   f"5h {llm._util(u.get('five_hour')):.0%}, 7d {llm._util(u.get('seven_day')):.0%}")
        except Exception as e:  # noqa: BLE001
            report("Claude on the subscription", False, str(e)[:160])
    hook = Path(__file__).resolve().parent.parent / ".git" / "hooks" / "pre-commit"
    report("secret-scanning pre-commit hook", hook.exists(), "" if hook.exists() else "run setup/install_hooks.sh")
    print("consents granted:", ", ".join(consent.listing()) or "none")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(prog="jarvis")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--claude", action="store_true", help="also make one tiny Claude call to verify the subscription")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--claude", action="store_true", help="allow Claude (subscription, within budget)")
    k = sub.add_parser("consent")
    k.add_argument("action", choices=["list", "grant", "revoke"])
    k.add_argument("scope", nargs="?")
    k.add_argument("--note", default="")
    sub.add_parser("google-login")
    ag = sub.add_parser("agenda")
    ag.add_argument("days", nargs="?", type=int, default=730)
    sub.add_parser("agenda-scan")
    s = sub.add_parser("scan")
    s.add_argument("files", nargs="+")
    args = p.parse_args()

    if args.cmd == "check":
        sys.exit(check(args.claude))
    if args.cmd == "ask":
        model = os.environ.get("JARVIS_MODEL")
        if not model:
            sys.exit("Set JARVIS_MODEL to an installed Ollama model (python -m jarvis check lists them).")
        answer, source = llm.ask(args.question, model, allow_claude=args.claude)
        print(f"{answer}\n\n({source})")
    elif args.cmd == "consent":
        if args.action == "list":
            for scope, meta in consent.listing().items():
                print(f"{scope}: granted {meta['granted_at']} {meta.get('note', '')}")
        elif not args.scope:
            sys.exit("give a scope, e.g. calendar")
        elif args.action == "grant":
            consent.grant(args.scope, args.note)
        else:
            consent.revoke(args.scope)
    elif args.cmd == "google-login":
        from . import google
        google.login()
    elif args.cmd == "agenda":
        from datetime import date, timedelta
        from . import agenda
        items = agenda.between(date.today(), date.today() + timedelta(args.days))
        print("\n".join(agenda.fmt(i) for i in items) or "No activities in that range.")
    elif args.cmd == "agenda-scan":
        from . import google
        google.run()
    elif args.cmd == "scan":
        bad = 0
        for f in args.files:
            try:
                hits = guard.find_secrets(Path(f).read_text(errors="replace"))
            except (OSError, IsADirectoryError):
                continue
            for h in hits:
                print(f"{f}: looks like a {h}")
                bad += 1
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
