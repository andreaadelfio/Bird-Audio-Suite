from __future__ import annotations

import csv
import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile

from bird_audio_suite.audio import detections_lock, export_detection_clips
from bird_audio_suite.cloud import (
    CloudConfig,
    Outbox,
    SupabaseStorage,
    convert_legacy_csvs,
    media_path_for,
    outbox_items,
    slugify_public_id,
)

SAMPLE_RATE = 16000


class FakeNames:
    def __init__(self) -> None:
        self.names = {"Turdus merula": {"english_name": "Blackbird", "italian_name": "Merlo", "german_name": "Amsel"}}

    def ensure(self, scientific_name, english_name=""):
        return self.names.setdefault(scientific_name, {"english_name": english_name, "italian_name": scientific_name,
                                                       "german_name": scientific_name})

    def display_names(self, scientific_name, fallback_english=""):
        row = self.names.get(scientific_name, {})
        return (row.get("italian_name") or scientific_name, row.get("german_name") or scientific_name,
                row.get("english_name") or fallback_english)

    def species_row(self, scientific_name):
        return {"scientific_name": scientific_name, **self.names[scientific_name]}


class FakeRest:
    def __init__(self, deleted=(), offline=False) -> None:
        self.deleted = list(deleted)
        self.offline = offline
        self.upserts: list[tuple[str, list[dict]]] = []
        self.observations: dict[tuple[str, str], int] = {}

    def select_column(self, table, column):
        if self.offline:
            raise RuntimeError("no network")
        assert (table, column) == ("deleted_clips", "id")
        return self.deleted

    def upsert(self, table, rows, on_conflict):
        self.upserts.append((table, rows))

    def observation_id(self, day, scientific_name):
        return self.observations.setdefault((day, scientific_name), len(self.observations) + 1)


class FakeStorage:
    def __init__(self, fail=False) -> None:
        self.fail = fail
        self.objects: dict[str, int] = {}

    def upload(self, path, object_path):
        if self.fail:
            raise RuntimeError("upload failed")
        self.objects[object_path] = Path(path).stat().st_size

    def exists(self, object_path):
        return object_path in self.objects


def fake_render(audio_path, output_path):
    Path(output_path).write_bytes(b"jpg")
    return output_path


def fake_encode(audio_path, output_path):
    Path(output_path).write_bytes(b"mp3")
    return output_path


def export(folder: Path, when: datetime, species=("Turdus merula", "Blackbird"), confidence=0.91) -> Path:
    samples = np.zeros(SAMPLE_RATE, dtype=np.float32)
    return export_detection_clips(
        samples, SAMPLE_RATE, {species[0]: (0.0, 1.0, confidence, species[1])}, FakeNames(), folder,
        latitude=45.8, longitude=13.5, place_name="Monfalcone", observed_at=when,
    )[0]


def outbox(folder, rest, storage=None) -> Outbox:
    config = CloudConfig(supabase_url="https://example.supabase.co", supabase_key="k")
    return Outbox(folder, FakeNames(), config, rest=rest, storage=storage or FakeStorage(),
                  render=fake_render, encode=fake_encode)


class ExportTests(unittest.TestCase):
    def test_each_clip_is_a_wav_plus_its_data_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            wav = export(folder, datetime(2026, 10, 9, 23, 59, 59))
            data = json.loads(wav.with_suffix(".json").read_text())
            files = sorted(path.name for path in folder.iterdir())

        self.assertEqual(files, ["Merlo_20261009_235959_0.910.json", "Merlo_20261009_235959_0.910.wav"])
        self.assertEqual((data["id"], data["scientific_name"], data["confidence"]),
                         ("Merlo_20261009_235959_0.910.wav", "Turdus merula", 0.91))
        self.assertTrue(data["recorded_at"].startswith("2026-10-09T23:59:59"))
        self.assertEqual((data["place_name"], data["latitude"], data["duration_s"]), ("Monfalcone", 45.8, 1.0))

    def test_a_clip_without_its_data_file_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            wav = export(folder, datetime(2026, 10, 9, 7))
            wav.with_suffix(".json").unlink()
            self.assertEqual(outbox_items(folder), [])


