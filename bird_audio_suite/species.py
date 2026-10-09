from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import wikipediaapi


CSV_HEADER = [
    "scientific_name",
    "english_name",
    "italian_name",
    "german_name",
    "first_seen_date",
    "last_seen_date",
    "observed_days",
    "total_count",
    "daily_average",
    "last_confidence",
    "rarity_score",
    "rarity",
]


@dataclass
class SpeciesRecord:
    scientific_name: str
    english_name: str
    italian_name: str
    german_name: str
    first_seen_date: str = ""
    last_seen_date: str = ""
    observed_days: int = 0
    total_count: int = 0
    daily_average: float = 0.0
    last_confidence: float = 0.0
    rarity_score: float = 0.0
    rarity: str = ""




class SpeciesCatalog:
    """Persistent CSV catalog for bird names and long-term detection stats."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, SpeciesRecord] = {}
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
        self.records = {}
        if not self.path.exists():
            self.path.touch()
            return

        raw_text = self.path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return

        if raw_text.splitlines()[0].startswith("scientific_name,"):
            self._load_csv_format()
        else:
            self._load_legacy_format(raw_text.splitlines())

    def _load_csv_format(self) -> None:
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                scientific_name = (row.get("scientific_name") or "").strip()
                if not scientific_name:
                    continue
                first_seen_date = (row.get("first_seen_date") or "").strip()
                last_seen_date = (row.get("last_seen_date") or "").strip()
                legacy_catalog_date = (row.get("catalog_date") or "").strip()
                legacy_daily_count = int(float(row.get("daily_count") or 0))
                total_count = int(float(row.get("total_count") or legacy_daily_count or 0))
                if not first_seen_date and total_count > 0:
                    first_seen_date = legacy_catalog_date or date.today().isoformat()
                if not last_seen_date and total_count > 0:
                    last_seen_date = legacy_catalog_date or first_seen_date
                record = SpeciesRecord(
                    scientific_name=scientific_name,
                    english_name=(row.get("english_name") or "").strip(),
                    italian_name=(row.get("italian_name") or "").strip(),
                    german_name=(row.get("german_name") or "").strip(),
                    first_seen_date=first_seen_date,
                    last_seen_date=last_seen_date,
                    observed_days=int(float(row.get("observed_days") or (1 if total_count > 0 else 0))),
                    total_count=total_count,
                    daily_average=float(row.get("daily_average") or 0.0),
                    last_confidence=float(row.get("last_confidence") or 0.0),
                    rarity_score=float(row.get("rarity_score") or 0.0),
                    rarity=(row.get("rarity") or "").strip(),
                )
                self.records[scientific_name] = record
        self._refresh_all_frequency_stats()

    def _load_legacy_format(self, lines: list[str]) -> None:
        for raw_line in lines:
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
            self.records[scientific_name] = SpeciesRecord(
                scientific_name=scientific_name,
                english_name=english_name,
                italian_name=italian_name,
                german_name=german_name,
            )

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
            writer.writeheader()
            for record in sorted(
                self.records.values(),
                key=lambda item: (
                    item.total_count > 0,
                    item.total_count,
                    item.scientific_name,
                ),
                reverse=True,
            ):
                writer.writerow(
                    {
                        "scientific_name": record.scientific_name,
                        "english_name": record.english_name,
                        "italian_name": record.italian_name,
                        "german_name": record.german_name,
                        "first_seen_date": record.first_seen_date,
                        "last_seen_date": record.last_seen_date,
                        "observed_days": record.observed_days,
                        "total_count": record.total_count,
                        "daily_average": f"{record.daily_average:.3f}",
                        "last_confidence": f"{record.last_confidence:.3f}",
                        "rarity_score": f"{record.rarity_score:.3f}",
                        "rarity": record.rarity,
                    }
                )

    def ensure_species(self, detections: Iterable[dict], observed_at: datetime | None = None) -> bool:
        observed_at = observed_at or datetime.now()
        observed_day = observed_at.date().isoformat()
        updated = False

        for detection in detections:
            scientific_name = detection.get("scientific_name", "").strip()
            english_name = detection.get("common_name", "").strip()
            if not scientific_name:
                continue

            record = self.records.get(scientific_name)
            if record is None:
                record = SpeciesRecord(
                    scientific_name=scientific_name,
                    english_name=english_name,
                    italian_name=self.lookup_italian_name(scientific_name) or english_name or scientific_name,
                    german_name=self.lookup_german_name(scientific_name) or english_name or scientific_name,
                    first_seen_date=observed_day,
                    last_seen_date=observed_day,
                )
                self.records[scientific_name] = record
                updated = True
            else:
                if english_name and english_name != record.english_name:
                    record.english_name = english_name
                    updated = True
                if not record.italian_name:
                    record.italian_name = self.lookup_italian_name(scientific_name) or english_name or scientific_name
                    updated = True
                if not record.german_name:
                    record.german_name = self.lookup_german_name(scientific_name) or english_name or scientific_name
                    updated = True

            confidence = float(detection.get("confidence", 0.0) or 0.0)

            # Dates are ISO strings, so they compare chronologically. Batch runs can feed
            # old recordings out of order: first/last seen must only ever widen.
            if record.total_count <= 0 or not record.first_seen_date or not record.last_seen_date:
                record.first_seen_date = observed_day
                record.last_seen_date = observed_day
                record.observed_days = 1
            elif observed_day > record.last_seen_date:
                record.last_seen_date = observed_day
                record.observed_days += 1
            elif observed_day < record.first_seen_date:
                record.first_seen_date = observed_day
                record.observed_days += 1
            record.total_count += 1
            record.last_confidence = max(record.last_confidence, confidence)

            updated = True

        if updated:
            self._refresh_all_frequency_stats()
            self.save()
        return updated

    def _monitoring_span_days(self) -> int:
        """Days between the first and the last detection of any species."""
        seen = [record for record in self.records.values() if record.total_count > 0]
        first_dates = [record.first_seen_date for record in seen if record.first_seen_date]
        last_dates = [record.last_seen_date or record.first_seen_date for record in seen if record.first_seen_date]
        if not first_dates:
            return 1
        return self._date_span_days(min(first_dates), max(last_dates))

    def _refresh_all_frequency_stats(self) -> None:
        span_days = self._monitoring_span_days()
        for record in self.records.values():
            self._refresh_frequency_stats(record, span_days)

    def _refresh_frequency_stats(self, record: SpeciesRecord, span_days: int | None = None) -> None:
        if record.total_count <= 0:
            record.observed_days = 0
            record.daily_average = 0.0
            record.rarity_score = 0.0
            record.rarity = "unseen"
            return

        # Average over the whole monitoring period, not the species' own first/last
        # span: otherwise a bird heard on a single busy day looks "common" forever.
        if span_days is None:
            span_days = self._monitoring_span_days()
        if record.observed_days <= 0:
            record.observed_days = 1
        record.daily_average = record.total_count / float(span_days)
        record.rarity_score = 1.0 / (1.0 + record.daily_average)
        if record.total_count == 1:
            record.rarity = "unique"
        elif record.daily_average < 0.25:
            record.rarity = "rare"
        elif record.daily_average < 1.0:
            record.rarity = "uncommon"
        else:
            record.rarity = "common"

    @staticmethod
    def _date_span_days(first_seen_date: str, last_seen_date: str) -> int:
        try:
            first_seen = date.fromisoformat(first_seen_date)
            last_seen = date.fromisoformat(last_seen_date or first_seen_date)
        except ValueError:
            return 1
        return max((last_seen - first_seen).days + 1, 1)

    def lookup_italian_name(self, scientific_name: str) -> str | None:
        return self._lookup_localized_name(self._wiki_it, scientific_name)

    def lookup_german_name(self, scientific_name: str) -> str | None:
        return self._lookup_localized_name(self._wiki_de, scientific_name)

    @staticmethod
    def _lookup_localized_name(wiki: wikipediaapi.Wikipedia, scientific_name: str) -> str | None:
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
                return SpeciesCatalog._clean_localized_name(summary.split("<b>", 1)[1].split("<", 1)[0])
            except (IndexError, ValueError):
                return None
        return None

    @staticmethod
    def _clean_localized_name(raw_name: str) -> str | None:
        """Keep the first name of a bold lead such as "rondone, " or "cuculo o"."""
        name = re.split(r"[,;(]", raw_name, maxsplit=1)[0].strip()
        name = re.sub(r"\s+(o|oder|or)$", "", name, flags=re.IGNORECASE).strip(" -:")
        return name.title() or None

    def display_names(
        self, scientific_name: str, fallback_english: str = ""
    ) -> tuple[str, str, str]:
        record = self.records.get(scientific_name)
        if record is None:
            italian_name = fallback_english or scientific_name
            german_name = fallback_english or scientific_name
            english_name = fallback_english
            return italian_name, german_name, english_name

        italian_name = record.italian_name or fallback_english or scientific_name
        german_name = record.german_name or fallback_english or scientific_name
        english_name = record.english_name or fallback_english
        return italian_name, german_name, english_name

    def daily_summary(self) -> list[SpeciesRecord]:
        return sorted(
            (record for record in self.records.values() if record.total_count > 0),
            key=lambda item: (
                item.total_count,
                item.scientific_name,
            ),
            reverse=True,
        )
