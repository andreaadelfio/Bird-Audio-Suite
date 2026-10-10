from __future__ import annotations

import argparse
import csv
import json
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .audio import (
    append_inaturalist_csv_row,
    apply_clip_span_policy,
    aggregate_detections_by_species,
    day_csv_path,
    denoise_signal,
    export_detection_clips,
    load_audio,
    parse_timestamp_from_name,
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
    DEFAULT_LOCATION_CACHE_FILE,
    DEFAULT_LOCATION_LOOKUP_TIMEOUT_SECONDS,
    DEFAULT_LOCATION_REFRESH_SECONDS,
    DEFAULT_LONGITUDE,
    DEFAULT_PLACE_NAME,
    DEFAULT_RECORDER_SAMPLE_RATE,
    DEFAULT_SLICE_INTERVAL,
    DEFAULT_SPECIES_FILE,
    DEFAULT_NOISE_REDUCTION_FACTOR,
)
from .detector import BirdNetDetector
from .geolocation import resolve_location
from .health import HeartbeatWriter
from .inaturalist import DEFAULT_API_BASE_URL, import_csv, resolve_jwt_token
from .live import LiveDetectionTracker, run_sliding_window_loop
from .repair import apply_repair_plan, build_repair_plan
from .species import SpeciesCatalog


