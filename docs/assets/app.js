// Bird Audio Suite: review site. Plain ES module, no build step (like MyFinance).
// Data: Supabase schema `birds` (see supabase/birds_schema.sql), open to the publishable key.
// The site changes only the review columns; everything else is written by `bird_audio_cli.py sync`.

const config = window.APP_CONFIG || {};
const PAGE_SIZE = 25;
const VISIBLE_CLIPS = 3; // the rest of a long observation stays folded
const STATUS_LABELS = {
  pending: "da rivedere",
  confirmed: "confermata",
  rejected: "scartata",
  uploaded: "caricata",
};

const BUCKET = "bird-audio";
// files live in Storage: the database keeps only their path (clips.media_path)
const MEDIA_BASE = config.mediaBaseUrl || `${config.supabaseUrl}/storage/v1/object/public/${BUCKET}`;

function mediaUrls(mediaPath) {
  const base = `${MEDIA_BASE}/${String(mediaPath || "").split("/").map(encodeURIComponent).join("/")}`;
  return { audio: `${base}.mp3`, spectrogram: `${base}-spectrogram.jpg` };
}

// everything about an observation that is computed from its clips, never stored
function observationFacts(observation) {
  const clips = [...(observation.clips || [])].sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
  const first = clips[0] || {};
  return {
    clips,
    maxConfidence: clips.reduce((best, clip) => Math.max(best, Number(clip.confidence) || 0), 0),
    firstHeard: first.recorded_at || null,
    lastHeard: clips.length ? clips[clips.length - 1].recorded_at : null,
    place: first.place_name || "",
    latitude: first.latitude ?? null,
    longitude: first.longitude ?? null,
  };
}
const OBSERVATION_SELECT = "*, species(italian_name,english_name,german_name), clips(*)";

const state = {
  client: null,
  view: "observations",
  offset: 0,
  species: new Map(),
  trash: new Map(), // observation id -> { whole, clips } shown in the Cestino
  observations: new Map(), // id -> observation currently shown, for the iNaturalist upload
};

const $ = (selector) => document.querySelector(selector);
const content = $("#content");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

