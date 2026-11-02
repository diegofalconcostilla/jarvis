"""Screen feedback: consent first, one capture per request, image deleted, answer stays local."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import consent, screen  # noqa: E402

_LOGS = tempfile.TemporaryDirectory()


def setUpModule():
    screen.LOG = Path(_LOGS.name, "screen.log")  # tests never write the real log


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
            self.assertEqual(screen.run(), 0)   # cancelling isn't an error for Shortcuts
        cap.assert_not_called()

    def test_capture_is_private_after_resize(self):
        def fake_run(cmd, **kw):
            if cmd[0] == "screencapture":
                Path(cmd[-1]).write_bytes(b"png")
            if cmd[0] == "sips":
                Path(cmd[-1]).chmod(0o644)  # sips rewrites the file with default permissions
        with tempfile.TemporaryDirectory() as d, mock.patch.object(screen, "TMP", Path(d)), \
                mock.patch.object(screen.subprocess, "run", side_effect=fake_run):
            img = screen.capture("screen")
            self.assertEqual(img.stat().st_mode & 0o777, 0o600)

    def test_log_has_no_content(self):
        with mock.patch.object(screen.consent, "require"), \
                mock.patch.object(screen, "capture", return_value=None), mock.patch.object(screen, "show"):
            screen.run("my secret question", "screen")
        logged = screen.LOG.read_text()
        self.assertIn("no screenshot", logged)
        self.assertNotIn("secret", logged)

    def test_screen_is_a_private_scope(self):
        self.assertIn("screen", consent.PRIVATE_SCOPES)


class Session(unittest.TestCase):
    """A sequence of screenshots in one conversation: history resent, old images become text, wiped after 3 h."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name, "screen-session")
        self.patch = mock.patch.object(screen, "SESSION_DIR", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _shot(self, question, answer="ok"):
        img = Path(self.tmp.name, f"{time.time_ns()}.png")
        img.write_bytes(b"png")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture", return_value=img), \
                mock.patch.object(screen.llm, "ollama_chat", return_value=answer) as chat, \
                mock.patch.object(screen.llm, "ollama", return_value="a described screen"), \
                mock.patch.object(screen, "show", return_value=None), mock.patch.object(screen, "notify"):
            screen.run_session(question, "screen")
        return chat

    def test_history_is_resent_and_only_newest_three_are_images(self):
        for i in range(4):
            self._shot(f"q{i}", f"a{i}")
        chat = self._shot("compare them")
        msgs = chat.call_args.args[0]
        users = [m for m in msgs if m["role"] == "user"]
        self.assertEqual(len(users), 5)
        self.assertEqual(sum(1 for m in users if m.get("images")), 3)
        self.assertIn("a described screen", users[0]["content"])           # oldest: text only
        self.assertIn({"role": "assistant", "content": "a0"}, msgs)        # earlier answers kept
        self.assertEqual(len(list(self.dir.glob("*.png"))), 5)              # all kept until the session is wiped

    def test_empty_question_just_adds(self):
        chat = self._shot("")
        chat.assert_not_called()
        self.assertEqual(len(screen.load_session()["turns"]), 1)

    def test_deep_thinks_and_sees_more_screenshots(self):
        for _ in range(6):
            self._shot("")
        img = Path(self.tmp.name, "x.png")
        img.write_bytes(b"png")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture", return_value=img), \
                mock.patch.object(screen.llm, "ollama_chat", return_value="deep") as chat, \
                mock.patch.object(screen.llm, "ollama", return_value="desc") as describe, \
                mock.patch.object(screen, "show", return_value=None):
            screen.run_session("why?", "screen", deep=True)
        msgs, kw = chat.call_args.args[0], chat.call_args.kwargs
        self.assertEqual(chat.call_args.args[1], screen.VISION_MODEL)       # one model sees and reasons
        self.assertTrue(kw["think"])
        self.assertIn("reasoning", kw["system"])                            # and isn't held to 10 lines
        self.assertEqual(sum(1 for m in msgs if m.get("images")), screen.DEEP_IMAGES)
        self.assertEqual(describe.call_count, 1)                            # only the 7th-newest became text
        self.assertNotIn("10 short lines", describe.call_args.kwargs["system"])  # descriptions aren't cut short

    def test_quick_answers_also_think(self):
        chat = self._shot("what is this?")
        self.assertTrue(chat.call_args.kwargs["think"])                     # accuracy over speed
        self.assertEqual(sum(1 for m in chat.call_args.args[0] if m.get("images")), 1)

    def test_follow_up_without_new_capture(self):
        img = Path(self.tmp.name, "f.png")
        img.write_bytes(b"png")
        with mock.patch.object(screen.consent, "require"), \
                mock.patch.object(screen, "capture", return_value=img) as cap, \
                mock.patch.object(screen.llm, "ollama_chat", side_effect=["first", "second"]) as chat, \
                mock.patch.object(screen, "show", side_effect=["and this?", None]):
            screen.run_session("what?", "screen")
        cap.assert_called_once()
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args.args[0][-1], {"role": "user", "content": "and this?"})

    def test_prompt_asks_about_the_session_without_capturing(self):
        self._shot("")
        self._shot("")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture") as cap, \
                mock.patch.object(screen.llm, "ollama_chat", return_value="both fine") as chat, \
                mock.patch.object(screen, "show", return_value=None):
            self.assertEqual(screen.prompt_session("feedback on all of them?"), 0)
        cap.assert_not_called()
        msgs = chat.call_args.args[0]
        self.assertEqual(sum(1 for m in msgs if m.get("images")), 2)
        self.assertEqual(msgs[-1], {"role": "user", "content": "feedback on all of them?"})
        self.assertEqual(screen.load_session()["turns"][-1]["answer"], "both fine")

    def test_ask_only_button_asks_without_capturing(self):
        self._shot("")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture") as cap, \
                mock.patch.object(screen, "ask_session", return_value=("what do you see?", "none")), \
                mock.patch.object(screen.llm, "ollama_chat", return_value="a page") as chat, \
                mock.patch.object(screen, "show", return_value=None):
            self.assertEqual(screen.run_session(mode="window"), 0)   # even from a --window shortcut
        cap.assert_not_called()
        self.assertEqual(chat.call_args.args[0][-1]["content"], "what do you see?")

    def test_empty_ask_only_does_nothing(self):
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture") as cap, \
                mock.patch.object(screen, "ask_session", return_value=("", "none")), \
                mock.patch.object(screen.llm, "ollama_chat") as chat:
            self.assertEqual(screen.run_session(), 0)
        cap.assert_not_called()
        chat.assert_not_called()

    def test_window_comes_back_after_adding_to_capture_another_or_ask(self):
        imgs = []
        for n in range(2):
            imgs.append(Path(self.tmp.name, f"loop{n}.png"))
            imgs[-1].write_bytes(b"png")
        windows = [("", "area"), ("", "area"), ("compare them", "none")]
        with mock.patch.object(screen.consent, "require"), \
                mock.patch.object(screen, "capture", side_effect=imgs) as cap, \
                mock.patch.object(screen, "ask_session", side_effect=windows) as ask, \
                mock.patch.object(screen.llm, "ollama_chat", return_value="same app") as chat, \
                mock.patch.object(screen, "show", return_value=None):
            self.assertEqual(screen.run_session(), 0)
        self.assertEqual(cap.call_count, 2)
        self.assertEqual([c.args for c in ask.call_args_list], [(0, False), (1, True), (2, True)])
        self.assertEqual(sum(1 for m in chat.call_args.args[0] if m.get("images")), 2)

    def test_cancel_after_adding_ends_quietly(self):
        img = Path(self.tmp.name, "one.png")
        img.write_bytes(b"png")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture", return_value=img), \
                mock.patch.object(screen, "ask_session", side_effect=[("", "area"), None]):
            self.assertEqual(screen.run_session(), 0)
        self.assertEqual(len(screen.load_session()["turns"]), 1)

    def test_prompt_without_session_asks_nothing(self):
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen.llm, "ollama_chat") as chat, \
                mock.patch.object(screen, "show") as show:
            self.assertEqual(screen.prompt_session("q"), 0)
        chat.assert_not_called()
        self.assertIn("No screenshots", show.call_args.args[0])

    def test_wiped_after_three_hours_unused(self):
        self._shot("")
        self.assertFalse(screen.expire(time.time() + screen.SESSION_TTL - 60))
        self.assertTrue(self.dir.exists())
        self.assertTrue(screen.expire(time.time() + screen.SESSION_TTL + 60))
        self.assertFalse(self.dir.exists())

    def test_end_word_deletes_everything(self):
        self._shot("")
        with mock.patch.object(screen.consent, "require"), mock.patch.object(screen, "capture") as cap, \
                mock.patch.object(screen, "show"):
            screen.run_session("/end", "screen")
        cap.assert_not_called()
        self.assertFalse(self.dir.exists())

    def test_session_refuses_without_consent(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), mock.patch.object(screen, "capture") as cap, \
                self.assertRaises(consent.ConsentRequired):
            screen.run_session("q", "screen")
        cap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
