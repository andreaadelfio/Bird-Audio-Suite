// Registra: dal sito (browser del PC o app sul telefono) si registra con il microfono e, mentre
// registra, BirdNET (birdnet-worker.js, nel browser) ascolta finestre di 3 s e mostra le specie in
// diretta. Alla fine la registrazione si riascolta (e si può scaricare) e per ogni specie scegli:
// confermata, da rivedere, o niente (il default: se non scegli nulla non parte nulla). Le scelte
// vanno subito su Supabase come le clip del live: la riga della specie, l'osservazione (con lo stato
// scelto) e la clip (confidenza, bande e candidate di BirdNET del browser), con lo stesso media_path.
// Il file audio va nel bucket come WAV, in inbox/: MP3 e spettrogramma (pesanti per un telefono)
// li fa il servizio di invio del PC, entro un minuto se è acceso, con l'analisi completa e Perch,
// poi toglie il WAV; intanto il sito suona il WAV. Senza rete la scelta resta sul telefono
// (IndexedDB) e parte appena la rete torna.

export const BIRDNET_RATE = 48000;
const WINDOW_SECONDS = 3;
const STEP_SECONDS = 1.5; // una finestra nuova ogni 1.5 s, se BirdNET ha finito la precedente
const SHOW_CONFIDENCE = 0.25; // da qui una specie compare nell'elenco
const BAND_CONFIDENCE = 0.6; // bande e specie candidate da qui, come l'analisi del PC (analysis.py)
const MAX_SECONDS = 10 * 60; // poi si ferma da sola (la memoria del telefono)
const PRE_ROLL_SECONDS = 3; // come il live: la clip parte 3 s prima della prima detection...
const MIN_CLIP_SECONDS = 10; // ...e dura almeno 10 s
const BUCKET = "bird-audio";
const INBOX = "inbox"; // i WAV in attesa del PC (MP3 e spettrogramma)

// ---------------------------------------------------------------- calcoli (senza pagina: testati)

// audio a un'altra frequenza (interpolazione lineare): BirdNET vuole 48 kHz
export function resample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const length = Math.round((samples.length * toRate) / fromRate);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const at = (i * fromRate) / toRate;
    const left = Math.floor(at);
    const right = Math.min(left + 1, samples.length - 1);
    out[i] = samples[left] + (samples[right] - samples[left]) * (at - left);
  }
  return out;
}

// la "settimana" di BirdNET (1-48, quattro al mese), come birdnetlib
export function week48(date) {
  const year = date.getFullYear();
  const day = (Date.UTC(year, date.getMonth(), date.getDate()) - Date.UTC(year, 0, 1)) / 86400000 + 1;
  const days = new Date(year, 1, 29).getDate() === 29 ? 366 : 365;
  return Math.max(1, Math.min(48, Math.ceil((day / days) * 48)));
}

// WAV mono a 16 bit, come le clip del live
export function wavBytes(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  }
  return new Uint8Array(buffer);
}

// come sanitize_filename del PC: lettere, cifre, _ - . e spazi
export function safeFileName(name) {
  const clean = String(name || "").trim().replace(/[^\p{L}\p{N}_\-. ]+/gu, "_").replace(/\s+/g, " ");
  return clean.replace(/^[ ._]+|[ ._]+$/g, "") || "detection";
}

const pad = (n) => String(n).padStart(2, "0");

