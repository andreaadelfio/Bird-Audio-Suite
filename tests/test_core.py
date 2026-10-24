from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np



from bird_audio_suite.audio import (
    aggregate_detections_by_species,
    apply_clip_span_policy,
    denoise_signal,
)
from bird_audio_suite.cli import build_parser
from bird_audio_suite.cli import choose_loudest_sounddevice_index
from bird_audio_suite.cli import detect_live_slice
from bird_audio_suite.cli import select_sounddevice_index


class AudioTests(unittest.TestCase):
    def test_denoise_without_reference_preserves_in_band_signal(self) -> None:
        sample_rate = 16000
        samples = np.sin(2 * np.pi * 1000 * np.arange(sample_rate) / sample_rate).astype(np.float32)

        denoised = denoise_signal(samples, sample_rate)

        input_rms = float(np.sqrt(np.mean(samples**2)))
        output_rms = float(np.sqrt(np.mean(denoised**2)))
        self.assertGreater(output_rms, input_rms * 0.8)

    def test_the_noise_reference_is_read_once_for_every_window(self) -> None:
        from bird_audio_suite import audio

        sample_rate = 16000
        rng = np.random.default_rng(5)
        with tempfile.TemporaryDirectory() as directory:
            noise = Path(directory) / "noise.wav"
            audio.write_wav_mono(noise, 0.05 * rng.standard_normal(sample_rate), sample_rate)
            window = (0.05 * rng.standard_normal(sample_rate)).astype(np.float32)
            with patch.object(audio, "load_audio", wraps=audio.load_audio) as load:
                first = denoise_signal(window, sample_rate, noise_reference=noise)
                second = denoise_signal(window, sample_rate, noise_reference=noise)
        self.assertEqual(load.call_count, 1)
        np.testing.assert_array_equal(first, second)
        self.assertLess(float(np.std(first)), float(np.std(window)))  # the noise is taken out

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

    def test_playback_subtraction_is_on_by_default_but_can_be_disabled(self) -> None:
        parser = build_parser()

        self.assertTrue(parser.parse_args(["live"]).enable_playback_subtraction)
        self.assertFalse(parser.parse_args(["live", "--disable-playback-subtraction"]).enable_playback_subtraction)

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


class LiveSliceTests(unittest.TestCase):
    def make_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            disable_denoise=True,
            lat=45.0,
            lon=7.0,
            min_confidence=0.85,
            playback_suppression_min_confidence=0.5,
            verbose=False,
        )

    def test_species_detected_in_pc_playback_is_not_reported_live(self) -> None:
        class Detector:
            def detect_samples(self, samples, *args, **kwargs):
                species = "Turdus merula"
                return [
                    {
                        "scientific_name": species,
                        "common_name": "Blackbird",
                        "start_time": 0.0,
                        "end_time": 3.0,
                        "confidence": 0.95 if samples[0] < 0 else 0.9,
                    }
                ]

        grouped, _samples, segments = detect_live_slice(
            np.ones(48_000, dtype=np.float32),
            48_000,
            Detector(),
            self.make_args(),
            -np.ones(48_000, dtype=np.float32),
        )

        self.assertEqual(grouped, {})
        self.assertEqual(segments, {})

    def test_species_not_in_pc_playback_still_reports_live(self) -> None:
        class Detector:
            def detect_samples(self, samples, *args, **kwargs):
                species, common = ("Pica pica", "Magpie") if samples[0] < 0 else ("Turdus merula", "Blackbird")
                return [
                    {
                        "scientific_name": species,
                        "common_name": common,
                        "start_time": 0.0,
                        "end_time": 3.0,
                        "confidence": 0.95 if samples[0] < 0 else 0.9,
                    }
                ]

        grouped, _samples, segments = detect_live_slice(
            np.ones(48_000, dtype=np.float32),
            48_000,
            Detector(),
            self.make_args(),
            -np.ones(48_000, dtype=np.float32),
        )

        self.assertIn("Turdus merula", grouped)
        self.assertIn("Turdus merula", segments)


if __name__ == "__main__":
    unittest.main()
