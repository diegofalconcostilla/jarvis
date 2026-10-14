"""Job applications: find open positions, tailor a resume + cover letter for each, track applications from Gmail.

Privacy model (same as the agenda):
- Everything personal lives in ~/.jarvis/jobs/ (owner-only, never in git): config.json (companies, roles, locations),
  resume/ (the master resume), jobs.json (postings, scores, applications), applications/ (generated documents).
- Needs the owner's consent scope "jobs" (resume + store); Gmail tracking also needs "email". Grant at the Mac:
  python3 -m jarvis consent grant jobs
- Postings come from the companies' PUBLIC job boards (Greenhouse, Lever, Ashby, SmartRecruiters JSON APIs; no login,
  no cost). Fit scores, resumes, cover letters and email reading all run on LOCAL models (Ollama). Nothing personal is
  sent to Claude or any cloud model; Telegram gets short summaries (and the documents only if send_documents is on).
- Nothing is invented: the generated resume only reorders/rephrases what's in the master resume, and code drops any
  employer, skill or number that isn't in it. Jarvis never applies for you; you review and submit.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import consent, llm

HOME = Path.home() / ".jarvis" / "jobs"
CONFIG = HOME / "config.json"
RESUME_DIR = HOME / "resume"
STORE = HOME / "jobs.json"
OUT = HOME / "applications"
SCORE_MODEL = "qwen2.5:7b"     # fit scores + email classification: small and fast
WRITE_MODEL = "qwen3:30b-a3b"  # resume + cover letter: better writing (MoE, ~18 GB while loaded)
WRITE_MIN_FREE_GB = 20

DEFAULT_CONFIG = {
    "companies": [
        {"name": "Example Co", "board": "greenhouse", "slug": "exampleco"},
    ],
    "roles": ["software engineer"],          # a posting matches when its title contains all words of one role
    "exclude": ["intern", "internship"],     # titles with any of these are skipped
    "locations": ["Seattle"],                # names from LOCATIONS below (or any text); empty list = anywhere
    "search_terms": ["software engineer"],   # queries for search-API boards (amazon, microsoft)
    "experience": [2, 5],                    # your years of experience: skip postings asking for more/less
    "require_sponsorship": True,             # only postings with evidence the employer sponsors work visas
    "no_visa_needed": [],                    # countries (US, UK, CA, MX) where you need no sponsorship
    "min_fit": 4,                            # 0-10 (deep) checklist fit needed to prepare documents
    "quick_fit_min": 2,                      # quick score needed for the deep re-score
    "deep_scores_per_day": 60,               # larger-model re-scores per day (~40-60 s each)
    "docs_per_week": 40,                     # Mondays: documents for the week's best-ranked matches
    "stop_by": "07:45",                      # large-model work stops here (the incubator starts at 08:00);
                                             # what's left continues the next morning
    "search_daily": True,
    "send_documents": False,                 # also send the .docx files to Telegram (your own bot)
    "add_posting_skills": True,              # owner's permission: list the posting's skills/technologies too
    "max_added_skills": 6,                   # ... at most this many per resume, AI-related first
}

# Location name -> (country, lowercase patterns found in postings' location text)
LOCATIONS = {
    "Seattle": ("US", ["seattle", "bellevue", "redmond", "kirkland"]),
    "California": ("US", ["california", "san francisco", "bay area", "palo alto", "mountain view", "sunnyvale",
                          "san jose", "santa clara", "menlo park", "redwood city", "san mateo", "cupertino",
                          "los angeles", "san diego", "irvine", "oakland", "berkeley", "foster city", "santa monica",
                          "south san francisco", "burlingame", ", ca,", ", ca ", ", ca)", ", ca/"]),
    "London": ("UK", ["london"]),
    "Vancouver": ("CA", ["vancouver"]),
    "Toronto": ("CA", ["toronto"]),
    "Mexico City": ("MX", ["mexico city", "ciudad de mexico", "ciudad de méxico", "cdmx", "méxico, d", "mexico, d"]),
    "Monterrey": ("MX", ["monterrey", "nuevo leon", "nuevo león", "san pedro garza"]),
}

STATUSES = ["applied", "in_review", "assessment", "interview", "offer", "rejected", "withdrawn"]


# --- storage -------------------------------------------------------------------------

def _write_private(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(path)


def init():
    """Create the private folders and a config template (doesn't overwrite an existing config)."""
    for d in (HOME, RESUME_DIR, OUT):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CONFIG.exists():
        _write_private(CONFIG, DEFAULT_CONFIG)
    print(f"config: {CONFIG}\nmaster resume goes in: {RESUME_DIR}/ (.docx, .pdf, .txt, .md or .rtf)")


def config() -> dict:
    return {**DEFAULT_CONFIG, **(json.loads(CONFIG.read_text()) if CONFIG.exists() else {})}


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"postings": {}, "applications": {}}


# --- public job boards -----------------------------------------------------------------

def _json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jarvis-jobs)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _text(html: str) -> str:
    html = htmllib.unescape(html or "")  # Greenhouse escapes its HTML once
    html = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", html)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def fetch(company: dict, conf: dict | None = None) -> list[dict]:
    """Open postings of one company, normalized: key, company, title, location, url, posted, text (empty = fetched
    later by details(), only for postings that match)."""
    board, slug, name = company["board"].lower(), company["slug"], company["name"]
    terms = company.get("search_terms") or (conf or config())["search_terms"]
    out = []
    if board == "greenhouse":
        for j in _json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").get("jobs", []):
            out.append({"id": j["id"], "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
                        "url": j["absolute_url"], "posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
                        "text": _text(j.get("content", ""))})
    elif board == "lever":
        for j in _json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
            cat = j.get("categories") or {}
            lists = "\n".join(f"{l.get('text', '')}\n{_text(l.get('content', ''))}" for l in j.get("lists", []))
            out.append({"id": j["id"], "title": j["text"],
                        "location": " / ".join(filter(None, [cat.get("location"), j.get("workplaceType")])),
                        "url": j["hostedUrl"],
                        "posted": datetime.fromtimestamp(j.get("createdAt", 0) / 1000).date().isoformat(),
                        "text": f"{j.get('descriptionPlain', '')}\n{lists}\n{j.get('additionalPlain', '')}"})
    elif board == "ashby":
        for j in _json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true").get("jobs", []):
            if not j.get("isListed", True):
                continue
            locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
            out.append({"id": j["id"], "title": j["title"],
                        "location": " / ".join(filter(None, locs + (["Remote"] if j.get("isRemote") else []))),
                        "url": j["jobUrl"], "posted": (j.get("publishedAt") or "")[:10],
                        "text": j.get("descriptionPlain") or _text(j.get("descriptionHtml", ""))})
    elif board == "smartrecruiters":
        res = _json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
        for j in res.get("content", []):
            loc = j.get("location") or {}
            out.append({"id": j["id"], "title": j["name"],
                        "location": " / ".join(filter(None, [loc.get("city"), loc.get("region"),
                                                             "Remote" if loc.get("remote") else ""])),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                        "posted": (j.get("releasedDate") or "")[:10], "text": ""})  # body fetched only if matched
    elif board == "amazon":  # amazon.jobs public search; newest first, filtered locally
        seen = set()
        for t in terms:
            for off in range(0, 1000, 100):
                q = urllib.parse.urlencode({"base_query": t, "result_limit": 100, "offset": off, "sort": "recent"})
                res = _json(f"https://www.amazon.jobs/en/search.json?{q}").get("jobs", [])
                for j in res:
                    if j["id_icims"] in seen:
                        continue
                    seen.add(j["id_icims"])
                    out.append({"id": j["id_icims"], "title": j["title"],
                                "location": f"{j.get('normalized_location', '')} / {j.get('location', '')}",
                                "url": "https://www.amazon.jobs" + j["job_path"], "posted": j.get("posted_date", ""),
                                "text": _text("\n".join(j.get(k, "") or "" for k in
                                                         ("description", "basic_qualifications", "preferred_qualifications")))})
                if len(res) < 100:
                    break
    elif board == "microsoft":  # apply.careers.microsoft.com public search (10 per page); details fetched if matched
        seen = set()
        for t in terms:
            for loc in company.get("search_locations", [""]):
                for start in range(0, 300, 10):
                    q = urllib.parse.urlencode({"domain": "microsoft.com", "query": t, "location": loc,
                                                "start": start, "num": 10})
                    res = (_json(f"https://apply.careers.microsoft.com/api/pcsx/search?{q}").get("data") or {}).get("positions", [])
                    for j in res:
                        if j["id"] in seen:
                            continue
                        seen.add(j["id"])
                        out.append({"id": j["id"], "title": j["name"],
                                    "location": " / ".join(j.get("standardizedLocations") or j.get("locations") or []),
                                    "url": "https://apply.careers.microsoft.com" + j.get("positionUrl", ""),
                                    "posted": datetime.fromtimestamp(j.get("postedTs", 0)).date().isoformat(), "text": ""})
                    if len(res) < 10:
                        break
    else:
        raise ValueError(f"{name}: unknown board '{board}' (greenhouse, lever, ashby, smartrecruiters, amazon, microsoft)")
    for j in out:
        j["company"], j["board"], j["slug"] = name, board, slug
        j["key"] = hashlib.sha1(f"{board}:{slug}:{j['id']}".encode()).hexdigest()[:8]
    return out


