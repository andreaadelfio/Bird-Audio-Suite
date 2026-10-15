"""The site's noise reduction (docs/assets/denoise-worker.js), run with Node when it is there."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "docs" / "assets" / "denoise-worker.js"

SCRIPT = r"""
const fs = require("fs"); const vm = require("vm");
const context = {}; vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\nthis.denoiseSamples = denoiseSamples;", context);
const rate = 48000, n = rate * 4;
const song = new Float32Array(n);
for (let i = Math.floor(1.5 * rate); i < 2.5 * rate; i++) song[i] = 0.1 * Math.sin(2 * Math.PI * 4000 * i / rate);
let seed = 1; const rand = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
const noisy = song.map((v) => v + 0.03 * rand());
const db = (x, a, b) => { let s = 0; for (let i = a; i < b; i++) s += x[i] * x[i]; return 10 * Math.log10(s / (b - a)); };
const same = context.denoiseSamples(song, rate, [[1.5, 2.5, 0.9]]).samples;
let error = 0; for (let i = 0; i < n; i++) error = Math.max(error, Math.abs(same[i] - song[i]));
const out = context.denoiseSamples(noisy, rate, [[1.5, 2.5, 0.9]], { overSubtraction: 1, floor: 0.3, percentile: 0.3 });
const quiet = context.denoiseSamples(noisy, rate, []);
console.log(JSON.stringify({
  identityError: error,
  noiseDrop: db(noisy, 0, rate) - db(out.samples, 0, rate),
  songKept: db(out.samples, 1.6 * rate, 2.4 * rate) - db(song, 1.6 * rate, 2.4 * rate),
  fromBands: out.fromBands, quietFromBands: quiet.fromBands,
}));
"""


@unittest.skipUnless(shutil.which("node"), "Node not installed")
class DenoiseWorkerTests(unittest.TestCase):
    def test_noise_goes_the_song_stays_silence_passes_unchanged(self) -> None:
        result = json.loads(subprocess.run(["node", "-e", SCRIPT, str(WORKER)], capture_output=True, text=True,
                                           check=True, timeout=120).stdout)
        self.assertLess(result["identityError"], 1e-6)  # nothing outside the bands: untouched
        self.assertGreater(result["noiseDrop"], 4.0)  # medium strength: background down
        self.assertGreater(result["songKept"], -1.5)  # the bird keeps its level
        self.assertTrue(result["fromBands"])
        self.assertFalse(result["quietFromBands"])  # no bands: the quietest frames


if __name__ == "__main__":
    unittest.main()
