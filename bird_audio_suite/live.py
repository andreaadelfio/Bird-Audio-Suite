from __future__ import annotations

import collections
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from .audio import export_detection_clips


@dataclass
class ActiveDetection:
    """A species heard in consecutive windows, accumulated into a single clip."""

    english_name: str
    confidence: float
    clip_start_time: datetime
    last_end_sample: int
    samples: list[np.ndarray] = field(default_factory=list)
    sample_count: int = 0
    missing_windows: int = 0

    def append(self, samples: np.ndarray) -> None:
        if samples.size:
            self.samples.append(np.asarray(samples, dtype=np.float32))
            self.sample_count += int(samples.size)


class LiveDetectionTracker:
    """Group overlapping live windows per species and export one clip per event.

    - the observation is recorded (species stats + CSV row) as soon as a species is first heard;
    - the clip starts ``pre_roll_seconds`` before the detection, to keep the start of the phrase;
    - while it keeps being detected, the new audio of each window is appended to its clip;
      after it stops, audio keeps being appended until the clip is ``min_clip_seconds`` long;
    - after more than ``grace_windows`` windows without it (pauses between phrases), or when
      the clip reaches ``max_clip_seconds``, the clip is written in the folder of the day it was heard.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        detections_dir: Path,
        species_names,
        record_observation: Callable[[str, str, float, datetime], None],
        export_kwargs: dict,
        clip_span: str = "from_detection",
        grace_windows: int = 2,
        pre_roll_seconds: float = 3.0,
        min_clip_seconds: float = 10.0,
        max_clip_seconds: float = 120.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.detections_dir = Path(detections_dir)
        self.species_names = species_names
        self.record_observation = record_observation
        self.export_kwargs = export_kwargs
        self.clip_span = clip_span
        self.grace_windows = grace_windows
        self.pre_roll_samples = int(pre_roll_seconds * self.sample_rate)
        self.min_clip_samples = int(min_clip_seconds * self.sample_rate)
        self.max_clip_samples = int(max_clip_seconds * self.sample_rate)
        self.active: dict[str, ActiveDetection] = {}

    def handle_result(
        self,
        grouped: dict[str, tuple[float, float, float, str]],
        working_samples: np.ndarray,
        window_start_sample: int,
        window_start_time: datetime,
    ) -> list[Path]:
        exported: list[Path] = []
        window_len = int(len(working_samples))
        window_end_sample = window_start_sample + window_len

        for scientific_name, (start_sec, _end_sec, confidence, english_name) in grouped.items():
            active = self.active.get(scientific_name)
            if active is None:
                if self.clip_span == "full_slice":
                    start_offset = 0
                else:
                    start_offset = int(start_sec * self.sample_rate) - self.pre_roll_samples
                start_offset = min(max(start_offset, 0), window_len)
                heard_at = window_start_time + timedelta(seconds=float(start_sec))
                self.record_observation(scientific_name, english_name, confidence, heard_at)
                active = ActiveDetection(
                    english_name=english_name,
                    confidence=confidence,
                    clip_start_time=window_start_time + timedelta(seconds=start_offset / self.sample_rate),
                    last_end_sample=window_end_sample,
                )
                active.append(working_samples[start_offset:])
                self.active[scientific_name] = active
            else:
                append_from = max(0, active.last_end_sample - window_start_sample)
                if append_from < window_len:
                    active.append(working_samples[append_from:])
                active.last_end_sample = max(active.last_end_sample, window_end_sample)
                active.confidence = max(active.confidence, confidence)
                active.missing_windows = 0

            if active.sample_count >= self.max_clip_samples:
                # Long choruses: cut the clip but keep the species active, so it is not
                # counted again as a new observation.
                exported += self._export(scientific_name, active)
                active.samples = []
                active.sample_count = 0
                active.clip_start_time = window_start_time + timedelta(seconds=window_len / self.sample_rate)

        for scientific_name in list(self.active):
            if scientific_name in grouped:
                continue
            active = self.active[scientific_name]
            active.missing_windows += 1
            if active.sample_count < self.min_clip_samples:
                # too short to be useful: borrow the audio that follows the last detection
                append_from = max(0, active.last_end_sample - window_start_sample)
                needed = self.min_clip_samples - active.sample_count
                tail = working_samples[append_from:append_from + needed]
                active.append(tail)
                active.last_end_sample = max(active.last_end_sample, window_start_sample + append_from + int(tail.size))
            if active.missing_windows > self.grace_windows:
                exported += self._export(scientific_name, active)
                del self.active[scientific_name]

        return exported

    def flush(self) -> list[Path]:
        """Export every clip still open (on shutdown)."""
        exported: list[Path] = []
        for scientific_name, active in list(self.active.items()):
            exported += self._export(scientific_name, active)
        self.active.clear()
        return exported

    def _export(self, scientific_name: str, active: ActiveDetection) -> list[Path]:
        if not active.samples:
            return []
        clip = np.concatenate(active.samples, axis=0)
        group = {
            scientific_name: (
                0.0,
                len(clip) / float(self.sample_rate),
                active.confidence,
                active.english_name,
            )
        }
        return export_detection_clips(
            clip,
            self.sample_rate,
            group,
            self.species_names,
            self.detections_dir,
            observed_at=active.clip_start_time,
            **self.export_kwargs,
        )


DetectWindow = Callable[[np.ndarray], tuple[dict[str, tuple[float, float, float, str]], np.ndarray]]


def run_sliding_window_loop(
    frames: Iterable[np.ndarray],
    *,
    sample_rate: int,
    slice_frames: int,
    detect_window: DetectWindow,
    tracker: LiveDetectionTracker,
    on_exported: Callable[[list[Path]], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    now: Callable[[], datetime] = datetime.now,
    shutdown_timeout: float = 30.0,
) -> None:
    """Feed 50%-overlapping windows of ``frames`` to a background detector.

    ``frames`` yields normalized float32 mono blocks. Detection runs in a worker
    thread so audio is never dropped while BirdNET is busy; results are handled
    here, in the caller's thread. On exit (end of frames, Ctrl+C, SIGTERM or an
    error) pending windows are completed and open clips are flushed to disk.
    """
    slice_frames = max(1, int(slice_frames))
    slide_step_frames = max(1, slice_frames // 2)
    task_queue: queue.Queue = queue.Queue()
    result_queue: queue.Queue = queue.Queue()

    def worker() -> None:
        while True:
            item = task_queue.get()
            if item is None:
                break
            window, window_start_sample, window_start_time = item
            try:
                grouped, working_samples = detect_window(window)
                result_queue.put((grouped, working_samples, window_start_sample, window_start_time))
            except Exception as exc:
                result_queue.put(exc)

    def drain_results() -> None:
        while True:
            try:
                result = result_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(result, Exception):
                    raise result
                exported = tracker.handle_result(*result)
                if exported and on_exported:
                    on_exported(exported)
            except Exception as exc:
                if on_error:
                    on_error(exc)

    worker_thread = threading.Thread(target=worker, name="birdnet-worker", daemon=True)
    worker_thread.start()

    frames_buf: collections.deque = collections.deque(maxlen=slice_frames)
    frames_since_last_window = 0
    total_samples = 0
    try:
        for frame in frames:
            frame = np.asarray(frame, dtype=np.float32).reshape(-1)
            frames_buf.append(frame)
            total_samples += frame.size
            frames_since_last_window += 1

            drain_results()

            if len(frames_buf) < slice_frames or frames_since_last_window < slide_step_frames:
                continue

            window = np.concatenate(frames_buf, axis=0)
            window_start_sample = total_samples - window.size
            # The last frame has just been captured: date the window from its end.
            window_start_time = now() - timedelta(seconds=window.size / float(sample_rate))
            task_queue.put((window, window_start_sample, window_start_time))
            frames_since_last_window = 0
    finally:
        task_queue.put(None)
        worker_thread.join(timeout=shutdown_timeout)
        drain_results()
        exported = tracker.flush()
        if exported and on_exported:
            on_exported(exported)