// "20261103_071500", l'ora locale nel nome del file (come il live)
export function fileStamp(date) {
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_`
    + `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

// "2026-11-03T07:15:00+01:00", come recorded_at del live (ora locale con il suo fuso)
export function isoWithOffset(date) {
  const minutes = -date.getTimezoneOffset();
  const sign = minutes >= 0 ? "+" : "-";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T`
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    + `${sign}${pad(Math.floor(Math.abs(minutes) / 60))}:${pad(Math.abs(minutes) % 60)}`;
}

// il pezzo di registrazione di una specie, in secondi: 3 s prima della prima finestra in cui
// BirdNET la sente, fino alla fine dell'ultima, almeno 10 s (allungato dove c'è audio)
export function clipRange(first, last, duration) {
  let start = Math.max(0, first - PRE_ROLL_SECONDS);
  let end = Math.min(duration, last);
  const missing = MIN_CLIP_SECONDS - (end - start);
  if (missing > 0) {
    end = Math.min(duration, end + missing);
    start = Math.max(0, end - MIN_CLIP_SECONDS);
  }
  return [start, end];
}

// come slugify_public_id del PC: senza accenti, minuscolo, il resto "-"
export function slugify(name) {
  return String(name).normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase()
    .replace(/[^a-z0-9_]+/g, "-").replace(/^-+|-+$/g, "");
}

// dove stanno i file di una clip nel bucket, senza estensione: <AAAAMMGG>/<slug> (cloud.media_path_for)
export function mediaPathFor(clipId, date) {
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}/${slugify(clipId.replace(/\.wav$/i, ""))}`;
}

// tratti che si toccano o si sovrappongono diventano uno, con la confidenza più alta (merge_segments)
export function mergeSpans(spans, gap = 0.05) {
  const merged = [];
  for (const [from, to, confidence] of [...spans].sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && from <= last[1] + gap) {
      last[1] = Math.max(last[1], to);
      last[2] = Math.max(last[2], confidence);
    } else {
      merged.push([from, to, confidence]);
    }
  }
  return merged;
}

const round = (value, digits) => Number(value.toFixed(digits));

// la clip di una specie, come la scrive il PC: l'audio (WAV), l'osservazione e la riga di
// birds.clips. detections: tutte le specie della registrazione (per le candidate);
// heardAt(secondi) -> Date: l'ora vera di quel punto; status: "confirmed" o "pending".
export function clipFor(detection, detections, samples, rate, heardAt, status, place = {}) {
  const duration = samples.length / rate;
  const [start, end] = clipRange(detection.first, detection.last, duration);
  const begins = heardAt(start);
  const id = `${safeFileName(detection.display)}_${fileStamp(begins)}_${detection.best.toFixed(3)}.wav`;
  const inside = (windows) => windows.filter(([from, to]) => to > start && from < end);
  // le altre specie probabili nello stesso pezzo, la sua compresa: [scientifico, inglese, confidenza]
  const candidates = [...detections.values()]
    .map((row) => [row.scientific, row.english, Math.max(0, ...inside(row.windows).map((w) => w[2]))])
    .filter(([, , confidence]) => confidence >= BAND_CONFIDENCE)
    .sort((a, b) => b[2] - a[2]).slice(0, 3)
    .map(([scientific, english, confidence]) => [scientific, english, round(confidence, 3)]);
  return {
    id,
    wav: wavBytes(samples.subarray(Math.round(start * rate), Math.round(end * rate)), rate),
    observation: { day: isoWithOffset(begins).slice(0, 10), scientific_name: detection.scientific, status },
    clip: {
      id,
      recorded_at: isoWithOffset(begins),
      confidence: round(detection.best, 3),
      live_confidence: round(detection.best, 3), // il PC rifà l'analisi sul file e aggiorna confidence
      duration_s: round(end - start, 2),
      place_name: "",
      latitude: place.latitude ?? null,
      longitude: place.longitude ?? null,
      media_path: mediaPathFor(id, begins),
      // dove BirdNET la sente da 0.6, in secondi dall'inizio della clip
      segments: mergeSpans(inside(detection.windows).filter((w) => w[2] >= BAND_CONFIDENCE))
        .map(([from, to, confidence]) => [round(Math.max(from, start) - start, 2), round(Math.min(to, end) - start, 2),
          round(confidence, 3)]),
      candidates,
    },
  };
}

// una finestra analizzata: ogni specie sentita si aggiunge (o aggiorna) all'elenco della registrazione
export function addWindow(detections, species, from, to, nameOf) {
  for (const [label, confidence] of species) {
    if (confidence < SHOW_CONFIDENCE) continue;
    const [scientific, english = ""] = label.split("_");
    const found = detections.get(scientific) || {
      scientific, english, display: nameOf(scientific, english), best: 0, first: from, last: to, windows: [],
      choice: "", // "" (non si manda), "pending" (da rivedere) o "confirmed": la scegli tu alla fine
    };
    found.best = Math.max(found.best, confidence);
    found.last = Math.max(found.last, to);
    found.windows.push([from, to, confidence]);
    detections.set(scientific, found);
  }
  return detections;
}