const timeFormat = new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayFormat = new Intl.DateTimeFormat("it-IT", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

function formatDay(isoDay) {
  return dayFormat.format(new Date(`${isoDay}T12:00:00`));
}

// how long a message stays: results go quickly, errors stay longer to be read, "in progress"
// ones ("Elimino…") are normally replaced by the result and only time out as a safety net
const STATUS_SECONDS = { done: 4, error: 8, progress: 15 };
let statusTimer = null;

function setStatus(message, isError = false) {
  const line = $("#status-line");
  line.textContent = message;
  line.classList.toggle("error", isError);
  clearTimeout(statusTimer);
  if (!message) return;
  const kind = isError ? "error" : message.endsWith("…") ? "progress" : "done";
  statusTimer = setTimeout(() => setStatus(""), STATUS_SECONDS[kind] * 1000);
}

$("#status-line").addEventListener("click", () => setStatus("")); // tap to dismiss

// ---------------------------------------------------------------- client

function createClient() {
  if (!config.supabaseUrl || !config.supabaseKey) return null;
  return window.supabase.createClient(config.supabaseUrl, config.supabaseKey, {
    db: { schema: config.schema || "birds" },
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

// ---------------------------------------------------------------- data

async function loadSpecies() {
  const { data, error } = await state.client
    .from("species_stats")
    .select("scientific_name,italian_name,english_name,first_seen_date");
  if (error) throw error;
  state.species = new Map(data.map((row) => [row.scientific_name, row]));
  $("#species-list").innerHTML = data
    .map((row) => `<option value="${escapeHtml(row.scientific_name)}">${escapeHtml(row.italian_name)}</option>`)
    .join("");
}

async function refreshCounts() {
  const [pending, rejected, wrongClips] = await Promise.all([
    state.client.from("observations").select("id", { count: "exact", head: true }).eq("status", "pending"),
    state.client.from("observations").select("id", { count: "exact", head: true }).eq("status", "rejected"),
    state.client.from("clips").select("id, observations!inner(status)", { count: "exact", head: true })
      .eq("review", "wrong").neq("observations.status", "rejected"),
  ]);
  $("#pending-count").textContent = pending.count ? String(pending.count) : "";
  const trash = (rejected.count || 0) + (wrongClips.count || 0);
  $("#trash-count").textContent = trash ? String(trash) : "";
}

async function matchingSpecies(text) {
  const needle = text.trim().toLowerCase();
  return [...state.species.values()]
    .filter((row) => [row.scientific_name, row.italian_name, row.english_name]
      .some((name) => (name || "").toLowerCase().includes(needle)))
    .map((row) => row.scientific_name);
}

async function fetchObservations() {
  let query = state.client
    .from("observations")
    .select(OBSERVATION_SELECT)
    .order("day", { ascending: false })
    .order("id", { ascending: false })
    .range(state.offset, state.offset + PAGE_SIZE - 1);

  const status = $("#filter-status").value;
  if (status) query = query.eq("status", status);
  if ($("#filter-from").value) query = query.gte("day", $("#filter-from").value);
  if ($("#filter-to").value) query = query.lte("day", $("#filter-to").value);
  const speciesText = $("#filter-species").value;
  if (speciesText.trim()) query = query.in("scientific_name", await matchingSpecies(speciesText));

  const { data, error } = await query;
  if (error) throw error;
  return data;
}

// changes: { status, corrected }; a missing field is left as it is, "" clears the correction
async function updateObservation(id, changes) {
  const row = { reviewed_at: new Date().toISOString() };
  if (changes.status !== undefined) row.status = changes.status;
  if (changes.corrected !== undefined) row.corrected_scientific_name = changes.corrected.trim() || null;
  const { error } = await state.client.from("observations").update(row).eq("id", id);
  if (error) throw error;
}

async function updateClip(id, review) {
  const { error } = await state.client.from("clips").update({ review }).eq("id", id);
  if (error) throw error;
}

// ---------------------------------------------------------------- observation cards

// where to check a species: icons, the name stays in the tooltip and for screen readers
const SPECIES_LINK_ICONS = {
  wikipedia: `<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15" fill="#fff" stroke="#c8ccd1"/>
    <text x="16" y="22" text-anchor="middle" font-family="Georgia, 'Times New Roman', serif" font-size="17" fill="#202122">W</text></svg>`,
  inaturalist: `<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15" fill="#74ac00"/>
    <text x="16" y="21" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="#fff">iN</text></svg>`,
};

// "Pica pica  W iN  status" over "Gazza · Eurasian Magpie"
function speciesHeaderHtml(observation, badge) {
  const species = observation.species || {};
  const names = [species.italian_name, species.english_name].filter(Boolean).map(escapeHtml).join(" · ");
  return `
      <header>
        <div class="title-row">
          <h2><i>${escapeHtml(observation.scientific_name)}</i></h2>
          ${speciesLinks(observation.scientific_name)}
          <span class="badge">${badge}</span>
        </div>
        ${names ? `<p class="common-names">${names}</p>` : ""}
      </header>`;
}

function speciesLinks(scientificName) {
  const name = String(scientificName || "").trim();
  if (!name) return "";
  const links = [
    ["wikipedia", `https://it.wikipedia.org/wiki/${encodeURIComponent(name.replace(/ /g, "_"))}`, `${name} su Wikipedia`],
    ["inaturalist", `https://www.inaturalist.org/taxa/search?q=${encodeURIComponent(name)}`, `${name} su iNaturalist`],
  ];
  return `<span class="species-links">${links
    .map(([icon, url, label]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener"
      title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${SPECIES_LINK_ICONS[icon]}</a>`)
    .join("")}</span>`;
}

// ---------------------------------------------------------------- clip player

const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 2h4v2H4V5h4l1-2zM6 9h12l-1 12H7L6 9zm4 2v8h2v-8h-2zm4 0v8h2v-8h-2z"/></svg>';
const SKIP_SECONDS = 2;
const BAND_PADDING_SECONDS = 0.5;

function formatSeconds(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

function percentOf(seconds, duration) {
  return `${(Math.min(Math.max(seconds / duration, 0), 1) * 100).toFixed(3)}%`;
}

function clipSegments(clip) {
  return Array.isArray(clip.segments) ? clip.segments : [];
}

function isTrimmed(clip) {
  return clip.trim_start_s != null && clip.trim_end_s != null;
}

// the clip object (and its observation) behind a figure, from what the page loaded
function findClip(clipId) {
  for (const observation of state.observations.values()) {
    const clip = (observation.clips || []).find((item) => item.id === clipId);
    if (clip) return { clip, observation, observationId: observation.id };
  }
  for (const [observationId, entry] of state.trash.entries()) {
    const clip = entry.clips.find((item) => item.id === clipId);
    if (clip) return { clip, observation: null, observationId };
  }
  return null;
}

function clipHtml(clip, folded = false) {
  if (clip.media_deleted_at) {
    // a week after the iNaturalist upload the files leave Supabase (the PC's uploader removes them)
    return `
    <figure class="clip media-gone${folded ? " folded" : ""}" data-clip="${escapeHtml(clip.id)}">
      <p class="media-gone-note">Audio tolto da Supabase il ${new Date(clip.media_deleted_at).toLocaleDateString("it-IT")}:
        è su iNaturalist.</p>
      <figcaption><span>${timeFormat.format(new Date(clip.recorded_at))} · clip_conf ${Number(clip.confidence).toFixed(3)}
        · ${Math.round(clip.duration_s || 0)} s</span></figcaption>
    </figure>`;
  }
  const urls = mediaUrls(clip.media_path);
  const duration = Number(clip.duration_s) || 1;
  const recorded = timeFormat.format(new Date(clip.recorded_at));
  const segments = clipSegments(clip);
  const trimmed = isTrimmed(clip);
  return `
    <figure class="clip review-${clip.review}${folded ? " folded" : ""}" data-clip="${escapeHtml(clip.id)}" data-duration="${duration}">
      <div class="spectrogram-box" title="Tocca per ascoltare da lì, trascina in orizzontale per selezionare">
        <img class="spectrogram" src="${escapeHtml(urls.spectrogram)}" alt="Spettrogramma" loading="lazy" draggable="false">
        ${segments.map(([start, end, confidence]) => `<span class="band" style="left:${percentOf(start, duration)};width:${percentOf(end - start, duration)};--strength:${confidence}"
          title="BirdNET ${Number(confidence).toFixed(2)} · ${Number(start).toFixed(1)}–${Number(end).toFixed(1)} s"></span>`).join("")}
        ${trimmed ? `<span class="trim-shade" style="left:0;width:${percentOf(clip.trim_start_s, duration)}"></span>
          <span class="trim-shade" style="left:${percentOf(clip.trim_end_s, duration)};right:0"></span>` : ""}
        <span class="selection hidden"></span>
        <span class="playhead"></span>
      </div>
      <audio preload="none" crossorigin="anonymous" src="${escapeHtml(urls.audio)}"></audio>
      <div class="player">
        <button type="button" class="play-button" data-player="toggle" aria-label="Riproduci">${ICON_PLAY}</button>
        <span class="player-time">0:00 / ${formatSeconds(duration)}</span>
        <button type="button" class="small" data-player="back" title="Indietro di ${SKIP_SECONDS} secondi">−${SKIP_SECONDS} s</button>
        <button type="button" class="small" data-player="speed" title="Rallenta per sentire i dettagli">1×</button>
        <button type="button" class="small" data-player="loop" title="Ripeti in continuazione">Ripeti</button>
      </div>
      ${segments.length ? `<div class="band-chips" aria-label="Dove BirdNET la sente">${segments.map(([start, end, confidence], index) => `
        <button type="button" class="chip" data-band="${index}" title="BirdNET ${Number(confidence).toFixed(2)}">▶ ${Number(start).toFixed(1)}–${Number(end).toFixed(1)} s</button>`).join("")}</div>` : ""}
      <div class="selection-actions hidden">
        <span class="selection-label"></span>
        <button type="button" class="small" data-player="play-selection">Ascolta selezione</button>
        <button type="button" class="small primary" data-player="trim">Taglia qui</button>
        <button type="button" class="small" data-player="clear-selection">Annulla</button>
      </div>
      <figcaption>
        <span>${recorded} · clip_conf ${Number(clip.confidence).toFixed(3)} · ${Math.round(clip.duration_s || 0)} s${trimmed
          ? ` · tagliata ${Number(clip.trim_start_s).toFixed(1)}–${Number(clip.trim_end_s).toFixed(1)} s
            <button type="button" class="link-button" data-player="untrim">togli taglio</button>` : ""}</span>
        <span class="clip-actions">
          <button type="button" class="small ${clip.review === "wrong" ? "on" : ""}" data-clip-review="wrong"
            title="Questa clip non contiene la specie: finisce nel Cestino">${clip.review === "wrong" ? "Ripristina clip" : "Scarta clip"}</button>
          <button type="button" class="icon-button trash-button" data-delete-clip
            title="Elimina definitivamente questa clip" aria-label="Elimina definitivamente questa clip">${ICON_TRASH}</button>
        </span>
      </figcaption>
    </figure>`;
}

function observationHtml(observation) {
  const facts = observationFacts(observation);
  const clips = facts.clips;
  const first = facts.firstHeard ? timeFormat.format(new Date(facts.firstHeard)) : "";
  const last = clips.length > 1 ? `–${timeFormat.format(new Date(facts.lastHeard))}` : "";
  const corrected = observation.corrected_scientific_name
    ? `<p class="corrected">Corretta in <i>${escapeHtml(observation.corrected_scientific_name)}</i>
        ${speciesLinks(observation.corrected_scientific_name)}</p>`
    : "";
  return `
    <article class="observation status-${observation.status}" data-id="${observation.id}">
      ${speciesHeaderHtml(observation, STATUS_LABELS[observation.status] || observation.status)}
      <p class="meta">${formatDay(observation.day)} · ${first}${last} · ${escapeHtml(facts.place)} ·
        max_obs_conf ${facts.maxConfidence.toFixed(3)} · ${clips.length} clip</p>
      ${corrected}
      <div class="clips">${clips.map((clip, index) => clipHtml(clip, index >= VISIBLE_CLIPS)).join("")}</div>
      ${clips.length > VISIBLE_CLIPS
        ? `<button type="button" class="show-clips" data-show-clips>Mostra altre ${clips.length - VISIBLE_CLIPS} clip</button>`
        : ""}
      <footer>
        <label>Specie corretta <input data-field="corrected" list="species-list" placeholder="se BirdNET ha sbagliato"
          value="${escapeHtml(observation.corrected_scientific_name || "")}"></label>
        <div class="actions">
          <button type="button" data-status="confirmed" class="primary">Conferma</button>
          <button type="button" data-status="rejected">Scarta</button>
          ${observation.status !== "pending" ? '<button type="button" data-status="pending">Rimetti da rivedere</button>' : ""}
          ${observation.status === "confirmed" ? '<button type="button" class="inat" data-inat-upload>Carica su iNaturalist</button>' : ""}
          ${observation.inat_observation_id
            ? `<a class="inat-link" href="https://www.inaturalist.org/observations/${Number(observation.inat_observation_id)}"
                target="_blank" rel="noopener">Vedi su iNaturalist</a>`
            : ""}
        </div>
      </footer>
    </article>`;
}

content.addEventListener("click", async (event) => {
  const card = event.target.closest(".observation");
  if (!card) return;
  const id = Number(card.dataset.id);

  const showClips = event.target.closest("[data-show-clips]");
  if (showClips) {
    card.querySelectorAll(".clip.folded").forEach((clip) => clip.classList.remove("folded"));
    showClips.remove();
    return;
  }

  const clipButton = event.target.closest("[data-clip-review]");
  if (clipButton) {
    const figure = clipButton.closest(".clip");
    const value = clipButton.classList.contains("on") ? "unreviewed" : "wrong";
    try {
      await updateClip(figure.dataset.clip, value);
      figure.classList.toggle("review-wrong", value === "wrong");
      clipButton.classList.toggle("on", value === "wrong");
      clipButton.textContent = value === "wrong" ? "Ripristina clip" : "Scarta clip";
      if (state.view === "trash" && value !== "wrong") figure.remove();
      refreshCounts();
    } catch (error) {
      setStatus(`Errore: ${error.message}`, true);
    }
    return;
  }

  const inatButton = event.target.closest("[data-inat-upload]");
  if (inatButton) {
    const observation = state.observations.get(id);
    if (!observation) return;
    inatButton.disabled = true;
    try {
      await uploadObservationToInat(observation, (step) => setStatus(`iNaturalist: ${step}…`));
      setStatus("Caricata su iNaturalist.");
      // now "caricata": it leaves a list filtered on another status (e.g. the confirmed ones)
      if (leavesFilteredView("uploaded")) removeCard(card);
      else card.outerHTML = observationHtml(observation);
      refreshCounts();
    } catch (error) {
      inatButton.disabled = false;
      setStatus(`iNaturalist: ${error.message}`, true);
    }
    return;
  }

  const deleteClipButton = event.target.closest("[data-delete-clip]");
  if (deleteClipButton) {
    const figure = deleteClipButton.closest(".clip");
    const found = findClip(figure.dataset.clip);
    if (!found) return;
    figure.querySelector("audio").pause();
    const deleted = await deleteForGood([{ observationId: found.observationId, whole: false, clips: [found.clip] }],
      { ask: false });
    if (deleted && state.view !== "trash") {
      if (found.observation) found.observation.clips = found.observation.clips.filter((clip) => clip.id !== found.clip.id);
      figure.remove();
      if (!card.querySelector(".clip")) card.remove(); // it was its last clip: the observation is gone too
    }
    return;
  }

  const deleteButton = event.target.closest("[data-delete]");
  if (deleteButton) {
    const entry = state.trash.get(id);
    if (entry) await deleteForGood([{ observationId: id, ...entry }]);
    return;
  }

  const statusButton = event.target.closest("[data-status]");
  if (statusButton) {
    const changes = { status: statusButton.dataset.status };
    const corrected = card.querySelector('[data-field="corrected"]');
    if (corrected) changes.corrected = corrected.value; // "" clears a previous correction
    try {
      await updateObservation(id, changes);
      setStatus(`Osservazione ${STATUS_LABELS[changes.status]}.`);
      if (leavesFilteredView(changes.status)) {
        removeCard(card);
      } else {
        card.className = `observation status-${changes.status}`;
        card.querySelector(".badge").textContent = STATUS_LABELS[changes.status];
      }
      refreshCounts();
    } catch (error) {
      setStatus(`Errore: ${error.message}`, true);
    }
  }
});

// a card leaves only when its new status no longer matches the list it is in: the status you
// are filtering on, or the Cestino (rejected only)
function leavesFilteredView(newStatus) {
  const statusFilter = $("#filter-status").value;
  return (state.view === "observations" && Boolean(statusFilter) && newStatus !== statusFilter)
    || (state.view === "trash" && newStatus !== "rejected");
}

function removeCard(card) {
  card.querySelectorAll("audio").forEach((audio) => audio.pause());
  card.classList.add("leaving");
  setTimeout(() => card.remove(), 250);
}

// one clip at a time; every playback runs over a range: the kept part (trim), a BirdNET band,
// the selection or from a tapped point; "Ripeti" loops over it
const players = new WeakMap();

function playerState(figure) {
  if (!players.has(figure)) players.set(figure, { range: null, loop: false, selection: null });
  return players.get(figure);
}

function keptRange(figure) {
  const found = findClip(figure.dataset.clip);
  const duration = Number(figure.dataset.duration) || 0;
  if (found && isTrimmed(found.clip)) return { start: Number(found.clip.trim_start_s), end: Number(found.clip.trim_end_s) };
  return { start: 0, end: duration };
}

function seekWhenReady(audio, seconds) {
  if (audio.readyState >= 1) {
    audio.currentTime = seconds;
  } else {
    audio.addEventListener("loadedmetadata", () => { audio.currentTime = seconds; }, { once: true });
  }
}

// ---------------------------------------------------------------- listening level

// Clips recorded until October 2026 sit 40-50 dB under full scale (system mic volume at 45%, far
// birds). Each clip plays through Web Audio with the gain that brings its peak to -1 dB, at most
// +30 dB: the uploader's rule for new MP3s, which so stay as they are. The peak is measured once
// per clip, decoding its MP3 (then the <audio> element reads it from the browser cache).
const LISTENING_PEAK_DB = -1;
const LISTENING_MAX_GAIN_DB = 30;
let listeningContext = null;
const listeningGains = new WeakMap(); // <audio> -> Promise of its gain in dB (null: plays as it is)

function audioContext() {
  if (!listeningContext) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    listeningContext = new Context();
    // iPhone: keep playing with the silent switch on, as the plain <audio> element does
    if (navigator.audioSession) navigator.audioSession.type = "playback";
  }
  return listeningContext;
}

async function peakOf(url, context) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`audio non scaricabile (${response.status})`);
  const buffer = await context.decodeAudioData(await response.arrayBuffer());
  let peak = 0;
  for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
    const samples = buffer.getChannelData(channel);
    for (let index = 0; index < samples.length; index += 1) {
      const value = Math.abs(samples[index]);
      if (value > peak) peak = value;
    }
  }
  return peak;
}

function listeningGain(audio) {
  if (!listeningGains.has(audio)) {
    listeningGains.set(audio, (async () => {
      const context = audioContext();
      if (!context) return null; // no Web Audio: plain playback
      const peak = await peakOf(audio.src, context);
      const gainDb = peak > 0 ? Math.min(LISTENING_PEAK_DB - 20 * Math.log10(peak), LISTENING_MAX_GAIN_DB) : 0;
      const gain = context.createGain();
      gain.gain.value = 10 ** (gainDb / 20);
      context.createMediaElementSource(audio).connect(gain).connect(context.destination);
      return gainDb;
    })().catch((error) => {
      console.warn("Volume della clip non calcolato, la riproduco così com'è:", error);
      return null;
    }));
  }
  return listeningGains.get(audio);
}

async function playRange(figure, start, end) {
  const audio = figure.querySelector("audio");
  playerState(figure).range = { start, end };
  state.currentFigure = figure;
  audioContext()?.resume(); // inside the tap: browsers start audio only on a user gesture
  await listeningGain(audio);
  seekWhenReady(audio, start);
  audio.play().catch((error) => setStatus(`Audio: ${error.message}`, true));
}

function togglePlayback(figure) {
  const audio = figure.querySelector("audio");
  state.currentFigure = figure;
  if (!audio.paused) {
    audio.pause();
    return;
  }
  const kept = keptRange(figure);
  const range = playerState(figure).range || kept;
  const resume = audio.currentTime > range.start && audio.currentTime < range.end - 0.05 ? audio.currentTime : range.start;
  playRange(figure, resume, range.end);
  playerState(figure).range = range;
}

function skip(figure, seconds) {
  const audio = figure.querySelector("audio");
  const kept = keptRange(figure);
  audio.currentTime = Math.min(Math.max(audio.currentTime + seconds, kept.start), kept.end);
}

content.addEventListener("play", (event) => {
  document.querySelectorAll("#content audio").forEach((audio) => {
    if (audio !== event.target) audio.pause();
  });
  const figure = event.target.closest(".clip");
  figure?.classList.add("playing");
  if (figure) figure.querySelector(".play-button").innerHTML = ICON_PAUSE;
}, true);

for (const type of ["pause", "ended"]) {
  content.addEventListener(type, (event) => {
    const figure = event.target.closest(".clip");
    if (!figure) return;
    const player = playerState(figure);
    if (type === "ended" && player.loop && player.range) {
      playRange(figure, player.range.start, player.range.end);
      return;
    }
    figure.classList.remove("playing");
    figure.querySelector(".play-button").innerHTML = ICON_PLAY;
  }, true);
}

content.addEventListener("timeupdate", (event) => {
  const audio = event.target;
  const figure = audio.closest(".clip");
  if (!figure) return;
  const duration = Number(figure.dataset.duration) || audio.duration || 1;
  figure.style.setProperty("--progress", String(Math.min(audio.currentTime / duration, 1)));
  figure.querySelector(".player-time").textContent = `${formatSeconds(audio.currentTime)} / ${formatSeconds(duration)}`;
  const player = playerState(figure);
  if (player.range && audio.currentTime >= player.range.end) {
    if (player.loop) {
      audio.currentTime = player.range.start;
    } else {
      audio.pause();
      audio.currentTime = player.range.start;
    }
  }
}, true);

// spectrogram: a tap plays from there, a horizontal drag selects a part
let drag = null;

function fractionAt(box, clientX) {
  const rect = box.getBoundingClientRect();
  return Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
}

function showSelection(figure, start, end) {
  const duration = Number(figure.dataset.duration) || 1;
  const mark = figure.querySelector(".selection");
  mark.classList.remove("hidden");
  mark.style.left = percentOf(start, duration);
  mark.style.width = percentOf(end - start, duration);
}

function clearSelection(figure) {
  playerState(figure).selection = null;
  figure.querySelector(".selection").classList.add("hidden");
  figure.querySelector(".selection-actions").classList.add("hidden");
}

content.addEventListener("pointerdown", (event) => {
  const box = event.target.closest(".spectrogram-box");
  if (!box || event.button > 0) return;
  drag = { box, figure: box.closest(".clip"), x: event.clientX, from: fractionAt(box, event.clientX), moved: false };
  box.setPointerCapture(event.pointerId);
});

content.addEventListener("pointermove", (event) => {
  if (!drag) return;
  if (!drag.moved && Math.abs(event.clientX - drag.x) < 8) return;
  drag.moved = true;
  const duration = Number(drag.figure.dataset.duration) || 1;
  const to = fractionAt(drag.box, event.clientX);
  showSelection(drag.figure, Math.min(drag.from, to) * duration, Math.max(drag.from, to) * duration);
});

content.addEventListener("pointerup", (event) => {
  if (!drag) return;
  const { figure, box, from, moved } = drag;
  drag = null;
  const duration = Number(figure.dataset.duration) || 1;
  if (!moved) {
    const kept = keptRange(figure);
    const at = Math.min(Math.max(from * duration, kept.start), kept.end);
    clearSelection(figure);
    playRange(figure, at, kept.end);
    return;
  }
  const to = fractionAt(box, event.clientX);
  const start = Math.min(from, to) * duration;
  const end = Math.max(from, to) * duration;
  if (end - start < 0.3) {
    clearSelection(figure);
    return;
  }
  playerState(figure).selection = { start, end };
  showSelection(figure, start, end);
  figure.querySelector(".selection-label").textContent = `${start.toFixed(1)}–${end.toFixed(1)} s`;
  figure.querySelector(".selection-actions").classList.remove("hidden");
});

content.addEventListener("pointercancel", () => { drag = null; });

async function saveTrim(figure, start, end) {
  const found = findClip(figure.dataset.clip);
  if (!found) return;
  const values = { trim_start_s: start == null ? null : Number(start.toFixed(2)), trim_end_s: end == null ? null : Number(end.toFixed(2)) };
  const { error } = await state.client.from("clips").update(values).eq("id", found.clip.id);
  if (error) throw error;
  Object.assign(found.clip, values);
  const folded = figure.classList.contains("folded");
  figure.querySelector("audio").pause();
  figure.outerHTML = clipHtml(found.clip, folded);
  setStatus(start == null ? "Taglio tolto." : `Clip tagliata: ${values.trim_start_s}–${values.trim_end_s} s (il file resta intero).`);
}

content.addEventListener("click", async (event) => {
  const figure = event.target.closest(".clip");
  if (!figure) return;
  const control = event.target.closest("[data-player]");
  const band = event.target.closest("[data-band]");
  if (band) {
    const found = findClip(figure.dataset.clip);
    const [start, end] = clipSegments(found.clip)[Number(band.dataset.band)];
    const kept = keptRange(figure);
    playRange(figure, Math.max(kept.start, start - BAND_PADDING_SECONDS), Math.min(kept.end, end + BAND_PADDING_SECONDS));
    return;
  }
  if (!control) return;
  const audio = figure.querySelector("audio");
  const player = playerState(figure);
  try {
    switch (control.dataset.player) {
      case "toggle":
        togglePlayback(figure);
        break;
      case "back":
        skip(figure, -SKIP_SECONDS);
        break;
      case "speed":
        audio.playbackRate = audio.playbackRate === 1 ? 0.5 : 1;
        control.textContent = audio.playbackRate === 1 ? "1×" : "½×";
        control.classList.toggle("on", audio.playbackRate !== 1);
        break;
      case "loop":
        player.loop = !player.loop;
        control.classList.toggle("on", player.loop);
        break;
      case "play-selection":
        if (player.selection) playRange(figure, player.selection.start, player.selection.end);
        break;
      case "clear-selection":
        clearSelection(figure);
        break;
      case "trim":
        if (player.selection) await saveTrim(figure, player.selection.start, player.selection.end);
        break;
      case "untrim":
        await saveTrim(figure, null, null);
        break;
    }
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
  }
});

// keyboard on a computer: space plays/pauses the last clip used, arrows move by 2 s
document.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, button") || event.metaKey || event.ctrlKey || event.altKey) return;
  const figure = state.currentFigure?.isConnected ? state.currentFigure : content.querySelector(".clip:not(.folded)");
  if (!figure) return;
  if (event.key === " ") {
    event.preventDefault();
    togglePlayback(figure);
  } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    skip(figure, event.key === "ArrowLeft" ? -SKIP_SECONDS : SKIP_SECONDS);
  }
});

async function renderObservations(append = false) {
  if (!append) {
    state.offset = 0;
    content.innerHTML = "";
  }
  setStatus("Carico…");
  const rows = await fetchObservations();
  if (!append) content.innerHTML = ""; // a refresh may have raced with this load
  rows.forEach((row) => state.observations.set(row.id, row));
  content.insertAdjacentHTML("beforeend", rows.map(observationHtml).join(""));
  state.offset += rows.length;
  $("#more").classList.toggle("hidden", rows.length < PAGE_SIZE);
  if (!content.children.length) {
    content.innerHTML = '<p class="empty">Nessuna osservazione.</p>';
  }
  setStatus("");
}

// ---------------------------------------------------------------- Cestino

async function fetchTrash() {
  const [rejected, partial] = await Promise.all([
    state.client.from("observations").select(OBSERVATION_SELECT)
      .eq("status", "rejected").order("day", { ascending: false }).limit(500),
    // kept observations, with only their rejected clips
    state.client.from("observations").select("*, species(italian_name,english_name,german_name), clips!inner(*)")
      .neq("status", "rejected").eq("clips.review", "wrong").order("day", { ascending: false }).limit(500),
  ]);
  if (rejected.error || partial.error) throw rejected.error || partial.error;
  return { rejected: rejected.data, partial: partial.data };
}

function trashCardHtml(observation, whole) {
  const clips = [...(observation.clips || [])].sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
  return `
    <article class="observation trash status-${observation.status}" data-id="${observation.id}">
      ${speciesHeaderHtml(observation, whole ? "osservazione scartata" : `${clips.length} clip scartate`)}
      <p class="meta">${formatDay(observation.day)} · ${escapeHtml(observationFacts(observation).place)} ·
        ${whole ? `tutte le ${clips.length} clip` : "il resto dell'osservazione resta"}</p>
      <div class="clips">${clips.map((clip, index) => clipHtml(clip, index >= VISIBLE_CLIPS)).join("")}</div>
      ${clips.length > VISIBLE_CLIPS
        ? `<button type="button" class="show-clips" data-show-clips>Mostra altre ${clips.length - VISIBLE_CLIPS} clip</button>`
        : ""}
      <footer>
        <div class="actions">
          <button type="button" class="danger" data-delete>Elimina definitivamente</button>
          ${whole ? '<button type="button" data-status="pending">Ripristina</button>' : ""}
        </div>
      </footer>
    </article>`;
}

async function renderTrash() {
  setStatus("Carico…");
  const { rejected, partial } = await fetchTrash();
  state.trash = new Map([
    ...rejected.map((observation) => [observation.id, { whole: true, clips: observation.clips || [] }]),
    ...partial.map((observation) => [observation.id, { whole: false, clips: observation.clips || [] }]),
  ]);
  const clipCount = [...state.trash.values()].reduce((sum, entry) => sum + entry.clips.length, 0);
  content.innerHTML = state.trash.size
    ? `<div class="trash-head">
         <p>${rejected.length} osservazioni scartate e ${partial.reduce((sum, o) => sum + o.clips.length, 0)} clip scartate
           in osservazioni tenute: ${clipCount} clip in tutto. Riascoltale se vuoi, poi eliminale: audio e spettrogrammi
           spariscono da Supabase, i WAV sul PC restano.</p>
         <button type="button" class="danger" data-empty-trash>Svuota il cestino (${clipCount} clip)</button>
       </div>
       ${rejected.map((observation) => trashCardHtml(observation, true)).join("")}
       ${partial.map((observation) => trashCardHtml(observation, false)).join("")}`
    : '<p class="empty">Il cestino è vuoto.</p>';
  setStatus("");
}

function chunks(items, size) {
  const result = [];
  for (let i = 0; i < items.length; i += size) result.push(items.slice(i, i + size));
  return result;
}

// entries: [{ observationId, whole, clips }]; true once deleted. ask: false for the trash button
// of a single clip, which deletes at once.
async function deleteForGood(entries, { ask = true } = {}) {
  const clips = entries.flatMap((entry) => entry.clips);
  const wholeCount = entries.filter((entry) => entry.whole).length;
  const question = `Eliminare definitivamente ${clips.length} clip${wholeCount ? ` (${wholeCount} osservazioni intere)` : ""}?\n\n`
    + "Audio e spettrogrammi vengono cancellati da Supabase e non si possono recuperare dal sito. I WAV sul PC restano.";
  if (!clips.length || (ask && !window.confirm(question))) return false;
  let deleted = false;

  setStatus("Elimino…");
  try {
    const ids = clips.map((clip) => clip.id);
    // 1. first the list `sync` reads: if anything below fails, they are not uploaded again
    for (const part of chunks(ids, 200)) {
      const { error } = await state.client.from("deleted_clips")
        .upsert(part.map((id) => ({ id })), { onConflict: "id", ignoreDuplicates: true });
      if (error) throw error;
    }
    // 2. files in the bucket
    // the files of each clip, from its path (none when the site runs on local files: make web-dev)
    const paths = config.mediaBaseUrl ? [] : clips.flatMap((clip) => [`${clip.media_path}.mp3`, `${clip.media_path}-spectrogram.jpg`]);
    for (const part of chunks(paths, 100)) {
      const { error } = await state.client.storage.from(BUCKET).remove(part);
      if (error) throw error;
    }
    // 3. rows: clips, then whole observations and kept ones left without clips
    for (const part of chunks(ids, 50)) {
      const { error } = await state.client.from("clips").delete().in("id", part);
      if (error) throw error;
    }
    const observationIds = entries.filter((entry) => entry.whole).map((entry) => entry.observationId);
    const partialIds = entries.filter((entry) => !entry.whole).map((entry) => entry.observationId);
    if (partialIds.length) {
      const { data, error } = await state.client.from("clips").select("observation_id").in("observation_id", partialIds);
      if (error) throw error;
      const stillWithClips = new Set(data.map((row) => row.observation_id));
      observationIds.push(...partialIds.filter((id) => !stillWithClips.has(id)));
    }
    for (const part of chunks(observationIds, 100)) {
      const { error } = await state.client.from("observations").delete().in("id", part);
      if (error) throw error;
    }
    setStatus(`Eliminate ${clips.length} clip.`);
    deleted = true;
  } catch (error) {
    setStatus(`Errore durante l'eliminazione: ${error.message}`, true);
  }
  await refreshCounts();
  if (state.view === "trash") await renderTrash();
  return deleted;
}

content.addEventListener("click", (event) => {
  if (!event.target.closest("[data-empty-trash]")) return;
  const entries = [...state.trash.entries()].map(([observationId, entry]) => ({ observationId, ...entry }));
  deleteForGood(entries);
});

// ---------------------------------------------------------------- iNaturalist
// Everything runs in the browser: the iNaturalist API accepts calls from any site, the token is
// the personal one from inaturalist.org/users/api_token (24 hours), Supabase keeps track of what
// is already online (observations.inat_observation_id, clips.inat_sound_id, status "uploaded").

const INAT_API = "https://api.inaturalist.org/v1";
const INAT_TOKEN_KEY = "inatToken";
const INAT_LOGIN_KEY = "inatLogin";
const INAT_GEOPRIVACY_KEY = "inatGeoprivacy";
const INAT_MAX_SOUNDS = 5;
const taxonIds = new Map();

function storageGet(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function storageSet(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // private window: nothing is remembered
  }
}

function tokenExpiry(token) {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.exp ? new Date(payload.exp * 1000) : null;
  } catch {
    return null;
  }
}

