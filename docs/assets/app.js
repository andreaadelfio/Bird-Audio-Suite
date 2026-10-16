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

// the live keeps everything from 0.6; from 0.8 an identification is "likely", below "unsure"
const LIKELY_CONFIDENCE = 0.8;

function confidenceClass(confidence) {
  return Number(confidence) >= LIKELY_CONFIDENCE ? "likely" : "unsure";
}

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
    // clips placed by hand have no score for this species (confidence 0): they never raise it
    maxConfidence: clips.reduce((best, clip) => Math.max(best, Number(clip.confidence) || 0), 0),
    byHandOnly: clips.length > 0 && clips.every((clip) => clip.manual),
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
const hourFormat = new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit" });
const dayFormat = new Intl.DateTimeFormat("it-IT", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const dayThisYearFormat = new Intl.DateTimeFormat("it-IT", { weekday: "short", day: "numeric", month: "short" });
const shortDateFormat = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short" });

// "mer 14 ott", with the year only when it is not this one
function formatDay(isoDay) {
  const date = new Date(`${isoDay}T12:00:00`);
  return (date.getFullYear() === new Date().getFullYear() ? dayThisYearFormat : dayFormat).format(date);
}

// "Berlin, Berlin, Germany" -> "Berlin, Germany"
function placeLabel(place) {
  const parts = String(place || "").split(",").map((part) => part.trim()).filter(Boolean);
  return parts.filter((part, index) => index === 0 || part.toLowerCase() !== parts[index - 1].toLowerCase()).join(", ");
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
  let { data, error } = await state.client
    .from("species_stats")
    .select("scientific_name,italian_name,english_name,first_seen_date,ebird_code,inat_taxon_id");
  if (error) {
    // the database without the eBird / iNaturalist codes yet (schema not re-run): just the names
    ({ data, error } = await state.client.from("species_stats").select("scientific_name,italian_name,english_name,first_seen_date"));
  }
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
  $("#pending-pill").textContent = pending.count ? String(pending.count) : "";
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

// likely: at least one clip >= 0.8; unsure: none (an anti-join on a second, aliased embedding,
// so the observation still brings all its clips)
function withConfidenceFilter(query) {
  const confidence = $("#filter-confidence").value;
  if (confidence === "likely") return query.gte("strong.confidence", LIKELY_CONFIDENCE).not("strong", "is", null);
  if (confidence === "unsure") return query.gte("strong.confidence", LIKELY_CONFIDENCE).is("strong", null);
  return query;
}

function observationSelect() {
  return $("#filter-confidence").value ? `${OBSERVATION_SELECT}, strong:clips(id)` : OBSERVATION_SELECT;
}

async function fetchObservations() {
  let query = state.client
    .from("observations")
    .select(observationSelect())
    .order("day", { ascending: false })
    .order("id", { ascending: false })
    .range(state.offset, state.offset + PAGE_SIZE - 1);

  const status = $("#filter-status").value;
  if (status) query = query.eq("status", status);
  query = withConfidenceFilter(query);
  if ($("#filter-from").value) query = query.gte("day", $("#filter-from").value);
  if ($("#filter-to").value) query = query.lte("day", $("#filter-to").value);
  const speciesText = $("#filter-species").value;
  if (speciesText.trim()) {
    // BirdNET's species or your correction (the one the report counts)
    const names = (await matchingSpecies(speciesText)).map((name) => `"${name}"`).join(",");
    query = query.or(`scientific_name.in.(${names}),corrected_scientific_name.in.(${names})`);
  }

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
  ebird: `<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15" fill="#2a6ebb"/>
    <text x="16" y="21" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="#fff">eB</text></svg>`,
};
const ICON_NOTE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z"/></svg>';
const ICON_LINK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z"/></svg>';
const ICON_UNLINK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 7h-4v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10zM2 4.27 4.73 7A5 5 0 0 0 7 17h4v-1.9H7a3.1 3.1 0 0 1-2.53-4.88L6.56 12.3 8 13h1.27L11 14.73V13h.73l4.25 4.25 1.27-1.25L3.27 3 2 4.27zM7 8.9h1.73l-1.9-1.9L7 7v1.9z"/></svg>';

// "Pica pica  W iN  status" over "Gazza · Eurasian Magpie"
function speciesHeaderHtml(observation, label, kind = observation.status) {
  const species = observation.species || {};
  const names = [species.italian_name, species.english_name].filter(Boolean).map(escapeHtml).join(" · ");
  return `
      <header class="obs-head">
        <div class="title-row">
          <h2><i>${escapeHtml(observation.scientific_name)}</i></h2>
          ${speciesLinks(observation.scientific_name)}
          <span class="badge badge-${kind}">${escapeHtml(label)}</span>
        </div>
        ${names ? `<p class="common-names">${names}</p>` : ""}
      </header>`;
}

// where to check a species: icons, the name stays in the tooltip and for screen readers.
// iNaturalist points at the exact taxon when its id is known (on Android the app opens it), eBird
// at the species page (the same Macaulay Library sounds Merlin plays); ♪ plays an example here.
function speciesLinks(scientificName) {
  const name = String(scientificName || "").trim();
  if (!name) return "";
  const known = state.species.get(name) || {};
  const links = [
    ["wikipedia", `https://it.wikipedia.org/wiki/${encodeURIComponent(name.replace(/ /g, "_"))}`, `${name} su Wikipedia`],
    ["inaturalist", known.inat_taxon_id
      ? `https://www.inaturalist.org/taxa/${Number(known.inat_taxon_id)}`
      : `https://www.inaturalist.org/taxa/search?q=${encodeURIComponent(name)}`, `${name} su iNaturalist`],
  ];
  if (known.ebird_code) {
    links.push(["ebird", `https://ebird.org/species/${encodeURIComponent(known.ebird_code)}`,
      `${name} su eBird (foto e suoni della Macaulay Library, quelli di Merlin)`]);
  }
  return `<span class="species-links">${links
    .map(([icon, url, label]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener"
      title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${SPECIES_LINK_ICONS[icon]}</a>`)
    .join("")}<button type="button" class="example-button" data-example="${escapeHtml(name)}"
      title="Ascolta un esempio (iNaturalist, i più votati): tocca ancora per il successivo"
      aria-label="Ascolta un esempio di ${escapeHtml(name)}">${ICON_NOTE}</button></span>`;
}

// ---------------------------------------------------------------- clip player

const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 2h4v2H4V5h4l1-2zM6 9h12l-1 12H7L6 9zm4 2v8h2v-8h-2zm4 0v8h2v-8h-2z"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>';
const ICON_CROSS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/></svg>';
const ICON_UNDO = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12.5 8c-2.65 0-5.05 1-6.9 2.6L2 7v9h9l-3.62-3.62A7.97 7.97 0 0 1 20.1 15.5l2.37-.78A10.48 10.48 0 0 0 12.5 8z"/></svg>';
const ICON_EDIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
const ICON_DENOISE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.5 5.6 10 7 8.6 4.5 10 2 7.5 3.4 5 2l1.4 2.5L5 7zm12 9.8L17 14l1.4 2.5L17 19l2.5-1.4L22 19l-1.4-2.5L22 14zM22 2l-2.5 1.4L17 2l1.4 2.5L17 7l2.5-1.4L22 7l-1.4-2.5zm-7.63 5.29a1 1 0 0 0-1.41 0L1.29 18.96a1 1 0 0 0 0 1.41l2.34 2.34a1 1 0 0 0 1.41 0L16.7 11.05a1 1 0 0 0 0-1.41l-2.33-2.35zm-1.03 5.49-2.12-2.12 2.44-2.44 2.12 2.12-2.44 2.44z"/></svg>';
const ICON_LOOP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>';
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

// clip_conf: BirdNET on the saved clip (the bands come from the same analysis); the live's own
// score, from other chunks of the stream, in the tooltip. A clip placed by hand in an observation
// of another species has neither: BirdNET's numbers were for the species it came from.
function clipFacts(clip) {
  const when = timeFormat.format(new Date(clip.recorded_at));
  const seconds = `${Math.round(clip.duration_s || 0)} s`;
  if (clip.manual) {
    const also = clip.linked_from ? " La stessa registrazione è anche nell'osservazione da cui viene." : "";
    return `${when} · <b class="by-hand" title="Messa qui a mano: BirdNET l'aveva analizzata per un'altra specie, quindi niente clip_conf né bande.${also}">collegata a mano</b> · ${seconds}`;
  }
  const live = clip.live_confidence != null && Math.abs(clip.live_confidence - clip.confidence) >= 0.005
    ? ` title="BirdNET sulla clip salvata; il live aveva dato ${Number(clip.live_confidence).toFixed(3)}"` : "";
  const linked = clip.linked_from ? ' · <span title="La stessa registrazione è anche in un\'altra osservazione">collegata</span>' : "";
  if (clip.segments == null) {
    return `${when} · clip_conf in analisi… · ${seconds}${linked}`;
  }
  return `${when}${linked} · clip_conf <b class="conf-${confidenceClass(clip.confidence)}"${live}>${Number(clip.confidence).toFixed(3)}</b>
    · ${seconds}`;
}

// canUnlink: only when the observation has other clips (a lone clip is already on its own, and
// can still be linked elsewhere); onInat: its observation is on iNaturalist (the bin then removes
// only the files)
function clipHtml(clip, { folded = false, canUnlink = true, onInat = false } = {}) {
  if (clip.media_deleted_at) {
    // on iNaturalist: its files left Supabase right after the upload (the row stays for the reports)
    return `
    <figure class="clip media-gone${folded ? " folded" : ""}" data-clip="${escapeHtml(clip.id)}">
      <p>${clipFacts(clip)}</p>
      <p>Audio tolto da Supabase il ${new Date(clip.media_deleted_at).toLocaleDateString("it-IT")}: è su iNaturalist.</p>
    </figure>`;
  }
  const urls = mediaUrls(clip.media_path);
  const duration = Number(clip.duration_s) || 1;
  const segments = clipSegments(clip);
  const trimmed = isTrimmed(clip);
  const wrong = clip.review === "wrong";
  return `
    <figure class="clip review-${clip.review}${folded ? " folded" : ""}" data-clip="${escapeHtml(clip.id)}" data-duration="${duration}">
      <div class="spectrogram-box" title="Tocca una banda (verde: likely, ambra: unsure) per sentire dove BirdNET riconosce la specie, altrove per ascoltare da lì; trascina per selezionare">
        <img class="spectrogram" src="${escapeHtml(urls.spectrogram)}" alt="Spettrogramma" loading="lazy" draggable="false">
        ${segments.map(([start, end, confidence]) => `<span class="band band-${confidenceClass(confidence)}" style="left:${percentOf(start, duration)};width:${percentOf(end - start, duration)};--strength:${confidence}"></span>`).join("")}
        ${trimmed ? `<span class="trim-shade" style="left:0;width:${percentOf(clip.trim_start_s, duration)}"></span>
          <span class="trim-shade" style="left:${percentOf(clip.trim_end_s, duration)};right:0"></span>` : ""}
        <span class="selection hidden"></span>
        <span class="playhead"></span>
        <span class="clip-info">${clipFacts(clip)}</span>
        ${trimmed ? `<button type="button" class="trim-chip" data-player="untrim"
          title="Tagliata (il file resta intero): tocca per togliere il taglio">✂ ${Number(clip.trim_start_s).toFixed(1)}–${Number(clip.trim_end_s).toFixed(1)} ✕</button>` : ""}
        <span class="player-time">0:00 / ${formatSeconds(duration)}</span>
      </div>
      <audio preload="none" crossorigin="anonymous" src="${escapeHtml(urls.audio)}"></audio>
      <div class="player">
        <button type="button" class="play-button" data-player="toggle" aria-label="Riproduci">${ICON_PLAY}</button>
        <button type="button" class="tool" data-player="back" title="Indietro di ${SKIP_SECONDS} secondi">−${SKIP_SECONDS}s</button>
        <button type="button" class="tool" data-player="speed" title="Velocità: metà per sentire i dettagli">1×</button>
        <button type="button" class="tool" data-player="loop" title="Ripeti in continuazione" aria-label="Ripeti">${ICON_LOOP}</button>
        <button type="button" class="tool denoise" data-player="denoise" aria-label="Riduci il rumore"
          title="Riduci il rumore: stima il fondo fuori dalle bande e lo sottrae. Tocca ancora per più forte, poi per l'originale">${ICON_DENOISE}</button>
        <span class="clip-tools">
        <button type="button" class="tool" data-player="link" title="Collega la clip anche a un'altra osservazione (o spostala): poi «Collega qui» su quella giusta"
          aria-label="Collega a un'altra osservazione">${ICON_LINK}</button>
        ${canUnlink ? `<button type="button" class="tool" data-player="unlink" title="Non è lo stesso uccello (o la stessa specie): stacca la clip in un'osservazione a sé"
          aria-label="Stacca la clip">${ICON_UNLINK}</button>` : ""}
        <button type="button" class="tool${wrong ? " on" : ""}" data-clip-review="wrong"
          title="${wrong ? "Rimetti la clip nell'osservazione" : "La clip non contiene la specie: va nel Cestino"}">${wrong ? "Ripristina" : "Scarta"}</button>
        ${onInat && !wrong
          ? `<button type="button" class="tool trash-button" data-delete-clip
          title="Togli subito audio e spettrogramma da Supabase (sono su iNaturalist): la clip resta nei report" aria-label="Togli i file da Supabase">${ICON_TRASH}</button>`
          : `<button type="button" class="tool trash-button" data-delete-clip
          title="Elimina subito e per sempre questa clip" aria-label="Elimina definitivamente questa clip">${ICON_TRASH}</button>`}
        </span>
      </div>
      ${candidatesHtml(clip)}
      <div class="selection-actions hidden">
        <span class="selection-label"></span>
        <button type="button" class="tool" data-player="play-selection">▶ Ascolta</button>
        <button type="button" class="tool on" data-player="trim" title="Tieni solo questa parte (il file resta intero)">✂ Taglia</button>
        <button type="button" class="tool" data-player="clear-selection" aria-label="Annulla la selezione">✕</button>
      </div>
    </figure>`;
}

function observationMeta(observation, facts, extra = "") {
  const first = facts.firstHeard ? hourFormat.format(new Date(facts.firstHeard)) : "";
  const last = facts.clips.length > 1 && facts.lastHeard ? hourFormat.format(new Date(facts.lastHeard)) : "";
  const hours = last && last !== first ? `${first}–${last}` : first;
  return `<p class="meta"><span>${formatDay(observation.day)} ${hours}</span>
    ${facts.place ? `<span class="place">${escapeHtml(placeLabel(facts.place))}</span>` : ""}
    ${facts.byHandOnly ? '<span>max_obs_conf <b class="by-hand">a mano</b></span>'
      : `<span>max_obs_conf <b class="conf-${confidenceClass(facts.maxConfidence)}">${facts.maxConfidence.toFixed(3)}</b>
      <span class="conf-tag conf-${confidenceClass(facts.maxConfidence)}">${confidenceClass(facts.maxConfidence)}</span></span>`}
    <span>${facts.clips.length} clip</span>${extra}</p>`;
}

function clipsHtml(clips, options = {}) {
  return `<div class="obs-clips">
      <div class="clips">${clips.map((clip, index) => clipHtml(clip, { ...options, folded: index >= VISIBLE_CLIPS })).join("")}</div>
      ${clips.length > VISIBLE_CLIPS
        ? `<button type="button" class="show-clips" data-show-clips>Mostra altre ${clips.length - VISIBLE_CLIPS} clip</button>`
        : ""}
    </div>`;
}

// only the actions that make sense for the current status
function observationActions(observation) {
  const status = observation.status;
  const buttons = [];
  if (state.linking && state.linking.observation.id !== observation.id) {
    buttons.push('<button type="button" class="primary link-here" data-link-here>Collega qui</button>');
  }
  if (status === "confirmed") {
    buttons.push('<button type="button" class="inat" data-inat-upload title="Carica su iNaturalist (serve il token, pannello iNat)">↑ iNaturalist</button>');
  }
  if (observation.inat_observation_id) {
    buttons.push(`<a class="inat-link" href="https://www.inaturalist.org/observations/${Number(observation.inat_observation_id)}"
      target="_blank" rel="noopener">Vedi su iNat ↗</a>`);
  }
  // the round ones on the right, within reach of a right thumb; the tick last
  buttons.push('<span class="spacer"></span>');
  if (status !== "uploaded") buttons.push(`<button type="button" class="round ghost" data-fix-toggle title="Correggi la specie se BirdNET ha sbagliato"
    aria-label="Correggi la specie">${ICON_EDIT}</button>`);
  if (status !== "pending") {
    buttons.push(`<button type="button" class="round ghost" data-status="pending" title="Rimetti da rivedere"
      aria-label="Rimetti da rivedere">${ICON_UNDO}</button>`);
  }
  if (status !== "rejected" && status !== "uploaded") {
    buttons.push(`<button type="button" data-status="rejected" class="round reject" title="Scarta: non è questa specie, va nel Cestino"
      aria-label="Scarta">${ICON_CROSS}</button>`);
  }
  if (status === "pending" || status === "rejected") {
    buttons.push(`<button type="button" data-status="confirmed" class="round confirm" title="Conferma: la specie è giusta"
      aria-label="Conferma">${ICON_CHECK}</button>`);
  }
  return buttons.join("");
}

function observationHtml(observation) {
  const facts = observationFacts(observation);
  const corrected = observation.corrected_scientific_name || "";
  // on iNaturalist the taxon is iNaturalist's: the PC's uploader copies it here every hour
  const onInat = observation.status === "uploaded";
  return `
    <article class="observation status-${observation.status} ${confidenceClass(facts.maxConfidence)}" data-id="${observation.id}">
      ${speciesHeaderHtml(observation, STATUS_LABELS[observation.status] || observation.status)}
      ${observationMeta(observation, facts)}
      <div class="fix${corrected ? "" : " hidden"}">
        <span>${onInat ? "Su iNaturalist" : "Specie corretta"}</span>
        <input data-field="corrected" list="species-list" placeholder="nome scientifico" value="${escapeHtml(corrected)}"
          ${onInat ? 'readonly title="La decide iNaturalist (identificazioni della comunità e tue): si aggiorna da sola ogni ora"' : ""}>
        ${corrected ? speciesLinks(corrected) : ""}
      </div>
      <div class="actions">${observationActions(observation)}</div>
      ${clipsHtml(facts.clips, { canUnlink: facts.clips.length > 1, onInat: observation.status === "uploaded" })}
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
      figure.classList.toggle("review-unreviewed", value !== "wrong");
      clipButton.classList.toggle("on", value === "wrong");
      clipButton.textContent = value === "wrong" ? "Ripristina" : "Scarta";
      clipButton.title = value === "wrong" ? "Rimetti la clip nell'osservazione" : "La clip non contiene la specie: va nel Cestino";
      const found = findClip(figure.dataset.clip);
      if (found) found.clip.review = value;
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
      const released = await uploadObservationToInat(observation, (step) => setStatus(`iNaturalist: ${step}…`));
      setStatus(`Caricata su iNaturalist: ${released ? UPLOADED_MESSAGE : UPLOADED_LATER}.`);
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
    if (deleted && found.clip.media_deleted_at) {
      // on iNaturalist: only its files went, the clip stays in its observation and in the reports
      figure.outerHTML = clipHtml(found.clip, { folded: figure.classList.contains("folded") });
    } else if (deleted && state.view !== "trash") {
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

  if (event.target.closest("[data-link-here]")) {
    try {
      await linkClipTo(id);
    } catch (error) {
      setStatus(`Collegamento non riuscito: ${error.message}`, true);
    }
    return;
  }

  const fixToggle = event.target.closest("[data-fix-toggle]");
  if (fixToggle) {
    const fix = card.querySelector(".fix");
    fix.classList.toggle("hidden");
    if (!fix.classList.contains("hidden")) fix.querySelector("input").focus();
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
      const observation = state.observations.get(id);
      if (leavesFilteredView(changes.status)) {
        removeCard(card);
      } else if (observation) {
        // drawn again: the actions depend on the status
        observation.status = changes.status;
        if (changes.corrected !== undefined) observation.corrected_scientific_name = changes.corrected.trim() || null;
        card.querySelectorAll("audio").forEach((audio) => audio.pause());
        card.outerHTML = observationHtml(observation);
      }
      refreshCounts();
    } catch (error) {
      setStatus(`Errore: ${error.message}`, true);
    }
  }
});

