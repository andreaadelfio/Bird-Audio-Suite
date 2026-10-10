"""Sync local detections to Supabase: data in the `birds` schema, audio and spectrograms
in the public Storage bucket `bird-audio` (or Cloudinary, if its keys are configured).

The laptop stays the source of truth: clips and daily CSVs are written locally as before,
and ``bird_audio_cli.py sync`` pushes what changed. It is idempotent and works offline
(it just catches up on the next run), so it suits a laptop that travels.

Settings live outside the repository, in ``~/.config/bird-audio-suite/cloud.env``:

    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_KEY=sb_publishable_...              # `birds` and the bucket are open: the site's key is enough
    DELETE_LOCAL_AUDIO=true                      # optional: remove WAVs once safely on Supabase
    # optional, instead of Supabase Storage:
    # CLOUDINARY_CLOUD_NAME=...  CLOUDINARY_API_KEY=...  CLOUDINARY_API_SECRET=...

``MEDIA_STORAGE=local`` (used by ``make web-dev``) makes media URLs point to
``MEDIA_BASE_URL`` (default ``media``), served by the local dev server.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from .audio import detections_lock
from .repair import ObservationPlan, build_repair_plan

DEFAULT_CLOUD_CONFIG = Path.home() / ".config" / "bird-audio-suite" / "cloud.env"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "bird-audio-suite"
CLOUDINARY_FOLDER = "bird-audio-suite"
STORAGE_BUCKET = "bird-audio"
SCHEMA = "birds"


@dataclass
class CloudConfig:
    supabase_url: str = ""
    supabase_key: str = ""
    rest_path: str = "/rest/v1"
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    media_base_url: str = "media"
    media_storage: str = ""  # "supabase" | "cloudinary" | "local"; empty = cloudinary if configured, else supabase
    delete_local_audio: bool = False  # the laptop as temporary storage: WAVs go once safely online

    @property
    def uses_cloudinary(self) -> bool:
        return bool(self.cloudinary_cloud_name and self.cloudinary_api_key and self.cloudinary_api_secret)

    @classmethod
    def load(cls, path: Path = DEFAULT_CLOUD_CONFIG, overrides: dict[str, str] | None = None) -> "CloudConfig":
        values: dict[str, str] = {}
        if Path(path).exists():
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        values.update(overrides or {})
        return cls(
            supabase_url=values.get("SUPABASE_URL", "").rstrip("/"),
            supabase_key=values.get("SUPABASE_KEY") or values.get("SUPABASE_SECRET_KEY", ""),
            rest_path=values.get("SUPABASE_REST_PATH", "/rest/v1"),
            cloudinary_cloud_name=values.get("CLOUDINARY_CLOUD_NAME", ""),
            cloudinary_api_key=values.get("CLOUDINARY_API_KEY", ""),
            cloudinary_api_secret=values.get("CLOUDINARY_API_SECRET", ""),
            media_base_url=values.get("MEDIA_BASE_URL", "media").rstrip("/"),
            media_storage=values.get("MEDIA_STORAGE", "").strip().lower(),
            delete_local_audio=values.get("DELETE_LOCAL_AUDIO", "").strip().lower() in {"1", "true", "yes", "si", "sì"},
        )

    @property
    def storage_backend(self) -> str:
        if self.media_storage:
            return self.media_storage
        return "cloudinary" if self.uses_cloudinary else "supabase"


# ---------------------------------------------------------------- Cloudinary


def cloudinary_signature(params: dict[str, str], api_secret: str) -> str:
    """Signature of a signed upload: sorted ``key=value`` pairs joined by ``&``, plus the secret, SHA-1."""
    to_sign = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key] not in ("", None))
    return hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()


def slugify_public_id(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9_]+", "-", ascii_name.lower()).strip("-")


class CloudinaryUploader:
    def __init__(self, config: CloudConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def upload(self, path: Path, name: str, resource_type: str) -> str:
        """Upload ``path`` (resource_type "video" for audio, "image" for pictures); return its HTTPS URL."""
        public_id = f"{CLOUDINARY_FOLDER}/{name}"
        params = {"public_id": public_id, "timestamp": str(int(time.time())), "overwrite": "true"}
        params["signature"] = cloudinary_signature(params, self.config.cloudinary_api_secret)
        params["api_key"] = self.config.cloudinary_api_key
        url = f"https://api.cloudinary.com/v1_1/{self.config.cloudinary_cloud_name}/{resource_type}/upload"
        with Path(path).open("rb") as handle:
            response = self.session.post(url, data=params, files={"file": handle}, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"Cloudinary upload failed ({response.status_code}): {response.text[:300]}")
        return response.json()["secure_url"]


# ---------------------------------------------------------------- Supabase Storage


class SupabaseStorageUploader:
    """Upload to the public bucket; the object path is ``<name><file suffix>``."""

    CONTENT_TYPES = {".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".png": "image/png", ".wav": "audio/wav"}

    def __init__(self, config: CloudConfig, bucket: str = STORAGE_BUCKET, session: requests.Session | None = None) -> None:
        if not config.supabase_url or not config.supabase_key:
            raise RuntimeError("SUPABASE_URL e SUPABASE_KEY mancano in cloud.env")
        self.base = f"{config.supabase_url}/storage/v1"
        self.bucket = bucket
        self.session = session or requests.Session()
        self.session.headers.update(supabase_auth_headers(config.supabase_key))

    def upload(self, path: Path, name: str, resource_type: str = "") -> str:
        object_path = f"{name}{Path(path).suffix.lower()}"
        response = self.session.post(
            f"{self.base}/object/{self.bucket}/{object_path}",
            data=Path(path).read_bytes(),
            headers={
                "Content-Type": self.CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream"),
                "x-upsert": "true",
                "Cache-Control": "max-age=31536000",
            },
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase Storage upload failed ({response.status_code}): {response.text[:300]}")
        return f"{self.base}/object/public/{self.bucket}/{object_path}"


def supabase_auth_headers(key: str) -> dict[str, str]:
    # same as supabase-js: the key in `apikey` and as bearer token (the gateway accepts sb_ keys there too)
    return {"apikey": key, "Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- Supabase REST


class SupabaseRest:
    def __init__(self, config: CloudConfig, session: requests.Session | None = None, timeout: float = 60) -> None:
        if not config.supabase_url or not config.supabase_key:
            raise RuntimeError("SUPABASE_URL e SUPABASE_KEY mancano in cloud.env")
        self.timeout = timeout
        self.base = f"{config.supabase_url}{config.rest_path}"
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                **supabase_auth_headers(config.supabase_key),
                "Accept-Profile": SCHEMA,
                "Content-Profile": SCHEMA,
                "Content-Type": "application/json",
            }
        )

    def get(self, table: str, params: dict) -> list[dict]:
        response = self.session.get(f"{self.base}/{table}", params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase {table} read failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    def get_observation(self, day: str, scientific_name: str) -> dict | None:
        rows = self.get(
            "observations",
            {
                "select": "max_confidence,observed_at,place_name,latitude,longitude",
                "day": f"eq.{day}",
                "scientific_name": f"eq.{scientific_name}",
            },
        )
        return rows[0] if rows else None

    def select_column(self, table: str, column: str, page_size: int = 1000) -> list:
        values: list = []
        while True:
            response = self.session.get(
                f"{self.base}/{table}",
                params={"select": column, "order": column},
                headers={"Range": f"{len(values)}-{len(values) + page_size - 1}"},
                timeout=60,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Supabase {table} read failed ({response.status_code}): {response.text[:300]}")
            page = [row[column] for row in response.json()]
            values.extend(page)
            if len(page) < page_size:
                return values

    def upsert(self, table: str, rows: list[dict], on_conflict: str, returning: bool = False) -> list[dict]:
        if not rows:
            return []
        prefer = "resolution=merge-duplicates," + ("return=representation" if returning else "return=minimal")
        response = self.session.post(
            f"{self.base}/{table}",
            params={"on_conflict": on_conflict},
            data=json.dumps(rows),
            headers={"Prefer": prefer},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase {table} upsert failed ({response.status_code}): {response.text[:300]}")
        return response.json() if returning else []


# ---------------------------------------------------------------- media


def render_spectrogram(audio_path: Path, output_path: Path) -> Path:
    """Mel spectrogram 0-12 kHz, the view used to judge a BirdNET detection by eye."""
    import librosa
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    samples, sample_rate = librosa.load(str(audio_path), sr=None, mono=True)
    fmax = min(12000, sample_rate // 2)
    mel = librosa.feature.melspectrogram(y=samples, sr=sample_rate, n_mels=128, fmin=150, fmax=fmax, hop_length=512)
    mel_db = librosa.power_to_db(mel, ref=np.max)
    # Remove the steady background (hum, wind, traffic) band by band: calls stand out.
    mel_db = mel_db - np.median(mel_db, axis=1, keepdims=True)
    duration = len(samples) / float(sample_rate)
    figure = plt.figure(figsize=(max(4.0, min(duration * 0.35, 16.0)), 2.4), dpi=100)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.imshow(mel_db, origin="lower", aspect="auto", cmap="magma", vmin=0, vmax=max(20.0, float(np.percentile(mel_db, 99.5))))
    axes.set_axis_off()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # JPEG: a noisy spectrogram is ~10x smaller than PNG and looks the same
    figure.savefig(output_path, format="jpeg", pil_kwargs={"quality": 80})
    plt.close(figure)
    return output_path


def encode_mp3(audio_path: Path, output_path: Path) -> Path:
    """MP3 for listening on the site: ~8x smaller than the WAV, BirdNET scores it the same."""
    import soundfile

    samples, sample_rate = soundfile.read(str(audio_path), dtype="float32")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(
        str(output_path),
        samples,
        sample_rate,
        format="MP3",
        subtype="MPEG_LAYER_III",
        compression_level=0.3,
        bitrate_mode="VARIABLE",
    )
    return output_path


def audio_duration(path: Path) -> float:
    import soundfile

    return float(soundfile.info(str(path)).duration)


# ---------------------------------------------------------------- sync


def merge_with_existing(row: dict, existing: dict | None) -> dict:
    """The transit folder only holds what is not online yet, so a later detection of the same
    species and day must add to the observation, not overwrite it: highest confidence, latest
    time, and the place where it was first heard."""
    if not existing:
        return row
    merged = dict(row)
    merged["max_confidence"] = max(float(row["max_confidence"]), float(existing.get("max_confidence") or 0))
    if existing.get("observed_at"):
        merged["observed_at"] = max(
            (row["observed_at"], existing["observed_at"]),
            key=lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00")),
        )
    if existing.get("latitude") is not None and existing.get("longitude") is not None:
        for column in ("place_name", "latitude", "longitude"):
            merged[column] = existing[column]
    return merged


_AUDIO_FILES = re.compile(r"(audio_files=)([^;]*)")


def remove_synced_from_csv(csv_path: Path, synced: set[str]) -> int:
    """Drop uploaded clips from a day CSV (call it under detections_lock); return rows left.

    A row whose clips are all online goes; a row with no clips yet (the live service writes
    the observation first and adds the clip when it closes) stays. An empty CSV is deleted."""
    from .audio import INATURALIST_CSV_HEADER

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    kept = []
    for row in rows:
        description = row.get("Descrizione") or ""
        match = _AUDIO_FILES.search(description)
        if match is None:
            kept.append(row)
            continue
        names = [name for name in match.group(2).split(",") if name]
        remaining = [name for name in names if name not in synced]
        if names and not remaining:
            continue
        row["Descrizione"] = description[: match.start()] + "audio_files=" + ",".join(remaining) + description[match.end():]
        kept.append(row)
    if not kept:
        csv_path.unlink()
        return 0
    if len(kept) != len(rows) or kept != rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INATURALIST_CSV_HEADER)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in INATURALIST_CSV_HEADER} for row in kept)
    return len(kept)


@dataclass
class SyncReport:
    observations_total: int = 0
    observations_synced: int = 0
    clips_uploaded: int = 0
    species_synced: int = 0
    local_deleted: int = 0
    local_bytes_freed: int = 0
    csv_removed: int = 0
    errors: list[str] = field(default_factory=list)


def remote_file_exists(url: str) -> bool:
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def _iso(value: datetime) -> str:
    # naive local wall-clock time -> with the local UTC offset
    return value.astimezone().isoformat()


def _observation_row(observation: ObservationPlan) -> dict:
    return {
        "day": observation.observed_at.date().isoformat(),
        "scientific_name": observation.taxon_name,
        "observed_at": _iso(observation.observed_at),
        "place_name": observation.place_name,
        "latitude": observation.latitude,
        "longitude": observation.longitude,
        "max_confidence": round(observation.confidence, 3),
    }


def _fingerprint(row: dict, clip_ids: list[str]) -> str:
    return hashlib.sha1(json.dumps([row, sorted(clip_ids)], sort_keys=True).encode()).hexdigest()


class DetectionSync:
    def __init__(
        self,
        detections_dir: Path,
        species_catalog,
        config: CloudConfig,
        *,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        rest: SupabaseRest | None = None,
        uploader: CloudinaryUploader | None = None,
        render=render_spectrogram,
        log=print,
        verify_remote=remote_file_exists,
    ) -> None:
        self.detections_dir = Path(detections_dir)
        self.catalog = species_catalog
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.state_path = self.cache_dir / "sync_state.json"
        self.rest = rest
        if uploader is None and config.storage_backend == "cloudinary":
            uploader = CloudinaryUploader(config)
        elif uploader is None and config.storage_backend == "supabase":
            uploader = SupabaseStorageUploader(config)
        self.uploader = uploader  # None: local media (make web-dev)
        self.render = render
        self.log = log
        self.verify_remote = verify_remote
        self.state = self._load_state()

    # state: which media are already online and which observations are unchanged
    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("target") != self.config.supabase_url:
            state = {"target": self.config.supabase_url, "media": {}, "observations": {}}
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def spectrogram_path(self, clip_path: Path) -> Path:
        return self.cache_dir / "spectrograms" / clip_path.parent.parent.name / f"{clip_path.stem}.jpg"

    def _media_urls(self, clip_path: Path) -> dict:
        cached = self.state["media"].get(clip_path.name)
        if cached:
            return cached
        spectrogram = self.spectrogram_path(clip_path)
        if not spectrogram.exists():
            self.render(clip_path, spectrogram)
        relative = clip_path.relative_to(self.detections_dir).as_posix()
        if self.uploader is not None:
            day = clip_path.parent.parent.name
            name = f"{day}/{slugify_public_id(clip_path.stem)}"
            # the WAV stays on the laptop (iNaturalist uploads use it); the site gets an MP3
            mp3 = encode_mp3(clip_path, self.cache_dir / "mp3" / day / f"{clip_path.stem}.mp3")
            audio_url = self.uploader.upload(mp3, name, "video")  # Cloudinary files audio under "video"
            image_url = self.uploader.upload(spectrogram, f"{name}-spectrogram", "image")
            mp3.unlink(missing_ok=True)
            media = {"audio_url": audio_url, "audio_public_id": name, "spectrogram_url": image_url}
        else:
            base = self.config.media_base_url
            media = {
                "audio_url": f"{base}/clips/{relative}",
                "audio_public_id": None,
                "spectrogram_url": f"{base}/spectrograms/{clip_path.parent.parent.name}/{spectrogram.name}",
            }
        media["duration_s"] = round(audio_duration(clip_path), 2)
        self.state["media"][clip_path.name] = media
        return media

    def _species_rows(self, taxa: set[str]) -> list[dict]:
        rows = []
        for name in sorted(taxa):
            record = self.catalog.records.get(name)
            italian, german, english = self.catalog.display_names(name)
            rows.append(
                {
                    "scientific_name": name,
                    "english_name": english,
                    "italian_name": italian,
                    "german_name": german,
                    "first_seen_date": (record.first_seen_date or None) if record else None,
                    "last_seen_date": (record.last_seen_date or None) if record else None,
                    "observed_days": record.observed_days if record else 0,
                    "total_count": record.total_count if record else 0,
                    "max_confidence": round(record.last_confidence, 3) if record else 0,
                }
            )
        return rows

    def run(self, *, dry_run: bool = False, limit: int | None = None) -> SyncReport:
        # clips already uploaded may have had their WAV removed: they still belong to their observation
        plan = build_repair_plan(self.detections_dir, self.catalog, stored_elsewhere=set(self.state["media"]))
        if plan.moves:
            raise RuntimeError(
                f"{len(plan.moves)} clip nel giorno sbagliato: esegui prima `repair-detections --apply`"
            )
        if self.rest is None and not dry_run:
            self.rest = SupabaseRest(self.config)
        # clips deleted for good from the site's "Cestino": never upload them again
        deleted: set[str] = set()
        if self.rest is not None:
            try:
                deleted = set(self.rest.select_column("deleted_clips", "id"))
            except RuntimeError as exc:
                # schema not updated yet on Supabase: keep syncing as before
                self.log(f"Lista delle clip eliminate non disponibile ({exc}): riesegui supabase/birds_schema.sql")
        for clip_id in deleted:
            self.state["media"].pop(clip_id, None)

        report = SyncReport(observations_total=len(plan.observations))
        pending = []
        trashed_on_disk = []
        for observation in plan.observations:
            trashed_on_disk += [clip for clip in observation.clips if clip.source.name in deleted and clip.on_disk]
            observation.clips = [clip for clip in observation.clips if clip.source.name not in deleted]
            if not observation.clips:
                report.observations_total -= 1
                continue
            row = _observation_row(observation)
            key = f"{row['day']}|{row['scientific_name']}"
            clip_ids = [clip.source.name for clip in observation.clips]
            if self.state["observations"].get(key) != _fingerprint(row, clip_ids):
                pending.append((key, row, observation))
        if limit is not None:
            pending = pending[:limit]

        if dry_run:
            new_media = sum(
                1 for _, _, obs in pending for clip in obs.clips if clip.source.name not in self.state["media"]
            )
            self.log(f"[dry-run] {len(pending)} osservazioni da sincronizzare, {new_media} clip da caricare")
            report.observations_synced = len(pending)
            if self.config.delete_local_audio and self.uploader is not None:
                removable = self._removable_local_clips(plan, verify=False) + trashed_on_disk
                report.local_deleted = len(removable)
                report.local_bytes_freed = sum(clip.source.stat().st_size for clip in removable)
            return report

        species = self._species_rows({row["scientific_name"] for _, row, _ in pending})
        self.rest.upsert("species", species, on_conflict="scientific_name")
        report.species_synced = len(species)

        for index, (key, row, observation) in enumerate(pending, start=1):
            try:
                upsert_row = row
                if self.config.delete_local_audio:
                    # earlier clips of this day may already be online and gone from here
                    upsert_row = merge_with_existing(row, self.rest.get_observation(row["day"], row["scientific_name"]))
                stored = self.rest.upsert("observations", [upsert_row], on_conflict="day,scientific_name", returning=True)
                observation_id = stored[0]["id"]
                clip_rows = []
                for clip in observation.clips:
                    already_online = clip.source.name in self.state["media"]
                    media = self._media_urls(clip.source)
                    report.clips_uploaded += 0 if already_online else 1
                    clip_rows.append(
                        {
                            "id": clip.source.name,
                            "observation_id": observation_id,
                            "recorded_at": _iso(clip.heard_at),
                            "confidence": round(clip.confidence, 3),
                            **media,
                        }
                    )
                self.rest.upsert("clips", clip_rows, on_conflict="id")
                self.state["observations"][key] = _fingerprint(row, [clip.source.name for clip in observation.clips])
                report.observations_synced += 1
            except Exception as exc:  # keep going: the next run retries what failed
                report.errors.append(f"{key}: {exc}")
            finally:
                self._save_state()
            if index % 20 == 0:
                self.log(f"  {index}/{len(pending)} osservazioni")

        if self.config.delete_local_audio and self.uploader is not None:
            self._empty_transit(plan, trashed_on_disk, deleted, report)
        return report

    def _empty_transit(self, plan, trashed_on_disk: list, deleted: set[str], report: SyncReport) -> None:
        """detections/ as a transit folder: whatever is safely online (or deleted from the
        Cestino) leaves the laptop."""
        removable = self._removable_local_clips(plan, verify=True)
        gone = {clip.source.name for clip in removable + trashed_on_disk} | deleted
        # clips already removed earlier (WAV gone, still listed in a CSV) are online too
        gone |= {
            clip.source.name
            for observation in plan.observations
            for clip in observation.clips
            if not clip.on_disk and self._observation_synced(observation)
        }
        with detections_lock(self.detections_dir):
            for clip in removable + trashed_on_disk:
                if clip.source.exists():
                    report.local_bytes_freed += clip.source.stat().st_size
                    clip.source.unlink()
                    report.local_deleted += 1
                try:
                    clip.source.parent.rmdir()  # species folder, once empty
                except OSError:
                    pass
            for csv_path in sorted(self.detections_dir.glob("*/inaturalist_import_*.csv")):
                if remove_synced_from_csv(csv_path, gone) == 0:
                    report.csv_removed += 1
            # empty species and day folders, deepest first (WAVs may have gone in earlier runs)
            for folder in sorted((path for path in self.detections_dir.rglob("*") if path.is_dir()), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass  # not empty: something still waits for a sync
            still_listed = self._clips_listed_locally()
        # forget what only Supabase holds now: the state stays as small as the transit folder
        self.state["media"] = {name: media for name, media in self.state["media"].items() if name in still_listed}
        days_left = {path.name for path in self.detections_dir.glob("*/") if path.is_dir()}
        self.state["observations"] = {
            key: value for key, value in self.state["observations"].items()
            if key.split("|", 1)[0].replace("-", "") in days_left
        }
        self._save_state()

    def _clips_listed_locally(self) -> set[str]:
        names: set[str] = set()
        for csv_path in self.detections_dir.glob("*/inaturalist_import_*.csv"):
            for match in _AUDIO_FILES.finditer(csv_path.read_text(encoding="utf-8")):
                names.update(name for name in match.group(2).split(",") if name)
        return names

    def _observation_synced(self, observation) -> bool:
        row = _observation_row(observation)
        key = f"{row['day']}|{row['scientific_name']}"
        return self.state["observations"].get(key) == _fingerprint(row, [clip.source.name for clip in observation.clips])

    def _removable_local_clips(self, plan, *, verify: bool) -> list:
        """WAVs whose observation is fully on Supabase and whose MP3 is really downloadable."""
        removable = []
        for observation in plan.observations:
            if not observation.clips or not self._observation_synced(observation):
                continue  # not (fully) synced yet: keep everything
            for clip in observation.clips:
                media = self.state["media"].get(clip.source.name)
                if not clip.on_disk or not media:
                    continue
                if verify and not self.verify_remote(media["audio_url"]):
                    self.log(f"  tengo {clip.source.name}: l'MP3 su Supabase non risponde")
                    continue
                removable.append(clip)
        return removable