function inatToken() {
  const token = storageGet(INAT_TOKEN_KEY);
  const expiry = tokenExpiry(token);
  if (token && expiry && expiry <= new Date()) {
    storageSet(INAT_TOKEN_KEY, "");
    return "";
  }
  return token;
}

async function renderInatPanel() {
  const token = inatToken();
  const expiry = tokenExpiry(token);
  const login = storageGet(INAT_LOGIN_KEY);
  const until = expiry ? ` fino alle ${expiry.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" })}` : "";
  $("#inat-state").textContent = token
    ? `Collegato${login ? ` come ${login}` : ""}${until}.`
    : "Non collegato: serve il token di oggi.";
  $("#inat-state").classList.toggle("error", !token);
  $("#inat-geoprivacy").value = storageGet(INAT_GEOPRIVACY_KEY);
  const { count } = await state.client.from("observations").select("id", { count: "exact", head: true }).eq("status", "confirmed");
  const button = $("#inat-upload-all");
  button.textContent = `Carica le confermate (${count || 0})`;
  button.disabled = !token || !count;
}

async function inatRequest(path, options = {}) {
  const response = await fetch(`${INAT_API}${path}`, {
    ...options,
    headers: { Accept: "application/json", Authorization: `Bearer ${inatToken()}`, ...(options.headers || {}) },
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    if (response.status === 401) {
      storageSet(INAT_TOKEN_KEY, "");
      renderInatPanel();
      throw new Error("token scaduto o non valido: ricollegati dal pannello iNat");
    }
    const detail = data?.error?.original?.error || data?.error?.original?.errors?.[0] || data?.error || text.slice(0, 160);
    throw new Error(`iNaturalist ${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return data;
}

async function resolveTaxonId(scientificName) {
  const name = scientificName.trim();
  if (taxonIds.has(name)) return taxonIds.get(name);
  const response = await fetch(`${INAT_API}/taxa?q=${encodeURIComponent(name)}&rank=species,subspecies&per_page=30`);
  const data = await response.json();
  // Exact name, or a name iNaturalist lists as a synonym (Corvus monedula -> Coloeus monedula).
  // Never "the first result": for Pica pica it is a Brazilian woodpecker ("pica-pau").
  const wanted = name.toLowerCase();
  const results = (data.results || []).filter((item) => item.is_active !== false);
  const taxon = results.find((item) => item.name.toLowerCase() === wanted)
    || results.find((item) => String(item.matched_term || "").toLowerCase() === wanted);
  if (!taxon) throw new Error(`specie "${name}" non trovata su iNaturalist`);
  taxonIds.set(name, taxon.id);
  return taxon.id;
}

// the trimmed part of a clip as a 16-bit mono WAV, built in the browser from its MP3
async function trimmedWav(url, start, end) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`audio non scaricabile (${response.status})`);
  const decoded = await new OfflineAudioContext(1, 1, 48000).decodeAudioData(await response.arrayBuffer());
  const rate = decoded.sampleRate;
  const samples = decoded.getChannelData(0).subarray(Math.floor(start * rate), Math.min(decoded.length, Math.ceil(end * rate)));
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
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
  samples.forEach((sample, index) => view.setInt16(44 + index * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true));
  return new Blob([buffer], { type: "audio/wav" });
}

function localDateTime(iso) {
  // "YYYY-MM-DD HH:MM:SS" in local time, like the CSV the importer used to send
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// observation: a row with species and clips (OBSERVATION_SELECT); updated in place
async function uploadObservationToInat(observation, onProgress = () => {}) {
  if (!inatToken()) throw new Error("collega iNaturalist dal pannello iNat");
  const name = observation.corrected_scientific_name || observation.scientific_name;
  const facts = observationFacts(observation);
  const clips = (observation.clips || [])
    .filter((clip) => clip.review !== "wrong" && !clip.media_deleted_at)
    .sort((a, b) => b.confidence - a.confidence)
    .slice(0, INAT_MAX_SOUNDS);
  if (!clips.length) throw new Error("nessuna clip da caricare (sono tutte scartate)");

  if (!observation.inat_observation_id) {
    onProgress(`creo l'osservazione di ${name}`);
    const taxonId = await resolveTaxonId(name);
    const fields = {
      taxon_id: taxonId,
      species_guess: name,
      observed_on_string: localDateTime(facts.firstHeard),
      place_guess: facts.place,
      description: `BirdNET confidence ${facts.maxConfidence.toFixed(3)}`,
      tag_list: "audio,birdnet",
    };
    if (facts.latitude != null && facts.longitude != null) {
      fields.latitude = facts.latitude;
      fields.longitude = facts.longitude;
    }
    const geoprivacy = storageGet(INAT_GEOPRIVACY_KEY);
    if (geoprivacy) fields.geoprivacy = geoprivacy;
    const created = await inatRequest("/observations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ observation: fields }),
    });
    const record = Array.isArray(created) ? created[0] : created?.results?.[0] || created;
    if (!record?.id) throw new Error("iNaturalist non ha restituito l'id dell'osservazione");
    // saved at once: if the sounds fail, the next attempt reuses this observation
    const { error } = await state.client.from("observations")
      .update({ inat_observation_id: record.id, inat_uuid: record.uuid || null })
      .eq("id", observation.id);
    if (error) throw error;
    observation.inat_observation_id = record.id;
    observation.inat_uuid = record.uuid || null;
  }

  for (const [index, clip] of clips.entries()) {
    if (clip.inat_sound_id != null) continue; // already online
    onProgress(`${name}: suono ${index + 1} di ${clips.length}`);
    const form = new FormData();
    form.append("observation_sound[observation_id]", String(observation.inat_observation_id));
    if (isTrimmed(clip)) {
      // only the part kept on the site
      const wav = await trimmedWav(mediaUrls(clip.media_path).audio, Number(clip.trim_start_s), Number(clip.trim_end_s));
      form.append("file", wav, clip.id);
    } else {
      const audio = await fetch(mediaUrls(clip.media_path).audio);
      if (!audio.ok) throw new Error(`audio non scaricabile (${audio.status})`);
      form.append("file", await audio.blob(), clip.id.replace(/\.wav$/i, ".mp3"));
    }
    const sound = await inatRequest("/observation_sounds", { method: "POST", body: form });
    const soundId = Array.isArray(sound) ? sound[0]?.id : sound?.id;
    const { error } = await state.client.from("clips").update({ inat_sound_id: soundId || 0 }).eq("id", clip.id);
    if (error) throw error;
    clip.inat_sound_id = soundId || 0;
  }

  const { error } = await state.client.from("observations")
    // uploaded_at: a week later the PC's uploader removes the files from Supabase
    .update({ status: "uploaded", reviewed_at: new Date().toISOString(), uploaded_at: new Date().toISOString() })
    .eq("id", observation.id);
  if (error) throw error;
  observation.status = "uploaded";
  return observation.inat_observation_id;
}

$("#inat-toggle").addEventListener("click", () => {
  const panel = $("#inat-panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    renderInatPanel().catch((error) => setStatus(`Errore: ${error.message}`, true));
  }
});

$("#inat-geoprivacy").addEventListener("change", (event) => storageSet(INAT_GEOPRIVACY_KEY, event.target.value));

$("#inat-token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  let token = $("#inat-token").value.trim();
  try {
    token = JSON.parse(token).api_token || token; // the page shows {"api_token":"..."}
  } catch {
    // pasted the bare token
  }
  if (!token) return;
  setStatus("Controllo il token…");
  const response = await fetch(`${INAT_API}/users/me`, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) {
    setStatus("Token non valido: copialo di nuovo dalla pagina del token.", true);
    return;
  }
  const me = await response.json();
  storageSet(INAT_TOKEN_KEY, token);
  storageSet(INAT_LOGIN_KEY, me.results?.[0]?.login || "");
  $("#inat-token").value = "";
  setStatus("iNaturalist collegato.");
  renderInatPanel();
});

