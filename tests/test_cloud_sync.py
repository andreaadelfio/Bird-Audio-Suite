from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

import csv

import numpy as np
import soundfile

fake_pyinaturalist = types.ModuleType("pyinaturalist")
fake_pyinaturalist.create_observation = lambda **kwargs: {}
fake_pyinaturalist.update_observation = lambda *args, **kwargs: {}
sys.modules.setdefault("pyinaturalist", fake_pyinaturalist)

from bird_audio_suite.audio import append_inaturalist_csv_row
from bird_audio_suite.cloud import (
    CloudConfig,
    DetectionSync,
    SupabaseStorageUploader,
    cloudinary_signature,
    slugify_public_id,
)


class FakeCatalog:
    records: dict = {}

    def display_names(self, scientific_name: str, fallback_english: str = "") -> tuple[str, str, str]:
        return ("Merlo", "Amsel", "Blackbird")


class FakeRest:
    def __init__(self, deleted: list[str] | None = None, existing: dict | None = None) -> None:
        self.calls: list[tuple[str, list[dict], str]] = []
        self.next_id = 1
        self.deleted = deleted or []
        self.existing = existing  # observation already on Supabase for the same day and species

    def get_observation(self, day, scientific_name):
        return self.existing

    def select_column(self, table, column):
        assert (table, column) == ("deleted_clips", "id")
        return list(self.deleted)

    def upsert(self, table, rows, on_conflict, returning=False):
        self.calls.append((table, rows, on_conflict))
        if returning:
            stored = [{**row, "id": self.next_id} for row in rows]
            self.next_id += 1
            return stored
        return []


class FakeUploader:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def upload(self, path, public_id, resource_type):
        self.uploads.append((public_id, resource_type, Path(path).suffix))
        return f"https://res.cloudinary.com/demo/{resource_type}/upload/v1/{public_id}{Path(path).suffix}"


def make_detections(root: Path) -> None:
    day = root / "20261009"
    clip = day / "Merlo" / "Merlo_20261009_065500_0.910.wav"
    clip.parent.mkdir(parents=True)
    soundfile.write(clip, np.zeros(16000, dtype=np.float32), 16000)
    append_inaturalist_csv_row(
        day / "inaturalist_import_20261009.csv",
        taxon_name="Turdus merula",
        observed_at=datetime(2026, 10, 9, 6, 55),
        english_name="Blackbird",
        italian_name="Merlo",
        german_name="Amsel",
        confidence=0.91,
        place_name="Monfalcone",
        latitude=45.8,
        longitude=13.5,
        audio_file_name=clip.name,
    )


def read_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def fake_render(audio_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"jpg")
    return output_path


class CloudinaryTests(unittest.TestCase):
    def test_signature_matches_the_documented_example(self) -> None:
        params = {
            "eager": "w_400,h_300,c_pad|w_260,h_200,c_crop",
            "public_id": "sample_image",
            "timestamp": "1315060510",
        }
        self.assertEqual(cloudinary_signature(params, "abcd"), "bfd09f95f331f558cbd1320e67aa8d488770583e")

    def test_public_ids_are_ascii_slugs(self) -> None:
        self.assertEqual(slugify_public_id("Codibùgnolo_20260901_113103_0.980"), "codibugnolo_20260901_113103_0-980")
        self.assertEqual(slugify_public_id("Gallinella D_Acqua_x"), "gallinella-d_acqua_x")


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict = {}
        self.posts: list = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, headers, len(data)))
        return types.SimpleNamespace(status_code=200, text="{}")


