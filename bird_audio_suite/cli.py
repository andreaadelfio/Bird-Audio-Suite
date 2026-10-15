from __future__ import annotations

import argparse
import json
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
import time
from datetime import datetime, timedelta
from pathlib import Path

from .analysis import BAND_MIN_CONFIDENCE
from .audio import (
    apply_clip_span_policy,
    aggregate_detections_by_species,
    detection_segments,
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
    DEFAULT_ENABLE_PLAYBACK_SUBTRACTION,
    DEFAULT_FRAME_LENGTH,
    DEFAULT_HIGH_PASS_HZ,
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
    DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE,
    LIKELY_CONFIDENCE,
    DEFAULT_RECORDER_SAMPLE_RATE,
    DEFAULT_SLICE_INTERVAL,
    DEFAULT_NOISE_REDUCTION_FACTOR,
)
from .detector import BirdNetDetector
from .echo import ACTIVE_RMS, PlaybackMonitor, PlaybackSubtractor
from .geolocation import resolve_location
from .health import HeartbeatWriter
from .inputgain import SOUND_SERVER_DEVICES, SourceVolumeCompensation
from .metrics import LiveMetrics
from .uploader import read_status as read_uploader_status, status_line as uploader_status_line
from .live import LiveDetectionTracker, run_sliding_window_loop
from .species import SpeciesNames


AUDIO_FILE_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
BIRDNET_SAMPLE_RATE = 48000
AUDIO_STALL_TIMEOUT_SECONDS = 30.0
LIVE_MAX_CLIP_SECONDS = 120.0
TABLE_REFRESH_SECONDS = 60.0
# bands on the spectrogram: where BirdNET hears the clip's species, also weakly, so you see the
# whole visit; the site colours them likely (>= 0.8) or unsure. The same threshold as the analysis
# of the saved clip (analysis.py), which replaces these bands once the clip is on Supabase
SEGMENT_MIN_CONFIDENCE = BAND_MIN_CONFIDENCE
# BirdNET's 3 s chunks every 1 s: where a call falls inside a chunk changes its score a lot (a
# chiffchaff heard at 0.95 scored 0.42 in fixed chunks), so the live missed clear birds
LIVE_OVERLAP_SECONDS = 2.0


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
        "--detections-dir",
        type=Path,
        default=DEFAULT_DETECTIONS_DIR,
        help=f"Transit folder: clips wait here until they are on Supabase. Default: {DEFAULT_DETECTIONS_DIR}",
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
        "--detections-dir",
        type=Path,
        default=DEFAULT_DETECTIONS_DIR,
        help=f"Transit folder: clips wait here until they are on Supabase. Default: {DEFAULT_DETECTIONS_DIR}",
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
    live_parser.add_argument(
        "--enable-playback-subtraction",
        dest="enable_playback_subtraction",
        action="store_true",
        default=DEFAULT_ENABLE_PLAYBACK_SUBTRACTION,
        help="Subtract what the PC is playing from the microphone before detection.",
    )
    live_parser.add_argument(
        "--disable-playback-subtraction",
        dest="enable_playback_subtraction",
        action="store_false",
        help="Do not subtract PC playback from the microphone.",
    )
    live_parser.add_argument(
        "--playback-suppression-min-confidence",
        type=float,
        default=DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE,
        help=(
            "When PC playback is detected as a bird, suppress that species from live observations. "
            f"Default: {DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE}"
        ),
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

    sync_parser = subparsers.add_parser(
        "sync",
        help="Send what waits in the transit folder to Supabase (with --watch: the uploader service).",
    )
    sync_parser.add_argument("--detections-dir", type=Path, default=DEFAULT_DETECTIONS_DIR)
    sync_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Settings file. Default: ~/.config/bird-audio-suite/cloud.env",
    )
    sync_parser.add_argument("--limit", type=int, help="Send at most N clips (to try it out).")
    sync_parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running: send each clip as soon as the live writes it (the bird-audio-suite-sync service).",
    )

    subparsers.add_parser(
        "refresh-species",
        help="Fill the eBird and iNaturalist codes of the species on Supabase that lack them.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-segments",
        help="Find with BirdNET where the species is heard in clips that were saved without it.",
    )
    backfill_parser.add_argument("--limit", type=int, help="At most N clips.")
    backfill_parser.add_argument("--retry-empty", action="store_true",
                                 help="Also clips already analysed where nothing was found.")
    backfill_parser.add_argument("--all", action="store_true",
                                 help="Every clip with its files, again (after a change of the band threshold).")

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
        enable_playback_subtraction=DEFAULT_ENABLE_PLAYBACK_SUBTRACTION,
        playback_suppression_min_confidence=DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE,
        noise_ref=None,
        high_pass_hz=DEFAULT_HIGH_PASS_HZ,
        noise_reduction_factor=DEFAULT_NOISE_REDUCTION_FACTOR,
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
            enable_playback_subtraction=prompt_bool(
                "Sottrarre l'audio riprodotto dal PC dal microfono",
                DEFAULT_ENABLE_PLAYBACK_SUBTRACTION,
            ),
            playback_suppression_min_confidence=DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE,
            noise_ref=noise_ref,
            high_pass_hz=high_pass_hz,
            noise_reduction_factor=noise_reduction_factor,
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