$("#inat-upload-all").addEventListener("click", async () => {
  const button = $("#inat-upload-all");
  const log = $("#inat-log");
  button.disabled = true;
  log.innerHTML = "";
  try {
    const { data, error } = await state.client.from("observations").select(OBSERVATION_SELECT)
      .eq("status", "confirmed").order("day").limit(200);
    if (error) throw error;
    let done = 0;
    for (const [index, observation] of data.entries()) {
      const label = `${observation.species?.italian_name || observation.scientific_name}, ${formatDay(observation.day)}`;
      try {
        await uploadObservationToInat(observation, (step) => setStatus(`iNaturalist ${index + 1}/${data.length}: ${step}…`));
        done += 1;
        log.insertAdjacentHTML("beforeend", `<li>✓ <a href="https://www.inaturalist.org/observations/${Number(observation.inat_observation_id)}"
          target="_blank" rel="noopener">${escapeHtml(label)}</a></li>`);
      } catch (uploadError) {
        log.insertAdjacentHTML("beforeend", `<li class="error">✗ ${escapeHtml(label)}: ${escapeHtml(uploadError.message)}</li>`);
        if (/token/.test(uploadError.message)) break; // no point going on without a valid token
      }
    }
    setStatus(`iNaturalist: caricate ${done} di ${data.length}.`, done < data.length);
  } catch (error) {
    setStatus(`iNaturalist: ${error.message}`, true);
  }
  await refreshCounts();
  await renderInatPanel();
  if (state.view !== "report" && state.view !== "trash") renderObservations();
});