def details(j: dict) -> str:
    """Posting text for boards whose list doesn't include it."""
    if j["text"]:
        return j["text"]
    if j["board"] == "smartrecruiters":
        d = _json(f"https://api.smartrecruiters.com/v1/companies/{j['slug']}/postings/{j['id']}")
        sections = (d.get("jobAd") or {}).get("sections") or {}
        j["text"] = "\n".join(_text(s.get("text", "")) for s in sections.values() if isinstance(s, dict))
    elif j["board"] == "microsoft":
        d = _json(f"https://apply.careers.microsoft.com/api/pcsx/position_details?position_id={j['id']}"
                  "&domain=microsoft.com").get("data") or {}
        j["text"] = _text(d.get("jobDescription", ""))
    return j["text"]


def where(job: dict, conf: dict) -> list[str]:
    """The configured locations a posting is in (by its location text)."""
    loc = f" {job['location'].lower()} "
    return [name for name in conf["locations"]
            if any(p in loc for p in LOCATIONS.get(name, ("", [name.lower()]))[1])]


def matches(job: dict, conf: dict) -> bool:
    words = lambda s: re.findall(r"[a-z0-9+#]+", s.lower())  # noqa: E731
    tw = set(words(job["title"]))
    title = f" {' '.join(words(job['title']))} "
    if any(f" {' '.join(words(x))} " in title for x in conf["exclude"] if words(x)):  # exclude = whole phrases
        return False
    if not any(set(words(r)) <= tw for r in conf["roles"] if words(r)):
        return False
    return not conf["locations"] or bool(where(job, conf))


# --- experience level ------------------------------------------------------------------

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "twelve": 12, "fifteen": 15}
YEARS = re.compile(r"(\d{1,2}|" + "|".join(NUM_WORDS) + r")\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}|"
                   + "|".join(NUM_WORDS) + r")\s*)?\+?\s*(?:\(\w+\)\s*)?years?", re.I)


def years_required(text: str) -> tuple[int, int] | None:
    """(min, max) years of experience a posting asks for, from phrases like '3+ years ... experience' or '2-5 years
    of experience'. The most permissive minimum wins ('5+ years, or 3+ with a PhD' -> 3). None if not stated."""
    lows, highs = [], []
    for m in YEARS.finditer(text):
        context = text[m.end():m.end() + 80].lower()
        if "experience" not in context and "industry" not in context and "professional" not in context:
            continue
        lo = int(NUM_WORDS.get(m.group(1).lower(), m.group(1)))
        hi = m.group(2) and int(NUM_WORDS.get(m.group(2).lower(), m.group(2)))
        if lo > 20:
            continue
        lows.append(lo)
        if hi:
            highs.append(hi)
    if not lows:
        return None
    return min(lows), max(highs) if highs else 99


SENIOR_TITLES = re.compile(r"\b(staff|principal|distinguished|director|head of|vp|vice president|architect|"
                           r"fellow|lead|manager|senior manager|sr\.? manager)\b", re.I)
JUNIOR_TITLES = re.compile(r"\b(new grad|new graduate|graduate|university|entry[- ]level|early career|apprentice|"
                           r"junior)\b", re.I)


def level_ok(job: dict, conf: dict) -> tuple[bool, str]:
    """Fits someone with conf['experience'] years: title level first, then the years the posting asks for."""
    lo, hi = conf.get("experience") or [0, 99]
    if SENIOR_TITLES.search(job["title"]):
        return False, "title above your level"
    if lo >= 2 and JUNIOR_TITLES.search(job["title"]):
        return False, "entry-level title"
    req = years_required(job["text"])
    if req and req[0] > hi:
        return False, f"asks for {req[0]}+ years"
    if req and req[1] < lo:
        return False, f"asks for at most {req[1]} years"
    return True, f"{req[0]}+ yrs" if req else "years not stated"


# --- work-visa sponsorship -------------------------------------------------------------
# Evidence, strongest first: what the posting says; then public data on the employer:
#   US: H-1B Labor Condition Applications filed this/last year (DOL disclosure data, searched through h1bdata.info,
#       which allows automated access) + USCIS H-1B Employer Data Hub approvals (FY2023, partial);
#   UK: Home Office register of licensed sponsors (Skilled Worker route);
#   Canada: employers issued a positive LMIA in the last 4 quarters (incl. Global Talent Stream; open.canada.ca).
# Mexico: only the posting's words (or no_visa_needed).

SPONSORS = HOME / "sponsors"
H1B_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-2023.csv"
UK_PAGE = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
LMIA_API = "https://open.canada.ca/data/api/action/package_show?id=90fed587-1364-4f33-a9ee-208181dc0b97"
NO_SPONSOR = re.compile(
    r"(not (be )?(able|in a position) to (offer |provide )?(visa |immigration )?sponsor|unable to (offer |provide )?"
    r"(visa |immigration )?sponsor|(do|does|will|can|are) not (currently )?(offer |provide )?(visa |immigration )?"
    r"sponsor|cannot (offer |provide )?(visa )?sponsor|can't sponsor|no (visa |immigration )?sponsorship|without "
    r"(the need for )?(current or future |any )?(visa |immigration |employer )?sponsorship|sponsorship (is |will )?not "
    r"(be )?(available|offered|provided|considered)|not eligible for (visa )?sponsorship|sponsorship is unavailable)", re.I)
YES_SPONSOR = re.compile(
    r"(we (do |can |will )?sponsor (work )?visas|visa sponsorship (is |may be )?(available|offered|provided|"
    r"possible)|(offer|provide)s? (visa|immigration) (sponsorship|support)|sponsorship (is )?available|"
    r"(able|happy|willing) to sponsor|relocation and (visa|immigration)|(visa|immigration) support|"
    r"open to sponsor)", re.I)
_SUFFIX = re.compile(r"\b(inc|llc|l l c|ltd|limited|corp|corporation|co|company|pbc|plc|lp|llp|gmbh|uk|us|usa|"
                     r"d b a|dba)\b")


def _cname(s: str) -> str:
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", _norm(s))).strip()