# ---------------------------------------------------------------- live table (Ctrl+Alt+L)


class LiveToday:
    """Species whose clip is still open, so the table shows them at once. Only until the clip is
    written: from then on the table takes it from the transit folder and, once sent, from Supabase,
    the one truth (a clip deleted or an observation rejected on the site must not come back)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[tuple[str, str], dict] = {}

    def add(self, scientific_name: str, english_name: str, confidence: float, heard_at: datetime) -> None:
        key = (heard_at.date().isoformat(), scientific_name)
        with self.lock:
            row = self.rows.setdefault(key, {"scientific_name": scientific_name, "english_name": english_name,
                                             "time": heard_at, "confidence": confidence})
            row["time"] = max(row["time"], heard_at)
            row["confidence"] = max(row["confidence"], confidence)

    def of_day(self, day: str) -> list[dict]:
        with self.lock:
            return [dict(row) for (row_day, _), row in self.rows.items() if row_day == day]

    def clips_written(self, paths) -> None:
        """Their clips are in the transit folder now: forget those species (next call reopens)."""
        for path in paths:
            try:
                data = json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            day = datetime.fromisoformat(data["recorded_at"]).date().isoformat()
            with self.lock:
                self.rows.pop((day, data.get("scientific_name", "")), None)


LIVE_TODAY = LiveToday()
LIVE_METRICS = LiveMetrics()
_PRINT_LOCK = threading.Lock()
_REMOTE: dict = {"loaded": False, "client": None}


def _remote_client():
    if not _REMOTE["loaded"]:
        _REMOTE["loaded"] = True
        try:
            from .cloud import DEFAULT_CLOUD_CONFIG, CloudConfig, SupabaseRest

            config = CloudConfig.load(DEFAULT_CLOUD_CONFIG)
            if config.configured and config.media_storage != "local":
                _REMOTE["client"] = SupabaseRest(config, timeout=4)  # short: it runs in the live loop
        except Exception:
            _REMOTE["client"] = None
    return _REMOTE["client"]


def remote_daily_rows(day: str) -> list[dict] | None:
    """Observations of `day` (YYYY-MM-DD) on Supabase, rejected ones excluded; None if offline."""
    client = _remote_client()
    if client is None:
        return None
    try:
        return client.get(
            "daily_summary",
            {
                "select": "scientific_name,english_name,italian_name,german_name,max_confidence,last_heard,rarity,daily_average",
                "day": f"eq.{day}",
                "status": "neq.rejected",
            },
        )
    except Exception:
        return None


def pending_clip_rows(detections_dir: Path, day: str) -> list[dict]:
    """Clips of `day` still waiting in the transit folder."""
    from .cloud import outbox_items

    rows = []
    for item in outbox_items(detections_dir):
        heard_at = item.recorded_at.astimezone()
        if heard_at.date().isoformat() == day:
            rows.append({"scientific_name": item.data["scientific_name"], "english_name": item.data.get("english_name", ""),
                         "time": heard_at, "confidence": float(item.data.get("confidence") or 0)})
    return rows


def merge_daily_records(remote: list[dict], local: list[dict], names: SpeciesNames | None) -> list[dict[str, str]]:
    """One row per species: Supabase plus what is not there yet, latest time, highest confidence."""
    merged: dict[str, dict] = {}
    for row in remote:
        merged[row["scientific_name"]] = {
            "time": datetime.fromisoformat(str(row["last_heard"]).replace("Z", "+00:00")).astimezone(),
            "confidence": float(row.get("max_confidence") or 0),
            "english": row.get("english_name") or "",
            "italian": row.get("italian_name") or "",
            "german": row.get("german_name") or "",
            "rarity": row.get("rarity") or "",
            "average": row.get("daily_average"),
        }
    for row in local:
        name = row["scientific_name"]
        time_heard = row["time"] if row["time"].tzinfo else row["time"].astimezone()
        current = merged.get(name)
        if current is None:
            italian, german, english = names.display_names(name, row.get("english_name", "")) if names else ("", "", "")
            merged[name] = {"time": time_heard, "confidence": row["confidence"], "english": english,
                            "italian": italian, "german": german, "rarity": "", "average": None}
        else:
            current["time"] = max(current["time"], time_heard)
            current["confidence"] = max(current["confidence"], row["confidence"])
    ordered = sorted(merged.items(), key=lambda item: item[1]["time"], reverse=True)
    return [
        {
            "time": data["time"].strftime("%H:%M:%S"),
            "taxon_name": name,
            "rarity": data["rarity"] or "-",
            "avg_counts": f"{float(data['average']):.2f}/day" if data["average"] is not None else "-",
            "taxon_en": data["english"],
            "taxon_ita": data["italian"],
            "taxon_ger": data["german"],
            "conf": f"{data['confidence']:.3f}",
        }
        for name, data in ordered
    ]


def print_daily_detection_summary(detections_dir: Path, names: SpeciesNames | None = None, title: str = "") -> None:
    """Today's table: Supabase (the shared truth, reviews included) plus what is still on its way."""
    day = datetime.now().date().isoformat()
    remote = remote_daily_rows(day)
    local = pending_clip_rows(detections_dir, day) + LIVE_TODAY.of_day(day)
    rows = merge_daily_records(remote or [], local, names)

    with _PRINT_LOCK:
        # Always emit clear/resize escapes, even under systemd: the journal is shown in a
        # terminal (journalctl -f, Ctrl+Alt+L shortcut) that executes them, leaving only the table.
        print("\033[2J\033[H", end="")
        if title:
            print(title)
        if remote is None:
            print("(Supabase non raggiungibile: solo i dati di questo PC)")
        # how fast and how heavy the live is, under the table (see metrics.py)
        performance = LIVE_METRICS.summary_lines()
        footer = ["", *performance, uploader_status_line(read_uploader_status())]
        if not rows:
            print("\n".join(["No observations today.", *footer]))
            return
        headers = ["time", "taxon_name", "rarity", "avg_counts", "taxon_en", "taxon_ita", "taxon_ger", "conf"]
        widths = {header: max(len(header), max(len(row[header]) for row in rows)) for header in headers}
        header = "|".join(header.ljust(widths[header]) for header in headers)
        width = max([len(header), *map(len, footer)])
        print(f"\033[8;{len(rows) + 2 + len(footer)};{width}t", end="")
        print(header)
        for row in rows:
            print("|".join(colour_confidence(row[header].ljust(widths[header])) if header == "conf"
                           else row[header].ljust(widths[header]) for header in headers))
        for line in footer:
            print(line)


