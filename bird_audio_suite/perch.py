"""Perch 2.0 (Google DeepMind) as a double check on the saved clips.

BirdNET finds the birds while listening and measures them on the saved clip (analysis.py). Perch,
a larger model trained on ~14,800 species (birds, and also frogs, grasshoppers, mammals) from
Xeno-canto and iNaturalist, says which species it hears in the same clip; the site shows it beside
BirdNET's candidates, nothing more. In the October 2026 test on 288 clips it agreed with BirdNET on
77% (92% where BirdNET was sure) and got right all 7 clips with a known answer, among them two
"pied flycatchers" of BirdNET's that were great spotted woodpeckers.

It has no idea where it is listening (left alone it suggests American owls): its species are
kept to those observed on iNaturalist within 50 km of the clip (research grade; birds, mammals,
amphibians, grasshoppers and crickets, cicadas), asked once per place and refreshed every three
months. Its names are iNaturalist's (Coloeus monedula), the same as the list's.

Its numbers are not BirdNET's. BirdNET scores each species on its own, 0 to 1; Perch shares a
total of 1 among its ~14,800 classes in each 5 s block, so look-alikes from elsewhere (the
American magpie beside ours) and a second bird take their part. What is kept is each species'
share among the species of this place, in the block where it is highest: a percentage on the
site. Blocks where the species of this place get less than 30% of Perch's total are left out: in
70 clips, there its first choice was the clip's species only 13-16% of the times (noise, or
something from elsewhere), from 50% up 85-99%.

The model (ONNX, 413 MB, Apache 2.0, converted from Google's release on Kaggle) is downloaded the
first time into ~/.cache/bird-audio-suite/perch, from a fixed revision, and checked against its
SHA-256. ONNX Runtime runs it one 5 s block at a time, every 2.5 s (~0.7 s per 10 s of audio,
~0.9 GB of RAM); the uploader releases it after each round, so the always-on service does not
keep it in memory. Without onnxruntime, offline at the first download or without the species
list, a clip simply gets no opinion (clips.perch stays null) and `backfill-perch` adds it later.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import time
from pathlib import Path

import numpy as np
import requests

CACHE_DIR = Path.home() / ".cache" / "bird-audio-suite" / "perch"
REVISION = "ccf5c5115178359914097dc48dc80e60d19946ed"  # tphakala/Perch-v2 on Hugging Face, October 2026
REPOSITORY = f"https://huggingface.co/tphakala/Perch-v2/resolve/{REVISION}"
FILES = {
    "perch-v2-fp32-no-dft.onnx": "4dcf71c18a147198545944bb5149697e89e3ad2e16637fa8f0edf6d13035a017",
    "labels.txt": "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057",
}
MODEL_FILE, LABELS_FILE = FILES

RATE = 32_000
BLOCK = 5 * RATE
HOP = BLOCK // 2
THREADS = 2  # the uploader runs beside the live: it must not take the CPU away from it
TOP = 3
MIN_LOCAL_SHARE = 0.3  # a block counts when the species of this place get at least this much
MIN_SHARE = 0.1  # of that, a species below this is not shown

INAT_SPECIES_COUNTS = "https://api.inaturalist.org/v1/observations/species_counts"
NEARBY_KM = 50
# iNaturalist taxa that make sounds Perch knows: birds, amphibians, mammals, Orthoptera, Cicadidae
NEARBY_TAXA = "3,20978,40151,47651,50186"
NEARBY_MAX_AGE_SECONDS = 90 * 24 * 3600


# ---------------------------------------------------------------- files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_files(cache_dir: Path = CACHE_DIR, session=requests) -> bool:
    """Model and labels in the cache, downloaded if missing; False if they cannot be had."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        path = cache_dir / name
        if path.exists():
            continue
        partial = path.with_name(path.name + ".part")
        try:
            with session.get(f"{REPOSITORY}/{name}", stream=True, timeout=60) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(1 << 20):
                        handle.write(chunk)
            if _sha256(partial) != expected:
                partial.unlink(missing_ok=True)
                return False  # not the file we checked: never run it
            partial.replace(path)
        except (OSError, requests.RequestException):
            partial.unlink(missing_ok=True)
            return False
    return True


# ---------------------------------------------------------------- the model


