// Service worker for the installed feed app (issue #154). Served at /sw.js so
// its scope is the whole site. The server replaces __VERSION__ with the asset
// hash, so every deploy that changes app.css/app.js installs a fresh worker and
// drops the old caches.
//
// Strategy: pages are network-first (a new deploy or new items show up the
// moment there's signal) and fall back to the last copy seen when offline.
// Versioned static assets are cache-first. Everything else (POSTs, fetch()
// fragments, /seen, /new-count, ...) goes straight to the network.
const VERSION = "__VERSION__";
const CACHE = `insights-${VERSION}`;
const SHELL = [
  `/static/app.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  "/static/manifest.webmanifest",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      // The home page is best-effort: a hiccup there mustn't block the update.
      .then((cache) => cache.addAll(SHELL).then(() => cache.add("/").catch(() => {})))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const hit = (await cache.match(request)) || (await cache.match("/"));
    if (hit) return hit;
    return new Response(
      "<!doctype html><meta name=viewport content='width=device-width'>" +
        "<p style='font:16px system-ui;padding:2rem'>You're offline, and this page isn't saved yet.</p>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
    );
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
  } else if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/covers/")) {
    event.respondWith(cacheFirst(request));
  }
});
