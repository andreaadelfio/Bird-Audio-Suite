from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from bird_audio_suite import perch


class FakeSession:
    """Perch's ONNX session: a block with a loud start says "bird A", a quiet one "bird B"; an
    American owl (class 3) gets `owl` as its logit."""

    def __init__(self, classes: int, owl: float = 3.0) -> None:
        self.classes, self.blocks, self.owl = classes, [], owl

    def run(self, names, feeds):
        block = feeds["inputs"]
        assert names == ["label"] and block.shape == (1, perch.BLOCK)
        self.blocks.append(block)
        logits = np.full((1, self.classes), -5.0, np.float32)
        logits[0, 0 if np.abs(block[0, :1000]).max() > 0.5 else 1] = 5.0
        logits[0, 3] = self.owl
        return [logits]


def model_with(labels: list[str], directory: Path, owl: float = 3.0) -> tuple[perch.Perch, FakeSession]:
    (directory / perch.LABELS_FILE).write_text("\n".join(["inat2024_fsd50k", *labels]), encoding="utf-8")
    session = FakeSession(len(labels), owl)
    return perch.Perch(cache_dir=directory, session=session), session


class PerchTests(unittest.TestCase):
    def test_blocks_of_five_seconds_every_two_and_a_half_at_32_khz_best_over_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model, session = model_with(["Bird a", "Bird b", "Speech", "Strix varia"], Path(directory))
            audio = np.zeros(12 * 48_000, np.float32)  # 12 s at 48 kHz, as the live writes them
            audio[: 48_000 // 10] = 0.9  # loud only at the start: the first block
            blocks = list(model.blocks(audio, 48_000))
            opinion = model.opinion(audio, 48_000, allowed={"Bird a", "Bird b"})
        # 12 s -> blocks at 0, 2.5, 5 s and one ending with the clip (7 s), each summing to 1
        self.assertEqual(len(session.blocks), 8)  # the blocks above, then again for the opinion
        self.assertEqual(len(blocks), 4)
        for probabilities in blocks:
            self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=6)
        # each heard well in one block: the best block counts, as a share of the species of here
        self.assertEqual([name for name, _ in opinion], ["Bird a", "Bird b"])
        self.assertGreater(min(share for _, share in opinion), 0.99)

    def test_only_the_species_around_the_clip_and_only_what_it_really_hears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model, _ = model_with(["Bird a", "Bird b", "Speech", "Strix varia"], Path(directory))
            opinion = model.opinion(np.zeros(5 * 32_000, np.float32), 32_000, allowed={"Bird b", "Bird a"})
        # Strix varia is not observed around here; Bird a is below MIN_SHARE; Speech is no species
        self.assertEqual([name for name, _ in opinion], ["Bird b"])
        self.assertGreater(opinion[0][1], 0.99)  # its share among the species of here, not of all

    def test_a_block_mostly_about_something_from_elsewhere_says_nothing(self) -> None:
        # the American owl takes ~95% of Perch's total: the species of here get under 30%
        with tempfile.TemporaryDirectory() as directory:
            model, _ = model_with(["Bird a", "Bird b", "Speech", "Strix varia"], Path(directory), owl=8.0)
            opinion = model.opinion(np.zeros(5 * 32_000, np.float32), 32_000, allowed={"Bird a", "Bird b"})
        self.assertEqual(opinion, [])

    def test_the_species_around_are_asked_once_and_kept_for_three_months(self) -> None:
        pages = [{"total_results": 600, "results": [{"taxon": {"name": "Vulpes vulpes"}}, {"taxon": {"name": "Strix aluco"}}]},
                 {"total_results": 600, "results": [{"taxon": {"name": "Coloeus monedula"}}]}]  # 500 a page

        class Session:
            calls = []

            def get(self, url, params, timeout):
                self.calls.append(params)
                page = pages[params["page"] - 1]
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: page})()

        class Offline:
            def get(self, *args, **kwargs):
                raise perch.requests.ConnectionError("offline")

        with tempfile.TemporaryDirectory() as directory, patch.object(perch, "NEARBY_KM", 50):
            cache = Path(directory)
            with patch("bird_audio_suite.perch.INAT_SPECIES_COUNTS", "x"), patch.object(perch, "json", json):
                first = perch.nearby_species(52.52, 13.405, cache_dir=cache, session=Session(), now=1000)
                with patch("bird_audio_suite.perch.INAT_SPECIES_COUNTS", "x"):
                    again = perch.nearby_species(52.52, 13.405, cache_dir=cache, session=Offline(), now=2000)
                    stale = perch.nearby_species(52.52, 13.405, cache_dir=cache, session=Offline(),
                                                 now=1000 + perch.NEARBY_MAX_AGE_SECONDS + 1)
                    elsewhere = perch.nearby_species(45.8, 13.5, cache_dir=cache, session=Offline(), now=2000)
        self.assertEqual(first, {"Vulpes vulpes", "Strix aluco", "Coloeus monedula"})
        self.assertEqual(len(Session.calls), 2)  # two pages, then the cache
        self.assertEqual(Session.calls[0]["taxon_id"], perch.NEARBY_TAXA)
        self.assertEqual(again, first)
        self.assertEqual(stale, first)  # too old, but offline: still better than nothing
        self.assertIsNone(elsewhere)  # a place never asked, offline: no opinion rather than a wrong one

    def test_a_download_that_does_not_match_its_checksum_is_never_used(self) -> None:
        content = b"model bytes"

        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield self.data

        class Session:
            def __init__(self, data):
                self.data = data

            def get(self, url, stream, timeout):
                return Response(self.data)

        files = {"model.onnx": hashlib.sha256(content).hexdigest()}
        with tempfile.TemporaryDirectory() as directory, patch.object(perch, "FILES", files):
            cache = Path(directory)
            self.assertFalse(perch.ensure_files(cache, session=Session(b"tampered")))
            self.assertEqual(list(cache.iterdir()), [])
            self.assertTrue(perch.ensure_files(cache, session=Session(content)))
            self.assertEqual((cache / "model.onnx").read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
