"""Job applications: matching, and the guard that drops anything the master resume doesn't support."""

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import jobs  # noqa: E402

RESUME = """Jane Doe
jane@example.com | Seattle, WA
Skills: Python, SQL, AWS
Experience
Data Engineer, Acme Corp, 2021-2024
- Cut pipeline runtime by 40% for 12 teams
Education
BS Computer Science, State University, 2020"""

CONF = {**jobs.DEFAULT_CONFIG, "roles": ["data engineer"], "exclude": ["intern"], "locations": ["Seattle", "Remote"]}


class Matching(unittest.TestCase):
    def job(self, title, loc):
        return {"title": title, "location": loc}

    def test_role_words_and_location(self):
        self.assertTrue(jobs.matches(self.job("Senior Data Engineer, Platform", "Seattle, WA"), CONF))
        self.assertTrue(jobs.matches(self.job("Engineer, Data", "Remote - US"), CONF))
        self.assertFalse(jobs.matches(self.job("Data Engineer Intern", "Seattle"), CONF))
        self.assertFalse(jobs.matches(self.job("Data Engineer", "New York"), CONF))
        self.assertFalse(jobs.matches(self.job("Data Scientist", "Seattle"), CONF))


class Sponsorship(unittest.TestCase):
    def test_employer_names(self):
        self.assertTrue(jobs._same_employer(jobs._cname("OPENAI OPCO LLC"), jobs._cname("OpenAI")))
        self.assertTrue(jobs._same_employer(jobs._cname("Stripe Payments UK Ltd"), jobs._cname("Stripe")))
        self.assertFalse(jobs._same_employer(jobs._cname("Crusoe Inns Ltd"), jobs._cname("Crusoe")))
        self.assertFalse(jobs._same_employer(jobs._cname("Harvey and London Ltd"), jobs._cname("Harvey")))

    def test_posting_words(self):
        self.assertTrue(jobs.NO_SPONSOR.search("We are unable to sponsor visas for this role."))
        self.assertTrue(jobs.NO_SPONSOR.search("must be authorized to work without visa sponsorship"))
        self.assertTrue(jobs.YES_SPONSOR.search("We do sponsor visas! However, we aren't able to for every role."))
        self.assertFalse(jobs.NO_SPONSOR.search("We do sponsor visas! However, we aren't able to for every role."))

    def test_locations(self):
        conf = {**jobs.DEFAULT_CONFIG, "locations": list(jobs.LOCATIONS)}
        self.assertEqual(jobs.where({"location": "México D.F., CDMX, MX"}, conf), ["Mexico City", "Mexico"])
        self.assertEqual(jobs.where({"location": "San Francisco, CA"}, conf), ["California"])
        self.assertEqual(jobs.where({"location": "Austin, TX"}, conf), [])


class ExperienceLevel(unittest.TestCase):
    conf = {**jobs.DEFAULT_CONFIG, "experience": [2, 5]}

    def ok(self, title, text):
        return jobs.level_ok({"title": title, "text": text}, self.conf)[0]

    def test_years(self):
        self.assertEqual(jobs.years_required("3+ years of professional software development experience"), (3, 99))
        self.assertEqual(jobs.years_required("2-5 years of experience building APIs"), (2, 5))
        self.assertEqual(jobs.years_required("8+ years experience, or 5+ years of experience with a PhD"), (5, 99))
        self.assertEqual(jobs.years_required("five years of industry experience"), (5, 99))
        self.assertIsNone(jobs.years_required("Founded 10 years ago. Great benefits."))

    def test_level(self):
        self.assertTrue(self.ok("Software Engineer II", "3+ years of experience"))
        self.assertTrue(self.ok("Senior Software Engineer", "5+ years of experience"))
        self.assertFalse(self.ok("Senior Software Engineer", "7+ years of experience"))
        self.assertFalse(self.ok("Staff Software Engineer", ""))
        self.assertFalse(self.ok("Software Engineer, New Grad", ""))
        self.assertFalse(self.ok("Software Engineer", "0-1 years of experience"))
        self.assertTrue(self.ok("AI Engineer", "We value curiosity."))

    def test_government(self):
        gov = lambda title, text="", company=None: jobs.government({"title": title, "text": text}, company or {})  # noqa: E731
        self.assertTrue(gov("Forward Deployed Software Engineer - US Government"))
        self.assertTrue(gov("Software Engineer", "Must hold an active Secret clearance."))
        self.assertTrue(gov("Software Engineer", "Build tools for our defense customers."))
        self.assertTrue(gov("Software Engineer", "", {"government": True}))
        self.assertFalse(gov("Software Engineer", "All applicants regardless of veteran or military status."))
        self.assertFalse(gov("AI Engineer", "Ship LLM features to millions of users."))


