from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from pyinaturalist import create_observation as pyinat_create_observation
from pyinaturalist import update_observation as pyinat_update_observation


DEFAULT_API_BASE_URL = "https://api.inaturalist.org/v2"
DEFAULT_TOKEN_ENV_VAR = "INATURALIST_JWT"


@dataclass
class ImportRow:
    taxon_name: str
    observed_at: str
    description: str
    place_name: str
    latitude: str
    longitude: str
    tags: str
    geoprivacy: str
    audio_files: list[str]
    audio_paths: list[Path]

    @property
    def key(self) -> str:
        return self.taxon_name


@dataclass
class ImportResult:
    action: str
    taxon_name: str
    observation_id: int | None
    observation_uuid: str
    observation_url: str


@dataclass
class ObservationRef:
    observation_id: int | None
    observation_uuid: str


def _parse_audio_files(description: str) -> list[str]:
    for part in description.split("; "):
        if part.startswith("audio_files="):
            values = part.split("=", 1)[1].strip()
            return [item.strip() for item in values.split(",") if item.strip()]
    return []


def _infer_audio_files_from_description(description: str, hour_dir: Path) -> list[str]:
    first_section = description.split(";", 1)[0].strip()
    localized_names = [part.strip() for part in first_section.split(",")]
    if len(localized_names) < 2:
        return []

    italian_name = localized_names[1]
    folder = hour_dir / italian_name
    if not folder.exists() or not folder.is_dir():
        return []

    return sorted(path.name for path in folder.glob("*.wav") if path.is_file())


def _resolve_audio_paths(hour_dir: Path, description: str, audio_files: list[str]) -> list[Path]:
    first_section = description.split(";", 1)[0].strip()
    localized_names = [part.strip() for part in first_section.split(",")]
    if len(localized_names) >= 2:
        italian_name = localized_names[1]
        folder = hour_dir / italian_name
        if folder.exists() and folder.is_dir():
            if audio_files:
                resolved = [folder / audio_name for audio_name in audio_files]
                return [path for path in resolved if path.exists() and path.is_file()]
            return sorted(path for path in folder.glob("*.wav") if path.is_file())

    resolved: list[Path] = []
    for audio_name in audio_files:
        matches = list(hour_dir.rglob(audio_name))
        if matches:
            resolved.append(matches[0])
    return resolved


def load_csv_rows(csv_path: Path) -> list[ImportRow]:
    rows: list[ImportRow] = []
    csv_path = Path(csv_path)
    hour_dir = csv_path.parent
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            taxon_name = (row.get("Nome del taxon") or "").strip()
            if not taxon_name:
                continue
            description = (row.get("Descrizione") or "").strip()
            audio_files = _parse_audio_files(description)
            if not audio_files:
                audio_files = _infer_audio_files_from_description(description, hour_dir)
            audio_paths = _resolve_audio_paths(hour_dir, description, audio_files)
            rows.append(
                ImportRow(
                    taxon_name=taxon_name,
                    observed_at=(row.get("Data osservazione") or "").strip(),
                    description=description,
                    place_name=(row.get("Nome del luogo") or "").strip(),
                    latitude=(row.get("Latitudine / coord y / nord") or "").strip(),
                    longitude=(row.get("Longitudine / coord x / est") or "").strip(),
                    tags=(row.get("Etichette") or "").strip(),
                    geoprivacy=(row.get("Geoprivacy") or "").strip(),
                    audio_files=audio_files,
                    audio_paths=audio_paths,
                )
            )
    return rows


def _load_state(state_path: Path) -> dict[str, dict]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state_path: Path, state: dict[str, dict]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


