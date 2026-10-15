from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from bird_audio_suite import species_links


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
                patch.object(species_links, "inat_taxon", lambda name: (336399, "Coloeus monedula")):
            self.assertEqual(species_links.species_codes("Corvus monedula"),
                             {"ebird_code": "eurjac", "inat_taxon_id": 336399})
        with patch.object(species_links, "_ebird_codes", {}), patch.object(species_links, "inat_taxon", lambda name: None):
            self.assertEqual(species_links.species_codes("Avis ignota"), {})  # nothing found: nothing overwritten


if __name__ == "__main__":
    unittest.main()