def _download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jarvis-jobs)"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    m = re.search(rb'Redirecting.*?href="([^"]+)"', data[:2000], re.S)
    if m:  # open.canada.ca answers with an HTML redirect page to the file's storage URL
        return _download(htmllib.unescape(m.group(1).decode()), dest)
    dest.write_bytes(data)


def refresh_sponsor_data(max_age_days: int = 30):
    """Download the public sponsor lists when missing or older than a month."""
    SPONSORS.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = lambda p: not p.exists() or time.time() - p.stat().st_mtime > max_age_days * 86400  # noqa: E731
    if old(SPONSORS / "h1b.csv"):
        _download(H1B_URL, SPONSORS / "h1b.csv")
    if old(SPONSORS / "uk_register.csv"):
        req = urllib.request.Request(UK_PAGE, headers={"User-Agent": "Mozilla/5.0 (jarvis-jobs)"})
        page = urllib.request.urlopen(req, timeout=60).read().decode(errors="replace")
        m = re.search(r"https://assets\.publishing\.service\.gov\.uk/[^\"]+\.csv", page)
        if m:
            _download(m.group(0), SPONSORS / "uk_register.csv")
    if old(SPONSORS / "ca_lmia.json"):
        res = _json(LMIA_API)["result"]["resources"]
        files = sorted((r for r in res if r["url"].endswith("_pos_en.xlsx")), key=lambda r: r.get("created", ""))[-4:]
        names = set()
        for r in files:
            url = r["url"] if r["url"].startswith("http") else "https://open.canada.ca" + r["url"]
            _download(url, SPONSORS / "lmia.xlsx")
            names |= _xlsx_column(SPONSORS / "lmia.xlsx", "employer")
        (SPONSORS / "lmia.xlsx").unlink(missing_ok=True)
        _write_private(SPONSORS / "ca_lmia.json", sorted(names))


def _xlsx_column(path: Path, header: str) -> set:
    """Values of the first column whose header contains `header` (stdlib-only xlsx reading)."""
    import zipfile
    from xml.etree import ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(path)
    shared = [("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)] \
        if "xl/sharedStrings.xml" in z.namelist() else []
    col, out = None, set()
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{ns['m']}}}row"):
        vals = {}
        for c in row.findall("m:c", ns):
            v = c.find("m:v", ns)
            txt = c.find(".//m:t", ns)
            val = shared[int(v.text)] if c.get("t") == "s" and v is not None else (v.text if v is not None else (txt.text if txt is not None else ""))
            vals[re.sub(r"\d", "", c.get("r", ""))] = val or ""
        if col is None:
            col = next((k for k, v in vals.items() if v.strip().lower().startswith(header) and len(v) < 40), None)
        elif vals.get(col):
            out.add(vals[col].strip())
    return out


_H1B: dict | None = None
_UK: set | None = None
_CA: set | None = None


def _sponsor_lists() -> tuple[dict, set, set]:
    global _H1B, _UK, _CA
    if _H1B is None:
        import csv
        refresh_sponsor_data()
        _H1B, _UK = {}, set()
        _CA = {_cname(n) for n in json.loads((SPONSORS / "ca_lmia.json").read_text())}
        with open(SPONSORS / "h1b.csv", newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                n = _cname(r.get("Employer") or "")
                try:
                    _H1B[n] = _H1B.get(n, 0) + int(r["Initial Approval"] or 0) + int(r["Continuing Approval"] or 0)
                except (KeyError, ValueError):
                    pass
        with open(SPONSORS / "uk_register.csv", newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                if "skilled worker" in (r.get("Route") or "").lower():
                    for part in re.split(r"\bt/a\b", r.get("Organisation Name") or "", flags=re.I):
                        _UK.add(_cname(part))  # "Roofoods Ltd t/a Deliveroo": both names
    return _H1B, _UK, _CA


GENERIC = {"technologies", "technology", "labs", "lab", "holdings", "global", "opco", "group", "international",
           "ai", "hq", "america", "north", "canada", "mexico", "uk", "usa",
           "services", "software", "development", "payments", "web", "europe", "emea", "systems", "cloud", "data",
           "operations", "ulc", "corp", "incorporated"}


def _same_employer(n: str, w: str) -> bool:
    """'openai opco' is OpenAI; 'crusoe inns' is not Crusoe (only generic words may follow the name)."""
    return bool(w) and (n == w or (n.startswith(w + " ") and set(n[len(w):].split()) <= GENERIC))


def _employer_hits(company: dict, names) -> list[str]:
    wanted = [_cname(n) for n in company.get("legal_names") or [company["name"]]]
    return [n for n in names if any(_same_employer(n, w) for w in wanted)]


def lca_filings(company: dict) -> int:
    """H-1B LCAs the employer filed this year + last year (cached 30 days per company)."""
    cache_f = SPONSORS / "lca_cache.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    hit = cache.get(company["name"])
    if hit and time.time() - hit["at"] < 30 * 86400:
        return hit["n"]
    wanted = [_cname(n) for n in company.get("legal_names") or [company["name"]]]
    n = 0
    for name in company.get("legal_names") or [company["name"]]:
        for year in (date.today().year, date.today().year - 1):
            q = urllib.parse.urlencode({"em": name, "job": "", "city": "", "year": year})
            req = urllib.request.Request(f"https://h1bdata.info/index.php?{q}", headers={"User-Agent": "Mozilla/5.0"})
            page = urllib.request.urlopen(req, timeout=60).read().decode(errors="replace")
            employers = re.findall(r"<tr><td><a [^>]*>([^<]*)</a>", page)
            n += sum(any(_same_employer(_cname(htmllib.unescape(e)), w) for w in wanted) for e in employers)
            time.sleep(1)  # be polite
    cache[company["name"]] = {"n": n, "at": time.time()}
    _write_private(cache_f, cache)
    return n


def sponsorship(job: dict, company: dict, country: str) -> tuple[str, str]:
    """("yes" | "no" | "unknown", evidence). The posting's own words win over employer data."""
    text = details(job)
    if m := NO_SPONSOR.search(text):
        return "no", f"posting: “{m.group(0)}”"
    if m := YES_SPONSOR.search(text):
        return "yes", f"posting: “{m.group(0)}”"
    if country in company.get("sponsors_in", []):
        return "yes", company.get("sponsor_note", "company sponsors here (config)")
    h1b, uk, ca = _sponsor_lists()
    if country == "US":
        n = lca_filings(company)
        if n >= 3:
            return "yes", f"{n} H-1B filings in {date.today().year - 1}-{date.today().year} (DOL data)"
        n = sum(h1b[k] for k in _employer_hits(company, h1b))
        if n >= 3:
            return "yes", f"{n} H-1B approvals (USCIS FY2023)"
    if country == "UK" and _employer_hits(company, uk):
        return "yes", "UK licensed visa sponsor (Home Office register)"
    if country == "CA" and _employer_hits(company, ca):
        return "yes", "approved to hire foreign workers in Canada (positive LMIA, last 4 quarters)"
    return "unknown", ""


def visa_ok(job: dict, company: dict, conf: dict) -> tuple[bool, str]:
    """A posting is kept when ANY of its matching locations needs no visa or has sponsorship evidence."""
    status, evidence = "unknown", ""
    for place in where(job, conf) or ["?"]:
        country = LOCATIONS.get(place, ("?", []))[0]
        if not conf["require_sponsorship"] or country in conf["no_visa_needed"]:
            return True, f"no visa needed ({place})"
        status, evidence = sponsorship(job, company, country)
        if status == "yes":
            return True, f"{evidence} ({place})"
        if status == "no":
            return False, evidence
    return False, evidence or "no sponsorship evidence"


def check_boards(visa: bool = True) -> list[str]:
    """Verify every configured company's board/slug (public data only; no consent needed)."""
    conf, lines = config(), []
    for c in conf["companies"]:
        try:
            jobs = fetch(c, conf)
            hit = [j for j in jobs if matches(j, conf)]
            ok = sum(visa_ok(j, c, conf)[0] for j in hit) if visa else 0
            lines.append(f"OK   {c['name']}: {len(jobs)} open, {len(hit)} match roles/locations"
                         + (f", {ok} with visa sponsorship" if visa else ""))
        except Exception as e:  # noqa: BLE001
            lines.append(f"FAIL {c['name']} ({c.get('board')}/{c.get('slug')}): {str(e)[:80]}")
    return lines


# --- master resume ---------------------------------------------------------------------

def resume_text() -> str:
    consent.require("jobs", "read your master resume in ~/.jarvis/jobs/resume, processed locally")
    files = sorted((p for p in RESUME_DIR.glob("*") if p.suffix.lower() in (".docx", ".pdf", ".txt", ".md", ".rtf")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"put your master resume in {RESUME_DIR}/")
    p = files[0]
    if p.suffix.lower() in (".txt", ".md"):
        return p.read_text(errors="replace")
    if p.suffix.lower() == ".pdf":  # macOS PDFKit through JavaScript for Automation: nothing to install
        js = (f"ObjC.import('PDFKit'); $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath({json.dumps(str(p))}))"
              ".string.js")
        return subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True,
                              timeout=60).stdout
    return subprocess.run(["textutil", "-convert", "txt", "-stdout", str(p)], capture_output=True, text=True,
                          timeout=60).stdout


# --- local model helpers ---------------------------------------------------------------

def _ask_json(prompt: str, model: str, num_ctx: int = 12288, think: bool = False) -> dict:
    """JSON from a local model; reasoning models think first only when asked (slower, better)."""
    raw = llm.ollama(prompt, model, temperature=0.2, num_ctx=num_ctx, json_mode=True,
                     think=think if model.startswith(("qwen3", "gpt-oss")) else None)
    raw = re.sub(r"(?s)<think>.*?</think>", "", raw).strip()
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _unload(*models):
    for m in models:
        try:
            llm._http("/api/generate", {"model": m, "keep_alive": 0}, timeout=30)
        except Exception:  # noqa: BLE001 - best effort
            pass


FIT_PROMPT = """You screen a candidate for ONE job. Be strict: count only what the RESUME actually shows.
1. List the job's 6 most important REQUIRED qualifications (not nice-to-haves).
2. For each, answer "yes" (the resume clearly shows it), "partial" (related experience) or "no".
3. Is the job's main domain/stack the same as the candidate's (e.g. frontend vs backend vs infra vs ML research)?
Answer with ONLY this JSON:
{{"requirements": [{{"req": "", "met": "yes|partial|no"}}], "same_domain": "yes|partial|no",
 "why": "one short sentence"}}

RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""


def score(job: dict, resume: str, deep: bool = False) -> dict:
    """0-10 from a requirement checklist (yes = 1, partial = 0.5, weighted by domain match): the local model judges
    each requirement, the code does the math, so scores are comparable across postings. deep = the larger model,
    thinking first (slow, more accurate): used to re-check the promising ones."""
    d = _ask_json(FIT_PROMPT.format(resume=resume[:8000], title=job["title"], company=job["company"],
                                    location=job["location"], posting=job["text"][:7000]),
                  WRITE_MODEL if deep else SCORE_MODEL, num_ctx=16384 if deep else 12288, think=deep)
    reqs = [r for r in d.get("requirements", []) if isinstance(r, dict) and r.get("req")][:6]
    met = lambda r: {"yes": 1, "partial": 0.5}.get(str(r.get("met", "")).lower(), 0)  # noqa: E731
    domain = {"yes": 1, "partial": 0.6}.get(str(d.get("same_domain", "")).lower(), 0.2)
    fit = round(10 * sum(map(met, reqs)) / len(reqs) * (0.5 + 0.5 * domain)) if reqs else 0
    return {"fit": fit, "why": str(d.get("why", ""))[:200], "scored_by": "deep" if deep else "quick",
            "missing": [str(r["req"])[:80] for r in reqs if met(r) == 0][:3]}


# --- tailored documents ------------------------------------------------------------------

RESUME_PROMPT = """Tailor a resume to one job. RULES:
- Experience, employers, titles, dates, degrees, numbers, metrics and achievements come ONLY from the MASTER RESUME.
  Never invent or inflate them, and never put a job requirement into an experience bullet the resume doesn't support.
- SKILLS: list the master resume's relevant skills{posting_skills_rule}
- You may: choose and reorder the most relevant experience and bullets, rephrase bullets to mirror the job's wording
  (same facts), and write a 2-3 sentence summary from the master resume{summary_rule}.
- Keep company names, titles and dates exactly as written in the master resume.
- It MUST fit on ONE page: about 450 words at most; 3-5 bullets for the most relevant role, 2-3 for others;
  at most 16 skills; summary 2-3 short sentences.
Answer with ONLY this JSON:
{{"name": "", "contact": ["email/phone/city/links exactly as in the master resume"], "summary": "",
 "skills": ["most relevant first"],
 "experience": [{{"company": "", "title": "", "dates": "", "location": "", "bullets": [""]}}],
 "education": [{{"school": "", "degree": "", "dates": ""}}],
 "other": [{{"heading": "e.g. Certifications / Projects", "items": [""]}}]}}

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""

LETTER_PROMPT = """Write a cover letter for this job. RULES:
- Experience, achievements, employers, numbers and years of experience come ONLY from the MASTER RESUME. Never
  invent projects or results. Don't claim to know people at the company.{letter_rule}
- 3-4 short paragraphs, under 300 words, confident and specific, no clichés. Mention the role and company.
- No address block, no date, no signature line (they are added separately).
Answer with ONLY this JSON: {{"greeting": "Dear Hiring Team,", "paragraphs": [""], "closing": "Sincerely,"}}

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""

REVIEW_RESUME = """You are a senior tech recruiter reviewing a tailored resume DRAFT for ONE job. Improve it, then
return the full improved resume in the same JSON shape as the draft.
Check and fix:
- Relevance: the most job-relevant experience and bullets first; drop weak or off-topic bullets.
- Keywords: the job's key skills/technologies appear naturally (ATS), named as in the posting{posting_skills_rule}
- Bullets: start with a strong verb, show scope and impact, one line or two, no filler, no first person.
- Summary: 2-3 sentences aimed at THIS role, specific, no clichés.
- Truth: experience, employers, titles, dates, numbers and achievements must match the MASTER RESUME exactly;
  remove anything the master resume doesn't support. Never move a job requirement into an experience bullet.
- Grammar, consistency (tense, punctuation), no duplicates.
- Length: it MUST fit on ONE page (about 450 words at most, at most 16 skills); cut the least relevant first.

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}

DRAFT (JSON):
{draft}"""

REVIEW_LETTER = """You are a senior tech recruiter reviewing a cover letter DRAFT for ONE job. Improve it and return
the same JSON shape: {{"greeting": "", "paragraphs": [""], "closing": ""}}
Check and fix:
- Opening: names the role and company and a specific reason this candidate fits (from the master resume).
- Middle: 1-2 concrete examples from the MASTER RESUME mapped to the job's top needs; why this company.
- Tone: confident, specific, warm; no clichés ("I am writing to express"), no repetition, under 300 words.
- Truth: experience, results, numbers and years only from the MASTER RESUME; skills the job asks for may be named
  as skills, never with invented projects or results.{letter_rule}

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}

DRAFT (JSON):
{draft}"""

NUMBER = re.compile(r"\d[\d,.]*\+?%?")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _supported(text: str, resume: str) -> bool:
    """True when every number in `text` appears in the master resume (the cheapest invention check)."""
    rnums = {n.rstrip(".,+%") for n in NUMBER.findall(resume)}
    return all(n.rstrip(".,+%") in rnums for n in NUMBER.findall(text))


def _in_resume(s: str, resume_n: str) -> bool:
    return bool(_norm(s)) and _norm(s) in resume_n


POSTING_SKILLS_RULE = (", PLUS the skills, tools and technologies the JOB posting asks for (especially AI: LLMs, "
                       "agents, RAG, prompt engineering, model APIs, ML frameworks), named exactly as in the posting.")
SUMMARY_RULE = "; it may name skills from the job posting (as skills, never as past results)"
LETTER_RULE = ("\n- You may say the candidate is skilled in / works with technologies the JOB asks for, without "
               "inventing where or how they were used.")


def verify_resume(d: dict, resume: str, posting: str = "") -> tuple[dict, list[str], list[str]]:
    """Drop anything the master resume doesn't support; skills may also come from the posting (owner's choice,
    add_posting_skills) and are reported separately. Returns (clean resume, dropped, skills added from the posting)."""
    rn, pn, dropped = _norm(resume), _norm(posting), []
    clean = {"name": d.get("name", "") if _in_resume(d.get("name", ""), rn) else "",
             "contact": [c for c in d.get("contact", []) if isinstance(c, str) and _in_resume(c, rn)]}
    summ = [s for s in re.split(r"(?<=[.!?])\s+", str(d.get("summary", ""))) if s]
    clean["summary"] = " ".join(s for s in summ if _supported(s, resume))
    dropped += [f"summary: {s}" for s in summ if not _supported(s, resume)]
    skills = list(dict.fromkeys(s.strip() for s in d.get("skills", []) if isinstance(s, str) and s.strip()))
    clean["skills"] = [s for s in skills if _in_resume(s, rn) or (pn and _in_resume(s, pn))]
    added = [s for s in clean["skills"] if not _in_resume(s, rn)]
    # owner's permission covers a few relevant skills, not a copy of the posting: AI ones first, capped
    ai = re.compile(r"\b(ai|llm|rag|agent|prompt|genai|generative|ml|machine learning|embedding|vector|"
                    r"fine-?tun|inference|pytorch|tensorflow|langchain|openai|anthropic|claude|gpt|transformer)", re.I)
    cap = config().get("max_added_skills", 6)
    keep = sorted(added, key=lambda s: not ai.search(s))[:cap]
    dropped += [f"skill (over the {cap}-skill cap): {s}" for s in added if s not in keep]
    clean["skills"] = [s for s in clean["skills"] if _in_resume(s, rn) or s in keep]
    added = [s for s in added if s in keep]
    dropped += [f"skill: {s}" for s in skills if s not in clean["skills"]]
    clean["experience"] = []
    for e in d.get("experience", []):
        if not isinstance(e, dict) or not _in_resume(e.get("company", ""), rn):
            dropped.append(f"experience: {e.get('company') if isinstance(e, dict) else e}")
            continue
        bullets = [b for b in e.get("bullets", []) if isinstance(b, str) and b.strip()]
        # a skill taken from the posting may be listed, never claimed as past work
        ok = lambda b: _supported(b, resume) and not any(_in_resume(a, _norm(b)) for a in added)  # noqa: E731
        dropped += [f"bullet: {b}" for b in bullets if not ok(b)]
        clean["experience"].append({k: e.get(k, "") if _in_resume(e.get(k, ""), rn) else ""
                                    for k in ("company", "title", "dates", "location")}
                                   | {"bullets": [b for b in bullets if ok(b)]})
    clean["education"] = [{k: ed.get(k, "") for k in ("school", "degree", "dates")}
                          for ed in d.get("education", []) if isinstance(ed, dict) and _in_resume(ed.get("school", ""), rn)]
    clean["other"] = [{"heading": o.get("heading", ""),
                       "items": [i for i in o.get("items", []) if isinstance(i, str) and _supported(i, resume)]}
                      for o in d.get("other", []) if isinstance(o, dict) and o.get("items")]
    return clean, dropped, added


def _e(s) -> str:
    return htmllib.escape(str(s or ""))


# textutil (used for the applications report .docx) reads CSS px as Word points, so sizes are in px.
PAGE_CSS = ("body{font-family:Arial;font-size:11px}h1{font-size:17px;margin:0}"
            "h2{font-size:12px;border-bottom:1px solid #444;margin:10px 0 4px}p{margin:0 0 6px 0}p.c{color:#333}"
            "ul{margin:0 0 6px 0}")
RESUME_MARGIN_IN = 0.6
RENDER_JS = Path(__file__).with_name("assets") / "render.js"


def render(spec: dict, pdf: Path | None, docx: Path | None, margin_in: float = RESUME_MARGIN_IN) -> float:
    """Typeset with macOS AppKit (assets/render.js): PDF (the deliverable) and/or an editable DOCX.
    Returns how many Letter pages the text fills."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f)
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", str(RENDER_JS), f.name, str(pdf or "-"), str(docx or "-"),
                              str(margin_in)], capture_output=True, text=True, timeout=120)
    finally:
        Path(f.name).unlink(missing_ok=True)
    for x in (pdf, docx):
        if x and x.exists():
            x.chmod(0o600)
    try:
        return float(json.loads(out.stdout)["pages"])
    except (ValueError, KeyError):
        raise RuntimeError(f"render failed: {out.stderr.strip()[:200]}")


