"""Owner requests relayed by an AI session and carried out by Jarvis itself.

An AI session may not use Jarvis's private permissions (consent.require refuses inside one). Instead it drops a
small request in ~/.jarvis/requests/inbox/; launchd (QueueDirectories, com.jarvis.relay) starts this module
outside the session, which runs ONLY the whitelisted actions below with Jarvis's own permissions and sends the
result straight to the owner's Telegram. The session gets back a status ("sent" / "error: <kind>"), never the
private content.

    python3 -m jarvis.relay submit agenda --start 2026-11-21 --end 2026-11-22 [--wait 300]
    python3 -m jarvis.relay submit jobs_track --json '{"days": 3}'   # re-check job emails now
    python3 -m jarvis.relay run        # what launchd runs; refuses inside an AI session

Local-only actions (never in git) can be added in ~/jarvis/local/<name>/jarvis_actions.py as
ACTIONS = {"name": (function(params) -> str, {"param": type, ...})}.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import consent

HOME = Path.home() / ".jarvis" / "requests"
INBOX, DONE = HOME / "inbox", HOME / "done"
LOCAL = Path(__file__).resolve().parent.parent / "local"
MAX_DAYS = 31


def _write(path: Path, data: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(path)


# --- actions --------------------------------------------------------------------------------

def _days(start: str, end: str) -> tuple[date, date]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s or (e - s).days > MAX_DAYS:
        raise ValueError(f"date range must be 0-{MAX_DAYS} days")
    return s, e


def agenda_check(p: dict) -> str:
    """Calendar events + bookings found in email (local model) for the days asked; answer goes to Telegram."""
    from . import agenda, google
    from .reminders import send
    s, e = _days(p["start"], p["end"])
    days = [s + timedelta(n) for n in range((e - s).days + 1)]
    # email: messages naming one of the days, read by the local model only
    consent.require("email", "check your agenda for the days you asked")
    words = sorted({f'"{d:%B} {d.day}"' for d in days} | {f'"{d:%b} {d.day}"' for d in days}
                   | {f'"{d.month}/{d.day}"' for d in days})
    q = f"newer_than:180d ({' OR '.join(words)}) -category:promotions -category:social"
    stats = {"new": 0, "opened": 0, "activities": 0}
    for m in google._get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                         {"q": q, "maxResults": 30}).get("messages", []):
        google._process(m["id"], stats)
    # calendar: everything in the range
    consent.require("calendar", "check your agenda for the days you asked")
    tz = datetime.now().astimezone().tzinfo
    items = google._get("https://www.googleapis.com/calendar/v3/calendars/primary/events", {
        "timeMin": datetime.combine(s, datetime.min.time(), tz).isoformat(),
        "timeMax": datetime.combine(e + timedelta(1), datetime.min.time(), tz).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}).get("items", [])
    lines = []
    for ev in items:
        if ev.get("status") == "cancelled":
            continue
        st = ev.get("start", {})
        when = st.get("dateTime") or st.get("date") or ""
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        label = dt.astimezone().strftime("%a %b %d %H:%M") if "T" in when else dt.strftime("%a %b %d")
        lines.append(f"• {label}: {(ev.get('summary') or '(no title)')[:60]}"
                     + (f" · {ev['location'][:50]}" if ev.get("location") else "") + " (calendar)")
    have = {re.sub(r"\W+", "", l.lower()) for l in lines}
    lines += [agenda.fmt(i) for i in agenda.between(s, e) if re.sub(r"\W+", "", agenda.fmt(i).lower()) not in have]
    span = f"{s:%a %b %d}" + (f" – {e:%a %b %d}" if e != s else "")
    send(f"📅 {span}\n" + ("\n".join(lines) if lines else "Nothing scheduled (calendar and email bookings)."))
    return "sent"


def jobs_track(p: dict) -> str:
    """Re-read the last days of job emails now (local model) and send the application statuses to Telegram."""
    from . import jobs
    from .reminders import send
    days = p.get("days", 3)
    if not 1 <= days <= 14:
        raise ValueError("days must be 1-14")
    stats: dict = {}
    events = jobs.track(days=days, recheck=p.get("recheck", True), stats=stats, max_opened=300)
    active = sorted((a for a in jobs._load().get("applications", {}).values()
                     if a.get("status") not in ("rejected", "withdrawn")), key=lambda a: a.get("updated", ""), reverse=True)
    send(f"📬 Job emails, last {days} day(s): {stats.get('found', 0)} found, {stats.get('filtered', 0)} read by the "
         f"local model, {stats.get('classified', 0)} about your applications.\n"
         + ("New: " + "; ".join(f"{e['company']} → {e['status']}" for e in events) if events else "No new status changes.")
         + "\n\nActive applications:\n" + ("\n".join(f"• {a['status']}: {a['company']} — {a.get('role') or 'role?'} "
                                                  f"({a.get('updated', '')})" for a in active[:30]) or "none"))
    return "sent"


def _send_long(text: str):
    """Telegram caps a message at 4096 characters: send line-aligned parts."""
    from .reminders import send
    part = ""
    for line in text.split("\n"):
        if len(part) + len(line) + 1 > 3900:
            send(part)
            part = ""
        part += line[:3900] + "\n"
    if part.strip():
        send(part)


def jobs_report(p: dict) -> str:
    """Re-check which postings are still open, rebuild the applications PDF (positions + URLs) and send it."""
    from . import jobs
    from .reminders import send
    counts, result = jobs.refresh_open()
    jobs.report()
    st = jobs._load()["postings"]
    lines = [f"• {c}: {o} open, {x} closed" for c, (o, x) in sorted(counts.items()) if x]
    for name in p.get("check", []):
        mine = [q for q in st.values() if jobs._norm(q["company"]) == jobs._norm(name)
                and (q["key"] in result or q.get("status") == "closed" and q.get("folder"))]
        mark = {"open": "✅ open", "closed": "❌ closed", "unknown": "❔ board unreachable"}
        lines.append(f"\n🔎 {name}:" + ("".join(f"\n  {mark[result.get(q['key'], 'closed')]}"
                                               f"{' since ' + q['closed_on'] if q['key'] not in result and q.get('closed_on') else ''}"
                                               f": {q['title']}\n  {q['url']}" for q in mine) or " no postings tracked"))
    _send_long("📄 Positions re-checked against each company's board.\n" + ("\n".join(lines) if lines else "All still open.")
         + "\n\nClosed ones are listed at the end of the PDF (resumes not regenerated).")
    if jobs.REPORT_PDF.exists():
        jobs._send_file(jobs.REPORT_PDF, "Job applications: positions with their URLs")
    return "sent"


def jobs_positions(p: dict) -> str:
    """Send a PDF with only two columns: position and URL."""
    from . import jobs
    jobs._send_file(jobs.positions_pdf(), "Positions and URLs")
    return "sent"


def jobs_upload_folder(p: dict) -> str:
    """Renumber ~/Job Applications/Resumes para subir to match the applications PDF's order."""
    from . import jobs
    from .reminders import send
    n = jobs.upload_folder()
    send(f"📁 Resumes para subir: {n} resume(s) renumbered in the same order as Applications.pdf.")
    return "sent"