// the corrected species is saved when you leave the field (or press Enter), without changing the status
content.addEventListener("change", async (event) => {
  const input = event.target.closest('[data-field="corrected"]');
  if (!input) return;
  const card = input.closest(".observation");
  const id = Number(card.dataset.id);
  try {
    await updateObservation(id, { corrected: input.value });
    const observation = state.observations.get(id);
    if (observation) observation.corrected_scientific_name = input.value.trim() || null;
    const links = card.querySelector(".fix .species-links");
    links?.remove();
    if (input.value.trim()) input.insertAdjacentHTML("afterend", speciesLinks(input.value.trim()));
    setStatus(input.value.trim() ? `Specie corretta: ${input.value.trim()}.` : "Correzione tolta.");
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
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

// ---------------------------------------------------------------- volume

// One volume for every clip, set by you, up to +30 dB (×32): clips recorded until October 2026
// sit 40-50 dB under full scale, newer ones at the microphone's real level. The <audio> element
// stops at 100%, so playback goes through a Web Audio gain. Remembered in this browser.
const VOLUME_KEY = "volumeDb";
const VOLUME_MIN_DB = -20;
const VOLUME_MAX_DB = 30;
let volumeDb = Math.min(Math.max(Number(storageGet(VOLUME_KEY)) || 0, VOLUME_MIN_DB), VOLUME_MAX_DB);
let listeningContext = null;
const volumeGains = new Set();

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

function volumeLabel(db) {
  return `${Math.round(100 * 10 ** (db / 20))}%`;
}

function routeThroughVolume(audio) {
  if (audio.dataset.volume) return;
  const context = audioContext();
  if (!context) return; // no Web Audio: plain playback, at most 100%
  try {
    const gain = context.createGain();
    gain.gain.value = 10 ** (volumeDb / 20);
    context.createMediaElementSource(audio).connect(gain).connect(context.destination);
    volumeGains.add(gain);
    audio.dataset.volume = "1";
  } catch (error) {
    console.warn("Volume oltre il 100% non disponibile per questa clip:", error);
  }
}

function setVolume(db) {
  volumeDb = Math.min(Math.max(db, VOLUME_MIN_DB), VOLUME_MAX_DB);
  storageSet(VOLUME_KEY, volumeDb ? String(volumeDb) : "");
  const now = listeningContext ? listeningContext.currentTime : 0;
  // also on what is playing now, smoothed over a few ms (no click)
  for (const gain of volumeGains) gain.gain.setTargetAtTime(10 ** (volumeDb / 20), now, 0.02);
  $("#volume").value = String(volumeDb);
  $("#volume-value").textContent = volumeLabel(volumeDb);
  $("#volume-value").classList.toggle("on", volumeDb !== 0);
  $("#volume-toggle").title = `Volume ${volumeLabel(volumeDb)}`;
}

// on the phone the volume opens under the bar; on a computer it is always there
$("#volume-toggle").addEventListener("click", () => {
  const open = $("#volume-panel").classList.toggle("open");
  $("#volume-toggle").setAttribute("aria-expanded", String(open));
});
$("#volume").addEventListener("input", (event) => setVolume(Number(event.target.value)));
$("#volume-value").addEventListener("click", () => setVolume(0)); // the percentage itself goes back to 100%
setVolume(volumeDb);

function playRange(figure, start, end) {
  const audio = figure.querySelector("audio");
  playerState(figure).range = { start, end };
  state.currentFigure = figure;
  audioContext()?.resume(); // inside the tap: browsers start audio only on a user gesture
  routeThroughVolume(audio);
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
  if (!box || event.button > 0 || event.target.closest("button")) return; // the trim label is a button
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
    // inside a yellow band: just that band (with a little margin); elsewhere: from there on
    const found = findClip(figure.dataset.clip);
    const band = found ? clipSegments(found.clip).find(([start, end]) => at >= start && at <= end) : null;
    if (band) playRange(figure, Math.max(kept.start, band[0] - BAND_PADDING_SECONDS), Math.min(kept.end, band[1] + BAND_PADDING_SECONDS));
    else playRange(figure, at, kept.end);
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
  figure.outerHTML = clipHtml(found.clip, { folded, canUnlink: (found.observation?.clips || []).length > 1,
    onInat: found.observation?.status === "uploaded" });
  setStatus(start == null ? "Taglio tolto." : `Clip tagliata: ${values.trim_start_s}–${values.trim_end_s} s (il file resta intero).`);
}

content.addEventListener("click", async (event) => {
  const figure = event.target.closest(".clip");
  if (!figure) return;
  const control = event.target.closest("[data-player]");
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
      case "denoise":
        await cycleDenoise(figure, control);
        break;
      case "unlink":
        await unlinkClip(figure);
        break;
      case "link":
        startLinking(figure);
        break;
    }
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
  }
});

// ---------------------------------------------------------------- noise reduction (listening only)

// Off -> medium -> strong -> off. The noise is the clip outside its yellow bands; the work runs in
// assets/denoise-worker.js. Measured on 14 real clips: medium lowers the background ~6 dB and
// lifts the bird over it in every clip (+2.5 dB); strong lowers it ~18 dB (+7 dB on average) but
// can eat a faint bird. Only for your ears: the file on Supabase, BirdNET and iNaturalist keep the
// original (BirdNET even scores denoised audio lower: it learnt on raw field recordings).
const DENOISE_LEVELS = [
  null,
  { label: "media", options: { overSubtraction: 1, floor: 0.3, percentile: 0.3 } },
  { label: "forte", options: { overSubtraction: 2, floor: 0.1, percentile: 0.5 } },
];
const denoisedUrls = new Map(); // "clip id|level" -> blob URL of the cleaned WAV
let denoiseWorker = null;
let denoiseJobs = 0;
const denoiseWaiting = new Map();

function denoiseInWorker(samples, rate, segments, options) {
  if (!denoiseWorker) {
    denoiseWorker = new Worker("./assets/denoise-worker.js?v=1");
    denoiseWorker.onmessage = (event) => {
      const { id, samples: cleaned } = event.data;
      denoiseWaiting.get(id)?.(cleaned);
      denoiseWaiting.delete(id);
    };
  }
  const id = ++denoiseJobs;
  return new Promise((resolve) => {
    denoiseWaiting.set(id, resolve);
    const copy = new Float32Array(samples); // transferred to the worker
    denoiseWorker.postMessage({ id, samples: copy, rate, segments, options }, [copy.buffer]);
  });
}

async function denoisedUrl(clip, level) {
  const key = `${clip.id}|${level}`;
  if (!denoisedUrls.has(key)) {
    const { samples, rate } = await decodeMono(mediaUrls(clip.media_path).audio);
    const cleaned = await denoiseInWorker(samples, rate, clipSegments(clip), DENOISE_LEVELS[level].options);
    denoisedUrls.set(key, URL.createObjectURL(wavBlob(cleaned, rate)));
  }
  return denoisedUrls.get(key);
}

// another source in the same player: same point, still playing if it was
function swapSource(audio, url) {
  const at = audio.currentTime;
  const playing = !audio.paused;
  audio.preload = "auto";
  audio.src = url;
  audio.load();
  seekWhenReady(audio, at);
  if (playing) audio.play().catch((error) => setStatus(`Audio: ${error.message}`, true));
}

async function cycleDenoise(figure, button) {
  const found = findClip(figure.dataset.clip);
  if (!found || button.classList.contains("working")) return;
  const player = playerState(figure);
  const level = ((player.denoise || 0) + 1) % DENOISE_LEVELS.length;
  const audio = figure.querySelector("audio");
  button.classList.add("working");
  try {
    swapSource(audio, level ? await denoisedUrl(found.clip, level) : mediaUrls(found.clip.media_path).audio);
    player.denoise = level;
    button.classList.toggle("on", level > 0);
    button.classList.toggle("strong", level === 2);
    // written on the spectrogram ("rumore ridotto: media"): the button stays one icon
    const box = figure.querySelector(".spectrogram-box");
    if (level) box.dataset.denoise = DENOISE_LEVELS[level].label;
    else delete box.dataset.denoise;
  } finally {
    button.classList.remove("working");
  }
}

// ---------------------------------------------------------------- examples, candidates, unlink

// an example of the species from iNaturalist (research grade, most voted), played here; a second
// tap plays the next one, a tap while it plays stops it. Its own <audio>, outside the volume gain:
// iNaturalist's files cannot go through Web Audio (no CORS), and examples are loud enough.
const examples = new Map(); // scientific name -> [{ url, attribution }]
const exampleNext = new Map();
let exampleAudio = null;

async function exampleSounds(name) {
  if (!examples.has(name)) {
    const taxon = state.species.get(name)?.inat_taxon_id;
    const query = taxon ? `taxon_id=${Number(taxon)}` : `taxon_name=${encodeURIComponent(name)}`;
    const response = await fetch(`${INAT_API}/observations?${query}&sounds=true&quality_grade=research&order_by=votes&per_page=10`);
    if (!response.ok) throw new Error(`iNaturalist ${response.status}`);
    const data = await response.json();
    examples.set(name, (data.results || []).flatMap((observation) => (observation.sounds || [])
      .filter((sound) => sound.file_url)
      .map((sound) => ({ url: sound.file_url, attribution: sound.attribution || "iNaturalist" }))));
  }
  return examples.get(name);
}

function stopExample() {
  if (!exampleAudio) return;
  exampleAudio.pause();
  document.querySelectorAll(".example-button.on").forEach((button) => button.classList.remove("on"));
}

async function playExample(name, button) {
  if (exampleAudio && !exampleAudio.paused && button.classList.contains("on")) {
    stopExample();
    return;
  }
  stopExample();
  document.querySelectorAll("#content audio").forEach((audio) => audio.pause());
  button.classList.add("working");
  try {
    const sounds = await exampleSounds(name);
    if (!sounds.length) {
      setStatus(`Nessun esempio audio di ${name} su iNaturalist.`);
      return;
    }
    const index = (exampleNext.get(name) || 0) % sounds.length;
    exampleNext.set(name, index + 1);
    exampleAudio = new Audio(sounds[index].url);
    exampleAudio.addEventListener("ended", () => button.classList.remove("on"));
    await exampleAudio.play();
    button.classList.add("on");
    setStatus(`Esempio ${index + 1} di ${sounds.length}: ${sounds[index].attribution} (iNaturalist)`);
  } catch (error) {
    setStatus(`Esempio non disponibile: ${error.message}`, true);
  } finally {
    button.classList.remove("working");
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-example]");
  if (button) playExample(button.dataset.example, button);
});
content.addEventListener("play", stopExample, true); // a clip starts: the example stops

