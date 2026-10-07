"""Gmail + Google Calendar reader for the agenda: read-only, incremental, processed locally.

- OAuth "installed app" flow (loopback + PKCE), standard library only. Client file ~/.jarvis/google_client.json and
  token ~/.jarvis/google_token.json, both owner-only and never in git.
- Requires the owner's consent scopes "email" / "calendar" (python3 -m jarvis consent grant ...).
- Gmail: only messages newer than the last check, Primary/Updates (no Promotions/Social). First the subject + sender;
  the body is read only for messages that look agenda-related, and only by the LOCAL model (agenda.extract).
- Calendar: the next 14 days of events; only flights, tickets and Airbnb stays are kept (agenda.classify, local model).
- If the login expired, the owner gets one Telegram message with what to run.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import agenda, consent

HOME = Path.home() / ".jarvis"
CLIENT = HOME / "google_client.json"
TOKEN = HOME / "google_token.json"
STATE = HOME / "google_state.json"  # last check time, processed message ids
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
# Cheap filter before the model: only flights, tickets and Airbnb stays matter for now (agenda.ALLOWED_KINDS).
AGENDA_HINT = re.compile(
    r"(flight|airline|itinerary|boarding|e-?ticket|ticket|reservation|booking|booked|confirm|airbnb|"
    r"check-in|trip|vuelo|boleto|reserva)", re.I)


def _write_private(path: Path, data: dict):
    HOME.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def _client() -> dict:
    d = json.loads(CLIENT.read_text())
    return d.get("installed") or d.get("web") or d


# --- OAuth -------------------------------------------------------------------------

def login():
    """Interactive, at the Mac: opens Google's approval page, stores the token privately."""
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Jarvis: access approved. You can close this tab.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})
    print("Opening Google's approval page (read-only Gmail + Calendar). If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"Login failed: {result.get('error', 'state mismatch')}")
    tok = _post(c["token_uri"], {"code": result["code"], "client_id": c["client_id"],
                                 "client_secret": c.get("client_secret", ""), "redirect_uri": redirect,
                                 "grant_type": "authorization_code", "code_verifier": verifier})
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _write_private(TOKEN, tok)
    print("Done: read-only access stored in ~/.jarvis/google_token.json (owner-only).")


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _access_token() -> str:
    tok = json.loads(TOKEN.read_text())
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    c = _client()
    new = _post(c["token_uri"], {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                 "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    tok.update(access_token=new["access_token"], expires_at=time.time() + new.get("expires_in", 3600) - 60)
    _write_private(TOKEN, tok)
    return tok["access_token"]


def _get(url: str, params: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# --- Gmail ---------------------------------------------------------------------------

def _header(msg: dict, name: str) -> str:
    return next((h["value"] for h in msg.get("payload", {}).get("headers", []) if h["name"].lower() == name.lower()), "")


def _html_text(html: str) -> str:
    """Readable text from an HTML email: drop head/style/script, turn tags into breaks, decode entities."""
    import html as htmllib
    html = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|li|h\d)>", "\n", html)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def _body(payload: dict) -> str:
    """Email text for the model: the plain-text part, or the HTML part when the plain one is just a stub (many
    confirmation emails, e.g. Ticketmaster, put everything in the HTML)."""
    def walk(p, mime):
        if p.get("mimeType") == mime and p.get("body", {}).get("data"):
            yield base64.urlsafe_b64decode(p["body"]["data"]).decode(errors="replace")
        for part in p.get("parts", []) or []:
            yield from walk(part, mime)
    plain = "\n".join(walk(payload, "text/plain"))
    html = _html_text("\n".join(walk(payload, "text/html")))
    text = html if len(plain.strip()) < 400 and len(html.strip()) > len(plain.strip()) else plain
    text = re.sub(r"[ \t\u00a0\u200c\u034f]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()[:8000]


def _process(mid: str, stats: dict, upcoming_only: bool = False, not_before: str = ""):
    """Subject/sender check; agenda-like messages are read by the LOCAL model only."""
    stats["new"] += 1
    meta = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
    sender, subject, snippet = _header(meta, "From"), _header(meta, "Subject"), meta.get("snippet", "")
    if not AGENDA_HINT.search(f"{subject} {snippet}") or agenda.looks_promotional(sender, subject, snippet):
        return
    received = datetime.fromtimestamp(int(meta["internalDate"]) / 1000).date()
    full = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}", {"format": "full"})
    stats["opened"] += 1
    acts = agenda.extract(sender, subject, _body(full.get("payload", {})), received)
    if upcoming_only:  # old mail: keep only what's still ahead (bookings made months in advance)
        today = max(date.today().isoformat(), not_before)
        acts = [a for a in acts if max(a["date"], a.get("end_date") or "") >= today or a["status"] in ("cancelled", "rescheduled")]
    stats["activities"] += agenda.upsert(acts, source=f"email: {subject[:80]}")


TICKETMASTER_Q = 'from:ticketmaster.com subject:(order OR tickets OR confirmed OR confirmation OR "you got") -in:chats'


def _ticketmaster_ids(when: str) -> list[str]:
    ids, page = [], None
    while True:
        res = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                   {"q": f"{when} {TICKETMASTER_Q}", "maxResults": 100, **({"pageToken": page} if page else {})})
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page or len(ids) >= 300:
            return ids


def scan_ticketmaster_history(months: int = 6) -> dict:
    """One-time: Ticketmaster order confirmations from the last N months; only upcoming events are kept."""
    consent.require("email", "agenda: one-time read of Ticketmaster confirmations, read-only, processed locally")
    after = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp())
    stats = {"new": 0, "opened": 0, "activities": 0}
    try:
        for mid in reversed(_ticketmaster_ids(f"after:{after}")):
            _process(mid, stats, upcoming_only=True)
    finally:
        _unload_model()
    return stats


def scan_gmail(max_opened: int = 25, max_listed: int = 300) -> dict:
    """Every new message gets a cheap subject/sender check; at most `max_opened` agenda-like ones (oldest first) are
    read by the local model. Messages left over are picked up by the next scan."""
    consent.require("email", "agenda: read new emails, read-only, processed locally")
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    since = st.get("gmail_after") or int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    q = f"after:{since} -category:promotions -category:social -in:chats"
    ids, page = [], None
    while len(ids) < max_listed:
        params = {"q": q, "maxResults": 100, **({"pageToken": page} if page else {})}
        res = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages", params)
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    # Ticketmaster confirmations usually land in Promotions: fetched by sender + order-like subject instead.
    ids += [i for i in _ticketmaster_ids(f"after:{since}") if i not in ids]
    seen = set(st.get("seen", []))
    stats = {"new": 0, "opened": 0, "activities": 0}
    complete = True
    for mid in reversed(ids):  # oldest first
        if mid in seen:
            continue
        if stats["opened"] >= max_opened:
            complete = False  # the rest waits for the next scan
            break
        _process(mid, stats)
        seen.add(mid)
    if complete and not page:
        st["gmail_after"] = int(time.time()) - 3600  # 1 h overlap; ids dedupe
    st["seen"] = list(seen)[-2000:]
    _write_private(STATE, st)
    return stats


# --- One-time backfill ---------------------------------------------------------------

BACKFILL = HOME / "google_backfill.json"  # resumable progress: pending ids, counts


def _available_gb() -> float:
    import subprocess
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    pages = sum(int(l.split()[-1].rstrip(".")) for l in out.splitlines()
                if l.startswith(("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")))
    return pages * 16384 / 1e9


def _wait_until_free():
    """Never compete with development or squeeze memory: unload the model and wait while busy or low."""
    waited = False
    while _incubator_busy() or _available_gb() < 8:
        if not waited:
            _unload_model()
            waited = True
        time.sleep(300)


def backfill(months: int = 6):
    """Scan the last N months once (older than the daily scan's start), oldest first, resumable.
    Only upcoming activities are kept. One Telegram message at the end."""
    from .reminders import send
    consent.require("email", "agenda: one-time read of the last months of email, read-only, processed locally")
    bf = json.loads(BACKFILL.read_text()) if BACKFILL.exists() else {}
    if bf.get("done"):
        return bf
    if "pending" not in bf:
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
        before = st.get("gmail_after") or int(time.time())
        after = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp())
        q = f"after:{after} before:{before} -category:promotions -category:social -in:chats"
        ids, page = [], None
        while True:
            res = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                       {"q": q, "maxResults": 500, **({"pageToken": page} if page else {})})
            ids += [m["id"] for m in res.get("messages", [])]
            page = res.get("nextPageToken")
            if not page:
                break
        ids += [i for i in _ticketmaster_ids(f"after:{after} before:{before}") if i not in ids]
        seen = set(st.get("seen", []))
        bf = {"pending": [i for i in reversed(ids) if i not in seen], "total": len(ids),
              "stats": {"new": 0, "opened": 0, "activities": 0}, "started": datetime.now().isoformat(timespec="minutes"),
              "not_before": bf.get("not_before", "")}
        _write_private(BACKFILL, bf)
    try:
        while bf["pending"]:
            _wait_until_free()
            _process(bf["pending"][0], bf["stats"], upcoming_only=True, not_before=bf.get("not_before", ""))
            bf["pending"].pop(0)
            if bf["stats"]["new"] % 20 == 0:
                _write_private(BACKFILL, bf)
    finally:
        _write_private(BACKFILL, bf)
        _unload_model()
    bf["done"] = datetime.now().isoformat(timespec="minutes")
    _write_private(BACKFILL, bf)
    s = bf["stats"]
    send(f"📬 Jarvis: 6-month email scan finished. {s['new']} emails checked, {s['opened']} read by Qwen on the Mac, "
         f"{s['activities']} new upcoming activities added.\n\nAll upcoming:\n"
         + ("\n".join(agenda.fmt(i) for i in agenda.between(date.today(), date.today() + timedelta(730))) or "nothing"))
    return bf


