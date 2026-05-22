from __future__ import annotations

import re
from datetime import datetime
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


def denoise_signal(
    samples: np.ndarray,
    sample_rate: int,
    noise_reference: Path | None = None,
    high_pass_hz: float = 500.0,
    noise_reduction_factor: float = 2.0,
) -> np.ndarray:
    signal = normalize_audio(samples)
    if signal.size == 0:
        return signal

    if noise_reference is None:
        noise_signal = signal
    else:
        noise_signal, _ = load_audio(noise_reference, sample_rate=sample_rate)

    signal_stft = librosa.stft(signal)
    noise_stft = librosa.stft(noise_signal)

    signal_magnitude = np.abs(signal_stft)
    signal_phase = np.angle(signal_stft)
    noise_profile = np.mean(np.abs(noise_stft), axis=1, keepdims=True)

    filtered_magnitude = np.maximum(
        signal_magnitude - noise_reduction_factor * noise_profile,
        0.0,
    )

    freqs = librosa.fft_frequencies(sr=sample_rate)
    filtered_magnitude[freqs < high_pass_hz, :] = 0.0

    rebuilt_stft = filtered_magnitude * np.exp(1j * signal_phase)
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


def export_detection_clips(
    samples: np.ndarray,
    sample_rate: int,
    grouped_detections: dict[str, tuple[float, float, float, str]],
    species_catalog,
    destination_dir: Path,
) -> list[Path]:
    exported_paths: list[Path] = []
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    hour = datetime.now().strftime("%H")
    normalized = normalize_audio(samples)

    for scientific_name, (start_sec, end_sec, confidence, english_name) in grouped_detections.items():
        start_index = max(int(start_sec * sample_rate), 0)
        end_index = min(int(end_sec * sample_rate), len(normalized))
        if end_index <= start_index:
            continue

        italian_name, _, _ = species_catalog.display_names(scientific_name, english_name)
        clip_name = sanitize_filename(italian_name)
        output_path = destination_dir / hour / clip_name / f"{clip_name}_{timestamp}_{confidence:.3f}.wav"
        write_wav_mono(output_path, normalized[start_index:end_index], sample_rate)
        exported_paths.append(output_path)

    return exported_paths
