const CACHE = 'lbdl-v2'
const SHELL = ['/', '/static/index.html', '/static/manifest.json', '/static/icon.svg', '/static/sw.js']

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url)
  // Never intercept API, WebSocket upgrades, or cross-origin requests
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/') ||
      url.origin !== self.location.origin) return

  if (url.pathname.startsWith('/static/') && !url.pathname.endsWith('.html')) {
    // Cache-first for static assets
    e.respondWith(
      caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
        const clone = res.clone()
        caches.open(CACHE).then(c => c.put(e.request, clone))
        return res
      }))
    )
  } else {
    // Network-first for HTML, fall back offline
    e.respondWith(
      fetch(e.request)
        .then(res => { caches.open(CACHE).then(c => c.put(e.request, res.clone())); return res })
        .catch(() => caches.match(e.request))
    )
  }
})
