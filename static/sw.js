// Minimal service worker: makes the app installable and keeps the shell available offline.
const CACHE = "lumen-v2";
const SHELL = ["/app", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Network first, cache as fallback, so the demo always shows fresh data when online.
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok && SHELL.includes(url.pathname)) caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
    return res;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/app"))));
});
