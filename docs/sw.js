// Bump V when you change index.html and want every user to get it on the next visit.
const V = "sif-v2";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
const put = (r, res) => { if (res && res.ok) { const cp = res.clone(); caches.open(V).then(c => c.put(r, cp)); } return res; };

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET" || new URL(r.url).origin !== location.origin) return;
  if (new URL(r.url).pathname.endsWith("/data/sif.json")) {
    // data: always try the network first, fall back to the last saved copy when offline
    e.respondWith(fetch(r).then(res => put(r, res)).catch(() => caches.match(r)));
    return;
  }
  // app files: show the saved copy instantly and refresh it in the background
  e.respondWith(caches.match(r).then(m => {
    const net = fetch(r).then(res => put(r, res)).catch(() => m);
    return m || net;
  }));
});
