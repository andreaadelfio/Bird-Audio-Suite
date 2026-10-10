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


class BirdNetDetector:
    """Thin wrapper around birdnetlib for reusable detection calls."""

    def __init__(self) -> None:
        self.analyzer = Analyzer()

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