class PushTests(unittest.TestCase):
    def test_a_clip_goes_to_supabase_once_and_leaves_the_laptop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            export(folder, datetime(2026, 10, 9, 6, 55))
            export(folder, datetime(2026, 10, 9, 18, 10), confidence=0.97)
            rest, storage = FakeRest(), FakeStorage()
            report = outbox(folder, rest, storage).push()
            left = [path.name for path in folder.iterdir() if path.name != ".lock"]

        self.assertEqual((report.sent, report.errors, left), (2, [], []))
        # two clips of the same species and day: one observation
        self.assertEqual(rest.observations, {("2026-10-09", "Turdus merula"): 1})
        clip_rows = [row for table, rows in rest.upserts if table == "clips" for row in rows]
        self.assertEqual([row["confidence"] for row in clip_rows], [0.91, 0.97])
        first = clip_rows[0]
        self.assertEqual(first["media_path"], "20261009/merlo_20261009_065500_0-910")
        # no URLs, no review columns: only what the clip is
        self.assertEqual(
            sorted(first),
            ["confidence", "duration_s", "id", "latitude", "longitude", "media_path", "observation_id", "place_name",
             "recorded_at", "segments"],
        )
        self.assertEqual(sorted(storage.objects), [
            "20261009/merlo_20261009_065500_0-910-spectrogram.jpg", "20261009/merlo_20261009_065500_0-910.mp3",
            "20261009/merlo_20261009_181000_0-970-spectrogram.jpg", "20261009/merlo_20261009_181000_0-970.mp3",
        ])
        species_rows = [row for table, rows in rest.upserts if table == "species" for row in rows]
        self.assertEqual(species_rows, [{"scientific_name": "Turdus merula", "english_name": "Blackbird",
                                         "italian_name": "Merlo", "german_name": "Amsel"}])

    def test_offline_everything_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            export(folder, datetime(2026, 10, 9, 7))
            report = outbox(folder, FakeRest(offline=True)).push()
            waiting = len(outbox_items(folder))

        self.assertEqual((report.sent, waiting, len(report.errors)), (0, 1, 1))

    def test_a_failed_upload_keeps_the_clip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            export(folder, datetime(2026, 10, 9, 7))
            rest = FakeRest()
            report = outbox(folder, rest, FakeStorage(fail=True)).push()
            waiting = len(outbox_items(folder))

        self.assertEqual((report.sent, waiting), (0, 1))
        self.assertNotIn("clips", [table for table, _ in rest.upserts])

    def test_clips_deleted_from_the_cestino_are_dropped_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            wav = export(folder, datetime(2026, 10, 9, 7))
            rest = FakeRest(deleted=[wav.name])
            report = outbox(folder, rest).push()
            waiting = len(outbox_items(folder))

        self.assertEqual((report.discarded, report.sent, waiting, rest.upserts), (1, 0, 0, []))

    def test_a_second_uploader_waits_its_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            export(folder, datetime(2026, 10, 9, 7))
            with detections_lock(folder):
                report = outbox(folder, FakeRest()).push()

        self.assertTrue(report.busy)
        self.assertEqual(report.sent, 0)


