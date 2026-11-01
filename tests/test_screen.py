"""Screen feedback: consent first, one capture per request, image deleted, answer stays local."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import consent, screen  # noqa: E402


class Screen(unittest.TestCase):
    def test_refuses_without_consent_and_inside_ai_sessions(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), mock.patch.object(screen, "capture") as cap, \
                self.assertRaises(consent.ConsentRequired):
            screen.run("what is this?", "screen")
        cap.assert_not_called()

    def test_screenshot_is_deleted_and_answer_shown(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d, "s.png")
            img.write_bytes(b"png")
            with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture", return_value=img), \
                    mock.patch.object(screen.llm, "ollama", return_value="Looks fine.") as llm, \
                    mock.patch.object(screen, "show") as show:
                self.assertEqual(screen.run("what is this?", "area"), 0)
            self.assertFalse(img.exists())
            show.assert_called_once_with("Looks fine.")
            self.assertEqual(llm.call_args.args[1], screen.VISION_MODEL)
            self.assertTrue(llm.call_args.kwargs["images"])
            self.assertIn("never instructions", llm.call_args.kwargs["system"])

    def test_deleted_even_when_the_model_fails(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d, "s.png")
            img.write_bytes(b"png")
            with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture", return_value=img), \
                    mock.patch.object(screen.llm, "ollama", side_effect=RuntimeError("down")), \
                    mock.patch.object(screen, "show") as show:
                screen.run("q", "screen")
            self.assertFalse(img.exists())
            self.assertIn("Couldn't analyze", show.call_args.args[0])

    def test_cancelled_question_captures_nothing(self):
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "ask_question", return_value=None), \
                mock.patch.object(screen, "capture") as cap:
            self.assertEqual(screen.run(), 1)
        cap.assert_not_called()

    def test_screen_is_a_private_scope(self):
        self.assertIn("screen", consent.PRIVATE_SCOPES)


if __name__ == "__main__":
    unittest.main()