class INaturalistClient:
    def __init__(self, jwt_token: str, base_url: str = DEFAULT_API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        self._taxon_id_cache: dict[str, int] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": jwt_token if jwt_token.startswith("Bearer ") else f"Bearer {jwt_token}",
                "Accept": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.session.request(method, f"{self.base_url}{path}", timeout=60, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"iNaturalist API error {response.status_code}: {response.text}")
        if not response.text.strip():
            return {}
        return response.json()

    def resolve_taxon_id(self, taxon_name: str) -> int:
        cached = self._taxon_id_cache.get(taxon_name)
        if cached is not None:
            return cached

        data = self._request(
            "GET",
            "/taxa/autocomplete",
            params={
                "q": taxon_name,
                "fields": "id,name,matched_term,preferred_common_name",
                "per_page": 10,
            },
        )
        results = data.get("results", [])
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"Unable to resolve iNaturalist taxon for '{taxon_name}'")

        normalized_name = taxon_name.strip().lower()
        exact_match = next(
            (
                item for item in results
                if isinstance(item, dict)
                and str(item.get("name", "")).strip().lower() == normalized_name
            ),
            None,
        )
        chosen = exact_match or results[0]
        taxon_id = int(chosen["id"])
        self._taxon_id_cache[taxon_name] = taxon_id
        return taxon_id

    @staticmethod
    def _build_observation_payload(row: ImportRow, taxon_id: int) -> dict:
        observation = {
            "taxon_id": taxon_id,
            "observed_on_string": row.observed_at,
            "description": row.description,
            "place_guess": row.place_name,
            "tag_list": row.tags,
        }
        if row.latitude:
            observation["latitude"] = float(row.latitude)
        if row.longitude:
            observation["longitude"] = float(row.longitude)
        if row.geoprivacy:
            observation["geoprivacy"] = row.geoprivacy
        return observation

    @staticmethod
    def _extract_pyinat_observation_ref(response: object) -> ObservationRef:
        if isinstance(response, list) and response and isinstance(response[0], dict):
            item = response[0]
            observation_id = int(item["id"]) if item.get("id") is not None else None
            observation_uuid = str(item.get("uuid") or "")
            if observation_uuid:
                return ObservationRef(observation_id=observation_id, observation_uuid=observation_uuid)
        if isinstance(response, dict):
            observation_id = int(response["id"]) if response.get("id") is not None else None
            observation_uuid = str(response.get("uuid") or "")
            if observation_uuid:
                return ObservationRef(observation_id=observation_id, observation_uuid=observation_uuid)
        raise RuntimeError(f"Unable to parse observation reference from pyinaturalist response: {response}")

    def create_observation(self, row: ImportRow, sounds: list[Path] | None = None) -> ObservationRef:
        taxon_id = self.resolve_taxon_id(row.taxon_name)
        payload = self._build_observation_payload(row, taxon_id)
        response = pyinat_create_observation(
            access_token=self.jwt_token,
            sounds=[str(path) for path in (sounds or [])],
            **payload,
        )
        return self._extract_pyinat_observation_ref(response)

    def update_observation(
        self,
        observation_id: int,
        observation_uuid: str,
        row: ImportRow,
        sounds: list[Path] | None = None,
    ) -> ObservationRef:
        taxon_id = self.resolve_taxon_id(row.taxon_name)
        payload = self._build_observation_payload(row, taxon_id)
        response = pyinat_update_observation(
            observation_id,
            access_token=self.jwt_token,
            sounds=[str(path) for path in (sounds or [])],
            **payload,
        )
        observation_ref = self._extract_pyinat_observation_ref(response)
        if not observation_ref.observation_uuid:
            return ObservationRef(observation_id=observation_id, observation_uuid=observation_uuid)
        return observation_ref

    def get_observation(self, observation_uuid: str) -> ObservationRef:
        data = self._request("GET", f"/observations/{observation_uuid}?fields=id,uuid,uri")
        return _extract_observation_ref(data)


def _extract_observation_ref(data: dict) -> ObservationRef:
    if isinstance(data.get("uuid"), str):
        observation_id = int(data["id"]) if data.get("id") is not None else None
        return ObservationRef(observation_id=observation_id, observation_uuid=data["uuid"])
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict) and isinstance(results[0].get("uuid"), str):
        observation_id = int(results[0]["id"]) if results[0].get("id") is not None else None
        return ObservationRef(observation_id=observation_id, observation_uuid=results[0]["uuid"])
    if isinstance(data.get("results"), dict) and isinstance(data["results"].get("uuid"), str):
        observation_id = int(data["results"]["id"]) if data["results"].get("id") is not None else None
        return ObservationRef(observation_id=observation_id, observation_uuid=data["results"]["uuid"])
    raise RuntimeError(f"Unable to parse observation reference from response: {data}")


