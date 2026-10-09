from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bird_audio_suite import geolocation
from bird_audio_suite.geolocation import ResolvedLocation, resolve_location
from bird_audio_suite.health import HeartbeatWriter, find_problems, update_state


NOW = 1_000_000.0


def heartbeat(age: float = 10.0, max_rms: float = 0.002, recent_seconds: float = 600.0) -> dict:
    return {"updated_at": NOW - age, "max_rms_recent": max_rms, "recent_seconds": recent_seconds, "device": "pulse"}


class FindProblemsTests(unittest.TestCase):
    def test_healthy_service(self) -> None:
        self.assertEqual(find_problems(NOW, "active", heartbeat()), {})

    def test_service_down_or_crash_looping(self) -> None:
        self.assertIn("service", find_problems(NOW, "activating", heartbeat()))
        self.assertIn("stale", find_problems(NOW, "active", None))
        self.assertIn("stale", find_problems(NOW, "active", heartbeat(age=20 * 60)))

    def test_digital_silence_needs_ten_minutes_of_data(self) -> None:
        self.assertIn("silent", find_problems(NOW, "active", heartbeat(max_rms=0.00004)))
        self.assertEqual(find_problems(NOW, "active", heartbeat(max_rms=0.00004, recent_seconds=120)), {})
        # a quiet room is not silence
        self.assertEqual(find_problems(NOW, "active", heartbeat(max_rms=0.0003)), {})


class AlertStateTests(unittest.TestCase):
    def test_alerts_after_two_checks_once_then_reports_recovery(self) -> None:
        problem = {"stale": "no audio"}

        state, alerts, resolved = update_state({}, problem)
        self.assertEqual((alerts, resolved), ([], []))  # e.g. just resumed from suspend

        state, alerts, resolved = update_state(state, problem)
        self.assertEqual(alerts, [("stale", "no audio")])

        state, alerts, resolved = update_state(state, problem)
        self.assertEqual(alerts, [])  # no repeated notifications

        state, alerts, resolved = update_state(state, {})
        self.assertEqual(resolved, ["stale"])
        self.assertEqual(state, {"counts": {}, "notified": []})

    def test_a_single_bad_check_is_forgotten(self) -> None:
        state, _, _ = update_state({}, {"stale": "no audio"})
        state, _, _ = update_state(state, {})
        _, alerts, _ = update_state(state, {"stale": "no audio"})
        self.assertEqual(alerts, [])


class HeartbeatTests(unittest.TestCase):
    def test_keeps_the_loudest_recent_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            writer = HeartbeatWriter(path)
            writer.beat(0.01, device="pulse")
            writer.beat(0.0001, device="pulse")
            data = json.loads(path.read_text())

        self.assertEqual(data["last_rms"], 0.0001)
        self.assertEqual(data["max_rms_recent"], 0.01)
        self.assertEqual(data["device"], "pulse")


class LocationCacheTests(unittest.TestCase):
    def resolve(self, cache_path: Path) -> ResolvedLocation:
        return resolve_location(
            fallback_latitude=52.52,
            fallback_longitude=13.40,
            fallback_place_name="Berlin, Germany",
            cache_path=cache_path,
        )

    def test_offline_uses_the_last_position_seen_instead_of_the_config_default(self) -> None:
        online = ResolvedLocation(45.80, 13.53, "Monfalcone", "ipwhois", False)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=False):
            cache = Path(directory) / "location.json"
            with patch.object(geolocation, "_lookup_environment", return_value=None):
                with patch.object(geolocation, "_lookup_ipapi", return_value=online):
                    self.assertEqual(self.resolve(cache).place_name, "Monfalcone")
                with patch.object(geolocation, "_lookup_ipapi", return_value=None), patch.object(
                    geolocation, "_lookup_ipwhois", return_value=None
                ):
                    offline = self.resolve(cache)

        self.assertEqual((offline.latitude, offline.longitude, offline.place_name), (45.80, 13.53, "Monfalcone"))
        self.assertTrue(offline.used_fallback)
        self.assertIn("last known", offline.source)

    def test_first_run_offline_falls_back_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(geolocation, "_lookup_environment", return_value=None), patch.object(
                geolocation, "_lookup_ipapi", return_value=None
            ), patch.object(geolocation, "_lookup_ipwhois", return_value=None):
                location = self.resolve(Path(directory) / "missing.json")

        self.assertEqual(location.place_name, "Berlin, Germany")
        self.assertEqual(location.source, "config")


if __name__ == "__main__":
    unittest.main()
