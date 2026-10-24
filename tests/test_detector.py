from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import numpy as np

from bird_audio_suite.detector import Analyzer, FastAnalyzer


def fake_analyzer(cls, positions: list):
    """The analyzer without its model: each chunk is already the scores BirdNET would give it."""
    analyzer = cls.__new__(cls)
    analyzer.labels = [f"Genus{index} species_Bird {index}" for index in range(40)]
    analyzer.use_custom_classifier = False
    analyzer.has_custom_species_list = False
    analyzer.classifier_model_path = None
    analyzer.predict = lambda chunk, sensitivity=1.0: [chunk]
    analyzer.set_predicted_species_list_from_position = positions.append
    return analyzer


class FastAnalyzerTests(unittest.TestCase):
    def test_same_detections_as_birdnetlib(self) -> None:
        rng = np.random.default_rng(3)
        chunks = [rng.random(40).astype(np.float32) for _ in range(6)]
        chunks[2][[5, 9, 30]] = np.float32(0.7)  # ties keep the labels' order
        chunks[3][:] = 0.0  # nothing heard
        chunks[4][7] = np.float32(0.3)  # exactly the threshold counts
        results, positions = [], []
        for cls in (Analyzer, FastAnalyzer):
            recording = SimpleNamespace(chunks=chunks, sample_secs=3.0, overlap=2.0, minimum_confidence=0.3,
                                        sensitivity=1.0, lat=45.8, lon=13.5, filename="buffer")
            analyzer = fake_analyzer(cls, positions)
            with redirect_stdout(io.StringIO()):
                analyzer.analyze_recording(recording)
            results.append((analyzer.results, [d.as_dict for d in recording.detection_list]))

        self.assertEqual(results[0], results[1])
        self.assertEqual(list(results[1][0]), ["0-3.0", "1.0-4.0", "2.0-5.0", "3.0-6.0", "4.0-7.0", "5.0-8.0"])
        self.assertEqual(results[1][0]["3.0-6.0"], [])
        self.assertEqual(len(positions), 2)  # both look up the species expected at the place


if __name__ == "__main__":
    unittest.main()
