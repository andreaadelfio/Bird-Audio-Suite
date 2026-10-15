// Noise reduction for one clip, in a worker so the page never stalls (a 60 s clip takes ~1 s).
//
// The noise is what the clip sounds like where BirdNET did NOT hear the species: the frames
// outside the yellow bands (with a margin). Its spectrum is the median of those frames, bin by
// bin (robust to the odd call outside the bands). It is then subtracted from the whole clip
// (spectral subtraction, over-subtracted a little), never below a floor: removing everything
// leaves the "musical noise" of chirping artefacts. Without bands, or with too little audio
// outside them, the quietest quarter of the clip is taken as noise.
//
// Message in: { id, samples: Float32Array, rate, segments: [[start_s, end_s, confidence], ...], options }
// Message out: { id, samples: Float32Array, noiseFrames, fromBands }

const FRAME = 2048;
const HOP = 512; // 75% overlap: Hann in and out adds up to 1.5
const BAND_MARGIN_SECONDS = 0.3;
const MIN_NOISE_FRAMES = 8;
const MAX_NOISE_FRAMES = 400; // enough for a median, cheap to sort
const OVER_SUBTRACTION = 2.0;
const NOISE_PERCENTILE = 0.5;
const FLOOR = 0.1; // at most -20 dB: below it the remaining noise starts to "chirp"
const RELEASE = 0.7; // per hop: the gain falls back gently after a call (no choppy tails)

const BITS = Math.log2(FRAME);
const REVERSED = new Uint32Array(FRAME);
for (let i = 0; i < FRAME; i += 1) {
  let reversed = 0;
  for (let bit = 0; bit < BITS; bit += 1) reversed |= ((i >> bit) & 1) << (BITS - 1 - bit);
  REVERSED[i] = reversed;
}
const COS = new Float64Array(FRAME / 2);
const SIN = new Float64Array(FRAME / 2);
for (let i = 0; i < FRAME / 2; i += 1) {
  COS[i] = Math.cos((2 * Math.PI * i) / FRAME);
  SIN[i] = -Math.sin((2 * Math.PI * i) / FRAME);
}
const WINDOW = new Float64Array(FRAME);
for (let i = 0; i < FRAME; i += 1) WINDOW[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / FRAME);

// in place, radix 2; inverse = conjugate, forward, conjugate, divide by FRAME
function fft(re, im, inverse = false) {
  for (let i = 0; i < FRAME; i += 1) {
    const j = REVERSED[i];
    if (j > i) {
      const r = re[i];
      re[i] = re[j];
      re[j] = r;
      const m = im[i];
      im[i] = im[j];
      im[j] = m;
    }
  }
  for (let size = 2; size <= FRAME; size *= 2) {
    const half = size / 2;
    const step = FRAME / size;
    for (let start = 0; start < FRAME; start += size) {
      for (let k = 0; k < half; k += 1) {
        const wr = COS[k * step];
        const wi = inverse ? -SIN[k * step] : SIN[k * step];
        const a = start + k;
        const b = a + half;
        const tr = re[b] * wr - im[b] * wi;
        const ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
      }
    }
  }
  if (inverse) {
    for (let i = 0; i < FRAME; i += 1) re[i] /= FRAME; // only the real part is used
  }
}

function spectrumOf(samples, frame, re, im) {
  const offset = frame * HOP;
  for (let i = 0; i < FRAME; i += 1) {
    const value = offset + i < samples.length ? samples[offset + i] : 0;
    re[i] = value * WINDOW[i];
    im[i] = 0;
  }
  fft(re, im);
}

