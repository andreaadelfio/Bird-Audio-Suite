"""Re-file detections that older versions saved under the day the live session started.

Until the fix, live mode computed ``detections/YYYYMMDD`` once at startup: a session
running for days saved every clip in its first day folder, and the per-folder CSV
merged all those days into a single observation per species. Each clip name still
carries the moment it was heard (``<name>_YYYYMMDD_HHMMSS_<confidence>.wav``), so the
clips can be moved to the right day and the CSVs rebuilt one row per species per day.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .audio import append_inaturalist_csv_row, day_csv_path, parse_timestamp_from_name

_CLIP_CONFIDENCE = re.compile(r"_(\d+\.\d+)\.wav$", re.IGNORECASE)


@dataclass
class ClipPlan:
    source: Path
    target: Path
    heard_at: datetime
    confidence: float


@dataclass
class ObservationPlan:
    taxon_name: str
    english_name: str
    italian_name: str
    german_name: str
    place_name: str
    latitude: float | None
    longitude: float | None
    tags: str
    geoprivacy: str
    observed_at: datetime
    confidence: float
    clips: list[ClipPlan] = field(default_factory=list)


@dataclass
class RepairPlan:
    detections_dir: Path
    source_csvs: list[Path]
    observations: list[ObservationPlan]
    unreferenced_clips: list[Path]
    missing_clips: list[str]

    @property
    def moves(self) -> list[ClipPlan]:
        return [clip for obs in self.observations for clip in obs.clips if clip.source != clip.target]

    @property
    def target_csvs(self) -> set[Path]:
        return {day_csv_path(self.detections_dir, obs.observed_at) for obs in self.observations}


def _parse_description(description: str) -> tuple[list[str], float, list[str]]:
    names: list[str] = []
    confidence = 0.0
    audio_files: list[str] = []
    for index, part in enumerate(description.split("; ")):
        if index == 0:
            names = [name.strip() for name in part.split(",")]
        elif part.startswith("birdnet_confidence="):
            try:
                confidence = float(part.split("=", 1)[1])
            except ValueError:
                confidence = 0.0
        elif part.startswith("audio_files="):
            audio_files = [name for name in part.split("=", 1)[1].split(",") if name]
    return names, confidence, audio_files


def _optional_float(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def build_repair_plan(detections_dir: Path, species_catalog=None) -> RepairPlan:
    detections_dir = Path(detections_dir)
    source_csvs = sorted(detections_dir.glob("*/inaturalist_import_*.csv"))
    clip_index: dict[str, list[Path]] = {}
    for clip in detections_dir.glob("*/*/*.wav"):
        clip_index.setdefault(clip.name, []).append(clip)

    referenced: set[Path] = set()
    missing: list[str] = []
    observations: dict[tuple[str, str], ObservationPlan] = {}

    for csv_path in source_csvs:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            taxon_name = (row.get("Nome del taxon") or "").strip()
            if not taxon_name:
                continue
            names, row_confidence, audio_files = _parse_description(row.get("Descrizione") or "")
            english_name = names[0] if names else ""
            if species_catalog is not None:
                italian_name, german_name, english_name = species_catalog.display_names(taxon_name, english_name)
            else:
                italian_name = names[1] if len(names) > 1 else taxon_name
                german_name = names[-1] if len(names) > 2 else taxon_name

            try:
                row_time = datetime.strptime((row.get("Data osservazione") or "").strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                row_time = None

            def observation_for(when: datetime) -> ObservationPlan:
                key = (when.strftime("%Y%m%d"), taxon_name)
                plan = observations.get(key)
                if plan is None:
                    plan = ObservationPlan(
                        taxon_name=taxon_name,
                        english_name=english_name,
                        italian_name=italian_name,
                        german_name=german_name,
                        place_name=(row.get("Nome del luogo") or "").strip(),
                        latitude=_optional_float(row.get("Latitudine / coord y / nord") or ""),
                        longitude=_optional_float(row.get("Longitudine / coord x / est") or ""),
                        tags=(row.get("Etichette") or "").strip(),
                        geoprivacy=(row.get("Geoprivacy") or "").strip(),
                        observed_at=when,
                        confidence=0.0,
                    )
                    observations[key] = plan
                plan.observed_at = max(plan.observed_at, when)
                return plan

            placed = False
            for audio_name in audio_files:
                candidates = [path for path in clip_index.get(audio_name, []) if path.parent.parent == csv_path.parent]
                candidates = candidates or clip_index.get(audio_name, [])
                heard_at = parse_timestamp_from_name(audio_name)
                if not candidates or heard_at is None:
                    missing.append(f"{csv_path.parent.name}/{audio_name}")
                    continue
                source = candidates[0]
                referenced.add(source)
                confidence_match = _CLIP_CONFIDENCE.search(audio_name)
                confidence = float(confidence_match.group(1)) if confidence_match else row_confidence
                plan = observation_for(heard_at)
                plan.confidence = max(plan.confidence, confidence)
                plan.clips.append(
                    ClipPlan(
                        source=source,
                        target=detections_dir / heard_at.strftime("%Y%m%d") / source.parent.name / source.name,
                        heard_at=heard_at,
                        confidence=confidence,
                    )
                )
                placed = True

            if not placed and row_time is not None:
                # observation without clips: it stays on the day it was recorded
                plan = observation_for(row_time)
                plan.confidence = max(plan.confidence, row_confidence)

    unreferenced = sorted(set(path for paths in clip_index.values() for path in paths) - referenced)
    ordered = sorted(observations.values(), key=lambda obs: (obs.observed_at, obs.taxon_name))
    return RepairPlan(detections_dir, source_csvs, ordered, unreferenced, missing)


def apply_repair_plan(plan: RepairPlan) -> Path:
    """Move the clips, rebuild every CSV and return the backup folder (CSVs + move manifest)."""
    detections_dir = plan.detections_dir
    backup_dir = detections_dir / f"_backup_repair_{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True)
    for csv_path in plan.source_csvs:
        target = backup_dir / csv_path.relative_to(detections_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(csv_path, target)

    moves = plan.moves
    manifest = [
        {"from": str(move.source.relative_to(detections_dir)), "to": str(move.target.relative_to(detections_dir))}
        for move in moves
    ]
    (backup_dir / "moves.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    for move in moves:
        if move.target.exists():
            raise FileExistsError(f"refusing to overwrite {move.target}")
        move.target.parent.mkdir(parents=True, exist_ok=True)
        move.source.rename(move.target)

    for csv_path in plan.source_csvs:
        csv_path.unlink()
    for observation in plan.observations:
        csv_path = day_csv_path(detections_dir, observation.observed_at)
        common = dict(
            taxon_name=observation.taxon_name,
            english_name=observation.english_name,
            italian_name=observation.italian_name,
            german_name=observation.german_name,
            place_name=observation.place_name,
            latitude=observation.latitude,
            longitude=observation.longitude,
            tags=observation.tags,
            geoprivacy=observation.geoprivacy,
        )
        if not observation.clips:
            append_inaturalist_csv_row(
                csv_path,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                **common,
            )
        for clip in sorted(observation.clips, key=lambda item: item.heard_at):
            append_inaturalist_csv_row(
                csv_path,
                observed_at=clip.heard_at,
                confidence=clip.confidence,
                audio_file_name=clip.target.name,
                **common,
            )

    for folder in sorted(detections_dir.glob("*/*/"), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()) and not folder.parent.name.startswith("_"):
            folder.rmdir()
    for folder in detections_dir.iterdir():
        if folder.is_dir() and not folder.name.startswith("_") and not any(folder.iterdir()):
            folder.rmdir()
    return backup_dir
