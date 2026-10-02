from __future__ import annotations

import argparse
import csv
import json
import queue
import shlex
import tempfile
import traceback
import os
from datetime import datetime
from pathlib import Path

from .audio import (
    append_inaturalist_csv_row,
    apply_clip_span_policy,
    aggregate_detections_by_species,
    denoise_signal,
    export_detection_clips,
    load_audio,
    rms_level,
    write_wav_mono,
)
from .config import (
    DEFAULT_BATCH_CLIP_SPAN,
    DEFAULT_BATCH_MIN_CONFIDENCE,
    DEFAULT_AUDIO_DEVICE_STATE_FILE,
    DEFAULT_DETECTIONS_DIR,
    DEFAULT_DENOISE_HIGH_PASS_HZ,
    DEFAULT_DENOISE_REDUCTION_FACTOR,
    DEFAULT_DEVICE_MIN_RMS,
    DEFAULT_ENABLE_AUTO_LOCATION,
    DEFAULT_FRAME_LENGTH,
    DEFAULT_HIGH_PASS_HZ,
    DEFAULT_INATURALIST_GEOPRIVACY,
    DEFAULT_INATURALIST_TAGS,
    DEFAULT_LATITUDE,
    DEFAULT_LIVE_BACKEND,
    DEFAULT_LIVE_CLIP_SPAN,
    DEFAULT_LIVE_DEVICE_INDEX,
    DEFAULT_LIVE_MIN_CONFIDENCE,
    DEFAULT_LOCATION_LOOKUP_TIMEOUT_SECONDS,
    DEFAULT_LONGITUDE,
    DEFAULT_PLACE_NAME,
    DEFAULT_RECORDER_SAMPLE_RATE,
    DEFAULT_SLICE_INTERVAL,
    DEFAULT_SPECIES_FILE,
    DEFAULT_NOISE_REDUCTION_FACTOR,
)
from .detector import BirdNetDetector
from .geolocation import resolve_location
from .inaturalist import DEFAULT_API_BASE_URL, import_csv, resolve_jwt_token
from .species import SpeciesCatalog


_DAILY_SUMMARY_LINE_COUNT = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bird Audio Suite: batch analysis, live recognition, and denoise.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    batch_parser = subparsers.add_parser(
        "batch",
        help="Analyze existing WAV files with BirdNET.",
    )
    batch_parser.add_argument(
        "--files",
        nargs="+",
        help="Specific WAV files to analyze.",
    )
    batch_parser.add_argument(
        "--directory",
        type=Path,
        help="Directory containing WAV files.",
    )
    batch_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search recursively when using --directory.",
    )
    batch_parser.add_argument(
        "--lat",
        type=float,
        default=DEFAULT_LATITUDE,
        help=f"Latitude for BirdNET context. Default: {DEFAULT_LATITUDE}",
    )
    batch_parser.add_argument(
        "--lon",
        type=float,
        default=DEFAULT_LONGITUDE,
        help=f"Longitude for BirdNET context. Default: {DEFAULT_LONGITUDE}",
    )
    batch_parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_BATCH_MIN_CONFIDENCE,
        help=f"BirdNET minimum confidence. Default: {DEFAULT_BATCH_MIN_CONFIDENCE}",
    )
    batch_parser.add_argument(
        "--denoise",
        action="store_true",
        help="Denoise the input before detection.",
    )
    batch_parser.add_argument(
        "--noise-ref",
        type=Path,
        help="Optional noise reference WAV for denoise.",
    )
    batch_parser.add_argument(
        "--high-pass-hz",
        type=float,
        default=DEFAULT_HIGH_PASS_HZ,
        help=f"High-pass cutoff for denoise. Default: {DEFAULT_HIGH_PASS_HZ}",
    )
    batch_parser.add_argument(
        "--noise-reduction-factor",
        type=float,
        default=DEFAULT_NOISE_REDUCTION_FACTOR,
        help=f"Denoise strength. Default: {DEFAULT_NOISE_REDUCTION_FACTOR}",
    )
    batch_parser.add_argument(
        "--export-clips",
        action="store_true",
        help="Export grouped detection clips.",
    )
    batch_parser.add_argument(
        "--clip-span",
        choices=("detection", "from_detection", "full_slice"),
        default=DEFAULT_BATCH_CLIP_SPAN,
        help=f"Span for exported clips. Default: {DEFAULT_BATCH_CLIP_SPAN}",
    )
    batch_parser.add_argument(
        "--species-file",
        type=Path,
        default=DEFAULT_SPECIES_FILE,
        help=f"Species cache file. Default: {DEFAULT_SPECIES_FILE}",
    )
    batch_parser.add_argument(
        "--detections-dir",
        type=Path,
        default=DEFAULT_DETECTIONS_DIR,
        help=f"Destination for exported clips. Default: {DEFAULT_DETECTIONS_DIR}",
    )

    live_parser = subparsers.add_parser(
        "live",
        help="Listen from microphone and export detections.",
    )
    live_parser.add_argument(
        "--lat",
        type=float,
        default=DEFAULT_LATITUDE,
        help=f"Latitude for BirdNET context. Default: {DEFAULT_LATITUDE}",
    )
    live_parser.add_argument(
        "--lon",
        type=float,
        default=DEFAULT_LONGITUDE,
        help=f"Longitude for BirdNET context. Default: {DEFAULT_LONGITUDE}",
    )
    live_parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_LIVE_MIN_CONFIDENCE,
        help=f"BirdNET minimum confidence. Default: {DEFAULT_LIVE_MIN_CONFIDENCE}",
    )
    live_parser.add_argument(
        "--frame-length",
        type=int,
        default=DEFAULT_FRAME_LENGTH,
        help=f"Recorder frame length. Default: {DEFAULT_FRAME_LENGTH}",
    )
    live_parser.add_argument(
        "--slice-interval",
        type=int,
        default=DEFAULT_SLICE_INTERVAL,
        help=f"Number of frames per analysis slice. Default: {DEFAULT_SLICE_INTERVAL}",
    )
    live_parser.add_argument(
        "--device-index",
        type=int,
        default=DEFAULT_LIVE_DEVICE_INDEX,
        help=f"Input device index for the selected backend. Default: {DEFAULT_LIVE_DEVICE_INDEX}",
    )
    live_parser.add_argument(
        "--device-probe-seconds",
        type=float,
        default=1.0,
        help="Seconds to listen to each input when selecting the device automatically.",
    )
    live_parser.add_argument(
        "--backend",
        choices=("sounddevice", "pvrecorder", "auto"),
        default=DEFAULT_LIVE_BACKEND,
        help=f"Audio backend for live mode. Default: {DEFAULT_LIVE_BACKEND}",
    )
    live_parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices for the selected backend and exit.",
    )
    live_parser.add_argument(
        "--enable-denoise",
        dest="enable_denoise",
        action="store_true",
        default=False,
        help="Enable denoise before detection.",
    )
    live_parser.add_argument(
        "--disable-denoise",
        dest="enable_denoise",
        action="store_false",
        help="Disable denoise before detection.",
    )
    live_parser.add_argument(
        "--noise-ref",
        type=Path,
        help="Optional noise reference WAV for live denoise.",
    )
    live_parser.add_argument(
        "--high-pass-hz",
        type=float,
        default=DEFAULT_HIGH_PASS_HZ,
        help=f"High-pass cutoff for live denoise. Default: {DEFAULT_HIGH_PASS_HZ}",
    )
    live_parser.add_argument(
        "--noise-reduction-factor",
        type=float,
        default=DEFAULT_NOISE_REDUCTION_FACTOR,
        help=f"Denoise strength. Default: {DEFAULT_NOISE_REDUCTION_FACTOR}",
    )
    live_parser.add_argument(
        "--species-file",
        type=Path,
        default=DEFAULT_SPECIES_FILE,
        help=f"Species cache file. Default: {DEFAULT_SPECIES_FILE}",
    )
    live_parser.add_argument(
        "--detections-dir",
        type=Path,
        default=DEFAULT_DETECTIONS_DIR,
        help=f"Destination for exported clips. Default: {DEFAULT_DETECTIONS_DIR}",
    )
    live_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra diagnostics for each live slice.",
    )
    live_parser.add_argument(
        "--clip-span",
        choices=("detection", "from_detection", "full_slice"),
        default=DEFAULT_LIVE_CLIP_SPAN,
        help=f"Span for exported live clips. Default: {DEFAULT_LIVE_CLIP_SPAN}",
    )

    denoise_parser = subparsers.add_parser(
        "denoise",
        help="Denoise a WAV file and optionally plot the result.",
    )
    denoise_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Input WAV file.",
    )
    denoise_parser.add_argument(
        "--output",
        type=Path,
        help="Output WAV file. Default: <input>_denoised.wav",
    )
    denoise_parser.add_argument(
        "--noise-ref",
        type=Path,
        help="Optional noise reference WAV.",
    )
    denoise_parser.add_argument(
        "--high-pass-hz",
        type=float,
        default=DEFAULT_DENOISE_HIGH_PASS_HZ,
        help=f"High-pass cutoff. Default: {DEFAULT_DENOISE_HIGH_PASS_HZ}",
    )
    denoise_parser.add_argument(
        "--noise-reduction-factor",
        type=float,
        default=DEFAULT_DENOISE_REDUCTION_FACTOR,
        help=f"Denoise strength. Default: {DEFAULT_DENOISE_REDUCTION_FACTOR}",
    )
    denoise_parser.add_argument(
        "--plot",
        action="store_true",
        help="Show spectrogram comparison.",
    )

    import_parser = subparsers.add_parser(
        "inat-import",
        help="Import an iNaturalist CSV and attach the referenced audio files.",
    )
    import_parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Path to an iNaturalist-compatible CSV generated by Bird Audio Suite.",
    )
    import_parser.add_argument(
        "--base-url",
        default=DEFAULT_API_BASE_URL,
        help=f"iNaturalist API base URL. Default: {DEFAULT_API_BASE_URL}",
    )
    import_parser.add_argument(
        "--token",
        help="Explicit iNaturalist JWT token. Overrides --token-env if provided.",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned import actions without calling the iNaturalist API.",
    )

    return parser


