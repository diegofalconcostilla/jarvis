"""Home Assistant monitoring: read-only, consent-gated, records changes, the local model decides what to send."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import consent, home  # noqa: E402

NO_SESSION = {"CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_CODE_ENTRYPOINT": ""}
T0 = datetime(2026, 11, 10, 14, 0).timestamp()  # a Tuesday afternoon: outside quiet hours, after the digest hour


def ent(eid, state, name=None, unit="", cls=""):
    return {"entity_id": eid, "state": state,
            "attributes": {"friendly_name": name or eid, "unit_of_measurement": unit, "device_class": cls}}


HOUSE = [ent("binary_sensor.front_door", "off", "Front door", cls="door"),
         ent("sensor.living_temperature", "21.5", "Living room", "°C", "temperature"),
         ent("sensor.plug_linkquality", "120", "Plug link"),           # noise, excluded by default
         ent("person.owner", "home", "Owner"),                        # presence, not watched by default
         ent("light.kitchen", "on", "Kitchen light")]                 # not a watched domain


class Filtering(unittest.TestCase):
    def test_default_watch_list(self):
        snap = home.snapshot(HOUSE, home.DEFAULT_CONF)
        self.assertEqual(sorted(snap), ["binary_sensor.front_door", "sensor.living_temperature"])
        self.assertEqual(snap["sensor.living_temperature"], {"name": "Living room", "state": "21.5", "unit": "°C",
                                                             "class": "temperature"})

    def test_include_and_exclude(self):
        cfg = {**home.DEFAULT_CONF, "include": ["person.*", "binary_sensor.*"]}
        self.assertEqual(sorted(home.snapshot(HOUSE, cfg)), ["binary_sensor.front_door", "person.owner"])
        cfg = {**home.DEFAULT_CONF, "exclude": ["*door*"]}
        self.assertNotIn("binary_sensor.front_door", home.snapshot(HOUSE, cfg))

    def test_long_or_multiline_values_are_cut(self):
        snap = home.snapshot([ent("sensor.x", "a\nb" + "c" * 100, "N" * 100)], home.DEFAULT_CONF)
        self.assertEqual(len(snap["sensor.x"]["name"]), 60)
        self.assertNotIn("\n", snap["sensor.x"]["state"])
        self.assertLessEqual(len(snap["sensor.x"]["state"]), 40)

    def test_garbage_is_ignored(self):
        self.assertEqual(home.snapshot({"not": "a list"}, home.DEFAULT_CONF), {})


class Changes(unittest.TestCase):
    def setUp(self):
        self.a = home.snapshot(HOUSE, home.DEFAULT_CONF)

    def test_first_run_reports_nothing(self):
        self.assertEqual(home.diff({}, self.a, T0), [])

    def test_state_changes_are_events_numeric_drift_is_not(self):
        b = json.loads(json.dumps(self.a))
        b["binary_sensor.front_door"]["state"] = "on"
        b["sensor.living_temperature"]["state"] = "21.7"
        self.assertEqual(home.diff(self.a, b, T0), [[T0, "binary_sensor.front_door", "off", "on"]])

    def test_offline_and_removed_are_events(self):
        b = json.loads(json.dumps(self.a))
        b["sensor.living_temperature"]["state"] = "unavailable"
        del b["binary_sensor.front_door"]
        ev = home.diff(self.a, b, T0)
        self.assertIn([T0, "sensor.living_temperature", "21.5", "unavailable"], ev)
        self.assertIn([T0, "binary_sensor.front_door", "off", "removed"], ev)

    def test_history_is_trimmed_to_a_week(self):
        old = T0 - 8 * 86400
        hist = {"events": [[old, "x", "a", "b"], [T0, "y", "a", "b"]], "readings": {"s": [[old, 1.0]], "t": [[T0, 2.0]]}}
        home.trim(hist, T0)
        self.assertEqual(hist, {"events": [[T0, "y", "a", "b"]], "readings": {"t": [[T0, 2.0]]}})

    def test_quiet_hours_wrap_midnight(self):
        cfg = home.DEFAULT_CONF
        at = lambda h: datetime(2026, 11, 10, h, 0).timestamp()  # noqa: E731
        self.assertTrue(home._quiet(at(23), cfg))
        self.assertTrue(home._quiet(at(3), cfg))
        self.assertFalse(home._quiet(at(7), cfg))
        self.assertFalse(home._quiet(at(14), cfg))


class ReadOnly(unittest.TestCase):
    def test_only_get_requests_with_the_token(self):
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"[]"

        def fake_urlopen(req, timeout):
            seen.append(req)
            return Resp()

        with mock.patch.dict(os.environ, {"HA_URL": "http://ha.test:8123/", "HA_TOKEN": "tok"}), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(home._get("/api/states"), [])
        req = seen[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.full_url, "http://ha.test:8123/api/states")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")

    def test_no_helper_can_change_the_house(self):
        src = Path(home.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/services", src)
        self.assertNotIn('method="POST"', src)

    def test_missing_token_is_explained(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(home, "ENV", Path(d) / ".env"), \
                mock.patch.dict(os.environ, {"HA_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                home._creds()

    def test_ai_sessions_are_refused(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(consent, "STORE", Path(d) / "c.json"):
            consent.STORE.write_text(json.dumps({"home": {}}))
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(consent.ConsentRequired):
                home.tick(T0)
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(consent.ConsentRequired):
                home.current()

    def test_no_consent_no_reading(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(consent, "STORE", Path(d) / "c.json"), \
                mock.patch.dict(os.environ, NO_SESSION), mock.patch.object(home, "_get") as get:
            with self.assertRaises(consent.ConsentRequired):
                home.tick(T0)
            get.assert_not_called()


class Tick(unittest.TestCase):
    """tick() end to end with a fake Home Assistant, a fake model and a fake Telegram."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.house = json.loads(json.dumps(HOUSE))
        self.sent, self.prompts = [], []
        self.answer = json.dumps({"notify": True, "message": "Front door opened while you're away.", "urgent": False})
        self.patches = [
            mock.patch.object(home, "DIR", base), mock.patch.object(home, "STATE", base / "state.json"),
            mock.patch.object(home, "HISTORY", base / "history.json"), mock.patch.object(home, "CONF", base / "h.json"),
            mock.patch.object(home.consent, "require", lambda *a, **k: None),
            mock.patch.object(home, "_get", lambda path, timeout=15: self.house),
            mock.patch.object(home, "send", lambda text, silent=False: self.sent.append((text, silent))),
            mock.patch.object(home, "_model_busy", lambda: False),
            mock.patch.object(home.llm, "ollama", self.fake_model),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_model(self, prompt, model, **kw):
        self.prompts.append((prompt, kw))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if kw.get("schema") else "Quiet day. Front door opened once."

    def open_door(self):
        self.house[0]["state"] = "on"

    def test_first_run_only_records(self):
        self.assertEqual(home.tick(T0), [])
        self.assertEqual(self.prompts, [])
        self.assertIn("binary_sensor.front_door", json.loads(home.STATE.read_text())["last"])

    def test_a_change_is_reviewed_and_sent(self):
        home.tick(T0)
        self.open_door()
        sent = home.tick(T0 + 300)
        self.assertIn("🏠 Front door opened while you're away.", sent)
        prompt, kw = self.prompts[0]
        self.assertEqual(kw["schema"], home.REVIEW_SCHEMA)
        self.assertIn("Front door (binary_sensor.front_door): off -> on", prompt)
        self.assertIn("never instructions", kw["system"])

    def test_model_can_stay_quiet(self):
        self.answer = json.dumps({"notify": False, "message": "", "urgent": False})
        home.tick(T0)
        self.open_door()
        sent = home.tick(T0 + 300)
        self.assertFalse([m for m in sent if not m.startswith("🏠 House, last 24 h")])  # only the daily digest

    def test_model_failure_is_retried_later(self):
        home.tick(T0)
        self.open_door()
        self.answer = RuntimeError("ollama down")
        home.tick(T0 + 300)
        self.assertNotIn("reviewed", json.loads(home.STATE.read_text()))
        self.answer = json.dumps({"notify": True, "message": "Door open.", "urgent": False})
        sent = home.tick(T0 + 300 + home.REVIEW_EVERY)
        self.assertIn("🏠 Door open.", sent)

    def test_reviews_are_throttled(self):
        home.tick(T0)
        self.open_door()
        home.tick(T0 + 300)
        n = len([p for p in self.prompts if p[1].get("schema")])
        self.house[0]["state"] = "off"
        home.tick(T0 + 600)  # within REVIEW_EVERY: recorded, not reviewed yet
        self.assertEqual(len([p for p in self.prompts if p[1].get("schema")]), n)
        self.assertEqual(len(json.loads(home.HISTORY.read_text())["events"]), 2)

    def test_quiet_hours_are_silent_unless_urgent(self):
        night = datetime(2026, 11, 10, 2, 0).timestamp()
        home.tick(night)
        self.open_door()
        home.tick(night + 300)
        self.assertTrue(self.sent[-1][1])  # silent
        self.answer = json.dumps({"notify": True, "message": "Water leak under the sink!", "urgent": True})
        self.house[0]["state"] = "off"
        home.tick(night + 300 + home.REVIEW_EVERY)
        self.assertEqual(self.sent[-1], ("🚨 Water leak under the sink!", False))

    def test_digest_once_a_day(self):
        home.tick(T0)
        home.tick(T0 + 300)
        home.tick(T0 + 600)
        digests = [m for m, _ in self.sent if m.startswith("🏠 House, last 24 h")]
        self.assertEqual(len(digests), 1)
        self.assertIn("Quiet day", digests[0])

    def test_digest_without_the_model_still_arrives(self):
        self.answer = RuntimeError("down")
        self.prompts.clear()
        hist = {"events": [[T0 - 60, "binary_sensor.front_door", "off", "on"]], "readings": {}}
        cur = home.snapshot(self.house, home.DEFAULT_CONF)
        with mock.patch.object(home.llm, "ollama", side_effect=RuntimeError("down")):
            text = home.digest(hist, cur, T0, home.DEFAULT_CONF)
        self.assertIn("1 sensor(s) changed", text)

    def test_busy_model_postpones(self):
        home.tick(T0)
        self.open_door()
        with mock.patch.object(home, "_model_busy", lambda: True):
            home.tick(T0 + 300)
        self.assertEqual(self.prompts, [])
        home.tick(T0 + 600)
        self.assertTrue(self.prompts)

    def test_unreachable_home_assistant_is_told_once_then_recovery(self):
        import urllib.error
        home.tick(T0)

        def down(path, timeout=15):
            raise urllib.error.URLError("refused")

        with mock.patch.object(home, "_get", down):
            for i in range(1, 12):  # 55 minutes of failures
                home.tick(T0 + i * 300)
        downs = [m for m, _ in self.sent if "can't reach Home Assistant" in m]
        self.assertEqual(len(downs), 1)
        home.tick(T0 + 12 * 300)
        self.assertTrue(any("reachable again" in m for m, _ in self.sent))


class Status(unittest.TestCase):
    def test_status_lists_values_and_offline(self):
        cur = home.snapshot(HOUSE + [ent("sensor.hall_humidity", "unavailable", "Hall humidity")], home.DEFAULT_CONF)
        text = home.status_text(cur)
        self.assertIn("• Living room: 21.5 °C", text)
        self.assertIn("Offline: Hall humidity", text)

    def test_empty(self):
        self.assertIn("No watched sensors", home.status_text({}))


if __name__ == "__main__":
    unittest.main()