def colour_confidence(text: str) -> str:
    """Likely (>= 0.8) green, unsure yellow, like on the site (the terminal of Ctrl+Alt+L shows it)."""
    try:
        value = float(text)
    except ValueError:
        return text
    return f"\033[{32 if value >= LIKELY_CONFIDENCE else 33}m{text}\033[0m"


def record_live_detection_observation(
    *,
    scientific_name: str,
    english_name: str,
    confidence: float,
    species_names: SpeciesNames,
    detections_dir: Path,
    observed_at: datetime | None = None,
) -> datetime:
    """A species just heard: name it (Wikipedia for a new one) and show it in the table at once.
    Its clip reaches Supabase when it closes."""
    observed_at = observed_at or datetime.now()
    species_names.ensure(scientific_name, english_name)
    LIVE_TODAY.add(scientific_name, english_name, confidence, observed_at.astimezone())
    print_daily_detection_summary(detections_dir, species_names)
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
    species_names = SpeciesNames()
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
                min_confidence=min(args.min_confidence, SEGMENT_MIN_CONFIDENCE),
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
                species_names.ensure(scientific_name, english_name)

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
                    species_names,
                    args.detections_dir,
                    latitude=args.lat,
                    longitude=args.lon,
                    place_name=args.place_name,
                    observed_at=recording_start,
                    segments=detection_segments(detections, SEGMENT_MIN_CONFIDENCE),
                )
                for path in exported:
                    print(f"  exported {path.name}")
        finally:
            if analysis_path != file_path and analysis_path.exists():
                analysis_path.unlink()

    if args.export_clips:
        # like the live service: the clips go to Supabase now, or stay until the next sync
        report = push_outbox(args.detections_dir, species_names)
        print(f"Su Supabase: {report.sent} clip, in attesa: {report.pending - report.sent - report.discarded}")
    return 0


