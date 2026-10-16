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
from . import config as settings  # read at use: the uploader reloads config.py while running
from .species import birdnet_english_name
from .species_links import species_codes

DEFAULT_CLOUD_CONFIG = Path.home() / ".config" / "bird-audio-suite" / "cloud.env"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "bird-audio-suite"
STORAGE_BUCKET = "bird-audio"
SCHEMA = "birds"
INAT_OBSERVATIONS_URL = "https://api.inaturalist.org/v1/observations"
SPECIES_RANK_LEVEL = 10  # iNaturalist's rank_level of a species (subspecies 5, genus 20)

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

    def observation_for(self, scientific_name: str, recorded_at: datetime, gap: timedelta | None = None) -> int:
        """The observation a clip belongs to: the one with a clip of the same species within `gap`
        of it (the nearest, if more), else a new one dated the clip's day. Review columns are never
        touched; across midnight it works too (it looks at times, not days)."""
        gap = gap or timedelta(minutes=settings.OBSERVATION_GAP_MINUTES)
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

    def delete_if_empty(self, observation_id: int) -> None:
        """An observation left without clips (its clip did not make it): it goes, unless reviewed."""
        if self.get("clips", {"select": "id", "observation_id": f"eq.{observation_id}", "limit": "1"}):
            return
        response = self.session.delete(f"{self.base}/observations",
                                       params={"id": f"eq.{observation_id}", "status": "eq.pending"},
                                       timeout=self.timeout)
        self._check(response, "observations delete")

    def delete_empty_observations(self, older_than: timedelta = timedelta(hours=1)) -> int:
        """Observations still without clips an hour after they were created: leftovers of uploads cut
        halfway (a timeout after the observation, before its clip). The live table and the views
        expect at least one clip."""
        cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
        empty = self.get("observations", [("select", "id,clips!left(id)"), ("clips", "is.null"),
                                          ("status", "eq.pending"), ("created_at", f"lt.{cutoff}")])
        for row in empty:
            self.delete_if_empty(int(row["id"]))
        return len(empty)


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

    def read_bytes(self, object_path: str) -> bytes:
        response = requests.get(self.public_url(object_path), timeout=60)
        response.raise_for_status()
        return response.content

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

    def read_bytes(self, object_path: str) -> bytes:
        return (self.folder / object_path).read_bytes()

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


