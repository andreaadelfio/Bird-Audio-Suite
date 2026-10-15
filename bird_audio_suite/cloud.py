"""Supabase: the only place where detections live.

The laptop writes each detection into ``detections/`` (WAV + JSON, see
``audio.export_detection_clips``); :class:`Outbox` sends it to Supabase and then removes it:
the MP3 and the spectrogram go to the Storage bucket ``bird-audio``, a row to ``birds.clips``,
and the clip joins the observation of the same species heard within 10 minutes (the same
visit) or starts a new one. Nothing is stored twice: the clip row keeps only the path of its
files, and every statistic comes from the views of supabase/birds_schema.sql. Whatever cannot
be sent (offline) stays in the folder for the next attempt of the uploader service
(uploader.py: moments after each clip, then every minute).

Settings, outside the repository, in ``~/.config/bird-audio-suite/cloud.env``:

    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_KEY=sb_publishable_...      # `birds` and the bucket are open: the site's key is enough

``MEDIA_STORAGE=local`` with ``LOCAL_MEDIA_DIR`` (used by ``make web-dev``) writes the files
into a folder instead of the bucket.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .audio import detections_lock, parse_timestamp_from_name
from .config import OBSERVATION_GAP_MINUTES

DEFAULT_CLOUD_CONFIG = Path.home() / ".config" / "bird-audio-suite" / "cloud.env"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "bird-audio-suite"
STORAGE_BUCKET = "bird-audio"
SCHEMA = "birds"
# observations uploaded to iNaturalist keep their files on Supabase this long, then only the rows
UPLOADED_MEDIA_DAYS = 7


@dataclass
class CloudConfig:
    supabase_url: str = ""
    supabase_key: str = ""
    rest_path: str = "/rest/v1"
    media_storage: str = "supabase"  # "supabase" | "local"
    local_media_dir: Path = DEFAULT_CACHE_DIR / "local-media"

    @property
    def configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

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
            media_storage=(values.get("MEDIA_STORAGE") or "supabase").strip().lower(),
            local_media_dir=Path(values.get("LOCAL_MEDIA_DIR") or DEFAULT_CACHE_DIR / "local-media"),
        )


def supabase_auth_headers(key: str) -> dict[str, str]:
    # same as supabase-js: the key in `apikey` and as bearer token (the gateway accepts sb_ keys there too)
    return {"apikey": key, "Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- Supabase REST


class SupabaseRest:
    def __init__(self, config: CloudConfig, session: requests.Session | None = None, timeout: float = 60) -> None:
        if not config.configured:
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

    @staticmethod
    def _check(response: requests.Response, what: str) -> requests.Response:
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase {what} ({response.status_code}): {response.text[:300]}")
        return response

    def get(self, table: str, params: dict | list[tuple[str, str]]) -> list[dict]:
        response = self.session.get(f"{self.base}/{table}", params=params, timeout=self.timeout)
        return self._check(response, f"{table} read").json()

    def select_column(self, table: str, column: str, page_size: int = 1000) -> list:
        values: list = []
        while True:
            response = self.session.get(
                f"{self.base}/{table}",
                params={"select": column, "order": column},
                headers={"Range": f"{len(values)}-{len(values) + page_size - 1}"},
                timeout=self.timeout,
            )
            page = [row[column] for row in self._check(response, f"{table} read").json()]
            values.extend(page)
            if len(page) < page_size:
                return values

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        response = self.session.post(
            f"{self.base}/{table}",
            params={"on_conflict": on_conflict},
            data=json.dumps(rows),
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=self.timeout,
        )
        self._check(response, f"{table} upsert")

    def update(self, table: str, filters: dict, values: dict) -> None:
        response = self.session.patch(
            f"{self.base}/{table}", params=filters, data=json.dumps(values),
            headers={"Prefer": "return=minimal"}, timeout=self.timeout,
        )
        self._check(response, f"{table} update")

    def observation_for(self, scientific_name: str, recorded_at: datetime,
                        gap: timedelta = timedelta(minutes=OBSERVATION_GAP_MINUTES)) -> int:
        """The observation a clip belongs to: the one with a clip of the same species within `gap`
        of it (the nearest, if more), else a new one dated the clip's day. Review columns are never
        touched; across midnight it works too (it looks at times, not days)."""
        near = self.get("clips", [
            ("select", "observation_id,recorded_at,observations!inner(scientific_name)"),
            ("observations.scientific_name", f"eq.{scientific_name}"),
            ("recorded_at", f"gte.{(recorded_at - gap).isoformat()}"),
            ("recorded_at", f"lte.{(recorded_at + gap).isoformat()}"),
        ])
        if near:
            nearest = min(near, key=lambda row: abs(datetime.fromisoformat(row["recorded_at"]) - recorded_at))
            return int(nearest["observation_id"])
        response = self.session.post(
            f"{self.base}/observations",
            data=json.dumps([{"day": recorded_at.astimezone().date().isoformat(), "scientific_name": scientific_name}]),
            headers={"Prefer": "return=representation"},
            timeout=self.timeout,
        )
        return int(self._check(response, "observations insert").json()[0]["id"])


# ---------------------------------------------------------------- media


def slugify_public_id(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9_]+", "-", ascii_name.lower()).strip("-")


def media_path_for(clip_id: str, recorded_at: datetime) -> str:
    """Path of a clip's files in the bucket, without extension: <YYYYMMDD>/<slug>."""
    return f"{recorded_at:%Y%m%d}/{slugify_public_id(Path(clip_id).stem)}"