def jobs_location(p: dict) -> str:
    """Why a place has no positions: configured? visa rule? postings on the boards? Counts only, to Telegram."""
    from . import jobs
    place = p["place"]
    if place not in jobs.LOCATIONS:
        raise ValueError("unknown place")
    r = jobs.location_check(place)
    found = sum(m for m, _ in r["companies"].values())
    kept = sum(k for _, k in r["companies"].values())
    _send_long(f"📍 {place}: {found} posting(s) for your roles on your companies' boards, {kept} pass the visa rule.\n"
               f"• In your search locations: {'yes' if r['configured'] else 'NO (Jarvis never searches it)'}\n"
               f"• Visa needed there ({r['country']}): {'no' if r['no_visa_needed'] else 'YES, so postings need sponsorship evidence'}\n"
               + "\n".join(f"  {c}: {m} match, {k} kept" for c, (m, k) in sorted(r["companies"].items(), key=lambda x: -x[1][0])))
    return "sent"


def home_status(p: dict) -> str:
    """The watched Home Assistant sensors right now, to Telegram (read-only)."""
    from . import home
    from .reminders import send
    send(home.status_text(home.current()))
    return "sent"


def home_digest(p: dict) -> str:
    """The house's last 24 hours, summarized by the local model, to Telegram."""
    from . import home
    from .reminders import send
    cur = home.current()
    send(home.digest(home._read(home.HISTORY, {"events": [], "readings": {}}), cur, time.time(), home.config()))
    return "sent"


