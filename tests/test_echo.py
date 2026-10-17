from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

import numpy as np

from bird_audio_suite.cli import subtract_playback_from_frames
from bird_audio_suite import echo
from bird_audio_suite.echo import EchoCanceller, PlaybackMonitor, PlaybackSubtractor, estimate_lag


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

    def test_skipping_ffts_while_the_pc_is_silent_changes_nothing(self) -> None:
        rng = np.random.default_rng(7)
        mic = rng.standard_normal(48_000 * 3).astype(np.float32) * 0.1
        reference = np.zeros_like(mic)
        reference[48_000:96_000] = rng.standard_normal(48_000).astype(np.float32) * 0.1  # plays for 1 s
        mic[48_000:96_000] += 0.5 * reference[48_000:96_000]
        fast, full = EchoCanceller(), EchoCanceller()
        full.skip_silence = False
        blocks = [slice(start, start + 1536) for start in range(0, mic.size, 1536)]
        out_fast = np.concatenate([fast.process(mic[part], reference[part]) for part in blocks])
        out_full = np.concatenate([full.process(mic[part], reference[part]) for part in blocks])
        np.testing.assert_allclose(out_fast, out_full, atol=1e-5)

    def test_a_video_call_is_removed_and_never_blows_up(self) -> None:
        # band-limited voice with pauses (no energy above 3.4 kHz, silent spells), heard by the mic
        # with wideband room noise: the weights used to grow without bound and the output got 30 dB
        # louder than the mic
        from scipy.signal import butter, sosfilt

        rate, seconds = 48_000, 20
        rng = np.random.default_rng(0)
        n = rate * seconds
        voice = sosfilt(butter(6, [300, 3400], btype="band", fs=rate, output="sos"), rng.standard_normal(n))
        talking = np.repeat(rng.random(seconds // 2 + 1) > 0.35, 2 * rate)[:n]
        reference = (0.2 * voice * talking).astype(np.float32)
        room = (0.01 * rng.standard_normal(n)).astype(np.float32)
        mic = room.copy()
        mic[200:] += 0.15 * reference[:-200]
        canceller = EchoCanceller()
        out = np.concatenate([canceller.process(mic[i:i + 1536], reference[i:i + 1536])
                              for i in range(0, n, 1536)])[canceller.latency:]
        tail = slice(10 * rate, n - canceller.latency)
        power = lambda x: float(np.mean(np.asarray(x[tail], np.float64) ** 2))
        self.assertLess(power(out), power(mic))  # never louder than what the mic heard
        self.assertLess(10 * np.log10(power(out) / power(room[:out.size])), 1.0)  # the call is gone
        self.assertLess(float(np.abs(canceller.weights).max()), 1.0)

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

    def test_a_pc_louder_at_the_mic_than_its_reference_is_still_removed(self) -> None:
        # the speakers can reach the mic louder than the digital reference: a guard that reset the
        # filter at every frame coming out louder than the mic kept the PC only ~7 dB lower
        from scipy.signal import butter, sosfilt

        rate, seconds = 48_000, 10
        rng = np.random.default_rng(1)
        n = rate * seconds
        voice = sosfilt(butter(6, [200, 4000], btype="band", fs=rate, output="sos"), rng.standard_normal(n))
        syllables = (np.sin(2 * np.pi * 3 * np.arange(n) / rate) > -0.3).astype(float)
        reference = (0.1 * voice * syllables).astype(np.float32)
        room = (0.003 * rng.standard_normal(n)).astype(np.float32)
        for gain in (1.5, 3.0):  # the mic hears the PC louder than its reference
            pc = np.zeros(n)
            pc[192:] = gain * reference[:-192]
            pc[576:] += 0.3 * gain * reference[:-576]  # a reflection
            mic = (pc + room).astype(np.float32)
            canceller = EchoCanceller()
            out = np.concatenate([canceller.process(mic[i:i + 1536], reference[i:i + 1536])
                                  for i in range(0, n, 1536)])[canceller.latency:]
            tail = slice(5 * rate, out.size)  # once it has learnt
            removed = 10 * np.log10(np.mean(pc[tail] ** 2) / np.mean((out[tail] - room[tail]) ** 2))
            self.assertGreater(removed, 20.0, f"x{gain}")
            self.assertLess(float(np.mean(out[tail] ** 2)), float(np.mean(mic[tail] ** 2)))

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


    def test_the_system_mic_volume_is_compensated_after_the_subtraction_on_the_mic_only(self) -> None:
        from bird_audio_suite.cli import compensate_system_volume

        class Compensation:
            def __init__(self, log=None) -> None:
                pass

            @staticmethod
            def available() -> bool:
                return True

            def apply(self, frame):
                return frame * 6.8  # the mic at -16.6 dB of system volume

            def stop(self) -> None:
                pass

        subtracted = [(np.full(4, 0.1, np.float32), np.full(4, 0.5, np.float32))]  # (mic, PC audio)
        with patch("bird_audio_suite.cli.SourceVolumeCompensation", Compensation):
            (mic, pc), = list(compensate_system_volume(subtracted, "pulse", argparse.Namespace()))
        np.testing.assert_allclose(mic, 0.68)
        np.testing.assert_array_equal(pc, 0.5)


class PassiveMonitorTests(unittest.TestCase):
    def test_monitor_capture_does_not_keep_the_output_awake(self) -> None:
        with patch("bird_audio_suite.echo.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
            command = PlaybackMonitor.command(48_000)
        self.assertEqual(command[0], "pw-record")
        self.assertIn("node.passive = true", " ".join(command))

    def test_playback_after_a_pause_counts_as_new(self) -> None:
        monitor = PlaybackMonitor(48_000, keep_seconds=1.0)
        with patch("bird_audio_suite.echo.time.monotonic", return_value=100.0):
            monitor._append(np.ones(480, np.float32))
        with patch("bird_audio_suite.echo.time.monotonic", return_value=100.01):
            monitor._append(np.ones(480, np.float32))
        self.assertEqual(monitor.position()[0::2], (960, 1))
        with patch("bird_audio_suite.echo.time.monotonic", return_value=100.01 + echo.GAP_SECONDS + 1):
            monitor._append(np.full(480, 0.5, np.float32))
        self.assertEqual(monitor.position()[0::2], (480, 2))
        np.testing.assert_array_equal(monitor.read(0, 480), np.full(480, 0.5, np.float32))

    def test_after_a_pause_the_pc_is_lined_up_within_seconds(self) -> None:
        # 15:56 of 16 October: the PC played again after an hour; the first measure used the
        # silence before it (correlation 6, "headphones?") and the next came 10 s later
        rate = 48_000
        rng = np.random.default_rng(3)
        silence, playing = 20 * rate, 8 * rate
        reference = np.zeros(silence + playing, np.float32)
        reference[silence:] = 0.1 * rng.standard_normal(playing)
        delay = 2400  # 50 ms from the speakers to the mic
        mic = (0.002 * rng.standard_normal(reference.size)).astype(np.float32)
        mic[delay:] += 0.8 * reference[:-delay]

        class Output:
            def position(self):
                return reference.size, 1.0, 1

            def flowing(self):
                return True

            def read(self, start, length):
                out = np.zeros(length, np.float32)
                begin, end = max(start, 0), min(start + length, reference.size)
                if end > begin:
                    out[begin - start:end - start] = reference[begin:end]
                return out

            def stop(self):
                pass

        subtractor = PlaybackSubtractor(rate, monitor=Output())
        subtractor.generation = 1
        subtractor.offset = -delay + subtractor.margin + 4800  # the rough guess, 100 ms off
        lined_up_at = None
        for start in range(0, reference.size, 1536):
            subtractor.process_with_reference(mic[start:start + 1536], arrived_at=0.0)
            if lined_up_at is None and subtractor.heard_by_mic and subtractor.offset == -delay + subtractor.margin:
                lined_up_at = (start - silence) / rate
        self.assertIsNotNone(lined_up_at)
        self.assertLess(lined_up_at, 3.0)  # 2 s of playback to measure, not the 10 s of before

    def test_silent_pc_does_not_slow_the_microphone_down(self) -> None:
        class SleepingOutput:
            def position(self):
                return 4800, 1.0, 1  # data came long ago: the output is asleep

            def flowing(self):
                return False

            def read(self, start, length):
                raise AssertionError("nothing to read while the PC is silent")

            def stop(self):
                pass

        subtractor = PlaybackSubtractor(48_000, monitor=SleepingOutput())
        with patch("bird_audio_suite.echo.time.sleep", side_effect=AssertionError("waited")):
            out, reference = subtractor.process_with_reference(np.ones(512, np.float32), arrived_at=50.0)
        self.assertEqual(out.size, 512)
        self.assertFalse(reference.any())


if __name__ == "__main__":
    unittest.main()
