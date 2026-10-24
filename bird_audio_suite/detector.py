from __future__ import annotations

import importlib.util
import io
import sys
import types
import warnings
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r".*Please use the LiteRT interpreter from the ai_edge_litert package.*",
    category=UserWarning,
    module=r"tensorflow\.lite\.python\.interpreter",
)


def _use_litert_as_tflite_runtime() -> None:
    """Let birdnetlib run on LiteRT (``ai-edge-litert``) instead of full TensorFlow.

    birdnetlib only tries ``tflite_runtime`` and then ``tensorflow.lite``. LiteRT is the
    maintained successor of tflite_runtime with the same Interpreter API, and avoids
    loading TensorFlow (~550 MB of RAM just to run the BirdNET .tflite model).
    """
    if importlib.util.find_spec("tflite_runtime") is not None:
        return
    try:
        from ai_edge_litert import interpreter as litert_interpreter
    except ImportError:
        return  # birdnetlib falls back to tensorflow.lite
    package = types.ModuleType("tflite_runtime")
    package.interpreter = litert_interpreter
    sys.modules["tflite_runtime"] = package
    sys.modules["tflite_runtime.interpreter"] = litert_interpreter


_use_litert_as_tflite_runtime()

import numpy as np
from birdnetlib import Recording, RecordingBuffer
from birdnetlib.analyzer import Analyzer


class FastAnalyzer(Analyzer):
    """birdnetlib's Analyzer with the same results, less Python per 3 s chunk.

    birdnetlib builds a dict of all ~6500 labels for every chunk and sorts it, to keep the few
    above the threshold: ~7.5 ms a chunk, about a tenth of the live window's BirdNET time (and of
    the uploader's analysis, with its 55 chunks per 30 s clip). Here numpy picks those few first,
    in the same order (best first; ties in label order, like the stable sort of the original).
    """

    def analyze_recording(self, recording):
        # as birdnetlib does: the species expected at this place and week (cached per position)
        if self.has_custom_species_list and recording.lon and recording.lat:
            raise ValueError("Recording lon/lat should not be used in conjunction with a custom species list or path.")
        if recording.lon and recording.lat and self.classifier_model_path is None:
            self.set_predicted_species_list_from_position(recording)

        start = 0
        end = recording.sample_secs
        results = {}
        for chunk in recording.chunks:
            if self.use_custom_classifier:
                scores = self.predict_with_custom_classifier(chunk, sensitivity=recording.sensitivity)[0]
            else:
                scores = self.predict(chunk, sensitivity=recording.sensitivity)[0]
            keep = np.flatnonzero(scores >= recording.minimum_confidence)
            keep = keep[np.argsort(-scores[keep], kind="stable")]
            results[str(start) + "-" + str(end)] = [(self.labels[index], scores[index]) for index in keep]
            start += recording.sample_secs - recording.overlap
            end = start + recording.sample_secs

        self.results = results
        recording.detection_list = self.detections


class BirdNetDetector:
    """Thin wrapper around birdnetlib for reusable detection calls."""

    def __init__(self) -> None:
        self.analyzer = FastAnalyzer()

    def detect(
        self,
        file_path: Path,
        latitude: float,
        longitude: float,
        when: datetime,
        min_confidence: float,
    ) -> list[dict]:
        recording = Recording(
            self.analyzer,
            str(file_path),
            lat=latitude,
            lon=longitude,
            date=when,
            min_conf=min_confidence,
        )
        return self._analyze(recording)

    def detect_samples(
        self,
        samples: np.ndarray,
        sample_rate: int,
        latitude: float,
        longitude: float,
        when: datetime,
        min_confidence: float,
        overlap: float = 0.0,
    ) -> list[dict]:
        """Analyze audio already in memory (live mode): no temporary WAV to write and decode.

        overlap (s) makes BirdNET's 3 s chunks overlap, so a call cut by a chunk border is still
        heard whole by the next one (slower: 3x the chunks with overlap=2)."""
        recording = RecordingBuffer(
            self.analyzer,
            np.asarray(samples, dtype=np.float32),
            int(sample_rate),
            lat=latitude,
            lon=longitude,
            date=when,
            min_conf=min_confidence,
            overlap=overlap,
        )
        return self._analyze(recording)

    @staticmethod
    def _analyze(recording) -> list[dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            recording.analyze()
        return recording.detections