class NothingInvented(unittest.TestCase):
    def test_numbers_must_come_from_the_resume(self):
        self.assertTrue(jobs._supported("Cut runtime by 40% for 12 teams", RESUME))
        self.assertFalse(jobs._supported("Led a team of 8 engineers", RESUME))
        self.assertFalse(jobs._supported("5+ years of experience", RESUME))

    def test_keep_all_bullets_falls_back_to_master_wording(self):
        raw = {"name": "Jane Doe", "experience": [{"company": "Acme Corp", "title": "Data Engineer",
                                                   "bullets": ["Cut pipeline runtime by 45% for 12 teams"]}]}
        real = jobs.config
        try:
            jobs.config = lambda: {**CONF, "keep_all_bullets": True}
            clean, dropped, _ = jobs.verify_resume(raw, RESUME)
            self.assertEqual(clean["experience"][0]["bullets"], ["Cut pipeline runtime by 40% for 12 teams"])
            jobs.config = lambda: {**CONF, "keep_all_bullets": False}
            clean, _, _ = jobs.verify_resume(raw, RESUME)
            self.assertEqual(clean["experience"][0]["bullets"], [])
        finally:
            jobs.config = real

    def test_fill_from_master_restores_header_bullets_and_skills(self):
        res = {"name": "", "contact": [], "summary": "", "skills": ["Python"],
               "experience": [{"company": "Acme Corp", "title": "Data Engineer", "dates": "2021-2024", "location": "",
                               "bullets": []}], "education": [], "other": []}
        real = jobs.config
        try:
            jobs.config = lambda: {**CONF, "keep_all_bullets": True}
            added = jobs.fill_from_master(res, RESUME)
        finally:
            jobs.config = real
        self.assertEqual(res["name"], "Jane Doe")
        self.assertIn("jane@example.com", res["contact"])
        self.assertEqual(res["experience"][0]["bullets"], ["Cut pipeline runtime by 40% for 12 teams"])
        self.assertEqual(res["skills"], ["Python", "SQL", "AWS"])
        self.assertIn("name", added)

    def test_verify_drops_unsupported_content(self):
        raw = {"name": "Jane Doe", "contact": ["jane@example.com", "555-0100"],
               "summary": "Data engineer focused on Python. Has 7 years of Spark experience.",
               "skills": ["Python", "Kubernetes", "AWS"],
               "experience": [{"company": "Acme Corp", "title": "Data Engineer", "dates": "2021-2024",
                               "bullets": ["Cut pipeline runtime by 40%", "Saved $2M per year"]},
                              {"company": "Globex", "title": "CTO", "dates": "2019", "bullets": ["Ran it"]}],
               "education": [{"school": "State University", "degree": "BS Computer Science", "dates": "2020"}]}
        clean, dropped, added = jobs.verify_resume(raw, RESUME)
        self.assertEqual(clean["contact"], ["jane@example.com"])
        self.assertEqual(clean["skills"], ["Python", "AWS"])
        self.assertEqual([e["company"] for e in clean["experience"]], ["Acme Corp"])
        self.assertEqual(clean["experience"][0]["bullets"], ["Cut pipeline runtime by 40%"])
        self.assertNotIn("7 years", clean["summary"])
        self.assertTrue(any("Globex" in d for d in dropped))
        self.assertEqual(added, [])

    def test_posting_skills_are_allowed_and_reported(self):
        raw = {"skills": ["Python", "LangChain", "RAG", "Rust"], "experience": [
            {"company": "Acme Corp", "bullets": ["Built LangChain agents for 30 teams", "Built RAG search",
                                                 "Cut pipeline runtime by 40%"]}]}
        clean, dropped, added = jobs.verify_resume(raw, RESUME, "We use LangChain and RAG with Python.")
        self.assertEqual(clean["skills"], ["Python", "LangChain", "RAG"])
        self.assertEqual(added, ["LangChain", "RAG"])
        self.assertEqual(clean["experience"][0]["bullets"], ["Cut pipeline runtime by 40%"])  # no invented work


