"""One analysis of a stored clip: everything the review shows comes from it.

BirdNET on the whole clip as it is saved, 3 s chunks every 0.5 s. From the same run:
  * confidence: the best score of the clip's species (clip_conf, max_obs_conf, likely/unsure);
  * segments: every passage where it hears that species from 0.6 (the bands, coloured by their own
    score: 0.6-0.8 unsure, from 0.8 likely; the best band is the clip's confidence when it is at
    least 0.6), so a clip found at 0.65 shows whether the bird reaches 0.8 elsewhere, and one at
    0.8 whether it comes back more faintly;
  * candidates: the (up to three) species heard from 0.6, the clip's included.
Below 0.6 nothing is shown: not trustworthy. Only the clip's confidence keeps its value even when
lower, because a 0.45 says that the saved audio does not confirm the species.
The score the live gave while listening is kept apart (clips.live_confidence): it came from other
chunks of the stream (and, until October 2026, from an older pipeline), so it cannot be compared
with what can be checked on the saved audio. The uploader runs this on each new clip, the
backfill on the clips already on Supabase.
"""

from __future__ import annotations

from datetime import datetime

from .audio import birdnet_candidates, detection_segments

ANALYSIS_OVERLAP_SECONDS = 2.5  # a chunk every 0.5 s: BirdNET's score depends a lot on where a call falls
ANALYSIS_MIN_CONFIDENCE = 0.05  # only for the clip's own best score (see above)
BAND_MIN_CONFIDENCE = 0.6  # bands and candidates: the live's threshold, below it nothing is trusted

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
    trusted = [d for d in detections if float(d.get("confidence", 0)) >= BAND_MIN_CONFIDENCE]
    duration = len(samples) / float(rate)
    return {
        "confidence": round(max((float(d["confidence"]) for d in own), default=0.0), 3),
        "segments": [[round(start, 2), round(min(end, duration), 2), round(confidence, 3)]
                     for start, end, confidence in bands],
        "candidates": birdnet_candidates(trusted),
    }