def clip_analysis(item) -> dict:
    """analysis.py on the clip about to be sent: its confidence, bands and candidates."""
    import soundfile

    from .analysis import analyse_samples

    samples, rate = soundfile.read(str(item.wav), dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    data = item.data
    return analyse_samples(
        samples, rate, data["scientific_name"],
        latitude=data.get("latitude") if data.get("latitude") is not None else 45.8,
        longitude=data.get("longitude") if data.get("longitude") is not None else 13.5,
        when=item.recorded_at,
    )


def analyse_stored_clip(storage, clip: dict, model=None) -> dict:
    """analysis.py on a clip already on Supabase (its MP3), for the species of its observation."""
    import soundfile

    from .analysis import analyse_samples

    with tempfile.NamedTemporaryFile(suffix=".mp3") as handle:
        handle.write(storage.read_bytes(f"{clip['media_path']}.mp3"))
        handle.flush()
        samples, rate = soundfile.read(handle.name, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    return analyse_samples(
        samples, rate, clip["observations"]["scientific_name"],
        latitude=clip["latitude"] if clip.get("latitude") is not None else 45.8,
        longitude=clip["longitude"] if clip.get("longitude") is not None else 13.5,
        when=datetime.fromisoformat(clip["recorded_at"]), model=model,
    )


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
        analyse=None,
        link_codes=species_codes,
    ) -> None:
        self.folder = Path(folder)
        self.names = species_names
        self.config = config
        self.rest = rest
        self.storage = storage
        self.render = render
        self.encode = encode
        self.analyse = analyse or clip_analysis  # confidence, bands and candidates from the saved audio
        self.link_codes = link_codes

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
            # with its eBird and iNaturalist codes (only those found: offline nothing is overwritten)
            self.rest.upsert("species", [{**self.names.species_row(name), **self.link_codes(name)}],
                             on_conflict="scientific_name")
            species_sent.add(name)

        media_path = media_path_for(item.clip_id, item.recorded_at)
        with tempfile.TemporaryDirectory() as work:
            mp3 = self.encode(item.wav, Path(work) / "clip.mp3")
            spectrogram = self.render(item.wav, Path(work) / "spectrogram.jpg")
            self.storage.upload(mp3, f"{media_path}.mp3")
            self.storage.upload(spectrogram, f"{media_path}-spectrogram.jpg")
        if not self.storage.exists(f"{media_path}.mp3"):
            raise RuntimeError("l'MP3 non risulta nel bucket: la clip resta sul PC")
        try:
            analysis = self.analyse(item)
        except Exception:  # never a reason to keep the clip back: the live's numbers then
            analysis = None

        # asked right before the clip row (the previous clip, just sent, may be within ten minutes);
        # if the clip row then fails, a new observation must not stay behind empty
        observation_id = self.rest.observation_for(name, item.recorded_at)
        try:
            self._insert_clip(item, data, observation_id, media_path, analysis)
        except Exception:
            try:
                self.rest.delete_if_empty(observation_id)
            except Exception:
                pass  # the hourly clean-up of the uploader removes it later
            raise

    def _insert_clip(self, item: OutboxItem, data: dict, observation_id: int, media_path: str, analysis) -> None:
        self.rest.upsert(
            "clips",
            [
                {
                    "id": item.clip_id,
                    "observation_id": observation_id,
                    "recorded_at": data["recorded_at"],
                    "confidence": analysis["confidence"] if analysis else data["confidence"],
                    "live_confidence": data["confidence"],  # what the live scored while listening
                    "duration_s": data.get("duration_s") or round(audio_duration(item.wav), 2),
                    "place_name": data.get("place_name") or "",
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "media_path": media_path,
                    # where BirdNET hears it, seconds from the clip start; its three likely species
                    "segments": analysis["segments"] if analysis else data.get("segments"),
                    "candidates": analysis["candidates"] if analysis else None,
                }
            ],
            on_conflict="id",
        )

    @staticmethod
    def _remove(item: OutboxItem) -> None:
        item.wav.unlink(missing_ok=True)
        item.sidecar.unlink(missing_ok=True)

    def publish_settings(self) -> None:
        """The config.py values the site needs too (the "likely" threshold), into
        birds.shared_settings: the site reads them when it opens."""
        self._connect()
        self.rest.upsert("shared_settings", [{"key": "likely_confidence", "value": settings.LIKELY_CONFIDENCE,
                                              "updated_at": datetime.now(timezone.utc).isoformat()}],
                         on_conflict="key")

    def tidy(self) -> tuple[int, int, int, int]:
        """The uploader's hourly housekeeping: files of clips on iNaturalist the site could not
        remove, observations left empty, the species iNaturalist shows for the uploaded ones, and
        names for species added by a correction. Returns how many of each."""
        purged = self.purge_uploaded_media()
        emptied = self.rest.delete_empty_observations()
        followed = self.follow_inat_species()
        return purged, emptied, followed, self.name_new_species()

    def follow_inat_species(self, session=requests) -> int:
        """Uploaded observations take the taxon iNaturalist shows for them (identifiers can correct
        it: a "crane" that is a fox; or you, keeping only the genus when unsure). It becomes the
        observation's correction, which every statistic counts. The same species under another name
        (BirdNET's Corvus monedula is iNaturalist's Coloeus monedula) is recognised by its
        iNaturalist taxon and needs no correction. Returns how many observations changed."""
        self._connect()
        rows = self.rest.get("observations", {
            "select": "id,scientific_name,corrected_scientific_name,inat_observation_id",
            "status": "eq.uploaded", "inat_observation_id": "not.is.null",
        })
        if not rows:
            return 0
        known = self.rest.get("species", {"select": "scientific_name,inat_taxon_id", "inat_taxon_id": "not.is.null"})
        by_taxon = {int(row["inat_taxon_id"]): row["scientific_name"] for row in known}
        taxon_of = {row["scientific_name"]: int(row["inat_taxon_id"]) for row in known}
        changed = 0
        for start in range(0, len(rows), 100):
            part = rows[start:start + 100]
            response = session.get(INAT_OBSERVATIONS_URL, timeout=30, params={
                "id": ",".join(str(row["inat_observation_id"]) for row in part), "per_page": 200})
            response.raise_for_status()
            shown = {int(item["id"]): inat_shown_taxon(item) for item in response.json().get("results", [])}
            for row in part:
                taxon = shown.get(int(row["inat_observation_id"]))
                if taxon is None:
                    continue  # no identification left, or deleted on iNaturalist: as it is
                taxon_id, name = taxon
                wanted = by_taxon.get(taxon_id, name)
                if wanted == row["scientific_name"] or taxon_of.get(row["scientific_name"]) == taxon_id:
                    wanted = None  # what BirdNET heard
                if wanted == row["corrected_scientific_name"]:
                    continue
                if wanted and wanted not in taxon_of:
                    # a taxon new here (the fox): its row, with the link; names come with name_new_species
                    self.rest.upsert("species", [{"scientific_name": wanted, "inat_taxon_id": taxon_id}],
                                     on_conflict="scientific_name")
                    by_taxon[taxon_id], taxon_of[wanted] = wanted, taxon_id
                self.rest.update("observations", {"id": f"eq.{row['id']}"}, {"corrected_scientific_name": wanted})
                changed += 1
        return changed

    def name_new_species(self, limit: int = 20) -> int:
        """Species added by a correction on the site (the database trigger writes only the
        scientific name): English name from BirdNET's labels (or Wikipedia), Italian and German from
        Wikipedia, eBird and iNaturalist codes. Returns how many got their names."""
        self._connect()
        rows = self.rest.get("species", {"select": "scientific_name", "english_name": "eq.", "limit": str(limit)})
        named = 0
        for row in rows:
            name = row["scientific_name"]
            if not any(self.names.ensure(name, birdnet_english_name(name)).values()):
                continue  # offline, or a name nobody knows: next hour
            self.rest.upsert("species", [{**self.names.species_row(name), **self.link_codes(name)}],
                             on_conflict="scientific_name")
            named += 1
        return named

    def purge_uploaded_media(self, now: datetime | None = None, limit: int = 200) -> int:
        """Files of observations on iNaturalist: deleted from Storage (they live on iNaturalist
        now). The site removes them as soon as the upload succeeds; this catches what it could not
        (offline halfway). The clip rows stay, marked media_deleted_at: reports, counts and rarity
        are computed from them. Discarded clips keep their files for the Cestino. Returns how many
        clips lost their files."""
        self._connect()
        now = now or datetime.now(timezone.utc)
        clips = self.rest.get("clips", {
            "select": "id,media_path,observations!inner(status)",
            "observations.status": "eq.uploaded",
            "review": "neq.wrong",
            "media_deleted_at": "is.null",
            "limit": str(limit),
        })
        eligible = {clip["id"] for clip in clips}
        purged = 0
        for clip in clips:
            # a clip linked on the site to another observation shares these files: they stay while
            # any clip using them is not on iNaturalist too
            sharing = self.rest.get("clips", {"select": "id", "media_path": f"eq.{clip['media_path']}",
                                              "media_deleted_at": "is.null", "id": f"neq.{clip['id']}"})
            if any(row["id"] not in eligible for row in sharing):
                continue
            self.storage.remove([f"{clip['media_path']}.mp3", f"{clip['media_path']}-spectrogram.jpg"])
            self.rest.update("clips", {"media_path": f"eq.{clip['media_path']}"}, {"media_deleted_at": now.isoformat()})
            purged += 1
        return purged


def inat_shown_taxon(observation: dict) -> tuple[int, str] | None:
    """(taxon id, name) iNaturalist shows for an observation: the community's when there is one,
    otherwise the observer's. A subspecies counts as its species; a genus stays a genus."""
    taxon = observation.get("taxon") or {}
    if not taxon.get("id") or not taxon.get("name"):
        return None
    if float(taxon.get("rank_level") or SPECIES_RANK_LEVEL) < SPECIES_RANK_LEVEL and taxon.get("min_species_taxon_id"):
        return int(taxon["min_species_taxon_id"]), " ".join(taxon["name"].split()[:2])
    return int(taxon["id"]), taxon["name"]


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
