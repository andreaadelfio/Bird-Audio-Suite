"""Undo the system microphone volume, so recordings do not depend on it.

The live records the default PipeWire/PulseAudio source, after its volume. That volume is a
desktop setting that video-call apps also move by themselves: at 45% (-20.8 dB) every clip came
out 21 dB quieter than what the microphone heard. PipeWire applies it in floating point and the
live records float32, so multiplying back is lossless: this reads the volume every 30 s (with
`pactl`, in a thread, never in the audio path) and applies the opposite gain, ramped so a change
does not click. Capped at +30 dB: a microphone turned almost to zero is not worth amplifying.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading

import numpy as np

REFRESH_SECONDS = 30.0
MAX_GAIN_DB = 30.0
# devices that record the default source through the sound server, so after its volume
SOUND_SERVER_DEVICES = ("pulse", "default", "pipewire")


def parse_volume_db(pactl_output: str) -> float | None:
    """Average of the channels' dB in `pactl get-source-volume` ("... / 45% / -20.75 dB, ...")."""
    values = []
    for match in re.findall(r"/\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB", pactl_output):
        values.append(float("-inf") if match == "-inf" else float(match))
    return sum(values) / len(values) if values else None


def read_source_volume_db() -> float | None:
    """The default source's volume in dB, None when unknown or muted."""
    try:
        mute = subprocess.run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"], capture_output=True, text=True,
                              timeout=5).stdout
        if "yes" in mute.lower():
            return None
        output = subprocess.run(["pactl", "get-source-volume", "@DEFAULT_SOURCE@"], capture_output=True, text=True,
                                timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_volume_db(output)


class SourceVolumeCompensation:
    def __init__(self, read_volume=read_source_volume_db, refresh_seconds: float = REFRESH_SECONDS,
                 log=None, start: bool = True) -> None:
        self.read_volume = read_volume
        self.refresh_seconds = refresh_seconds
        self.log = log or (lambda message: None)
        self.volume_db: float | None = None
        self.target_gain = 1.0
        self.gain = 1.0
        self.stopped = threading.Event()
        self.refresh()
        if start:
            threading.Thread(target=self._run, name="mic-volume", daemon=True).start()

    @staticmethod
    def available() -> bool:
        return shutil.which("pactl") is not None

    def _run(self) -> None:
        while not self.stopped.wait(self.refresh_seconds):
            self.refresh()

    def stop(self) -> None:
        self.stopped.set()

    def refresh(self) -> None:
        volume_db = self.read_volume()
        if volume_db is None or volume_db == float("-inf"):
            gain_db = 0.0  # unknown or muted: leave the signal alone
        else:
            gain_db = min(-volume_db, MAX_GAIN_DB)
        if volume_db != self.volume_db:
            if volume_db is not None and abs(gain_db) > 0.1:
                self.log(f"volume del microfono di sistema {volume_db:.1f} dB: compenso {gain_db:+.1f} dB")
            self.volume_db = volume_db
        self.target_gain = float(10 ** (gain_db / 20))

    @property
    def gain_db(self) -> float:
        return float(20 * np.log10(self.gain))

    def apply(self, frame: np.ndarray) -> np.ndarray:
        frame = np.asarray(frame, np.float32)
        if self.gain == self.target_gain:
            return frame if self.gain == 1.0 else frame * np.float32(self.gain)
        # a volume change: ramp over this frame, no click
        ramp = np.linspace(self.gain, self.target_gain, frame.size, dtype=np.float32)
        self.gain = self.target_gain
        return frame * ramp