def detect_live_slice(
    raw_samples,
    sample_rate: int,
    detector: BirdNetDetector,
    args: argparse.Namespace,
    playback_samples=None,
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

    def detect(samples, min_confidence: float | None = None):
        return detector.detect_samples(
            samples,
            sample_rate,
            latitude=args.lat,
            longitude=args.lon,
            when=datetime.now(),
            # lower than the clip threshold: weaker calls of a species already heard are
            # still highlighted on its spectrogram; clips start only above args.min_confidence
            min_confidence=(
                min(args.min_confidence, SEGMENT_MIN_CONFIDENCE)
                if min_confidence is None
                else min_confidence
            ),
            overlap=LIVE_OVERLAP_SECONDS,
        )

    detections = detect(working_samples)
    if not any(float(d.get("confidence", 0)) >= args.min_confidence for d in detections) and not args.disable_denoise:
        processing_mode = "raw-fallback"
        working_samples = raw_samples
        detections = detect(working_samples)

    suppressed_species: set[str] = set()
    # BirdNET on the PC audio only when the mic has a bird above the threshold: only then does it
    # matter whether it came from the PC (a call would otherwise double the work all along)
    heard_a_bird = any(float(d.get("confidence", 0)) >= args.min_confidence for d in detections)
    if heard_a_bird and playback_samples is not None and rms_level(playback_samples) > ACTIVE_RMS:
        playback_min_confidence = getattr(
            args,
            "playback_suppression_min_confidence",
            DEFAULT_PLAYBACK_SUPPRESSION_MIN_CONFIDENCE,
        )
        playback_detections = detect(playback_samples, min_confidence=playback_min_confidence)
        suppressed_species = {
            detection.get("scientific_name", "")
            for detection in playback_detections
            if float(detection.get("confidence", 0.0)) >= playback_min_confidence
        }
        if suppressed_species:
            detections = [
                detection
                for detection in detections
                if detection.get("scientific_name", "") not in suppressed_species
            ]

    if getattr(args, "verbose", False):
        print(
            f"{datetime.now():%H:%M:%S} - "
            f"mode={processing_mode} "
            f"raw_rms={rms_level(raw_samples):.4f} "
            f"used_rms={rms_level(working_samples):.4f} "
            f"pc_suppressed={','.join(sorted(suppressed_species)) or '-'} "
            f"detections={sum(1 for d in detections if float(d.get('confidence', 0)) >= args.min_confidence)}"
        )

    duration_seconds = len(working_samples) / float(sample_rate)
    grouped = aggregate_detections_by_species(
        detections,
        duration_seconds=duration_seconds,
        min_confidence=args.min_confidence,
    )
    return grouped, working_samples, detection_segments(detections, SEGMENT_MIN_CONFIDENCE)


def print_live_header(args: argparse.Namespace, device_label: str, sample_rate: int) -> None:
    print(f"{datetime.now():%H:%M:%S} - Working directory: {Path.cwd()}")
    print(f"{datetime.now():%H:%M:%S} - Transit folder (clips wait here until they are on Supabase): {args.detections_dir}")
    print(f"{datetime.now():%H:%M:%S} - Device: {device_label}")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")
    playback_mode = "on" if getattr(args, "enable_playback_subtraction", DEFAULT_ENABLE_PLAYBACK_SUBTRACTION) else "off"
    print(f"{datetime.now():%H:%M:%S} - Sottrazione audio PC: {playback_mode}")


def push_outbox(detections_dir: Path, species_names: SpeciesNames, limit: int | None = None):
    """Send what waits in the transit folder to Supabase (no-op without cloud.env)."""
    from .cloud import DEFAULT_CLOUD_CONFIG, CloudConfig, Outbox, PushReport

    config = CloudConfig.load(DEFAULT_CLOUD_CONFIG)
    if not config.configured:
        return PushReport()
    return Outbox(Path(detections_dir), species_names, config).push(limit=limit)


def start_table_refresher(detections_dir: Path, species_names: SpeciesNames):
    """Reprint today's table every minute (reviews made on the site, the uploader's progress)
    and shortly after each new clip. Only a light Supabase read: sending the clips is the
    uploader's job, in its own process (uploader.py), so it never slows the listening down."""
    wake = threading.Event()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            if wake.wait(TABLE_REFRESH_SECONDS):
                wake.clear()
                stop.wait(10)  # give the uploader a moment: the new clip shows as sent
            if stop.is_set():
                break
            try:
                print_daily_detection_summary(detections_dir, species_names)
            except Exception as exc:
                print(f"{datetime.now():%H:%M:%S} - Tabella non aggiornata: {exc!r}")

    threading.Thread(target=work, name="table", daemon=True).start()
    return wake, stop


def compensate_system_volume(frames, device_label: str, args: argparse.Namespace):
    """Recordings at the microphone's own level, whatever the system mic volume (inputgain.py).
    Only for devices that go through the sound server, which is where that volume applies."""
    if device_label.lower() not in SOUND_SERVER_DEVICES or not SourceVolumeCompensation.available():
        yield from frames
        return
    compensation = SourceVolumeCompensation(log=lambda message: print(f"{datetime.now():%H:%M:%S} - {message}"))
    LIVE_METRICS.input_gain = compensation
    try:
        for frame in frames:
            yield compensation.apply(frame)
    finally:
        compensation.stop()


def subtract_playback_from_frames(frames, sample_rate: int, args: argparse.Namespace):
    """Yield live frames after subtracting the default output monitor, when available."""
    if not getattr(args, "enable_playback_subtraction", DEFAULT_ENABLE_PLAYBACK_SUBTRACTION):
        yield from frames
        return

    if not PlaybackMonitor.available():
        if getattr(args, "verbose", False):
            print(f"{datetime.now():%H:%M:%S} - pw-record/parec non trovati: niente sottrazione audio PC")
        yield from frames
        return

    log = lambda message: print(f"{datetime.now():%H:%M:%S} - {message}")
    subtractor = PlaybackSubtractor(sample_rate, log=log if getattr(args, "verbose", False) else None)
    LIVE_METRICS.subtractor = subtractor
    if getattr(args, "verbose", False):
        print(f"{datetime.now():%H:%M:%S} - Sottrazione audio PC attiva")
    try:
        for frame in frames:
            # CPU time, not wall time: while the PC plays, waiting for its audio is not a cost
            started = time.thread_time()
            result = subtractor.process_with_reference(frame, time.monotonic())
            LIVE_METRICS.record_echo(time.thread_time() - started, len(frame) / float(sample_rate))
            yield result
    finally:
        subtractor.close()


def run_live_detection(
    args: argparse.Namespace,
    frames,
    sample_rate: int,
    species_names: SpeciesNames,
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
            species_names=species_names,
            detections_dir=args.detections_dir,
            observed_at=heard_at,
        )

    tracker = LiveDetectionTracker(
        sample_rate=sample_rate,
        detections_dir=args.detections_dir,
        species_names=species_names,
        record_observation=record_observation,
        export_kwargs={"latitude": args.lat, "longitude": args.lon, "place_name": args.place_name},
        clip_span=getattr(args, "clip_span", DEFAULT_LIVE_CLIP_SPAN),
        max_clip_seconds=LIVE_MAX_CLIP_SECONDS,
    )
    wake_table, stop_table = start_table_refresher(args.detections_dir, species_names)

    def on_exported(paths: list[Path]) -> None:
        LIVE_TODAY.clips_written(paths)
        if args.verbose:
            for path in paths:
                print(f"{datetime.now():%H:%M:%S} - Clip pronta: {path.name}")
        wake_table.set()  # the uploader service sends it; the table shows it shortly

    def on_error(exc: Exception) -> None:
        print(f"{datetime.now():%H:%M:%S} - Detection error: {exc!r}")

    def detect_window(window, playback_window=None):
        result = detect_live_slice(window, sample_rate, detector, args, playback_window)
        heartbeat.beat(rms_level(window), device=device_label, place_name=args.place_name,
                       **LIVE_METRICS.snapshot())
        return result

    def on_location_change() -> None:
        tracker.export_kwargs.update(latitude=args.lat, longitude=args.lon, place_name=args.place_name)

    stop_refresh = start_location_refresh(args, on_location_change, DEFAULT_LOCATION_REFRESH_SECONDS)
    print_daily_detection_summary(args.detections_dir, species_names)
    frames = compensate_system_volume(frames, device_label, args)
    frames = subtract_playback_from_frames(frames, sample_rate, args)
    try:
        run_sliding_window_loop(
            frames,
            sample_rate=sample_rate,
            slice_frames=args.slice_interval,
            detect_window=detect_window,
            tracker=tracker,
            on_exported=on_exported,
            on_error=on_error,
            on_timing=LIVE_METRICS.record_window,
        )
    finally:
        stop_refresh.set()
        stop_table.set()
        # the clips flushed on shutdown wait in the transit folder: the uploader sends them


def load_species_names() -> SpeciesNames:
    """Names from Supabase (with the local cache as fallback when offline)."""
    names = SpeciesNames()
    client = _remote_client()
    if client is not None:
        try:
            names.refresh_from(client.get("species", {"select": "scientific_name,english_name,italian_name,german_name"}))
        except Exception:
            pass
    return names


def run_live_pvrecorder(args: argparse.Namespace) -> int:
    import numpy as np
    from pvrecorder import PvRecorder

    resolve_runtime_location(args)
    species_names = load_species_names()
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
        run_live_detection(args, frames(), sample_rate, species_names, detector, selected_device_name)
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
    species_names = load_species_names()
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
                species_names,
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


def run_sync(args: argparse.Namespace) -> int:
    from .cloud import DEFAULT_CLOUD_CONFIG, CloudConfig, Outbox

    config = CloudConfig.load(args.config or DEFAULT_CLOUD_CONFIG)
    if not config.configured:
        print("cloud.env senza SUPABASE_URL e SUPABASE_KEY: niente da fare.")
        return 1
    def make_outbox():
        names = SpeciesNames() if config.media_storage == "local" else load_species_names()
        return Outbox(Path(args.detections_dir), names, config)

    if getattr(args, "watch", False):
        from .uploader import run_uploader

        print(f"{datetime.now():%H:%M:%S} - Invio a Supabase: guardo {args.detections_dir}")
        run_uploader(Path(args.detections_dir), make_outbox)
        return 0
    report = make_outbox().push(limit=args.limit)
    if report.busy:
        print("Un altro invio e' in corso (il servizio di invio): riprovo dopo.")
        return 0
    waiting = report.pending - report.sent - report.discarded
    print(
        f"{config.supabase_url} | inviate {report.sent}, eliminate dal Cestino {report.discarded}, "
        f"in attesa {waiting}" + (f", convertite dal vecchio formato {report.converted}" if report.converted else "")
    )
    for error in report.errors[:10]:
        print(f"  errore: {error}")
    return 1 if report.errors else 0


def run_backfill_segments(args: argparse.Namespace) -> int:
    """analysis.py on the clips already on Supabase: their MP3 analysed again, confidence, bands and
    candidates replaced (the live's own score stays in live_confidence). Without --all only the
    clips never analysed; --retry-empty also those where the species was not found."""
    import requests
    import soundfile

    from .analysis import analyse_samples, detector
    from .cloud import CloudConfig, SupabaseRest, SupabaseStorage

    config = CloudConfig.load()
    rest, storage = SupabaseRest(config), SupabaseStorage(config)
    clips = rest.get(
        "clips",
        {
            "select": "id,media_path,recorded_at,latitude,longitude,observations(scientific_name)",
            "media_deleted_at": "is.null",  # files already removed (on iNaturalist): nothing to analyse
            **({} if args.all else {"or": "(segments.is.null,segments.eq.[])"} if args.retry_empty
               else {"segments": "is.null"}),
            "order": "recorded_at",
            "limit": str(args.limit or 100000),
        },
    )
    print(f"Clip da analizzare: {len(clips)}")
    model = detector()
    done = empty = 0
    for index, clip in enumerate(clips, start=1):
        try:
            response = requests.get(storage.public_url(f"{clip['media_path']}.mp3"), timeout=60)
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".mp3") as handle:
                handle.write(response.content)
                handle.flush()
                samples, sample_rate = soundfile.read(handle.name, dtype="float32")
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            analysis = analyse_samples(
                samples, sample_rate, clip["observations"]["scientific_name"],
                latitude=clip["latitude"] if clip["latitude"] is not None else DEFAULT_LATITUDE,
                longitude=clip["longitude"] if clip["longitude"] is not None else DEFAULT_LONGITUDE,
                when=datetime.fromisoformat(clip["recorded_at"]), model=model,
            )
            rest.update("clips", {"id": f"eq.{clip['id']}"}, analysis)
            done += 1
            empty += 0 if analysis["segments"] else 1
        except Exception as exc:
            print(f"  {clip['id']}: {exc}")
        if index % 25 == 0:
            print(f"  {index}/{len(clips)}")
    print(f"Fatto: {done} clip analizzate, {empty} dove BirdNET non ritrova la specie (nemmeno sopra 0.1).")
    return 0


def run_refresh_species(args: argparse.Namespace) -> int:
    """eBird code and iNaturalist taxon of each species still without them (links on the site)."""
    from .cloud import CloudConfig, SupabaseRest
    from .species_links import species_codes

    rest = SupabaseRest(CloudConfig.load())
    rows = rest.get("species", {"select": "scientific_name,ebird_code,inat_taxon_id",
                                "or": "(ebird_code.is.null,inat_taxon_id.is.null)"})
    print(f"Specie senza codici: {len(rows)}")
    missing = []
    for row in rows:
        codes = species_codes(row["scientific_name"])
        if codes:
            rest.update("species", {"scientific_name": f"eq.{row['scientific_name']}"}, codes)
        if len(codes) < 2:
            missing.append(row["scientific_name"])
    print(f"Fatto. Senza codice eBird o iNaturalist: {', '.join(missing) or 'nessuna'}")
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
    if args.command == "sync":
        return run_sync(args)
    if args.command == "refresh-species":
        return run_refresh_species(args)
    if args.command == "backfill-segments":
        return run_backfill_segments(args)

    parser.error("Unknown command.")
    return 2
