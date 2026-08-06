/* Service worker: makes the app installable and instant on reopen.

   Deliberately narrow. Only the app shell is cached; API responses and
   generated images never are, because a stale gallery or a cached health
   check would be actively misleading. */

const CACHE = 'filmroom-shell-v1';
const SHELL = [
  '/',
  '/static/styles.css',
  '/static/app.js',
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (event.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;   // always live
  if (url.origin !== self.location.origin) return;

  // Network first so edits to the app show up immediately; cache is only a
  // fallback for when the laptop is asleep or off the network.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match('/')))
  );
});
