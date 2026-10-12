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
    "require_sponsorship": True,             # only postings with evidence the employer sponsors work visas
    "no_visa_needed": [],                    # countries (US, UK, CA, MX) where you need no sponsorship
    "min_fit": 7,                            # 0-10 local-model fit score needed to prepare documents
    "docs_per_day": 3,
    "search_daily": True,
    "send_documents": False,                 # also send the .docx files to Telegram (your own bot)
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
    if any(set(words(x)) <= tw for x in conf["exclude"] if words(x)):
        return False
    if not any(set(words(r)) <= tw for r in conf["roles"] if words(r)):
        return False
    return not conf["locations"] or bool(where(job, conf))


# --- work-visa sponsorship -------------------------------------------------------------
# Evidence, strongest first: what the posting says; then official public data on the employer:
#   US: USCIS H-1B Employer Data Hub (approvals per employer), UK: Home Office register of licensed sponsors.
# Canada / Mexico have no public sponsor list: only the posting's own words (or a company's "sponsors_in").

SPONSORS = HOME / "sponsors"
H1B_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-2023.csv"
UK_PAGE = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
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
        dest.write_bytes(r.read())


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


_H1B: dict | None = None
_UK: set | None = None


def _sponsor_lists() -> tuple[dict, set]:
    global _H1B, _UK
    if _H1B is None:
        import csv
        refresh_sponsor_data()
        _H1B, _UK = {}, set()
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
                    _UK.add(_cname(r.get("Organisation Name") or ""))
    return _H1B, _UK


def _employer_hits(company: dict, names) -> list[str]:
    wanted = [_cname(n) for n in company.get("legal_names") or [company["name"]]]
    return [n for n in names if any(n == w or n.startswith(w + " ") for w in wanted if w)]


def sponsorship(job: dict, company: dict, country: str) -> tuple[str, str]:
    """("yes" | "no" | "unknown", evidence). The posting's own words win over employer data."""
    text = details(job)
    if m := NO_SPONSOR.search(text):
        return "no", f"posting: “{m.group(0)}”"
    if m := YES_SPONSOR.search(text):
        return "yes", f"posting: “{m.group(0)}”"
    if country in company.get("sponsors_in", []):
        return "yes", company.get("sponsor_note", "company sponsors here (config)")
    h1b, uk = _sponsor_lists()
    if country == "US":
        n = sum(h1b[k] for k in _employer_hits(company, h1b))
        if n >= 3:
            return "yes", f"{n} H-1B approvals (USCIS FY2023)"
    if country == "UK" and _employer_hits(company, uk):
        return "yes", "UK licensed visa sponsor (Home Office register)"
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

def _ask_json(prompt: str, model: str, num_ctx: int = 12288) -> dict:
    if model.startswith("qwen3"):
        prompt = "/no_think\n" + prompt
    raw = llm.ollama(prompt, model, temperature=0.2, num_ctx=num_ctx, json_mode=True)
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


FIT_PROMPT = """You compare a candidate's resume with a job posting. Be strict and honest.
Answer with ONLY this JSON: {{"fit": 0-10, "why": "one short sentence", "missing": ["up to 3 key requirements the
resume does not show"]}}
10 = meets every key requirement; 7 = meets most, a credible application; 4 = a stretch; 0 = unrelated.

RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""


def score(job: dict, resume: str) -> dict:
    d = _ask_json(FIT_PROMPT.format(resume=resume[:6000], title=job["title"], company=job["company"],
                                    location=job["location"], posting=job["text"][:5000]), SCORE_MODEL)
    try:
        fit = max(0, min(10, int(d.get("fit", 0))))
    except (TypeError, ValueError):
        fit = 0
    return {"fit": fit, "why": str(d.get("why", ""))[:200],
            "missing": [str(m)[:80] for m in d.get("missing", []) if m][:3]}


# --- tailored documents ------------------------------------------------------------------

RESUME_PROMPT = """Tailor a resume to one job. STRICT RULES:
- Use ONLY facts in the MASTER RESUME. Never invent or inflate employers, titles, dates, degrees, skills, tools,
  numbers, metrics or achievements. Never copy a requirement from the job into the resume unless the master resume
  already shows it.
- You may: choose and reorder the most relevant experience and bullets, rephrase bullets to mirror the job's wording
  (same facts), and write a 2-3 sentence summary built only from the master resume.
