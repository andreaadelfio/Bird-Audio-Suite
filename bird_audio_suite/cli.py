from __future__ import annotations

import argparse
import queue
import shlex
import tempfile
from datetime import datetime
from pathlib import Path

from .audio import (
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
    DEFAULT_DETECTIONS_DIR,
    DEFAULT_DENOISE_HIGH_PASS_HZ,
    DEFAULT_DENOISE_REDUCTION_FACTOR,
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
from .inaturalist import DEFAULT_API_BASE_URL, DEFAULT_TOKEN_ENV_VAR, import_csv, resolve_jwt_token
from .species import SpeciesCatalog


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
        "--token-env",
        default=DEFAULT_TOKEN_ENV_VAR,
        help=f"Environment variable containing the iNaturalist JWT. Default: {DEFAULT_TOKEN_ENV_VAR}",
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


def ensure_folder(base_dir: Path) -> Path:
    folder = base_dir
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def print_batch_detections(file_path: Path, detections: list[dict], species_catalog: SpeciesCatalog) -> None:
    print(f"\nFile: {file_path}")
    if not detections:
        print("  No detections.")
        return

    for detection in detections:
        scientific_name = detection.get("scientific_name", "")
        english_name = detection.get("common_name", "")
        italian_name, german_name, english_name = species_catalog.display_names(scientific_name, english_name)
        start_time = detection.get("start_time", 0)
        end_time = detection.get("end_time", 0)
        confidence = round(float(detection.get("confidence", 0.0)), 3)
        print(
            f"  {start_time}s - {end_time}s -> "
            f"{italian_name} ({scientific_name}, {english_name}, {german_name}) [{confidence}]"
        )


def print_live_detections(grouped: dict[str, tuple[int, int, float, str]], species_catalog: SpeciesCatalog) -> None:
    if not grouped:
        print(f"\r{datetime.now():%H:%M:%S} - No detections.", end='')
        return

    for scientific_name, (_, _, confidence, english_name) in grouped.items():
        italian_name, german_name, english_name = species_catalog.display_names(scientific_name, english_name)
        print('\r'+
            f"{datetime.now():%H:%M:%S} - "
            f"{italian_name} ({scientific_name}, {english_name}, {german_name}) [{confidence}]"
        )


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
            detections = detector.detect(
                analysis_path,
                latitude=args.lat,
                longitude=args.lon,
                when=datetime.fromtimestamp(file_path.stat().st_ctime),
                min_confidence=args.min_confidence,
            )
            species_catalog.ensure_species(detections)
            print_batch_detections(file_path, detections, species_catalog)

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
                    destination_dir = args.detections_dir / datetime.fromtimestamp(
                        file_path.stat().st_ctime
                    ).strftime("%Y%m%d")
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
    raw_dir: Path,
    args: argparse.Namespace,
) -> None:
    # slice_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_rms = rms_level(raw_samples)
    processing_mode = "raw"
    raw_slice_path = raw_dir / f"tmp_raw.wav"
    write_wav_mono(raw_slice_path, raw_samples, sample_rate)

    with tempfile.NamedTemporaryFile(
        suffix=".wav",
        delete=False,
        dir=output_dir,
    ) as temp_handle:
        temp_path = Path(temp_handle.name)

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
            denoised_slice_path = raw_dir / f"tmp_denoised.wav"
            write_wav_mono(denoised_slice_path, working_samples, sample_rate)

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
        if temp_path.exists():
            temp_path.unlink()

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

    species_catalog.ensure_species(detections)
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
    print_live_detections(grouped, species_catalog)

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
        # print(f"{datetime.now():%H:%M:%S} - Exported {len(exported)} clip(s).")


def run_live_pvrecorder(args: argparse.Namespace) -> int:
    from pvrecorder import PvRecorder

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    detector = BirdNetDetector()
    output_dir = ensure_today_folder(args.detections_dir)
    raw_dir = ensure_folder(args.detections_dir)
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
    print(f"{datetime.now():%H:%M:%S} - Saving reusable live temp files to: {raw_dir}")
    print(f"{datetime.now():%H:%M:%S} - Device index: {args.device_index} ({selected_device_name})")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")

    audio_frames: list[int] = []
    slice_index = 0

    try:
        recorder.start()
        started = True
        while True:
            frame = recorder.read()
            audio_frames.extend(frame)
            slice_index += 1

            if slice_index % args.slice_interval != 0:
                continue

            samples = list(audio_frames)
            audio_frames = []
            process_live_slice(
                samples,
                sample_rate,
                slice_index // args.slice_interval,
                detector,
                species_catalog,
                output_dir,
                raw_dir,
                args,
            )

    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        return 0
    finally:
        if started:
            recorder.stop()
        recorder.delete()


def run_live_sounddevice(args: argparse.Namespace) -> int:
    import numpy as np
    import sounddevice as sd

    resolve_runtime_location(args)
    species_catalog = SpeciesCatalog(args.species_file)
    detector = BirdNetDetector()
    output_dir = ensure_today_folder(args.detections_dir)
    raw_dir = ensure_folder(args.detections_dir)
    devices = get_available_audio_devices_sounddevice()
    if devices:
        print("Device audio disponibili (sounddevice):")
        for device in devices:
            print(
                f"  {device['index']}. {device['name']} "
                f"(channels={device['channels']}, default_sr={device['samplerate']})"
            )
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
    print(f"{datetime.now():%H:%M:%S} - Saving reusable live temp files to: {raw_dir}")
    print(f"{datetime.now():%H:%M:%S} - Device index: {args.device_index} ({selected_device_name})")
    print(f"{datetime.now():%H:%M:%S} - Recorder sample rate: {sample_rate}")
    print(f"{datetime.now():%H:%M:%S} - Slice duration: {(args.frame_length * args.slice_interval) / float(sample_rate):.2f}s")
    print(f"{datetime.now():%H:%M:%S} - Denoise live: {'off' if args.disable_denoise else 'on'}")

    audio_frames: list[float] = []
    slice_index = 0
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    status_queue: queue.Queue[str] = queue.Queue()

    def audio_callback(indata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            status_queue.put(str(status))
        audio_queue.put(indata[:, 0].copy())

    try:
        with sd.InputStream(
            device=device,
            channels=1,
            samplerate=sample_rate,
            dtype="float32",
            blocksize=args.frame_length,
            callback=audio_callback,
        ):
            while True:
                frame = audio_queue.get()
                while not status_queue.empty():
                    message = status_queue.get_nowait()
                    if args.verbose:
                        print(f"{datetime.now():%H:%M:%S} - audio status: {message}")
                audio_frames.extend(frame.tolist())
                slice_index += 1

                if slice_index % args.slice_interval != 0:
                    continue

                samples = np.asarray(audio_frames, dtype=np.float32)
                audio_frames = []
                process_live_slice(
                    samples,
                    sample_rate,
                    slice_index // args.slice_interval,
                    detector,
                    species_catalog,
                    output_dir,
                    raw_dir,
                    args,
                )
    except KeyboardInterrupt:
        print("\nStopping live recognition.")
        return 0
    except Exception as exc:
        print(f"\nLive audio error ({args.backend}): {exc}")
        return 1
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
        jwt_token = resolve_jwt_token(args.token, args.token_env)

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