def prompt_text(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{message}{suffix}: ").strip()
    if raw:
        return raw
    return default or ""


def get_default_live_args() -> argparse.Namespace:
    return argparse.Namespace(
        command="live",
        backend=DEFAULT_LIVE_BACKEND,
        lat=DEFAULT_LATITUDE,
        lon=DEFAULT_LONGITUDE,
        min_confidence=DEFAULT_LIVE_MIN_CONFIDENCE,
        frame_length=DEFAULT_FRAME_LENGTH,
        slice_interval=DEFAULT_SLICE_INTERVAL,
        device_index=DEFAULT_LIVE_DEVICE_INDEX,
        device_probe_seconds=1.0,
        list_devices=False,
        enable_denoise=False,
        noise_ref=None,
        high_pass_hz=DEFAULT_HIGH_PASS_HZ,
        noise_reduction_factor=DEFAULT_NOISE_REDUCTION_FACTOR,
        species_file=DEFAULT_SPECIES_FILE,
        detections_dir=DEFAULT_DETECTIONS_DIR,
        verbose=False,
        clip_span=DEFAULT_LIVE_CLIP_SPAN,
    )


def clean_path_string(value: str) -> str:
    return value.strip().strip("\"'")


def parse_interactive_file_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []

    if "," in raw:
        return [clean_path_string(part) for part in raw.split(",") if clean_path_string(part)]

    try:
        parts = shlex.split(raw, posix=False)
    except ValueError:
        parts = [raw]

    cleaned = [clean_path_string(part) for part in parts if clean_path_string(part)]

    if len(cleaned) > 1:
        candidate = clean_path_string(raw)
        if Path(candidate).suffix.lower() == ".wav":
            return [candidate]

    return cleaned


def prompt_bool(message: str, default: bool = False) -> bool:
    default_label = "Y/n" if default else "y/N"
    raw = input(f"{message} [{default_label}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "s", "si"}


def prompt_float(message: str, default: float) -> float:
    while True:
        raw = input(f"{message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print("Inserisci un numero valido.")


def prompt_int(message: str, default: int) -> int:
    while True:
        raw = input(f"{message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Inserisci un intero valido.")


def get_available_audio_devices() -> list[str]:
    try:
        from pvrecorder import PvRecorder

        devices = PvRecorder.get_available_devices()
        return list(devices) if devices else []
    except Exception:
        return []


def get_available_audio_devices_sounddevice() -> list[dict]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        input_devices: list[dict] = []
        for index, device in enumerate(devices):
            if device.get("max_input_channels", 0) > 0:
                input_devices.append(
                    {
                        "index": index,
                        "name": device.get("name", f"Device {index}"),
                        "channels": int(device.get("max_input_channels", 0)),
                        "samplerate": int(float(device.get("default_samplerate", 0) or 0)),
                    }
                )
        return input_devices
    except Exception:
        return []


def print_available_audio_devices(backend: str = "pvrecorder"):
    if backend == "sounddevice":
        devices = get_available_audio_devices_sounddevice()
        if not devices:
            print("Nessun device audio disponibile o elenco non recuperabile.")
            return []

        print("Device audio disponibili (sounddevice):")
        for device in devices:
            print(
                f"  {device['index']}. {device['name']} "
                f"(channels={device['channels']}, default_sr={device['samplerate']})"
            )
        return devices

    devices = get_available_audio_devices()
    if not devices:
        print("Nessun device audio disponibile o elenco non recuperabile.")
        return []

    print("Device audio disponibili (pvrecorder):")
    for index, name in enumerate(devices):
        print(f"  {index}. {name}")
    return devices


def choose_preferred_sounddevice_index(devices: list[dict]) -> int:
    if not devices:
        return -1

    for device in devices:
        if device["index"] == DEFAULT_LIVE_DEVICE_INDEX:
            return device["index"]

    ranked_checks = (
        lambda name: "capture" in name,
        lambda name: "microphone" in name and "array" in name,
        lambda name: "microphone" in name,
        lambda name: True,
    )

    for check in ranked_checks:
        for device in devices:
            name = device["name"].lower()
            if check(name):
                return device["index"]
    return devices[0]["index"]


def choose_loudest_sounddevice_index(measurements: list[dict]) -> int:
    if not measurements:
        return -1
    selected = max(measurements, key=lambda item: (item["rms"], item["peak"]))
    return int(selected["index"])


def probe_sounddevice_inputs(
    devices: list[dict],
    duration_seconds: float = 1.0,
) -> list[dict]:
    import numpy as np
    import sounddevice as sd

    measurements: list[dict] = []
    duration_seconds = max(float(duration_seconds), 0.1)
    print(f"Provo i device audio per {duration_seconds:.1f}s ciascuno...")

    for device in devices:
        sample_rate = int(device["samplerate"] or DEFAULT_RECORDER_SAMPLE_RATE)
        frame_count = max(1, int(sample_rate * duration_seconds))
        try:
            samples = sd.rec(
                frame_count,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                device=device["index"],
                blocking=True,
            )
            mono = np.asarray(samples, dtype=np.float32).reshape(-1)
            rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
            peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        except Exception as exc:
            print(f"  {device['index']}: {device['name']} -> errore: {exc}")
            continue

        measurements.append(
            {
                "index": device["index"],
                "name": device["name"],
                "rms": rms,
                "peak": peak,
                "samplerate": sample_rate,
            }
        )
        print(
            f"  {device['index']}: {device['name']} "
            f"RMS={rms:.4f} PEAK={peak:.4f}"
        )

    return measurements


def load_sounddevice_state(state_path: Path = DEFAULT_AUDIO_DEVICE_STATE_FILE) -> dict:
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_sounddevice_state(
    device: dict,
    measurement: dict,
    state_path: Path = DEFAULT_AUDIO_DEVICE_STATE_FILE,
) -> None:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "index": int(device["index"]),
                "name": device["name"],
                "samplerate": int(measurement["samplerate"]),
                "rms": float(measurement["rms"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def find_saved_sounddevice(
    devices: list[dict],
    state: dict,
) -> dict | None:
    saved_name = str(state.get("name", "")).strip()
    if saved_name:
        matching_name = next(
            (device for device in devices if device["name"] == saved_name),
            None,
        )
        if matching_name is not None:
            return matching_name

    saved_index = state.get("index")
    return next(
        (device for device in devices if device["index"] == saved_index),
        None,
    )


def select_sounddevice_index(
    devices: list[dict],
    duration_seconds: float = 1.0,
    state_path: Path = DEFAULT_AUDIO_DEVICE_STATE_FILE,
) -> int:
    if not devices:
        return -1

    state = load_sounddevice_state(state_path)
    saved_device = find_saved_sounddevice(devices, state)
    if saved_device is not None:
        print(f"Verifico il device salvato: {saved_device['index']} ({saved_device['name']})")
        saved_measurements = probe_sounddevice_inputs([saved_device], duration_seconds)
        if saved_measurements and saved_measurements[0]["rms"] >= DEFAULT_DEVICE_MIN_RMS:
            save_sounddevice_state(saved_device, saved_measurements[0], state_path)
            print(f"Riutilizzo il device salvato {saved_device['index']}.")
            return int(saved_device["index"])
        print("Il device salvato non ha un segnale sufficiente; provo gli altri.")
        devices_to_probe = [device for device in devices if device != saved_device]
    else:
        devices_to_probe = devices

    measurements = probe_sounddevice_inputs(devices_to_probe, duration_seconds)
    valid_measurements = [
        measurement
        for measurement in measurements
        if measurement["rms"] >= DEFAULT_DEVICE_MIN_RMS
    ]
    selected_measurement = max(
        valid_measurements,
        key=lambda item: (item["rms"], item["peak"]),
        default=None,
    )
    if selected_measurement is None:
        return -1

    selected_device = next(
        device
        for device in devices_to_probe
        if device["index"] == selected_measurement["index"]
    )
    save_sounddevice_state(selected_device, selected_measurement, state_path)
    print(f"Selezionato e salvato il device {selected_device['index']}.")
    return int(selected_device["index"])


def interactive_args() -> argparse.Namespace:
    print("Bird Audio Suite - modalita' interattiva")
    print("Scegli una modalita':")
    print("  1. batch")
    print("  2. live")
    print("  3. denoise")

    selection = ""
    while selection not in {"1", "2", "3", "batch", "live", "denoise"}:
        selection = input("Modalita' [1]: ").strip().lower() or "1"

    if selection in {"1", "batch"}:
        files_raw = prompt_text(
            "File WAV separati da virgola o spazio (vuoto = tutti i .wav nella cartella corrente)",
            "",
        )
        file_list = parse_interactive_file_list(files_raw)

        directory_raw = prompt_text("Cartella da scandire opzionale", "")
        directory = Path(clean_path_string(directory_raw)).expanduser() if directory_raw else None

        use_denoise = prompt_bool("Applica denoise prima della detection", False)
        noise_ref = None
        high_pass_hz = DEFAULT_HIGH_PASS_HZ
        noise_reduction_factor = DEFAULT_NOISE_REDUCTION_FACTOR
        if use_denoise:
            noise_ref = (lambda raw: Path(clean_path_string(raw)).expanduser() if clean_path_string(raw) else None)(
                prompt_text("File WAV di rumore opzionale", "")
            )
            high_pass_hz = prompt_float("Filtro high-pass (Hz)", DEFAULT_HIGH_PASS_HZ)
            noise_reduction_factor = prompt_float(
                "Intensita' riduzione rumore",
                DEFAULT_NOISE_REDUCTION_FACTOR,
            )

        return argparse.Namespace(
            command="batch",
            files=file_list or None,
            directory=directory,
            recursive=prompt_bool("Ricerca ricorsiva nelle sottocartelle", False),
            lat=prompt_float("Latitudine", DEFAULT_LATITUDE),
            lon=prompt_float("Longitudine", DEFAULT_LONGITUDE),
            min_confidence=prompt_float("Confidenza minima BirdNET", DEFAULT_BATCH_MIN_CONFIDENCE),
            denoise=use_denoise,
            noise_ref=noise_ref,
            high_pass_hz=high_pass_hz,
            noise_reduction_factor=noise_reduction_factor,
            export_clips=prompt_bool("Esporta clip per specie rilevata", True),
            clip_span=DEFAULT_BATCH_CLIP_SPAN,
            species_file=DEFAULT_SPECIES_FILE,
            detections_dir=DEFAULT_DETECTIONS_DIR,
        )

    if selection in {"2", "live"}:
        backend = (
            prompt_text(
                "Backend live (sounddevice/pvrecorder/auto)",
                DEFAULT_LIVE_BACKEND,
            ).strip().lower()
            or DEFAULT_LIVE_BACKEND
        )
        if backend not in {"sounddevice", "pvrecorder", "auto"}:
            backend = DEFAULT_LIVE_BACKEND
        devices = print_available_audio_devices("sounddevice" if backend == "auto" else backend)
        if devices and backend in {"sounddevice", "auto"}:
            suggested_device = choose_preferred_sounddevice_index(devices)
        elif backend == "pvrecorder":
            suggested_device = DEFAULT_LIVE_DEVICE_INDEX
        elif devices:
            suggested_device = 0
        else:
            suggested_device = DEFAULT_LIVE_DEVICE_INDEX
        use_denoise = prompt_bool("Applica denoise live", False)
        noise_ref = None
        high_pass_hz = DEFAULT_HIGH_PASS_HZ
        noise_reduction_factor = DEFAULT_NOISE_REDUCTION_FACTOR
        if use_denoise:
            noise_ref = (lambda raw: Path(clean_path_string(raw)).expanduser() if clean_path_string(raw) else None)(
                prompt_text("File WAV di rumore opzionale", "")
            )
            high_pass_hz = prompt_float("Filtro high-pass (Hz)", DEFAULT_HIGH_PASS_HZ)
            noise_reduction_factor = prompt_float(
                "Intensita' riduzione rumore",
                DEFAULT_NOISE_REDUCTION_FACTOR,
            )

        return argparse.Namespace(
            command="live",
            backend=backend,
            lat=prompt_float("Latitudine", DEFAULT_LATITUDE),
            lon=prompt_float("Longitudine", DEFAULT_LONGITUDE),
            min_confidence=prompt_float("Confidenza minima BirdNET", DEFAULT_LIVE_MIN_CONFIDENCE),
            frame_length=prompt_int("Frame length recorder", DEFAULT_FRAME_LENGTH),
            slice_interval=prompt_int("Intervallo slice (numero frame)", DEFAULT_SLICE_INTERVAL),
            device_index=prompt_int("Indice dispositivo audio", suggested_device),
            device_probe_seconds=1.0,
            list_devices=False,
            enable_denoise=use_denoise,
            noise_ref=noise_ref,
            high_pass_hz=high_pass_hz,
            noise_reduction_factor=noise_reduction_factor,
            species_file=DEFAULT_SPECIES_FILE,
            detections_dir=DEFAULT_DETECTIONS_DIR,
            verbose=prompt_bool("Mostrare diagnostica live", True),
            clip_span=DEFAULT_LIVE_CLIP_SPAN,
        )

    input_path = Path(prompt_text("File WAV da denoisare")).expanduser()
    output_raw = prompt_text("File output opzionale", "")
    noise_ref_raw = prompt_text("File WAV di rumore opzionale", "")
    return argparse.Namespace(
        command="denoise",
        input=Path(clean_path_string(str(input_path))).expanduser(),
        output=Path(clean_path_string(output_raw)).expanduser() if output_raw else None,
        noise_ref=Path(clean_path_string(noise_ref_raw)).expanduser() if noise_ref_raw else None,
        high_pass_hz=prompt_float("Filtro high-pass (Hz)", DEFAULT_DENOISE_HIGH_PASS_HZ),
        noise_reduction_factor=prompt_float(
            "Intensita' riduzione rumore",
            DEFAULT_DENOISE_REDUCTION_FACTOR,
        ),
        plot=prompt_bool("Mostrare grafico spettrogramma", False),
    )


def collect_batch_files(file_args: list[str] | None, directory: Path | None, recursive: bool) -> list[Path]:
    files: list[Path] = []

    if file_args:
        files.extend(Path(item).expanduser().resolve() for item in file_args)

    if directory:
        pattern = "*.wav"
        directory = directory.expanduser().resolve()
        iterator = directory.rglob(pattern) if recursive else directory.glob(pattern)
        files.extend(path.resolve() for path in iterator)

    if not files:
        files.extend(path.resolve() for path in Path.cwd().glob("*.wav"))

    unique_files: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            unique_files.append(path)

    return unique_files


def ensure_today_folder(base_dir: Path) -> Path:
    folder = base_dir / datetime.now().strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def load_species_frequency_stats(species_file: Path = DEFAULT_SPECIES_FILE) -> dict[str, dict[str, str]]:
    if not species_file.exists():
        return {}

    stats: dict[str, dict[str, str]] = {}
    with species_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            scientific_name = (row.get("scientific_name") or "").strip()
            if not scientific_name:
                continue
            stats[scientific_name] = {
                "rarity": (row.get("rarity") or "unseen").strip(),
                "daily_average": (row.get("daily_average") or "0.000").strip(),
            }
    return stats


def load_daily_observation_rows(day_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    species_stats = load_species_frequency_stats()
    for csv_path in sorted(day_dir.glob("inaturalist_import_*.csv")):
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                taxon_name = (row.get("Nome del taxon") or "").strip()
                observed_at = (row.get("Data osservazione") or "").strip()
                description = (row.get("Descrizione") or "").strip()
                if not taxon_name or not observed_at:
                    continue
                key = (taxon_name, observed_at, description)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "taxon_name": taxon_name,
                        "time": observed_at,
                        "description": description,
                        "rarity": species_stats.get(taxon_name, {}).get("rarity", "unseen"),
                        "avg_counts": str(int(float(species_stats.get(taxon_name, {}).get("daily_average", "0.000")))) + '/day',
                    }
                )

    rows.sort(key=lambda row: row["time"], reverse=True)
    return rows


def split_description(desc: str) -> tuple[str, str, str, str]:
    if not desc:
        return "", "", "", ""

    parts = desc.split("; ")
    names = [n.strip() for n in parts[0].split(",") if n.strip()]
    names += [""] * (3 - len(names))

    confidence = ""
    for part in parts[1:]:
        if part.startswith("birdnet_confidence="):
            confidence = part.split("=", 1)[1].strip()
            break

    return names[0], names[1], names[2], confidence


def print_daily_detection_summary(day_dir: Path, title: str = "") -> None:
    records = load_daily_observation_rows(day_dir)

    print("\033[2J\033[H", end="")
    if title:
        print(title)
    if not records:
        print("No observations today.")
        return

    rows: list[dict[str, str]] = []
    for record in records:
        taxon_en, taxon_ita, taxon_ger, conf = split_description(record.get("description", ""))
        rows.append(
            {
                "time": datetime.strptime(record.get("time", ""), "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S"),
                "taxon_name": record.get("taxon_name", ""),
                "rarity": record.get("rarity", ""),
                "avg_counts": record.get("avg_counts", ""),
                "taxon_en": taxon_en,
                "taxon_ita": taxon_ita,
                "taxon_ger": taxon_ger,
                "conf": conf,
            }
        )

    headers = ["time", "taxon_name", "rarity", "avg_counts", "taxon_en", "taxon_ita", "taxon_ger", "conf"]
    widths = {header: max(len(header), max(len(row[header]) for row in rows)) for header in headers}
    header = "|".join(header.ljust(widths[header]) for header in headers)
    strings_width = len(header)
    strings_len = len(rows) + 2
    print(f"\033[8;{strings_len};{strings_width}t", end="")
    print(header)
    for row in rows:
        print("|".join(row[header].ljust(widths[header]) for header in headers))


def record_live_detection_observation(
    *,
    scientific_name: str,
    english_name: str,
    confidence: float,
    species_catalog: SpeciesCatalog,
    output_dir: Path,
    args: argparse.Namespace,
) -> datetime:
    observed_at = datetime.now()
    species_catalog.ensure_species(
        [
            {
                "scientific_name": scientific_name,
                "common_name": english_name,
                "confidence": confidence,
            }
        ],
        observed_at=observed_at,
    )
    italian_name, german_name, english_name = species_catalog.display_names(scientific_name, english_name)
    append_inaturalist_csv_row(
        output_dir / f"inaturalist_import_{output_dir.name}.csv",
        taxon_name=scientific_name,
        observed_at=observed_at,
        english_name=english_name,
        italian_name=italian_name,
        german_name=german_name,
        confidence=confidence,
        place_name=args.place_name,
        latitude=args.lat,
        longitude=args.lon,
        tags=DEFAULT_INATURALIST_TAGS,
        geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
    )
    print_daily_detection_summary(output_dir)
    return observed_at


def resolve_runtime_location(args: argparse.Namespace) -> None:
    resolved = resolve_location(
        fallback_latitude=float(getattr(args, "lat", DEFAULT_LATITUDE)),
        fallback_longitude=float(getattr(args, "lon", DEFAULT_LONGITUDE)),
        fallback_place_name=getattr(args, "place_name", DEFAULT_PLACE_NAME) or DEFAULT_PLACE_NAME,
        enable_auto_lookup=DEFAULT_ENABLE_AUTO_LOCATION,
        timeout_seconds=DEFAULT_LOCATION_LOOKUP_TIMEOUT_SECONDS,
    )
    args.lat = resolved.latitude
    args.lon = resolved.longitude
    args.place_name = resolved.place_name
    source_label = "fallback config" if resolved.used_fallback else resolved.source
    print(
        f"{datetime.now():%H:%M:%S} - Observation location: "
        f"{args.place_name} ({args.lat:.6f}, {args.lon:.6f}) [{source_label}]"
    )


def run_batch(args: argparse.Namespace) -> int:
    files = collect_batch_files(args.files, args.directory, args.recursive)
    if not files:
        print("No WAV files found.")
        return 1

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    detector = BirdNetDetector()

    for file_path in files:
        if not file_path.exists():
            print(f"Skipping missing file: {file_path}")
            continue

        analysis_path = file_path
        analysis_samples = None
        analysis_rate = None

        if args.denoise:
            analysis_samples, analysis_rate = load_audio(file_path)
            analysis_samples = denoise_signal(
                analysis_samples,
                analysis_rate,
                noise_reference=args.noise_ref,
                high_pass_hz=args.high_pass_hz,
                noise_reduction_factor=args.noise_reduction_factor,
            )

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_handle:
                analysis_path = Path(temp_handle.name)
            write_wav_mono(analysis_path, analysis_samples, analysis_rate)

        try:
            file_observed_at = datetime.fromtimestamp(file_path.stat().st_ctime)
            detections = detector.detect(
                analysis_path,
                latitude=args.lat,
                longitude=args.lon,
                when=file_observed_at,
                min_confidence=args.min_confidence,
            )
            species_catalog_changed = species_catalog.ensure_species(
                detections,
                observed_at=file_observed_at,
            )

            if args.export_clips and detections:
                if analysis_samples is None or analysis_rate is None:
                    analysis_samples, analysis_rate = load_audio(file_path)

                grouped = aggregate_detections_by_species(
                    detections,
                    duration_seconds=len(analysis_samples) / float(analysis_rate),
                    min_confidence=args.min_confidence,
                )
                grouped = apply_clip_span_policy(
                    grouped,
                    duration_seconds=len(analysis_samples) / float(analysis_rate),
                    clip_span=getattr(args, "clip_span", DEFAULT_BATCH_CLIP_SPAN),
                )

                if grouped:
                    destination_dir = args.detections_dir / file_observed_at.strftime("%Y%m%d")
                    exported = export_detection_clips(
                        analysis_samples,
                        analysis_rate,
                        grouped,
                        species_catalog,
                        destination_dir,
                        latitude=args.lat,
                        longitude=args.lon,
                        place_name=args.place_name,
                        tags=DEFAULT_INATURALIST_TAGS,
                        geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
                    )
                    print(f"  Exported {len(exported)} clip(s) to {destination_dir}")
                    if species_catalog_changed and exported:
                        print_daily_detection_summary(destination_dir, title=f"File: {file_path}")
        finally:
            if analysis_path != file_path and analysis_path.exists():
                analysis_path.unlink()

    return 0


def process_live_slice(
    raw_samples,
    sample_rate: int,
    slice_number: int,
    detector: BirdNetDetector,
    species_catalog: SpeciesCatalog,
    output_dir: Path,
    args: argparse.Namespace,
    temp_path: Path | None = None,
) -> None:
    # slice_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_rms = rms_level(raw_samples)
    processing_mode = "raw"

    created_temp = False
    if temp_path is None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=output_dir) as temp_handle:
            temp_path = Path(temp_handle.name)
        created_temp = True

    try:
        if args.disable_denoise:
            working_samples = raw_samples
        else:
            working_samples = denoise_signal(
                raw_samples,
                sample_rate,
                noise_reference=args.noise_ref,
                high_pass_hz=args.high_pass_hz,
                noise_reduction_factor=args.noise_reduction_factor,
            )
            processing_mode = "denoised"

        write_wav_mono(temp_path, working_samples, sample_rate)
        detections = detector.detect(
            temp_path,
            latitude=args.lat,
            longitude=args.lon,
            when=datetime.now(),
            min_confidence=args.min_confidence,
        )

        if not detections and not args.disable_denoise:
            processing_mode = "raw-fallback"
            write_wav_mono(temp_path, raw_samples, sample_rate)
            working_samples = raw_samples
            detections = detector.detect(
                temp_path,
                latitude=args.lat,
                longitude=args.lon,
                when=datetime.now(),
                min_confidence=args.min_confidence,
            )
    finally:
        if created_temp and temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    if args.verbose:
        working_rms = rms_level(working_samples)
        print(
            f"{datetime.now():%H:%M:%S} - "
            f"slice={slice_number} "
            f"mode={processing_mode} "
            f"raw_rms={raw_rms:.4f} "
            f"used_rms={working_rms:.4f} "
            f"detections={len(detections)}"
        )
    print(detections)
    species_catalog_changed = species_catalog.ensure_species(detections, observed_at=datetime.now())
    duration_seconds = len(raw_samples) / float(sample_rate)
    grouped = aggregate_detections_by_species(
        detections,
        duration_seconds=duration_seconds,
        min_confidence=args.min_confidence,
    )
    grouped = apply_clip_span_policy(
        grouped,
        duration_seconds=duration_seconds,
        clip_span=getattr(args, "clip_span", DEFAULT_LIVE_CLIP_SPAN),
    )
    if grouped:
        exported = export_detection_clips(
            working_samples,
            sample_rate,
            grouped,
            species_catalog,
            output_dir,
            latitude=args.lat,
            longitude=args.lon,
            place_name=args.place_name,
            tags=DEFAULT_INATURALIST_TAGS,
            geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
        )
        if species_catalog_changed and exported:
            print_daily_detection_summary(output_dir)
        # print(f"{datetime.now():%H:%M:%S} - Exported {len(exported)} clip(s).")


def detect_live_slice(
    raw_samples,
    sample_rate: int,
    detector: BirdNetDetector,
    args: argparse.Namespace,
    temp_path: Path | None = None,
) -> tuple[dict[str, tuple[float, float, float, str]], np.ndarray]:
    """Detect on a live slice and return grouped detections and the working samples.

    This function does NOT export clips; the caller is responsible for aggregating
    detections across slices and exporting when appropriate.
    """
    import numpy as np

    if args.disable_denoise:
        working_samples = raw_samples
        processing_mode = "raw"
    else:
        working_samples = denoise_signal(
            raw_samples,
            sample_rate,
            noise_reference=args.noise_ref,
            high_pass_hz=args.high_pass_hz,
            noise_reduction_factor=args.noise_reduction_factor,
        )
        processing_mode = "denoised"

    created_temp = False
    if temp_path is None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_handle:
            temp_path = Path(temp_handle.name)
        created_temp = True

    try:
        write_wav_mono(temp_path, working_samples, sample_rate)
        detections = detector.detect(
            temp_path,
            latitude=args.lat,
            longitude=args.lon,
            when=datetime.now(),
            min_confidence=args.min_confidence,
        )

        if not detections and not args.disable_denoise:
            write_wav_mono(temp_path, raw_samples, sample_rate)
            working_samples = raw_samples
            detections = detector.detect(
                temp_path,
                latitude=args.lat,
                longitude=args.lon,
                when=datetime.now(),
                min_confidence=args.min_confidence,
            )
    finally:
        if created_temp and temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    duration_seconds = len(working_samples) / float(sample_rate)
    grouped = aggregate_detections_by_species(
        detections,
        duration_seconds=duration_seconds,
        min_confidence=args.min_confidence,
    )
    grouped = apply_clip_span_policy(
        grouped,
        duration_seconds=duration_seconds,
        clip_span=getattr(args, "clip_span", DEFAULT_LIVE_CLIP_SPAN),
    )
    return grouped, working_samples


def run_live_pvrecorder(args: argparse.Namespace) -> int:
    from pvrecorder import PvRecorder

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    async_detector = BirdNetDetector()
    output_dir = ensure_today_folder(args.detections_dir)
    devices = get_available_audio_devices()
    if devices:
        print("Device audio disponibili:")
        for index, name in enumerate(devices):
            print(f"  {index}. {name}")
    selected_device_name = (
        devices[args.device_index]
        if devices and 0 <= args.device_index < len(devices)
        else "default/unknown"
    )
    recorder = PvRecorder(device_index=args.device_index, frame_length=args.frame_length)
    sample_rate = getattr(recorder, "sample_rate", DEFAULT_RECORDER_SAMPLE_RATE)
    started = False

    print(f"{datetime.now():%H:%M:%S} - Working directory: {Path.cwd()}")
    print(f"{datetime.now():%H:%M:%S} - Saving detections to: {output_dir}")
    print(f"{datetime.now():%H:%M:%S} - Device index: {args.device_index} ({selected_device_name})")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")
    print_daily_detection_summary(output_dir)

    audio_frames: list[int] = []
    slice_index = 0

    # Choose a temp directory (prefer tmpfs /dev/shm if available)
    tmp_dir = Path("/dev/shm") if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK) else output_dir
    shared_temp = tmp_dir / f".birdnet_live_tmp_{os.getpid()}.wav"

    try:
        import threading
        import collections
        import numpy as _np

        # sliding window parameters (50% overlap)
        slice_frames = int(args.slice_interval)
        slide_step_frames = max(1, slice_frames // 2)

        # queues for async detection
        task_queue: queue.Queue = queue.Queue()
        result_queue: queue.Queue = queue.Queue()

        # Worker thread runs detections asynchronously using the pre-loaded analyzer.
        def _detection_worker():
            while True:
                item = task_queue.get()
                if item is None:
                    break
                window_samples, window_start = item
                try:
                    grouped, working_samples = detect_live_slice(
                        window_samples,
                        sample_rate,
                        async_detector,
                        args,
                        temp_path=shared_temp,
                    )
                    result_queue.put((grouped, working_samples, window_start))
                except Exception as exc:
                    result_queue.put(("error", exc))

        worker = threading.Thread(target=_detection_worker, daemon=True)
        worker.start()

        # sliding buffer of recent frames (keeps last slice_frames frames)
        frames_buf: collections.deque = collections.deque(maxlen=slice_frames)
        frames_since_last_detection = 0
        total_samples = 0

        recorder.start()
        started = True
        if "_active_groups" not in locals():
            _active_groups: dict = {}
            _grace_slices = 1

        while True:
            frame = recorder.read()
            arr = _np.asarray(frame, dtype=_np.float32)
            frames_buf.append(arr)
            total_samples += arr.size
            frames_since_last_detection += 1

            # process any completed detections
            while not result_queue.empty():
                res = result_queue.get()
                if isinstance(res, tuple) and res[0] == "error":
                    # log and continue
                    if args.verbose:
                        print(f"Detection worker error: {res[1]}")
                    continue
                grouped, working_samples, win_start = res
                present = set(grouped.keys())

                for sci, (start_sec, end_sec, conf, english) in grouped.items():
                    win_len = int(len(working_samples))
                    if sci in _active_groups:
                        last_end = _active_groups[sci].get("last_end_sample", _active_groups[sci].get("start_sample", win_start))
                        append_from = max(0, last_end - win_start)
                        if append_from < win_len:
                            part = _np.asarray(working_samples[append_from:], dtype=_np.float32)
                            _active_groups[sci]["samples"].append(part)
                            _active_groups[sci]["last_end_sample"] = max(_active_groups[sci].get("last_end_sample", win_start), win_start + win_len)
                        _active_groups[sci]["conf"] = max(conf, _active_groups[sci]["conf"])
                        _active_groups[sci]["missing"] = 0
                    else:
                        observed_at = record_live_detection_observation(
                            scientific_name=sci,
                            english_name=english,
                            confidence=conf,
                            species_catalog=species_catalog,
                            output_dir=output_dir,
                            args=args,
                        )
                        _active_groups[sci] = {
                            "samples": [_np.asarray(working_samples, dtype=_np.float32)],
                            "conf": conf,
                            "english": english,
                            "missing": 0,
                            "start_sample": win_start,
                            "last_end_sample": win_start + int(len(working_samples)),
                            "observed_at": observed_at,
                            "csv_recorded": True,
                        }

                # increment missing for groups not present in this result and finalize
                for sci in list(_active_groups.keys()):
                    if sci not in present:
                        _active_groups[sci]["missing"] += 1
                        if _active_groups[sci]["missing"] > _grace_slices:
                            concat = _np.concatenate(_active_groups[sci]["samples"], axis=0)
                            confidence = _active_groups[sci]["conf"]
                            english = _active_groups[sci]["english"]
                            dummy_group = {sci: (0.0, len(concat) / float(sample_rate), confidence, english)}
                            exported = export_detection_clips(
                                concat,
                                sample_rate,
                                dummy_group,
                                species_catalog,
                                output_dir,
                                latitude=args.lat,
                                longitude=args.lon,
                                place_name=args.place_name,
                                tags=DEFAULT_INATURALIST_TAGS,
                                geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
                                observed_at=_active_groups[sci].get("observed_at"),
                            )
                            if exported:
                                print_daily_detection_summary(output_dir)
                            del _active_groups[sci]

            # if we don't yet have a full window, continue
            if len(frames_buf) < slice_frames:
                continue

            # only submit a new detection every slide_step_frames
            if frames_since_last_detection < slide_step_frames:
                continue

            # build window from last slice_frames frames and submit to worker
            window = _np.concatenate(list(frames_buf), axis=0)
            window_start = total_samples - (slice_frames * args.frame_length)
            task_queue.put((window.copy(), int(window_start)))
            frames_since_last_detection = 0

    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        try:
            if "task_queue" in locals():
                task_queue.put(None)
        except Exception:
            pass
        return 0
    finally:
        if started:
            recorder.stop()
        recorder.delete()
        try:
            if shared_temp and shared_temp.exists():
                shared_temp.unlink()
        except Exception:
            pass


def run_live_sounddevice(args: argparse.Namespace) -> int:
    import numpy as np
    import sounddevice as sd

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    async_detector = BirdNetDetector()
    output_dir = ensure_today_folder(args.detections_dir)
    devices = get_available_audio_devices_sounddevice()
    if devices:
        print("Device audio disponibili (sounddevice):")
        for device in devices:
            print(
                f"  {device['index']}. {device['name']} "
                f"(channels={device['channels']}, default_sr={device['samplerate']})"
            )
    if args.device_index < 0 and devices:
        selected_index = select_sounddevice_index(
            devices,
            duration_seconds=getattr(args, "device_probe_seconds", 1.0),
        )
        if selected_index >= 0:
            args.device_index = selected_index
    selected_device = next(
        (device for device in devices if device["index"] == args.device_index),
        None,
    )
    selected_device_name = (
        selected_device["name"]
        if selected_device
        else "default/unknown"
    )
    sample_rate = (
        selected_device["samplerate"]
        if selected_device and selected_device["samplerate"] > 0
        else DEFAULT_RECORDER_SAMPLE_RATE
    )
    if args.device_index < 0 and devices:
        sample_rate = max(
            int(float(sd.query_devices(kind="input").get("default_samplerate", 0) or 0)),
            sample_rate,
        )
    device = None if args.device_index < 0 else args.device_index

    print(f"{datetime.now():%H:%M:%S} - Working directory: {Path.cwd()}")
    print(f"{datetime.now():%H:%M:%S} - Saving detections to: {output_dir}")
    print(f"{datetime.now():%H:%M:%S} - Device index: {args.device_index} ({selected_device_name})")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")
    print_daily_detection_summary(output_dir)

    audio_frames: list[float] = []
    slice_index = 0
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    status_queue: queue.Queue[str] = queue.Queue()

    def audio_callback(indata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            status_queue.put(str(status))
        audio_queue.put(indata[:, 0].copy())

    # Choose a temp directory (prefer tmpfs /dev/shm if available)
    tmp_dir = Path("/dev/shm") if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK) else output_dir
    shared_temp = tmp_dir / f".birdnet_live_tmp_{os.getpid()}.wav"

    try:
        import threading
        import collections

        # sliding window parameters (50% overlap)
        slice_frames = int(args.slice_interval)
        slide_step_frames = max(1, slice_frames // 2)

        # queues for async detection
        task_queue: queue.Queue = queue.Queue()
        result_queue: queue.Queue = queue.Queue()

        def _detection_worker():
            while True:
                item = task_queue.get()
                if item is None:
                    break
                window_samples, window_start = item
                try:
                    grouped, working_samples = detect_live_slice(
                        window_samples,
                        sample_rate,
                        async_detector,
                        args,
                        temp_path=shared_temp,
                    )
                    result_queue.put((grouped, working_samples, window_start))
                except Exception as exc:
                    result_queue.put(("error", exc))

        worker = threading.Thread(target=_detection_worker, daemon=True)
        worker.start()

        frames_buf: collections.deque = collections.deque(maxlen=slice_frames)
        frames_since_last_detection = 0
        total_samples = 0

        with sd.InputStream(
            device=device,
            channels=1,
            samplerate=sample_rate,
            dtype="float32",
            blocksize=args.frame_length,
            callback=audio_callback,
        ):
            if "_active_groups" not in locals():
                _active_groups: dict = {}
                _grace_slices = 1
            while True:
                frame = audio_queue.get()
                while not status_queue.empty():
                    message = status_queue.get_nowait()
                    if args.verbose:
                        print(f"{datetime.now():%H:%M:%S} - audio status: {message}")

                arr = np.asarray(frame, dtype=np.float32)
                frames_buf.append(arr)
                total_samples += arr.size
                frames_since_last_detection += 1

                # process completed detections
                while not result_queue.empty():
                    res = result_queue.get()
                    if isinstance(res, tuple) and res[0] == "error":
                        if args.verbose:
                            print(f"Detection worker error: {res[1]}")
                        continue
                    grouped, working_samples, win_start = res
                    present = set(grouped.keys())

                    for sci, (start_sec, end_sec, conf, english) in grouped.items():
                        win_len = int(len(working_samples))
                        if sci in _active_groups:
                            last_end = _active_groups[sci].get("last_end_sample", _active_groups[sci].get("start_sample", win_start))
                            append_from = max(0, last_end - win_start)
                            if append_from < win_len:
                                part = working_samples[append_from:]
                                _active_groups[sci]["samples"].append(part)
                                _active_groups[sci]["last_end_sample"] = max(_active_groups[sci].get("last_end_sample", win_start), win_start + win_len)
                            _active_groups[sci]["conf"] = max(conf, _active_groups[sci]["conf"])
                            _active_groups[sci]["missing"] = 0
                        else:
                            observed_at = record_live_detection_observation(
                                scientific_name=sci,
                                english_name=english,
                                confidence=conf,
                                species_catalog=species_catalog,
                                output_dir=output_dir,
                                args=args,
                            )
                            _active_groups[sci] = {
                                "samples": [working_samples],
                                "conf": conf,
                                "english": english,
                                "missing": 0,
                                "start_sample": win_start,
                                "last_end_sample": win_start + int(len(working_samples)),
                                "observed_at": observed_at,
                                "csv_recorded": True,
                            }

                    import numpy as _np
                    for sci in list(_active_groups.keys()):
                        if sci not in present:
                            _active_groups[sci]["missing"] += 1
                            if _active_groups[sci]["missing"] > _grace_slices:
                                concat = _np.concatenate(_active_groups[sci]["samples"], axis=0)
                                confidence = _active_groups[sci]["conf"]
                                english = _active_groups[sci]["english"]
                                dummy_group = {sci: (0.0, len(concat) / float(sample_rate), confidence, english)}
                                exported = export_detection_clips(
                                    concat,
                                    sample_rate,
                                    dummy_group,
                                    species_catalog,
                                    output_dir,
                                    latitude=args.lat,
                                    longitude=args.lon,
                                    place_name=args.place_name,
                                    tags=DEFAULT_INATURALIST_TAGS,
                                    geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
                                    observed_at=_active_groups[sci].get("observed_at"),
                                )
                                if exported:
                                    print_daily_detection_summary(output_dir)
                                del _active_groups[sci]

                # if we don't yet have a full window, continue
                if len(frames_buf) < slice_frames:
                    continue

                # only submit a new detection every slide_step_frames
                if frames_since_last_detection < slide_step_frames:
                    continue

                window = np.concatenate(list(frames_buf), axis=0)
                window_start = total_samples - (slice_frames * args.frame_length)
                task_queue.put((window.copy(), int(window_start)))
                frames_since_last_detection = 0
    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        try:
            if "task_queue" in locals():
                task_queue.put(None)
        except Exception:
            pass
        try:
            if shared_temp and shared_temp.exists():
                shared_temp.unlink()
        except Exception:
            pass
        return 0
    except Exception as exc:
        print(f"\nLive audio error ({args.backend}): {exc}")
        traceback.print_exc()
        try:
            if "task_queue" in locals():
                task_queue.put(None)
        except Exception:
            pass
        try:
            if shared_temp and shared_temp.exists():
                shared_temp.unlink()
        except Exception:
            pass
        return 1
    try:
        try:
            if "task_queue" in locals():
                task_queue.put(None)
        except Exception:
            pass
        if shared_temp and shared_temp.exists():
            shared_temp.unlink()
    except Exception:
        pass
    return 0


def run_live(args: argparse.Namespace) -> int:
    backend = getattr(args, "backend", DEFAULT_LIVE_BACKEND)
    if backend == "auto":
        backend = DEFAULT_LIVE_BACKEND

    if getattr(args, "list_devices", False):
        print_available_audio_devices(backend)
        return 0

    if backend == "sounddevice":
        return run_live_sounddevice(args)
    return run_live_pvrecorder(args)


def run_denoise(args: argparse.Namespace) -> int:
    input_path = args.input.expanduser().resolve()
    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 1

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}_denoised.wav")
    )

    samples, sample_rate = load_audio(input_path)
    denoised = denoise_signal(
        samples,
        sample_rate,
        noise_reference=args.noise_ref,
        high_pass_hz=args.high_pass_hz,
        noise_reduction_factor=args.noise_reduction_factor,
    )
    write_wav_mono(output_path, denoised, sample_rate)
    print(f"Denoised file written to: {output_path}")

    if args.plot:
        import librosa
        import librosa.display
        import matplotlib.pyplot as plt
        import numpy as np

        original_stft = librosa.stft(samples)
        denoised_stft = librosa.stft(denoised)

        plt.figure(figsize=(12, 10))
        plt.subplot(2, 1, 1)
        librosa.display.specshow(
            librosa.amplitude_to_db(np.abs(original_stft), ref=np.max),
            sr=sample_rate,
            y_axis="log",
            x_axis="time",
        )
        plt.title("Original Spectrogram")
        plt.colorbar(format="%+2.0f dB")

        plt.subplot(2, 1, 2)
        librosa.display.specshow(
            librosa.amplitude_to_db(np.abs(denoised_stft), ref=np.max),
            sr=sample_rate,
            y_axis="log",
            x_axis="time",
        )
        plt.title("Denoised Spectrogram")
        plt.colorbar(format="%+2.0f dB")

        plt.tight_layout()
        plt.show()

    return 0


def run_inat_import(args: argparse.Namespace) -> int:
    jwt_token = ""
    if not args.dry_run:
        jwt_token = resolve_jwt_token(args.token)

    imported_count, updated_count, results = import_csv(
        args.csv,
        jwt_token=jwt_token,
        base_url=args.base_url,
        dry_run=args.dry_run,
    )
    for result in results:
        print(
            f"{result.action}: {result.taxon_name} -> "
            f"id={result.observation_id} "
            f"{result.observation_uuid} ({result.observation_url})"
        )
    print(
        f"iNaturalist import completed for {args.csv}: "
        f"created={imported_count}, updated={updated_count}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        print("No command specified. Avvio in modalità live con parametri predefiniti.")
        args = get_default_live_args()

    if getattr(args, "enable_denoise", False):
        args.disable_denoise = False
    else:
        args.disable_denoise = True

    if args.command == "batch":
        return run_batch(args)
    if args.command == "live":
        return run_live(args)
    if args.command == "denoise":
        return run_denoise(args)
    if args.command == "inat-import":
        return run_inat_import(args)

    parser.error("Unknown command.")
    return 2