def _heading(t: str) -> dict:
    return {"text": t.upper(), "size": 10.5, "bold": True, "rule": True, "before": 7, "after": 3}


def _resume_blocks(r: dict) -> list[dict]:
    B = [{"text": r["name"] or " ", "size": 18, "bold": True, "after": 1},
         {"text": "  ·  ".join(r["contact"]) or " ", "size": 9.5, "gray": 0.3, "after": 3}]
    if r["summary"]:
        B += [_heading("Summary"), {"text": r["summary"], "size": 10.5, "after": 1}]
    if r["skills"]:
        B += [_heading("Skills"), {"text": ", ".join(r["skills"]), "size": 10.5, "after": 1}]
    if r["experience"]:
        B.append(_heading("Experience"))
        for e in r["experience"]:
            where = "  ·  ".join(x for x in (e["company"], e["location"]) if x)
            B.append({"runs": [{"text": e["title"] or e["company"], "bold": True}]
                      + ([{"text": f"  ·  {where}"}] if e["title"] else []),
                      "right": e["dates"], "ritalic": True, "size": 10.5, "before": 4, "after": 1})
            B += [{"text": f"•\t{b}", "indent": 11, "size": 10.5, "after": 1} for b in e["bullets"]]
    if r["education"]:
        B.append(_heading("Education"))
        B += [{"runs": [{"text": ed["degree"] or ed["school"], "bold": True}]
               + ([{"text": f"  ·  {ed['school']}"}] if ed["degree"] else []),
               "right": ed["dates"], "ritalic": True, "size": 10.5, "after": 1} for ed in r["education"]]
    for o in r["other"]:
        if o["items"]:
            B += [_heading(o["heading"] or "Other")] + [{"text": f"•\t{i}", "indent": 11, "size": 10.5, "after": 1}
                                                       for i in o["items"]]
    return B