// BirdNET's three most likely species in the clip; a tap opens the correction with that name
function candidatesHtml(clip) {
  const candidates = Array.isArray(clip.candidates) ? clip.candidates : [];
  if (!candidates.length) return "";
  return `<div class="candidates" title="Le specie più probabili per BirdNET in questa clip: tocca per correggere l'osservazione">
      <span>BirdNET:</span>${candidates.map(([name, english, confidence]) => {
        const label = state.species.get(name)?.italian_name || english || name;
        return `<button type="button" class="candidate conf-${confidenceClass(confidence)}" data-candidate="${escapeHtml(name)}"
          title="${escapeHtml(name)}">${escapeHtml(label)} <b>${Number(confidence).toFixed(2)}</b></button>`;
      }).join("")}
    </div>`;
}

content.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-candidate]");
  if (!chip) return;
  const card = chip.closest(".observation");
  const fix = card?.querySelector(".fix");
  if (!fix) return;
  fix.classList.remove("hidden");
  const input = fix.querySelector("input");
  input.value = chip.dataset.candidate;
  input.focus();
  setStatus(card.querySelectorAll(".clip").length > 1
    ? "Vale per tutta l'osservazione: se è solo questa clip, prima staccala (catena spezzata). Esci dal campo per salvare."
    : "Esci dal campo per salvare la correzione.");
});

