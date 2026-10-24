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

    def test_live_falling_behind_or_growing_too_big(self) -> None:
        keeping_up = {**heartbeat(), "lag_s": 0.4, "analysis_avg_s": 0.6, "load_pct": 13, "rss_mb": 320}
        self.assertEqual(find_problems(NOW, "active", keeping_up), {})
        behind = find_problems(NOW, "active", {**keeping_up, "lag_s": 95, "analysis_avg_s": 6.1, "load_pct": 127})
        self.assertEqual(list(behind), ["slow"])
        self.assertIn("95 s", behind["slow"])
        self.assertEqual(list(find_problems(NOW, "active", {**keeping_up, "rss_mb": 1500})), ["memory"])
        # keeping up only by skipping windows is falling behind too; one now and then is not
        self.assertEqual(find_problems(NOW, "active", {**keeping_up, "skipped": 2}), {})
        skipping = find_problems(NOW, "active", {**keeping_up, "skipped": 40, "load_pct": 180})
        self.assertEqual(list(skipping), ["slow"])
        self.assertIn("saltato 40 finestre", skipping["slow"])


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


    def test_a_connection_cut_halfway_or_a_strange_answer_is_just_no_answer(self) -> None:
        class Reset:
            def __enter__(self):
                raise ConnectionResetError(104, "Connection reset by peer")  # not a URLError

            def __exit__(self, *args):
                return False

        with patch.object(geolocation, "urlopen", lambda request, timeout: Reset()):
            self.assertIsNone(geolocation._fetch_json("https://ipapi.co/json/", 1))
        with patch.object(geolocation, "_fetch_json", return_value={"error": True, "reason": "RateLimited"}):
            self.assertIsNone(geolocation._lookup_ipapi(1))
        with patch.object(geolocation, "_fetch_json", return_value={"latitude": "n/a", "longitude": 13.5, "city": "X"}):
            self.assertIsNone(geolocation._lookup_ipapi(1))
        with patch.object(geolocation, "_fetch_json", return_value={"success": True, "latitude": 95.0, "longitude": 13.5,
                                                                   "city": "X", "connection": None}):
            self.assertIsNone(geolocation._lookup_ipwhois(1))
        with patch.object(geolocation, "_fetch_json", return_value={"latitude": "45.8", "longitude": "13.53",
                                                                   "city": "Monfalcone", "country_name": "Italy"}):
            self.assertEqual(geolocation._lookup_ipapi(1).latitude, 45.8)

    def test_the_last_position_is_saved_even_without_its_folder(self) -> None:
        online = ResolvedLocation(45.80, 13.53, "Monfalcone", "ipwhois", False)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "runtime" / "location.json"
            with patch.object(geolocation, "_lookup_environment", return_value=None), \
                    patch.object(geolocation, "_lookup_ipapi", return_value=online):
                self.resolve(cache)
            self.assertTrue(cache.exists())

if __name__ == "__main__":
    unittest.main()
