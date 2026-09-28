"""Proves the guardrails block what they claim to.  Run: python3 -m unittest discover tests"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import consent, guard, llm  # noqa: E402


class NoApiBilling(unittest.TestCase):
    def test_detects_api_keys(self):
        self.assertEqual(guard.api_keys_in_env({"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": ""}), ["ANTHROPIC_API_KEY"])

    def test_model_calls_refuse_with_an_api_key_set(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            with self.assertRaises(PermissionError):
                llm.claude("hi")
            with self.assertRaises(PermissionError):
                llm.ollama("hi", "any-model")

    def test_profile_exports_are_found(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".zshrc").write_text("export OPENAI_API_KEY=abc\n")
            self.assertEqual(guard.api_keys_in_profiles(Path(d)), [".zshrc: OPENAI_API_KEY"])


class Secrets(unittest.TestCase):
    def test_finds_common_secrets(self):
        fake_tg = "1234567890:" + "A" * 35
        text = f"token={fake_tg}\nkey=sk-ant-" + "b" * 30
        self.assertIn("Telegram bot token", guard.find_secrets(text))
        self.assertIn("Anthropic key", guard.find_secrets(text))

    def test_clean_text_passes(self):
        self.assertEqual(guard.find_secrets("JARVIS_MODEL=qwen3:30b-a3b\nport 11434"), [])


class Consent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(consent, "STORE", Path(self.tmp.name) / "consent.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_private_scopes_denied_by_default(self):
        for scope in consent.PRIVATE_SCOPES:
            with self.assertRaises(consent.ConsentRequired):
                consent.require(scope)

    def test_scripts_cannot_grant_themselves(self):
        with mock.patch.object(sys, "stdin", io.StringIO("y\n")):  # not a TTY
            self.assertFalse(consent.grant("email"))
        self.assertFalse(consent.granted("email"))

    def test_person_at_terminal_can_grant_and_revoke(self):
        fake_tty = io.StringIO("y\n")
        fake_tty.isatty = lambda: True
        with mock.patch.object(sys, "stdin", fake_tty), mock.patch("builtins.input", return_value="y"):
            self.assertTrue(consent.grant("calendar"))
        consent.require("calendar")  # no exception now
        consent.revoke("calendar")
        self.assertFalse(consent.granted("calendar"))


if __name__ == "__main__":
    unittest.main()