def _letter_blocks(r: dict, greeting: str, paras: list[str], closing: str) -> list[dict]:
    return ([{"text": r["name"] or " ", "size": 16, "bold": True, "after": 1},
             {"text": "  ·  ".join(r["contact"]) or " ", "size": 9.5, "gray": 0.3, "after": 18},
             {"text": f"{date.today():%B %d, %Y}", "size": 11, "after": 12},
             {"text": greeting, "size": 11, "after": 8}]
            + [{"text": p, "size": 11, "after": 8, "line": 1.1} for p in paras]
            + [{"text": closing, "size": 11, "before": 4, "after": 2}, {"text": r["name"] or " ", "size": 11}])


def fit_one_page(res: dict, folder: Path) -> list[str]:
    """Measure the typeset resume; while it's longer than one page, cut the least important line: extra skills,
    then the last bullet of the role with the most bullets (older roles first on ties), then 'other' items, then
    the summary's last sentence. Writes resume.pdf + resume.docx. Returns what was cut."""
    cut = []
    for _ in range(40):
        if render({"blocks": _resume_blocks(res)}, None, None) <= 0.99:
            break
        if len(res["skills"]) > 16:
            cut.append(f"skill: {res['skills'].pop()}")
            continue
        roles = [e for e in res["experience"] if len(e["bullets"]) > 2] or [e for e in res["experience"] if len(e["bullets"]) > 1]
        if roles:
            role = max(reversed(roles), key=lambda e: len(e["bullets"]))
            cut.append(f"bullet ({role['company']}): {role['bullets'].pop()}")
            continue
        items = [o for o in res["other"] if o["items"]]
        if items:
            cut.append(f"{items[-1]['heading']}: {items[-1]['items'].pop()}")
            continue
        sents = re.split(r"(?<=[.!?])\s+", res["summary"])
        if len(sents) > 1:
            res["summary"] = " ".join(sents[:-1])
            cut.append(f"summary: {sents[-1]}")
            continue
        break
    render({"blocks": _resume_blocks(res)}, folder / "resume.pdf", folder / "resume.docx")
    return cut


def _page(body: str, css: str = PAGE_CSS) -> str:
    return f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{body}</body></html>"


def _docx(html: str, path: Path):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
    try:
        subprocess.run(["textutil", "-convert", "docx", "-output", str(path), f.name], check=True,
                       capture_output=True, timeout=60)
    finally:
        Path(f.name).unlink(missing_ok=True)
    path.chmod(0o600)