class SupabaseStorage:
    CONTENT_TYPES = {".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".png": "image/png", ".wav": "audio/wav"}

    def __init__(self, config: CloudConfig, bucket: str = STORAGE_BUCKET, session: requests.Session | None = None) -> None:
        if not config.configured:
            raise RuntimeError("SUPABASE_URL e SUPABASE_KEY mancano in cloud.env")
        self.base = f"{config.supabase_url}/storage/v1"
        self.bucket = bucket
        self.session = session or requests.Session()
        self.session.headers.update(supabase_auth_headers(config.supabase_key))

    def public_url(self, object_path: str) -> str:
        return f"{self.base}/object/public/{self.bucket}/{object_path}"

    def upload(self, path: Path, object_path: str) -> None:
        response = self.session.post(
            f"{self.base}/object/{self.bucket}/{object_path}",
            data=Path(path).read_bytes(),
            headers={
                "Content-Type": self.CONTENT_TYPES.get(Path(object_path).suffix.lower(), "application/octet-stream"),
                "x-upsert": "true",
                "Cache-Control": "max-age=31536000",
            },
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase Storage upload failed ({response.status_code}): {response.text[:300]}")

    def exists(self, object_path: str) -> bool:
        try:
            return requests.head(self.public_url(object_path), timeout=30).status_code == 200
        except requests.RequestException:
            return False

    def remove(self, object_paths: list[str]) -> None:
        """Delete files (missing ones are not an error)."""
        if not object_paths:
            return
        response = self.session.delete(f"{self.base}/object/{self.bucket}", json={"prefixes": object_paths},
                                       timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"Supabase Storage delete failed ({response.status_code}): {response.text[:300]}")


class LocalMediaStorage:
    """`make web-dev`: the same files, in a local folder served by the dev server."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def upload(self, path: Path, object_path: str) -> None:
        target = self.folder / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

    def exists(self, object_path: str) -> bool:
        return (self.folder / object_path).is_file()

    def remove(self, object_paths: list[str]) -> None:
        for object_path in object_paths:
            (self.folder / object_path).unlink(missing_ok=True)


def render_spectrogram(audio_path: Path, output_path: Path) -> Path:
    """Mel spectrogram 0-12 kHz with the steady background removed band by band: calls stand out."""
    import librosa
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    samples, sample_rate = librosa.load(str(audio_path), sr=None, mono=True)
    fmax = min(12000, sample_rate // 2)
    mel = librosa.feature.melspectrogram(y=samples, sr=sample_rate, n_mels=128, fmin=150, fmax=fmax, hop_length=512)
    mel_db = librosa.power_to_db(mel, ref=np.max)
    mel_db = mel_db - np.median(mel_db, axis=1, keepdims=True)
    duration = len(samples) / float(sample_rate)
    figure = plt.figure(figsize=(max(4.0, min(duration * 0.35, 16.0)), 2.4), dpi=100)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.imshow(mel_db, origin="lower", aspect="auto", cmap="magma", vmin=0,
                vmax=max(20.0, float(np.percentile(mel_db, 99.5))))
    axes.set_axis_off()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # JPEG: a noisy spectrogram is ~10x smaller than PNG and looks the same
    figure.savefig(output_path, format="jpeg", pil_kwargs={"quality": 80})
    plt.close(figure)
    return output_path


def encode_mp3(audio_path: Path, output_path: Path) -> Path:
    """MP3 for listening and for iNaturalist: ~8x smaller than the WAV, BirdNET scores it the same.
    At the recorded level (no normalization: it raises the background as much as the bird; the
    site has a volume up to +30 dB instead)."""
    import soundfile

    samples, sample_rate = soundfile.read(str(audio_path), dtype="float32")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(output_path), samples, sample_rate, format="MP3", subtype="MPEG_LAYER_III",
                    compression_level=0.3, bitrate_mode="VARIABLE")
    return output_path


def audio_duration(path: Path) -> float:
    import soundfile

    return float(soundfile.info(str(path)).duration)


# ---------------------------------------------------------------- transit folder


@dataclass
class OutboxItem:
    wav: Path
    sidecar: Path
    data: dict

    @property
    def clip_id(self) -> str:
        return self.data["id"]

    @property
    def recorded_at(self) -> datetime:
        return datetime.fromisoformat(self.data["recorded_at"])


@dataclass
class PushReport:
    pending: int = 0
    sent: int = 0
    discarded: int = 0  # deleted from the site's Cestino: removed without sending
    converted: int = 0  # left by the previous version (day CSVs), turned into items
    busy: bool = False  # another uploader was already at work
    sent_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def outbox_items(folder: Path) -> list[OutboxItem]:
    """Complete clips waiting in the transit folder, oldest first."""
    items = []
    for sidecar in Path(folder).glob("*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        wav = sidecar.with_suffix(".wav")
        if wav.exists() and data.get("id") == wav.name:
            items.append(OutboxItem(wav, sidecar, data))
    return sorted(items, key=lambda item: item.data.get("recorded_at", ""))


class Outbox:
    def __init__(
        self,
        folder: Path,
        species_names,
        config: CloudConfig,
        *,
        rest: SupabaseRest | None = None,
        storage=None,
        render=render_spectrogram,
        encode=encode_mp3,
    ) -> None:
        self.folder = Path(folder)
        self.names = species_names
        self.config = config
        self.rest = rest
        self.storage = storage
        self.render = render
        self.encode = encode

    def _connect(self) -> None:
        if self.rest is None:
            self.rest = SupabaseRest(self.config)
        if self.storage is None:
            self.storage = (
                LocalMediaStorage(self.config.local_media_dir)
                if self.config.media_storage == "local"
                else SupabaseStorage(self.config)
            )

    def push(self, limit: int | None = None) -> PushReport:
        report = PushReport()
        with detections_lock(self.folder, blocking=False) as acquired:
            if not acquired:
                report.busy = True
                return report
            report.converted = convert_legacy_csvs(self.folder, self.names)
            items = outbox_items(self.folder)
            report.pending = len(items)
            if not items:
                return report
            if limit is not None:
                items = items[:limit]
            try:
                self._connect()
                deleted = set(self.rest.select_column("deleted_clips", "id"))
            except Exception as exc:  # offline: everything waits for the next attempt
                report.errors.append(f"Supabase non raggiungibile: {exc}")
                return report
            species_sent: set[str] = set()
            for item in items:
                try:
                    if item.clip_id in deleted:
                        self._remove(item)
                        report.discarded += 1
                        continue
                    self._send(item, species_sent)
                    self._remove(item)
                    report.sent += 1
                    report.sent_ids.append(item.clip_id)
                except Exception as exc:  # it stays in the folder: the next attempt retries it
                    report.errors.append(f"{item.clip_id}: {exc}")
        return report

    def _send(self, item: OutboxItem, species_sent: set[str]) -> None:
        data = item.data
        name = data["scientific_name"]
        if name not in species_sent:
            self.names.ensure(name, data.get("english_name", ""))
            self.rest.upsert("species", [self.names.species_row(name)], on_conflict="scientific_name")
            species_sent.add(name)

        # asked for each clip: the previous one, just sent, may be the one within ten minutes
        observation_id = self.rest.observation_for(name, item.recorded_at)

        media_path = media_path_for(item.clip_id, item.recorded_at)
        with tempfile.TemporaryDirectory() as work:
            mp3 = self.encode(item.wav, Path(work) / "clip.mp3")
            spectrogram = self.render(item.wav, Path(work) / "spectrogram.jpg")
            self.storage.upload(mp3, f"{media_path}.mp3")
            self.storage.upload(spectrogram, f"{media_path}-spectrogram.jpg")
        if not self.storage.exists(f"{media_path}.mp3"):
            raise RuntimeError("l'MP3 non risulta nel bucket: la clip resta sul PC")

        self.rest.upsert(
            "clips",
            [
                {
                    "id": item.clip_id,
                    "observation_id": observation_id,
                    "recorded_at": data["recorded_at"],
                    "confidence": data["confidence"],
                    "duration_s": data.get("duration_s") or round(audio_duration(item.wav), 2),
                    "place_name": data.get("place_name") or "",
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "media_path": media_path,
                    "segments": data.get("segments"),  # where BirdNET heard it, seconds from the clip start
                }
            ],
            on_conflict="id",
        )

    @staticmethod
    def _remove(item: OutboxItem) -> None:
        item.wav.unlink(missing_ok=True)
        item.sidecar.unlink(missing_ok=True)

    def purge_uploaded_media(self, days: float = UPLOADED_MEDIA_DAYS, now: datetime | None = None,
                             limit: int = 200) -> int:
        """Files of observations on iNaturalist for more than `days`: deleted from Storage (they
        live on iNaturalist now). The clip rows stay, marked media_deleted_at: reports, counts and
        rarity are computed from them. Returns how many clips lost their files."""
        self._connect()
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat()
        clips = self.rest.get("clips", {
            "select": "id,media_path,observations!inner(status,uploaded_at)",
            "observations.status": "eq.uploaded",
            "observations.uploaded_at": f"lt.{cutoff}",
            "media_deleted_at": "is.null",
            "limit": str(limit),
        })
        for clip in clips:
            self.storage.remove([f"{clip['media_path']}.mp3", f"{clip['media_path']}-spectrogram.jpg"])
            self.rest.update("clips", {"id": f"eq.{clip['id']}"}, {"media_deleted_at": now.isoformat()})
        return len(clips)


# ---------------------------------------------------------------- previous version

_CLIP_CONFIDENCE = re.compile(r"_(\d+\.\d+)\.wav$", re.IGNORECASE)


def convert_legacy_csvs(folder: Path, species_names) -> int:
    """Clips left by the previous version (day folders with an iNaturalist CSV) become items
    of the transit folder; their CSV and folders go. Returns how many clips were converted."""
    folder = Path(folder)
    converted = 0
    for csv_path in sorted(folder.glob("*/inaturalist_import_*.csv")):
        day_dir = csv_path.parent
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            taxon = (row.get("Nome del taxon") or "").strip()
            description = row.get("Descrizione") or ""
            english = description.split(";", 1)[0].split(",")[0].strip()
            match = re.search(r"audio_files=([^;]*)", description)
            for audio_name in (match.group(1).split(",") if match else []):
                wav = next(day_dir.rglob(audio_name), None) if audio_name else None
                heard_at = parse_timestamp_from_name(audio_name) if audio_name else None
                if wav is None or heard_at is None:
                    continue  # already online: nothing left to send
                confidence = _CLIP_CONFIDENCE.search(audio_name)
                species_names.ensure(taxon, english)
                target = folder / audio_name
                shutil.move(str(wav), target)
                sidecar = {
                    "id": audio_name,
                    "scientific_name": taxon,
                    "english_name": english,
                    "recorded_at": heard_at.astimezone().isoformat(),
                    "confidence": float(confidence.group(1)) if confidence else 0.0,
                    "duration_s": None,
                    "place_name": (row.get("Nome del luogo") or "").strip(),
                    "latitude": _optional_float(row.get("Latitudine / coord y / nord")),
                    "longitude": _optional_float(row.get("Longitudine / coord x / est")),
                }
                target.with_suffix(".json").write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
                converted += 1
        csv_path.unlink()
        for leftover in sorted((path for path in day_dir.rglob("*") if path.is_dir()), reverse=True):
            _rmdir_quietly(leftover)
        _rmdir_quietly(day_dir)
    return converted


def _optional_float(value) -> float | None:
    try:
        return float(value) if str(value or "").strip() else None
    except ValueError:
        return None


def _rmdir_quietly(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