// ---------------------------------------------------------------- Supabase e telefono

// una clip scelta su Supabase, come le scrive il PC: il WAV nel bucket (inbox/, finché il PC non fa
// MP3 e spettrogramma), la specie se manca, l'osservazione con lo stato scelto, la riga della clip.
// Rifarla non duplica: se la clip c'è già, è fatta.
export async function sendClip(supabase, item) {
  const { data: there, error: lookError } = await supabase.from("clips").select("id").eq("id", item.id);
  if (lookError) throw lookError;
  if (there.length) return;
  const { error: fileError } = await supabase.storage.from(BUCKET)
    .upload(`${INBOX}/${item.id}`, item.wav, { contentType: "audio/wav", upsert: true });
  if (fileError) throw fileError;
  // la specie, se è la prima volta (i nomi e i link li aggiunge il PC entro l'ora)
  const { error: speciesError } = await supabase.from("species")
    .upsert({ scientific_name: item.observation.scientific_name }, { onConflict: "scientific_name", ignoreDuplicates: true });
  if (speciesError) throw speciesError;
  const { data: observation, error: observationError } = await supabase.from("observations")
    .insert({ ...item.observation, reviewed_at: item.observation.status === "confirmed" ? new Date().toISOString() : null })
    .select("id").single();
  if (observationError) throw observationError;
  const { error: clipError } = await supabase.from("clips").insert({ ...item.clip, observation_id: observation.id });
  if (clipError) {
    await supabase.from("observations").delete().eq("id", observation.id); // non resta vuota
    throw clipError;
  }
}