def _build_observation_url(base_url: str, observation_uuid: str) -> str:
    return f"https://www.inaturalist.org/observations/{observation_uuid}"


def import_csv(
    csv_path: Path,
    *,
    jwt_token: str,
    base_url: str = DEFAULT_API_BASE_URL,
    dry_run: bool = False,
) -> tuple[int, int, list[ImportResult]]:
    csv_path = Path(csv_path)
    hour_dir = csv_path.parent
    state_path = csv_path.with_suffix(".state.json")
    rows = load_csv_rows(csv_path)
    state = _load_state(state_path)
    imported_count = 0
    updated_count = 0
    results: list[ImportResult] = []

    client = None if dry_run else INaturalistClient(jwt_token=jwt_token, base_url=base_url)

    for row in rows:
        state_entry = state.get(row.key)
        existing_uuid = state_entry.get("observation_uuid") if state_entry else None
        existing_id = state_entry.get("observation_id") if state_entry else None
        if existing_uuid == "dry-run-observation":
            existing_uuid = None

        if existing_uuid:
            observation_uuid = existing_uuid
            observation_id = int(existing_id) if existing_id is not None else None
            pending_audio_paths: list[Path] = []
            uploaded_audio_files = set(state_entry.get("uploaded_audio_files", []))
            for audio_path in row.audio_paths:
                audio_key = str(audio_path.relative_to(hour_dir))
                if audio_key not in uploaded_audio_files:
                    pending_audio_paths.append(audio_path)
            if dry_run:
                print(f"[dry-run] Update observation {observation_uuid} for {row.taxon_name}")
            else:
                if observation_id is None:
                    observation_id = client.get_observation(observation_uuid).observation_id
                if observation_id is None:
                    raise RuntimeError(f"Missing numeric observation ID for {row.taxon_name} ({observation_uuid})")
                observation_ref = client.update_observation(
                    observation_id,
                    observation_uuid,
                    row,
                    sounds=pending_audio_paths,
                )
                observation_uuid = observation_ref.observation_uuid or observation_uuid
                observation_id = observation_ref.observation_id
                if observation_id is None:
                    observation_id = client.get_observation(observation_uuid).observation_id
            updated_count += 1
            action = "updated"
        else:
            pending_audio_paths = list(row.audio_paths)
            if dry_run:
                observation_id = None
                observation_uuid = "dry-run-observation"
                print(f"[dry-run] Create observation for {row.taxon_name} -> {observation_uuid}")
            else:
                observation_ref = client.create_observation(row, sounds=pending_audio_paths)
                observation_uuid = observation_ref.observation_uuid
                observation_id = observation_ref.observation_id
            uploaded_audio_files = set()
            imported_count += 1
            action = "created"

        for audio_path in row.audio_paths:
            audio_key = str(audio_path.relative_to(hour_dir))
            if dry_run and audio_key not in uploaded_audio_files:
                print(f"[dry-run] Attach {audio_path.name} to {row.taxon_name}")
            if (not dry_run and audio_path in pending_audio_paths) or audio_key in uploaded_audio_files:
                uploaded_audio_files.add(audio_key)

        if not dry_run:
            state[row.key] = {
                "observation_id": observation_id,
                "observation_uuid": observation_uuid,
                "uploaded_audio_files": sorted(uploaded_audio_files),
                "observed_at": row.observed_at,
                "description": row.description,
            }
        results.append(
            ImportResult(
                action=action,
                taxon_name=row.taxon_name,
                observation_id=observation_id,
                observation_uuid=observation_uuid,
                observation_url=_build_observation_url(base_url, observation_uuid),
            )
        )

    if not dry_run:
        _save_state(state_path, state)
    return imported_count, updated_count, results


def load_jwt_from_env(env_var: str = DEFAULT_TOKEN_ENV_VAR) -> str:
    token = os.getenv(env_var, "").strip()
    if not token:
        raise RuntimeError(
            f"Missing iNaturalist token. Set the JWT in the environment variable {env_var}."
        )
    return token


def resolve_jwt_token(explicit_token: str | None = None, env_var: str = DEFAULT_TOKEN_ENV_VAR) -> str:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    return load_jwt_from_env(env_var)
