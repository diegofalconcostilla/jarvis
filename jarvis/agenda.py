"""Agenda: activities with dates, extracted locally by Ollama, stored privately, reminded on Telegram.

Privacy model:
- The store lives in ~/.jarvis/agenda.json (owner-only, never in git). Both the local model and Claude sessions on
  this Mac can read it; nothing here sends it anywhere except the short reminders.
- Extraction runs on a LOCAL model (Ollama). Email text never goes to Claude or any cloud model.
- Reading real email requires the owner's consent (`consent.require("email")`); extracting from text you pass in
  (tests, pasted messages) does not.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import llm

STORE = Path.home() / ".jarvis" / "agenda.json"
IGNORE = Path.home() / ".jarvis" / "agenda_ignore.json"  # title words the owner removed: never re-added
MODEL = "qwen2.5:7b"  # small local model: enough for extraction, light on memory
# The owner's choice (2026-10-06): for now, only these get into the agenda. Everything else is dropped in code.
ALLOWED_KINDS = ("flight", "ticket", "airbnb")

EXTRACT_PROMPT = """You extract bookings from ONE email. Today is {today} ({weekday}). The email was received
on {received}. For relative dates ("tomorrow", "next Friday"), DON'T calculate: look the day up in this calendar
(the days after the email was received):
{calendar}

