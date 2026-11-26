"""Report -> "Scarica" (docs/assets/report-csv.js): the period's observations as a CSV, provato con Node.

Il file si apre con un doppio clic in Excel o LibreOffice in italiano: ";" tra i campi, virgola
decimale, UTF-8 con BOM (gli accenti), e nessuna cella che un foglio di calcolo esegua come formula
(il database e' scrivibile da chi ha il link del sito).
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPORT_CSV = Path(__file__).resolve().parent.parent / "docs" / "assets" / "report-csv.js"
NODE = shutil.which("node")

RUNNER = """
const source = require("fs").readFileSync(process.argv[1], "utf8");
import("data:text/javascript;base64," + Buffer.from(source).toString("base64")).then((r) => {
  const rows = JSON.parse(process.argv[2]);
  const details = new Map(JSON.parse(process.argv[3]));
  process.stdout.write(JSON.stringify(r.observationsCsv(rows, details)));
});
"""

ROWS = [
    {"observation_id": 7, "day": "2026-11-19", "scientific_name": "Tyto alba", "birdnet_scientific_name": "Tyto alba",
     "italian_name": "Barbagianni Comune", "english_name": "Barn Owl", "status": "pending", "max_confidence": 0.9712,
     "clips": 1, "seconds": 10.0, "first_heard": "2026-11-19T09:59:08+00:00"},
    {"observation_id": 3, "day": "2026-10-16", "scientific_name": "Vulpes vulpes", "birdnet_scientific_name": "Grus grus",
     "italian_name": "Volpe Rossa", "english_name": "Red Fox", "status": "uploaded", "max_confidence": None,
     "clips": 2, "seconds": 41.5, "first_heard": "2026-10-16T05:40:00+00:00"},
]
DETAILS = [
    [7, {"place_name": "Basaldella; Friuli", "latitude": 46.061848, "longitude": 13.241912}],
    [3, {"place_name": '=HYPERLINK("http://x")', "latitude": 45.5, "longitude": -0.25, "inat_observation_id": 321}],
]


@unittest.skipUnless(NODE, "serve Node.js")
class ReportCsvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run([NODE, "-e", RUNNER, str(REPORT_CSV), json.dumps(ROWS), json.dumps(DETAILS)],
                              capture_output=True, text=True, timeout=60, env={"TZ": "Europe/Rome", "PATH": "/usr/bin:/bin"})
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.text = json.loads(done.stdout)
        cls.table = list(csv.reader(io.StringIO(cls.text.lstrip("﻿")), delimiter=";"))

    def test_excel_in_italian_opens_it_as_it_is(self) -> None:
        self.assertTrue(self.text.startswith("﻿Data;Ora;Specie;"))
        self.assertTrue(self.text.endswith("\r\n"))
        self.assertEqual(len({len(row) for row in self.table}), 1)  # the same columns on every row

    def test_one_row_per_observation_oldest_first_in_local_time(self) -> None:
        head, fox, owl = self.table
        self.assertEqual((fox[0], fox[1], owl[0], owl[1]), ("2026-10-16", "07:40", "2026-11-19", "10:59"))
        self.assertEqual(owl[head.index("Stato")], "da rivedere")
        self.assertEqual(fox[head.index("Stato")], "su iNaturalist")

    def test_numbers_with_a_decimal_comma(self) -> None:
        head, fox, owl = self.table
        self.assertEqual(owl[head.index("Confidenza max")], "0,971")
        self.assertEqual(fox[head.index("Confidenza max")], "")  # a corrected species has no BirdNET score
        self.assertEqual((owl[head.index("Latitudine")], fox[head.index("Longitudine")]), ("46,06185", "-0,25000"))
        self.assertEqual(fox[head.index("Secondi di audio")], "41,5")

    def test_a_correction_keeps_what_birdnet_heard_and_the_link_to_inaturalist(self) -> None:
        head, fox, owl = self.table
        self.assertEqual(fox[head.index("BirdNET aveva sentito")], "Grus grus")
        self.assertEqual(owl[head.index("BirdNET aveva sentito")], "")
        self.assertEqual(fox[head.index("iNaturalist")], "https://www.inaturalist.org/observations/321")

    def test_a_semicolon_stays_in_its_cell_and_no_cell_is_a_formula(self) -> None:
        head, fox, owl = self.table
        self.assertEqual(owl[head.index("Luogo")], "Basaldella; Friuli")
        self.assertEqual(fox[head.index("Luogo")], "'=HYPERLINK(\"http://x\")")


if __name__ == "__main__":
    unittest.main()
