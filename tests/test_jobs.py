"""Job applications: matching, and the guard that drops anything the master resume doesn't support."""

import sys
import unittest
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
        self.assertEqual(jobs.where({"location": "México D.F., CDMX, MX"}, conf), ["Mexico City"])
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


class NothingInvented(unittest.TestCase):
    def test_numbers_must_come_from_the_resume(self):
        self.assertTrue(jobs._supported("Cut runtime by 40% for 12 teams", RESUME))
        self.assertFalse(jobs._supported("Led a team of 8 engineers", RESUME))
        self.assertFalse(jobs._supported("5+ years of experience", RESUME))

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


if __name__ == "__main__":
    unittest.main()
