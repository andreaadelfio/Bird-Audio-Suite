"""Remove from the microphone what the PC itself is playing (videos, music, calls).

The live service also reads the monitor of the default output (what goes to the speakers,
through `pw-record`, or `parec` where PipeWire is missing) and subtracts from the microphone its "imprint": the playback as it reaches
the microphone, learned continuously by an adaptive filter (NLMS in 1024-point subbands with
16 taps, ~85 ms of echo). Only a linear subtraction: unlike the echo cancellers of video calls
there is no suppressor that turns the microphone down while the PC plays, so a bird singing
outside at the same time is kept (tested: a blackbird played from a phone while the PC played
a magpie at full volume was recognised as well as without the filter; the WebRTC canceller of
PipeWire lost it). What the laptop speakers distort cannot be subtracted: in simulation the
playback comes out 20-30 dB lower, on the laptop less. It works on the mic as the sound card gives
it: the system mic volume is compensated afterwards (cli.compensate_system_volume).

The two streams start at different moments: the delay between them is measured by
cross-correlation (GCC-PHAT) while the PC plays, and the filter starts over when it changes. When
the PC starts playing after a pause the first measure comes after 2 s of playback, on that
playback only (not on the silence before it), and is repeated every 2 s until the mic is found.
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
# Safety, learnt from a video call (band-limited voice with pauses) that blew the output up by
# 30 dB: where the reference has no energy (above the codec's band, in pauses) the filter used to
# "explain" the room noise with a near-zero reference and its weights grew without bound.
REGULARISATION = 0.05  # x the long-term reference power per bin: bins without excitation do not learn
MAX_WEIGHT = 2.0  # loudspeaker to mic never gains: a bigger weight is a filter going astray
# A frame coming out with twice the power the mic has had over the last second passes the mic as it
# was, and this many in a row mean a filter gone astray: it starts again. Judged against the
# single frame, the quiet frames right after a loud sound (a pause in a video, between syllables)
# kept resetting the filter whenever the PC reached the mic louder than its reference: ~7 dB
# instead of 20-30.
DIVERGENCE_RATIO = 2.0
DIVERGENCE_PATIENCE = 8
MIC_LEVEL_SMOOTHING = 0.99  # per hop: about the last second
LAG_MARGIN_SECONDS = 0.02  # the filter only looks back: the reference leads the mic by this much
LAG_SEARCH_SECONDS = 0.5
MIN_SHARPNESS = 8.0  # correlation peak vs the rest: below this the mic does not hear the PC
ALIGN_WINDOW_SECONDS = 8.0
ALIGN_EVERY_SECONDS = 10.0
FIRST_ALIGN_SECONDS = 2.0  # playback needed for the first measure after a pause
RETRY_ALIGN_SECONDS = 2.0  # until the mic is found
ACTIVE_RMS = 1e-4  # about -80 dBFS: below this the PC is silent
QUIET_SECONDS = 5.0  # "stopped playing" only after this much silence
GAP_SECONDS = 0.5  # no monitor data for this long: the output is asleep, nothing is playing


class EchoCanceller:
    """Streaming subband NLMS: the mic minus the filtered reference. As many samples come out as
    go in, `latency` samples late."""

    skip_silence = True  # no FFTs while the PC is silent (same output: see the tests)

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
        self._silent_frames = self.taps  # the history holds only silence
        self._reference_power = 0.0  # long-term mean power of a reference bin
        self._overshoot = 0  # frames in a row that came out louder than DIVERGENCE_RATIO x the mic
        self._mic_level = getattr(self, "_mic_level", 0.0)  # the mic's recent power: kept across resets
        self.resets = getattr(self, "resets", 0)

    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        mic_buffer = np.concatenate((self._mic, np.asarray(mic, np.float32)))
        ref_buffer = np.concatenate((self._ref, np.asarray(reference, np.float32)))
        produced = []
        start = 0
        while start + self.frame <= mic_buffer.size:
            mic_frame = mic_buffer[start:start + self.frame]
            ref_frame = ref_buffer[start:start + self.frame]
            self._silent_frames = self._silent_frames + 1 if not ref_frame.any() else 0
            if self.skip_silence and self._silent_frames >= self.taps:
                # the PC is silent and has been for the whole filter: nothing to subtract, the
                # frame goes back as it came in (what the FFT round trip would give), without FFTs
                self._overlap += mic_frame * self.window * self.window / 2
            else:
                spectrum = np.fft.rfft(mic_frame * self.window)
                self.history = np.roll(self.history, 1, axis=0)
                self.history[0] = np.fft.rfft(ref_frame * self.window)
                frame_power = float(np.mean(np.abs(self.history[0]) ** 2))
                if frame_power > 0:
                    self._reference_power = (frame_power if self._reference_power == 0
                                             else 0.99 * self._reference_power + 0.01 * frame_power)
                error = spectrum - (self.weights * self.history).sum(axis=0)
                mic_energy = float(np.sum(np.abs(spectrum) ** 2))
                self._mic_level = (mic_energy if self._mic_level == 0 else
                                   MIC_LEVEL_SMOOTHING * self._mic_level + (1 - MIC_LEVEL_SMOOTHING) * mic_energy)
                overshoot = float(np.sum(np.abs(error) ** 2)) > DIVERGENCE_RATIO * max(mic_energy, self._mic_level) + 1e-20
                self._overshoot = self._overshoot + 1 if overshoot else 0
                if self._overshoot >= DIVERGENCE_PATIENCE:
                    # gone astray: forget what was learnt
                    self.weights[:] = 0
                    self.resets += 1
                    self._overshoot = 0
                else:
                    regularisation = 1e-12 + REGULARISATION * self.taps * self._reference_power
                    power = (np.abs(self.history) ** 2).sum(axis=0) + regularisation
                    self.weights += (self.step * np.conj(self.history) * error / power).astype(np.complex64)
                    magnitude = np.abs(self.weights)
                    too_big = magnitude > MAX_WEIGHT
                    if too_big.any():
                        self.weights[too_big] *= (MAX_WEIGHT / magnitude[too_big]).astype(np.float32)
                # an overshooting frame passes as it came in: never twice as loud as the mic has been
                out = spectrum if overshoot else error
                # sqrt-Hann on both sides at a quarter-frame hop adds up to 2
                self._overlap += np.fft.irfft(out, self.frame).astype(np.float32) * self.window / 2
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
    """What the PC is playing, read from the monitor of the default output.

    With PipeWire the capture stream is passive: it does not keep the output awake, so the
    speakers' amplifier is suspended as usual when nothing plays, and data arrives only while
    something does. Each stretch of playback after such a pause is a new "generation", counted
    from 0 (its timing has nothing to do with the previous one). `parec` (plain PulseAudio) has
    no passive mode: it keeps the output awake and streams silence.
    """

    def __init__(self, sample_rate: int, keep_seconds: float = 10.0) -> None:
        self.sample_rate = sample_rate
        self.ring = np.zeros(int(keep_seconds * sample_rate), np.float32)
        self.count = 0  # samples read in this generation
        self.last_time = 0.0  # when the last ones arrived (time.monotonic)
        self.generation = 0  # a new one after every pause or restart of the reader
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread = threading.Thread(target=self._run, name="playback-monitor", daemon=True)

    @staticmethod
    def available() -> bool:
        return PlaybackMonitor.command(48000) is not None

    @staticmethod
    def command(sample_rate: int) -> list[str] | None:
        if shutil.which("pw-record"):
            return ["pw-record", "--rate", str(sample_rate), "--channels", "1", "--format", "f32",
                    "--latency", "20ms", "-P",
                    "{ stream.capture.sink = true node.passive = true "
                    "node.name = bird-audio-suite-pc-playback media.name = pc-playback-reference }",
                    "-"]
        if shutil.which("parec"):
            return ["parec", "--device=@DEFAULT_MONITOR@", "--raw", "--format=float32le",
                    f"--rate={sample_rate}", "--channels=1", "--latency-msec=20",
                    "--client-name=bird-audio-suite", "--stream-name=pc-playback-reference"]
        return None

    def start(self) -> PlaybackMonitor:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run(self) -> None:
        command = self.command(self.sample_rate)
        while command and not self.stopped.is_set():
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                return
            with self.lock:
                self.last_time = 0.0  # the next data starts a new generation
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
        now = time.monotonic()
        with self.lock:
            if not self.last_time or now - self.last_time > GAP_SECONDS:
                self.ring[:] = 0
                self.count = 0
                self.generation += 1
            samples = samples[-size:]
            index = (self.count + np.arange(samples.size)) % size
            self.ring[index] = samples
            self.count += samples.size
            self.last_time = now

    def position(self) -> tuple[int, float, int]:
        """(samples read, when the last ones arrived, generation)."""
        with self.lock:
            return self.count, self.last_time, self.generation

    def flowing(self) -> bool:
        """Data is arriving: something is playing (or, with parec, the output is awake)."""
        with self.lock:
            return bool(self.last_time) and time.monotonic() - self.last_time <= GAP_SECONDS

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
        # what _align learned about the rough offset (the two streams' latencies differ by a
        # fairly constant amount): applied straight away when playback starts again
        self.correction = 0
        self.generation = -1
        self.history_mic: deque[np.ndarray] = deque()
        self.history_ref: deque[np.ndarray] = deque()
        self.history_size = 0
        self.next_alignment = 0
        self.playing_since = 0  # mic sample at which this stretch of playback began
        self.playing = False
        self.last_sound = -1
        # for the live table: does the mic hear the PC, and how much quieter is it once subtracted
        self.heard_by_mic: bool | None = None  # None: not checked yet in this stretch of playback
        self.sharpness = 0.0
        self.energy_in = 0.0
        self.energy_out = 0.0

    def close(self) -> None:
        self.monitor.stop()

    def process(self, block: np.ndarray, arrived_at: float) -> np.ndarray:
        return self.process_with_reference(block, arrived_at)[0]

    def process_with_reference(self, block: np.ndarray, arrived_at: float) -> tuple[np.ndarray, np.ndarray]:
        """`arrived_at`: time.monotonic() when the block reached the audio callback."""
        block = np.asarray(block, np.float32).reshape(-1)
        start = self.mic_count
        self.mic_count += block.size
        if self.monitor.position()[2] != self.generation:
            self.offset = self._rough_offset(arrived_at)
            self.canceller.reset()
        if self.offset is None or not self.monitor.flowing():
            # nothing playing (the output is asleep): no data will come, do not wait for it
            reference = np.zeros(block.size, np.float32)
        else:
            needed = start + self.offset + block.size
            deadline = time.monotonic() + 0.3
            while self.monitor.position()[0] < needed and time.monotonic() < deadline:
                time.sleep(0.005)
            reference = self.monitor.read(start + self.offset, block.size)
        self._track_playing(reference)
        self._remember(block, reference)
        out = self.canceller.process(block, reference)
        if self.playing:
            # a few seconds of memory: what the mic had against what goes on to BirdNET
            self.energy_in = 0.995 * self.energy_in + float(np.mean(block.astype(np.float64) ** 2))
            self.energy_out = 0.995 * self.energy_out + float(np.mean(out.astype(np.float64) ** 2))
        return out, reference

    def status(self) -> dict:
        reduction = (10 * np.log10(self.energy_in / self.energy_out)
                     if self.energy_in > 0 and self.energy_out > 0 else None)
        return {"playing": self.playing, "heard": self.heard_by_mic, "sharpness": round(self.sharpness, 1),
                "reduction_db": round(float(reduction), 1) if reduction is not None else None}

    def _rough_offset(self, arrived_at: float) -> int | None:
        """From arrival times: tens of ms off, fixed by _align as soon as the PC plays."""
        count, last_time, generation = self.monitor.position()
        if not last_time:
            return None
        self.generation = generation
        reference_now = count + (arrived_at - last_time) * self.sample_rate
        return int(round(reference_now - self.mic_count)) + self.margin + self.correction

    def _track_playing(self, reference: np.ndarray) -> None:
        if reference.size and float(np.sqrt(np.mean(reference ** 2))) > ACTIVE_RMS:
            self.last_sound = self.mic_count
            if not self.playing:
                self.playing = True
                self.playing_since = self.mic_count
                self.next_alignment = self.mic_count + int(FIRST_ALIGN_SECONDS * self.sample_rate)
                self.heard_by_mic = None
                self.energy_in = self.energy_out = 0.0
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
        if self.playing and self.mic_count >= self.next_alignment:
            found = self._align()
            # found: every ALIGN_EVERY_SECONDS; not yet (or headphones): again soon
            every = ALIGN_EVERY_SECONDS if found else RETRY_ALIGN_SECONDS
            self.next_alignment = self.mic_count + int(every * self.sample_rate)

    def _align(self) -> bool:
        """Measure the delay on the last seconds of this playback (never on the silence before
        it); True when the mic hears the PC."""
        playing = min(self.history_size, self.mic_count - self.playing_since)
        if self.offset is None or playing < FIRST_ALIGN_SECONDS * self.sample_rate:
            return False
        reference = np.concatenate(self.history_ref)[-playing:]
        if float(np.sqrt(np.mean(reference ** 2))) < ACTIVE_RMS:
            return False
        lag, sharpness = estimate_lag(np.concatenate(self.history_mic)[-playing:], reference,
                                      int(LAG_SEARCH_SECONDS * self.sample_rate))
        self.sharpness = sharpness
        self.heard_by_mic = sharpness >= MIN_SHARPNESS
        if not self.heard_by_mic:
            return False  # e.g. headphones: the mic does not hear the PC, nothing to line up
        shift = lag - self.margin
        if abs(shift) > self.sample_rate // 500:  # more than 2 ms off: line up and learn again
            self.offset -= shift
            self.correction -= shift
            self.canceller.reset()
            self.log(f"audio del PC riallineato al microfono ({shift / self.sample_rate * 1000:+.0f} ms)")
        return True
