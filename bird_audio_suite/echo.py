"""Remove from the microphone what the PC itself is playing (videos, music, calls).

The live service also reads the monitor of the default output (what goes to the speakers,
through `parec`) and subtracts from the microphone its "imprint": the playback as it reaches
the microphone, learned continuously by an adaptive filter (NLMS in 1024-point subbands with
16 taps, ~85 ms of echo). Only a linear subtraction: unlike the echo cancellers of video calls
there is no suppressor that turns the microphone down while the PC plays, so a bird singing
outside at the same time is kept (tested: a blackbird played from a phone while the PC played
a magpie at full volume was recognised as well as without the filter; the WebRTC canceller of
PipeWire lost it). What the laptop speakers distort cannot be subtracted: expect the playback
6-10 dB lower, not gone.

The two streams start at different moments: the delay between them is measured by
cross-correlation (GCC-PHAT) while the PC plays, and the filter starts over when it changes.
With nothing playing the reference is silence and the microphone passes through unchanged
(perfect-reconstruction STFT, 21 ms of latency).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

FRAME = 1024
HOP = 256
TAPS = 16
STEP = 0.2
LAG_MARGIN_SECONDS = 0.02  # the filter only looks back: the reference leads the mic by this much
LAG_SEARCH_SECONDS = 0.5
MIN_SHARPNESS = 8.0  # correlation peak vs the rest: below this the mic does not hear the PC
ALIGN_WINDOW_SECONDS = 8.0
ALIGN_EVERY_SECONDS = 10.0
ACTIVE_RMS = 1e-4  # about -80 dBFS: below this the PC is silent
QUIET_SECONDS = 5.0  # "stopped playing" only after this much silence


class EchoCanceller:
    """Streaming subband NLMS: the mic minus the filtered reference. As many samples come out as
    go in, `latency` samples late."""

    def __init__(self, frame: int = FRAME, hop: int = HOP, taps: int = TAPS, step: float = STEP) -> None:
        self.frame, self.hop, self.taps, self.step = frame, hop, taps, step
        self.window = np.sqrt(np.hanning(frame + 1)[:frame]).astype(np.float32)
        self.latency = frame
        self._mic = np.zeros(frame - hop, np.float32)
        self._ref = np.zeros(frame - hop, np.float32)
        self._overlap = np.zeros(frame, np.float32)
        # frames come out a hop at a time: one hop of silence in front keeps out == in
        self._ready = np.zeros(hop, np.float32)
        self.reset()

    def reset(self) -> None:
        bins = self.frame // 2 + 1
        self.weights = np.zeros((self.taps, bins), np.complex64)
        self.history = np.zeros((self.taps, bins), np.complex64)

    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        mic_buffer = np.concatenate((self._mic, np.asarray(mic, np.float32)))
        ref_buffer = np.concatenate((self._ref, np.asarray(reference, np.float32)))
        floor = 1e-10 + 1e-4 * self.frame * float(np.mean(ref_buffer ** 2))
        produced = []
        start = 0
        while start + self.frame <= mic_buffer.size:
            spectrum = np.fft.rfft(mic_buffer[start:start + self.frame] * self.window)
            self.history = np.roll(self.history, 1, axis=0)
            self.history[0] = np.fft.rfft(ref_buffer[start:start + self.frame] * self.window)
            error = spectrum - (self.weights * self.history).sum(axis=0)
            power = (np.abs(self.history) ** 2).sum(axis=0) + floor
            self.weights += (self.step * np.conj(self.history) * error / power).astype(np.complex64)
            # sqrt-Hann on both sides at a quarter-frame hop adds up to 2
            self._overlap += np.fft.irfft(error, self.frame).astype(np.float32) * self.window / 2
            produced.append(self._overlap[:self.hop].copy())
            self._overlap = np.concatenate((self._overlap[self.hop:], np.zeros(self.hop, np.float32)))
            start += self.hop
        self._mic, self._ref = mic_buffer[start:], ref_buffer[start:]
        self._ready = np.concatenate([self._ready, *produced])
        out, self._ready = self._ready[:len(mic)], self._ready[len(mic):]
        return out


def estimate_lag(mic: np.ndarray, reference: np.ndarray, max_lag: int) -> tuple[int, float]:
    """How many samples the mic lags the reference (mic[n] ~ reference[n - lag]), and how sharp
    the correlation peak is compared with the rest."""
    size = 1 << int(np.ceil(np.log2(mic.size + reference.size)))
    cross = np.fft.rfft(mic, size) * np.conj(np.fft.rfft(reference, size))
    cross /= np.abs(cross) + 1e-12
    correlation = np.fft.irfft(cross, size)
    window = np.concatenate((correlation[-max_lag:], correlation[:max_lag + 1]))
    peak = int(np.argmax(window))
    rest = np.delete(window, slice(max(peak - 50, 0), peak + 50))
    sharpness = float(window[peak] / (np.std(rest) + 1e-12))
    return peak - max_lag, sharpness


class PlaybackMonitor:
    """What the PC is playing, read with `parec` from the monitor of the default output."""

    def __init__(self, sample_rate: int, keep_seconds: float = 10.0) -> None:
        self.sample_rate = sample_rate
        self.ring = np.zeros(int(keep_seconds * sample_rate), np.float32)
        self.count = 0  # samples read since this parec started
        self.last_time = 0.0  # when the last ones arrived (time.monotonic)
        self.generation = 0  # a restarted parec counts from 0 again
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread = threading.Thread(target=self._run, name="playback-monitor", daemon=True)

    @staticmethod
    def available() -> bool:
        return shutil.which("parec") is not None

    def start(self) -> PlaybackMonitor:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run(self) -> None:
        command = ["parec", "--device=@DEFAULT_MONITOR@", "--raw", "--format=float32le",
                   f"--rate={self.sample_rate}", "--channels=1", "--latency-msec=20",
                   "--client-name=bird-audio-suite", "--stream-name=pc-playback-reference"]
        while not self.stopped.is_set():
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                return
            with self.lock:
                self.count, self.last_time = 0, 0.0
                self.generation += 1
            leftover = b""
            while not self.stopped.is_set():
                data = self.process.stdout.read1(16384)
                if not data:
                    break
                data = leftover + data
                usable = len(data) - len(data) % 4
                leftover = data[usable:]
                self._append(np.frombuffer(data[:usable], dtype="<f4"))
            if self.process.poll() is None:
                self.process.terminate()
            self.stopped.wait(2.0)  # e.g. PipeWire restarting: try again

    def _append(self, samples: np.ndarray) -> None:
        size = self.ring.size
        with self.lock:
            samples = samples[-size:]
            index = (self.count + np.arange(samples.size)) % size
            self.ring[index] = samples
            self.count += samples.size
            self.last_time = time.monotonic()

    def position(self) -> tuple[int, float, int]:
        """(samples read, when the last ones arrived, parec generation)."""
        with self.lock:
            return self.count, self.last_time, self.generation

    def read(self, start: int, length: int) -> np.ndarray:
        """Samples [start, start + length); silence for what is not (or no longer) there."""
        out = np.zeros(length, np.float32)
        with self.lock:
            lo, hi = max(start, self.count - self.ring.size, 0), min(start + length, self.count)
            if hi > lo:
                out[lo - start:hi - start] = self.ring[np.arange(lo, hi) % self.ring.size]
        return out


class PlaybackSubtractor:
    """For the live loop: lines up the PC playback with each mic block and subtracts it."""

    def __init__(self, sample_rate: int, monitor: PlaybackMonitor | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.sample_rate = sample_rate
        self.monitor = monitor if monitor is not None else PlaybackMonitor(sample_rate).start()
        self.canceller = EchoCanceller()
        self.log = log or (lambda message: None)
        self.margin = int(LAG_MARGIN_SECONDS * sample_rate)
        self.mic_count = 0
        self.offset: int | None = None  # reference index = mic index + offset
        self.generation = -1
        self.history_mic: deque[np.ndarray] = deque()
        self.history_ref: deque[np.ndarray] = deque()
        self.history_size = 0
        self.next_alignment = 0
        self.playing = False
        self.last_sound = -1

    def close(self) -> None:
        self.monitor.stop()

    def process(self, block: np.ndarray, arrived_at: float) -> np.ndarray:
        """`arrived_at`: time.monotonic() when the block reached the audio callback."""
        block = np.asarray(block, np.float32).reshape(-1)
        start = self.mic_count
        self.mic_count += block.size
        if self.monitor.position()[2] != self.generation:
            self.offset = self._rough_offset(arrived_at)
            self.canceller.reset()
        if self.offset is None:
            reference = np.zeros(block.size, np.float32)
        else:
            needed = start + self.offset + block.size
            deadline = time.monotonic() + 0.3
            while self.monitor.position()[0] < needed and time.monotonic() < deadline:
                time.sleep(0.005)
            reference = self.monitor.read(start + self.offset, block.size)
        self._track_playing(reference)
        self._remember(block, reference)
        return self.canceller.process(block, reference)

    def _rough_offset(self, arrived_at: float) -> int | None:
        """From arrival times: tens of ms off, fixed by _align as soon as the PC plays."""
        count, last_time, generation = self.monitor.position()
        if not last_time:
            return None
        self.generation = generation
        reference_now = count + (arrived_at - last_time) * self.sample_rate
        return int(round(reference_now - self.mic_count)) + self.margin

    def _track_playing(self, reference: np.ndarray) -> None:
        if reference.size and float(np.sqrt(np.mean(reference ** 2))) > ACTIVE_RMS:
            self.last_sound = self.mic_count
            if not self.playing:
                self.playing = True
                self.log("il PC sta suonando: lo sottraggo dal microfono")
        elif self.playing and self.mic_count - self.last_sound > QUIET_SECONDS * self.sample_rate:
            self.playing = False
            self.log("il PC ha smesso di suonare")

    def _remember(self, block: np.ndarray, reference: np.ndarray) -> None:
        self.history_mic.append(block)
        self.history_ref.append(reference)
        self.history_size += block.size
        window = int(ALIGN_WINDOW_SECONDS * self.sample_rate)
        while self.history_size - self.history_mic[0].size >= window:
            self.history_size -= self.history_mic.popleft().size
            self.history_ref.popleft()
        if self.playing and self.mic_count >= self.next_alignment and self.history_size >= window:
            self.next_alignment = self.mic_count + int(ALIGN_EVERY_SECONDS * self.sample_rate)
            self._align()

    def _align(self) -> None:
        reference = np.concatenate(self.history_ref)
        if self.offset is None or float(np.sqrt(np.mean(reference ** 2))) < ACTIVE_RMS:
            return
        lag, sharpness = estimate_lag(np.concatenate(self.history_mic), reference,
                                      int(LAG_SEARCH_SECONDS * self.sample_rate))
        if sharpness < MIN_SHARPNESS:
            return  # e.g. headphones: the mic does not hear the PC, nothing to line up
        shift = lag - self.margin
        if abs(shift) > self.sample_rate // 500:  # more than 2 ms off: line up and learn again
            self.offset -= shift
            self.canceller.reset()
            self.log(f"audio del PC riallineato al microfono ({shift / self.sample_rate * 1000:+.0f} ms)")
