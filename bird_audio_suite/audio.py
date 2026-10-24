from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import librosa
import numpy as np
import scipy.io.wavfile as wavfile


def normalize_audio(samples: np.ndarray) -> np.ndarray:
    array = np.asarray(samples)
    if array.size == 0:
        return np.asarray([], dtype=np.float32)

    if np.issubdtype(array.dtype, np.integer):
        max_value = max(abs(np.iinfo(array.dtype).min), np.iinfo(array.dtype).max)
        return array.astype(np.float32) / float(max_value)

    return array.astype(np.float32)


def to_int16(samples: np.ndarray) -> np.ndarray:
    normalized = normalize_audio(samples)
    clipped = np.clip(normalized, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def rms_level(samples: np.ndarray) -> float:
    normalized = normalize_audio(samples)
    if normalized.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(normalized))))


def write_wav_mono(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, sample_rate, to_int16(samples))


def load_audio(path: Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    samples, loaded_rate = librosa.load(
        str(path),
        sr=sample_rate,
        mono=True,
        res_type="kaiser_fast",
    )
    return samples.astype(np.float32), int(loaded_rate)


@lru_cache(maxsize=4)
def noise_profile(noise_reference: Path, sample_rate: int) -> np.ndarray | None:
    """Mean spectrum of a noise recording, read once: the live denoises every window with it."""
    noise_signal, _ = load_audio(noise_reference, sample_rate=sample_rate)
    if not noise_signal.size:
        return None
    return np.mean(np.abs(librosa.stft(noise_signal)), axis=1, keepdims=True)


def denoise_signal(
    samples: np.ndarray,
    sample_rate: int,
    noise_reference: Path | None = None,
    high_pass_hz: float = 500.0,
    low_pass_hz: float = 5000.0,
    noise_reduction_factor: float = 2.0,
) -> np.ndarray:
    signal = normalize_audio(samples)
    if signal.size == 0:
        return signal

    signal_stft = librosa.stft(signal)

    signal_magnitude = np.abs(signal_stft)
    signal_phase = np.angle(signal_stft)
    profile = noise_profile(Path(noise_reference), int(sample_rate)) if noise_reference is not None else None
    if profile is None:
        profile = np.zeros((signal_stft.shape[0], 1), dtype=np.float32)

    filtered_magnitude = np.maximum(
        signal_magnitude - noise_reduction_factor * profile,
        0.0,
    )

    freqs = librosa.fft_frequencies(sr=sample_rate)
    filtered_magnitude[freqs < high_pass_hz, :] = 0.0
    filtered_magnitude[freqs > low_pass_hz, :] = 0.0

    rebuilt_stft = filtered_magnitude * np.exp(1j * signal_phase)
    denoised = librosa.istft(rebuilt_stft, length=len(signal))
    return denoised.astype(np.float32)

def cut_signal_freq(
    samples: np.ndarray,
    sample_rate: int,
    high_pass_hz: float = 500.0,
    low_pass_hz: float = 5000.0,
) -> np.ndarray:
    signal = normalize_audio(samples)
    if signal.size == 0:
        return signal

    signal_stft = librosa.stft(signal)
    signal_magnitude = np.abs(signal_stft)
    signal_phase = np.angle(signal_stft)

    freqs = librosa.fft_frequencies(sr=sample_rate)
    signal_magnitude[freqs < high_pass_hz, :] = 0.0
    signal_magnitude[freqs > low_pass_hz, :] = 0.0

    rebuilt_stft = signal_magnitude * np.exp(1j * signal_phase)
    denoised = librosa.istft(rebuilt_stft, length=len(signal))
    return denoised.astype(np.float32)

def aggregate_detections_by_species(
    detections: list[dict],
    duration_seconds: float,
    min_confidence: float,
) -> dict[str, tuple[float, float, float, str]]:
    grouped: dict[str, tuple[float, float, float, str]] = {}

    for detection in detections:
        confidence = float(detection.get("confidence", 0.0))
        if confidence < min_confidence:
            continue

        scientific_name = detection.get("scientific_name", "")
        english_name = detection.get("common_name", "")
        start_sec = max(float(detection.get("start_time", 0.0)), 0.0)
        end_sec = min(float(detection.get("end_time", 0.0)), float(duration_seconds))
        if end_sec <= start_sec:
            end_sec = min(start_sec + 1.0, float(duration_seconds) or start_sec + 1.0)

        previous = grouped.get(scientific_name)
        if previous is None:
            grouped[scientific_name] = (
                start_sec,
                end_sec,
                round(confidence, 3),
                english_name,
            )
            continue

        grouped[scientific_name] = (
            min(start_sec, previous[0]),
            max(end_sec, previous[1]),
            max(round(confidence, 3), previous[2]),
            english_name or previous[3],
        )

    return grouped


Segment = tuple[float, float, float]  # start s, end s, confidence


def merge_segments(segments: list[Segment], gap: float = 0.05) -> list[Segment]:
    """Join overlapping (or touching) bands, keeping the highest confidence."""
    merged: list[list[float]] = []
    for start, end, confidence in sorted(segments):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = max(merged[-1][2], confidence)
        else:
            merged.append([start, end, confidence])
    return [(start, end, confidence) for start, end, confidence in merged]


def detection_segments(detections: list[dict], min_confidence: float) -> dict[str, list[Segment]]:
    """Where BirdNET heard each species (its 3 s chunks), in the time of the analysed audio."""
    by_species: dict[str, list[Segment]] = {}
    for detection in detections:
        confidence = float(detection.get("confidence", 0.0))
        if confidence < min_confidence:
            continue
        by_species.setdefault(detection.get("scientific_name", ""), []).append(
            (float(detection.get("start_time", 0.0)), float(detection.get("end_time", 0.0)), confidence)
        )
    return {name: merge_segments(segments) for name, segments in by_species.items()}


def birdnet_candidates(detections: list[dict], top: int = 3) -> list[list]:
    """BirdNET's most likely species in a clip: [[scientific, english, best confidence], ...]."""
    best: dict[str, tuple[float, str]] = {}
    for detection in detections:
        name = detection.get("scientific_name", "")
        confidence = float(detection.get("confidence", 0.0))
        if name and confidence > best.get(name, (0.0, ""))[0]:
            best[name] = (confidence, detection.get("common_name", ""))
    ranked = sorted(best.items(), key=lambda item: -item[1][0])[:top]
    return [[name, english, round(confidence, 3)] for name, (confidence, english) in ranked]


def clip_relative_segments(segments: list[Segment], clip_start: float, clip_end: float) -> list[list[float]]:
    """Bands inside a clip, in seconds from its start (what the spectrogram highlights)."""
    relative = []
    for start, end, confidence in merge_segments(segments):
        if end <= clip_start or start >= clip_end:
            continue
        relative.append([round(max(start, clip_start) - clip_start, 2), round(min(end, clip_end) - clip_start, 2),
                         round(confidence, 3)])
    return relative


def apply_clip_span_policy(
    grouped_detections: dict[str, tuple[float, float, float, str]],
    duration_seconds: float,
    clip_span: str = "detection",
) -> dict[str, tuple[float, float, float, str]]:
    adjusted: dict[str, tuple[float, float, float, str]] = {}
    slice_end = max(float(duration_seconds), 0.0)

    for scientific_name, (start_sec, end_sec, confidence, english_name) in grouped_detections.items():
        next_start = max(0.0, start_sec)
        next_end = max(next_start, end_sec)

        if clip_span == "full_slice":
            next_start = 0.0
            next_end = slice_end
        elif clip_span == "from_detection":
            next_end = slice_end

        if next_end <= next_start:
            next_end = max(next_start, slice_end)

        adjusted[scientific_name] = (
            next_start,
            next_end,
            confidence,
            english_name,
        )

    return adjusted


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", value.strip(), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ._") or "detection"


