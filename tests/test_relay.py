"""Relay: AI sessions can only queue whitelisted requests; Jarvis runs them outside the session."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import relay  # noqa: E402


class Relay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.p = [mock.patch.object(relay, "HOME", base), mock.patch.object(relay, "INBOX", base / "inbox"),
                  mock.patch.object(relay, "DONE", base / "done"), mock.patch.object(relay, "LOCAL", base / "none")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_only_known_actions_and_parameters(self):
        with self.assertRaises(ValueError):
            relay.validate("shell", {"cmd": "ls"})
        with self.assertRaises(ValueError):
            relay.validate("agenda", {"start": "2026-11-21", "end": "2026-11-22", "extra": "x"})
        with self.assertRaises(ValueError):
            relay.validate("agenda", {"start": "2026-11-21\nrm", "end": "2026-11-22"})
        self.assertEqual(relay.validate("agenda", {"start": "2026-11-21", "end": "2026-11-22"}),
                         {"start": "2026-11-21", "end": "2026-11-22"})

    def test_submit_queues_without_running(self):
        rid = relay.submit("agenda", {"start": "2026-11-21", "end": "2026-11-22"})
        self.assertEqual(relay.status(rid), "queued")
        self.assertEqual(json.loads((relay.INBOX / f"{rid}.json").read_text())["action"], "agenda")

    def test_run_refuses_inside_an_ai_session(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(SystemExit):
            relay.run()

    def test_run_records_status_only(self):
        rid = relay.submit("agenda", {"start": "2026-11-21", "end": "2026-11-22"})
        fake = mock.Mock(return_value="sent")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_CODE_ENTRYPOINT": ""}), \
                mock.patch.dict(relay.ACTIONS, {"agenda": (fake, relay.ACTIONS["agenda"][1])}):
            relay.run()
        self.assertEqual(relay.status(rid), "sent")
        self.assertFalse(list(relay.INBOX.glob("*.json")))

    def test_jobs_track_params(self):
        self.assertEqual(relay.validate("jobs_track", {"days": 3, "recheck": True}), {"days": 3, "recheck": True})
        with self.assertRaises(ValueError):
            relay.validate("jobs_track", {"days": "3; rm"})

    def test_long_messages_are_split(self):
        sent = []
        with mock.patch("jarvis.reminders.send", sent.append):
            relay._send_long("\n".join(["x" * 100] * 100))
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(m) <= 4096 for m in sent))

    def test_long_ranges_are_refused(self):
        with self.assertRaises(ValueError):
            relay._days("2026-11-01", "2026-12-31")


if __name__ == "__main__":
    unittest.main()