// ---------------------------------------------------------------- report

function isoDaysAgo(days) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toISOString().slice(0, 10);
}

async function renderReport(days = Number(localStorage.getItem("reportDays") || 7)) {
  setStatus("Carico…");
  const from = isoDaysAgo(days - 1);
  // a species name opens its observations of exactly this period
  const speciesLink = (row) => `<button type="button" class="species-link" data-species="${escapeHtml(row.scientific_name)}"
    data-from="${from}" title="Vedi le osservazioni di questo periodo">${escapeHtml(row.italian_name)}</button>`;
  const [{ data: rows, error }, { data: clips, error: clipsError }] = await Promise.all([
    state.client.from("daily_summary").select("*").gte("day", from).neq("status", "rejected"),
    state.client.from("clips").select("recorded_at, observations!inner(status,day)")
      .gte("observations.day", from).neq("observations.status", "rejected"),
  ]);
  if (error || clipsError) throw error || clipsError;

  const bySpecies = new Map();
  for (const row of rows) {
    const entry = bySpecies.get(row.scientific_name) || { ...row, days: 0, clips: 0, seconds: 0, best: 0 };
    entry.days += 1;
    entry.clips += row.clips;
    entry.seconds += Number(row.seconds);
    entry.best = Math.max(entry.best, Number(row.max_confidence));
    bySpecies.set(row.scientific_name, entry);
  }
  const speciesRows = [...bySpecies.values()].sort((a, b) => b.days - a.days || b.clips - a.clips);
  const newSpecies = speciesRows.filter((row) => (state.species.get(row.scientific_name)?.first_seen_date || "") >= from);
  const hours = Array.from({ length: 24 }, () => 0);
  for (const clip of clips) hours[new Date(clip.recorded_at).getHours()] += 1;
  const maxHour = Math.max(1, ...hours);
  const totalMinutes = Math.round(speciesRows.reduce((sum, row) => sum + row.seconds, 0) / 60);

  content.innerHTML = `
    <div class="report-head">
      <h2>Ultimi ${days} giorni</h2>
      <div class="segmented">
        ${[7, 30, 90].map((n) => `<button type="button" data-days="${n}" class="${n === days ? "on" : ""}">${n} gg</button>`).join("")}
      </div>
    </div>
    <div class="tiles">
      <div class="tile"><b>${speciesRows.length}</b><span>specie</span></div>
      <div class="tile"><b>${rows.length}</b><span>osservazioni</span></div>
      <div class="tile"><b>${clips.length}</b><span>clip</span></div>
      <div class="tile"><b>${totalMinutes}</b><span>minuti di audio</span></div>
      <div class="tile"><b>${newSpecies.length}</b><span>specie nuove</span></div>
    </div>
    ${newSpecies.length ? `<p class="new-species">Prima volta: ${newSpecies.map(speciesLink).join(", ")}</p>` : ""}
    <h3>Attività per ora</h3>
    <div class="hours">${hours.map((count, hour) => `
      <div class="hour" title="${hour}:00 · ${count} clip">
        <span class="bar" style="height:${Math.round((count / maxHour) * 100)}%"></span>
        <span class="label">${hour}</span>
      </div>`).join("")}</div>
    <h3>Specie</h3>
    <div class="table-scroll"><table class="report-table">
      <thead><tr><th>Specie</th><th>Giorni</th><th>Clip</th><th>Conf. max</th><th>Ultima volta</th></tr></thead>
      <tbody>${speciesRows.map((row) => `
        <tr>
          <td>${speciesLink(row)} <small><i>${escapeHtml(row.scientific_name)}</i></small></td>
          <td>${row.days}</td><td>${row.clips}</td><td>${row.best.toFixed(3)}</td>
          <td>${row.last_heard ? dayFormat.format(new Date(row.last_heard)) : ""}</td>
        </tr>`).join("")}</tbody>
    </table></div>`;
  setStatus("");
}