function noiseFrameIndexes(samples, rate, segments, frames) {
  const centre = (frame) => (frame * HOP + FRAME / 2) / rate;
  const inBand = (time) => segments.some(([start, end]) => time >= start - BAND_MARGIN_SECONDS && time <= end + BAND_MARGIN_SECONDS);
  const outside = [];
  for (let frame = 0; frame < frames; frame += 1) if (!inBand(centre(frame))) outside.push(frame);
  if (segments.length && outside.length >= MIN_NOISE_FRAMES) return { indexes: outside, fromBands: true };
  // no usable bands: the quietest quarter of the clip
  const energy = [];
  for (let frame = 0; frame < frames; frame += 1) {
    let sum = 0;
    for (let i = frame * HOP; i < Math.min(samples.length, frame * HOP + FRAME); i += 1) sum += samples[i] * samples[i];
    energy.push([sum, frame]);
  }
  energy.sort((a, b) => a[0] - b[0]);
  const count = Math.max(1, Math.floor(frames / 4));
  return { indexes: energy.slice(0, count).map(([, frame]) => frame), fromBands: false };
}

function denoiseSamples(samples, rate, segments = [],
  { overSubtraction = OVER_SUBTRACTION, floor = FLOOR, percentile = NOISE_PERCENTILE, useBands = true } = {}) {
  const frames = Math.max(1, Math.ceil(Math.max(0, samples.length - FRAME) / HOP) + 1);
  const bins = FRAME / 2 + 1;
  const re = new Float64Array(FRAME);
  const im = new Float64Array(FRAME);

  // 1. the noise: median magnitude, bin by bin, over (at most MAX_NOISE_FRAMES of) the noise frames
  const { indexes, fromBands } = noiseFrameIndexes(samples, rate, useBands ? segments : [], frames);
  const every = Math.max(1, Math.ceil(indexes.length / MAX_NOISE_FRAMES));
  const chosen = indexes.filter((_, index) => index % every === 0);
  const magnitudes = chosen.map((frame) => {
    spectrumOf(samples, frame, re, im);
    const row = new Float32Array(bins);
    for (let k = 0; k < bins; k += 1) row[k] = Math.sqrt(re[k] * re[k] + im[k] * im[k]);
    return row;
  });
  const noise = new Float32Array(bins);
  const column = new Float32Array(magnitudes.length);
  for (let k = 0; k < bins; k += 1) {
    for (let index = 0; index < magnitudes.length; index += 1) column[index] = magnitudes[index][k];
    column.sort();
    noise[k] = column[Math.min(column.length - 1, Math.floor(column.length * percentile))];
  }

  // 2. subtract it everywhere, overlap-add back to samples
  const out = new Float32Array(frames * HOP + FRAME);
  const previous = new Float64Array(bins).fill(1);
  for (let frame = 0; frame < frames; frame += 1) {
    spectrumOf(samples, frame, re, im);
    for (let k = 0; k < bins; k += 1) {
      const magnitude = Math.sqrt(re[k] * re[k] + im[k] * im[k]) + 1e-12;
      let gain = Math.max(1 - (overSubtraction * noise[k]) / magnitude, floor);
      gain = Math.max(gain, previous[k] * RELEASE);
      previous[k] = gain;
      re[k] *= gain;
      im[k] *= gain;
      if (k > 0 && k < FRAME / 2) {
        re[FRAME - k] = re[k];
        im[FRAME - k] = -im[k];
      }
    }
    fft(re, im, true);
    const offset = frame * HOP;
    for (let i = 0; i < FRAME; i += 1) out[offset + i] += (re[i] * WINDOW[i]) / 1.5;
  }
  return { samples: out.subarray(0, samples.length), noiseFrames: indexes.length, fromBands };
}

if (typeof self !== "undefined" && typeof self.postMessage === "function") {
  self.onmessage = (event) => {
    const { id, samples, rate, segments, options } = event.data;
    const result = denoiseSamples(samples, rate, segments, options);
    const copy = new Float32Array(result.samples); // its own buffer, to transfer
    self.postMessage({ id, samples: copy, noiseFrames: result.noiseFrames, fromBands: result.fromBands }, [copy.buffer]);
  };
}
