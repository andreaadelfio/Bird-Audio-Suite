from __future__ import annotations

import argparse
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd


OUTPUT_DIR = Path(__file__).resolve().parent / "mic_probe_sounddevice"


def list_input_devices() -> list[dict]:
    devices = sd.query_devices()
    input_devices: list[dict] = []
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) > 0:
            input_devices.append(
                {
                    "index": index,
                    "name": device.get("name", f"Device {index}"),
                    "channels": int(device.get("max_input_channels", 0)),
                    "samplerate": float(device.get("default_samplerate", 0)),
                }
            )
    return input_devices


def get_device_info(device_index: int) -> dict | None:
    for device in list_input_devices():
        if device["index"] == device_index:
            return device
    return None


def print_input_devices() -> list[dict]:
    devices = list_input_devices()
    if not devices:
        print("No input devices found via sounddevice.")
        return []

    print("Available input devices via sounddevice:")
    for device in devices:
        print(
            f"  {device['index']}: {device['name']} "
            f"(channels={device['channels']}, default_sr={device['samplerate']:.0f})"
        )
    return devices


def rms_level(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    return float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))


def peak_level(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    return float(np.max(np.abs(mono.astype(np.float32))))


def render_meter(value: float, width: int = 32) -> str:
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "#" * filled + "-" * (width - filled)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    mono = pcm.mean(axis=1).astype(np.int16) if pcm.ndim > 1 else pcm
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(mono.tobytes())


def resolve_sample_rate(device_index: int, requested_sample_rate: int) -> int:
    if requested_sample_rate > 0:
        return requested_sample_rate
    device = get_device_info(device_index)
    if device and device["samplerate"] > 0:
        return int(device["samplerate"])
    return 16000


def monitor_device(device_index: int, seconds: int, sample_rate: int) -> int:
    devices = print_input_devices()
    if not any(d["index"] == device_index for d in devices):
        print(f"Invalid device index: {device_index}")
        return 1
    sample_rate = resolve_sample_rate(device_index, sample_rate)

    print(f"\nMonitoring device {device_index} for {seconds}s at {sample_rate} Hz...")
    print("Play sound near the microphone. Press Ctrl+C to stop.\n")

    stream = sd.InputStream(
        device=device_index,
        channels=1,
        samplerate=sample_rate,
        dtype="float32",
        blocksize=2048,
    )

    deadline = time.time() + seconds if seconds > 0 else None
    try:
        with stream:
            while True:
                data, overflowed = stream.read(2048)
                rms = rms_level(data)
                peak = peak_level(data)
                meter = render_meter(max(rms * 8.0, peak))
                extra = " overflow" if overflowed else ""
                sys.stdout.write(
                    f"\rRMS={rms:0.4f} PEAK={peak:0.4f} [{meter}]{extra:<10}"
                )
                sys.stdout.flush()
                if deadline is not None and time.time() >= deadline:
                    break
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\nError opening device {device_index}: {exc}")
        return 1
    print()
    return 0


def record_sample(device_index: int, seconds: float, sample_rate: int, output_dir: Path) -> tuple[Path, float, float]:
    sample_rate = resolve_sample_rate(device_index, sample_rate)
    print(f"Recording {seconds:.1f}s from device {device_index} at {sample_rate} Hz...")
    samples = sd.rec(
        int(seconds * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device_index,
    )
    sd.wait()
    avg_rms = rms_level(samples)
    max_peak = peak_level(samples)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = next(d["name"] for d in list_input_devices() if d["index"] == device_index)
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
    output_path = output_dir / f"device_{device_index:02d}_{safe_name}_{timestamp}.wav"
    write_wav(output_path, samples, sample_rate)
    return output_path, avg_rms, max_peak


def scan_all_devices(seconds: float, sample_rate: int, output_dir: Path) -> int:
    devices = print_input_devices()
    if not devices:
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput directory: {output_dir}\n")
    for device in devices:
        try:
            path, avg_rms, max_peak = record_sample(device["index"], seconds, sample_rate, output_dir)
            print(f"  saved: {path.name}")
            print(f"  avg_rms={avg_rms:.4f} max_peak={max_peak:.4f}\n")
        except Exception as exc:
            print(f"  error: {exc}\n")
    return 0


def interactive_main(args: argparse.Namespace) -> int:
    devices = print_input_devices()
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
        raw_seconds = input("Monitoring duration in seconds [15]: ").strip() or "15"
        try:
            seconds = int(raw_seconds)
        except ValueError:
            print("Invalid duration.")
            return 1
        return monitor_device(device_index, seconds, args.sample_rate)

    raw_seconds = input("Seconds to record for each device [4]: ").strip() or "4"
    try:
        seconds = float(raw_seconds)
    except ValueError:
        print("Invalid duration.")
        return 1
    return scan_all_devices(seconds, args.sample_rate, args.output_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Alternative microphone probe using sounddevice instead of PvRecorder.",
    )
    parser.add_argument("--list", action="store_true", help="List input devices and exit.")
    parser.add_argument("--monitor", type=int, help="Monitor one device index.")
    parser.add_argument("--scan-all", action="store_true", help="Record a short sample from every device.")
    parser.add_argument("--seconds", type=float, default=4.0, help="Duration for recordings.")
    parser.add_argument("--sample-rate", type=int, default=0, help="Recording sample rate. Use 0 for device default.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Where to save recordings.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        print_input_devices()
        return 0
    if args.monitor is not None:
        return monitor_device(args.monitor, int(args.seconds), args.sample_rate)
    if args.scan_all:
        return scan_all_devices(args.seconds, args.sample_rate, args.output_dir)
    return interactive_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