class LegacyConversionTests(unittest.TestCase):
    def test_clips_left_in_the_old_day_folders_become_transit_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            day = folder / "20261009"
            (day / "Merlo").mkdir(parents=True)
            soundfile.write(day / "Merlo" / "Merlo_20261009_065500_0.910.wav", np.zeros(800, dtype=np.float32), 16000)
            header = ["Nome del taxon", "Data osservazione", "Descrizione", "Nome del luogo",
                      "Latitudine / coord y / nord", "Longitudine / coord x / est", "Etichette", "Geoprivacy"]
            with (day / "inaturalist_import_20261009.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerow(["Turdus merula", "2026-10-09 06:55:00",
                                 "Blackbird, Merlo, Amsel; birdnet_confidence=0.910; "
                                 "audio_files=Merlo_20261009_065500_0.910.wav,Merlo_already_online.wav",
                                 "Monfalcone", "45.800000", "13.500000", "audio,birdnet", ""])
            converted = convert_legacy_csvs(folder, FakeNames())
            items = outbox_items(folder)
            left = sorted(path.name for path in folder.iterdir())

        self.assertEqual(converted, 1)
        self.assertEqual(left, ["Merlo_20261009_065500_0.910.json", "Merlo_20261009_065500_0.910.wav"])
        data = items[0].data
        self.assertEqual((data["scientific_name"], data["confidence"], data["place_name"], data["latitude"]),
                         ("Turdus merula", 0.91, "Monfalcone", 45.8))


class PurgeTests(unittest.TestCase):
    def test_files_of_observations_on_inaturalist_for_a_week_go_rows_stay(self) -> None:
        class Rest:
            def __init__(self) -> None:
                self.queries, self.updates = [], []

            def get(self, table, params):
                self.queries.append((table, params))
                return [{"id": "Allocco_20261004_193743_0.992.wav", "media_path": "20261004/allocco_x"}]

            def update(self, table, filters, values):
                self.updates.append((table, filters, values))

        class Storage:
            removed: list = []

            def remove(self, paths):
                self.removed.extend(paths)

        rest, storage = Rest(), Storage()
        config = CloudConfig(supabase_url="https://example.supabase.co", supabase_key="k")
        now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        purged = Outbox(Path("."), FakeNames(), config, rest=rest, storage=storage).purge_uploaded_media(now=now)

        self.assertEqual(purged, 1)
        table, params = rest.queries[0]
        self.assertEqual((table, params["observations.status"], params["media_deleted_at"]),
                         ("clips", "eq.uploaded", "is.null"))
        self.assertEqual(params["observations.uploaded_at"], "lt.2026-10-11T12:00:00+00:00")
        self.assertEqual(storage.removed, ["20261004/allocco_x.mp3", "20261004/allocco_x-spectrogram.jpg"])
        self.assertEqual(rest.updates, [("clips", {"id": "eq.Allocco_20261004_193743_0.992.wav"},
                                         {"media_deleted_at": "2026-10-18T12:00:00+00:00"})])


class StorageTests(unittest.TestCase):
    def test_uploads_to_the_public_bucket_with_upsert(self) -> None:
        posts = []

        class Session:
            headers: dict = {}

            def post(self, url, data=None, headers=None, timeout=None):
                posts.append((url, headers))
                return types.SimpleNamespace(status_code=200, text="{}")

        config = CloudConfig(supabase_url="https://abc.supabase.co", supabase_key="sb_publishable_x")
        storage = SupabaseStorage(config, session=Session())
        with tempfile.TemporaryDirectory() as directory:
            mp3 = Path(directory) / "clip.mp3"
            mp3.write_bytes(b"ID3")
            storage.upload(mp3, "20261009/merlo.mp3")

        self.assertEqual(posts[0][0], "https://abc.supabase.co/storage/v1/object/bird-audio/20261009/merlo.mp3")
        self.assertEqual((posts[0][1]["Content-Type"], posts[0][1]["x-upsert"]), ("audio/mpeg", "true"))
        self.assertEqual(storage.public_url("x.mp3"), "https://abc.supabase.co/storage/v1/object/public/bird-audio/x.mp3")

    def test_media_paths_are_ascii(self) -> None:
        self.assertEqual(slugify_public_id("Codibùgnolo_20260901_113103_0.980"), "codibugnolo_20260901_113103_0-980")
        self.assertEqual(media_path_for("Gallinella D_Acqua_20261004_205425_0.867.wav", datetime(2026, 10, 4, 20, 54)),
                         "20261004/gallinella-d_acqua_20261004_205425_0-867")


if __name__ == "__main__":
    unittest.main()