- Keep company names, titles and dates exactly as written in the master resume.
Answer with ONLY this JSON:
{{"name": "", "contact": ["email/phone/city/links exactly as in the master resume"], "summary": "",
 "skills": ["skills from the master resume, most relevant first"],
 "experience": [{{"company": "", "title": "", "dates": "", "location": "", "bullets": [""]}}],
 "education": [{{"school": "", "degree": "", "dates": ""}}],
 "other": [{{"heading": "e.g. Certifications / Projects", "items": [""]}}]}}

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""

LETTER_PROMPT = """Write a cover letter for this job. STRICT RULES:
- Every claim about the candidate must come from the MASTER RESUME. Never invent experience, skills, numbers,
  years of experience, or reasons you can't support. Don't claim to know people at the company.
- 3-4 short paragraphs, under 300 words, confident and specific, no clichés. Mention the role and company.
- No address block, no date, no signature line (they are added separately).
Answer with ONLY this JSON: {{"greeting": "Dear Hiring Team,", "paragraphs": [""], "closing": "Sincerely,"}}

MASTER RESUME:
{resume}

JOB: {title} at {company} ({location})
{posting}"""

NUMBER = re.compile(r"\d[\d,.]*\+?%?")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _supported(text: str, resume: str) -> bool:
    """True when every number in `text` appears in the master resume (the cheapest invention check)."""
    rnums = {n.rstrip(".,+%") for n in NUMBER.findall(resume)}
    return all(n.rstrip(".,+%") in rnums for n in NUMBER.findall(text))


def _in_resume(s: str, resume_n: str) -> bool:
    return bool(_norm(s)) and _norm(s) in resume_n


def verify_resume(d: dict, resume: str) -> tuple[dict, list[str]]:
    """Drop anything the master resume doesn't support. Returns (clean resume, what was dropped)."""
    rn, dropped = _norm(resume), []
    clean = {"name": d.get("name", "") if _in_resume(d.get("name", ""), rn) else "",
             "contact": [c for c in d.get("contact", []) if isinstance(c, str) and _in_resume(c, rn)]}
    summ = [s for s in re.split(r"(?<=[.!?])\s+", str(d.get("summary", ""))) if s]
    clean["summary"] = " ".join(s for s in summ if _supported(s, resume))
    dropped += [f"summary: {s}" for s in summ if not _supported(s, resume)]
    clean["skills"] = [s for s in d.get("skills", []) if isinstance(s, str) and _in_resume(s, rn)]
    dropped += [f"skill: {s}" for s in d.get("skills", []) if isinstance(s, str) and not _in_resume(s, rn)]
    clean["experience"] = []
    for e in d.get("experience", []):
        if not isinstance(e, dict) or not _in_resume(e.get("company", ""), rn):
            dropped.append(f"experience: {e.get('company') if isinstance(e, dict) else e}")
            continue
        bullets = [b for b in e.get("bullets", []) if isinstance(b, str) and b.strip()]
        dropped += [f"bullet: {b}" for b in bullets if not _supported(b, resume)]
        clean["experience"].append({k: e.get(k, "") if _in_resume(e.get(k, ""), rn) else ""
                                    for k in ("company", "title", "dates", "location")}
                                   | {"bullets": [b for b in bullets if _supported(b, resume)]})
    clean["education"] = [{k: ed.get(k, "") for k in ("school", "degree", "dates")}
                          for ed in d.get("education", []) if isinstance(ed, dict) and _in_resume(ed.get("school", ""), rn)]
    clean["other"] = [{"heading": o.get("heading", ""),
                       "items": [i for i in o.get("items", []) if isinstance(i, str) and _supported(i, resume)]}
                      for o in d.get("other", []) if isinstance(o, dict) and o.get("items")]
    return clean, dropped


def _e(s) -> str:
    return htmllib.escape(str(s or ""))


def _resume_html(r: dict) -> str:
    h = [f"<h1>{_e(r['name'])}</h1><p class=c>{' &middot; '.join(_e(c) for c in r['contact'])}</p>"]
    if r["summary"]:
        h.append(f"<h2>Summary</h2><p>{_e(r['summary'])}</p>")
    if r["skills"]:
        h.append(f"<h2>Skills</h2><p>{', '.join(_e(s) for s in r['skills'])}</p>")
    if r["experience"]:
        h.append("<h2>Experience</h2>")
        for e in r["experience"]:
            h.append(f"<p><b>{_e(e['title'])}</b> &mdash; {_e(e['company'])}"
                     f"{', ' + _e(e['location']) if e['location'] else ''}<br><i>{_e(e['dates'])}</i></p>"
                     "<ul>" + "".join(f"<li>{_e(b)}</li>" for b in e["bullets"]) + "</ul>")
    if r["education"]:
        h.append("<h2>Education</h2>" + "".join(
            f"<p><b>{_e(ed['degree'])}</b> &mdash; {_e(ed['school'])} <i>{_e(ed['dates'])}</i></p>" for ed in r["education"]))
    for o in r["other"]:
        if o["items"]:
            h.append(f"<h2>{_e(o['heading'])}</h2><ul>" + "".join(f"<li>{_e(i)}</li>" for i in o["items"]) + "</ul>")
    return _page("".join(h))


