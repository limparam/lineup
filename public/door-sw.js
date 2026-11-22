const CACHE = "lineup-door-v9";
const ASSETS = [
  "/assets/style.css",
  "/assets/common.js",
  "/assets/door.js",
  "/assets/door-offline.js",
  "/assets/jsQR.js",
  "/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      await cache.put(request, response.clone());
      return response;
    }
    if (response && response.status >= 500) {
      const fallback = await cache.match(request, { ignoreSearch: true });
      if (fallback) return fallback;
    }
    return response;
  } catch (error) {
    const exact = await cache.match(request);
    if (exact) return exact;
    const cached = await cache.match(request, { ignoreSearch: true });
    if (cached) return cached;
    const fallback = await cache.match("/door");
    if (fallback) return fallback;
    throw error;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate" && url.pathname.startsWith("/door/")) {
    event.respondWith(networkFirst(request));
    return;
  }
  if (url.pathname.startsWith("/assets/") || url.pathname === "/favicon.svg") {
    event.respondWith(networkFirst(request));
  }
});
