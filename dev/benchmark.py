#!/usr/bin/env python3
"""How long each step of the pipeline takes and how much memory it needs (`make test`, which
first runs the test suite and stops if it fails; `make test-fast` is the suite alone).

Real BirdNET on synthetic audio (timing does not depend on what is in it), the same calls the
live service, the sync and the backfill make. Each time is compared with the reference saved in
dev/benchmark_baseline.json (this laptop): more than 30% slower is flagged. Every run is also
appended to ~/.cache/bird-audio-suite/benchmark_history.jsonl, to follow the trend.

    python dev/benchmark.py                  # measure and compare
    python dev/benchmark.py --save-baseline  # the current numbers become the reference
"""

from __future__ import annotations

import argparse
import io
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import soundfile  # noqa: E402

from bird_audio_suite.metrics import memory_mb  # noqa: E402

BASELINE = ROOT / "dev" / "benchmark_baseline.json"
HISTORY = Path.home() / ".cache" / "bird-audio-suite" / "benchmark_history.jsonl"
SLOWER_IS_A_PROBLEM = 1.3
SAMPLE_RATE = 48000


def synthetic_audio(seconds: float, seed: int = 0) -> np.ndarray:
    """Quiet noise with a few bird-like chirps."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    audio = 0.01 * rng.standard_normal(t.size)
    for start in np.arange(0.5, seconds - 0.5, 1.7):
        part = (t >= start) & (t < start + 0.25)
        sweep = 3000 + 4000 * (t[part] - start) / 0.25
        audio[part] += 0.2 * np.sin(2 * np.pi * sweep * (t[part] - start)) * np.hanning(part.sum())
    return audio.astype(np.float32)


def timed(function, repeat: int = 1) -> float:
    """Median seconds over `repeat` runs."""
    runs = []
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        runs.append(time.perf_counter() - started)
    return statistics.median(runs)


def quiet(function):
    def run():
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return function()
    return run


class TestsFailed(Exception):
    pass


def measure() -> list[dict]:
    steps = []

    def step(name: str, seconds: float, note: str = "") -> None:
        rss, _peak = memory_mb()
        steps.append({"step": name, "seconds": round(seconds, 4), "rss_mb": round(rss), "note": note})
        print(f"  {name:<48} {seconds:7.3f} s   RAM {rss:5.0f} MB   {note}", flush=True)

    # the test suite, in its own process (its peak memory from the children's usage)
    started = time.perf_counter()
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--durations=5",
                            "--durations-min=0.05", str(ROOT / "tests")], cwd=ROOT, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if tests.returncode != 0:
        print(tests.stdout[-6000:], tests.stderr[-2000:], sep="\n")
        raise TestsFailed
    lines = tests.stdout.strip().splitlines()
    passed = lines[-1].strip("= ") if lines else "?"
    slowest = [line for line in lines if line[:1].isdigit() and "s " in line and "::" in line]
    if slowest:
        print("  test piu' lenti:")
        for line in slowest:
            print(f"    {line}")
    children_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    steps.append({"step": "test (pytest, intera suite)", "seconds": round(elapsed, 3), "rss_mb": round(children_peak),
                  "note": passed})
    print(f"  {'test (pytest, intera suite)':<48} {elapsed:7.3f} s   RAM {children_peak:5.0f} MB   {passed}", flush=True)

    detector = None

    def load():
        nonlocal detector
        from bird_audio_suite.detector import BirdNetDetector  # the import loads LiteRT/birdnetlib

        detector = BirdNetDetector()

    step("BirdNET: import e caricamento modello", timed(quiet(load)))

    window = synthetic_audio(9.6, seed=1)
    when = datetime(2026, 6, 1, 6, 30)

    def birdnet_window():
        detector.detect_samples(window, SAMPLE_RATE, latitude=45.8, longitude=13.5, when=when, min_confidence=0.5,
                                overlap=2.0)  # as the live does

    quiet(birdnet_window)()  # warm-up
    seconds = timed(quiet(birdnet_window), repeat=5)
    step("live: BirdNET su una finestra da 9.6 s", seconds, f"{100 * seconds / 4.8:.0f}% dei 4.8 s disponibili")

    from bird_audio_suite.cli import detect_live_slice

    args = argparse.Namespace(disable_denoise=True, lat=45.8, lon=13.5, min_confidence=0.85,
                              playback_suppression_min_confidence=0.5, verbose=False)
    playback = synthetic_audio(9.6, seed=2)
    seconds = timed(quiet(lambda: detect_live_slice(window, SAMPLE_RATE, detector, args, playback)), repeat=3)
    step("live: finestra con il PC che suona", seconds, f"{100 * seconds / 4.8:.0f}% dei 4.8 s (BirdNET sul PC solo se il microfono sente un uccello)")

    from bird_audio_suite.echo import EchoCanceller

    mic, reference = synthetic_audio(60, seed=3), synthetic_audio(60, seed=4)

    def subtract():
        canceller = EchoCanceller()
        for start in range(0, mic.size, 1536):  # the live frame length
            canceller.process(mic[start:start + 1536], reference[start:start + 1536])

    seconds = timed(subtract)
    step("live: sottrazione, PC che suona, 60 s", seconds, f"{100 * seconds / 60:.1f}% del tempo reale")
    reference = np.zeros_like(reference)
    seconds = timed(subtract)
    step("live: sottrazione, PC in silenzio, 60 s", seconds, f"{100 * seconds / 60:.1f}% del tempo reale")

    from bird_audio_suite.cloud import encode_mp3, render_spectrogram

    with tempfile.TemporaryDirectory() as directory:
        wav = Path(directory) / "clip.wav"
        soundfile.write(wav, synthetic_audio(30, seed=5), SAMPLE_RATE)
        step("sync: MP3 di una clip da 30 s", timed(lambda: encode_mp3(wav, Path(directory) / "clip.mp3")))
        step("sync: spettrogramma di una clip da 30 s",
             timed(lambda: render_spectrogram(wav, Path(directory) / "clip.jpg")))

    clip = synthetic_audio(30, seed=6)
    seconds = timed(quiet(lambda: detector.detect_samples(clip, SAMPLE_RATE, latitude=45.8, longitude=13.5, when=when,
                                                          min_confidence=0.1, overlap=2.0)))
    step("backfill: BirdNET su 30 s sovrapposti", seconds)

    _, peak = memory_mb()
    steps.append({"step": "picco RAM del benchmark", "seconds": None, "rss_mb": round(peak), "note": ""})
    print(f"  {'picco RAM del benchmark':<48} {'':>9}   RAM {peak:5.0f} MB")

    # Perch, the uploader's double check (perch.py), after the peak above: it brings its own ~0.9 GB,
    # only while the uploader uses it. Measured only when its model is already in the cache.
    from bird_audio_suite import perch

    if (perch.CACHE_DIR / perch.MODEL_FILE).exists() and perch.model() is not None:
        everything = set(perch.model().labels)
        seconds = timed(lambda: perch.model().opinion(clip, SAMPLE_RATE, everything))
        step("sync: Perch su 30 s (doppio controllo)", seconds, f"{100 * seconds / 30:.1f}% del tempo reale")
        perch.release()
    return steps


def compare(steps: list[dict], baseline: dict) -> list[str]:
    print("\nRispetto al riferimento (dev/benchmark_baseline.json):")
    warnings = []
    for entry in steps:
        reference = baseline.get(entry["step"])
        if not reference:
            continue
        if entry["seconds"] is not None and reference.get("seconds"):
            ratio = entry["seconds"] / reference["seconds"]
            # tiny steps jitter a lot: flag only what is also slower in absolute terms
            slow = ratio > SLOWER_IS_A_PROBLEM and entry["seconds"] - reference["seconds"] > 0.05
            mark = "  << PIU' LENTO" if slow else ""
            print(f"  {entry['step']:<48} {entry['seconds']:7.3f} s  rif. {reference['seconds']:7.3f} s"
                  f"  {100 * (ratio - 1):+5.0f}%{mark}")
            if slow:
                warnings.append(f"{entry['step']}: {entry['seconds']:.2f} s invece di {reference['seconds']:.2f} s")
        if entry["rss_mb"] and reference.get("rss_mb") and entry["rss_mb"] > SLOWER_IS_A_PROBLEM * reference["rss_mb"]:
            warnings.append(f"{entry['step']}: {entry['rss_mb']} MB di RAM invece di {reference['rss_mb']} MB")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save-baseline", action="store_true", help="Save these numbers as the reference.")
    args = parser.parse_args()

    print("Benchmark Bird Audio Suite")
    try:
        steps = measure()
    except TestsFailed:
        print("\nTest falliti: benchmark interrotto.")
        return 1
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "steps": steps}) + "\n")

    if args.save_baseline:
        BASELINE.write_text(json.dumps({entry["step"]: {"seconds": entry["seconds"], "rss_mb": entry["rss_mb"]}
                                        for entry in steps}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nRiferimento salvato in {BASELINE.relative_to(ROOT)}.")
        return 0
    if not BASELINE.exists():
        print("\nNessun riferimento: python dev/benchmark.py --save-baseline")
        return 0
    warnings = compare(steps, json.loads(BASELINE.read_text(encoding="utf-8")))
    if warnings:
        print("\nATTENZIONE, piu' lento o piu' pesante del riferimento:")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("\nTutto in linea con il riferimento.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