def _wait_for_memory(gb: float, minutes: int = 60) -> bool:
    from .google import _available_gb, _incubator_busy
    for _ in range(minutes // 5):
        if _available_gb() >= gb and not _incubator_busy():
            return True
        time.sleep(300)
    return False


def make_documents(job: dict, resume: str) -> tuple[Path, list[str], list[str]]:
    """Tailored resume + cover letter (.docx) in ~/.jarvis/jobs/applications/<company>-<title>-<key>/.
    Returns (folder, lines removed as unsupported, skills added from the posting)."""
    details(job)
    extra = config()["add_posting_skills"]
    args = dict(resume=resume[:8000], title=job["title"], company=job["company"], location=job["location"],
                posting=job["text"][:6000], posting_skills_rule=POSTING_SKILLS_RULE if extra else ".",
                summary_rule=SUMMARY_RULE if extra else "", letter_rule=LETTER_RULE if extra else "")
    draft = _ask_json(RESUME_PROMPT.format(**args), WRITE_MODEL, num_ctx=24576, think=True)
    raw = _ask_json(REVIEW_RESUME.format(draft=json.dumps(draft, ensure_ascii=False), **args), WRITE_MODEL,
                    num_ctx=24576, think=True) or draft
    res, dropped, added = verify_resume(raw, resume, job["text"] if extra else "")
    letter = _ask_json(LETTER_PROMPT.format(**args), WRITE_MODEL, num_ctx=24576, think=True)
    letter = _ask_json(REVIEW_LETTER.format(draft=json.dumps(letter, ensure_ascii=False), **args), WRITE_MODEL,
                       num_ctx=24576, think=True) or letter
    paras = []
    for p in letter.get("paragraphs", []):
        sents = re.split(r"(?<=[.!?])\s+", str(p))
        dropped += [f"letter: {s}" for s in sents if not _supported(s, resume)]
        if kept := " ".join(s for s in sents if _supported(s, resume)):
            paras.append(kept)
    folder = OUT / re.sub(r"[^A-Za-z0-9]+", "-", f"{job['company']}-{job['title']}")[:60].strip("-")
    folder = folder.with_name(f"{folder.name}-{job['key']}")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    trimmed = fit_one_page(res, folder)
    dropped += [f"cut to fit one page — {t}" for t in trimmed]
    render({"blocks": _letter_blocks(res, letter.get("greeting") or "Dear Hiring Team,", paras,
                                     letter.get("closing") or "Sincerely,")},
           folder / "cover_letter.pdf", folder / "cover_letter.docx", 1.0)
    (folder / "posting.txt").write_text(f"{job['title']} - {job['company']} ({job['location']})\n{job['url']}\n\n{job['text']}")
    (folder / "check.txt").write_text(
        "Skills added from the job posting (not in your master resume; be ready to talk about them):\n"
        + ("\n".join(f"  {a}" for a in added) if added else "  (none)")
        + "\n\nRemoved because the master resume doesn't support it:\n"
        + ("\n".join(f"  {d}" for d in dropped) if dropped else "  (nothing)") + "\n")
    for f in ("posting.txt", "check.txt"):
        (folder / f).chmod(0o600)
    meta = {"key": job["key"], "added_skills": added, "removed": len(dropped)}
    _write_private(folder / "meta.json", meta)
    return folder, dropped, added


# --- search --------------------------------------------------------------------------

def search(max_scored: int = 150) -> tuple[list[dict], dict]:
    """New matching postings from the configured companies; those without visa-sponsorship evidence are skipped
    (and remembered, so they aren't re-checked). Up to `max_scored` of the rest are scored locally, newest first and
    spread across companies; the others wait for the next run. Returns (new matches best first, skip counts)."""
    conf, st = config(), _load()
    resume = resume_text()
    new, skipped = [], {"no sponsorship": 0, "sponsorship unconfirmed": 0}
    queue: dict[str, list] = {}
    for c in conf["companies"]:
        try:
            jobs = [j for j in fetch(c, conf) if j["key"] not in st["postings"] and matches(j, conf)]
        except Exception as e:  # noqa: BLE001 - one broken board doesn't stop the rest
            print(f"{c.get('name')}: {e}")
            continue
        for j in jobs:
            details(j)
            fits, level = level_ok(j, conf)
            if not fits:
                skipped["experience"] = skipped.get("experience", 0) + 1
                st["postings"][j["key"]] = {k: v for k, v in j.items() if k != "text"} | {
                    "status": "level", "level": level, "fit": 0, "found": date.today().isoformat()}
                continue
            j["level"] = level
            try:
                ok, evidence = visa_ok(j, c, conf)
            except Exception as e:  # noqa: BLE001 - try again next run
                print(f"{c['name']} {j['title']}: {e}")
                continue
            j.update(visa=evidence, found=date.today().isoformat())
            if ok:
                queue.setdefault(c["name"], []).append(j)
            else:
                skipped["no sponsorship" if evidence.startswith("posting") else "sponsorship unconfirmed"] += 1
                st["postings"][j["key"]] = {k: v for k, v in j.items() if k != "text"} | {"status": "no_visa", "fit": 0}
    for q in queue.values():
        q.sort(key=lambda j: _posted(j["posted"]), reverse=True)
    while queue and len(new) < max_scored:  # round-robin: one per company per pass
        for name in list(queue):
            if len(new) >= max_scored:
                break
            j = queue[name].pop(0)
            if not queue[name]:
                del queue[name]
            j.update(score(j, resume), status="new")
            st["postings"][j["key"]] = {k: v for k, v in j.items() if k != "text"}
            new.append(j)
        _write_private(STORE, st)
    _deep_scores(new, st, conf, resume)
    _write_private(STORE, st)
    return sorted(new, key=lambda j: -j["fit"]), skipped


def _deep_scores(new: list[dict], st: dict, conf: dict, resume: str) -> bool:
    """Second opinion: the larger model (thinking) re-scores the promising ones; its score is the one that counts.
    Leftover slots go to earlier finds still waiting for it (best quick score first). Saves as it goes.
    False when the writer model never got enough free memory (nothing was deep-scored)."""
    deep = sorted((j for j in new if j["fit"] >= conf["quick_fit_min"]), key=lambda j: -j["fit"])[:conf["deep_scores_per_day"]]
    backlog = sorted((p for p in st["postings"].values() if p.get("status") == "new" and p.get("scored_by") != "deep"
                      and p.get("fit", 0) >= conf["quick_fit_min"] and p["key"] not in {j["key"] for j in new}),
                     key=lambda p: -p["fit"])
    boards: dict = {}
    for p in backlog:
        if len(deep) >= conf["deep_scores_per_day"]:
            break
        b = (p["company"], p["board"], p["slug"])
        if b not in boards:
            try:
                boards[b] = {j["key"]: j for j in fetch({**_company(p["company"]), "board": p["board"], "slug": p["slug"]}, conf)}
            except Exception:  # noqa: BLE001
                boards[b] = {}
        j = boards[b].get(p["key"])
        if j is None:
            p["status"] = "closed"
            continue
        details(j)
        deep.append({**p, "text": j["text"]})
    ok = True
    if deep:
        _unload(SCORE_MODEL)
        ok = _wait_for_memory(WRITE_MIN_FREE_GB)
        if ok:
            for j in deep:
                if _past_stop():
                    break
                j.update(score(j, resume, deep=True), fit_quick=j["fit"])
                st["postings"][j["key"]].update({k: j[k] for k in ("fit", "fit_quick", "why", "missing", "scored_by")})
                _write_private(STORE, st)
            _unload(WRITE_MODEL)
    _write_private(STORE, st)
    return ok


def _posted(s: str) -> str:
    """Sortable date from '2026-10-01' or Amazon's 'October  1, 2026'."""
    try:
        return datetime.strptime(" ".join(s.split()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return s[:10]


def _company(name: str) -> dict:
    return next((c for c in config()["companies"] if c["name"] == name), {"name": name})


def prepare(key: str, resume: str | None = None) -> tuple[Path, list[str], list[str]]:
    """Documents for one stored posting (CLI: jobs docs KEY). The posting is re-read from its board."""
    p = _load()["postings"][key]
    job = next(j for j in fetch({**_company(p["company"]), "board": p["board"], "slug": p["slug"]}) if j["key"] == key)
    _unload(SCORE_MODEL)
    folder, dropped, added = make_documents(job, resume or resume_text())
    st = _load()
    st["postings"][key].update(status="documents", folder=str(folder), added_skills=added)
    _write_private(STORE, st)
    report()
    return folder, dropped, added


# --- the applications document -------------------------------------------------------

REPORT_HTML = HOME / "Applications.html"
REPORT_DOCX = HOME / "Applications.docx"
REPORT_PDF = HOME / "Applications.pdf"
HOW_TO_APPLY = {
    "greenhouse": "Open the posting → Apply (Greenhouse form): upload resume.pdf + cover_letter.pdf",
    "lever": "Open the posting → Apply for this job (Lever form): upload both files",
    "ashby": "Open the posting → Application tab (Ashby form): upload both files",
    "smartrecruiters": "Open the posting → I'm interested (SmartRecruiters): upload both files",
    "amazon": "Open the posting → Apply now (sign in to amazon.jobs): upload resume; cover letter optional",
}
STATUS_LABEL = {"documents": "Ready to apply", "new": "Match (no documents yet)", "applied": "Applied",
                "in_review": "In review", "assessment": "Assessment", "interview": "Interview", "offer": "Offer",
                "rejected": "Rejected", "withdrawn": "Withdrawn"}


def report() -> Path:
    """~/.jarvis/jobs/Applications.html (+ .docx): one row per position with documents (and the best other
    matches): position, how/where to apply, where the tailored resume and cover letter are, status."""
    conf, st = config(), _load()
    ready = [p for p in st["postings"].values() if p.get("folder")]
    ready.sort(key=lambda p: (p.get("status") != "documents", -p.get("fit", 0)))
    more = sorted((p for p in st["postings"].values() if p.get("status") == "new" and p.get("fit", 0) >= conf["min_fit"]),
                  key=lambda p: -p.get("fit", 0))[:40]
    home = str(Path.home())

    def row(i, p, docs):
        folder = Path(p["folder"]) if docs else None
        f = lambda name: (f"<a href='file://{_e(folder / name)}'>{name}</a>" if folder else "—")  # noqa: E731
        pdf = lambda name: name.replace(".docx", ".pdf") if folder and (folder / name.replace(".docx", ".pdf")).exists() else name  # noqa: E731
        added = ", ".join(p.get("added_skills", [])[:8])
        return (f"<tr><td>{i}</td><td><b>{_e(p['company'])}</b><br>{_e(p['title'])}<br><small>{_e(p['location'][:70])}"
                f"</small></td><td><nobr>{p.get('fit', '')}/10</nobr><br><small>{_e(p.get('level', ''))}</small></td>"
                f"<td><a href='{_e(p['url'])}'>Posting / apply</a><br><small>{_e(HOW_TO_APPLY.get(p['board'], 'Open the posting and apply'))}"
                f"</small></td><td>{f(pdf('resume.docx'))}<br>{f(pdf('cover_letter.docx'))}"
                + (f"<br><small>{_e(str(folder).replace(home, '~'))}</small>" if folder else
                   f"<br><small>make them: python3 -m jarvis jobs docs {p['key']}</small>")
                + (f"<br><small>Added from posting: {_e(added)}</small>" if added else "")
                + f"</td><td><small>{_e(p.get('visa', ''))}</small></td>"
                f"<td>{_e(STATUS_LABEL.get(p.get('status'), p.get('status', '')))}</td></tr>")

    widths = (18, 150, 40, 160, 175, 85, 62)  # 690 pt: fits a landscape page with 0.4in margins
    head = ("".join(f"<col width='{w}'>" for w in widths) +
            "<tr><th>#</th><th>Position</th><th>Fit</th><th>Where / how to apply</th><th>Tailored resume & cover "
            "letter</th><th>Visa</th><th>Status</th></tr>")
    html = (f"<h1>Job applications</h1><p>Updated {datetime.now():%B %d, %Y %H:%M}. {len(ready)} position(s) with "
            f"tailored documents (PDF to send; DOCX copy to edit). Review each one (check.txt lists skills added from the posting) before applying."
            f"</p><h2>Ready to apply</h2><table>{head}"
            + "".join(row(i, p, True) for i, p in enumerate(ready, 1)) + "</table>"
            + (f"<h2>Other good matches (fit ≥ {conf['min_fit']}/10, best first)</h2><table>{head}"
               + "".join(row(i, p, False) for i, p in enumerate(more, 1)) + "</table>" if more else ""))
    page = _page(html).replace("</style>", "table{border-collapse:collapse;width:690px;table-layout:fixed}td,th{border:1px solid #bbb;"
                               "padding:4px;vertical-align:top;font-size:10px}th{background:#eee}</style>")
    REPORT_HTML.write_text(page)
    REPORT_HTML.chmod(0o600)
    _docx(page, REPORT_DOCX)
    render({"source": str(REPORT_HTML), "landscape": True}, REPORT_PDF, None, 0.4)
    return REPORT_HTML


# --- Gmail tracking ----------------------------------------------------------------

JOB_Q = ('(application OR applying OR applied OR candidacy OR candidate OR interview OR "your interest" OR offer '
         'OR assessment OR recruiter OR recruiting OR "next steps" OR position) -category:promotions '
         '-category:social -in:chats')
ATS = re.compile(r"(greenhouse|lever\.co|ashbyhq|smartrecruiters|workday|icims|jobvite|taleo|successfactors|"
                 r"recruitee|workable|bamboohr|talent|careers|recruit|hiring|jobs@)", re.I)
STRONG = re.compile(r"(thank(s| you) for (applying|your application|your interest)|application (received|"
                    r"submitted|status|update)|we received your application|interview|unfortunately|not (to )?move "
                    r"forward|other candidates|offer letter|coding (challenge|assessment)|next steps)", re.I)

TRACK_PROMPT = """Is this email about one of the recipient's OWN job applications? Job alerts, newsletters,
recommendations, recruiting marketing and "jobs you may like" are NOT. Answer with ONLY this JSON:
{{"is_application": true/false, "company": "", "role": "", "status": "applied|in_review|assessment|interview|offer|
rejected|withdrawn", "next_step": "short, e.g. 'phone screen Oct 14' or ''"}}
- applied: confirmation the application was received. in_review: being reviewed / recruiter reached out.
  assessment: coding challenge or test. interview: interview invite or scheduling. offer: an offer.
  rejected: not moving forward. withdrawn: the candidate withdrew.

From: {sender}
Subject: {subject}
{body}"""


def _app_key(company: str, role: str) -> str:
    return f"{_norm(company)}|{_norm(role)}"


def track(months: int | None = None, max_opened: int = 40) -> list[dict]:
    """Read new job-related emails with the local model and update application statuses. Returns the new events
    (also kept in jobs.json "events" for the weekly summary)."""
    consent.require("jobs", "track your job applications")
    consent.require("email", "job applications: read job-related emails, read-only, processed locally")
    from . import google
    st = _load()
    apps, seen = st.setdefault("applications", {}), set(st.get("seen", []))
    if months:
        since = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp())
    else:
        since = st.get("gmail_after") or int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    ids, page = [], None
    while True:
        res = google._get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                          {"q": f"after:{since} {JOB_Q}", "maxResults": 100, **({"pageToken": page} if page else {})})
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page or len(ids) >= 2000:
            break
    changes, opened, complete = [], 0, True
    for mid in reversed(ids):  # oldest first, so the latest status wins
        if mid in seen:
            continue
        if opened >= max_opened:
            complete = False
            break
        seen.add(mid)
        meta = google._get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                           {"format": "metadata", "metadataHeaders": ["From", "Subject"]})
        sender, subject, snip = google._header(meta, "From"), google._header(meta, "Subject"), meta.get("snippet", "")
        if not (STRONG.search(f"{subject} {snip}") or ATS.search(sender)):
            continue
        full = google._get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}", {"format": "full"})
        opened += 1
        d = _ask_json(TRACK_PROMPT.format(sender=sender, subject=subject,
                                          body=google._body(full.get("payload", {}))[:5000]), SCORE_MODEL, 8192)
        if d.get("is_application") is not True or d.get("status") not in STATUSES or not d.get("company"):
            continue
        when = datetime.fromtimestamp(int(meta["internalDate"]) / 1000).date().isoformat()
        key = _app_key(d["company"], d.get("role", ""))
        if not d.get("role"):  # "your application to X": attach to that company's only open application
            same = [k for k in apps if k.startswith(_norm(d["company"]) + "|")]
            key = same[0] if len(same) == 1 else key
        a = apps.setdefault(key, {"company": d["company"][:80], "role": str(d.get("role", ""))[:120],
                                  "history": []})
        if a.get("status") != d["status"] or d["status"] in ("interview", "in_review", "assessment"):
            ev = {"date": when, "company": a["company"], "role": a["role"], "status": d["status"],
                  "next_step": str(d.get("next_step", ""))[:120], "subject": subject[:100]}
            st.setdefault("events", []).append(ev)
            changes.append(ev)
        a.update(status=d["status"], updated=when, next_step=str(d.get("next_step", ""))[:120])
        a["history"].append([when, d["status"]])
        for p in st.get("postings", {}).values():  # link to a posting Jarvis found
            if _norm(p["company"]) == _norm(a["company"]) and _norm(p["title"]) == _norm(a["role"]):
                p["status"] = d["status"]
    if complete and not months:
        st["gmail_after"] = int(time.time()) - 3600
    st["seen"] = list(seen)[-3000:]
    st["events"] = [e for e in st.get("events", []) if e["date"] >= (date.today() - timedelta(days=60)).isoformat()]
    _write_private(STORE, st)
    return changes


