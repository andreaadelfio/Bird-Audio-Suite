from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

import numpy as np

from bird_audio_suite.cli import subtract_playback_from_frames
from bird_audio_suite.echo import EchoCanceller, estimate_lag


class EchoCancellerTests(unittest.TestCase):
    def test_silent_reference_passes_microphone_with_fixed_latency(self) -> None:
        sample_rate = 48_000
        samples = (0.2 * np.sin(2 * np.pi * 1_000 * np.arange(sample_rate) / sample_rate)).astype(np.float32)
        canceller = EchoCanceller()

        output = np.concatenate(
            [
                canceller.process(block, np.zeros_like(block))
                for block in np.array_split(samples, 32)
            ]
        )

        delayed = output[canceller.latency:]
        original = samples[: delayed.size]
        self.assertLess(float(np.sqrt(np.mean((delayed - original) ** 2))), 1e-5)

    def test_matching_reference_is_subtracted_without_suppressing_unrelated_audio(self) -> None:
        sample_rate = 48_000
        t = np.arange(sample_rate * 2, dtype=np.float32) / sample_rate
        reference = (0.20 * np.sin(2 * np.pi * 900 * t) + 0.08 * np.sin(2 * np.pi * 2_200 * t)).astype(np.float32)
        bird = (0.03 * np.sin(2 * np.pi * 4_100 * t)).astype(np.float32)
        echo = 0.6 * np.concatenate([np.zeros(120, dtype=np.float32), reference[:-120]])
        mic = echo + bird
        canceller = EchoCanceller(step=0.2)

        output = np.concatenate(
            [
                canceller.process(mic[start:start + 1536], reference[start:start + 1536])
                for start in range(0, mic.size, 1536)
            ]
        )

        delayed_output = output[sample_rate + canceller.latency:]
        aligned_bird = bird[sample_rate:sample_rate + delayed_output.size]
        aligned_mic = mic[sample_rate:sample_rate + delayed_output.size]
        input_residual = float(np.sqrt(np.mean((aligned_mic - aligned_bird) ** 2)))
        output_residual = float(np.sqrt(np.mean((delayed_output - aligned_bird) ** 2)))
        self.assertLess(output_residual, input_residual * 0.25)

    def test_lag_estimation_finds_reference_leading_microphone(self) -> None:
        rng = np.random.default_rng(12)
        reference = rng.normal(0, 0.05, 8_000).astype(np.float32)
        lag = 137
        mic = np.concatenate([np.zeros(lag, dtype=np.float32), reference[:-lag]])

        estimated, sharpness = estimate_lag(mic, reference, max_lag=500)

        self.assertEqual(estimated, lag)
        self.assertGreater(sharpness, 8.0)


class PlaybackSubtractionFrameTests(unittest.TestCase):
    def test_unavailable_monitor_leaves_frames_unchanged(self) -> None:
        frames = [np.array([0.1, -0.2], dtype=np.float32)]
        args = argparse.Namespace(enable_playback_subtraction=True, verbose=False)

        with patch("bird_audio_suite.cli.PlaybackMonitor.available", return_value=False):
            output = list(subtract_playback_from_frames(frames, 48_000, args))

        self.assertEqual(len(output), 1)
        np.testing.assert_array_equal(output[0], frames[0])


if __name__ == "__main__":
    unittest.main()