_FILENAME_TIMESTAMP_PATTERNS = (
    # 20260601_063000, 20260601-063000, 20260601T063000 (AudioMoth, Song Meter, our own clips)
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    # 2026-06-01 06-30-00, 2026-06-01_06.30.00, 2026-06-01T06:30:00 (phone recorders)
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[_\-T ](\d{2})[\-:.](\d{2})[\-:.](\d{2})(?!\d)"),
)


def parse_timestamp_from_name(name: str) -> datetime | None:
    """Return the first valid date-time embedded in a file name, if any."""
    for pattern in _FILENAME_TIMESTAMP_PATTERNS:
        for match in pattern.finditer(name):
            try:
                return datetime(*(int(part) for part in match.groups()))
            except ValueError:
                continue
    return None


def replace_durably(partial: Path, final: Path) -> None:
    """Give a finished file its name only once it is on disk: after a power cut (the laptop's
    battery) a clip is whole or not there, never an empty WAV next to a JSON pointing at it."""
    with Path(partial).open("rb+") as handle:
        os.fsync(handle.fileno())
    Path(partial).replace(final)


@contextmanager
def detections_lock(detections_dir: Path, blocking: bool = True):
    """Exclusive lock on the transit folder, so two uploaders (the live service and the `sync`
    timer) never send the same clip at once. Non-blocking: yields False if it is taken."""
    detections_dir = Path(detections_dir)
    detections_dir.mkdir(parents=True, exist_ok=True)
    with (detections_dir / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def export_detection_clips(
    samples: np.ndarray,
    sample_rate: int,
    grouped_detections: dict[str, tuple[float, float, float, str]],
    species_names,
    detections_dir: Path,
    latitude: float | None = None,
    longitude: float | None = None,
    place_name: str = "",
    observed_at: datetime | None = None,
    segments: dict[str, list[Segment]] | None = None,
) -> list[Path]:
    """Write one clip per species into the transit folder, ready for Supabase.

    ``segments`` (per species, in the time of ``samples``) are where BirdNET heard it: they
    are stored relative to the clip, so the site highlights them on the spectrogram.

    Each clip is a WAV plus a JSON with what Supabase needs (species, time, confidence,
    place). The JSON is written last and atomically: an uploader never sees half a clip.
    ``observed_at`` is the wall-clock time of the first sample of ``samples``; each species
    is timestamped at ``observed_at + start_sec``.
    """
    exported_paths: list[Path] = []
    detections_dir = Path(detections_dir)
    detections_dir.mkdir(parents=True, exist_ok=True)
    recording_start = observed_at or datetime.now()
    normalized = normalize_audio(samples)

    for scientific_name, (start_sec, end_sec, confidence, english_name) in grouped_detections.items():
        start_index = max(int(start_sec * sample_rate), 0)
        end_index = min(int(end_sec * sample_rate), len(normalized))
        if end_index <= start_index:
            continue

        heard_at = (recording_start + timedelta(seconds=float(start_sec))).astimezone()
        italian_name, _german_name, english_name = species_names.display_names(scientific_name, english_name)
        clip_name = sanitize_filename(italian_name)
        output_path = detections_dir / f"{clip_name}_{heard_at:%Y%m%d_%H%M%S}_{confidence:.3f}.wav"
        partial_wav = output_path.with_suffix(".wav.part")
        write_wav_mono(partial_wav, normalized[start_index:end_index], sample_rate)
        replace_durably(partial_wav, output_path)
        sidecar = {
            "id": output_path.name,
            "scientific_name": scientific_name,
            "english_name": english_name,
            "recorded_at": heard_at.isoformat(),
            "confidence": round(float(confidence), 3),
            "duration_s": round((end_index - start_index) / float(sample_rate), 2),
            "place_name": place_name,
            "latitude": latitude,
            "longitude": longitude,
            "segments": clip_relative_segments((segments or {}).get(scientific_name, []), float(start_sec),
                                               end_index / float(sample_rate)),
        }
        partial_json = output_path.with_suffix(".json.part")
        partial_json.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        replace_durably(partial_json, output_path.with_suffix(".json"))
        exported_paths.append(output_path)

    return exported_paths

def plot_spectrogram(samples: np.ndarray, sample_rate: int) -> None:
    import librosa.display
    import matplotlib.pyplot as plt

    stft = librosa.stft(samples)
    magnitude_db = librosa.amplitude_to_db(np.abs(stft), ref=np.max)
    plt.figure(figsize=(10, 6))
    librosa.display.specshow(magnitude_db, sr=sample_rate, x_axis="time", y_axis="hz")
    plt.colorbar(format="%+2.0f dB")
    plt.title("Spectrogram")
    plt.tight_layout()
    plt.show()

def raise_intensity_sound(samples: np.ndarray, factor: float) -> np.ndarray:
    normalized = normalize_audio(samples)
    amplified = normalized * factor
    clipped = np.clip(amplified, -1.0, 1.0)
    return clipped.astype(np.float32)


if __name__ == "__main__":
    # this is to denoise an audio file using a mean of the spectrum as noise profile and writing the result to a new file
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=str, help="Path to the input audio file")
    parser.add_argument("--output", default='', type=str, help="Path to the output audio file")
    parser.add_argument("--noise-reference", type=Path, help="Path to an optional noise reference audio file")
    parser.add_argument("--high-pass-hz", type=float, default=2700.0, help="High-pass filter cutoff frequency in Hz")
    parser.add_argument("--low-pass-hz", type=float, default=3200.0, help="Low-pass filter cutoff frequency in Hz")
    parser.add_argument("--noise-reduction-factor", type=float, default=2.0, help="Factor by which to reduce the noise profile")
    parser.add_argument("--plot", action="store_true", help="Plot the spectrogram of the audio file")
    args = parser.parse_args()
    if not args.output:
        args.output = args.input + "_denoised.wav"
    try:
        samples, sample_rate = load_audio(args.input)
        if args.plot:
            plot_spectrogram(samples, sample_rate)
        samples = cut_signal_freq(
            samples,
            sample_rate,
            high_pass_hz=args.high_pass_hz,
            low_pass_hz=args.low_pass_hz,
        )
        if args.plot:
            plot_spectrogram(samples, sample_rate)
        samples = raise_intensity_sound(samples, factor=5.5)

        write_wav_mono(args.input + "_cut.wav", samples, sample_rate)

        denoised = denoise_signal(
            samples,
            sample_rate,
            noise_reference=args.noise_reference,
            high_pass_hz=args.high_pass_hz,
            low_pass_hz=args.low_pass_hz,
            noise_reduction_factor=args.noise_reduction_factor,
        )
        denoised = raise_intensity_sound(denoised, factor=20.5)

        if args.plot:
            plot_spectrogram(denoised, sample_rate)

        write_wav_mono(args.output, denoised, sample_rate)
        print(f"Denoised audio written to: {args.output}")
    except Exception as e:
        print(f"Error processing audio: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
    # Example usage:
    # python audio.py input.wav output_denoised.wav --noise-reference noise.wav --high-pass-hz 500 --noise-reduction-factor 2.0
