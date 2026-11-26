// Report -> "Scarica": the period's observations (what the report counts) as a CSV for a spreadsheet.
// Excel and LibreOffice in Italian open it with a double click: ";" between fields, decimal comma,
// UTF-8 with a BOM (accents). The database is writable by whoever has the site's link: a text cell
// that a spreadsheet would run as a formula (=, +, -, @) gets a leading apostrophe.

const STATUS = { pending: "da rivedere", confirmed: "confermata", uploaded: "su iNaturalist" };
const HEAD = ["Data", "Ora", "Specie", "Nome italiano", "Nome inglese", "Stato", "Confidenza max", "Clip",
  "Secondi di audio", "Luogo", "Latitudine", "Longitudine", "BirdNET aveva sentito", "iNaturalist"];

const NUMBER = /^-?\d+(,\d+)?$/;

function cell(value) {
  let text = value == null ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(text) && !NUMBER.test(text)) text = `'${text}`;
  return /[";\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function decimal(value, digits) {
  return value == null || value === "" || !Number.isFinite(Number(value)) ? "" : Number(value).toFixed(digits).replace(".", ",");
}

function localTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

// rows: daily_summary (one per observation); details: observation id -> { place_name, latitude,
// longitude, inat_observation_id }, read when the file is asked for
export function observationsCsv(rows, details = new Map()) {
  const lines = [...rows]
    .sort((a, b) => String(a.first_heard || a.day).localeCompare(String(b.first_heard || b.day)))
    .map((row) => {
      const more = details.get(row.observation_id) || {};
      return [
        row.day, localTime(row.first_heard), row.scientific_name, row.italian_name, row.english_name,
        STATUS[row.status] || row.status, decimal(row.max_confidence, 3), row.clips, decimal(row.seconds, 1),
        more.place_name, decimal(more.latitude, 5), decimal(more.longitude, 5),
        row.birdnet_scientific_name && row.birdnet_scientific_name !== row.scientific_name ? row.birdnet_scientific_name : "",
        more.inat_observation_id ? `https://www.inaturalist.org/observations/${Number(more.inat_observation_id)}` : "",
      ].map(cell).join(";");
    });
  return `﻿${[HEAD.join(";"), ...lines].join("\r\n")}\r\n`;
}