# --- Calendar ------------------------------------------------------------------------

def scan_calendar(days: int = 14) -> int:
    consent.require("calendar", "agenda: read upcoming events, read-only")
    now = datetime.now(timezone.utc)
    items = _get("https://www.googleapis.com/calendar/v3/calendars/primary/events", {
        "timeMin": now.isoformat(), "timeMax": (now + timedelta(days=days)).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}).get("items", [])
    acts = []
    for ev in items:
        start = ev.get("start", {})
        when = start.get("dateTime") or start.get("date")
        if not when or ev.get("status") == "cancelled":
            continue
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        local = dt.astimezone() if "T" in when else dt
        acts.append({"title": (ev.get("summary") or "(no title)")[:60], "date": local.date().isoformat(),
                     "time": local.strftime("%H:%M") if "T" in when else "", "end_date": "",
                     "location": (ev.get("location") or "")[:80], "kind": "event", "status": "confirmed"})
    acts = [{**a, "kind": k} for a in acts if (k := agenda.classify({**a, "source": "calendar"}))]
    return agenda.upsert(acts, source="calendar")


INCUBATOR_DB = Path.home() / "game-incubator" / "state" / "incubator.db"


def _incubator_busy() -> bool:
    """True while an incubator job is running: the agenda scan never competes with development work."""
    if not INCUBATOR_DB.exists():
        return False
    import sqlite3
    try:
        with sqlite3.connect(f"file:{INCUBATOR_DB}?mode=ro", uri=True, timeout=5) as c:
            return c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def _unload_model():
    """Free the memory right away so the incubator's model loads fast."""
    try:
        from . import llm
        llm._http("/api/generate", {"model": agenda.MODEL, "keep_alive": 0}, timeout=30)
    except Exception:  # noqa: BLE001 - best effort
        pass


