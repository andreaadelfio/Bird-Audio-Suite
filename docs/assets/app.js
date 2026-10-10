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
  uploaded: "su iNaturalist",
};

const BUCKET = "bird-audio";
const OBSERVATION_SELECT = "*, species(italian_name,english_name,german_name), clips(*)";

const state = {
  client: null,
  view: "review",
  offset: 0,
  species: new Map(),
  trash: new Map(), // observation id -> { whole, clips } shown in the Cestino
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

function setStatus(message, isError = false) {
  const line = $("#status-line");
  line.textContent = message;
  line.classList.toggle("error", isError);
}

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
    .from("species")
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
    .order("observed_at", { ascending: false })
    .range(state.offset, state.offset + PAGE_SIZE - 1);

  const status = state.view === "review" ? "pending" : $("#filter-status").value;
  if (status) query = query.eq("status", status);
  if ($("#filter-from").value) query = query.gte("day", $("#filter-from").value);
  if ($("#filter-to").value) query = query.lte("day", $("#filter-to").value);
  const speciesText = $("#filter-species").value;
  if (speciesText.trim()) query = query.in("scientific_name", await matchingSpecies(speciesText));

  const { data, error } = await query;
  if (error) throw error;
  return data;
}

// changes: { status, notes, corrected }; a missing field is left as it is, "" clears the correction
async function updateObservation(id, changes) {
  const row = { reviewed_at: new Date().toISOString() };
  if (changes.status !== undefined) row.status = changes.status;
  if (changes.notes !== undefined) row.notes = changes.notes;
  if (changes.corrected !== undefined) row.corrected_scientific_name = changes.corrected.trim() || null;
  const { error } = await state.client.from("observations").update(row).eq("id", id);
  if (error) throw error;
}

async function updateClip(id, review) {
  const { error } = await state.client.from("clips").update({ review }).eq("id", id);
  if (error) throw error;
}

// ---------------------------------------------------------------- observation cards

function clipHtml(clip, folded = false) {
  const recorded = timeFormat.format(new Date(clip.recorded_at));
  return `
    <figure class="clip review-${clip.review}${folded ? " folded" : ""}" data-clip="${escapeHtml(clip.id)}">
      ${clip.spectrogram_url
        ? `<div class="spectrogram-box" title="Tocca per ascoltare">
             <img class="spectrogram" src="${escapeHtml(clip.spectrogram_url)}" alt="Spettrogramma" loading="lazy">
             <span class="playhead"></span>
             <span class="play-badge" aria-hidden="true"></span>
           </div>`
        : ""}
      <audio controls preload="none" src="${escapeHtml(clip.audio_url)}"></audio>
      <figcaption>
        <span>${recorded} · ${Number(clip.confidence).toFixed(3)} · ${Math.round(clip.duration_s || 0)} s</span>
        <span class="clip-actions">
          <button type="button" class="small ${clip.review === "wrong" ? "on" : ""}" data-clip-review="wrong"
            title="Questa clip non contiene la specie: finisce nel Cestino">${clip.review === "wrong" ? "Ripristina clip" : "Scarta clip"}</button>
        </span>
      </figcaption>
    </figure>`;
}

function observationHtml(observation) {
  const species = observation.species || {};
  const clips = [...(observation.clips || [])].sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
  const first = clips[0] ? timeFormat.format(new Date(clips[0].recorded_at)) : "";
  const last = clips.length > 1 ? `–${timeFormat.format(new Date(clips[clips.length - 1].recorded_at))}` : "";
  const corrected = observation.corrected_scientific_name
    ? `<p class="corrected">Corretta in <i>${escapeHtml(observation.corrected_scientific_name)}</i></p>`
    : "";
  return `
    <article class="observation status-${observation.status}" data-id="${observation.id}">
      <header>
        <h2>${escapeHtml(species.italian_name || observation.scientific_name)}
          <small><i>${escapeHtml(observation.scientific_name)}</i> · ${escapeHtml(species.english_name || "")}</small></h2>
        <span class="badge">${STATUS_LABELS[observation.status] || observation.status}</span>
      </header>
      <p class="meta">${formatDay(observation.day)} · ${first}${last} · ${escapeHtml(observation.place_name)} ·
        confidenza max ${Number(observation.max_confidence).toFixed(3)} · ${clips.length} clip</p>
      ${corrected}
      <div class="clips">${clips.map((clip, index) => clipHtml(clip, index >= VISIBLE_CLIPS)).join("")}</div>
      ${clips.length > VISIBLE_CLIPS
        ? `<button type="button" class="show-clips" data-show-clips>Mostra altre ${clips.length - VISIBLE_CLIPS} clip</button>`
        : ""}
      <footer>
        <label class="grow">Note <textarea data-field="notes" rows="1">${escapeHtml(observation.notes)}</textarea></label>
        <label>Specie corretta <input data-field="corrected" list="species-list" placeholder="se BirdNET ha sbagliato"
          value="${escapeHtml(observation.corrected_scientific_name || "")}"></label>
        <div class="actions">
          <button type="button" data-status="confirmed" class="primary">Conferma</button>
          <button type="button" data-status="rejected">Scarta</button>
          ${observation.status !== "pending" ? '<button type="button" data-status="pending">Rimetti da rivedere</button>' : ""}
        </div>
      </footer>
    </article>`;
}

