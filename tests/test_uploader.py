from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from bird_audio_suite.health import find_upload_problems
from bird_audio_suite.uploader import UploaderStatus, read_status, run_uploader, status_line


def write_clip(folder: Path, name: str, recorded_at: str = "2026-10-11T08:00:00+02:00") -> None:
    (folder / f"{name}.wav").write_bytes(b"RIFF")
    (folder / f"{name}.json").write_text(json.dumps({"id": f"{name}.wav", "recorded_at": recorded_at}))


class FakeOutbox:
    """Sends (deletes) what is in the folder, or fails while offline."""

    def __init__(self, folder: Path) -> None:
        self.folder, self.pushes, self.offline, self.busy = folder, 0, False, False
        self.purges = 0

    def purge_uploaded_media(self) -> int:
        self.purges += 1
        return 0

    def push(self):
        self.pushes += 1
        if self.busy:
            return types.SimpleNamespace(busy=True, sent=0, errors=[])
        if self.offline:
            return types.SimpleNamespace(busy=False, sent=0, errors=["offline"])
        sent = 0
        for sidecar in self.folder.glob("*.json"):
            sidecar.unlink()
            sidecar.with_suffix(".wav").unlink()
            sent += 1
        return types.SimpleNamespace(busy=False, sent=sent, errors=[])


class Loop:
    """Drives run_uploader step by step: `script[i]` runs before the i-th sleep."""

    def __init__(self, script) -> None:
        self.script, self.step, self.now = list(script), 0, 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.step < len(self.script):
            self.script[self.step]()
        self.step += 1

    def stop(self) -> bool:
        return self.step > len(self.script)


class UploaderLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        # the status lives outside the transit folder, as in the service (runtime folder)
        self.runtime = tempfile.TemporaryDirectory()
        self.status_path = Path(self.runtime.name) / "uploader.json"

    def tearDown(self) -> None:
        self.runtime.cleanup()

    def run_loop(self, folder: Path, outbox: FakeOutbox, script) -> dict | None:
        loop = Loop(script)
        run_uploader(folder, lambda: outbox, poll_seconds=3, retry_seconds=60,
                     status=UploaderStatus(self.status_path), log=lambda message: None,
                     should_stop=loop.stop, sleep=loop.sleep, clock=lambda: loop.now)
        return read_status(self.status_path)

    def test_a_new_clip_is_sent_within_a_poll_and_nothing_is_redone_while_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            outbox = FakeOutbox(folder)
            idle = [lambda: None] * 5
            status = self.run_loop(folder, outbox, [*idle, lambda: write_clip(folder, "Merlo_1"), *idle])
            left = list(folder.glob("*.wav"))

        # start, the new clip, the folder emptied by that push; not the idle polls
        self.assertEqual(outbox.pushes, 3)
        self.assertEqual((left, status["pending"], status["sent_total"]), ([], 0, 1))

    def test_offline_clips_are_retried_every_minute_not_every_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_clip(folder, "Merlo_1")
            outbox = FakeOutbox(folder)
            outbox.offline = True
            status = self.run_loop(folder, outbox, [lambda: None] * 45)  # 45 polls = 135 s

        self.assertEqual(outbox.pushes, 3)  # at 0, 60 and 120 s
        self.assertEqual(outbox.purges, 1)  # once an hour
        self.assertEqual((status["pending"], status["last_error"]), (1, "offline"))

    def test_a_busy_folder_is_looked_at_again_after_a_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_clip(folder, "Merlo_1")
            outbox = FakeOutbox(folder)
            outbox.busy = True

            def free() -> None:
                outbox.busy = False

            self.run_loop(folder, outbox, [lambda: None, free, lambda: None])
            left = list(folder.glob("*.wav"))

        self.assertEqual(left, [])
        self.assertLessEqual(outbox.pushes, 4)  # one per poll at most: it never spins


class StatusTests(unittest.TestCase):
    def test_the_line_under_the_live_table(self) -> None:
        status = {"updated_at": 1000.0, "pending": 2, "last_sent_at": None, "seconds_per_clip": 2.34,
                  "last_error": "ConnectionError"}
        self.assertEqual(status_line(status, now=1010.0),
                         "Invio a Supabase: 2 in attesa · 2.3 s/clip · errore: ConnectionError")
        self.assertIn("non risponde", status_line(status, now=1000.0 + 600))
        self.assertIn("non risponde", status_line(None))

    def test_health_check_of_the_uploader(self) -> None:
        now = 1_800_000_000.0
        fine = {"updated_at": now - 30, "pending": 0, "oldest_pending": None}
        self.assertEqual(find_upload_problems(now, fine), {})
        self.assertIn("uploader", find_upload_problems(now, None))
        self.assertIn("uploader", find_upload_problems(now, {**fine, "updated_at": now - 3600}))
        from datetime import datetime, timezone

        three_hours_ago = datetime.fromtimestamp(now - 3 * 3600, timezone.utc).isoformat()
        stuck = find_upload_problems(now, {**fine, "pending": 4, "oldest_pending": three_hours_ago,
                                           "last_error": "HTTP 500"})
        self.assertEqual(list(stuck), ["stuck"])
        self.assertIn("HTTP 500", stuck["stuck"])


if __name__ == "__main__":
    unittest.main()
