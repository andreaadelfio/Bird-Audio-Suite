"""Registra (docs/assets/registra.js): i calcoli del sito provati con Node contro quelli del PC.

La clip che il browser scrive (le righe dell'osservazione e della clip, il WAV in inbox/ del bucket)
deve essere quella che scrive il PC: stesso nome, stesso media_path, stesse bande, ora leggibile, WAV
leggibile; poi complete_site_clips le fa MP3, spettrogramma e analisi come a una clip del live.
"""

from __future__ import annotations

import base64
import calendar
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile

from bird_audio_suite.audio import merge_segments, sanitize_filename
from bird_audio_suite.cloud import LocalMediaStorage, media_path_for, slugify_public_id

REGISTRA = Path(__file__).resolve().parent.parent / "docs" / "assets" / "registra.js"
NODE = shutil.which("node")

# il modulo importato da un data: URL (Node prende i .js per CommonJS); scrive i risultati in JSON
RUNNER = """
const source = require("fs").readFileSync(process.argv[1], "utf8");
import("data:text/javascript;base64," + Buffer.from(source).toString("base64")).then((r) => {
  const out = {};
  out.resampled = r.resample(new Float32Array(44100).fill(0.5), 44100, 48000).length;
  out.same = r.resample(new Float32Array(10), 48000, 48000).length;
  out.weeks = ["2026-01-01", "2026-03-15", "2026-11-03", "2026-12-31", "2028-12-31"].map((day) => {
    const [y, m, d] = day.split("-").map(Number);
    return r.week48(new Date(y, m - 1, d, 12));
  });
  out.names = ["Gallinella d'acqua", "  Picchio rosso maggiore ", "Cincia/mora?", "...", "Pettirosso éè"].map(r.safeFileName);
  out.iso = r.isoWithOffset(new Date(2026, 10, 3, 7, 15, 0));
  out.ranges = [r.clipRange(20, 23, 60), r.clipRange(1, 4, 60), r.clipRange(55, 58, 60), r.clipRange(2, 50, 60)];
  out.slugs = ["Gallinella d'acqua_20261103_071500_0.912", "Picchio rosso maggiore", "Cincia mora (giovane)"].map(r.slugify);
  out.mediaPath = r.mediaPathFor("Merlo_20261103_071509_0.910.wav", new Date(2026, 10, 3, 7, 15, 9));
  out.merged = r.mergeSpans([[4.5, 7.5, 0.62], [3, 6, 0.91], [9, 12, 0.7], [7.53, 8, 0.5]]);
  // una specie sentita in due finestre, in una registrazione di 30 s a 48 kHz (un'onda a 1 kHz)
  const rate = 48000;
  const samples = new Float32Array(rate * 30).map((_, i) => 0.5 * Math.sin(2 * Math.PI * 1000 * i / rate));
  const detections = r.addWindow(new Map(), [["Turdus merula_Eurasian Blackbird", 0.91], ["Parus major_Great Tit", 0.65],
    ["Erithacus rubecula_European Robin", 0.2]], 12, 15, (sci, english) => sci === "Turdus merula" ? "Merlo" : english);
  r.addWindow(detections, [["Turdus merula_Eurasian Blackbird", 0.62]], 13.5, 16.5, () => "Merlo");
  const started = new Date(2026, 10, 3, 7, 15, 0);
  const clip = r.clipFor(detections.get("Turdus merula"), detections, samples, rate,
    (seconds) => new Date(started.getTime() + seconds * 1000), "confirmed", { latitude: 52.5, longitude: 13.4 });
  out.listed = [...detections.values()].map((row) => [row.scientific, row.choice, row.windows.length]);
  out.clip = { id: clip.id, observation: clip.observation, row: clip.clip, wav: Buffer.from(clip.wav).toString("base64") };
  process.stdout.write(JSON.stringify(out));
});
"""