ACTIONS = {"agenda": (agenda_check, {"start": str, "end": str}),
           "home_status": (home_status, {}),
           "home_digest": (home_digest, {}),
           "jobs_location": (jobs_location, {"place": str}),
           "jobs_upload_folder": (jobs_upload_folder, {}),
           "jobs_positions": (jobs_positions, {}),
           "jobs_report": (jobs_report, {"check": list}),
           "jobs_track": (jobs_track, {"days": int, "recheck": bool})}


def _load_local():
    """Owner's local-only actions (e.g. ~/jarvis/local/cookidoo/jarvis_actions.py)."""
    for f in sorted(LOCAL.glob("*/jarvis_actions.py")) if LOCAL.exists() else []:
        spec = importlib.util.spec_from_file_location(f"jarvis_local_{f.parent.name}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ACTIONS.update(getattr(mod, "ACTIONS", {}))


def validate(action: str, params: dict) -> dict:
    """Only known actions, only their parameters, plain values of the declared type, short strings."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action '{action}'")
    schema = ACTIONS[action][1]
    if set(params) - set(schema):
        raise ValueError(f"unexpected parameters: {sorted(set(params) - set(schema))}")
    out = {}
    for k, v in params.items():
        t = schema[k]
        if t is list:
            if not isinstance(v, list) or len(v) > 20 or not all(isinstance(x, str) and len(x) <= 40 for x in v):
                raise ValueError(f"{k}: a list of up to 20 short strings")
        elif t is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k}: true/false")
        elif t is int:
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1000:
                raise ValueError(f"{k}: a number 0-1000")
        elif not isinstance(v, str) or len(v) > 120 or re.search(r"[\x00-\x1f]", v):
            raise ValueError(f"{k}: a short single-line text")
        out[k] = v
    return out


# --- queue ---------------------------------------------------------------------------------

def submit(action: str, params: dict) -> str:
    """Queue a request (any session may do this; it carries no private data and runs nothing itself)."""
    _load_local()
    validate(action, params)
    rid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    _write(INBOX / f"{rid}.json", {"id": rid, "action": action, "params": params,
                                   "queued": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return rid


def status(rid: str) -> str:
    f = DONE / f"{rid}.json"
    return json.loads(f.read_text())["status"] if f.exists() else "queued"


def run():
    """Carry out queued requests (launchd). Never inside an AI session."""
    if consent.in_ai_session():
        sys.exit("jarvis.relay run is for launchd only; AI sessions submit requests instead.")
    _load_local()
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(HOME / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for f in sorted(INBOX.glob("*.json")):
            try:
                req = json.loads(f.read_text())
                fn = ACTIONS[req["action"]][0]
                result = fn(validate(req["action"], req.get("params", {})))
            except Exception as e:  # noqa: BLE001 - the status never carries private content, only the error kind
                result = f"error: {type(e).__name__}"
                try:
                    from .reminders import send
                    send(f"⚠️ Jarvis couldn't do a request ({f.stem}): {type(e).__name__}")
                except Exception:  # noqa: BLE001
                    pass
            _write(DONE / f.name, {"status": result, "done": datetime.now().isoformat(timespec="seconds")})
            f.unlink(missing_ok=True)
            print(f"{datetime.now():%F %H:%M} {f.stem}: {result}")


def main():
    ap = argparse.ArgumentParser(prog="jarvis.relay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("action")
    s.add_argument("--json", default="{}", help='parameters as JSON, e.g. {"start": "2026-11-21"}')
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--wait", type=int, default=0, help="seconds to wait for the status")
    sub.add_parser("run")
    a = ap.parse_args()
    if a.cmd == "run":
        return run()
    params = json.loads(a.json)
    params.update({k: v for k, v in (("start", a.start), ("end", a.end or a.start)) if v})
    rid = submit(a.action, params)
    st = "queued"
    for _ in range(a.wait // 5):
        time.sleep(5)
        if (st := status(rid)) != "queued":
            break
    print(f"{rid}: {st}")


if __name__ == "__main__":
    main()