content.addEventListener("click", async (event) => {
  const spectrogram = event.target.closest(".spectrogram-box");
  if (spectrogram) {
    const audio = spectrogram.closest(".clip").querySelector("audio");
    if (audio.paused) {
      // tapping where the call is on the spectrogram starts from there
      const box = spectrogram.getBoundingClientRect();
      const fraction = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
      if (Number.isFinite(audio.duration) && audio.duration > 0 && fraction > 0.02) {
        audio.currentTime = fraction * audio.duration;
      }
      audio.play();
    } else {
      audio.pause();
    }
    return;
  }

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

  const deleteButton = event.target.closest("[data-delete]");
  if (deleteButton) {
    const entry = state.trash.get(id);
    if (entry) await deleteForGood([{ observationId: id, ...entry }]);
    return;
  }

  const statusButton = event.target.closest("[data-status]");
  if (statusButton) {
    const changes = { status: statusButton.dataset.status };
    const notes = card.querySelector('[data-field="notes"]');
    const corrected = card.querySelector('[data-field="corrected"]');
    if (notes) changes.notes = notes.value;
    if (corrected) changes.corrected = corrected.value; // "" clears a previous correction
    try {
      await updateObservation(id, changes);
      setStatus(`Osservazione ${STATUS_LABELS[changes.status]}.`);
      const leavesView =
        (state.view === "review" && changes.status !== "pending") ||
        (state.view === "trash" && changes.status !== "rejected");
      if (leavesView) {
        card.querySelectorAll("audio").forEach((audio) => audio.pause());
        card.classList.add("leaving");
        setTimeout(() => card.remove(), 250);
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

// one clip at a time, with a playhead moving over its spectrogram
content.addEventListener("play", (event) => {
  document.querySelectorAll("#content audio").forEach((audio) => {
    if (audio !== event.target) audio.pause();
  });
  event.target.closest(".clip")?.classList.add("playing");
}, true);

for (const type of ["pause", "ended"]) {
  content.addEventListener(type, (event) => {
    event.target.closest(".clip")?.classList.remove("playing");
  }, true);
}

content.addEventListener("timeupdate", (event) => {
  const audio = event.target;
  const clip = audio.closest(".clip");
  if (clip && audio.duration > 0) {
    clip.style.setProperty("--progress", String(audio.currentTime / audio.duration));
  }
}, true);

// save notes when leaving the field, without changing the status
content.addEventListener("change", async (event) => {
  if (event.target.dataset.field !== "notes") return;
  const card = event.target.closest(".observation");
  try {
    await updateObservation(Number(card.dataset.id), { notes: event.target.value });
    setStatus("Note salvate.");
  } catch (error) {
    setStatus(`Errore: ${error.message}`, true);
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
  content.insertAdjacentHTML("beforeend", rows.map(observationHtml).join(""));
  state.offset += rows.length;
  $("#more").classList.toggle("hidden", rows.length < PAGE_SIZE);
  if (!content.children.length) {
    content.innerHTML = `<p class="empty">${state.view === "review" ? "Niente da rivedere." : "Nessuna osservazione."}</p>`;
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
  const species = observation.species || {};
  const clips = [...(observation.clips || [])].sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
  return `
    <article class="observation trash status-${observation.status}" data-id="${observation.id}">
      <header>
        <h2>${escapeHtml(species.italian_name || observation.scientific_name)}
          <small><i>${escapeHtml(observation.scientific_name)}</i></small></h2>
        <span class="badge">${whole ? "osservazione scartata" : `${clips.length} clip scartate`}</span>
      </header>
      <p class="meta">${formatDay(observation.day)} · ${escapeHtml(observation.place_name)} ·
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

function storagePath(url) {
  const marker = `/storage/v1/object/public/${BUCKET}/`;
  const index = String(url || "").indexOf(marker);
  return index >= 0 ? decodeURIComponent(url.slice(index + marker.length)) : null;
}

function chunks(items, size) {
  const result = [];
  for (let i = 0; i < items.length; i += size) result.push(items.slice(i, i + size));
  return result;
}

// entries: [{ observationId, whole, clips }]
async function deleteForGood(entries) {
  const clips = entries.flatMap((entry) => entry.clips);
  const wholeCount = entries.filter((entry) => entry.whole).length;
  const question = `Eliminare definitivamente ${clips.length} clip${wholeCount ? ` (${wholeCount} osservazioni intere)` : ""}?\n\n`
    + "Audio e spettrogrammi vengono cancellati da Supabase e non si possono recuperare dal sito. I WAV sul PC restano.";
  if (!clips.length || !window.confirm(question)) return;

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
    const paths = clips.flatMap((clip) => [storagePath(clip.audio_url), storagePath(clip.spectrogram_url)]).filter(Boolean);
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
  } catch (error) {
    setStatus(`Errore durante l'eliminazione: ${error.message}`, true);
  }
  await refreshCounts();
  if (state.view === "trash") await renderTrash();
}

content.addEventListener("click", (event) => {
  if (!event.target.closest("[data-empty-trash]")) return;
  const entries = [...state.trash.entries()].map(([observationId, entry]) => ({ observationId, ...entry }));
  deleteForGood(entries);
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
  state.view = "all";
  window.scrollTo(0, 0);
  render();
}

// ---------------------------------------------------------------- navigation

async function render() {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === state.view));
  $("#filters").classList.toggle("hidden", state.view !== "all");
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
