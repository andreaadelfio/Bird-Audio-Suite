"""Codes that link a species to eBird (and its Macaulay Library sounds) and to iNaturalist.

eBird: the species code (e.g. ``eurnut2``) from the public eBird taxonomy, downloaded once and
kept in ~/.cache/bird-audio-suite (refreshed after six months). iNaturalist: the taxon id, from
its API, matching the exact name or a synonym it lists (Corvus monedula -> Coloeus monedula),
never just the first result. Both are optional: offline, or for a name a source does not know,
the value is simply missing and nothing already stored is overwritten.
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import requests

CACHE_DIR = Path.home() / ".cache" / "bird-audio-suite"
EBIRD_TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird?fmt=csv&cat=species"
EBIRD_TAXONOMY_FILE = CACHE_DIR / "ebird_taxonomy.csv"
EBIRD_MAX_AGE_SECONDS = 180 * 24 * 3600
INAT_TAXA_URL = "https://api.inaturalist.org/v1/taxa"

_ebird_codes: dict[str, str] | None = None
_found_codes: dict[str, dict] = {}  # species with both codes: the uploader sends one row per clip


def _load_ebird_taxonomy() -> dict[str, str]:
    fresh = EBIRD_TAXONOMY_FILE.exists() and time.time() - EBIRD_TAXONOMY_FILE.stat().st_mtime < EBIRD_MAX_AGE_SECONDS
    if not fresh:
        try:
            response = requests.get(EBIRD_TAXONOMY_URL, timeout=60)
            response.raise_for_status()
            EBIRD_TAXONOMY_FILE.parent.mkdir(parents=True, exist_ok=True)
            EBIRD_TAXONOMY_FILE.write_text(response.text, encoding="utf-8")
        except (OSError, requests.RequestException):
            pass  # an older copy is still better than nothing
    try:
        text = EBIRD_TAXONOMY_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {row["SCIENTIFIC_NAME"].strip().lower(): row["SPECIES_CODE"].strip()
            for row in csv.DictReader(io.StringIO(text)) if row.get("SPECIES_CODE")}


def ebird_code(scientific_name: str) -> str | None:
    global _ebird_codes
    if _ebird_codes is None:
        _ebird_codes = _load_ebird_taxonomy()
    return _ebird_codes.get(scientific_name.strip().lower())


def inat_taxon(scientific_name: str, session=requests) -> tuple[int, str] | None:
    """(taxon id, the name iNaturalist accepts), matching the exact name or a synonym."""
    name = scientific_name.strip()
    try:
        response = session.get(INAT_TAXA_URL, params={"q": name, "rank": "species,subspecies", "per_page": 30},
                               timeout=30)
        response.raise_for_status()
        results = [item for item in response.json().get("results", []) if item.get("is_active", True) is not False]
    except (requests.RequestException, ValueError):
        return None
    wanted = name.lower()
    for match in (lambda item: item.get("name", "").lower() == wanted,
                  lambda item: str(item.get("matched_term", "")).lower() == wanted):
        found = next((item for item in results if match(item)), None)
        if found:
            return int(found["id"]), found.get("name", name)
    return None


def species_codes(scientific_name: str) -> dict:
    """The codes found for a species (missing ones left out, so they never overwrite). eBird is
    also tried with the name iNaturalist accepts: BirdNET's Corvus monedula is eBird's Coloeus.
    Once both are found they are remembered while the process runs: no request to iNaturalist
    for every clip of a blackbird."""
    if scientific_name in _found_codes:
        return dict(_found_codes[scientific_name])
    taxon = inat_taxon(scientific_name)
    code = ebird_code(scientific_name) or (ebird_code(taxon[1]) if taxon else None)
    codes = {key: value for key, value in {"ebird_code": code, "inat_taxon_id": taxon[0] if taxon else None}.items()
             if value is not None}
    if len(codes) == 2:
        _found_codes[scientific_name] = dict(codes)
    return codes