def _page(body: str) -> str:
    return ("<html><head><meta charset='utf-8'><style>body{font-family:Calibri,Helvetica;font-size:11pt}"
            "h1{font-size:18pt;margin:0}h2{font-size:12pt;border-bottom:1px solid #444;margin:10pt 0 4pt}"
            "p.c{color:#333}ul{margin:0 0 6pt 0}</style></head><body>" + body + "</body></html>")


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


def make_documents(job: dict, resume: str) -> tuple[Path, list[str]]:
    """Tailored resume + cover letter (.docx) in ~/.jarvis/jobs/applications/<company>-<title>-<key>/."""
    details(job)
    args = dict(resume=resume[:8000], title=job["title"], company=job["company"], location=job["location"],
                posting=job["text"][:6000])
    raw = _ask_json(RESUME_PROMPT.format(**args), WRITE_MODEL, num_ctx=16384)
    res, dropped = verify_resume(raw, resume)
    letter = _ask_json(LETTER_PROMPT.format(**args), WRITE_MODEL, num_ctx=16384)
    paras = []
    for p in letter.get("paragraphs", []):
        sents = re.split(r"(?<=[.!?])\s+", str(p))
        dropped += [f"letter: {s}" for s in sents if not _supported(s, resume)]
        if kept := " ".join(s for s in sents if _supported(s, resume)):
            paras.append(kept)
    folder = OUT / re.sub(r"[^A-Za-z0-9]+", "-", f"{job['company']}-{job['title']}")[:60].strip("-")
    folder = folder.with_name(f"{folder.name}-{job['key']}")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _docx(_resume_html(res), folder / "resume.docx")
    head = f"<p><b>{_e(res['name'])}</b><br>{'<br>'.join(_e(c) for c in res['contact'])}</p>"
    _docx(_page(f"{head}<p>{date.today():%B %d, %Y}</p><p>{_e(letter.get('greeting') or 'Dear Hiring Team,')}</p>"
                + "".join(f"<p>{_e(p)}</p>" for p in paras)
                + f"<p>{_e(letter.get('closing') or 'Sincerely,')}<br>{_e(res['name'])}</p>"), folder / "cover_letter.docx")
    (folder / "posting.txt").write_text(f"{job['title']} - {job['company']} ({job['location']})\n{job['url']}\n\n{job['text']}")
    (folder / "check.txt").write_text("Removed because the master resume doesn't support it:\n"
                                      + ("\n".join(dropped) if dropped else "(nothing)") + "\n")
    for f in ("posting.txt", "check.txt"):
        (folder / f).chmod(0o600)
    return folder, dropped


# --- search --------------------------------------------------------------------------

def search(max_scored: int = 40) -> tuple[list[dict], dict]:
    """New matching postings from the configured companies; those without visa-sponsorship evidence are skipped
    (and remembered, so they aren't re-checked), the rest scored locally. Returns (new matches best first, skip
    counts by reason)."""
    conf, st = config(), _load()
    resume = resume_text()
    new, skipped = [], {"no sponsorship": 0, "sponsorship unconfirmed": 0}
    for c in conf["companies"]:
        try:
            jobs = fetch(c, conf)
        except Exception as e:  # noqa: BLE001 - one broken board doesn't stop the rest
            print(f"{c.get('name')}: {e}")
            continue
        for j in jobs:
            if j["key"] in st["postings"] or not matches(j, conf):
                continue
            if len(new) >= max_scored:
                break  # the rest is scored next run
            try:
                ok, evidence = visa_ok(j, c, conf)
            except Exception as e:  # noqa: BLE001 - try again next run
                print(f"{c['name']} {j['title']}: {e}")
                continue
            j.update(visa=evidence, found=date.today().isoformat())
            if not ok:
                skipped["no sponsorship" if evidence.startswith("posting") else "sponsorship unconfirmed"] += 1
                st["postings"][j["key"]] = {k: v for k, v in j.items() if k != "text"} | {"status": "no_visa", "fit": 0}
                continue
            j.update(score(j, resume), status="new")
            st["postings"][j["key"]] = {k: v for k, v in j.items() if k != "text"}
            new.append(j)
    _write_private(STORE, st)
    return sorted(new, key=lambda j: -j["fit"]), skipped


