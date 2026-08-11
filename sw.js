/**
 * Service worker — caches the app shell so Prompt Forge opens instantly and
 * works with no connection.
 *
 * Model weights are deliberately NOT cached here. transformers.js manages its
 * own cache for those, they are hundreds of megabytes, and duplicating them in
 * a second cache would waste a serious amount of a phone's storage.
 */

const CACHE = 'promptforge-shell-v1';

const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './src/app.js',
  './src/compiler.js',
  './src/eras.js',
  './src/vocab.js',
  './src/vision.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // addAll fails the whole install if any single file 404s, which would
      // leave the app with no offline support at all. Tolerate stragglers.
      await Promise.allSettled(SHELL.map((url) => cache.add(url)));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Never intercept the model CDN — let the library's own caching handle it.
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(request, { ignoreSearch: true });

      // Stale-while-revalidate: instant load, quiet background refresh.
      const network = fetch(request)
        .then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => null);

      if (cached) return cached;

      const fresh = await network;
      if (fresh) return fresh;

      // Offline navigation with a cold cache: fall back to the shell.
      if (request.mode === 'navigate') {
        const shell = await cache.match('./index.html');
        if (shell) return shell;
      }
      return new Response('Offline and not cached.', {
        status: 503,
        headers: { 'Content-Type': 'text/plain' },
      });
    })(),
  );
});
