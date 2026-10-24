from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import scipy.io.wavfile as wavfile

from bird_audio_suite import cli
from bird_audio_suite.audio import (
    clip_relative_segments,
    merge_segments,
    export_detection_clips,
    parse_timestamp_from_name,
)
from bird_audio_suite.live import LiveDetectionTracker, run_sliding_window_loop
from bird_audio_suite.species import SpeciesNames


SAMPLE_RATE = 1000


class FakeCatalog:
    path = Path("species.csv")

    def display_names(self, scientific_name: str, fallback_english: str = "") -> tuple[str, str, str]:
        return {"Turdus merula": ("Merlo", "Amsel", "Blackbird")}.get(
            scientific_name, (scientific_name, scientific_name, fallback_english)
        )


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_row_kwargs(**overrides) -> dict:
    kwargs = dict(
        taxon_name="Turdus merula",
        english_name="Blackbird",
        italian_name="Merlo",
        german_name="Amsel",
        confidence=0.9,
        place_name="Somewhere",
        latitude=45.0,
        longitude=7.0,
    )
    kwargs.update(overrides)
    return kwargs


class TimestampParsingTests(unittest.TestCase):
    def test_recorder_and_clip_names(self) -> None:
        self.assertEqual(parse_timestamp_from_name("20260601_063000.WAV"), datetime(2026, 6, 1, 6, 30))
        self.assertEqual(parse_timestamp_from_name("SMA01234_20260601_063000.wav"), datetime(2026, 6, 1, 6, 30))
        self.assertEqual(
            parse_timestamp_from_name("Allocco Comune_20261004_232648_0.912.wav"),
            datetime(2026, 10, 4, 23, 26, 48),
        )
        self.assertEqual(parse_timestamp_from_name("2026-06-01 06-30-15.m4a"), datetime(2026, 6, 1, 6, 30, 15))

    def test_names_without_valid_dates(self) -> None:
        self.assertIsNone(parse_timestamp_from_name("merlo.wav"))
        self.assertIsNone(parse_timestamp_from_name("20261399_250000.wav"))

    def test_batch_start_falls_back_to_mtime_minus_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merlo.wav"
            path.write_bytes(b"")
            end = datetime(2026, 5, 1, 7, 0, 0)
            os.utime(path, (end.timestamp(), end.timestamp()))

            started_at, source = cli.infer_recording_start(path, 60.0)

        self.assertEqual(started_at, datetime(2026, 5, 1, 6, 59, 0))
        self.assertEqual(source, "file modification time")


class SegmentTests(unittest.TestCase):
    def test_overlapping_bands_merge_and_keep_the_best_confidence(self) -> None:
        self.assertEqual(merge_segments([(3.0, 6.0, 0.7), (0.0, 3.0, 0.8), (5.0, 8.0, 0.9), (12.0, 15.0, 0.6)]),
                         [(0.0, 8.0, 0.9), (12.0, 15.0, 0.6)])

    def test_bands_are_cut_to_the_clip(self) -> None:
        self.assertEqual(clip_relative_segments([(1.0, 4.0, 0.8), (9.0, 12.0, 0.7), (20.0, 23.0, 0.9)], 3.0, 10.0),
                         [[0.0, 1.0, 0.8], [6.0, 7.0, 0.7]])


class ExportTimeTests(unittest.TestCase):
    def test_each_species_is_dated_when_it_was_heard_across_midnight(self) -> None:
        samples = np.zeros(30 * SAMPLE_RATE, dtype=np.float32)
        grouped = {
            "Turdus merula": (5.0, 8.0, 0.9, "Blackbird"),
            "Strix aluco": (15.0, 18.0, 0.8, "Tawny Owl"),
        }
        with tempfile.TemporaryDirectory() as directory:
            exported = export_detection_clips(
                samples, SAMPLE_RATE, grouped, FakeCatalog(), Path(directory),
                observed_at=datetime(2026, 9, 1, 23, 59, 50),
            )
            times = {json.loads(path.with_suffix(".json").read_text())["scientific_name"]:
                     json.loads(path.with_suffix(".json").read_text())["recorded_at"][:19] for path in exported}
            leftovers = list(Path(directory).glob("*.part"))

        self.assertEqual(leftovers, [])  # each WAV and JSON written aside, then renamed
        self.assertEqual(sorted(path.name for path in exported),
                         ["Merlo_20260901_235955_0.900.wav", "Strix aluco_20260902_000005_0.800.wav"])
        self.assertEqual(times, {"Turdus merula": "2026-09-01T23:59:55", "Strix aluco": "2026-09-02T00:00:05"})


