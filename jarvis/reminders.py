"""Agenda reminders on Telegram: a weekly overview on Monday and a reminder on the day of each activity.

Runs once a day (launchd com.jarvis.reminders, 03:30). Reads only ~/.jarvis/agenda.json; sends short lines (title,
time, place), never email content. Telegram credentials come from ~/.jarvis/.env (owner-only, never in git).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from . import agenda

ENV = Path.home() / ".jarvis" / ".env"
SENT = Path.home() / ".jarvis" / "reminders_sent.json"  # so a re-run never sends the same reminder twice


def _creds() -> tuple[str, str]:
    vals = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"')
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID", ""))


def send(text: str, silent: bool = False) -> bool:
    token, chat = _creds()
    if not token or not chat:
        print(text)  # no credentials: print instead of sending
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true",
                                   "disable_notification": "true" if silent else "false"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
        return json.loads(r.read()).get("ok", False)


def _sent() -> dict:
    try:
        return json.loads(SENT.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def run(today: date | None = None, dry_run: bool = False) -> list[str]:
    """Send today's reminders (and the weekly overview on Mondays). Returns the messages."""
    today = today or date.today()
    sent, msgs = _sent(), []
    if today.weekday() == 0 and sent.get("weekly") != today.isoformat():
        week = agenda.between(today, today + timedelta(6))
        if week:
            msgs.append("🗓 This week\n" + "\n".join(agenda.fmt(i) for i in week))
        sent["weekly"] = today.isoformat()
    todays = [i for i in agenda.between(today, today) if i["id"] not in sent.get(today.isoformat(), [])]
    if todays:
        msgs.append("⏰ Today\n" + "\n".join(agenda.fmt(i, with_day=False) for i in todays))
        sent[today.isoformat()] = sent.get(today.isoformat(), []) + [i["id"] for i in todays]
    if not dry_run:
        for m in msgs:
            send(m, silent=True)  # sent at 03:30: delivered without a sound, waiting for the morning
        sent = {k: v for k, v in sent.items() if k == "weekly" or k >= (today - timedelta(7)).isoformat()}
        SENT.parent.mkdir(mode=0o700, exist_ok=True)
        SENT.write_text(json.dumps(sent))
        SENT.chmod(0o600)
    return msgs


if __name__ == "__main__":
    run()
