from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bird_audio_suite import species_links
from bird_audio_suite.species import SpeciesNames


class FakeSession:
    def __init__(self, results):
        self.results = results

    def get(self, url, params=None, timeout=None):
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": self.results})


class SpeciesLinksTests(unittest.TestCase):
    def test_exact_name_or_synonym_never_the_first_result(self) -> None:
        results = [{"id": 1, "name": "Picumnus pica"}, {"id": 2, "name": "Pica pica"}]
        self.assertEqual(species_links.inat_taxon("Pica pica", FakeSession(results)), (2, "Pica pica"))
        synonym = [{"id": 336399, "name": "Coloeus monedula", "matched_term": "Corvus monedula"}]
        self.assertEqual(species_links.inat_taxon("Corvus monedula", FakeSession(synonym)), (336399, "Coloeus monedula"))
        self.assertIsNone(species_links.inat_taxon("Pica pica", FakeSession([{"id": 1, "name": "Picumnus pica"}])))

    def test_ebird_code_also_through_the_name_inaturalist_accepts(self) -> None:
        with patch.object(species_links, "_ebird_codes", {"coloeus monedula": "eurjac", "pica pica": "eurmag1"}), \
                patch.object(species_links, "_found_codes", {}), \
                patch.object(species_links, "inat_taxon", lambda name: (336399, "Coloeus monedula")):
            self.assertEqual(species_links.species_codes("Corvus monedula"),
                             {"ebird_code": "eurjac", "inat_taxon_id": 336399})
        with patch.object(species_links, "_ebird_codes", {}), patch.object(species_links, "_found_codes", {}), \
                patch.object(species_links, "inat_taxon", lambda name: None):
            self.assertEqual(species_links.species_codes("Avis ignota"), {})  # nothing found: nothing overwritten


    def test_codes_found_are_not_asked_again_for_every_clip(self) -> None:
        asked = []

        def taxon(name):
            asked.append(name)
            return None if name == "Avis ignota" else (12716, name)

        with patch.object(species_links, "_ebird_codes", {"turdus merula": "eurbla"}), \
                patch.object(species_links, "_found_codes", {}), patch.object(species_links, "inat_taxon", taxon):
            for _ in range(3):
                self.assertEqual(species_links.species_codes("Turdus merula"), {"ebird_code": "eurbla", "inat_taxon_id": 12716})
                species_links.species_codes("Avis ignota")  # not found (or offline): asked again next time
        self.assertEqual(asked, ["Turdus merula", "Avis ignota", "Avis ignota", "Avis ignota"])


class WikipediaNamesTests(unittest.TestCase):
    def test_a_name_wikipedia_does_not_have_is_asked_once_an_error_again(self) -> None:
        class Page:
            def __init__(self, exists, summary=""):
                self._exists, self.summary = exists, summary

            def exists(self):
                return self._exists

        class Wiki:
            calls = 0
            offline = False

            def page(self, name):
                Wiki.calls += 1
                if Wiki.offline:
                    raise ConnectionError("offline")
                return Page(name == "Turdus merula", "Il <b>merlo</b> (Turdus merula)")

        with tempfile.TemporaryDirectory() as directory:
            names = SpeciesNames(Path(directory) / "names.json")
            names._wiki["it"] = Wiki()
            self.assertEqual(names.lookup("it", "Turdus merula"), "Merlo")
            self.assertIsNone(names.lookup("it", "Avis ignota"))
            self.assertIsNone(names.lookup("it", "Avis ignota"))  # not asked again
            self.assertEqual(Wiki.calls, 2)
            Wiki.offline = True
            self.assertIsNone(names.lookup("it", "Pica pica"))
            self.assertIsNone(names.lookup("it", "Pica pica"))  # offline is not an answer: asked again
            self.assertEqual(Wiki.calls, 4)

    def test_an_empty_name_on_supabase_does_not_erase_the_cached_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            names = SpeciesNames(Path(directory) / "names.json", lookup_online=False)
            names.names["Turdus merula"] = {"english_name": "Blackbird", "italian_name": "Merlo", "german_name": ""}
            names.refresh_from([{"scientific_name": "Turdus merula", "english_name": "Eurasian Blackbird",
                                 "italian_name": "", "german_name": "Amsel"}])
            self.assertEqual(names.names["Turdus merula"],
                             {"english_name": "Eurasian Blackbird", "italian_name": "Merlo", "german_name": "Amsel"})

if __name__ == "__main__":
    unittest.main()
