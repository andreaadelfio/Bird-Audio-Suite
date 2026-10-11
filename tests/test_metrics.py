from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np

from bird_audio_suite.live import run_sliding_window_loop
from bird_audio_suite.metrics import LiveMetrics, memory_mb


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class LiveMetricsTests(unittest.TestCase):
    def test_nothing_to_show_before_the_first_window(self) -> None:
        metrics = LiveMetrics()
        self.assertEqual(metrics.snapshot(), {})
        self.assertEqual(metrics.summary_lines(), [])

    def test_load_is_analysis_time_over_the_time_available(self) -> None:
        clock = Clock()
        metrics = LiveMetrics(clock=clock)
        for analysis in (0.5, 0.7, 1.2):
            clock.now += 4.8
            metrics.record_window(analysis, 4.8, lag_seconds=0.3)
        metrics.record_echo(0.01, 1.0)
        data = metrics.snapshot()
        self.assertEqual((data["windows"], data["analysis_max_s"], data["lag_s"]), (3, 1.2, 0.3))
        self.assertAlmostEqual(data["load_pct"], 100 * 2.4 / 14.4, places=1)
        self.assertEqual(data["echo_pct"], 1.0)
        self.assertGreater(data["rss_mb"], 0)
        lines = metrics.summary_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("17% del tempo", lines[0])
        self.assertIn("sottrazione PC 1.0% CPU", lines[1])

    def test_only_the_last_ten_minutes_count(self) -> None:
        clock = Clock()
        metrics = LiveMetrics(clock=clock)
        metrics.record_window(9.0, 4.8, 30.0)  # a slow spell long ago
        clock.now += 11 * 60
        metrics.record_window(0.5, 4.8, 0.2)
        data = metrics.snapshot()
        self.assertEqual((data["windows"], data["analysis_max_s"], data["lag_max_s"]), (1, 0.5, 0.2))

    def test_memory_of_this_process(self) -> None:
        rss, peak = memory_mb()
        self.assertGreater(rss, 10)
        self.assertGreaterEqual(peak, rss)


class LoopTimingTests(unittest.TestCase):
    def test_the_loop_reports_each_window(self) -> None:
        class Tracker:
            def handle_result(self, *args):
                return []

            def flush(self):
                return []

        timings = []
        start = datetime(2026, 10, 11, 8, 0)
        ticks = iter(start + timedelta(seconds=0.1 * i) for i in range(1000))
        run_sliding_window_loop(
            (np.zeros(100, np.float32) for _ in range(8)),
            sample_rate=100,
            slice_frames=4,
            detect_window=lambda window: ({}, window),
            tracker=Tracker(),
            now=lambda: next(ticks),
            on_timing=lambda analysis, budget, lag: timings.append((analysis, budget, lag)),
        )
        self.assertEqual(len(timings), 3)  # 4-frame windows every 2 frames, from 8 frames
        analysis, budget, lag = timings[0]
        self.assertLess(analysis, 1.0)
        self.assertEqual(budget, 2.0)  # half of a 4 s window
        self.assertIsInstance(lag, float)


if __name__ == "__main__":
    unittest.main()