def run():
    """Daily 03:00 scan. Yields to the incubator (waits up to 45 min, else skips the day) and unloads its model when
    done. On an expired login: one Telegram note, then stop until the owner logs in again."""
    from .reminders import send
    if not TOKEN.exists():
        print("Not logged in: run `python3 -m jarvis google-login` at the Mac.")
        return
    for _ in range(9):  # 9 x 5 min = 45 min
        if not _incubator_busy():
            break
        time.sleep(300)
    else:
        print(f"{datetime.now():%F %H:%M} skipped: the incubator was busy for 45 min")
        return
    try:
        backfilling = BACKFILL.exists() and not json.loads(BACKFILL.read_text()).get("done")
        g = scan_gmail() if consent.granted("email") and not backfilling else None
        c = scan_calendar() if consent.granted("calendar") else None
        print(f"{datetime.now():%F %H:%M} gmail={g} calendar_new={c}")
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            flag = HOME / "google_relogin_notified"
            if not flag.exists():
                send("🔑 Jarvis lost access to Gmail/Calendar (Google login expired). At the Mac run: "
                     "python3 -m jarvis google-login")
                flag.write_text(date.today().isoformat())
            return
        raise
    finally:
        _unload_model()
    (HOME / "google_relogin_notified").unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["backfill"]:
        backfill()
        # one-off job: remove its launchd agent when finished
        agent = Path.home() / "Library/LaunchAgents/com.jarvis.backfill.plist"
        if agent.exists():
            agent.unlink()
    else:
        run()