class Notifications(unittest.TestCase):
    """Telegram: interview requests / recruiter updates right away; never rejections; Monday = weekly summary."""

    def run_on(self, day, events, weekly_done=""):
        import json
        import tempfile
        from datetime import date
        from unittest import mock
        sent = []
        with tempfile.TemporaryDirectory() as d:
            store = Path(d, "jobs.json")
            store.write_text(json.dumps({"postings": {}, "events": events, "weekly_done": weekly_done}))
            with mock.patch.object(jobs, "STORE", store), mock.patch.object(jobs, "REPORT_DOCX", Path(d, "x.docx")), \
                    mock.patch.object(jobs, "REPORT_PDF", Path(d, "x.pdf")), \
                    mock.patch.object(jobs, "_send_file", side_effect=lambda *a, **k: sent.append(("file", a))), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call in a test")), \
                    mock.patch.object(jobs.consent, "granted", return_value=True), \
                    mock.patch("jarvis.google._incubator_busy", return_value=False), \
                    mock.patch.object(jobs, "track", return_value=events), \
                    mock.patch.object(jobs, "search", return_value=([], {})), \
                    mock.patch.object(jobs, "make_week_documents", return_value=(0, [], 0)), \
                    mock.patch.object(jobs, "report"), mock.patch.object(jobs, "_unload"), \
                    mock.patch.object(jobs, "config", return_value={**jobs.DEFAULT_CONFIG, "companies": [1]}), \
                    mock.patch("jarvis.reminders.send", side_effect=lambda t, **k: sent.append(t)):
                jobs.run(day)
        return sent

    ev = lambda self, status: {"date": "2026-10-13", "company": "Acme", "role": "SWE", "status": status,  # noqa: E731
                               "next_step": "", "subject": "s"}

    def test_weekday_rejection_is_silent(self):
        from datetime import date
        self.assertEqual(self.run_on(date(2026, 10, 13), [self.ev("rejected"), self.ev("applied")], "2026-W42"), [])

    def test_weekday_interview_is_sent(self):
        from datetime import date
        sent = self.run_on(date(2026, 10, 13), [self.ev("interview"), self.ev("rejected")], "2026-W42")
        self.assertEqual(len(sent), 1)
        self.assertIn("Interview", sent[0])
        self.assertNotIn("reject", sent[0].lower())

    def test_monday_weekly_summary_lists_rejections(self):
        from datetime import date
        sent = [s for s in self.run_on(date(2026, 10, 19), [self.ev("rejected")], "2026-W42") if isinstance(s, str)]
        self.assertEqual(len(sent), 1)
        self.assertIn("Weekly job update", sent[0])
        self.assertIn("Rejections (1)", sent[0])



class TrackFilters(unittest.TestCase):
    def test_assessment_platform_emails_pass_the_filters(self):
        self.assertTrue(jobs.ATS.search("Roblox via CodeSignal <noreply@codesignal.com>"))
        self.assertTrue(jobs.ATS.search("HackerRank <noreply@hackerrankforwork.com>"))
        self.assertTrue(jobs.STRONG.search("Complete your Roblox online assessment"))
        self.assertTrue(jobs.STRONG.search("Your technical test is ready"))
        self.assertIn("from:(codesignal OR", jobs.JOB_Q)
        self.assertIn("platform", jobs.TRACK_PROMPT)

    def test_email_straight_from_an_applied_company_reaches_the_model(self):
        st = {"postings": {"k": {"company": "Roblox", "title": "Senior SWE", "folder": "/x"}}, "applications": {}}
        msgs = [{"id": "m1"}]
        meta = {"payload": {"headers": [{"name": "From", "value": "Roblox <no-reply@roblox.com>"},
                                        {"name": "Subject", "value": "Your next step with us"}]},
                "snippet": "Hi, please complete the following", "internalDate": "1793000000000"}
        get = mock.Mock(side_effect=[{"messages": msgs}, meta, {"payload": {}}])
        with mock.patch.object(jobs, "_load", return_value=st), mock.patch.object(jobs, "_write_private"), \
                mock.patch.object(jobs.consent, "require"), mock.patch("jarvis.google._get", get), \
                mock.patch.object(jobs, "_ask_json", return_value={"is_application": True, "company": "Roblox",
                                                                     "role": "Senior SWE", "status": "assessment"}):
            events = jobs.track(days=3, recheck=True)
        self.assertEqual([e["status"] for e in events], ["assessment"])
        self.assertEqual(st["postings"]["k"]["status"], "assessment")



