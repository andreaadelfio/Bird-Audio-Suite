"""One analysis of a stored clip: everything the review shows comes from it.

BirdNET on the whole clip as it is saved, 3 s chunks every 0.5 s. From the same run:
  * confidence: the best score of the clip's species (clip_conf, max_obs_conf, likely/unsure);
  * segments: where it hears that species, from BAND_MIN_CONFIDENCE (the bands, coloured by their
    own score; the best band is the clip's confidence, so colours and numbers always agree);
  * candidates: the three most likely species of all.
The score the live gave while listening is kept apart (clips.live_confidence): it came from other
chunks of the stream (and, until October 2026, from an older pipeline), so it cannot be compared
with what can be checked on the saved audio. The uploader runs this on each new clip, the
backfill on the clips already on Supabase.
"""

from __future__ import annotations

from datetime import datetime

from .audio import birdnet_candidates, detection_segments

ANALYSIS_OVERLAP_SECONDS = 2.5  # a chunk every 0.5 s: BirdNET's score depends a lot on where a call falls
ANALYSIS_MIN_CONFIDENCE = 0.05  # low: the candidates and the weak passages need it
BAND_MIN_CONFIDENCE = 0.3
FALLBACK_MIN_CONFIDENCE = 0.1  # nothing above the band threshold: the best chunk still shows where to look

_detector = None


def detector():
    """The BirdNET model, loaded once per process (never in the live: it has its own)."""
    global _detector
    if _detector is None:
        from .detector import BirdNetDetector

        _detector = BirdNetDetector()
    return _detector


def analyse_samples(samples, rate: int, species: str, latitude: float, longitude: float, when: datetime,
                    model=None) -> dict:
    model = model or detector()
    detections = model.detect_samples(samples, rate, latitude=latitude, longitude=longitude, when=when,
                                      min_confidence=ANALYSIS_MIN_CONFIDENCE, overlap=ANALYSIS_OVERLAP_SECONDS)
    own = [d for d in detections if d.get("scientific_name") == species]
    bands = detection_segments(own, BAND_MIN_CONFIDENCE).get(species, [])
    if not bands:
        heard = [d for d in own if float(d.get("confidence", 0)) >= FALLBACK_MIN_CONFIDENCE]
        if heard:
            best = max(heard, key=lambda d: d["confidence"])
            bands = [(float(best["start_time"]), float(best["end_time"]), float(best["confidence"]))]
    duration = len(samples) / float(rate)
    return {
        "confidence": round(max((float(d["confidence"]) for d in own), default=0.0), 3),
        "segments": [[round(start, 2), round(min(end, duration), 2), round(confidence, 3)]
                     for start, end, confidence in bands],
        "candidates": birdnet_candidates(detections),
    }
