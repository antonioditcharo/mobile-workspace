/**
 * Service worker — caches the app shell so Prompt Forge opens instantly and
 * works with no connection.
 *
 * Model weights are deliberately NOT cached here. transformers.js manages its
 * own cache for those, they are hundreds of megabytes, and duplicating them in
 * a second cache would waste a serious amount of a phone's storage.
 */

/**
 * Bump this on every shipped change. The browser only looks for a new service
 * worker when sw.js itself differs, so an unchanged version string means a fix
 * can sit on the server while devices keep running the old cached shell.
 */
const CACHE = 'promptforge-shell-v6';

const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './src/app.js',
  './src/compiler.js',
  './src/eras.js',
  './src/vocab.js',
  './src/vision.js',
  './src/perchance.js',
  './src/runner.js',
  './src/worker.js',
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

      // Network-first, falling back to cache.
      //
      // The obvious choice here is stale-while-revalidate, and it was the
      // original one — but it serves the cached copy first and only refreshes
      // afterwards, so a device stays exactly one load behind the server. That
      // turned a shipped bug fix into "still broken" on first reload. The shell
      // is tiny, so paying a network round trip when online is well worth
      // always running current code; offline still works from the cache.
      try {
        const response = await fetch(request);
        if (response.ok) await cache.put(request, response.clone());
        return response;
      } catch {
        const cached = await cache.match(request, { ignoreSearch: true });
        if (cached) return cached;

        // Offline navigation with a cold cache: fall back to the shell.
        if (request.mode === 'navigate') {
          const shell = await cache.match('./index.html');
          if (shell) return shell;
        }
        return new Response('Offline and not cached.', {
          status: 503,
          headers: { 'Content-Type': 'text/plain' },
        });
      }
    })(),
  );
});
