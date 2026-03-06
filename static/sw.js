/**
 * lbdl Service Worker — minimal, safe
 *
 * Only handles same-origin requests. Lets cross-origin (fonts, CDN),
 * data: URIs, WebSocket upgrades, and all API calls pass through untouched.
 */

const CACHE = 'lbdl-v2';

const PRECACHE = ['/', '/static/logo.svg', '/static/icon-192.png', '/static/icon-512.png'];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);

  // ── Hard pass-throughs (do NOT call respondWith — let browser handle) ──
  // 1. Non-HTTP(S) schemes: data:, blob:, chrome-extension:, etc.
  if (!url.protocol.startsWith('http')) return;

  // 2. Cross-origin requests (Google Fonts, CDN, anything external)
  if (url.origin !== self.location.origin) return;

  // 3. API and WebSocket paths — always need live responses
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;

  // ── Same-origin, non-API: cache-first for static, network-first for HTML ──
  if (req.mode === 'navigate') {
    // Navigation: network first, fall back to cached shell
    e.respondWith(
      fetch(req)
        .then(resp => {
          if (resp.ok) caches.open(CACHE).then(c => c.put(req, resp.clone()));
          return resp;
        })
        .catch(() => caches.match('/').then(r => r || fetch(req)))
    );
    return;
  }

  // Static assets: cache-first, update in background
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(req).then(cached => {
          const fresh = fetch(req).then(resp => {
            if (resp.ok) cache.put(req, resp.clone());
            return resp;
          });
          return cached || fresh;
        })
      )
    );
    return;
  }

  // Everything else same-origin: network only (no respondWith = browser default)
});
