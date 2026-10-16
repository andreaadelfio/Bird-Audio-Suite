from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bird_audio_suite import cli, config
from bird_audio_suite.config_watch import ConfigWatcher


class ConfigWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.py"
        self.path.write_text("DEFAULT_LIVE_MIN_CONFIDENCE = 0.7\nLIKELY_CONFIDENCE = 0.8\nDEFAULT_SLICE_INTERVAL = 300\n")
        self.saved = {name: getattr(config, name) for name in dir(config) if name.isupper()}
        # known starting values, whatever the real config.py says today
        config.DEFAULT_LIVE_MIN_CONFIDENCE, config.LIKELY_CONFIDENCE, config.DEFAULT_SLICE_INTERVAL = 0.7, 0.8, 300
        self.now, self.lines = 0.0, []

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(config, name, value)
        self.directory.cleanup()

    def watcher(self) -> ConfigWatcher:
        return ConfigWatcher({"DEFAULT_LIVE_MIN_CONFIDENCE", "LIKELY_CONFIDENCE"}, path=self.path,
                             log=self.lines.append, clock=lambda: self.now)

    def edit(self, text: str) -> None:
        self.path.write_text(text)
        stat = self.path.stat()
        # a later modification time, as a real save a moment after
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.now += 5

    def reload_from(self, path: Path):
        # the module is re-run from the test file instead of the real config.py
        code = compile(path.read_text(), str(path), "exec")
        exec(code, vars(config))

    def test_a_change_is_taken_without_restarting_and_logged(self) -> None:
        watcher = self.watcher()
        self.assertEqual(watcher.poll(), {})  # nothing yet, and no re-read while unchanged
        self.edit("DEFAULT_LIVE_MIN_CONFIDENCE = 0.8\nLIKELY_CONFIDENCE = 0.9\nDEFAULT_SLICE_INTERVAL = 200\n")
        with patch("importlib.reload", side_effect=lambda module: self.reload_from(self.path)):
            changes = watcher.poll()
        self.assertEqual(changes["DEFAULT_LIVE_MIN_CONFIDENCE"], (0.7, 0.8))
        self.assertEqual(config.LIKELY_CONFIDENCE, 0.9)
        self.assertTrue(any("DEFAULT_SLICE_INTERVAL" in line and "riavvio" in line for line in self.lines))

        args = SimpleNamespace(min_confidence=0.7, playback_suppression_min_confidence=0.33)
        cli.apply_config_changes(args, changes)
        self.assertEqual(args.min_confidence, 0.8)
        self.assertEqual(args.playback_suppression_min_confidence, 0.33)  # given on the command line: stays

    def test_a_broken_file_keeps_the_values_in_use(self) -> None:
        watcher = self.watcher()
        before = config.LIKELY_CONFIDENCE
        self.edit("LIKELY_CONFIDENCE = 0.95\nDEFAULT_LIVE_MIN_CONFIDENCE = (\n")
        with patch("importlib.reload", side_effect=lambda module: self.reload_from(self.path)):
            self.assertEqual(watcher.poll(), {})
        self.assertEqual(config.LIKELY_CONFIDENCE, before)
        self.assertIn("restano i valori di prima", self.lines[-1])

    def test_it_looks_at_the_file_only_every_few_seconds(self) -> None:
        watcher = self.watcher()
        with patch.object(Path, "stat", wraps=self.path.stat) as stat:
            for _ in range(10):
                self.now += 0.5
                watcher.poll()
        self.assertEqual(stat.call_count, 1)  # 5 s in half-second steps: one look


if __name__ == "__main__":
    unittest.main()