AUDIO_FILE_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
BIRDNET_SAMPLE_RATE = 48000
AUDIO_STALL_TIMEOUT_SECONDS = 30.0
LIVE_MAX_CLIP_SECONDS = 120.0


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

    repair_parser = subparsers.add_parser(
        "repair-detections",
        help="Move clips saved under the wrong day and rebuild the CSVs one row per species per day.",
    )
    repair_parser.add_argument(
        "--detections-dir",
        type=Path,
        default=DEFAULT_DETECTIONS_DIR,
        help=f"Detections folder to repair. Default: {DEFAULT_DETECTIONS_DIR}",
    )
    repair_parser.add_argument(
        "--species-file",
        type=Path,
        default=DEFAULT_SPECIES_FILE,
        help=f"Species catalog used for the names. Default: {DEFAULT_SPECIES_FILE}",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files and rewrite CSVs. Without it only the plan is printed.",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Push detections to Supabase (data) and Cloudinary (audio + spectrograms).",
    )
    sync_parser.add_argument("--detections-dir", type=Path, default=DEFAULT_DETECTIONS_DIR)
    sync_parser.add_argument("--species-file", type=Path, default=DEFAULT_SPECIES_FILE)
    sync_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Credentials file. Default: ~/.config/bird-audio-suite/cloud.env",
    )
    sync_parser.add_argument("--dry-run", action="store_true", help="Only count what would be synced.")
    sync_parser.add_argument("--limit", type=int, help="Sync at most N observations (to try it out).")
    sync_parser.add_argument(
        "--keep-local",
        action="store_true",
        help="Do not remove local WAVs this time, even with DELETE_LOCAL_AUDIO=true.",
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


_SOUNDDEVICE_PROBE_SCRIPT = r'''
import json
import sys
import time

import numpy as np
import sounddevice as sd

index, name, duration = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
sample_rates = [int(rate) for rate in sys.argv[4].split(",")]
settle_seconds = 0.5
devices = sd.query_devices()
if index >= len(devices) or devices[index]["name"] != name:
    index = next(i for i, d in enumerate(devices) if d["name"] == name and d["max_input_channels"] > 0)

errors = []
for sample_rate in sample_rates:
    blocks = []
    try:
        # same stream settings as live mode, so a device that passes here also opens there
        with sd.InputStream(
            device=index,
            channels=1,
            samplerate=sample_rate,
            dtype="float32",
            callback=lambda data, frames, time_info, status: blocks.append(data[:, 0].copy()),
        ):
            time.sleep(settle_seconds + duration)
    except Exception as exc:
        errors.append(f"{sample_rate} Hz: {exc}")
        continue
    mono = np.concatenate(blocks) if blocks else np.empty(0, dtype=np.float32)
    # drop the click some ALSA plugins emit when opened: it made silent inputs look loud
    mono = mono[int(sample_rate * settle_seconds):]
    print(json.dumps({
        "rms": float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0,
        "peak": float(np.max(np.abs(mono))) if mono.size else 0.0,
        "samplerate": sample_rate,
    }))
    sys.exit(0)
sys.exit("; ".join(errors) or "no sample rate to try")
'''


def probe_sample_rates(default_rate: int) -> list[int]:
    """Sample rates to try, best first: BirdNET works at 48 kHz."""
    rates = [BIRDNET_SAMPLE_RATE]
    if default_rate > 0 and default_rate not in rates:
        rates.append(int(default_rate))
    return rates


def _probe_sounddevice_input(device: dict, duration_seconds: float, timeout_seconds: float) -> dict:
    """Record from one input in a child process, with the same stream settings as live mode.

    Some ALSA plugins (e.g. ``speex``, ``upmix``) segfault inside PortAudio: in-process
    that kills the service before the chosen device is saved, so it crash-loops forever.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _SOUNDDEVICE_PROBE_SCRIPT,
            str(device["index"]),
            str(device["name"]),
            str(duration_seconds),
            ",".join(str(rate) for rate in probe_sample_rates(int(device.get("samplerate") or 0))),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        reason = (
            f"crashed (signal {-completed.returncode})"
            if completed.returncode < 0
            else f"exit code {completed.returncode}"
        )
        detail = (completed.stderr.strip().splitlines() or [""])[-1]
        raise RuntimeError(f"{reason} {detail}".strip())
    return json.loads(completed.stdout.strip().splitlines()[-1])


def probe_sounddevice_inputs(
    devices: list[dict],
    duration_seconds: float = 1.0,
) -> list[dict]:
    measurements: list[dict] = []
    duration_seconds = max(float(duration_seconds), 0.1)
    print(f"Provo i device audio per {duration_seconds:.1f}s ciascuno...")

    for device in devices:
        try:
            result = _probe_sounddevice_input(
                device,
                duration_seconds,
                timeout_seconds=duration_seconds + 20.0,
            )
        except Exception as exc:
            print(f"  {device['index']}: {device['name']} -> errore: {exc}")
            continue

        measurements.append(
            {
                "index": device["index"],
                "name": device["name"],
                "rms": float(result["rms"]),
                "peak": float(result["peak"]),
                "samplerate": int(result["samplerate"]),
            }
        )
        print(
            f"  {device['index']}: {device['name']} "
            f"RMS={result['rms']:.4f} PEAK={result['peak']:.4f} @ {result['samplerate']} Hz"
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


SOUND_SERVER_DEVICE_NAMES = {"default", "pulse", "pipewire"}


def is_sound_server_device(name: str) -> bool:
    return name.strip().lower() in SOUND_SERVER_DEVICE_NAMES


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
    # Among inputs with real signal prefer, in order:
    # - full bandwidth: at 16 kHz BirdNET loses everything above 8 kHz;
    # - the sound server (PipeWire/PulseAudio): raw hw:X,Y devices are exclusive, so they
    #   are "unavailable" when the desktop holds them and steal the mic from calls;
    # - the loudest one.
    selected_measurement = max(
        valid_measurements,
        key=lambda item: (
            int(item.get("samplerate", 0)) >= 44100,
            is_sound_server_device(item["name"]),
            item["rms"],
            item["peak"],
        ),
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


def is_audio_file(path: Path) -> bool:
    # case-insensitive: recorders such as AudioMoth write ".WAV"
    return path.is_file() and path.suffix.lower() in AUDIO_FILE_SUFFIXES


def collect_batch_files(file_args: list[str] | None, directory: Path | None, recursive: bool) -> list[Path]:
    files: list[Path] = []

    if file_args:
        files.extend(Path(item).expanduser().resolve() for item in file_args)

    if directory:
        directory = directory.expanduser().resolve()
        iterator = directory.rglob("*") if recursive else directory.glob("*")
        files.extend(sorted(path.resolve() for path in iterator if is_audio_file(path)))

    if not files:
        files.extend(sorted(path.resolve() for path in Path.cwd().glob("*") if is_audio_file(path)))

    unique_files: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            unique_files.append(path)

    return unique_files


def today_folder(base_dir: Path) -> Path:
    """Folder of the current day; computed on each use so long runs follow the calendar."""
    return day_csv_path(base_dir, datetime.now()).parent


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
                "english_name": (row.get("english_name") or "").strip(),
                "italian_name": (row.get("italian_name") or "").strip(),
                "german_name": (row.get("german_name") or "").strip(),
            }
    return stats


_REMOTE_SUMMARY: dict = {"loaded": False, "client": None}


def remote_daily_rows(day: str) -> list[dict] | None:
    """Observations of `day` (YYYY-MM-DD) on Supabase, rejected excluded; None if not configured
    or unreachable. Short timeout: it runs in the live loop."""
    if not _REMOTE_SUMMARY["loaded"]:
        _REMOTE_SUMMARY["loaded"] = True
        try:
            from .cloud import DEFAULT_CLOUD_CONFIG, CloudConfig, SupabaseRest

            config = CloudConfig.load(DEFAULT_CLOUD_CONFIG)
            if config.supabase_url and config.supabase_key and config.storage_backend != "local":
                _REMOTE_SUMMARY["client"] = SupabaseRest(config, timeout=4)
        except Exception:
            _REMOTE_SUMMARY["client"] = None
    client = _REMOTE_SUMMARY["client"]
    if client is None:
        return None
    try:
        return client.get(
            "daily_summary",
            {
                "select": "scientific_name,max_confidence,last_heard",
                "day": f"eq.{day}",
                "status": "neq.rejected",
            },
        )
    except Exception:
        return None


def merge_daily_records(local: list[dict[str, str]], remote: list[dict], stats: dict) -> list[dict[str, str]]:
    """One row per species: what is on Supabase plus what still waits in the transit folder,
    with the latest time and the highest confidence."""
    merged: dict[str, dict[str, str]] = {}

    def add(taxon: str, time_text: str, confidence: float) -> None:
        names = stats.get(taxon, {})
        current = merged.get(taxon)
        if current is None:
            merged[taxon] = {
                "taxon_name": taxon,
                "time": time_text,
                "description": f"{names.get('english_name', '')}, {names.get('italian_name', '')}, "
                f"{names.get('german_name', '')}; birdnet_confidence={confidence:.3f}",
                "rarity": names.get("rarity", "unseen"),
                "avg_counts": f"{float(names.get('daily_average', '0') or 0):.2f}/day",
                "_confidence": f"{confidence:.6f}",
            }
            return
        best = max(confidence, float(current["_confidence"]))
        current["time"] = max(current["time"], time_text)
        current["_confidence"] = f"{best:.6f}"
        current["description"] = re.sub(r"birdnet_confidence=[0-9.]+", f"birdnet_confidence={best:.3f}", current["description"])

    for row in remote:
        heard = datetime.fromisoformat(str(row["last_heard"]).replace("Z", "+00:00")).astimezone()
        add(row["scientific_name"], heard.strftime("%Y-%m-%d %H:%M:%S"), float(row.get("max_confidence") or 0))
    for record in local:
        _en, _it, _de, confidence = split_description(record.get("description", ""))
        add(record["taxon_name"], record["time"], float(confidence or 0))
    rows = sorted(merged.values(), key=lambda row: row["time"], reverse=True)
    for row in rows:
        row.pop("_confidence", None)
    return rows


def load_daily_observation_rows(
    day_dir: Path,
    species_file: Path = DEFAULT_SPECIES_FILE,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    species_stats = load_species_frequency_stats(species_file)
    for csv_path in sorted(Path(day_dir).glob("inaturalist_import_*.csv")):
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
                        "avg_counts": f"{float(species_stats.get(taxon_name, {}).get('daily_average', '0.000')):.2f}/day",
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


def _format_summary_time(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
    except ValueError:
        return value


def print_daily_detection_summary(
    day_dir: Path,
    title: str = "",
    species_file: Path = DEFAULT_SPECIES_FILE,
) -> None:
    records = load_daily_observation_rows(day_dir, species_file)
    # detections/ is only a transit folder: what is already online comes from Supabase
    day_name = Path(day_dir).name
    if len(day_name) == 8 and day_name.isdigit():
        remote = remote_daily_rows(f"{day_name[:4]}-{day_name[4:6]}-{day_name[6:]}")
        if remote is not None:
            records = merge_daily_records(records, remote, load_species_frequency_stats(species_file))

    # Always emit clear/resize escapes, even under systemd: the journal is shown in a
    # terminal (journalctl -f, Ctrl+Alt+L shortcut) that executes them, leaving only the table.
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
                "time": _format_summary_time(record.get("time", "")),
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
    print(f"\033[8;{len(rows) + 2};{len(header)}t", end="")
    print(header)
    for row in rows:
        print("|".join(row[header].ljust(widths[header]) for header in headers))


def record_live_detection_observation(
    *,
    scientific_name: str,
    english_name: str,
    confidence: float,
    species_catalog: SpeciesCatalog,
    detections_dir: Path,
    args: argparse.Namespace,
    observed_at: datetime | None = None,
) -> datetime:
    observed_at = observed_at or datetime.now()
    csv_path = day_csv_path(detections_dir, observed_at)
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
        csv_path,
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
    print_daily_detection_summary(csv_path.parent, species_file=species_catalog.path)
    return observed_at


def resolve_runtime_location(args: argparse.Namespace, only_report_changes: bool = False) -> bool:
    """Update args.lat/lon/place_name; return True if the position changed."""
    previous = (getattr(args, "lat", None), getattr(args, "lon", None), getattr(args, "place_name", None))
    resolved = resolve_location(
        fallback_latitude=float(getattr(args, "lat", DEFAULT_LATITUDE)),
        fallback_longitude=float(getattr(args, "lon", DEFAULT_LONGITUDE)),
        fallback_place_name=getattr(args, "place_name", DEFAULT_PLACE_NAME) or DEFAULT_PLACE_NAME,
        enable_auto_lookup=DEFAULT_ENABLE_AUTO_LOCATION,
        timeout_seconds=DEFAULT_LOCATION_LOOKUP_TIMEOUT_SECONDS,
        cache_path=DEFAULT_LOCATION_CACHE_FILE,
    )
    if only_report_changes and resolved.used_fallback:
        return False  # offline during a refresh: keep the position we already have
    args.lat = resolved.latitude
    args.lon = resolved.longitude
    args.place_name = resolved.place_name
    changed = previous != (args.lat, args.lon, args.place_name)
    if changed or not only_report_changes:
        source_label = "fallback config" if resolved.source == "config" else resolved.source
        print(
            f"{datetime.now():%H:%M:%S} - Observation location: "
            f"{args.place_name} ({args.lat:.6f}, {args.lon:.6f}) [{source_label}]"
        )
    return changed


def start_location_refresh(args: argparse.Namespace, on_change, interval_seconds: float) -> threading.Event:
    """Look the position up again every ``interval_seconds`` in a daemon thread."""
    stop = threading.Event()

    def refresh() -> None:
        while not stop.wait(interval_seconds):
            try:
                if resolve_runtime_location(args, only_report_changes=True):
                    on_change()
            except Exception as exc:
                print(f"{datetime.now():%H:%M:%S} - Location refresh failed: {exc!r}")

    threading.Thread(target=refresh, name="location-refresh", daemon=True).start()
    return stop


def infer_recording_start(file_path: Path, duration_seconds: float) -> tuple[datetime, str]:
    """Best guess of when a recording started, with the source of the guess.

    The ctime used before is the inode change time on Linux (i.e. when the file was
    copied), so it dated old recordings to the day they were analyzed.
    """
    parsed = parse_timestamp_from_name(file_path.name)
    if parsed is not None:
        return parsed, "file name"
    # mtime is when the recorder finished writing: step back to the start
    modified_at = datetime.fromtimestamp(file_path.stat().st_mtime)
    return modified_at - timedelta(seconds=duration_seconds), "file modification time"


def audio_duration_seconds(file_path: Path) -> float:
    import librosa

    return float(librosa.get_duration(path=str(file_path)))


def run_batch(args: argparse.Namespace) -> int:
    files = collect_batch_files(args.files, args.directory, args.recursive)
    if not files:
        print("No audio files found.")
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
            if analysis_samples is not None:
                duration_seconds = len(analysis_samples) / float(analysis_rate)
            else:
                duration_seconds = audio_duration_seconds(file_path)
            recording_start, start_source = infer_recording_start(file_path, duration_seconds)
            print(
                f"{file_path.name}: {duration_seconds:.1f}s recorded at "
                f"{recording_start:%Y-%m-%d %H:%M:%S} (from {start_source})"
            )

            detections = detector.detect(
                analysis_path,
                latitude=args.lat,
                longitude=args.lon,
                when=recording_start,
                min_confidence=args.min_confidence,
            )
            grouped = aggregate_detections_by_species(
                detections,
                duration_seconds=duration_seconds,
                min_confidence=args.min_confidence,
            )
            if not grouped:
                print("  no detections")
                continue

            for scientific_name, (start_sec, _end_sec, confidence, english_name) in sorted(
                grouped.items(),
                key=lambda item: item[1][0],
            ):
                heard_at = recording_start + timedelta(seconds=start_sec)
                print(f"  {heard_at:%Y-%m-%d %H:%M:%S}  {scientific_name} ({english_name})  conf={confidence:.3f}")
                # one observation per species per file, like one event per species in live mode
                species_catalog.ensure_species(
                    [{"scientific_name": scientific_name, "common_name": english_name, "confidence": confidence}],
                    observed_at=heard_at,
                )

            if args.export_clips:
                if analysis_samples is None or analysis_rate is None:
                    analysis_samples, analysis_rate = load_audio(file_path)

                clip_spans = apply_clip_span_policy(
                    grouped,
                    duration_seconds=duration_seconds,
                    clip_span=getattr(args, "clip_span", DEFAULT_BATCH_CLIP_SPAN),
                )
                exported = export_detection_clips(
                    analysis_samples,
                    analysis_rate,
                    clip_spans,
                    species_catalog,
                    args.detections_dir,
                    latitude=args.lat,
                    longitude=args.lon,
                    place_name=args.place_name,
                    tags=DEFAULT_INATURALIST_TAGS,
                    geoprivacy=DEFAULT_INATURALIST_GEOPRIVACY,
                    observed_at=recording_start,
                )
                for path in exported:
                    print(f"  exported {path}")
        finally:
            if analysis_path != file_path and analysis_path.exists():
                analysis_path.unlink()

    return 0


def detect_live_slice(
    raw_samples,
    sample_rate: int,
    detector: BirdNetDetector,
    args: argparse.Namespace,
):
    """Detect on a live slice and return grouped detections and the working samples.

    This function does NOT export clips; the caller is responsible for aggregating
    detections across slices and exporting when appropriate.
    """
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

    def detect(samples):
        return detector.detect_samples(
            samples,
            sample_rate,
            latitude=args.lat,
            longitude=args.lon,
            when=datetime.now(),
            min_confidence=args.min_confidence,
        )

    detections = detect(working_samples)
    if not detections and not args.disable_denoise:
        processing_mode = "raw-fallback"
        working_samples = raw_samples
        detections = detect(working_samples)

    if getattr(args, "verbose", False):
        print(
            f"{datetime.now():%H:%M:%S} - "
            f"mode={processing_mode} "
            f"raw_rms={rms_level(raw_samples):.4f} "
            f"used_rms={rms_level(working_samples):.4f} "
            f"detections={len(detections)}"
        )

    duration_seconds = len(working_samples) / float(sample_rate)
    grouped = aggregate_detections_by_species(
        detections,
        duration_seconds=duration_seconds,
        min_confidence=args.min_confidence,
    )
    return grouped, working_samples


def print_live_header(args: argparse.Namespace, device_label: str, sample_rate: int) -> None:
    print(f"{datetime.now():%H:%M:%S} - Working directory: {Path.cwd()}")
    print(f"{datetime.now():%H:%M:%S} - Saving detections to: {args.detections_dir}/YYYYMMDD")
    print(f"{datetime.now():%H:%M:%S} - Device: {device_label}")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")


def run_live_detection(
    args: argparse.Namespace,
    frames,
    sample_rate: int,
    species_catalog: SpeciesCatalog,
    detector: BirdNetDetector,
    device_label: str = "",
) -> None:
    """Shared live pipeline for every backend: ``frames`` yields float32 blocks in [-1, 1]."""
    args.detections_dir = Path(args.detections_dir)
    heartbeat = HeartbeatWriter()

    def record_observation(scientific_name: str, english_name: str, confidence: float, heard_at: datetime) -> None:
        record_live_detection_observation(
            scientific_name=scientific_name,
            english_name=english_name,
            confidence=confidence,
            species_catalog=species_catalog,
            detections_dir=args.detections_dir,
            args=args,
            observed_at=heard_at,
        )

    tracker = LiveDetectionTracker(
        sample_rate=sample_rate,
        detections_dir=args.detections_dir,
        species_catalog=species_catalog,
        record_observation=record_observation,
        export_kwargs={
            "latitude": args.lat,
            "longitude": args.lon,
            "place_name": args.place_name,
            "tags": DEFAULT_INATURALIST_TAGS,
            "geoprivacy": DEFAULT_INATURALIST_GEOPRIVACY,
        },
        clip_span=getattr(args, "clip_span", DEFAULT_LIVE_CLIP_SPAN),
        max_clip_seconds=LIVE_MAX_CLIP_SECONDS,
    )
    def on_exported(paths: list[Path]) -> None:
        print_daily_detection_summary(today_folder(args.detections_dir), species_file=species_catalog.path)
        if args.verbose:
            for path in paths:
                print(f"{datetime.now():%H:%M:%S} - Exported {path}")

    def on_error(exc: Exception) -> None:
        print(f"{datetime.now():%H:%M:%S} - Detection error: {exc!r}")

    def detect_window(window):
        result = detect_live_slice(window, sample_rate, detector, args)
        heartbeat.beat(rms_level(window), device=device_label, place_name=args.place_name)
        return result

    def on_location_change() -> None:
        tracker.export_kwargs.update(latitude=args.lat, longitude=args.lon, place_name=args.place_name)

    stop_refresh = start_location_refresh(args, on_location_change, DEFAULT_LOCATION_REFRESH_SECONDS)
    print_daily_detection_summary(today_folder(args.detections_dir), species_file=species_catalog.path)
    try:
        run_sliding_window_loop(
            frames,
            sample_rate=sample_rate,
            slice_frames=args.slice_interval,
            detect_window=detect_window,
            tracker=tracker,
            on_exported=on_exported,
            on_error=on_error,
        )
    finally:
        stop_refresh.set()


def run_live_pvrecorder(args: argparse.Namespace) -> int:
    import numpy as np
    from pvrecorder import PvRecorder

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    detector = BirdNetDetector()
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
    print_live_header(args, f"{args.device_index} ({selected_device_name})", sample_rate)

    def frames():
        while True:
            # PvRecorder returns int16 values: scale them to [-1, 1] like sounddevice
            yield np.asarray(recorder.read(), dtype=np.int16).astype(np.float32) / 32768.0

    started = False
    try:
        recorder.start()
        started = True
        run_live_detection(args, frames(), sample_rate, species_catalog, detector, selected_device_name)
    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        return 0
    finally:
        if started:
            recorder.stop()
        recorder.delete()
    return 0


def choose_sounddevice_sample_rate(device: int | None, default_rate: int) -> int:
    """Prefer at least 44.1 kHz: BirdNET works at 48 kHz and analyzes up to ~15 kHz,
    so a 16 kHz input (8 kHz Nyquist) hides many high-pitched songs."""
    import sounddevice as sd

    if default_rate >= 44100:
        return default_rate
    try:
        sd.check_input_settings(device=device, samplerate=BIRDNET_SAMPLE_RATE, channels=1, dtype="float32")
        return BIRDNET_SAMPLE_RATE
    except Exception:
        return default_rate


def rechunk_frames(blocks, frame_length: int):
    """Yield fixed-size frames from blocks of any size."""
    import numpy as np

    pending = np.empty(0, dtype=np.float32)
    for block in blocks:
        pending = np.concatenate((pending, np.asarray(block, dtype=np.float32).reshape(-1)))
        while pending.size >= frame_length:
            yield pending[:frame_length]
            pending = pending[frame_length:]


def run_live_sounddevice(args: argparse.Namespace) -> int:
    import sounddevice as sd

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    detector = BirdNetDetector()
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
    saved_state = load_sounddevice_state()
    if selected_device and saved_state.get("name") == selected_device["name"] and saved_state.get("samplerate"):
        # the probe opened this device with exactly these settings
        sample_rate = int(saved_state["samplerate"])
    else:
        sample_rate = choose_sounddevice_sample_rate(device, int(sample_rate))
    print_live_header(args, f"{args.device_index} ({selected_device_name})", sample_rate)

    audio_queue: queue.Queue = queue.Queue()
    status_queue: queue.Queue[str] = queue.Queue()

    def audio_callback(indata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            status_queue.put(str(status))
        audio_queue.put(indata[:, 0].copy())

    def blocks():
        while True:
            try:
                block = audio_queue.get(timeout=AUDIO_STALL_TIMEOUT_SECONDS)
            except queue.Empty:
                # e.g. USB microphone unplugged: fail so systemd restarts us, instead of hanging
                raise RuntimeError(
                    f"no audio received for {AUDIO_STALL_TIMEOUT_SECONDS:.0f}s from device {selected_device_name}"
                ) from None
            while not status_queue.empty():
                message = status_queue.get_nowait()
                if args.verbose:
                    print(f"{datetime.now():%H:%M:%S} - audio status: {message}")
            yield block

    try:
        # blocksize=0 lets PortAudio pick the period: ALSA plugins such as "samplerate"
        # reject a fixed 1536-frame block at 44.1 kHz (ALSA error -22). Frames of
        # args.frame_length are rebuilt in software, so the slice duration is unchanged.
        stream = sd.InputStream(
            device=device,
            channels=1,
            samplerate=sample_rate,
            dtype="float32",
            callback=audio_callback,
        )
    except Exception as exc:
        print(f"\nCannot open audio device {args.device_index} ({selected_device_name}): {exc}")
        # forget the cached choice so the next (re)start probes the inputs again
        DEFAULT_AUDIO_DEVICE_STATE_FILE.unlink(missing_ok=True)
        return 1

    try:
        with stream:
            run_live_detection(
                args,
                rechunk_frames(blocks(), args.frame_length),
                sample_rate,
                species_catalog,
                detector,
                selected_device_name,
            )
    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        return 0
    except Exception as exc:
        print(f"\nLive audio error ({args.backend}): {exc}")
        traceback.print_exc()
        return 1
    return 0


def _raise_keyboard_interrupt(signum, frame) -> None:
    del signum, frame
    raise KeyboardInterrupt


def run_live(args: argparse.Namespace) -> int:
    backend = getattr(args, "backend", DEFAULT_LIVE_BACKEND)
    if backend == "auto":
        backend = DEFAULT_LIVE_BACKEND

    if getattr(args, "list_devices", False):
        print_available_audio_devices(backend)
        return 0

    # systemd stops the service with SIGTERM: handle it like Ctrl+C so open clips are saved
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        if backend == "sounddevice":
            return run_live_sounddevice(args)
        return run_live_pvrecorder(args)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


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


def run_repair_detections(args: argparse.Namespace) -> int:
    plan = build_repair_plan(args.detections_dir, SpeciesCatalog(args.species_file))
    moves = plan.moves
    print(f"CSV letti: {len(plan.source_csvs)}")
    print(f"Osservazioni (specie x giorno) dopo la riparazione: {len(plan.observations)}")
    print(f"Clip da spostare nel giorno corretto: {len(moves)}")
    by_folder: dict[tuple[str, str], int] = {}
    for move in moves:
        key = (move.source.parent.parent.name, move.target.parent.parent.name)
        by_folder[key] = by_folder.get(key, 0) + 1
    for (source_day, target_day), count in sorted(by_folder.items()):
        print(f"  {source_day} -> {target_day}: {count}")
    if plan.missing_clips:
        print(f"Clip citate nei CSV ma non trovate: {len(plan.missing_clips)}")
        for name in plan.missing_clips[:20]:
            print(f"  {name}")
    if plan.unreferenced_clips:
        print(f"Clip non citate in nessun CSV (lasciate dove sono): {len(plan.unreferenced_clips)}")
        for path in plan.unreferenced_clips[:20]:
            print(f"  {path.relative_to(plan.detections_dir)}")

    if not args.apply:
        print("\nDry run: nessun file modificato. Rilancia con --apply per applicare.")
        return 0

    backup_dir = apply_repair_plan(plan)
    print(f"\nFatto. Backup dei CSV originali e lista degli spostamenti in: {backup_dir}")
    return 0


def run_sync(args: argparse.Namespace) -> int:
    from .cloud import DEFAULT_CLOUD_CONFIG, CloudConfig, DetectionSync

    config = CloudConfig.load(args.config or DEFAULT_CLOUD_CONFIG)
    if args.keep_local:
        config.delete_local_audio = False
    print(
        f"Supabase: {config.supabase_url or '(non configurato)'} | "
        f"media: {config.storage_backend} | "
        f"WAV locali: {'eliminati dopo il caricamento' if config.delete_local_audio else 'tenuti'}"
    )
    sync = DetectionSync(args.detections_dir, SpeciesCatalog(args.species_file), config)
    report = sync.run(dry_run=args.dry_run, limit=args.limit)
    print(
        f"Osservazioni: {report.observations_synced} sincronizzate su {report.observations_total}, "
        f"clip caricate: {report.clips_uploaded}, specie: {report.species_synced}"
    )
    if config.delete_local_audio:
        verb = "da eliminare" if args.dry_run else "eliminati"
        print(f"WAV locali {verb}: {report.local_deleted} ({report.local_bytes_freed / 1e6:.0f} MB)")
        if not args.dry_run:
            print(f"CSV svuotati e tolti da detections/: {report.csv_removed}")
    for error in report.errors:
        print(f"  errore: {error}")
    return 1 if report.errors else 0


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
    if args.command == "repair-detections":
        return run_repair_detections(args)
    if args.command == "sync":
        return run_sync(args)

    parser.error("Unknown command.")
    return 2
