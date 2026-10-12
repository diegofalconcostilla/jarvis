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
        clean, dropped = jobs.verify_resume(raw, RESUME)
        self.assertEqual(clean["contact"], ["jane@example.com"])
        self.assertEqual(clean["skills"], ["Python", "AWS"])
        self.assertEqual([e["company"] for e in clean["experience"]], ["Acme Corp"])
        self.assertEqual(clean["experience"][0]["bullets"], ["Cut pipeline runtime by 40%"])
        self.assertNotIn("7 years", clean["summary"])
        self.assertTrue(any("Globex" in d for d in dropped))


if __name__ == "__main__":
    unittest.main()