// a clip that is not the same bird (or species) as the rest: it becomes an observation of its own
function localDay(iso) {
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

async function unlinkClip(figure) {
  const found = findClip(figure.dataset.clip);
  const card = figure.closest(".observation");
  if (!found?.observation || !card) return;
  if ((found.observation.clips || []).length < 2) {
    setStatus("È già l'unica clip della sua osservazione.");
    return;
  }
  const { data, error } = await state.client.from("observations")
    .insert({ day: localDay(found.clip.recorded_at), scientific_name: found.observation.scientific_name, status: "pending" })
    .select("*, species(italian_name,english_name,german_name)").single();
  if (error) throw error;
  const { error: moveError } = await state.client.from("clips").update({ observation_id: data.id }).eq("id", found.clip.id);
  if (moveError) throw moveError;
  found.observation.clips = found.observation.clips.filter((clip) => clip.id !== found.clip.id);
  found.clip.observation_id = data.id;
  const separate = { ...data, clips: [found.clip] };
  state.observations.set(separate.id, separate);
  figure.querySelector("audio")?.pause();
  card.outerHTML = observationHtml(found.observation);
  content.querySelector(`.observation[data-id="${found.observation.id}"]`)
    .insertAdjacentHTML("afterend", observationHtml(separate));
  refreshCounts();
  setStatus("Clip staccata: ora è un'osservazione a sé, da rivedere (correggi la specie con la matita se serve).");
}

// ---------------------------------------------------------------- link a clip to another observation

// "Collega" on a clip, then "Collega qui" on another observation: the clip appears there too
// ("anche qui": a second clip row with the same files) or moves there ("sposta"), and is part of it
// from then on, ready for iNaturalist. No new analysis: you know what is in it. In an observation of
// another species BirdNET's numbers no longer apply (they were for the species it came from): the
// clip is marked as placed by hand (clips.manual), without clip_conf or bands, and leaves
// max_obs_conf alone; its candidates stay, they still say what BirdNET hears in it. The only clip of
// an observation is never moved away (that observation would be left empty, and its species may
// well be in the clip too): it is linked, and stays there as well.
function showLinking() {
  const banner = $("#link-banner");
  banner.classList.toggle("hidden", !state.linking);
  document.querySelectorAll("[data-link-here]").forEach((button) => button.remove());
  if (!state.linking) return;
  const { clip, observation, mode } = state.linking;
  $("#link-what").textContent = `Collega la clip delle ${timeFormat.format(new Date(clip.recorded_at))} `
    + `(${state.species.get(observation.scientific_name)?.italian_name || observation.scientific_name}): `
    + "tocca «Collega qui» sull'osservazione giusta.";
  const alone = (observation.clips || []).length < 2;
  document.querySelectorAll("[data-link-mode]").forEach((button) => button.classList.toggle("on", button.dataset.linkMode === (alone ? "copy" : mode)));
  const move = document.querySelector('[data-link-mode="move"]');
  move.disabled = alone;
  move.title = alone
    ? "È l'unica clip della sua osservazione: resta anche lì (la sua specie può esserci comunque; se non c'è, scartala lì)"
    : "Lascia la sua osservazione";
  content.querySelectorAll(".observation").forEach((card) => {
    if (Number(card.dataset.id) !== observation.id && !card.classList.contains("trash")) {
      card.querySelector(".actions")?.insertAdjacentHTML("afterbegin",
        '<button type="button" class="primary link-here" data-link-here>Collega qui</button>');
    }
  });
}

function startLinking(figure) {
  const found = findClip(figure.dataset.clip);
  if (!found?.observation) return;
  state.linking = { clip: found.clip, observation: found.observation, mode: state.linking?.mode || "copy" };
  showLinking();
}

function stopLinking() {
  state.linking = null;
  showLinking();
}

function redrawObservation(observation) {
  const card = content.querySelector(`.observation[data-id="${observation.id}"]`);
  if (!card) return;
  card.querySelectorAll("audio").forEach((audio) => audio.pause());
  card.outerHTML = observationHtml(observation);
}

async function linkClipTo(targetId) {
  const { clip, observation: source } = state.linking;
  const target = state.observations.get(targetId);
  if (!target) return;
  const chosen = state.linking.mode;
  const mode = (source.clips || []).length < 2 ? "copy" : chosen;
  const sameSpecies = target.scientific_name === source.scientific_name;
  const byHand = sameSpecies ? {} : { manual: true, confidence: 0, segments: [] };
  if (mode === "move") {
    const { error } = await state.client.from("clips").update({ observation_id: targetId, ...byHand }).eq("id", clip.id);
    if (error) throw error;
    source.clips = source.clips.filter((item) => item.id !== clip.id);
    Object.assign(clip, { observation_id: targetId, ...byHand });
    target.clips = [...(target.clips || []), clip];
    redrawObservation(source);
  } else {
    const { created_at: _created, ...fields } = clip;
    const copy = { ...fields, ...byHand, id: `${clip.linked_from || clip.id}@${targetId}`, observation_id: targetId,
      review: "unreviewed", inat_sound_id: null, linked_from: clip.linked_from || clip.id };
    const { error } = await state.client.from("clips").insert(copy);
    if (error) throw new Error(error.code === "23505" ? "la clip è già in quell'osservazione" : error.message);
    target.clips = [...(target.clips || []), copy];
  }
  stopLinking();
  redrawObservation(target);
  refreshCounts();
  setStatus(`Clip ${mode === "move" ? "spostata" : "collegata anche"} qui: fa parte di questa osservazione e si carica con lei su iNaturalist.`
    + (mode !== chosen ? " Era l'unica della sua osservazione, quindi resta anche lì." : ""));
}

$("#link-cancel").addEventListener("click", stopLinking);
document.querySelectorAll("[data-link-mode]").forEach((button) => button.addEventListener("click", () => {
  if (!state.linking) return;
  state.linking.mode = button.dataset.linkMode;
  showLinking();
}));

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
  if (!append) state.offset = 0;
  const rows = await fetchObservations();
  if (!append) {
    // replaced only now that the new list is here: no blank page while it loads
    content.querySelectorAll("audio").forEach((audio) => audio.pause());
    content.innerHTML = "";
    state.observations = new Map();
  }
  rows.forEach((row) => state.observations.set(row.id, row));
  content.insertAdjacentHTML("beforeend", rows.map(observationHtml).join(""));
  state.offset += rows.length;
  $("#more").classList.toggle("hidden", rows.length < PAGE_SIZE);
  if (!content.children.length) {
    content.innerHTML = '<p class="empty">Nessuna osservazione con questi filtri.</p>';
  }
}