class LiveTrackerTests(unittest.TestCase):
    def make_tracker(self, root: Path, observations: list, **kwargs) -> LiveDetectionTracker:
        return LiveDetectionTracker(
            sample_rate=SAMPLE_RATE,
            detections_dir=root,
            species_names=FakeCatalog(),
            record_observation=lambda *args: observations.append(args),
            export_kwargs={},
            **kwargs,
        )

    def test_overlapping_windows_become_one_clip_without_duplicated_audio(self) -> None:
        window = 10 * SAMPLE_RATE
        step = window // 2
        start = datetime(2026, 9, 1, 12, 0, 0)
        detection = {"Turdus merula": (2.0, 5.0, 0.8, "Blackbird")}
        observations: list = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracker = self.make_tracker(root, observations)
            exported: list[Path] = []
            for index, grouped in enumerate([detection, detection, {}, {}, {}]):
                offset = index * step
                exported += tracker.handle_result(
                    grouped,
                    np.zeros(window, dtype=np.float32),
                    offset,
                    start + timedelta(seconds=offset / SAMPLE_RATE),
                )

            self.assertEqual(len(exported), 1)
            clip_seconds = len(wavfile.read(exported[0])[1]) / SAMPLE_RATE
            # 3s of pre-roll before the detection at 2s, clamped to the window start
            self.assertEqual(exported[0].name, "Merlo_20260901_120000_0.800.wav")

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0][3], datetime(2026, 9, 1, 12, 0, 2))
        # from the window start to the end of the second window (15s)
        self.assertAlmostEqual(clip_seconds, 15.0)

    def test_a_failing_bookkeeping_never_costs_the_clip(self) -> None:
        # the live table used to be printed inside record_observation: when it failed (an empty
        # observation on Supabase) the clip of the new species was never opened
        def broken(*args):
            raise ValueError("Invalid isoformat string: 'None'")

        window = 10 * SAMPLE_RATE
        start = datetime(2026, 10, 16, 8, 47, 0)
        with tempfile.TemporaryDirectory() as directory:
            tracker = LiveDetectionTracker(sample_rate=SAMPLE_RATE, detections_dir=Path(directory),
                                           species_names=FakeCatalog(), record_observation=broken, export_kwargs={})
            exported: list[Path] = []
            for index, grouped in enumerate([{"Columba livia": (2.0, 5.0, 0.72, "Rock Pigeon")}, {}, {}, {}]):
                offset = index * window // 2
                exported += tracker.handle_result(grouped, np.zeros(window, dtype=np.float32), offset,
                                                  start + timedelta(seconds=offset / SAMPLE_RATE))
            self.assertEqual(len(exported), 1)

    def test_short_isolated_detection_gets_pre_roll_and_minimum_length(self) -> None:
        window = 10 * SAMPLE_RATE
        step = window // 2
        start = datetime(2026, 9, 1, 12, 0, 0)
        observations: list = []
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(Path(directory), observations)
            exported: list[Path] = []
            results = [{"Phylloscopus collybita": (8.0, 9.0, 0.86, "Chiffchaff")}, {}, {}, {}]
            for index, grouped in enumerate(results):
                offset = index * step
                exported += tracker.handle_result(
                    grouped,
                    np.arange(offset, offset + window, dtype=np.float32) / 1e6,
                    offset,
                    start + timedelta(seconds=offset / SAMPLE_RATE),
                )

            self.assertEqual(len(exported), 1)
            clip = wavfile.read(exported[0])[1]
            self.assertEqual(exported[0].name, "Phylloscopus collybita_20260901_120005_0.860.wav")

        # starts 3s before the detection (8s -> 5s) and is extended to 10s with the following audio
        self.assertAlmostEqual(len(clip) / SAMPLE_RATE, 10.0)

    def test_birdnet_bands_are_stored_relative_to_the_clip(self) -> None:
        window = 10 * SAMPLE_RATE
        step = window // 2
        start = datetime(2026, 9, 1, 12, 0, 0)
        observations: list = []
        # window 1: heard at 6-9 s; window 2 (starts at 5 s): the same call at 1-4 s, plus a weaker
        # one at 7-10 s; window 3 (starts at 10 s): only a weak band at 0.5-1.5 s (below the clip
        # threshold, but the clip is open)
        results = [
            ({"Turdus merula": (6.0, 9.0, 0.8, "Blackbird")}, {"Turdus merula": [(6.0, 9.0, 0.8)]}),
            ({"Turdus merula": (1.0, 4.0, 0.9, "Blackbird")}, {"Turdus merula": [(1.0, 4.0, 0.9), (7.0, 10.0, 0.55)]}),
            ({}, {"Turdus merula": [(0.5, 1.5, 0.6)]}),
            ({}, {}),
            ({}, {}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(Path(directory), observations)
            exported: list[Path] = []
            for index, (grouped, bands) in enumerate(results):
                offset = index * step
                exported += tracker.handle_result(grouped, np.zeros(window, dtype=np.float32), offset,
                                                  start + timedelta(seconds=offset / SAMPLE_RATE), bands)
            data = json.loads(exported[0].with_suffix(".json").read_text())

        # the clip runs 3-15 s of the stream (pre-roll before 6 s): 6-9 twice -> 3-6 s with the best
        # 0.9, 10.5-11.5 -> 7.5-8.5, 12-15 -> 9-12
        self.assertEqual(data["segments"], [[3.0, 6.0, 0.9], [7.5, 8.5, 0.6], [9.0, 12.0, 0.55]])

    def test_open_clips_are_flushed_on_shutdown(self) -> None:
        observations: list = []
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(Path(directory), observations)
            tracker.handle_result(
                {"Turdus merula": (0.0, 3.0, 0.9, "Blackbird")},
                np.zeros(5 * SAMPLE_RATE, dtype=np.float32),
                0,
                datetime(2026, 9, 1, 8),
            )
            self.assertEqual(len(tracker.flush()), 1)
            self.assertEqual(tracker.active, {})

    def test_long_choruses_are_cut_without_counting_new_observations(self) -> None:
        window = 10 * SAMPLE_RATE
        detection = {"Turdus merula": (0.0, 3.0, 0.9, "Blackbird")}
        observations: list = []
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(Path(directory), observations, max_clip_seconds=20.0)
            exported: list[Path] = []
            for index in range(6):
                offset = index * window // 2
                exported += tracker.handle_result(
                    detection,
                    np.zeros(window, dtype=np.float32),
                    offset,
                    datetime(2026, 9, 1, 8) + timedelta(seconds=offset / SAMPLE_RATE),
                )

        self.assertEqual(len(observations), 1)
        self.assertEqual(len(exported), 1)
        self.assertIn("Turdus merula", tracker.active)

    def test_sliding_loop_dates_windows_and_flushes(self) -> None:
        frames = [np.zeros(SAMPLE_RATE, dtype=np.float32) for _ in range(6)]
        calls: list[int] = []

        def detect(window: np.ndarray):
            calls.append(window.size)
            return {"Turdus merula": (1.0, 2.0, 0.9, "Blackbird")}, window

        observations: list = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracker = self.make_tracker(root, observations)
            run_sliding_window_loop(
                frames,
                sample_rate=SAMPLE_RATE,
                slice_frames=4,
                detect_window=detect,
                tracker=tracker,
                now=lambda: datetime(2026, 9, 2, 0, 0, 2),
            )
            wav_files = list(root.rglob("*.wav"))

        self.assertEqual(calls, [4 * SAMPLE_RATE, 4 * SAMPLE_RATE])
        # window ended at 00:00:02 and lasted 4s: the call at +1s was still on the 1st of September
        self.assertEqual(observations[0][3], datetime(2026, 9, 1, 23, 59, 59))
        self.assertEqual(len(wav_files), 1)
        self.assertIn("_20260901_", wav_files[0].name)


class SpeciesNamesTests(unittest.TestCase):
    def test_names_come_from_supabase_and_survive_offline_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "names.json"
            names = SpeciesNames(cache, lookup_online=False)
            names.refresh_from([{"scientific_name": "Pica pica", "english_name": "Magpie",
                                 "italian_name": "Gazza", "german_name": "Elster"}])
            offline = SpeciesNames(cache, lookup_online=False)

        self.assertEqual(offline.display_names("Pica pica"), ("Gazza", "Elster", "Magpie"))
        # never seen and no Wikipedia: English name, then the scientific one
        self.assertEqual(offline.display_names("Strix aluco", "Tawny Owl"), ("Tawny Owl", "Tawny Owl", "Tawny Owl"))
        self.assertEqual(offline.species_row("Pica pica")["italian_name"], "Gazza")

    def test_a_new_species_is_looked_up_once(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            names = SpeciesNames(Path(directory) / "names.json")
            names.lookup = lambda language, name: calls.append(language) or {"it": "Merlo", "de": "Amsel"}[language]
            first = names.ensure("Turdus merula", "Blackbird")
            names.ensure("Turdus merula", "Blackbird")

        self.assertEqual(first, {"english_name": "Blackbird", "italian_name": "Merlo", "german_name": "Amsel"})
        self.assertEqual(calls, ["it", "de"])

    def test_wikipedia_errors_do_not_break_detection(self) -> None:
        class BrokenWiki:
            def page(self, name):
                raise ConnectionError("offline")

        self.assertIsNone(SpeciesNames._lookup_localized_name(BrokenWiki(), "Turdus merula"))

    def test_localized_names_are_cleaned(self) -> None:
        self.assertEqual(SpeciesNames._clean_localized_name("rondone, "), "Rondone")
        self.assertEqual(SpeciesNames._clean_localized_name("cuculo o"), "Cuculo")
        self.assertEqual(SpeciesNames._clean_localized_name("gallinella d'acqua"), "Gallinella D'Acqua")


class DeviceProbeTests(unittest.TestCase):
    def test_a_crashing_device_does_not_kill_the_process(self) -> None:
        devices = [
            {"index": 12, "name": "speex", "samplerate": 1000},
            {"index": 4, "name": "mic", "samplerate": 1000},
        ]

        crash_script = "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"
        with patch.object(cli, "_SOUNDDEVICE_PROBE_SCRIPT", crash_script):
            with self.assertRaisesRegex(RuntimeError, "crashed"):
                cli._probe_sounddevice_input(devices[0], 0.1, timeout_seconds=30)

        with patch.object(
            cli,
            "_probe_sounddevice_input",
            side_effect=[RuntimeError("crashed (signal 11)"), {"rms": 0.01, "peak": 0.1, "samplerate": 48000}],
        ):
            measurements = cli.probe_sounddevice_inputs(devices, 0.1)

        self.assertEqual([item["index"] for item in measurements], [4])


class DeviceSelectionTests(unittest.TestCase):
    def test_full_bandwidth_input_beats_a_slightly_louder_16k_one(self) -> None:
        devices = [
            {"index": 4, "name": "dmic 48k", "samplerate": 48000},
            {"index": 5, "name": "dmic 16k", "samplerate": 16000},
        ]
        measurements = [
            {"index": 4, "name": "dmic 48k", "rms": 0.0005, "peak": 0.01, "samplerate": 48000},
            {"index": 5, "name": "dmic 16k", "rms": 0.0009, "peak": 0.01, "samplerate": 16000},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, "probe_sounddevice_inputs", return_value=measurements):
                selected = cli.select_sounddevice_index(devices, state_path=Path(directory) / "device.json")

        self.assertEqual(selected, 4)

    def test_sound_server_beats_a_louder_exclusive_hw_device(self) -> None:
        devices = [
            {"index": 4, "name": "sof-hda-dsp: - (hw:0,6)", "samplerate": 48000},
            {"index": 16, "name": "default", "samplerate": 44100},
        ]
        measurements = [
            {"index": 4, "name": "sof-hda-dsp: - (hw:0,6)", "rms": 0.004, "peak": 0.02, "samplerate": 48000},
            {"index": 16, "name": "default", "rms": 0.0004, "peak": 0.007, "samplerate": 48000},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, "probe_sounddevice_inputs", return_value=measurements):
                selected = cli.select_sounddevice_index(devices, state_path=Path(directory) / "device.json")

        self.assertEqual(selected, 16)


class LiveSummaryTests(unittest.TestCase):
    def test_an_observation_without_clips_does_not_break_the_table(self) -> None:
        remote = [{"scientific_name": "Columba livia", "max_confidence": None, "last_heard": None},
                  {"scientific_name": "Pica pica", "english_name": "Magpie", "italian_name": "Gazza",
                   "german_name": "Elster", "max_confidence": 0.9, "last_heard": "2026-10-16T06:40:00+00:00",
                   "rarity": "rare", "daily_average": 0.1}]
        rows = cli.merge_daily_records(remote, [], None)
        self.assertEqual([row["taxon_name"] for row in rows], ["Pica pica"])

    def test_several_visits_of_a_species_make_one_row_with_the_latest_and_the_best(self) -> None:
        visit = {"scientific_name": "Pica pica", "english_name": "Magpie", "italian_name": "Gazza",
                 "german_name": "Elster", "rarity": "rare", "daily_average": 0.1}
        remote = [{**visit, "max_confidence": 0.91, "last_heard": "2026-10-16T06:40:00+00:00"},
                  {**visit, "max_confidence": 0.65, "last_heard": "2026-10-16T09:10:00+00:00"},
                  {**visit, "max_confidence": None, "last_heard": "2026-10-16T08:00:00+00:00"}]  # corrected
        rows = cli.merge_daily_records(remote, [], None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conf"], "0.910")
        self.assertEqual(rows[0]["time"], datetime(2026, 10, 16, 9, 10, tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S"))

    def test_a_species_leaves_the_live_memory_once_its_clip_is_written(self) -> None:
        from bird_audio_suite.cli import LiveToday

        heard = datetime(2026, 10, 15, 12, 27, 42).astimezone()
        today = LiveToday()
        today.add("Parus major", "Great Tit", 0.79, heard)
        today.add("Sitta europaea", "Eurasian Nuthatch", 0.66, heard)
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "Cinciallegra_20261015_122742_0.792.wav"
            wav.with_suffix(".json").write_text(json.dumps({"scientific_name": "Parus major",
                                                            "recorded_at": heard.isoformat()}))
            today.clips_written([wav])
        # the great tit is in the transit folder (then on Supabase): if it is deleted there, it is gone
        self.assertEqual([row["scientific_name"] for row in today.of_day("2026-10-15")], ["Sitta europaea"])

    def test_a_clip_started_before_midnight_clears_the_owl_heard_after_it(self) -> None:
        from bird_audio_suite.cli import LiveToday

        today = LiveToday()
        today.add("Strix aluco", "Tawny Owl", 0.9, datetime(2026, 10, 16, 0, 0, 1).astimezone())
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "Allocco_20261015_235958_0.900.wav"  # 3 s of pre-roll
            wav.with_suffix(".json").write_text(json.dumps({
                "scientific_name": "Strix aluco", "duration_s": 10.0,
                "recorded_at": datetime(2026, 10, 15, 23, 59, 58).astimezone().isoformat()}))
            today.clips_written([wav])
        self.assertEqual(today.of_day("2026-10-16"), [])

    def test_supabase_transit_and_memory_become_one_row_per_species(self) -> None:
        remote = [{"scientific_name": "Pica pica", "english_name": "Magpie", "italian_name": "Gazza",
                   "german_name": "Elster", "max_confidence": 0.99, "last_heard": "2026-10-10T08:00:00+00:00",
                   "rarity": "rare", "daily_average": 0.16}]
        late = datetime(2026, 10, 10, 23, 30).astimezone()
        local = [
            {"scientific_name": "Pica pica", "english_name": "Magpie", "time": late, "confidence": 0.80},
            {"scientific_name": "Turdus merula", "english_name": "Blackbird", "time": late, "confidence": 0.91},
        ]

        rows = cli.merge_daily_records(remote, local, FakeCatalog())

        self.assertEqual([row["taxon_name"] for row in rows], ["Pica pica", "Turdus merula"])
        magpie, blackbird = rows
        # latest time from the laptop, highest confidence and rarity from Supabase
        self.assertEqual((magpie["time"], magpie["conf"], magpie["rarity"], magpie["avg_counts"]),
                         ("23:30:00", "0.990", "rare", "0.16/day"))
        # not on Supabase yet: shown at once, names from the local catalog
        self.assertEqual((blackbird["taxon_ita"], blackbird["conf"], blackbird["rarity"]), ("Merlo", "0.910", "-"))


if __name__ == "__main__":
    unittest.main()
