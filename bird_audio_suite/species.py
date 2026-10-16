"""Species names (English, Italian, German) for the clips and the live table.

Supabase (birds.species) is where the names live. This class keeps a copy in
~/.cache/bird-audio-suite/species_names.json only so the live service can name a clip when it
starts offline, and looks up the Italian and German names of a species never seen before on
Wikipedia. Statistics (counts, days, rarity) are not kept here: they come from the
birds.species_stats view.
"""

from __future__ import annotations

import importlib.util
import json
import re
from functools import lru_cache
from pathlib import Path

import wikipediaapi

DEFAULT_NAMES_CACHE = Path.home() / ".cache" / "bird-audio-suite" / "species_names.json"
NAME_FIELDS = ("english_name", "italian_name", "german_name")
BIRDNET_LABELS = "models/analyzer/BirdNET_GLOBAL_6K_V2.4_Labels.txt"


@lru_cache(maxsize=1)
def _birdnet_labels() -> dict[str, str]:
    # found without importing birdnetlib (that would load TensorFlow Lite)
    spec = importlib.util.find_spec("birdnetlib")
    if spec is None or not spec.origin:
        return {}
    try:
        lines = (Path(spec.origin).parent / BIRDNET_LABELS).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(line.split("_", 1) for line in lines if "_" in line)


def birdnet_english_name(scientific_name: str) -> str:
    """BirdNET's English name of a species ("" when BirdNET does not know it)."""
    return _birdnet_labels().get(scientific_name.strip(), "")


class SpeciesNames:
    def __init__(self, cache_path: Path = DEFAULT_NAMES_CACHE, lookup_online: bool = True) -> None:
        self.cache_path = Path(cache_path)
        self.lookup_online = lookup_online
        self.names: dict[str, dict[str, str]] = {}
        self._wiki: dict[str, wikipediaapi.Wikipedia] = {}
        self._load_cache()

    # ------------------------------------------------------------ cache and Supabase

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.names = {
            name: {field: str(row.get(field, "")) for field in NAME_FIELDS}
            for name, row in data.items()
            if isinstance(row, dict)
        }

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.names, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.replace(self.cache_path)

    def refresh_from(self, rows: list[dict]) -> None:
        """Take the names stored on Supabase (they win over the cache)."""
        for row in rows:
            name = str(row.get("scientific_name") or "").strip()
            if name:
                self.names[name] = {field: str(row.get(field) or "") for field in NAME_FIELDS}
        self.save_cache()

    # ------------------------------------------------------------ names

    def ensure(self, scientific_name: str, english_name: str = "") -> dict[str, str]:
        """Names of a species; a new one is looked up on Wikipedia and remembered."""
        scientific_name = scientific_name.strip()
        current = self.names.get(scientific_name)
        if current and current.get("italian_name") and current.get("german_name"):
            if english_name and not current.get("english_name"):
                current["english_name"] = english_name
                self.save_cache()
            return current
        found = {
            "english_name": english_name or (current or {}).get("english_name") or self.lookup("en", scientific_name) or "",
            "italian_name": (current or {}).get("italian_name") or self.lookup("it", scientific_name) or "",
            "german_name": (current or {}).get("german_name") or self.lookup("de", scientific_name) or "",
        }
        self.names[scientific_name] = found
        self.save_cache()
        return found

    def display_names(self, scientific_name: str, fallback_english: str = "") -> tuple[str, str, str]:
        """(italian, german, english), falling back to English and then to the scientific name."""
        row = self.names.get(scientific_name, {})
        english = row.get("english_name") or fallback_english
        italian = row.get("italian_name") or english or scientific_name
        german = row.get("german_name") or english or scientific_name
        return italian, german, english

    def species_row(self, scientific_name: str) -> dict[str, str]:
        row = self.names.get(scientific_name, {})
        return {"scientific_name": scientific_name, **{field: row.get(field, "") for field in NAME_FIELDS}}

    # ------------------------------------------------------------ Wikipedia

    def lookup(self, language: str, scientific_name: str) -> str | None:
        if not self.lookup_online:
            return None
        if language not in self._wiki:
            self._wiki[language] = wikipediaapi.Wikipedia(
                language=language,
                extract_format=wikipediaapi.ExtractFormat.HTML,
                user_agent="bird-audio-suite",
            )
        return self._lookup_localized_name(self._wiki[language], scientific_name)

    @staticmethod
    def _lookup_localized_name(wiki, scientific_name: str) -> str | None:
        # Network errors must never break detection (in live mode this runs in the audio loop).
        try:
            page = wiki.page(scientific_name)
            if not page.exists():
                return None
            summary = page.summary or ""
        except Exception as exc:
            print(f"Wikipedia lookup failed for {scientific_name}: {exc}")
            return None

        if "<b>" in summary:
            try:
                return SpeciesNames._clean_localized_name(summary.split("<b>", 1)[1].split("<", 1)[0])
            except (IndexError, ValueError):
                return None
        return None

    @staticmethod
    def _clean_localized_name(raw_name: str) -> str | None:
        """Keep the first name of a bold lead such as "rondone, " or "cuculo o"."""
        name = re.split(r"[,;(]", raw_name, maxsplit=1)[0].strip()
        name = re.sub(r"\s+(o|oder|or)$", "", name, flags=re.IGNORECASE).strip(" -:")
        return name.title() or None
