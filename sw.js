// sw.js — Service Worker para Mi Casa Inteligente PWA
const CACHE   = 'mi-casa-v2';
const OFFLINE = ['/'];

// Al instalar: cachear la app
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(OFFLINE))
      .then(() => self.skipWaiting())
  );
});

// Al activar: limpiar caches viejos
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// Fetch: red primero, cache como respaldo
self.addEventListener('fetch', e => {
  // No cachear las llamadas a la API de Spotify ni al backend
  const url = e.request.url;
  if (url.includes('127.0.0.1:8888') ||
      url.includes('api.spotify.com') ||
      url.includes('accounts.spotify.com') ||
      url.includes('openweathermap.org')) {
    return; // dejar pasar sin interceptar
  }

  e.respondWith(
    fetch(e.request)
      .then(res => {
        // Cachear respuestas exitosas del dashboard
        if (res.ok && e.request.method === 'GET') {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() =>
        // Sin red: servir desde cache
        caches.match(e.request).then(r => r || caches.match('/'))
      )
  );
});
