from __future__ import annotations

import argparse
import math
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from pvrecorder import PvRecorder


OUTPUT_DIR = Path(__file__).resolve().parent / "mic_probe"
DEFAULT_FRAME_LENGTH = 512


def get_devices() -> list[str]:
    devices = PvRecorder.get_available_devices()
    return list(devices) if devices else []


def rms_level(samples: list[int] | np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float32)
    if array.size == 0:
        return 0.0
    # PvRecorder returns 16-bit PCM-like integer samples
    normalized = array / 32768.0
    return float(np.sqrt(np.mean(np.square(normalized))))


def peak_level(samples: list[int] | np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float32)
    if array.size == 0:
        return 0.0
    normalized = np.abs(array / 32768.0)
    return float(np.max(normalized))


def render_meter(value: float, width: int = 32) -> str:
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "#" * filled + "-" * (width - filled)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(clipped.tobytes())


def list_devices() -> list[str]:
    devices = get_devices()
    if not devices:
        print("No audio input devices found.")
        return []

    print("Available audio input devices:")
    for index, name in enumerate(devices):
        print(f"  {index}: {name}")
    return devices


def monitor_device(device_index: int, seconds: int, frame_length: int) -> int:
    devices = get_devices()
    if not devices:
        print("No audio input devices found.")
        return 1
    if device_index < 0 or device_index >= len(devices):
        print(f"Invalid device index: {device_index}")
        return 1

    recorder = PvRecorder(device_index=device_index, frame_length=frame_length)
    sample_rate = getattr(recorder, "sample_rate", 16000)
    print(f"Monitoring device {device_index}: {devices[device_index]}")
    print(f"Sample rate: {sample_rate} Hz")
    print("Play sound near the microphone. Press Ctrl+C to stop.\n")

    started = False
    deadline = time.time() + seconds if seconds > 0 else None
    try:
        recorder.start()
        started = True
        while True:
            frame = recorder.read()
            rms = rms_level(frame)
            peak = peak_level(frame)
            meter = render_meter(max(rms * 8.0, peak))
            sys.stdout.write(
                f"\rRMS={rms:0.4f} PEAK={peak:0.4f} [{meter}]"
            )
            sys.stdout.flush()
            if deadline is not None and time.time() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if started:
            recorder.stop()
        recorder.delete()
    print()
    return 0


def record_device_sample(device_index: int, duration: float, frame_length: int, output_dir: Path) -> tuple[Path, float, float]:
    devices = get_devices()
    recorder = PvRecorder(device_index=device_index, frame_length=frame_length)
    sample_rate = getattr(recorder, "sample_rate", 16000)
    frame_count = max(1, int(math.ceil((duration * sample_rate) / frame_length)))
    all_samples: list[int] = []
    rms_values: list[float] = []
    peak_values: list[float] = []
    started = False

    try:
        recorder.start()
        started = True
        for _ in range(frame_count):
            frame = recorder.read()
            all_samples.extend(frame)
            rms_values.append(rms_level(frame))
            peak_values.append(peak_level(frame))
    finally:
        if started:
            recorder.stop()
        recorder.delete()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in devices[device_index]).strip()
    output_path = output_dir / f"device_{device_index:02d}_{safe_name}_{timestamp}.wav"
    write_wav(output_path, np.asarray(all_samples, dtype=np.int16), sample_rate)
    avg_rms = float(np.mean(rms_values)) if rms_values else 0.0
    max_peak = float(np.max(peak_values)) if peak_values else 0.0
    return output_path, avg_rms, max_peak


def scan_all_devices(duration: float, frame_length: int, output_dir: Path) -> int:
    devices = get_devices()
    if not devices:
        print("No audio input devices found.")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Recording {duration:.1f}s sample from each device...")
    print(f"Output directory: {output_dir}\n")

    results: list[tuple[int, str, Path, float, float]] = []
    for index, name in enumerate(devices):
        print(f"[{index}] Recording from: {name}")
        path, avg_rms, max_peak = record_device_sample(index, duration, frame_length, output_dir)
        results.append((index, name, path, avg_rms, max_peak))
        print(f"    saved: {path.name}")
        print(f"    avg_rms={avg_rms:.4f} max_peak={max_peak:.4f}\n")

    print("Summary:")
    for index, name, path, avg_rms, max_peak in results:
        print(f"  {index}: avg_rms={avg_rms:.4f} max_peak={max_peak:.4f} -> {name}")
        print(f"     {path}")

    print("\nListen to the saved WAV files and pick the device that actually contains your test audio.")
    return 0


def interactive_main(args: argparse.Namespace) -> int:
    devices = list_devices()
    if not devices:
        return 1

    print("\nChoose mode:")
    print("  1. Monitor one device live")
    print("  2. Record a short sample from every device")
    choice = input("Mode [2]: ").strip() or "2"

    if choice == "1":
        raw_index = input("Device index [0]: ").strip() or "0"
        try:
            device_index = int(raw_index)
        except ValueError:
            print("Invalid device index.")
            return 1
        raw_seconds = input("Monitoring duration in seconds [15, 0=until Ctrl+C]: ").strip() or "15"
        try:
            seconds = int(raw_seconds)
        except ValueError:
            print("Invalid duration.")
            return 1
        return monitor_device(device_index, seconds, args.frame_length)

    raw_duration = input("Seconds to record for each device [4]: ").strip() or "4"
    try:
        duration = float(raw_duration)
    except ValueError:
        print("Invalid duration.")
        return 1
    return scan_all_devices(duration, args.frame_length, args.output_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone microphone device probe for Bird Audio Suite.",
    )
    parser.add_argument("--list", action="store_true", help="List devices and exit.")
    parser.add_argument("--monitor", type=int, help="Monitor one device index live.")
    parser.add_argument("--scan-all", action="store_true", help="Record a short WAV from every device.")
    parser.add_argument("--seconds", type=float, default=4.0, help="Duration for scan-all or monitor.")
    parser.add_argument("--frame-length", type=int, default=DEFAULT_FRAME_LENGTH, help="PvRecorder frame length.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Where to save probe WAV files.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        list_devices()
        return 0
    if args.monitor is not None:
        seconds = int(args.seconds) if args.seconds > 0 else 0
        return monitor_device(args.monitor, seconds, args.frame_length)
    if args.scan_all:
        return scan_all_devices(args.seconds, args.frame_length, args.output_dir)
    return interactive_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