class Perch:
    def __init__(self, cache_dir: Path = CACHE_DIR, session=None) -> None:
        """`session`: anything with ONNX Runtime's run(); the real one is built from the cache."""
        self.labels = (cache_dir / LABELS_FILE).read_text(encoding="utf-8").splitlines()[1:]  # 1st: the set's name
        if session is None:
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = THREADS
            session = onnxruntime.InferenceSession(str(cache_dir / MODEL_FILE), options,
                                                   providers=["CPUExecutionProvider"])
        self.session = session

    def blocks(self, samples: np.ndarray, rate: int):
        """Perch's probabilities (summing to 1) for each 5 s block, every 2.5 s, the end of the clip too."""
        audio = np.asarray(samples, np.float32).reshape(-1)
        if rate != RATE:
            from scipy.signal import resample_poly

            common = math.gcd(int(rate), RATE)
            audio = resample_poly(audio, RATE // common, int(rate) // common).astype(np.float32)
        starts = list(range(0, max(audio.size - BLOCK, 0) + 1, HOP))
        if audio.size > BLOCK and (audio.size - BLOCK) % HOP:
            starts.append(audio.size - BLOCK)
        for start in starts:  # one block at a time: a batch of a long clip took 5 GB
            block = np.zeros((1, BLOCK), np.float32)
            piece = audio[start:start + BLOCK]
            block[0, :piece.size] = piece
            logits = np.asarray(self.session.run(["label"], {"inputs": block})[0][0], np.float64)
            probabilities = np.exp(logits - logits.max())
            yield probabilities / probabilities.sum()

    def opinion(self, samples: np.ndarray, rate: int, allowed: set[str]) -> list[list]:
        """The (up to three) species of `allowed` Perch hears, [[scientific, share], ...]: its share
        among the species of this place, in the block where it is highest."""
        local = np.fromiter((name in allowed for name in self.labels), bool, len(self.labels))
        best = np.zeros(len(self.labels))
        for probabilities in self.blocks(samples, rate):
            total = float(probabilities[local].sum())
            if total < MIN_LOCAL_SHARE:
                continue  # mostly something not from here, or nothing at all
            np.maximum(best, np.where(local, probabilities, 0.0) / total, out=best)
        ranked = [i for i in np.argsort(best)[::-1][:TOP] if best[i] >= MIN_SHARE]
        return [[self.labels[i], round(float(best[i]), 3)] for i in ranked]


_model: Perch | None = None


def model() -> Perch | None:
    """Perch, loaded once until release(); None when it cannot run here."""
    global _model
    if _model is None:
        if importlib.util.find_spec("onnxruntime") is None or not ensure_files():
            return None
        _model = Perch()
    return _model


def release() -> None:
    """Let the ~0.9 GB go: the uploader calls this after each round."""
    global _model
    _model = None


# ---------------------------------------------------------------- where it listens


def nearby_species(latitude: float, longitude: float, cache_dir: Path = CACHE_DIR, session=requests,
                   now: float | None = None) -> set[str] | None:
    """Species observed on iNaturalist within NEARBY_KM (research grade, the taxa Perch knows),
    cached per place (to 0.1 degree) for three months; an older list is still used offline."""
    now = time.time() if now is None else now
    path = cache_dir / f"nearby_{latitude:.1f}_{longitude:.1f}.json"
    cached = None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if cached and now - cached.get("fetched", 0) < NEARBY_MAX_AGE_SECONDS:
        return set(cached["species"])
    names: set[str] = set()
    try:
        page = 1
        while True:
            response = session.get(INAT_SPECIES_COUNTS, timeout=30, params={
                "lat": round(latitude, 3), "lng": round(longitude, 3), "radius": NEARBY_KM,
                "quality_grade": "research", "taxon_id": NEARBY_TAXA, "per_page": 500, "page": page})
            response.raise_for_status()
            data = response.json()
            names.update(item["taxon"]["name"] for item in data.get("results", []) if item.get("taxon"))
            if page * 500 >= int(data.get("total_results", 0)) or not data.get("results"):
                break
            page += 1
    except (requests.RequestException, ValueError, KeyError):
        return set(cached["species"]) if cached else None
    if names:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched": now, "species": sorted(names)}), encoding="utf-8")
    return names or (set(cached["species"]) if cached else None)


def perch_opinion(samples: np.ndarray, rate: int, latitude: float, longitude: float) -> list[list] | None:
    """What Perch hears in a clip, among the species around it; None when it cannot say."""
    perch = model()
    allowed = nearby_species(latitude, longitude) if perch is not None else None
    if perch is None or not allowed:
        return None
    return perch.opinion(samples, rate, allowed)
