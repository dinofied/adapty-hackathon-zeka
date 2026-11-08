// Minimal service worker: makes the app installable and keeps the shell available offline.
const CACHE = "lumen-v3-{{VERSION}}";
const SHELL = ["/app", "/static/app-launch.js", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("message", (e) => {
  if (e.data?.type === "SKIP_WAITING") {
    e.waitUntil(self.skipWaiting());
    return;
  }
  if (e.data?.type !== "OPEN_LUMEN") return;
  e.waitUntil((async () => {
    let opened = false;
    try {
      const url = new URL(e.data.url);
      if (url.origin !== location.origin || url.pathname !== "/app") throw new Error("Invalid app link");
      const windows = await self.clients.matchAll({ type:"window", includeUncontrolled:true });
      let client = windows.find((c) => c.id !== e.source?.id && new URL(c.url).pathname === "/app");
      if (client && (client.url !== url.href || e.data.refresh)) client = await client.navigate(url.href);
      if (!client) client = await self.clients.openWindow(url.href);
      if (client) { await client.focus(); opened = true; }
    } catch {}
    e.ports[0]?.postMessage({ opened });
  })());
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