Return ONLY this JSON:
{{"activities": [{{"title": "short name (<= 60 chars)", "when_text": "the exact words in the email that give the day
                  (e.g. \"next Friday\", \"this Thursday\", \"October 13\")", "date": "YYYY-MM-DD", "time": "HH:MM 24h or empty",
                  "end_date": "YYYY-MM-DD or empty (multi-day trips, stays)", "location": "place or empty",
                  "kind": "flight|ticket|airbnb",
                  "status": "confirmed|tentative|cancelled|rescheduled"}}]}}

Rules:
- Extract ONLY these three kinds, and only when the email CONFIRMS a booking the reader made:
  * "flight": a flight reservation or itinerary (airline or travel agency), one activity per flight segment.
  * "ticket": a ticket confirmation for an event or transport (concert, show, sports, movie, museum, train, bus,
    ferry), including Ticketmaster order confirmations. NOT support tickets, NOT parking or traffic tickets, NOT
    Ticketmaster marketing ("presale", "on sale now", "recommended for you").
  * "airbnb": an Airbnb reservation (date = check-in, end_date = check-out, location = city or address).
- EVERYTHING ELSE returns {{"activities": []}}: meetings, appointments, deadlines, deliveries, restaurant
  reservations, hotel or car bookings, bills, reminders, marketing, newsletters, "sale until Friday".
- A reschedule: one activity with the NEW date and status "rescheduled". A cancellation: status "cancelled".
- Never invent a date or time that isn't in the email; leave time empty if not given. Keep the time for cancelled
  or rescheduled activities too.

Email:
From: {sender}
Subject: {subject}

{body}"""


def _load() -> list[dict]:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]):
    STORE.parent.mkdir(mode=0o700, exist_ok=True)
    STORE.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    STORE.chmod(0o600)


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def resolve_relative(text: str, received: date) -> date | None:
    """Relative day words -> exact date, in code (small models get weekday arithmetic wrong).
    'today'/'tomorrow'; 'this X' or 'X' = the next X from the received day; 'next X' = X in the following week."""
    t = (text or "").lower().strip()
    if re.search(r"\b(today|hoy)\b", t):
        return received
    if re.search(r"\b(tomorrow|mañana)\b", t):
        return received + timedelta(1)
    for names in (WEEKDAYS, WEEKDAYS_ES):
        for i, name in enumerate(names):
            if re.search(rf"\b{name}\b", t) and not re.search(r"\b\d{1,2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t):
                if re.search(r"\b(next|pr[oó]ximo)\b", t):  # X of the following calendar week (weeks start Monday)
                    next_monday = received + timedelta(7 - received.weekday())
                    return next_monday + timedelta(i)
                return received + timedelta((i - received.weekday()) % 7)  # this X: today if it's X
    return None  # an explicit date ("October 13"): trust the model's date


def _valid(a: dict, received: date | None = None) -> dict | None:
    rel = resolve_relative(str(a.get("when_text") or ""), received) if received else None
    try:
        d = rel or date.fromisoformat(str(a.get("date", "")))
    except ValueError:
        return None
    time = str(a.get("time") or "").strip()
    if time and not re.fullmatch(r"\d{2}:\d{2}", time):
        time = ""
    end = str(a.get("end_date") or "").strip()
    try:
        end = date.fromisoformat(end).isoformat() if end else ""
    except ValueError:
        end = ""
    title = str(a.get("title") or "").strip()[:60]
    kind = str(a.get("kind") or "").strip().lower()
    if not title or kind not in ALLOWED_KINDS:
        return None
    return {"title": title, "date": d.isoformat(), "time": time, "end_date": end,
            "location": str(a.get("location") or "").strip()[:80],
            "kind": kind, "status": str(a.get("status") or "confirmed")}


PROMO = re.compile(r"(\b\d{1,2}% off|\bsale\b|\bdeals?\b|\bshop now\b|\bpromo|\bcoupon|\bnewsletter\b|"
                   r"\bunsubscribe\b|^(deals|offers|marketing|news|promo)@)", re.I)


# Senders whose confirmations look like marketing (Gmail files them under Promotions): never pre-filtered as promo;
# the model still decides whether the email confirms a booking. The owner asked for Ticketmaster (2026-10-06).
TRUSTED_SENDERS = ("ticketmaster.com",)


def looks_promotional(sender: str, subject: str, body: str) -> bool:
    """Cheap code filter before the model: marketing mail never becomes an activity."""
    addr = re.search(r"<?([\w.+-]+@[\w.-]+)>?", sender or "")
    if addr and addr.group(1).lower().endswith(TRUSTED_SENDERS):
        return False
    hits = len(PROMO.findall(f"{subject}\n{body[:1500]}")) + (1 if addr and PROMO.search(addr.group(1)) else 0)
    return hits >= 2


def _calendar(received: date, days: int = 21) -> str:
    return "\n".join(f"{(received + timedelta(n)).strftime('%A %b %d %Y')} = {(received + timedelta(n)).isoformat()}"
                     + (" (today)" if n == 0 else " (tomorrow)" if n == 1 else "") for n in range(days))


def extract(sender: str, subject: str, body: str, received: date | None = None) -> list[dict]:
    """Local model -> validated activities (bad dates/fields dropped, never guessed)."""
    received = received or date.today()
    if looks_promotional(sender, subject, body):
        return []
    raw = llm.ollama(EXTRACT_PROMPT.format(today=date.today().isoformat(), weekday=date.today().strftime("%A"),
                                           calendar=_calendar(received),
                                           received=received.isoformat(), sender=sender, subject=subject,
                                           body=body[:6000]), MODEL, temperature=0.1, json_mode=True)
    try:
        acts = json.loads(raw).get("activities", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    acts = [v for v in (_valid(a, received) for a in acts if isinstance(a, dict)) if v]
    return [a for a in acts if a["kind"] != "airbnb" or "airbnb" in f"{sender} {subject} {body}".lower()]


CLASSIFY_PROMPT = """Classify ONE agenda entry. Answer with ONLY this JSON: {{"kind": "flight|ticket|airbnb|none"}}
- flight: a flight reservation. ticket: a ticket for an event or transport (concert, show, sports, movie, museum,
  train, bus, ferry); not support/parking tickets. airbnb: an Airbnb stay.
- Anything else (meetings, appointments, deadlines, deliveries, restaurants, hotels, other events): "none".

Title: {title}
Place: {location}
Came from: {source}"""


def classify(item: dict) -> str:
    """Kind of an already-stored or calendar entry ('' if it isn't an allowed kind). Local model only."""
    raw = llm.ollama(CLASSIFY_PROMPT.format(title=item.get("title", ""), location=item.get("location", ""),
                                            source=item.get("source", "")), MODEL, temperature=0, json_mode=True)
    try:
        kind = str(json.loads(raw).get("kind", "")).lower()
    except (json.JSONDecodeError, AttributeError):
        return ""
    if kind == "airbnb" and "airbnb" not in json.dumps(item).lower():
        return ""  # hotels etc. aren't Airbnb stays
    return kind if kind in ALLOWED_KINDS else ""


def prune() -> tuple[int, int]:
    """Re-check every stored entry and drop the ones that aren't an allowed kind. Returns (kept, removed)."""
    items, keep = _load(), []
    for i in items:
        kind = classify(i)
        if kind:
            keep.append({**i, "kind": kind})
    _save(keep)
    return len(keep), len(items) - len(keep)


def upsert(activities: list[dict], source: str = "") -> int:
    """Add or update activities. Same title on the same day = same activity; cancellations mark it cancelled."""
    items, added = _load(), 0
    ignored = json.loads(IGNORE.read_text()) if IGNORE.exists() else []
    for a in activities:
        if any(w in a["title"].lower() for w in ignored):
            continue
        key = hashlib.sha1(f"{a['title'].lower()}|{a['date']}".encode()).hexdigest()[:12]
        a = {**a, "id": key, "source": source[:120], "updated": datetime.now().isoformat(timespec="seconds")}
        if a["status"] == "rescheduled":  # drop the old date of the same activity
            items = [i for i in items if not (i["title"].lower() == a["title"].lower() and i["date"] != a["date"])]
        old = next((i for i in items if i["id"] == key), None)
        if old:
            old.update(a)
        else:
            items.append(a)
            added += 1
    _save(sorted(items, key=lambda i: (i["date"], i["time"] or "99:99")))
    return added


def between(start: date, end: date, include_cancelled: bool = False) -> list[dict]:
    return [i for i in _load() if start.isoformat() <= i["date"] <= end.isoformat()
            and (include_cancelled or i["status"] != "cancelled")]


def fmt(i: dict, with_day: bool = True) -> str:
    d = date.fromisoformat(i["date"])
    when = (d.strftime("%a %b %d") if with_day else "") + (f" {i['time']}" if i["time"] else "")
    extra = f" · {i['location']}" if i["location"] else ""
    flag = " (rescheduled)" if i["status"] == "rescheduled" else " (tentative)" if i["status"] == "tentative" else ""
    return f"• {when.strip()}: {i['title']}{extra}{flag}"
