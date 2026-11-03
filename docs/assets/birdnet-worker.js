// BirdNET nel browser (Registra): il modello V2.4 di BirdNET in formato TensorFlow.js, lo stesso
// che usa il PC (birdnetlib), gira qui in un worker, così la pagina resta fluida mentre ascolta.
// Modelli in docs/models/birdnet (Zenodo, record 15050749, licenza CC BY-NC 4.0) e TensorFlow.js
// in docs/vendor/tfjs: tutto dal sito, così il service worker lo tiene e funziona anche senza rete.
//
// Messaggi dalla pagina:
//   { type: "load" }                                -> "progress" ... poi "ready"
//   { type: "place", latitude, longitude, week }    -> "place" (quante specie possibili lì in quella settimana)
//   { type: "place" }                               -> di nuovo tutte le specie
//   { type: "analyse", id, samples }                -> "result" { id, species: [[etichetta, confidenza], ...] }
// samples: 3 s di audio mono a 48 kHz (144000 campioni), come vuole BirdNET.

importScripts("../vendor/tfjs/tf.min.js", "../vendor/tfjs/tf-backend-wasm.min.js");
tf.wasm.setWasmPaths(new URL("../vendor/tfjs/", self.location.href).href);

const MODEL_URL = "../models/birdnet/model.json";
const PLACE_MODEL_URL = "../models/birdnet/mdata/model.json";
const LABELS_URL = "../models/birdnet/labels.json";
const SAMPLES = 144000; // 3 s a 48 kHz
const MIN_CONFIDENCE = 0.1; // sotto non si manda niente alla pagina
const PLACE_THRESHOLD = 0.03; // come birdnetlib: specie possibile lì in quella settimana

// Lo spettrogramma mel è un livello del modello che TensorFlow.js non conosce: lo si definisce qui.
// Come nel livello con cui BirdNET è stato addestrato: normalizza tra -1 e 1, STFT con finestra di
// Hann, tiene la parte REALE (non il modulo: con il modulo i punteggi cambiano, 0.64 invece di
// 0.94 sul campione ufficiale), banco di filtri mel, potenza, scala con il peso "magnitude_scaling".
// Parte reale della STFT e filtri mel sono entrambi lineari: diventano UNA moltiplicazione dei
// pezzi di segnale per una matrice calcolata una volta (finestra x coseni x filtri mel). Stesso
// risultato, circa dieci volte meno conti della FFT, e va su ogni backend (WASM non ha la FFT).
class MelSpecLayerSimple extends tf.layers.Layer {
  constructor(config) {
    super(config);
    this.specShape = config.specShape;
    this.frameStep = config.frameStep;
    this.frameLength = config.frameLength;
    this.kernel = melKernel(config.frameLength, config.melFilterbank);
  }

  build(inputShape) {
    this.magScale = this.addWeight("magnitude_scaling", [], "float32", tf.initializers.constant({ value: 1.23 }));
    super.build(inputShape);
  }

  computeOutputShape(inputShape) {
    return [inputShape[0], this.specShape[0], this.specShape[1], 1];
  }

  call(inputs) {
    return tf.tidy(() => {
      const batch = inputs[0];
      return tf.stack(tf.split(batch, batch.shape[0]).map((one) => {
        let signal = one.squeeze();
        signal = tf.sub(signal, tf.min(signal, -1, true));
        signal = tf.div(signal, tf.max(signal, -1, true).add(0.000001));
        signal = tf.mul(tf.sub(signal, 0.5), 2.0);
        const frames = tf.signal.frame(signal, this.frameLength, this.frameStep);
        let spec = tf.matMul(frames, this.kernel).pow(2.0);
        spec = spec.pow(tf.div(1.0, tf.add(1.0, tf.exp(this.magScale.read()))));
        return tf.transpose(tf.reverse(spec, -1)).expandDims(-1);
      }));
    });
  }

  static get className() {
    return "MelSpecLayerSimple";
  }
}
tf.serialization.registerClass(MelSpecLayerSimple);

// (finestra di Hann periodica x coseni della DFT) moltiplicata per i filtri mel: [frameLength, bande]
function melKernel(frameLength, melFilterbank) {
  const bins = frameLength / 2 + 1;
  const basis = new Float32Array(frameLength * bins);
  for (let n = 0; n < frameLength; n += 1) {
    const window = 0.5 - 0.5 * Math.cos((2 * Math.PI * n) / frameLength);
    for (let k = 0; k < bins; k += 1) {
      basis[n * bins + k] = window * Math.cos((2 * Math.PI * ((k * n) % frameLength)) / frameLength);
    }
  }
  return tf.tidy(() => tf.matMul(tf.tensor2d(basis, [frameLength, bins]), tf.tensor2d(melFilterbank)));
}

let model = null;
let placeModel = null;
let labels = [];
let allowed = null; // specie possibili nel luogo e nella settimana (null: tutte)

// la scheda grafica se c'è (PC, quasi tutti i telefoni), altrimenti WebAssembly, altrimenti la CPU
async function chooseBackend() {
  for (const backend of ["webgl", "wasm", "cpu"]) {
    try {
      if (await tf.setBackend(backend)) return backend;
    } catch {
      // non disponibile qui: il prossimo
    }
  }
  return tf.getBackend();
}

async function load() {
  const backend = await chooseBackend();
  labels = await fetch(LABELS_URL).then((response) => response.json());
  model = await tf.loadLayersModel(MODEL_URL, {
    onProgress: (fraction) => postMessage({ type: "progress", fraction }),
  });
  const started = performance.now();
  tf.tidy(() => model.predict(tf.zeros([1, SAMPLES]))); // il primo giro prepara il backend
  postMessage({ type: "ready", backend, warmup: (performance.now() - started) / 1000 });
}

// il modello "mdata" di BirdNET: quanto è probabile ogni specie in un punto e in una settimana
async function place(latitude, longitude, week) {
  if (!placeModel) placeModel = await tf.loadGraphModel(PLACE_MODEL_URL);
  const scores = await tf.tidy(() => placeModel.predict(tf.tensor([[latitude, longitude, week]]))).data();
  allowed = new Set();
  scores.forEach((score, index) => { if (score >= PLACE_THRESHOLD) allowed.add(index); });
  postMessage({ type: "place", count: allowed.size });
}

async function analyse(id, samples) {
  const started = performance.now();
  // questa versione del modello dà già le probabilità (la sigmoide è dentro, sul PC la fa birdnetlib)
  const scores = await tf.tidy(() => model.predict(tf.tensor(samples, [1, SAMPLES]))).data();
  const species = [];
  scores.forEach((confidence, index) => {
    if (confidence >= MIN_CONFIDENCE && (!allowed || allowed.has(index))) species.push([labels[index], confidence]);
  });
  species.sort((a, b) => b[1] - a[1]);
  postMessage({ type: "result", id, species: species.slice(0, 5), seconds: (performance.now() - started) / 1000 });
}

onmessage = async ({ data }) => {
  try {
    if (data.type === "load") await load();
    if (data.type === "place" && data.latitude == null) {
      allowed = null; // posizione tolta: tutte le specie
      postMessage({ type: "place", count: null });
    } else if (data.type === "place") await place(data.latitude, data.longitude, data.week);
    if (data.type === "analyse") await analyse(data.id, data.samples);
  } catch (error) {
    postMessage({ type: "error", id: data.id, message: String((error && error.message) || error) });
  }
};
