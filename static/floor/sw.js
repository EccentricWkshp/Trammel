// Trammel shop-floor service worker.
// Makes the app installable and keeps static assets fast. It does not queue
// anything offline (SPEC §1.2): with no network, pages show a plain notice.
const CACHE = "trammel-floor-v1";
const OFFLINE = "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>" +
  "<title>Offline</title><body style='font-family:system-ui;padding:2rem'>" +
  "<h1>No connection</h1><p>The shop-floor app needs the network. Reconnect and try again.</p>";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const hit = await cache.match(request);
        const fresh = fetch(request).then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        });
        return hit || fresh;
      })
    );
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() =>
      new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
  }
});