def _company(name: str) -> dict:
    return next((c for c in config()["companies"] if c["name"] == name), {"name": name})


def prepare(key: str) -> tuple[Path, list[str]]:
    """Documents for one stored posting (CLI: jobs docs KEY)."""
    st = _load()
    p = st["postings"][key]
    job = next(j for j in fetch({**_company(p["company"]), "board": p["board"], "slug": p["slug"]}) if j["key"] == key)
    _unload(SCORE_MODEL)
    folder, dropped = make_documents(job, resume_text())
    p.update(status="documents", folder=str(folder))
    _write_private(STORE, st)
    return folder, dropped


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


def track(months: int | None = None, max_opened: int = 40) -> list[str]:
    """Read new job-related emails with the local model and update application statuses. Returns change lines."""
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
        if a.get("status") != d["status"]:
            changes.append(f"{a['company']} — {a['role'] or 'role?'}: {d['status'].replace('_', ' ')}"
                           + (f" ({d['next_step'][:60]})" if d.get("next_step") else ""))
        a.update(status=d["status"], updated=when, next_step=str(d.get("next_step", ""))[:120])
        a["history"].append([when, d["status"]])
        for p in st.get("postings", {}).values():  # link to a posting Jarvis found
            if _norm(p["company"]) == _norm(a["company"]) and _norm(p["title"]) == _norm(a["role"]):
                p["status"] = d["status"]
    if complete and not months:
        st["gmail_after"] = int(time.time()) - 3600
    st["seen"] = list(seen)[-3000:]
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


def run():
    """Daily (launchd com.jarvis.jobs, 04:00): search -> score -> documents for the best few -> Gmail tracking ->
    one Telegram summary. Yields to the incubator and unloads its models when done."""
    from .google import _incubator_busy
    from .reminders import send
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
    conf, msg = config(), []
    try:
        if conf["search_daily"] and conf["companies"]:
            new, skipped = search()
            good = [j for j in new if j["fit"] >= conf["min_fit"]]
            if new:
                msg.append(f"💼 {len(new)} new position(s) with visa sponsorship, {len(good)} with fit ≥ {conf['min_fit']}:")
                msg += [f"• {j['fit']}/10 {j['company']} — {j['title']} ({j['location'][:60]})\n  {j['why']}\n"
                        f"  🛂 {j['visa']}\n  {j['url']}\n  key {j['key']}" for j in new[:8]]
            if any(skipped.values()):
                msg.append("🚫 Skipped (visa): " + ", ".join(f"{n} {k}" for k, n in skipped.items() if n))
            todo = good[:conf["docs_per_day"]]
            if todo:
                _unload(SCORE_MODEL)
                if _wait_for_memory(WRITE_MIN_FREE_GB):
                    resume = resume_text()
                    st = _load()
                    for j in todo:
                        folder, dropped = make_documents(j, resume)
                        st["postings"][j["key"]].update(status="documents", folder=str(folder))
                        msg.append(f"📝 Resume + cover letter ready: {j['company']} — {j['title']}\n  "
                                   f"~/{folder.relative_to(Path.home())}"
                                   + (f"\n  ({len(dropped)} unsupported line(s) removed, see check.txt)" if dropped else ""))
                        if conf["send_documents"]:
                            for f in ("resume.docx", "cover_letter.docx"):
                                _send_file(folder / f, f"{j['company']} — {j['title']}")
                    _write_private(STORE, st)
                else:
                    msg.append("📝 Documents postponed: not enough free memory (retried tomorrow; or run "
                               "python3 -m jarvis jobs docs KEY)")
        if consent.granted("email"):
            changes = track()
            if changes:
                msg.append("📬 Application updates:\n" + "\n".join(f"• {c}" for c in changes))
    finally:
        _unload(SCORE_MODEL, WRITE_MODEL)
    print(f"{datetime.now():%F %H:%M} " + (" | ".join(m.splitlines()[0] for m in msg) or "nothing new"))
    if msg:
        send("\n\n".join(msg))


if __name__ == "__main__":
    run()