content.addEventListener("click", (event) => {
  const speciesButton = event.target.closest("[data-species]");
  if (speciesButton) {
    showSpeciesObservations(speciesButton.dataset.species, speciesButton.dataset.from);
    return;
  }
  const button = event.target.closest("[data-days]");
  if (!button) return;
  localStorage.setItem("reportDays", button.dataset.days);
  renderReport(Number(button.dataset.days)).catch((error) => setStatus(`Errore: ${error.message}`, true));
});

function showSpeciesObservations(scientificName, fromDay) {
  $("#filter-status").value = "";
  $("#filter-species").value = scientificName;
  $("#filter-from").value = fromDay;
  $("#filter-to").value = new Date().toISOString().slice(0, 10);
  $("#filters").open = true;
  state.view = "observations";
  window.scrollTo(0, 0);
  render();
}

// ---------------------------------------------------------------- navigation

async function render() {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === state.view));
  $("#filters").classList.toggle("hidden", state.view !== "observations");
  $("#more").classList.add("hidden");
  try {
    if (state.view === "report") await renderReport();
    else if (state.view === "trash") await renderTrash();
    else await renderObservations();
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
  }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  state.view = tab.dataset.view;
  render();
}));

let filterTimer = null;
document.querySelectorAll("#filters input, #filters select").forEach((input) => input.addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(render, 300);
}));

$("#more").addEventListener("click", () => renderObservations(true).catch((error) => setStatus(`Errore: ${error.message}`, true)));

$("#refresh").addEventListener("click", async () => {
  try {
    await loadSpecies();
    await refreshCounts();
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
    return;
  }
  render();
});

// filters open on wide screens, folded away on the phone
$("#filters").open = window.matchMedia("(min-width: 721px)").matches;

async function start() {
  try {
    await loadSpecies();
    await refreshCounts();
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
    return;
  }
  render();
}

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("./service-worker.js", { scope: "./" }).catch((error) => {
    console.warn("Service worker non registrato", error);
  });
}

state.client = createClient();
if (!state.client) {
  setStatus("Configurazione mancante: compila assets/config.js (URL e chiave publishable di Supabase).", true);
} else {
  start();
}