class SupabaseStorageTests(unittest.TestCase):
    def test_uploads_to_the_public_bucket_with_upsert(self) -> None:
        session = FakeSession()
        config = CloudConfig(supabase_url="https://abc.supabase.co", supabase_key="sb_publishable_x")
        uploader = SupabaseStorageUploader(config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            mp3 = Path(directory) / "clip.mp3"
            mp3.write_bytes(b"ID3data")
            url = uploader.upload(mp3, "20261009/merlo_20261009_065500_0-910", "video")

        posted_url, headers, size = session.posts[0]
        self.assertEqual(posted_url, "https://abc.supabase.co/storage/v1/object/bird-audio/20261009/merlo_20261009_065500_0-910.mp3")
        self.assertEqual((headers["Content-Type"], headers["x-upsert"], size), ("audio/mpeg", "true", 7))
        self.assertEqual(session.headers["apikey"], "sb_publishable_x")
        self.assertEqual(url, "https://abc.supabase.co/storage/v1/object/public/bird-audio/20261009/merlo_20261009_065500_0-910.mp3")

    def test_supabase_storage_is_the_default_without_cloudinary_keys(self) -> None:
        self.assertEqual(CloudConfig(supabase_url="u", supabase_key="k").storage_backend, "supabase")
        self.assertEqual(
            CloudConfig(cloudinary_cloud_name="c", cloudinary_api_key="k", cloudinary_api_secret="s").storage_backend,
            "cloudinary",
        )


class DetectionSyncTests(unittest.TestCase):
    def run_sync(self, root: Path, cache: Path, rest: FakeRest, uploader=None, delete_local=False, remote_ok=True):
        config = CloudConfig(supabase_url="https://example.supabase.co", supabase_key="secret")
        config.delete_local_audio = delete_local
        if uploader is None:
            config.media_storage = "local"
        sync = DetectionSync(
            root,
            FakeCatalog(),
            config,
            cache_dir=cache,
            rest=rest,
            uploader=uploader,
            render=fake_render,
            log=lambda *_: None,
            verify_remote=lambda url: remote_ok,
        )
        return sync.run()

    def test_pushes_species_observation_and_clip_without_touching_review_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            rest = FakeRest()
            report = self.run_sync(root, cache, rest)

        tables = [call[0] for call in rest.calls]
        self.assertEqual(tables, ["species", "observations", "clips"])
        observation = rest.calls[1][1][0]
        self.assertEqual(observation["day"], "2026-10-09")
        self.assertEqual(rest.calls[1][2], "day,scientific_name")
        for review_column in ("status", "notes", "corrected_scientific_name", "reviewed_at"):
            self.assertNotIn(review_column, observation)
        clip = rest.calls[2][1][0]
        self.assertEqual(clip["id"], "Merlo_20261009_065500_0.910.wav")
        self.assertEqual(clip["audio_url"], "media/clips/20261009/Merlo/Merlo_20261009_065500_0.910.wav")
        self.assertEqual(clip["duration_s"], 1.0)
        self.assertNotIn("review", clip)
        self.assertEqual((report.observations_synced, report.clips_uploaded, report.errors), (1, 1, []))

    def test_clips_deleted_from_the_site_are_never_uploaded_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            rest = FakeRest(deleted=["Merlo_20261009_065500_0.910.wav"])
            report = self.run_sync(root, cache, rest)

        # its only clip was deleted: the observation is not recreated either
        self.assertEqual((report.observations_total, report.observations_synced), (0, 0))
        self.assertEqual([call[0] for call in rest.calls], ["species"])

    def test_sync_keeps_working_before_the_schema_has_deleted_clips(self) -> None:
        class OldSchemaRest(FakeRest):
            def select_column(self, table, column):
                raise RuntimeError("Supabase deleted_clips read failed (404): PGRST205")

        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            report = self.run_sync(root, cache, OldSchemaRest())

        self.assertEqual((report.observations_synced, report.errors), (1, []))

    def test_wavs_go_once_safely_on_supabase_and_are_not_forgotten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            report = self.run_sync(root, cache, FakeRest(), FakeUploader(), delete_local=True)
            left = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.name != ".lock")
            rest = FakeRest()
            again = self.run_sync(root, cache, rest, FakeUploader(), delete_local=True)

        # transit folder: WAV, CSV and day folder are gone once online
        self.assertEqual((report.local_deleted, report.csv_removed, left), (1, 1, []))
        self.assertEqual((again.observations_total, again.observations_synced, again.errors), (0, 0, []))

    def test_wavs_stay_when_the_option_is_off_or_the_mp3_is_not_reachable(self) -> None:
        for delete_local, remote_ok in ((False, True), (True, False)):
            with tempfile.TemporaryDirectory() as directory:
                root, cache = Path(directory) / "detections", Path(directory) / "cache"
                make_detections(root)
                report = self.run_sync(root, cache, FakeRest(), FakeUploader(), delete_local=delete_local, remote_ok=remote_ok)
                self.assertEqual((report.local_deleted, len(list(root.rglob("*.wav")))), (0, 1))

    def test_transit_keeps_what_is_not_online_yet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            day = root / "20261009"
            # the live service has just recorded a second species, its clip is still open
            append_inaturalist_csv_row(
                day / "inaturalist_import_20261009.csv",
                taxon_name="Strix aluco",
                observed_at=datetime(2026, 10, 9, 7, 30),
                english_name="Tawny Owl",
                italian_name="Allocco",
                german_name="Waldkauz",
                confidence=0.8,
                place_name="Monfalcone",
                latitude=45.8,
                longitude=13.5,
            )
            self.run_sync(root, cache, FakeRest(), FakeUploader(), delete_local=True)
            rows = read_rows(day / "inaturalist_import_20261009.csv")

        self.assertEqual([row["Nome del taxon"] for row in rows], ["Strix aluco"])

    def test_a_clip_that_failed_to_upload_stays_with_its_row(self) -> None:
        class FailingUploader(FakeUploader):
            def upload(self, path, public_id, resource_type):
                raise RuntimeError("no network")

        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            report = self.run_sync(root, cache, FakeRest(), FailingUploader(), delete_local=True)
            rows = read_rows(root / "20261009" / "inaturalist_import_20261009.csv")
            wavs = list(root.rglob("*.wav"))

        self.assertEqual((len(report.errors), len(rows), len(wavs)), (1, 1, 1))
        self.assertIn("audio_files=Merlo_20261009_065500_0.910.wav", rows[0]["Descrizione"])

    def test_a_later_detection_adds_to_the_observation_online(self) -> None:
        existing = {
            "max_confidence": 0.97,
            "observed_at": "2026-10-09T03:00:00+00:00",  # 05:00 local, before the 06:55 detection
            "place_name": "Grado",
            "latitude": 45.68,
            "longitude": 13.38,
        }
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            rest = FakeRest(existing=existing)
            self.run_sync(root, cache, rest, FakeUploader(), delete_local=True)

        row = rest.calls[1][1][0]
        # 0.91 heard now does not lower the 0.97 already online; time moves on; place stays
        self.assertEqual(row["max_confidence"], 0.97)
        self.assertTrue(row["observed_at"].startswith("2026-10-09T06:55:00"))
        self.assertEqual((row["place_name"], row["latitude"]), ("Grado", 45.68))

    def test_old_csvs_of_wavs_already_removed_are_emptied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            # previous version: WAV removed after upload, CSV kept
            self.run_sync(root, cache, FakeRest(), FakeUploader(), delete_local=False)
            next(root.rglob("*.wav")).unlink()
            report = self.run_sync(root, cache, FakeRest(), FakeUploader(), delete_local=True)
            left = [path for path in root.rglob("*") if path.name != ".lock"]

        self.assertEqual((report.csv_removed, left), (1, []))

    def test_rows_of_clips_deleted_from_the_cestino_leave_the_transit_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            next(root.rglob("*.wav")).unlink()  # WAV already gone, its row still listed
            rest = FakeRest(deleted=["Merlo_20261009_065500_0.910.wav"])
            report = self.run_sync(root, cache, rest, FakeUploader(), delete_local=True)
            left = [path for path in root.rglob("*") if path.name != ".lock"]

        self.assertEqual((report.csv_removed, left), (1, []))

    def test_wavs_deleted_from_the_cestino_are_removed_locally_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            rest = FakeRest(deleted=["Merlo_20261009_065500_0.910.wav"])
            report = self.run_sync(root, cache, rest, FakeUploader(), delete_local=True)
            wavs_left = list(root.rglob("*.wav"))

        self.assertEqual((report.local_deleted, wavs_left), (1, []))

    def test_second_run_skips_unchanged_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            self.run_sync(root, cache, FakeRest())
            rest = FakeRest()
            report = self.run_sync(root, cache, rest)

        self.assertEqual(report.observations_synced, 0)
        self.assertEqual([call[0] for call in rest.calls], ["species"])
        self.assertEqual(rest.calls[0][1], [])

    def test_remote_storage_gets_an_mp3_not_the_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory) / "detections", Path(directory) / "cache"
            make_detections(root)
            rest, uploader = FakeRest(), FakeUploader()
            self.run_sync(root, cache, rest, uploader)

        clip = rest.calls[2][1][0]
        self.assertEqual(
            uploader.uploads,
            [
                ("20261009/merlo_20261009_065500_0-910", "video", ".mp3"),
                ("20261009/merlo_20261009_065500_0-910-spectrogram", "image", ".jpg"),
            ],
        )
        self.assertTrue(clip["audio_url"].endswith(".mp3"))
        self.assertTrue(clip["spectrogram_url"].endswith("-spectrogram.jpg"))


if __name__ == "__main__":
    unittest.main()
