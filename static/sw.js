// lbdl service worker — v3
// Strategy:
//   • App shell (static assets + HTML)  → cache-first, stale-while-revalidate
//   • Google Fonts CSS + woff2          → cache-first (immutable once fetched)
//   • /api/* + /ws/*                    → network-only (never cached)
//   • Everything else same-origin       → network-first, fall back to cache

const SHELL_CACHE  = 'lbdl-shell-v3'
const FONT_CACHE   = 'lbdl-fonts-v1'   // separate so font updates don't bust the shell

const SHELL_ASSETS = [
  '/',
  '/static/index.html',
  '/static/manifest.json',
  '/static/icon.svg',
  '/static/sw.js',
]

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(SHELL_CACHE)
      .then(c => c.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  )
})

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k !== SHELL_CACHE && k !== FONT_CACHE)
          .map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  )
})

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const { request } = e
  const url = new URL(request.url)

  // 1. Never intercept non-GET or WebSocket upgrades
  if (request.method !== 'GET') return
  if (request.headers.get('upgrade') === 'websocket') return

  // 2. Never intercept API calls
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return

  // 3. Google Fonts — cache-first (fonts are content-addressed, safe to keep forever)
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(
      caches.open(FONT_CACHE).then(cache =>
        cache.match(request).then(hit => {
          if (hit) return hit
          return fetch(request).then(res => {
            if (res.ok) cache.put(request, res.clone())
            return res
          })
        })
      )
    )
    return
  }

  // 4. Cross-origin requests (thumbnails from YouTube CDN, Cover Art Archive etc.)
  //    — pass through, do not cache (they may be large/dynamic)
  if (url.origin !== self.location.origin) return

  // 5. App shell static assets — cache-first, background refresh
  if (SHELL_ASSETS.includes(url.pathname) || url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(SHELL_CACHE).then(cache =>
        cache.match(request).then(hit => {
          const networkFetch = fetch(request).then(res => {
            if (res.ok) cache.put(request, res.clone())
            return res
          })
          // Return cache immediately; update in background (stale-while-revalidate)
          return hit || networkFetch
        })
      )
    )
    return
  }

  // 6. HTML navigation — network-first, offline fallback to cached shell
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request)
        .then(res => {
          if (res.ok) {
            const clone = res.clone()
            caches.open(SHELL_CACHE).then(c => c.put(request, clone))
          }
          return res
        })
        .catch(() =>
          caches.match('/static/index.html').then(hit =>
            hit || caches.match('/')
          )
        )
    )
  }
})

// ── Background sync placeholder ───────────────────────────────────────────────
// Could be used to queue sync tasks for offline → online transition in the future
self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting()
})