# --- daily run --------------------------------------------------------------------------

def _send_file(path: Path, caption: str):
    from .reminders import _creds
    token, chat = _creds()
    if not token or not chat:
        return
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1000]}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=60).read()


def listing() -> str:
    st = _load()
    lines = ["Applications:"] + [f"  {a['status']:<10} {a['company']} — {a['role']} ({a.get('updated', '')})"
                                 + (f" next: {a['next_step']}" if a.get("next_step") else "")
                                 for a in sorted(st.get("applications", {}).values(), key=lambda a: a.get("updated", ""),
                                                 reverse=True)]
    open_ = sorted((p for p in st.get("postings", {}).values() if p.get("status") in ("new", "documents")),
                   key=lambda p: -p.get("fit", 0))
    lines += ["Matches (key fit company — title):"] + [
        f"  {p['key']} {p['fit']:>2} {p['company']} — {p['title']} [{p['status']}] {p['url']}" for p in open_[:30]]
    return "\n".join(lines)


NOTIFY_NOW = ("interview", "in_review", "assessment", "offer")  # interview requests / recruiter updates
GROUPS = [("📅 Interviews", ("interview",)), ("🎉 Offers", ("offer",)),
          ("➡️ Moving forward", ("in_review", "assessment")), ("❌ Rejections", ("rejected",)),
          ("📨 Applications confirmed", ("applied",)), ("↩️ Withdrawn", ("withdrawn",))]


