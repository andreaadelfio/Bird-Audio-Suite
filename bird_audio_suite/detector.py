from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from birdnetlib import Recording
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
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            recording.analyze()
        return recording.detections
