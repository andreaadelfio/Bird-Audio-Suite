from __future__ import annotations

import sys
import types
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np


fake_pyinaturalist = types.ModuleType("pyinaturalist")
fake_pyinaturalist.create_observation = lambda **kwargs: {}
fake_pyinaturalist.update_observation = lambda *args, **kwargs: {}
sys.modules.setdefault("pyinaturalist", fake_pyinaturalist)

from bird_audio_suite.audio import (
    aggregate_detections_by_species,
    apply_clip_span_policy,
    denoise_signal,
)
from bird_audio_suite.cli import build_parser
from bird_audio_suite.cli import choose_loudest_sounddevice_index
from bird_audio_suite.cli import select_sounddevice_index
from bird_audio_suite.species import SpeciesCatalog


class AudioTests(unittest.TestCase):
    def test_denoise_without_reference_preserves_in_band_signal(self) -> None:
        sample_rate = 16000
        samples = np.sin(2 * np.pi * 1000 * np.arange(sample_rate) / sample_rate).astype(np.float32)

        denoised = denoise_signal(samples, sample_rate)

        input_rms = float(np.sqrt(np.mean(samples**2)))
        output_rms = float(np.sqrt(np.mean(denoised**2)))
        self.assertGreater(output_rms, input_rms * 0.8)

    def test_clip_span_policy_can_cover_the_full_slice(self) -> None:
        grouped = {"Turdus merula": (2.0, 3.0, 0.9, "Blackbird")}

        adjusted = apply_clip_span_policy(grouped, 10.0, "full_slice")

        self.assertEqual(adjusted["Turdus merula"][:2], (0.0, 10.0))

    def test_detections_are_grouped_by_species(self) -> None:
        detections = [
            {
                "scientific_name": "Turdus merula",
                "common_name": "Blackbird",
                "start_time": 1.0,
                "end_time": 2.0,
                "confidence": 0.8,
            },
            {
                "scientific_name": "Turdus merula",
                "common_name": "Blackbird",
                "start_time": 4.0,
                "end_time": 5.0,
                "confidence": 0.95,
            },
        ]

        grouped = aggregate_detections_by_species(detections, 10.0, 0.7)

        self.assertEqual(grouped["Turdus merula"], (1.0, 5.0, 0.95, "Blackbird"))


class CliTests(unittest.TestCase):
    def test_list_devices_is_opt_in(self) -> None:
        parser = build_parser()

        self.assertFalse(parser.parse_args(["live"]).list_devices)
        self.assertTrue(parser.parse_args(["live", "--list-devices"]).list_devices)

    def test_auto_device_chooses_highest_rms(self) -> None:
        measurements = [
            {"index": 3, "rms": 0.02, "peak": 0.2},
            {"index": 7, "rms": 0.15, "peak": 0.3},
        ]

        self.assertEqual(choose_loudest_sounddevice_index(measurements), 7)

    def test_saved_device_is_probed_alone_when_signal_is_present(self) -> None:
        devices = [
            {"index": 13, "name": "Microphone", "samplerate": 48000},
            {"index": 16, "name": "Loopback", "samplerate": 48000},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "device.json"
            state_path.write_text(
                '{"index": 13, "name": "Microphone"}',
                encoding="utf-8",
            )
            with patch(
                "bird_audio_suite.cli.probe_sounddevice_inputs",
                return_value=[
                    {
                        "index": 13,
                        "name": "Microphone",
                        "rms": 0.001,
                        "peak": 0.01,
                        "samplerate": 48000,
                    }
                ],
            ) as probe:
                selected_index = select_sounddevice_index(
                    devices,
                    state_path=state_path,
                )

            self.assertEqual(selected_index, 13)
            probe.assert_called_once_with([devices[0]], 1.0)


class SpeciesCatalogTests(unittest.TestCase):
    def test_observed_days_use_observation_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog = SpeciesCatalog(Path(temporary_directory) / "species.csv")
            detection = {
                "scientific_name": "Turdus merula",
                "common_name": "Blackbird",
                "confidence": 0.9,
            }
            with patch.object(catalog, "lookup_italian_name", return_value="Merlo"), patch.object(
                catalog,
                "lookup_german_name",
                return_value="Amsel",
            ):
                catalog.ensure_species([detection], observed_at=datetime(2026, 1, 1))
                catalog.ensure_species([detection], observed_at=datetime(2026, 1, 3))

            record = catalog.records["Turdus merula"]
            self.assertEqual(record.first_seen_date, "2026-01-01")
            self.assertEqual(record.last_seen_date, "2026-01-03")
            self.assertEqual(record.observed_days, 2)
            self.assertAlmostEqual(record.daily_average, 2 / 3)


if __name__ == "__main__":
    unittest.main()