// le clip scelte e non ancora su Supabase, sul telefono (IndexedDB): ci restano anche chiudendo l'app
function openQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("bird-audio-suite", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("registra", { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function inQueue(mode, work) {
  return openQueue().then((db) => new Promise((resolve, reject) => {
    const transaction = db.transaction("registra", mode);
    const request = work(transaction.objectStore("registra"));
    transaction.oncomplete = () => resolve(request?.result);
    transaction.onerror = () => reject(transaction.error);
  }));
}

export const queue = {
  put: (item) => inQueue("readwrite", (store) => store.put(item)),
  remove: (id) => inQueue("readwrite", (store) => store.delete(id)),
  all: () => inQueue("readonly", (store) => store.getAll()),
};

// ---------------------------------------------------------------- la pagina

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

const clock = new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function minutesSeconds(seconds) {
  return `${Math.floor(seconds / 60)}:${pad(Math.floor(seconds % 60))}`;
}

// client(): il client di Supabase (null offline); nameOf(scientifico, inglese): il nome da mostrare;
// say(messaggio, errore): il messaggio in basso del sito; confidenceClass(c): "likely" o "unsure"
export function setupRecorder({ client, nameOf, say, confidenceClass }) {
  const $ = (id) => document.getElementById(id);
  const panel = $("record-panel");
  const toggle = $("record-toggle");
  const startButton = $("record-start");
  const stateLabel = $("record-state");
  const timeLabel = $("record-time");
  const level = $("record-level");
  const now = $("record-now");
  const list = $("record-species");
  const done = $("record-done");
  const placeBox = $("record-place");

  let worker = null;
  let ready = null; // promessa: BirdNET caricato
  let rec = null; // la registrazione in corso, o l'ultima fatta
  let place = {}; // { latitude, longitude } se l'hai permesso
  let wakeLock = null;
  const waiting = new Map(); // finestra -> [inizio, fine] in secondi

  try {
    placeBox.checked = localStorage.getItem("recordPlace") === "1";
  } catch {
    // niente memoria nel browser: resta spenta
  }

  function loadBirdnet() {
    if (ready) return ready;
    stateLabel.textContent = "carico BirdNET…";
    startButton.disabled = true;
    ready = new Promise((resolve, reject) => {
      worker = new Worker("./assets/birdnet-worker.js?v=1");
      worker.onmessage = ({ data }) => {
        if (data.type === "progress") stateLabel.textContent = `carico BirdNET… ${Math.round(data.fraction * 100)}%`;
        if (data.type === "ready") {
          stateLabel.textContent = "pronto";
          stateLabel.title = `BirdNET V2.4 nel browser (${data.backend})`;
          startButton.disabled = false;
          resolve();
          if (placeBox.checked) usePlace();
        }
        if (data.type === "place") {
          stateLabel.textContent = data.count == null ? "pronto" : `pronto · ${data.count} specie possibili qui in questa settimana`;
        }
        if (data.type === "result") onResult(data);
        if (data.type === "error") {
          if (waiting.has(data.id)) onResult({ id: data.id, species: [] });
          else {
            stateLabel.textContent = "BirdNET non si carica";
            say(`BirdNET non si carica: ${data.message}`, true);
            reject(new Error(data.message));
          }
        }
      };
      worker.postMessage({ type: "load" });
    });
    return ready;
  }

  // la posizione del telefono (facoltativa): BirdNET tiene solo le specie possibili lì in questa settimana
  function usePlace() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition((position) => {
      place = { latitude: Number(position.coords.latitude.toFixed(5)), longitude: Number(position.coords.longitude.toFixed(5)) };
      worker?.postMessage({ type: "place", ...place, week: week48(new Date()) });
    }, () => say("Posizione non disponibile: BirdNET considera tutte le specie.", true), { timeout: 15000, maximumAge: 600000 });
  }

  placeBox.addEventListener("change", () => {
    try {
      localStorage.setItem("recordPlace", placeBox.checked ? "1" : "");
    } catch {
      // non ricordata: vale per ora
    }
    if (placeBox.checked) usePlace();
    else {
      place = {};
      worker?.postMessage({ type: "place" }); // senza posizione: di nuovo tutte le specie
    }
  });

  toggle.addEventListener("click", () => {
    const open = panel.classList.toggle("hidden") === false;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) loadBirdnet().catch(() => {});
  });

  startButton.addEventListener("click", () => (rec?.running ? stop() : start()).catch((error) => {
    say(`Registra: ${error.message}`, true);
    if (rec?.running) stop();
  }));

  // ------------------------------------------------ registrazione

  async function start() {
    await loadBirdnet();
    discard();
    let context;
    try {
      context = new AudioContext({ sampleRate: BIRDNET_RATE }); // il browser porta il microfono a 48 kHz
    } catch {
      context = new AudioContext(); // altrimenti la sua frequenza, e si ricampiona qui
    }
    await context.audioWorklet.addModule("./assets/registra-worklet.js?v=1");
    const node = new AudioWorkletNode(context, "raccoglitore");
    // prova: ?prova-audio=<file> usa un file al posto del microfono (test, e per provare senza uccelli)
    const test = new URLSearchParams(location.search).get("prova-audio");
    let stream = null;
    let source;
    if (test) {
      const buffer = await context.decodeAudioData(await fetch(test).then((response) => response.arrayBuffer()));
      source = new AudioBufferSourceNode(context, { buffer });
      source.onended = () => { if (rec?.running) stop(); };
    } else {
      stream = await navigator.mediaDevices.getUserMedia({
        // l'audio com'è: niente filtri pensati per la voce, che tolgono proprio gli uccelli
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
      });
      source = context.createMediaStreamSource(stream);
      stream.getAudioTracks()[0].addEventListener("ended", () => {
        if (rec?.running) {
          say("Il microfono si è fermato (un'altra app, o il telefono): la registrazione finisce qui.", true);
          stop();
        }
      });
    }
    source.connect(node);
    node.connect(context.destination); // in silenzio: serve solo perché il browser lo faccia girare
    rec = {
      running: true, context, node, source, stream, rate: context.sampleRate,
      chunks: [], length: 0, recent: [], recentLength: 0, marks: [], lastWindowEnd: 0, busy: false,
      detections: new Map(), startedAt: new Date(), windows: 0, slow: 0,
    };
    node.port.onmessage = ({ data }) => onSamples(data);
    if (test) source.start();
    await keepAwake();
    startButton.textContent = "■ Ferma";
    startButton.classList.add("recording");
    toggle.classList.add("recording");
    now.textContent = "Ascolto…";
    list.innerHTML = "";
    done.classList.add("hidden");
  }

  function onSamples(block) {
    if (!rec?.running) return;
    // dove cade ogni blocco nel tempo vero: se Android ha fermato il microfono per un po', le
    // ore delle clip dopo restano giuste (il pezzo mancante manca e basta)
    rec.marks.push([rec.length, Date.now()]);
    const int16 = new Int16Array(block.length);
    let energy = 0;
    for (let i = 0; i < block.length; i += 1) {
      int16[i] = Math.max(-1, Math.min(1, block[i])) * 0x7fff;
      energy += block[i] * block[i];
    }
    rec.chunks.push(int16);
    rec.length += block.length;
    rec.recent.push(block);
    rec.recentLength += block.length;
    const keep = WINDOW_SECONDS * rec.rate;
    while (rec.recentLength - rec.recent[0].length >= keep) rec.recentLength -= rec.recent.shift().length;
    level.style.width = `${Math.min(100, Math.sqrt(energy / block.length) * 400)}%`;
    timeLabel.textContent = minutesSeconds(rec.length / rec.rate);
    if (rec.length >= MAX_SECONDS * rec.rate) {
      say("Dieci minuti: la registrazione si ferma qui.");
      stop();
      return;
    }
    if (!rec.busy && rec.length >= keep && rec.length - rec.lastWindowEnd >= STEP_SECONDS * rec.rate) analyseLatest();
  }

  // gli ultimi 3 s a BirdNET; se non ha ancora finito la finestra prima, questa si salta (si
  // sovrappongono: un telefono lento ne analizza meno, mai in ritardo)
  function analyseLatest() {
    const keep = WINDOW_SECONDS * rec.rate;
    const window = new Float32Array(keep);
    let offset = keep;
    for (let i = rec.recent.length - 1; i >= 0 && offset > 0; i -= 1) {
      const chunk = rec.recent[i];
      const take = Math.min(chunk.length, offset);
      window.set(chunk.subarray(chunk.length - take), offset - take);
      offset -= take;
    }
    rec.busy = true;
    rec.windows += 1;
    rec.lastWindowEnd = rec.length;
    waiting.set(rec.windows, [Math.max(0, rec.length - keep) / rec.rate, rec.length / rec.rate]);
    // esattamente 3 s a 48 kHz, anche quando il ricampionamento arrotonda
    const samples = new Float32Array(BIRDNET_RATE * WINDOW_SECONDS);
    const resampled = resample(window, rec.rate, BIRDNET_RATE);
    samples.set(resampled.subarray(0, Math.min(resampled.length, samples.length)));
    worker.postMessage({ type: "analyse", id: rec.windows, samples }, [samples.buffer]);
  }

  function onResult({ id, species, seconds }) {
    const span = waiting.get(id);
    waiting.delete(id);
    if (!rec || !span) return;
    rec.busy = false;
    if (seconds > STEP_SECONDS) rec.slow += 1;
    addWindow(rec.detections, species, span[0], span[1], nameOf);
    const heard = species.filter(([, confidence]) => confidence >= SHOW_CONFIDENCE);
    if (!rec.running) {
      finishTail();
      summary();
    } else {
      now.innerHTML = heard.length
        ? `Ora: ${heard.slice(0, 3).map(([label, confidence]) => {
          const [scientific, english] = label.split("_");
          return `<b>${escapeHtml(nameOf(scientific, english))}</b> ${confidence.toFixed(2)}`;
        }).join(" · ")}`
        : "Ascolto… nessun uccello in questi 3 secondi";
    }
    drawList();
  }

  const CHOICES = [["", "Non mandare"], ["pending", "Da rivedere"], ["confirmed", "✓ Confermata"]];

  // ogni specie: confidenza, i tratti in cui BirdNET la sente (dall'inizio della registrazione) e,
  // a registrazione finita, la scelta: niente (non parte nulla), da rivedere o confermata
  function drawList() {
    const rows = [...rec.detections.values()].sort((a, b) => b.best - a.best);
    list.innerHTML = rows.map((row) => {
      const spans = mergeSpans(row.windows).map(([from, to]) => `${minutesSeconds(from)}–${minutesSeconds(to)}`);
      const choice = rec.running ? "" : `<span class="segmented record-choice" role="group" aria-label="${escapeHtml(row.display)}">
        ${CHOICES.map(([value, label]) => `<button type="button" data-choice="${value}" data-species="${escapeHtml(row.scientific)}"
          class="${row.choice === value ? "on" : ""}" aria-pressed="${row.choice === value}">${label}</button>`).join("")}</span>`;
      return `<li><span><b>${escapeHtml(row.display)}</b> <i>${escapeHtml(row.scientific)}</i>
        <span class="conf-${confidenceClass(row.best)}">${row.best.toFixed(2)}</span></span>
        <small>${spans.join(", ")} · dalle ${clock.format(heardAt(row.first))}</small>${choice}</li>`;
    }).join("");
    const chosen = rows.filter((row) => row.choice).length;
    const send = $("record-save");
    send.textContent = chosen ? `Manda su Supabase (${chosen})` : "Manda su Supabase";
    send.disabled = !chosen;
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-choice]");
    const found = button && rec?.detections.get(button.dataset.species);
    if (!found) return;
    found.choice = button.dataset.choice;
    drawList();
  });

  // l'ora vera di un punto della registrazione (secondi dall'inizio)
  function heardAt(seconds) {
    const sample = seconds * rec.rate;
    let mark = rec.marks[0] || [0, rec.startedAt.getTime()];
    for (const candidate of rec.marks) {
      if (candidate[0] > sample) break;
      mark = candidate;
    }
    return new Date(mark[1] + ((sample - mark[0]) / rec.rate) * 1000);
  }

  function stop() {
    if (!rec?.running) return Promise.resolve();
    rec.running = false;
    rec.node.port.onmessage = null;
    try {
      rec.source.disconnect();
      rec.node.disconnect();
    } catch {
      // già staccati
    }
    rec.stream?.getTracks().forEach((track) => track.stop());
    rec.context.close();
    releaseAwake();
    startButton.textContent = "● Registra";
    startButton.classList.remove("recording");
    toggle.classList.remove("recording");
    // tutta la registrazione, da riascoltare e scaricare
    rec.samples = new Float32Array(rec.length);
    let offset = 0;
    for (const chunk of rec.chunks) {
      for (let i = 0; i < chunk.length; i += 1) rec.samples[offset + i] = chunk[i] / 0x7fff;
      offset += chunk.length;
    }
    rec.chunks = [];
    const url = URL.createObjectURL(new Blob([wavBytes(rec.samples, rec.rate)], { type: "audio/wav" }));
    rec.url = url;
    $("record-audio").src = url;
    const download = $("record-download");
    download.href = url;
    download.download = `registrazione_${fileStamp(rec.startedAt)}.wav`;
    finishTail();
    summary();
    drawList(); // ora con le scelte
    done.classList.remove("hidden");
    return Promise.resolve();
  }

  // l'ultimo pezzo dopo l'ultima finestra (fino a 1.5 s, o tutto se è durata meno di 3 s): una
  // volta, quando BirdNET è libero; una registrazione corta si completa con silenzio
  function finishTail() {
    if (rec.tailDone || rec.busy || !rec.length || rec.length - rec.lastWindowEnd < 0.5 * rec.rate) return;
    rec.tailDone = true;
    analyseLatest();
  }

  function summary() {
    const count = rec.detections.size;
    now.textContent = `${minutesSeconds(rec.length / rec.rate)} registrati, ${rec.windows} `
      + `${rec.windows === 1 ? "finestra analizzata" : "finestre analizzate"}`
      + (count ? `: ${count} specie. Scegli per ognuna se è confermata o da rivedere: le altre non partono.`
        : ": nessun uccello riconosciuto.")
      + (rec.slow > rec.windows / 3 ? " (Questo dispositivo è lento per BirdNET: ha saltato delle finestre.)" : "")
      + (rec.busy ? " Analizzo l'ultimo pezzo…" : "");
  }

  // ------------------------------------------------ mandare su Supabase (o tenere sul telefono)

  // le scelte fatte: prima sul telefono (IndexedDB, così non si perdono neanche chiudendo l'app),
  // poi su Supabase; senza rete restano lì e partono quando torna
  $("record-save").addEventListener("click", async () => {
    const chosen = [...(rec?.detections.values() || [])].filter((row) => row.choice);
    if (!chosen.length) return;
    const button = $("record-save");
    button.disabled = true;
    try {
      for (const detection of chosen) {
        const item = clipFor(detection, rec.detections, rec.samples, rec.rate, heardAt, detection.choice, place);
        await queue.put(item);
      }
    } catch (error) {
      say(`Non salvata sul telefono: ${error.message}. Scarica il WAV per non perderla.`, true);
      button.disabled = false;
      return;
    }
    discard();
    now.textContent = "";
    await sendWaiting(chosen.length);
  });

  // manda quello che aspetta sul telefono; chosen: quante ne hai appena scelte (per il messaggio)
  let sending = false;
  async function sendWaiting(chosen = 0) {
    const supabase = client();
    if (sending) return;
    sending = true;
    let sent = 0;
    let failure = null;
    try {
      const items = await queue.all();
      for (const item of items) {
        if (!supabase || !navigator.onLine) break;
        say(`Mando ${sent + 1} di ${items.length}: ${item.observation.scientific_name}…`);
        try {
          await sendClip(supabase, item);
          await queue.remove(item.id);
          sent += 1;
        } catch (error) {
          failure = error;
          break; // rete caduta a metà: le altre aspettano con lei
        }
      }
      const left = (await queue.all()).length;
      showWaiting(left);
      if (sent && !left) {
        say(`Mandate ${sent} su Supabase: sono tra le Osservazioni. MP3 e spettrogramma li aggiunge il PC `
          + "(entro un minuto se è acceso), intanto si ascolta il WAV.");
      } else if (left && (chosen || failure)) {
        say(`${left} ${left === 1 ? "clip resta" : "clip restano"} sul telefono: ${failure ? errorText(failure) : "nessuna rete"}. `
          + "Partono da sole appena c'è rete.", Boolean(failure));
      }
    } catch (error) {
      say(`Registra: ${error.message}`, true);
    } finally {
      sending = false;
    }
  }

  function errorText(error) {
    return /failed to fetch|network|load failed/i.test(error.message) ? "nessuna rete" : error.message;
  }

  function showWaiting(left) {
    const label = $("record-waiting");
    label.textContent = left ? `${left} ${left === 1 ? "clip" : "clip"} sul telefono, in attesa di rete` : "";
  }

  window.addEventListener("online", () => sendWaiting());
  queue.all().then((items) => {
    showWaiting(items.length);
    if (items.length) sendWaiting();
  }).catch(() => {});

  $("record-discard").addEventListener("click", () => {
    if (rec?.detections.size && !window.confirm("Scartare la registrazione? Non è stata salvata.")) return;
    discard();
    now.textContent = "";
  });

  function discard() {
    if (rec?.url) URL.revokeObjectURL(rec.url);
    if (rec && !rec.running) rec = null;
    list.innerHTML = "";
    done.classList.add("hidden");
    $("record-audio").removeAttribute("src");
  }

  // ------------------------------------------------ schermo acceso, app in secondo piano

  async function keepAwake() {
    try {
      wakeLock = await navigator.wakeLock?.request("screen");
    } catch {
      wakeLock = null; // non concesso (batteria scarica...): si registra lo stesso
    }
  }

  function releaseAwake() {
    wakeLock?.release().catch(() => {});
    wakeLock = null;
  }

  // tornando sull'app: lo schermo di nuovo acceso; se Android aveva sospeso l'audio, riparte, e
  // si dice quanto manca
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !rec?.running) return;
    keepAwake();
    if (rec.context.state !== "running") rec.context.resume().catch(() => {});
    const last = rec.marks[rec.marks.length - 1];
    const missing = last ? (Date.now() - last[1]) / 1000 : 0;
    if (missing > 2) say(`Mentre l'app era in secondo piano il microfono si è fermato: mancano circa ${Math.round(missing)} s.`, true);
  });
}