class RefreshOpen(unittest.TestCase):
    def test_postings_missing_from_the_board_close_and_unreadable_boards_change_nothing(self):
        st = {"postings": {
            "a": {"key": "a", "company": "Cursor", "board": "ashby", "slug": "cursor", "status": "documents"},
            "b": {"key": "b", "company": "Cursor", "board": "ashby", "slug": "cursor", "status": "documents"},
            "c": {"key": "c", "company": "Down", "board": "lever", "slug": "down", "status": "new"},
            "d": {"key": "d", "company": "Cursor", "board": "ashby", "slug": "cursor", "status": "applied"}}}

        def fetch(c, conf):
            if c["slug"] == "down":
                raise OSError("board unavailable")
            return [{"key": "a", "url": "https://cursor.com/careers/a"}]
        with mock.patch.object(jobs, "_load", return_value=st), mock.patch.object(jobs, "_write_private"), \
                mock.patch.object(jobs, "config", return_value={"companies": []}), mock.patch.object(jobs, "fetch", fetch):
            counts, result = jobs.refresh_open()
        self.assertEqual(result, {"a": "open", "b": "closed", "c": "unknown"})
        self.assertEqual([st["postings"][k]["status"] for k in "abcd"], ["documents", "closed", "new", "applied"])
        self.assertEqual(counts["Cursor"], [1, 1])
        self.assertEqual(st["postings"]["a"]["url"], "https://cursor.com/careers/a")


class MasterResume(unittest.TestCase):
    def test_tailored_copies_are_never_read_as_the_master(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jobs, "RESUME_DIR", Path(d)), \
                mock.patch.object(jobs.consent, "require"), mock.patch.object(jobs, "config", return_value={"file_prefix": "X"}):
            Path(d, "master.txt").write_text("MASTER")
            for name in ("X_resume.pdf", "001_Roblox_X_resume.pdf"):
                Path(d, name).write_text("tailored")
            self.assertEqual(jobs.resume_text(), "MASTER")



class PositionsPdf(unittest.TestCase):
    def test_only_position_and_url_columns_and_no_closed(self):
        import tempfile
        st = {"postings": {"a": {"company": "Cursor", "title": "SWE", "url": "https://x/a", "folder": "/f", "status": "documents", "fit": 8},
                           "b": {"company": "Gone", "title": "SWE", "url": "https://x/b", "folder": "/f", "status": "closed"}}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jobs, "HOME", Path(d)), \
                mock.patch.object(jobs, "_load", return_value=st), mock.patch.object(jobs, "render"):
            jobs.positions_pdf()
            html = Path(d, "Positions.html").read_text()
        self.assertIn("<th>Position</th><th>URL</th></tr>", html)
        self.assertIn("<b>Cursor</b>", html)
        self.assertEqual(html.count("<th>"), 2)
        self.assertIn("https://x/a", html)
        self.assertNotIn("Gone", html)



class AppliedAndGrouped(unittest.TestCase):
    def test_applied_positions_found_by_status_or_matching_application(self):
        st = {"postings": {
            "a": {"company": "Roblox", "title": "Senior Software Engineer, Automation Tools", "status": "documents"},
            "b": {"company": "Roblox", "title": "Software Engineer, Payments", "status": "documents"},
            "c": {"company": "Cursor", "title": "Software Engineer", "status": "assessment"},
            "d": {"company": "Lyft", "title": "Software Engineer", "status": "documents"}},
            "applications": {"x": {"company": "roblox", "role": "Senior Software Engineer - Automation Tools"},
                             "y": {"company": "Lyft", "role": ""}}}
        self.assertEqual(jobs.applied_keys(st), {"a", "c"})

    def test_grouped_by_company(self):
        ps = [{"company": "Zoox", "fit": 9}, {"company": "Cursor", "fit": 5}, {"company": "Cursor", "fit": 8}]
        self.assertEqual([(p["company"], p["fit"]) for p in jobs.by_company(ps)], [("Cursor", 8), ("Cursor", 5), ("Zoox", 9)])



