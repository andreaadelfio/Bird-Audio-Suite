from __future__ import annotations

from pathlib import Path
from typing import Iterable

import wikipediaapi


class SpeciesCatalog:
    """Persistent cache for scientific, English, Italian, and German bird names."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.names: dict[str, tuple[str, str, str]] = {}
        self._wiki_it = wikipediaapi.Wikipedia(
            language="it",
            extract_format=wikipediaapi.ExtractFormat.HTML,
            user_agent="bird-audio-suite",
        )
        self._wiki_de = wikipediaapi.Wikipedia(
            language="de",
            extract_format=wikipediaapi.ExtractFormat.HTML,
            user_agent="bird-audio-suite",
        )
        self.load()

    def load(self) -> None:
        self.names = {}
        if not self.path.exists():
            self.path.touch()
            return

        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) == 3:
                    scientific_name, english_name, italian_name = parts
                    german_name = ""
                elif len(parts) == 4:
                    scientific_name, english_name, italian_name, german_name = parts
                else:
                    continue
                self.names[scientific_name] = (italian_name, german_name, english_name)

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            for scientific_name in sorted(self.names):
                italian_name, german_name, english_name = self.names[scientific_name]
                handle.write(
                    f"{scientific_name}|{english_name}|{italian_name}|{german_name}\n"
                )

    def ensure_species(self, detections: Iterable[dict]) -> None:
        updated = False
        for detection in detections:
            scientific_name = detection.get("scientific_name", "").strip()
            english_name = detection.get("common_name", "").strip()
            if not scientific_name:
                continue

            if scientific_name in self.names:
                italian_name, german_name, stored_english = self.names[scientific_name]
                if not german_name:
                    german_name = self.lookup_german_name(scientific_name) or ""
                if english_name and english_name != stored_english:
                    self.names[scientific_name] = (italian_name, german_name, english_name)
                    updated = True
                elif german_name != self.names[scientific_name][1]:
                    self.names[scientific_name] = (italian_name, german_name, stored_english)
                    updated = True
                continue

            italian_name = self.lookup_italian_name(scientific_name) or english_name or scientific_name
            german_name = self.lookup_german_name(scientific_name) or english_name or scientific_name
            self.names[scientific_name] = (italian_name, german_name, english_name)
            updated = True

        if updated:
            self.save()

    def lookup_italian_name(self, scientific_name: str) -> str | None:
        return self._lookup_localized_name(self._wiki_it, scientific_name)

    def lookup_german_name(self, scientific_name: str) -> str | None:
        return self._lookup_localized_name(self._wiki_de, scientific_name)

    @staticmethod
    def _lookup_localized_name(wiki: wikipediaapi.Wikipedia, scientific_name: str) -> str | None:
        page = wiki.page(scientific_name)
        if not page.exists():
            return None

        summary = page.summary or ""
        if "<b>" in summary:
            try:
                return summary.split("<b>", 1)[1].split("<", 1)[0].title()
            except (IndexError, ValueError):
                return None
        return None

    def display_names(
        self, scientific_name: str, fallback_english: str = ""
    ) -> tuple[str, str, str]:
        italian_name, german_name, english_name = self.names.get(
            scientific_name,
            (
                fallback_english or scientific_name,
                fallback_english or scientific_name,
                fallback_english,
            ),
        )
        if not english_name:
            english_name = fallback_english
        if not italian_name:
            italian_name = fallback_english or scientific_name
        if not german_name:
            german_name = fallback_english or scientific_name
        return italian_name, german_name, english_name