@unittest.skipUnless(NODE, "serve Node.js")
class RegistraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run([NODE, "-e", RUNNER, str(REGISTRA)], capture_output=True, text=True, timeout=60,
                              env={"TZ": "Europe/Rome", "PATH": "/usr/bin:/bin"})
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout)

    def test_audio_goes_to_birdnet_at_48_khz(self) -> None:
        self.assertEqual((self.out["resampled"], self.out["same"]), (48000, 10))

    def test_the_week_is_birdnets_as_birdnetlib_computes_it(self) -> None:
        def week48(day: str) -> int:  # birdnetlib.utils.return_week_48_from_datetime
            date = datetime.fromisoformat(day)
            return math.ceil(date.timetuple().tm_yday / (366 if calendar.isleap(date.year) else 365) * 48)

        self.assertEqual(self.out["weeks"], [week48(d) for d in ("2026-01-01", "2026-03-15", "2026-11-03",
                                                                 "2026-12-31", "2028-12-31")])

    def test_file_names_as_the_pc_makes_them(self) -> None:
        names = ["Gallinella d'acqua", "  Picchio rosso maggiore ", "Cincia/mora?", "...", "Pettirosso éè"]
        self.assertEqual(self.out["names"], [sanitize_filename(name) for name in names])

    def test_the_time_carries_its_offset_like_the_live(self) -> None:
        self.assertEqual(self.out["iso"], "2026-11-03T07:15:00+01:00")
        self.assertEqual(datetime.fromisoformat(self.out["iso"]).utcoffset().total_seconds(), 3600)

    def test_a_clip_starts_3_s_before_and_lasts_at_least_10_s(self) -> None:
        self.assertEqual(self.out["ranges"], [[17, 27], [0, 10], [50, 60], [0, 50]])

    def test_nothing_is_chosen_until_you_choose(self) -> None:
        # the robin at 0.2 is not even listed; nothing leaves unless you choose it
        self.assertEqual(self.out["listed"], [["Turdus merula", "", 2], ["Parus major", "", 1]])

    def test_slug_and_media_path_as_the_pc_makes_them(self) -> None:
        names = ["Gallinella d'acqua_20261103_071500_0.912", "Picchio rosso maggiore", "Cincia mora (giovane)"]
        self.assertEqual(self.out["slugs"], [slugify_public_id(name) for name in names])
        self.assertEqual(self.out["mediaPath"], media_path_for("Merlo_20261103_071509_0.910.wav",
                                                               datetime(2026, 11, 3, 7, 15, 9)))

    def test_bands_are_merged_as_the_pc_merges_them(self) -> None:
        spans = [(4.5, 7.5, 0.62), (3, 6, 0.91), (9, 12, 0.7), (7.53, 8, 0.5)]
        self.assertEqual([tuple(span) for span in self.out["merged"]], merge_segments(spans))

    def test_the_clip_of_the_site_has_the_rows_of_one_of_the_live_and_the_pc_completes_it(self) -> None:
        clip, row = self.out["clip"], self.out["clip"]["row"]
        self.assertEqual(clip["id"], "Merlo_20261103_071509_0.910.wav")  # 3 s before the first window (12 s)
        self.assertEqual(clip["observation"], {"day": "2026-11-03", "scientific_name": "Turdus merula",
                                               "status": "confirmed"})
        self.assertEqual((row["recorded_at"], row["duration_s"], row["media_path"]),
                         ("2026-11-03T07:15:09+01:00", 10, "20261103/merlo_20261103_071509_0-910"))
        self.assertEqual(row["segments"], [[3, 7.5, 0.91]])  # the two windows from 0.6, merged
        self.assertEqual(row["candidates"], [["Turdus merula", "Eurasian Blackbird", 0.91],
                                             ["Parus major", "Great Tit", 0.65]])

        from test_cloud_sync import FakeRest, outbox

        class Rest(FakeRest):
            updates: list = []

            def get(self, table, params):  # the row the site wrote
                return [{**row, "observations": {"scientific_name": "Turdus merula"}}]

            def update(self, table, filters, values):
                self.updates.append(values)

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            (media / "inbox").mkdir(parents=True)
            (media / "inbox" / clip["id"]).write_bytes(base64.b64decode(clip["wav"]))
            samples, rate = soundfile.read(str(media / "inbox" / clip["id"]), dtype="float32")
            rest = Rest()
            box = outbox(Path(directory) / "detections", rest, LocalMediaStorage(media))
            self.assertEqual(box.complete_site_clips(), 1)
            made = sorted(path.name for path in (media / "20261103").iterdir())

        self.assertEqual((rate, len(samples)), (48000, 480000))  # 10 s, as written by the browser
        self.assertAlmostEqual(float(np.abs(samples).max()), 0.5, places=3)
        self.assertEqual(made, ["merlo_20261103_071509_0-910-spectrogram.jpg", "merlo_20261103_071509_0-910.mp3"])
        self.assertIn("candidates", rest.updates[0])


if __name__ == "__main__":
    unittest.main()
