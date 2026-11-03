// Service worker (come in MyFinance e Listino Prezzi): serve per installare il sito come app.
// Prima la rete, così si vede sempre l'ultima versione; senza rete si apre almeno
// l'interfaccia salvata. Dati, audio e spettrogrammi (Supabase) non passano di qui.
const CACHE_NAME = "birds-shell-v6"; // v6: Registra (registra.js, i suoi worker), novembre 2026
// BirdNET (modelli, ~80 MB) e TensorFlow.js: una volta scaricati restano, anche quando cambia il
// sito (cache a parte, mai svuotata da un aggiornamento); a un modello nuovo si cambia il percorso
const MODELS_CACHE = "birds-models-v1";
const APP_SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./assets/styles.css",
  "./assets/app.js",
  "./assets/denoise-worker.js",
  "./assets/registra.js",
  "./assets/registra-worklet.js",
  "./assets/birdnet-worker.js",
  "./assets/config.js",
  "./assets/favicon-64.png",
  "./assets/apple-touch-icon.png",
  "./assets/icon-192.png",
  "./assets/icon-512.png",
  "./assets/icon-maskable-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => Promise.all(
      cacheNames
        .filter((cacheName) => cacheName !== CACHE_NAME && cacheName !== MODELS_CACHE)
        .map((cacheName) => caches.delete(cacheName))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // modelli e TensorFlow.js: prima la cache (non cambiano mai allo stesso indirizzo)
  const scope = new URL(self.registration.scope).pathname;
  if (url.pathname.startsWith(`${scope}models/`) || url.pathname.startsWith(`${scope}vendor/`)) {
    event.respondWith((async () => {
      const cache = await caches.open(MODELS_CACHE);
      const cached = await cache.match(url.origin + url.pathname);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) await cache.put(url.origin + url.pathname, response.clone());
      return response;
    })());
    return;
  }

  const shouldHandle =
    url.pathname.endsWith("/") ||
    url.pathname.endsWith("/index.html") ||
    url.pathname.endsWith("/manifest.webmanifest") ||
    url.pathname.endsWith("/service-worker.js") ||
    url.pathname.startsWith(`${new URL(self.registration.scope).pathname}assets/`);

  if (!shouldHandle) {
    return;
  }

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    // La chiave è il percorso senza "?v=...": in cache resta l'ultima versione scaricata
    const cacheKey = new Request(url.origin + url.pathname, { method: "GET" });
    const cachedResponse = await cache.match(cacheKey);

    try {
      const networkResponse = await fetch(request);
      if (networkResponse.ok) {
        await cache.put(cacheKey, networkResponse.clone());
      }
      return networkResponse;
    } catch (error) {
      if (cachedResponse) {
        return cachedResponse;
      }
      throw error;
    }
  })());
});
