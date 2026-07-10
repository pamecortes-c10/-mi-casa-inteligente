// sw.js — Service Worker para Mi Casa Inteligente PWA
const CACHE   = 'mi-casa-v4';
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

// Rutas dinámicas del propio backend que NUNCA deben cachearse
// (proxies a Spotify/OWM/NewsAPI, control Tuya, tokens, etc.)
const DYNAMIC_PATHS = ['/news', '/weather', '/forecast', '/token', '/login',
                        '/callback', '/tuya/command', '/health'];

// Fetch: red primero, cache como respaldo
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // No cachear las llamadas a la API de Spotify, OWM, NewsAPI ni al backend dinámico
  if (url.hostname.includes('127.0.0.1') ||
      url.hostname.includes('api.spotify.com') ||
      url.hostname.includes('accounts.spotify.com') ||
      url.hostname.includes('openweathermap.org') ||
      url.hostname.includes('newsapi.org') ||
      url.hostname.includes('rss2json.com') ||
      DYNAMIC_PATHS.some(p => url.pathname.startsWith(p))) {
    return; // dejar pasar sin interceptar
  }

  e.respondWith(
    // {cache:'no-store'} obliga al navegador a ignorar por completo su
    // caché HTTP normal y siempre ir a la red — no solo confiar en los
    // headers Cache-Control del servidor.
    fetch(e.request, { cache: 'no-store' })
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
