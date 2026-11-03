"""Registra (docs/assets/registra.js): i calcoli del sito provati con Node contro quelli del PC.

La clip che il browser prepara (WAV + JSON nella cartella inbox/ del bucket) deve essere quella
che il PC si aspetta: stessa forma del nome, ora leggibile, WAV leggibile, e poi pull_inbox + push
la mandano come una clip del live.
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

from bird_audio_suite.audio import sanitize_filename
from bird_audio_suite.cloud import LocalMediaStorage, outbox_items

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
  // una specie sentita in due finestre, in una registrazione di 30 s a 48 kHz (un'onda a 1 kHz)
  const rate = 48000;
  const samples = new Float32Array(rate * 30).map((_, i) => 0.5 * Math.sin(2 * Math.PI * 1000 * i / rate));
  const detections = r.addWindow(new Map(), [["Turdus merula_Eurasian Blackbird", 0.91], ["Parus major_Great Tit", 0.2]],
    12, 15, (sci, english) => sci === "Turdus merula" ? "Merlo" : english);
  r.addWindow(detections, [["Turdus merula_Eurasian Blackbird", 0.62]], 13.5, 16.5, () => "Merlo");
  const started = new Date(2026, 10, 3, 7, 15, 0);
  const clip = r.clipFor(detections.get("Turdus merula"), samples, rate,
    (seconds) => new Date(started.getTime() + seconds * 1000), { latitude: 52.5, longitude: 13.4 });
  out.kept = [...detections.values()].map((row) => [row.scientific, row.keep, row.windows.length]);
  out.clip = { name: clip.name, sidecar: clip.sidecar, wav: Buffer.from(clip.wav).toString("base64") };
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

    def test_what_is_heard_from_0_5_is_already_ticked(self) -> None:
        self.assertEqual(self.out["kept"], [["Turdus merula", True, 2]])  # the tit at 0.2 is not even listed

    def test_the_clip_of_the_site_reaches_supabase_like_one_of_the_live(self) -> None:
        clip = self.out["clip"]
        sidecar = clip["sidecar"]
        self.assertEqual(clip["name"], "Merlo_20261103_071509_0.910")  # 3 s before the first window (12 s)
        self.assertEqual((sidecar["id"], sidecar["recorded_at"], sidecar["duration_s"]),
                         ("Merlo_20261103_071509_0.910.wav", "2026-11-03T07:15:09+01:00", 10.0))
        self.assertEqual(sidecar["segments"], [[3.0, 6.0, 0.91], [4.5, 7.5, 0.62]])

        from test_cloud_sync import FakeRest, outbox

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "media" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / f"{clip['name']}.wav").write_bytes(base64.b64decode(clip["wav"]))
            (inbox / f"{clip['name']}.json").write_text(json.dumps(sidecar), encoding="utf-8")
            box = outbox(root / "detections", FakeRest(), LocalMediaStorage(root / "media"))
            self.assertEqual(box.pull_inbox(), 1)
            item = outbox_items(root / "detections")[0]
            samples, rate = soundfile.read(str(item.wav), dtype="float32")
            report = box.push()

        self.assertEqual((rate, len(samples)), (48000, 480000))  # 10 s, as written by the browser
        self.assertAlmostEqual(float(np.abs(samples).max()), 0.5, places=3)
        self.assertEqual((report.sent, report.errors), (1, []))


if __name__ == "__main__":
    unittest.main()