class UploadFolder(unittest.TestCase):
    def test_numbered_in_report_order_and_never_the_master_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for k in ("z", "c"):
                (d / k).mkdir()
                (d / k / "X_resume.pdf").write_text(k)
            st = {"postings": {"z": {"company": "Zoox", "title": "SWE", "folder": str(d / "z"), "status": "documents"},
                               "c": {"company": "Cursor", "title": "SWE", "folder": str(d / "c"), "status": "documents"}}}
            (d / "up").mkdir()
            (d / "up" / "keep.txt").write_text("mine")
            with mock.patch.object(jobs, "UPLOAD_DIR", d / "up"), mock.patch.object(jobs, "RESUME_DIR", d / "master"), \
                    mock.patch.object(jobs, "_load", return_value=st), mock.patch.object(jobs, "config", return_value={"file_prefix": "X"}):
                self.assertEqual(jobs.upload_folder(), 2)
            self.assertEqual(sorted(f.name for f in (d / "up").iterdir()),
                             ["001_Cursor_X_resume.pdf", "002_Zoox_X_resume.pdf", "keep.txt"])
            with mock.patch.object(jobs, "UPLOAD_DIR", d / "master"), mock.patch.object(jobs, "RESUME_DIR", d / "master"), \
                    self.assertRaises(RuntimeError):
                jobs.upload_folder()



class CareersPage(unittest.TestCase):
    def test_ashby_ids_map_to_the_company_careers_page(self):
        html = ('{\\"id\\":\\"0ec39ed7-a5dc-4551-bb26-b7f4f9fb4a74\\",\\"title\\":\\"Software Engineer, Growth \\",'
                '\\"location\\":\\"SF\\",\\"urlSlug\\":\\"software-engineer-growth\\"},'
                '{\\"id\\":\\"aef8f0e7-37cd-4d6e-96c9-3af72f588bae\\",\\"title\\":\\"RL Data\\",\\"urlSlug\\":\\"rl-data\\"}')
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = html.encode()
        with mock.patch.object(jobs.urllib.request, "urlopen", return_value=resp):
            links = jobs._careers_links("https://cursor.com/careers")
        self.assertEqual(links, {"0ec39ed7-a5dc-4551-bb26-b7f4f9fb4a74": "https://cursor.com/careers/software-engineer-growth",
                                 "aef8f0e7-37cd-4d6e-96c9-3af72f588bae": "https://cursor.com/careers/rl-data"})



class LocationCheck(unittest.TestCase):
    def test_counts_matches_and_visa_for_one_place_without_saving(self):
        conf = {"locations": ["California"], "roles": ["software engineer"], "exclude": [], "require_sponsorship": True,
                "no_visa_needed": [], "companies": [{"name": "Acme", "board": "greenhouse", "slug": "acme"}]}
        posts = [{"title": "Software Engineer", "location": "Mexico City, Mexico", "text": "x"},
                 {"title": "Software Engineer", "location": "Austin, TX", "text": "x"}]
        with mock.patch.object(jobs, "config", return_value=conf), mock.patch.object(jobs, "fetch", return_value=posts), \
                mock.patch.object(jobs, "visa_ok", return_value=(False, "")), mock.patch.object(jobs, "_write_private") as w:
            r = jobs.location_check("Mexico City")
        self.assertEqual((r["configured"], r["country"], r["no_visa_needed"]), (False, "MX", False))
        self.assertEqual(r["companies"], {"Acme": [1, 0]})
        w.assert_not_called()



class MexicoLocation(unittest.TestCase):
    def test_anywhere_in_mexico_remote_or_latam_but_not_new_mexico(self):
        conf = {"locations": ["Mexico"]}
        for loc in ("Remote - Mexico", "Guadalajara, Jalisco", "LATAM (Remote)", "Ciudad de México", "Mexico City, MX"):
            self.assertEqual(jobs.where({"location": loc}, conf), ["Mexico"], loc)
        for loc in ("Albuquerque, New Mexico", "Santa Fe, NM", "Remote - US"):
            self.assertEqual(jobs.where({"location": loc}, conf), [], loc)
        self.assertEqual(jobs.where({"location": "Remote - New Mexico or Mexico"}, conf), ["Mexico"])


if __name__ == "__main__":
    unittest.main()