// the next page loads by itself when the end of the list comes into view
let loadingMore = false;
async function loadMore() {
  if (loadingMore || $("#more").classList.contains("hidden") || state.view !== "observations") return;
  loadingMore = true;
  $("#more").textContent = "Carico…";
  try {
    await renderObservations(true);
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
  } finally {
    loadingMore = false;
    $("#more").textContent = "Carica altre";
  }
}

if ("IntersectionObserver" in window) {
  new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadMore();
  }, { rootMargin: "600px 0px" }).observe($("#more"));
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
  const facts = observationFacts(observation);
  const label = whole ? "scartata" : `${facts.clips.length} clip scartate`;
  return `
    <article class="observation trash status-rejected" data-id="${observation.id}">
      ${speciesHeaderHtml(observation, label, "trash")}
      ${observationMeta(observation, facts, whole ? "" : "<span>il resto dell'osservazione resta</span>")}
      <div class="actions">
        ${whole ? '<button type="button" class="primary" data-status="pending" title="Rimetti da rivedere">↺ Ripristina</button>' : ""}
        <button type="button" class="danger" data-delete title="Audio, spettrogrammi e righe spariscono da Supabase">Elimina per sempre</button>
      </div>
      ${clipsHtml(facts.clips, { canUnlink: false })}
    </article>`;
}

