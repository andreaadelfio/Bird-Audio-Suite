from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile

from bird_audio_suite.cloud import encode_mp3, listening_gain
from bird_audio_suite.inputgain import SourceVolumeCompensation, parse_volume_db

PACTL = ("Volume: front-left: 29555 /  45% / -20.75 dB,   front-right: 29555 /  45% / -20.75 dB,   "
         "rear-left: 29555 /  45% / -20.75 dB,   rear-right: 29555 /  45% / -20.75 dB\n        balance 0.00")


def db(samples) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.asarray(samples, np.float64) ** 2))))


class SystemVolumeTests(unittest.TestCase):
    def test_reads_pactl_volume(self) -> None:
        self.assertAlmostEqual(parse_volume_db(PACTL), -20.75)
        self.assertEqual(parse_volume_db("Volume: front-left: 0 / 0% / -inf dB"), float("-inf"))
        self.assertIsNone(parse_volume_db("Connection failure"))

    def test_a_turned_down_microphone_is_brought_back_to_its_level(self) -> None:
        compensation = SourceVolumeCompensation(read_volume=lambda: -20.75, start=False)
        quiet = np.full(1000, 0.01, np.float32)
        out = compensation.apply(quiet)
        out = compensation.apply(quiet)  # after the first ramp
        self.assertAlmostEqual(db(out) - db(quiet), 20.75, places=2)

    def test_volume_changes_ramp_without_a_jump(self) -> None:
        volume = {"db": 0.0}
        compensation = SourceVolumeCompensation(read_volume=lambda: volume["db"], start=False)
        ones = np.ones(480, np.float32)
        np.testing.assert_array_equal(compensation.apply(ones), ones)
        volume["db"] = -12.0
        compensation.refresh()
        ramp = compensation.apply(ones)
        self.assertAlmostEqual(float(ramp[0]), 1.0, places=5)
        self.assertAlmostEqual(float(ramp[-1]), 10 ** (12 / 20), places=4)
        self.assertTrue(np.all(np.diff(ramp) >= 0))

    def test_muted_or_unknown_or_zero_leaves_the_signal_alone_and_gain_is_capped(self) -> None:
        for volume in (None, float("-inf")):
            compensation = SourceVolumeCompensation(read_volume=lambda volume=volume: volume, start=False)
            self.assertEqual(compensation.target_gain, 1.0)
        nearly_off = SourceVolumeCompensation(read_volume=lambda: -60.0, start=False)
        self.assertAlmostEqual(20 * np.log10(nearly_off.target_gain), 30.0, places=4)


class ListeningLevelTests(unittest.TestCase):
    def test_a_far_bird_is_brought_to_a_comfortable_level(self) -> None:
        sr = 48000
        t = np.arange(3 * sr) / sr
        far_bird = (0.01 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32)  # peak -40 dBFS
        self.assertAlmostEqual(20 * np.log10(listening_gain(far_bird)), 30.0, places=4)  # +39 needed, capped
        self.assertAlmostEqual(20 * np.log10(listening_gain(far_bird * 10)), 19.0, places=2)
        self.assertEqual(listening_gain(np.zeros(10, np.float32)), 1.0)
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "clip.wav"
            soundfile.write(wav, far_bird * 10, sr)  # peak -20 dBFS
            mp3 = encode_mp3(wav, Path(directory) / "clip.mp3")
            heard, _ = soundfile.read(mp3, dtype="float32")
            raw = encode_mp3(wav, Path(directory) / "raw.mp3", normalize=False)
            kept, _ = soundfile.read(raw, dtype="float32")
        self.assertAlmostEqual(20 * np.log10(np.abs(heard).max()), -1.0, delta=0.5)
        self.assertAlmostEqual(20 * np.log10(np.abs(kept).max()), -20.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
