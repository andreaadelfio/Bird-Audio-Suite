from __future__ import annotations

import unittest
from datetime import datetime

import numpy as np

from bird_audio_suite.analysis import analyse_samples


class Model:
    def __init__(self, detections):
        self.detections = detections
        self.calls = []

    def detect_samples(self, samples, rate, **kwargs):
        self.calls.append(kwargs)
        return self.detections


def detection(name, start, confidence, english=""):
    return {"scientific_name": name, "common_name": english, "start_time": start, "end_time": start + 3.0,
            "confidence": confidence}


class AnalysisTests(unittest.TestCase):
    def analyse(self, detections, seconds=10):
        model = Model(detections)
        result = analyse_samples(np.zeros(48000 * seconds, np.float32), 48000, "Parus major", 52.5, 13.4,
                                 datetime(2026, 10, 15, 12, 0), model=model)
        return result, model

    def test_one_run_gives_confidence_bands_and_candidates_that_agree(self) -> None:
        result, model = self.analyse([
            detection("Parus major", 2.0, 0.83, "Great Tit"), detection("Parus major", 2.5, 0.71, "Great Tit"),
            detection("Parus major", 6.0, 0.65, "Great Tit"), detection("Parus major", 8.0, 0.45, "Great Tit"),
            detection("Cyanistes caeruleus", 4.0, 0.62, "Eurasian Blue Tit"),
            detection("Alcedo atthis", 1.0, 0.12, "Common Kingfisher"),
        ])
        self.assertEqual(result["confidence"], 0.83)  # clip_conf = the best band
        self.assertEqual(max(segment[2] for segment in result["segments"]), 0.83)
        # 2-5.5 s (0.83 and 0.71 merged, likely), 6-9 s (0.65, unsure); 0.45 is below 0.6: no band
        self.assertEqual([segment[:2] for segment in result["segments"]], [[2.0, 5.5], [6.0, 9.0]])
        # only species from 0.6: the kingfisher at 0.12 is not a candidate
        self.assertEqual([name for name, _, _ in result["candidates"]], ["Parus major", "Cyanistes caeruleus"])
        self.assertEqual(model.calls[0]["overlap"], 2.5)  # a chunk every 0.5 s

    def test_species_not_found_on_the_saved_clip_says_so(self) -> None:
        result, _ = self.analyse([detection("Parus major", 0.0, 0.45), detection("Sitta europaea", 3.0, 0.9)])
        self.assertEqual(result["confidence"], 0.45)  # the saved audio does not confirm it
        self.assertEqual(result["segments"], [])  # nothing from 0.6
        self.assertEqual(result["candidates"], [["Sitta europaea", "", 0.9]])  # BirdNET says something else
        nothing, _ = self.analyse([detection("Sitta europaea", 3.0, 0.9)])
        self.assertEqual((nothing["confidence"], nothing["segments"]), (0.0, []))


if __name__ == "__main__":
    unittest.main()