function trashSummary(observations, clips) {
  const parts = [];
  if (observations) parts.push(`<b>${observations}</b> ${observations === 1 ? "osservazione scartata" : "osservazioni scartate"}`);
  if (clips) parts.push(`<b>${clips}</b> ${clips === 1 ? "clip scartata" : "clip scartate"} in osservazioni tenute`);
  return `${parts.join(" e ")}.`;
}

async function renderTrash() {
  const { rejected, partial } = await fetchTrash();
  state.trash = new Map([
    ...rejected.map((observation) => [observation.id, { whole: true, clips: observation.clips || [] }]),
    ...partial.map((observation) => [observation.id, { whole: false, clips: observation.clips || [] }]),
  ]);
  const clipCount = [...state.trash.values()].reduce((sum, entry) => sum + entry.clips.length, 0);
  content.innerHTML = state.trash.size
    ? `<div class="trash-head">
         <p>${trashSummary(rejected.length, partial.reduce((sum, o) => sum + o.clips.length, 0))}
           Eliminandole, audio e spettrogrammi spariscono da Supabase.</p>
         <button type="button" class="danger" data-empty-trash>Svuota il cestino (${clipCount} clip)</button>
       </div>
       ${rejected.map((observation) => trashCardHtml(observation, true)).join("")}
       ${partial.map((observation) => trashCardHtml(observation, false)).join("")}`
    : '<p class="empty">Il cestino è vuoto.</p>';
}

function chunks(items, size) {
  const result = [];
  for (let i = 0; i < items.length; i += size) result.push(items.slice(i, i + size));
  return result;
}

// What counts in the reports stays there. A row goes only with what you judged wrong (the Cestino
// holds only that, the bin of a clip says it): a clip of an observation on iNaturalist, and still
// right, loses only its files (they are on iNaturalist), its row stays, marked media_deleted_at.
// The database refuses to delete those rows anyway (the keep_*_history triggers).
function keepsItsRow(observation, clip) {
  return observation?.status === "uploaded" && clip.review !== "wrong";
}

// The files of these clips leave the bucket, from their paths (none when the site runs on local
// files: make web-dev). Files shared with a clip linked to another observation stay while that
// clip uses them.
async function removeFiles(clips) {
  const ids = clips.map((clip) => clip.id);
  const { data: sharing, error } = await state.client.from("clips").select("id,media_path,media_deleted_at")
    .in("media_path", [...new Set(clips.map((clip) => clip.media_path))]);
  if (error) throw error;
  const stillUsed = new Set(sharing.filter((row) => !ids.includes(row.id) && !row.media_deleted_at).map((row) => row.media_path));
  const paths = config.mediaBaseUrl ? [] : clips.filter((clip) => !stillUsed.has(clip.media_path))
    .flatMap((clip) => [`${clip.media_path}.mp3`, `${clip.media_path}-spectrogram.jpg`]);
  for (const part of chunks(paths, 100)) {
    const { error: removeError } = await state.client.storage.from(BUCKET).remove(part);
    if (removeError) throw removeError;
  }
}

