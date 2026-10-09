from __future__ import annotations

import csv
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import scipy.io.wavfile as wavfile

fake_pyinaturalist = types.ModuleType("pyinaturalist")
fake_pyinaturalist.create_observation = lambda **kwargs: {}
fake_pyinaturalist.update_observation = lambda *args, **kwargs: {}
sys.modules.setdefault("pyinaturalist", fake_pyinaturalist)

from bird_audio_suite import cli, inaturalist
from bird_audio_suite.audio import (
    append_inaturalist_csv_row,
    export_detection_clips,
    parse_timestamp_from_name,
)
from bird_audio_suite.repair import apply_repair_plan, build_repair_plan
from bird_audio_suite.live import LiveDetectionTracker, run_sliding_window_loop
from bird_audio_suite.species import SpeciesCatalog


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


class CsvMergeTests(unittest.TestCase):
    def test_same_day_detections_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "obs.csv"
            append_inaturalist_csv_row(csv_path, observed_at=datetime(2026, 9, 1, 8), audio_file_name="a.wav", **csv_row_kwargs())
            append_inaturalist_csv_row(csv_path, observed_at=datetime(2026, 9, 1, 9), audio_file_name="b.wav", **csv_row_kwargs())

            rows = read_rows(csv_path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Data osservazione"], "2026-09-01 09:00:00")
        self.assertIn("audio_files=a.wav,b.wav", rows[0]["Descrizione"])

    def test_detections_of_different_days_are_never_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "obs.csv"
            append_inaturalist_csv_row(csv_path, observed_at=datetime(2026, 8, 31, 15), audio_file_name="a.wav", **csv_row_kwargs())
            append_inaturalist_csv_row(csv_path, observed_at=datetime(2026, 9, 2, 20), audio_file_name="b.wav", **csv_row_kwargs())

            rows = read_rows(csv_path)

        self.assertEqual([row["Data osservazione"][:10] for row in rows], ["2026-09-02", "2026-08-31"])


class ExportRoutingTests(unittest.TestCase):
    def test_clips_go_to_the_day_they_were_heard(self) -> None:
        samples = np.zeros(30 * SAMPLE_RATE, dtype=np.float32)
        grouped = {
            "Turdus merula": (5.0, 8.0, 0.9, "Blackbird"),
            "Strix aluco": (15.0, 18.0, 0.8, "Tawny Owl"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported = export_detection_clips(
                samples,
                SAMPLE_RATE,
                grouped,
                FakeCatalog(),
                root,
                observed_at=datetime(2026, 9, 1, 23, 59, 50),
            )

            names = sorted(str(path.relative_to(root)) for path in exported)
            before = read_rows(root / "20260901" / "inaturalist_import_20260901.csv")
            after = read_rows(root / "20260902" / "inaturalist_import_20260902.csv")

        self.assertEqual(
            names,
            [
                "20260901/Merlo/Merlo_20260901_235955_0.900.wav",
                "20260902/Strix aluco/Strix aluco_20260902_000005_0.800.wav",
            ],
        )
        self.assertEqual(before[0]["Data osservazione"], "2026-09-01 23:59:55")
        self.assertEqual(after[0]["Nome del taxon"], "Strix aluco")


class LiveTrackerTests(unittest.TestCase):
    def make_tracker(self, root: Path, observations: list, **kwargs) -> LiveDetectionTracker:
        return LiveDetectionTracker(
            sample_rate=SAMPLE_RATE,
            detections_dir=root,
            species_catalog=FakeCatalog(),
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
        self.assertEqual(wav_files[0].parent.parent.name, "20260901")


class SpeciesCatalogDateTests(unittest.TestCase):
    def make_catalog(self, directory: str) -> SpeciesCatalog:
        catalog = SpeciesCatalog(Path(directory) / "species.csv")
        catalog.lookup_italian_name = lambda name: "Merlo"
        catalog.lookup_german_name = lambda name: "Amsel"
        return catalog

    def test_out_of_order_batch_dates_never_move_last_seen_backwards(self) -> None:
        detection = {"scientific_name": "Turdus merula", "common_name": "Blackbird", "confidence": 0.9}
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            catalog.ensure_species([detection], observed_at=datetime(2026, 6, 10))
            catalog.ensure_species([detection], observed_at=datetime(2026, 6, 1))
            catalog.ensure_species([detection], observed_at=datetime(2026, 6, 1))

            record = catalog.records["Turdus merula"]

        self.assertEqual((record.first_seen_date, record.last_seen_date), ("2026-06-01", "2026-06-10"))
        self.assertEqual(record.observed_days, 2)

    def test_daily_average_uses_the_whole_monitoring_period(self) -> None:
        common = {"scientific_name": "Apus apus", "common_name": "Common Swift", "confidence": 0.9}
        burst = {"scientific_name": "Streptopelia turtur", "common_name": "Turtle-Dove", "confidence": 0.9}
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            catalog.ensure_species([common], observed_at=datetime(2026, 6, 1))
            catalog.ensure_species([common], observed_at=datetime(2026, 6, 10))
            catalog.ensure_species([burst] * 5, observed_at=datetime(2026, 6, 5))

            record = catalog.records["Streptopelia turtur"]

        # 5 detections over 10 monitored days, not 5 per day
        self.assertAlmostEqual(record.daily_average, 0.5)
        self.assertEqual(record.rarity, "uncommon")

    def test_wikipedia_errors_do_not_break_detection(self) -> None:
        class BrokenWiki:
            def page(self, name):
                raise ConnectionError("offline")

        self.assertIsNone(SpeciesCatalog._lookup_localized_name(BrokenWiki(), "Turdus merula"))

    def test_localized_names_are_cleaned(self) -> None:
        self.assertEqual(SpeciesCatalog._clean_localized_name("rondone, "), "Rondone")
        self.assertEqual(SpeciesCatalog._clean_localized_name("cuculo o"), "Cuculo")
        self.assertEqual(SpeciesCatalog._clean_localized_name("gallinella d'acqua"), "Gallinella D'Acqua")


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


class RepairTests(unittest.TestCase):
    def test_multi_day_folder_is_split_by_clip_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_dir = root / "20260831"
            clips = ["Merlo_20260831_150000_0.800.wav", "Merlo_20260902_070000_0.950.wav"]
            for clip in clips:
                (old_dir / "Merlo").mkdir(parents=True, exist_ok=True)
                (old_dir / "Merlo" / clip).write_bytes(b"RIFF")
            # what the old code wrote: one row for both days, dated at the last one
            for clip, when in zip(clips, [datetime(2026, 8, 31, 15), datetime(2026, 8, 31, 16)]):
                append_inaturalist_csv_row(
                    old_dir / "inaturalist_import_20260831.csv",
                    observed_at=when,
                    audio_file_name=clip,
                    **csv_row_kwargs(),
                )

            plan = build_repair_plan(root, FakeCatalog())
            self.assertEqual(len(plan.moves), 1)
            apply_repair_plan(plan)

            first = read_rows(root / "20260831" / "inaturalist_import_20260831.csv")
            second = read_rows(root / "20260902" / "inaturalist_import_20260902.csv")
            moved = (root / "20260902" / "Merlo" / clips[1]).exists()

        self.assertTrue(moved)
        self.assertEqual(first[0]["Data osservazione"], "2026-08-31 15:00:00")
        self.assertIn(f"audio_files={clips[0]}", first[0]["Descrizione"])
        self.assertEqual(second[0]["Data osservazione"], "2026-09-02 07:00:00")
        self.assertIn("birdnet_confidence=0.950", second[0]["Descrizione"])


class InaturalistStateTests(unittest.TestCase):
    def test_state_is_saved_before_a_later_row_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "inaturalist_import_20260901.csv"
            for taxon in ("Turdus merula", "Strix aluco"):
                append_inaturalist_csv_row(csv_path, observed_at=datetime(2026, 9, 1, 8), **csv_row_kwargs(taxon_name=taxon, italian_name=taxon))

            class FlakyClient:
                def __init__(self, *args, **kwargs):
                    self.calls = 0

                def create_observation(self, row, sounds=None):
                    self.calls += 1
                    if self.calls == 2:
                        raise RuntimeError("network down")
                    return inaturalist.ObservationRef(observation_id=1, observation_uuid="uuid-1")

            with patch.object(inaturalist, "INaturalistClient", FlakyClient):
                with self.assertRaises(RuntimeError):
                    inaturalist.import_csv(csv_path, jwt_token="token")

            state = inaturalist._load_state(csv_path.with_suffix(".state.json"))

        self.assertEqual(list(state), ["Turdus merula"])


if __name__ == "__main__":
    unittest.main()