def _event_line(e: dict) -> str:
    return (f"• {e['company']} — {e['role'] or 'role?'}" + (f": {e['next_step']}" if e.get("next_step") else "")
            + f" ({e['date'][5:]})")


def _past_stop() -> bool:
    return datetime.now().strftime("%H:%M") >= config().get("stop_by", "23:59") and datetime.now().hour >= 5


def make_week_documents(limit: int | None = None) -> tuple[int, list[str], int]:
    """Resume + cover letter for the week's best-ranked new positions (fit >= min_fit, top docs_per_week; ties:
    newest first)."""
    conf = config()
    st = _load()
    if any(p.get("status") == "new" and p.get("scored_by") != "deep" and p.get("fit", 0) >= conf["quick_fit_min"]
           for p in st["postings"].values()):
        if not _deep_scores([], st, conf, resume_text()):  # finish the week's careful scores first
            raise MemoryError("not enough free memory for the scoring model")
    todo = sorted((p for p in _load()["postings"].values()
                   if p.get("status") == "new" and p.get("scored_by") == "deep" and p.get("fit", 0) >= conf["min_fit"]),
                  key=lambda p: (p["fit"], _posted(p.get("posted", ""))), reverse=True)[:limit or conf["docs_per_week"]]
    if not todo:
        return 0, [], 0
    _unload(SCORE_MODEL)
    if not _wait_for_memory(WRITE_MIN_FREE_GB):
        raise MemoryError("not enough free memory for the writing model")
    resume, made, lines, left = resume_text(), 0, [], 0
    for i, j in enumerate(todo):
        if _past_stop():
            left = len(todo) - i  # the rest continues tomorrow morning (docs_pending)
            break
        try:
            folder, dropped, added = prepare(j["key"], resume)
        except StopIteration:  # the posting closed since it was found
            st = _load()
            st["postings"][j["key"]]["status"] = "closed"
            _write_private(STORE, st)
            continue
        made += 1
        lines.append(f"• {j['fit']}/10 {j['company']} — {j['title']}")
        if conf["send_documents"]:
            for f in ("resume.pdf", "cover_letter.pdf"):
                _send_file(folder / f, f"{j['company']} — {j['title']}")
    return made, lines, left


def weekly_summary(made: int, doc_lines: list[str], today: date) -> str:
    st, conf = _load(), config()
    since = (today - timedelta(days=7)).isoformat()
    events = [e for e in st.get("events", []) if e["date"] >= since]
    parts = [f"🗓 Weekly job update ({(today - timedelta(days=7)):%b %d} – {today - timedelta(days=1):%b %d})"]
    for label, statuses in GROUPS:
        group = [e for e in events if e["status"] in statuses]
        if group:
            parts.append(f"{label} ({len(group)}):\n" + "\n".join(_event_line(e) for e in group[:15]))
    if not events:
        parts.append("No application emails this week.")
    found = [p for p in st["postings"].values() if p.get("found", "") >= since and p.get("status") not in ("level", "no_visa")]
    good = [p for p in found if p.get("fit", 0) >= conf["min_fit"]]
    parts.append(f"💼 New positions this week: {len(found)} with visa sponsorship at your level, {len(good)} "
                 f"good matches (fit ≥ {conf['min_fit']}/10).")
    if made:
        parts.append(f"📝 Tailored resume + cover letter ready for {made} position(s):\n" + "\n".join(doc_lines[:20])
                     + ("\n…" if len(doc_lines) > 20 else "")
                     + "\n📋 Apply links and document locations: ~/Job Applications/Applications.html (attached)")
    return "\n\n".join(parts)


def run(today: date | None = None):
    """Daily 05:00 (launchd com.jarvis.jobs): Gmail tracking + search for new positions.
    Telegram: right away only for interview requests / recruiter updates (never rejections). Mondays (or the first
    run after a missed Monday): resumes + cover letters for the week's new positions, then one weekly summary with
    the report attached. Yields to the incubator and unloads its models when done."""
    from .google import _incubator_busy
    from .reminders import send
    today = today or date.today()
    if not consent.granted("jobs"):
        print("consent 'jobs' not granted: python3 -m jarvis consent grant jobs")
        return
    for _ in range(9):  # wait up to 45 min for running incubator jobs, else skip the day
        if not _incubator_busy():
            break
        time.sleep(300)
    else:
        print(f"{datetime.now():%F %H:%M} skipped: the incubator was busy for 45 min")
        return
    conf, log = config(), []
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    try:
        if consent.granted("email"):
            events = track()
            log.append(f"tracking: {len(events)} event(s)")
            urgent = [e for e in events if e["status"] in NOTIFY_NOW]
            if urgent:
                send("📬 Job application update:\n" + "\n".join(
                    f"{'📅 Interview' if e['status'] == 'interview' else '🎉 Offer' if e['status'] == 'offer' else '➡️ Recruiter update'}"
                    f" — {e['company']}, {e['role'] or 'role?'}" + (f"\n   {e['next_step']}" if e.get("next_step") else "")
                    + f"\n   email: “{e['subject']}”" for e in urgent))
        done = _load().get("weekly_done", "")
        if (today.weekday() == 0 and done != week) or (done and done < week):  # Monday, or a missed Monday
            try:
                made, lines, pending = make_week_documents()
            except MemoryError as e:  # no summary yet: tomorrow's run counts as the missed Monday and retries
                from .google import _available_gb
                log.append(f"weekly postponed: {e} ({_available_gb():.1f} GB available)")
            else:
                report()
                send(weekly_summary(made, lines, today)
                     + (f"\n\n⏳ {pending} more resume set(s) will be finished tomorrow morning." if pending else ""))
                if REPORT_PDF.exists():
                    _send_file(REPORT_PDF, "Job applications: positions, apply links, document locations")
                st = _load()
                st["weekly_done"] = week
                st["docs_pending"] = pending
                _write_private(STORE, st)
                log.append(f"weekly: {made} document set(s), {pending} pending")
        elif _load().get("docs_pending"):  # Monday's leftovers, quietly (they're in next Monday's summary)
            made, _, left = make_week_documents(limit=_load()["docs_pending"])
            st = _load()
            st["docs_pending"] = left
            _write_private(STORE, st)
            report()
            log.append(f"leftover documents: {made}")
        if conf["search_daily"] and conf["companies"] and not _past_stop():
            new, skipped = search()
            log.append(f"search: {len(new)} new, skipped {skipped}")
    finally:
        _unload(SCORE_MODEL, WRITE_MODEL)
    print(f"{datetime.now():%F %H:%M} " + " | ".join(log))


if __name__ == "__main__":
    run()