// entries: [{ observationId, whole, clips }]; true once done. ask: false for the trash button
// of a single clip, which acts at once.
async function deleteForGood(entries, { ask = true } = {}) {
  const clips = entries.flatMap((entry) => entry.clips);
  const archived = new Set(entries.flatMap((entry) => entry.clips
    .filter((clip) => keepsItsRow(state.observations.get(entry.observationId), clip)).map((clip) => clip.id)));
  const wholeCount = entries.filter((entry) => entry.whole).length;
  const question = `Eliminare definitivamente ${clips.length} clip${wholeCount ? ` (${wholeCount} osservazioni intere)` : ""}?\n\n`
    + "Audio e spettrogrammi vengono cancellati da Supabase e non si possono recuperare dal sito. I WAV sul PC restano.";
  if (!clips.length || (ask && !window.confirm(question))) return false;
  let deleted = false;

  setStatus("Elimino…");
  try {
    const ids = clips.map((clip) => clip.id);
    const removed = ids.filter((id) => !archived.has(id));
    // 1. first the list `sync` reads: if anything below fails, they are not uploaded again
    for (const part of chunks(removed, 200)) {
      const { error } = await state.client.from("deleted_clips")
        .upsert(part.map((id) => ({ id })), { onConflict: "id", ignoreDuplicates: true });
      if (error) throw error;
    }
    // 2. files in the bucket
    await removeFiles(clips);
    // 3. rows: clips (those on iNaturalist only marked), then whole observations and kept ones
    // left without clips
    for (const part of chunks(removed, 50)) {
      const { error } = await state.client.from("clips").delete().in("id", part);
      if (error) throw error;
    }
    if (archived.size) {
      const now = new Date().toISOString();
      const { error } = await state.client.from("clips").update({ media_deleted_at: now }).in("id", [...archived]);
      if (error) throw error;
      clips.filter((clip) => archived.has(clip.id)).forEach((clip) => { clip.media_deleted_at = now; });
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
      // one on iNaturalist stays even with all its clips discarded (without clips it counts nowhere)
      const { error } = await state.client.from("observations").delete().in("id", part).neq("status", "uploaded");
      if (error) throw error;
    }
    setStatus([removed.length ? `Eliminate ${removed.length} clip.` : "",
      archived.size ? `Audio tolto da Supabase per ${archived.size} clip su iNaturalist: restano nei report.` : ""].join(" ").trim());
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
    ? `collegato${login ? ` come ${login}` : ""}${until}`
    : "non collegato";
  $("#inat-token-form").classList.toggle("hidden", Boolean(token));
  $("#inat-token-hint").classList.toggle("hidden", Boolean(token));
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
// a clip's MP3 as mono samples at 48 kHz (from the browser cache once it has been played)
async function decodeMono(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`audio non scaricabile (${response.status})`);
  const decoded = await new OfflineAudioContext(1, 1, 48000).decodeAudioData(await response.arrayBuffer());
  return { samples: decoded.getChannelData(0), rate: decoded.sampleRate };
}

// 16-bit mono WAV
function wavBlob(samples, rate) {
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
  for (let index = 0; index < samples.length; index += 1) {
    view.setInt16(44 + index * 2, Math.max(-1, Math.min(1, samples[index])) * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// the trimmed part of a clip as a WAV, built in the browser from its MP3
async function trimmedWav(url, start, end) {
  const { samples, rate } = await decodeMono(url);
  return wavBlob(samples.subarray(Math.floor(start * rate), Math.min(samples.length, Math.ceil(end * rate))), rate);
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
    // the clips you placed here by hand first (your choice), then BirdNET's best
    .sort((a, b) => (Boolean(b.manual) - Boolean(a.manual)) || (b.confidence - a.confidence))
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
      // BirdNET's score is about the species it heard: none for a corrected one
      description: facts.byHandOnly || name !== observation.scientific_name ? "" : `BirdNET confidence ${facts.maxConfidence.toFixed(3)}`,
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
    .update({ status: "uploaded", reviewed_at: new Date().toISOString(), uploaded_at: new Date().toISOString() })
    .eq("id", observation.id);
  if (error) throw error;
  observation.status = "uploaded";
  try {
    await releaseFiles(observation);
    return true;
  } catch (releaseError) {
    console.warn("File non tolti da Supabase dopo il caricamento:", releaseError);
    return false; // the PC's uploader removes them within the hour
  }
}

// On iNaturalist now: the files of its clips leave Supabase at once, also those of the clips beyond
// the INAT_MAX_SOUNDS sent (the observation is there). The rows stay, marked media_deleted_at, and
// keep counting in the reports. Discarded clips keep their files for the Cestino.
async function releaseFiles(observation) {
  const kept = (observation.clips || []).filter((clip) => clip.review !== "wrong" && !clip.media_deleted_at);
  if (!kept.length) return;
  await removeFiles(kept);
  const now = new Date().toISOString();
  const { error } = await state.client.from("clips").update({ media_deleted_at: now }).in("id", kept.map((clip) => clip.id));
  if (error) throw error;
  kept.forEach((clip) => { clip.media_deleted_at = now; });
}

const UPLOADED_MESSAGE = "audio e spettrogrammi tolti da Supabase, le righe restano nei report";
const UPLOADED_LATER = "i file li toglie da Supabase il PC entro un'ora";

$("#inat-toggle").addEventListener("click", () => {
  const panel = $("#inat-panel");
  panel.classList.toggle("hidden");
  $("#inat-toggle").setAttribute("aria-expanded", String(!panel.classList.contains("hidden")));
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
    let done = 0, later = 0;
    for (const [index, observation] of data.entries()) {
      const label = `${observation.species?.italian_name || observation.scientific_name}, ${formatDay(observation.day)}`;
      try {
        if (!(await uploadObservationToInat(observation, (step) => setStatus(`iNaturalist ${index + 1}/${data.length}: ${step}…`)))) later += 1;
        done += 1;
        log.insertAdjacentHTML("beforeend", `<li>✓ <a href="https://www.inaturalist.org/observations/${Number(observation.inat_observation_id)}"
          target="_blank" rel="noopener">${escapeHtml(label)}</a></li>`);
      } catch (uploadError) {
        log.insertAdjacentHTML("beforeend", `<li class="error">✗ ${escapeHtml(label)}: ${escapeHtml(uploadError.message)}</li>`);
        if (/token/.test(uploadError.message)) break; // no point going on without a valid token
      }
    }
    setStatus(`iNaturalist: caricate ${done} di ${data.length}${done ? `; ${later ? UPLOADED_LATER : UPLOADED_MESSAGE}` : ""}.`, done < data.length);
  } catch (error) {
    setStatus(`iNaturalist: ${error.message}`, true);
  }
  await refreshCounts();
  await renderInatPanel();
  if (state.view !== "report" && state.view !== "trash") renderObservations();
});

// ---------------------------------------------------------------- report

// every row of a query: Supabase returns at most 1000 at a time, and a year of clips is many more
async function fetchAll(makeQuery, pageSize = 1000) {
  const rows = [];
  for (let from = 0; ; from += pageSize) {
    const { data, error } = await makeQuery().range(from, from + pageSize - 1);
    if (error) throw error;
    rows.push(...data);
    if (data.length < pageSize) return rows;
  }
}

// How often a species is on iNaturalist in each month of the year, over the whole history (all
// years together): among the days of that month with observations you uploaded, the share with
// that species. Raw counts would make the months you listened to and reviewed more look richer.
const MONTH_LETTERS = ["G", "F", "M", "A", "M", "G", "L", "A", "S", "O", "N", "D"];
const monthNameFormat = new Intl.DateTimeFormat("it-IT", { month: "long" });
const MONTH_NAMES = MONTH_LETTERS.map((_, month) => monthNameFormat.format(new Date(2026, month, 1)));

function seasonHtml(uploaded) {
  const months = () => Array.from({ length: 12 }, () => new Set());
  const listened = months(); // days with something on iNaturalist
  const bySpecies = new Map();
  let firstDay = "";
  for (const row of uploaded) {
    const month = Number(row.day.slice(5, 7)) - 1;
    listened[month].add(row.day);
    if (!firstDay || row.day < firstDay) firstDay = row.day;
    const entry = bySpecies.get(row.scientific_name) || { row, months: months(), days: new Set() };
    entry.months[month].add(row.day);
    entry.days.add(row.day);
    bySpecies.set(row.scientific_name, entry);
  }
  if (!bySpecies.size) return '<p class="empty">Ancora nessuna osservazione caricata.</p>';
  const species = [...bySpecies.values()].sort((a, b) => b.days.size - a.days.size);
  const head = MONTH_LETTERS.map((letter, month) => `<th title="${MONTH_NAMES[month]}: ${listened[month].size} giorni con osservazioni caricate">
    ${letter}<small>${listened[month].size || ""}</small></th>`).join("");
  const body = species.map(({ row, months: heard }) => {
    const name = row.italian_name || row.scientific_name;
    const cells = heard.map((days, month) => {
      const total = listened[month].size;
      if (!total) return `<td class="none" title="${MONTH_NAMES[month]}: nessuna osservazione caricata">·</td>`;
      const share = days.size / total;
      const label = `${escapeHtml(name)}, ${MONTH_NAMES[month]}: ${days.size} giorni su ${total}`;
      return days.size
        ? `<td class="${share >= 0.5 ? "strong" : ""}" style="--share:${Math.round(15 + share * 85)}%" title="${label}">${Math.round(share * 100)}</td>`
        : `<td title="${label}"></td>`;
    }).join("");
    return `<tr><th scope="row"><button type="button" class="species-link" data-species="${escapeHtml(row.scientific_name)}"
      data-from="${firstDay}" title="Vedi le sue osservazioni">${escapeHtml(name)}</button></th>${cells}</tr>`;
  }).join("");
  return `<div class="table-scroll"><table class="season-table">
      <thead><tr><th>Specie <small>giorni con caricate →</small></th>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
}

// beside a species name in the report: its scientific name when the shown name is another one
// (a genus like Turdus has only that), and an optional remark
function speciesNote(row, remark = "") {
  const latin = row.italian_name && row.italian_name !== row.scientific_name ? `<i>${escapeHtml(row.scientific_name)}</i>` : "";
  const parts = [latin, remark].filter(Boolean);
  return parts.length ? ` <small>${parts.join(" · ")}</small>` : "";
}

function isoDaysAgo(days) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toISOString().slice(0, 10);
}

async function renderReport(days = Number(localStorage.getItem("reportDays") || 7)) {
  const from = isoDaysAgo(days - 1);
  // a species name opens its observations of exactly this period
  const speciesLink = (row) => `<button type="button" class="species-link" data-species="${escapeHtml(row.scientific_name)}"
    data-from="${from}" title="Vedi le osservazioni di questo periodo">${escapeHtml(row.italian_name || row.scientific_name)}</button>`;
  // the rows stay when the files leave Storage after iNaturalist: a year back is all there
  const [rows, clips, onInat, inatHistory] = await Promise.all([
    fetchAll(() => state.client.from("daily_summary").select("*").gte("day", from).neq("status", "rejected")
      .order("observation_id")),
    fetchAll(() => state.client.from("clips").select("id, recorded_at, observations!inner(status,day)")
      .gte("observations.day", from).neq("observations.status", "rejected").neq("review", "wrong").order("id")),
    fetchAll(() => state.client.from("observations").select("id, inat_observation_id").eq("status", "uploaded")
      .gte("day", from).order("id")),
    // the seasons need every month, whatever period is chosen
    fetchAll(() => state.client.from("daily_summary").select("observation_id, day, scientific_name, italian_name")
      .eq("status", "uploaded").order("observation_id")),
  ]);
  // what is on iNaturalist (its audio lives there now), the latest first
  const inatIds = new Map(onInat.map((row) => [row.id, row.inat_observation_id]));
  const uploaded = rows.filter((row) => row.status === "uploaded")
    .sort((a, b) => String(b.first_heard).localeCompare(String(a.first_heard)));

  const bySpecies = new Map();
  for (const row of rows) {
    // one row per observation (a visit): a species can have several in a day
    const entry = bySpecies.get(row.scientific_name) || { ...row, dayset: new Set(), clips: 0, seconds: 0, best: 0 };
    entry.dayset.add(row.day);
    entry.days = entry.dayset.size;
    entry.clips += row.clips;
    entry.seconds += Number(row.seconds);
    entry.best = Math.max(entry.best, Number(row.max_confidence));
    if (row.last_heard && (!entry.last_heard || row.last_heard > entry.last_heard)) entry.last_heard = row.last_heard;
    bySpecies.set(row.scientific_name, entry);
  }
  const speciesRows = [...bySpecies.values()].sort((a, b) => b.days - a.days || b.clips - a.clips);
  const newSpecies = speciesRows.filter((row) => (state.species.get(row.scientific_name)?.first_seen_date || "") >= from);
  const hours = Array.from({ length: 24 }, () => 0);
  for (const clip of clips) hours[new Date(clip.recorded_at).getHours()] += 1;
  const maxHour = Math.max(1, ...hours);
  const totalMinutes = Math.round(speciesRows.reduce((sum, row) => sum + row.seconds, 0) / 60);
  const pending = rows.filter((row) => row.status === "pending").length;

  content.innerHTML = `
    <div class="report-head">
      <h2>Ultimi ${days} giorni</h2>
      <div class="segmented">
        ${[7, 30, 90, 365].map((n) => `<button type="button" data-days="${n}" class="${n === days ? "on" : ""}">${n === 365 ? "1 anno" : `${n} gg`}</button>`).join("")}
      </div>
    </div>
    <details class="inat-uploads">
      <summary>Su iNaturalist <b>${uploaded.length}</b> <small>caricate in questo periodo</small></summary>
      ${uploaded.length ? `<div class="table-scroll"><table class="report-table inat-table">
        <thead><tr><th>Specie</th><th>Quando</th><th>Clip</th><th></th></tr></thead>
        <tbody>${uploaded.map((row) => {
          const inatId = Number(inatIds.get(row.observation_id));
          const heard = row.first_heard ? new Date(row.first_heard) : null;
          return `
          <tr>
            <td>${speciesLink(row)}${speciesNote(row, row.birdnet_scientific_name && row.birdnet_scientific_name !== row.scientific_name
              ? `BirdNET: ${escapeHtml(state.species.get(row.birdnet_scientific_name)?.italian_name || row.birdnet_scientific_name)}` : "")}</td>
            <td>${heard ? `${shortDateFormat.format(heard)} ${hourFormat.format(heard)}` : ""}</td>
            <td>${row.clips}</td>
            <td>${inatId ? `<a class="inat-link" href="https://www.inaturalist.org/observations/${inatId}" target="_blank" rel="noopener">iNat ↗</a>` : ""}</td>
          </tr>`;
        }).join("")}</tbody>
      </table></div>` : '<p class="empty">Nessuna osservazione caricata in questo periodo.</p>'}
      <h3>Specie per mese <small>tutte le caricate, da sempre: % dei giorni del mese con caricate in cui c'è la specie</small></h3>
      ${seasonHtml(inatHistory)}
    </details>
    <div class="report-grid">
      <section>
        <div class="tiles">
          <div class="tile"><b>${speciesRows.length}</b><span>specie</span></div>
          <div class="tile"><b>${rows.length}</b><span>osservazioni</span></div>
          <div class="tile"><b>${pending}</b><span>da rivedere</span></div>
          <div class="tile"><b>${clips.length}</b><span>clip</span></div>
          <div class="tile"><b>${totalMinutes}</b><span>min di audio</span></div>
          <div class="tile"><b>${newSpecies.length}</b><span>specie nuove</span></div>
        </div>
        ${newSpecies.length ? `<p class="new-species">Prima volta: ${newSpecies.map(speciesLink).join(", ")}</p>` : ""}
        <h3>Attività per ora</h3>
        <div class="hours">${hours.map((count, hour) => `
          <div class="hour" title="${hour}:00 · ${count} clip">
            <span class="bar" style="height:${Math.round((count / maxHour) * 100)}%"></span>
            <span class="label">${hour}</span>
          </div>`).join("")}</div>
      </section>
      <section>
        <h3>Specie</h3>
        ${speciesRows.length ? `<div class="table-scroll"><table class="report-table">
          <thead><tr><th>Specie</th><th>Giorni</th><th>Clip</th><th title="Confidenza massima">Conf.</th><th>Ultima</th></tr></thead>
          <tbody>${speciesRows.map((row) => `
            <tr>
              <td>${speciesLink(row)}${speciesNote(row)}</td>
              <td>${row.days}</td><td>${row.clips}</td><td>${row.best ? row.best.toFixed(3) : "—"}</td>
              <td>${row.last_heard ? shortDateFormat.format(new Date(row.last_heard)) : ""}</td>
            </tr>`).join("")}</tbody>
        </table></div>` : '<p class="empty">Nessuna specie in questo periodo.</p>'}
      </section>
    </div>`;
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
  $("#more-filters").open = true;
  state.view = "observations";
  window.scrollTo(0, 0);
  render();
}

// ---------------------------------------------------------------- navigation

// the current page stays (dimmed) until the new one is ready: no blank flashes
let renderToken = 0;
async function render() {
  const token = ++renderToken;
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === state.view));
  $("#filters").classList.toggle("hidden", state.view !== "observations");
  showFilterState();
  content.classList.add("busy");
  const slow = setTimeout(() => setStatus("Carico…"), 400);
  try {
    if (state.view !== "observations") $("#more").classList.add("hidden");
    if (state.view === "report") await renderReport();
    else if (state.view === "trash") await renderTrash();
    else await renderObservations();
    if ($("#status-line").textContent === "Carico…") setStatus("");
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
  } finally {
    clearTimeout(slow);
    if (token === renderToken) content.classList.remove("busy");
  }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  if (state.view !== tab.dataset.view) window.scrollTo({ top: 0 });
  state.view = tab.dataset.view;
  render();
}));

// status: one tap on its pill; species and dates: typed, applied after a short pause
function showFilterState() {
  const status = $("#filter-status").value;
  document.querySelectorAll("[data-filter-status]").forEach((pill) => pill.classList.toggle("on", pill.dataset.filterStatus === status));
  const confidence = $("#filter-confidence").value;
  document.querySelectorAll("[data-filter-confidence]").forEach((pill) => pill.classList.toggle("on", pill.dataset.filterConfidence === confidence));
  const narrowed = Boolean($("#filter-species").value.trim() || $("#filter-from").value || $("#filter-to").value);
  $("#filters-active").classList.toggle("hidden", !narrowed);
  $("#filters-clear").classList.toggle("hidden", !narrowed);
}

document.querySelectorAll("[data-filter-status]").forEach((pill) => pill.addEventListener("click", () => {
  $("#filter-status").value = pill.dataset.filterStatus;
  render();
}));

// likely / unsure: a second tap on the active one shows both again
document.querySelectorAll("[data-filter-confidence]").forEach((pill) => pill.addEventListener("click", () => {
  const value = pill.dataset.filterConfidence;
  $("#filter-confidence").value = $("#filter-confidence").value === value ? "" : value;
  render();
}));

let filterTimer = null;
document.querySelectorAll("#more-filters input").forEach((input) => input.addEventListener("input", () => {
  showFilterState();
  clearTimeout(filterTimer);
  filterTimer = setTimeout(render, 300);
}));

$("#filters-clear").addEventListener("click", () => {
  for (const id of ["#filter-species", "#filter-from", "#filter-to"]) $(id).value = "";
  render();
});

$("#more").addEventListener("click", loadMore);

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

// species and dates open on wide screens, folded away on the phone
$("#more-filters").open = window.matchMedia("(min-width: 721px)").matches;

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
