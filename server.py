#!/usr/bin/env python3
"""
server.py — Mi Casa Inteligente
Backend completo para Render.com
Sirve: PWA estática + Spotify OAuth2 PKCE + proxy OWM + control de foco Tuya
"""
import http.server, urllib.parse, urllib.request
import json, os, secrets, hashlib, base64
import threading, time, sys

try:
    import tinytuya
    TUYA_AVAILABLE = True
except ImportError:
    TUYA_AVAILABLE = False
    print('  ⚠️  tinytuya no instalado — el control de foco Tuya estará deshabilitado')
    print('      Agrega "tinytuya" a requirements.txt para habilitarlo')

# ── Config ────────────────────────────────────────────────────
SP_CLIENT_ID     = os.environ.get('SP_CLIENT_ID',     '6f85d19e83bb47ac95beb6d94364eff3')
SP_CLIENT_SECRET = os.environ.get('SP_CLIENT_SECRET', '4f52890e442848249fc3524fd8ac271c')
OWM_KEY          = os.environ.get('OWM_KEY', '')       # OpenWeatherMap key
PORT             = int(os.environ.get('PORT', 8888))   # Render inyecta PORT automáticamente
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))

# Redirect URI — en Render usa la URL del servicio
# En local usa 127.0.0.1
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', '')
if RENDER_URL:
    REDIRECT_URI = f'{RENDER_URL}/callback'
else:
    REDIRECT_URI = f'http://127.0.0.1:{PORT}/callback'

SCOPES = ' '.join([
    'user-read-playback-state', 'user-modify-playback-state',
    'user-read-currently-playing', 'streaming',
    'playlist-read-private', 'user-read-email', 'user-read-private',
])

# ── Estado en memoria ─────────────────────────────────────────
store = {
    'access_token':  None,
    'refresh_token': None,
    'expires_at':    0,
    'verifier':      None,
}

# ── PKCE ─────────────────────────────────────────────────────
def b64url(buf):
    if not isinstance(buf, bytes): buf = bytes(buf)
    return base64.urlsafe_b64encode(buf).rstrip(b'=').decode()

def gen_verifier():   return b64url(secrets.token_bytes(64))
def gen_challenge(v): return b64url(hashlib.sha256(v.encode()).digest())

# ── Spotify exchange ──────────────────────────────────────────
def sp_exchange(code, verifier):
    data = urllib.parse.urlencode({
        'grant_type':    'authorization_code',
        'code':           code,
        'redirect_uri':   REDIRECT_URI,
        'client_id':      SP_CLIENT_ID,
        'code_verifier':  verifier,
    }).encode()
    req = urllib.request.Request(
        'https://accounts.spotify.com/api/token', data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def sp_refresh():
    if not store['refresh_token']: return False
    data = urllib.parse.urlencode({
        'grant_type':    'refresh_token',
        'refresh_token':  store['refresh_token'],
        'client_id':      SP_CLIENT_ID,
        'client_secret':  SP_CLIENT_SECRET,
    }).encode()
    try:
        req = urllib.request.Request(
            'https://accounts.spotify.com/api/token', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST')
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        store['access_token'] = d['access_token']
        store['expires_at']   = time.time() + d.get('expires_in', 3600) - 60
        if 'refresh_token' in d: store['refresh_token'] = d['refresh_token']
        print('  🔄 Token renovado')
        return True
    except Exception as e:
        print(f'  ⚠️  Refresh error: {e}')
        return False

# ── Archivos estáticos ────────────────────────────────────────
STATIC = {
    '/':             ('index.html',    'text/html; charset=utf-8'),
    '/index.html':   ('index.html',    'text/html; charset=utf-8'),
    '/manifest.json':('manifest.json', 'application/manifest+json'),
    '/sw.js':        ('sw.js',         'application/javascript'),
    '/icon-192.png': ('icon-192.png',  'image/png'),
    '/icon-512.png': ('icon-512.png',  'image/png'),
}

SUCCESS_PAGE = b'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Conectado</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{display:flex;align-items:center;justify-content:center;
       min-height:100vh;background:#0d1220;font-family:sans-serif;
       color:#4ade80;text-align:center;padding:20px}
  .icon{font-size:72px;margin-bottom:16px}
  h2{color:#4ade80;font-size:24px;margin-bottom:8px}
  p{color:#8b96b0;font-size:14px}
</style></head>
<body>
  <div>
    <div class="icon">&#127925;</div>
    <h2>Spotify conectado</h2>
    <p>Cerrando esta ventana...</p>
    <script>
      setTimeout(() => {
        try { window.close(); } catch(e) {}
        // Si no se puede cerrar, redirigir al dashboard
        setTimeout(() => { window.location.href = '/'; }, 500);
      }, 1500);
    </script>
  </div>
</body></html>'''

# ── Handler HTTP ──────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f'  [{args[1]}] {args[0]}')

    def cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    # ── POST /tuya/command → controla el foco Tuya en la red local ──
    # NOTA: este endpoint solo funciona si el backend corre en la
    # MISMA red local que el foco (ej. tu computadora en casa).
    # Si el backend está en Render.com (nube), no puede alcanzar
    # un dispositivo en tu red doméstica — para eso se necesitaría
    # la Tuya Cloud API en vez de conexión LAN directa.
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path

        if path == '/tuya/command':
            if not TUYA_AVAILABLE:
                self.send_json({'error': 'tinytuya no está instalado en el servidor'}, 500)
                return

            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json({'error': 'JSON inválido'}, 400)
                return

            device_id  = body.get('device_id')
            local_key  = body.get('local_key')
            device_ip  = body.get('device_ip')  # opcional, si no se pasa se autodetecta en LAN
            commands   = body.get('commands', [])

            if not device_id or not local_key:
                self.send_json({'error': 'Faltan device_id o local_key'}, 400)
                return

            try:
                d = tinytuya.OutletDevice(device_id, device_ip or 'Auto', local_key)
                d.set_version(3.3)
                if not device_ip:
                    # Autodetectar IP en la red local mediante escaneo
                    d.set_socketPersistent(False)

                results = []
                for cmd in commands:
                    code  = cmd.get('code')
                    value = cmd.get('value')
                    # Mapear códigos DPS comunes de focos Tuya
                    dps_map = {
                        'switch_led':       1,
                        'work_mode':        2,
                        'bright_value_v2':  3,
                        'temp_value_v2':    4,
                        'colour_data_v2':   5,
                    }
                    dps_id = dps_map.get(code)
                    if dps_id:
                        r = d.set_value(dps_id, value)
                        results.append({'code': code, 'result': str(r)})

                self.send_json({'ok': True, 'results': results})
            except Exception as e:
                print(f'  ❌ Tuya error: {e}')
                self.send_json({'error': str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not found')

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        # ── Archivos estáticos (PWA) ───────────────────────
        if path in STATIC:
            fname, ctype = STATIC[path]
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.exists(fpath):
                body = open(fpath, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type',    ctype)
                self.send_header('Content-Length',  len(body))
                self.send_header('Service-Worker-Allowed', '/')
                # Cache agresivo para assets, no para HTML
                if fname.endswith('.png') or fname == 'manifest.json':
                    self.send_header('Cache-Control', 'public, max-age=86400')
                elif fname == 'sw.js':
                    self.send_header('Cache-Control', 'no-cache')
                else:
                    self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f'Archivo no encontrado: {fname}'.encode())
            return

        # ── GET /login → inicia OAuth2 Spotify ────────────
        if path == '/login':
            verifier  = gen_verifier()
            challenge = gen_challenge(verifier)
            store['verifier'] = verifier
            params = urllib.parse.urlencode({
                'client_id':             SP_CLIENT_ID,
                'response_type':         'code',
                'redirect_uri':          REDIRECT_URI,
                'scope':                 SCOPES,
                'code_challenge_method': 'S256',
                'code_challenge':         challenge,
                'show_dialog':           'false',
            })
            self.send_response(302)
            self.send_header('Location',
                f'https://accounts.spotify.com/authorize?{params}')
            self.end_headers()
            return

        # ── GET /callback → recibe code de Spotify ─────────
        if path == '/callback':
            code  = qs.get('code',  [None])[0]
            error = qs.get('error', [None])[0]
            if error:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(
                    f'<h2 style="color:red;font-family:sans-serif;padding:40px">Error: {error}</h2>'.encode())
                return
            try:
                d = sp_exchange(code, store['verifier'])
                store['access_token']  = d['access_token']
                store['refresh_token']  = d.get('refresh_token')
                store['expires_at']     = time.time() + d.get('expires_in', 3600) - 60
                print(f'  ✅ Spotify token OK — expira en {d.get("expires_in")}s')
                self.send_response(200)
                self.send_header('Content-Type',   'text/html; charset=utf-8')
                self.send_header('Content-Length', len(SUCCESS_PAGE))
                self.end_headers()
                self.wfile.write(SUCCESS_PAGE)
            except Exception as e:
                print(f'  ❌ Exchange error: {e}')
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'Error: {e}'.encode())
            return

        # ── GET /token → devuelve token al frontend ─────────
        if path == '/token':
            if store['access_token'] and time.time() > store['expires_at']:
                sp_refresh()
            if store['access_token']:
                self.send_json({'access_token': store['access_token'], 'ok': True})
            else:
                self.send_json({'ok': False, 'error': 'not_authorized'}, 401)
            return

        # ── GET /weather?lat=X&lon=Y → proxy OWM ───────────
        # Evita exponer la API key al cliente
        if path == '/weather':
            lat = qs.get('lat', [None])[0]
            lon = qs.get('lon', [None])[0]
            key = qs.get('key', [OWM_KEY])[0]  # acepta key del cliente también
            if not lat or not lon or not key:
                self.send_json({'error': 'Faltan parámetros: lat, lon, key'}, 400)
                return
            try:
                wurl = f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={key}&units=metric&lang=es'
                with urllib.request.urlopen(wurl) as r:
                    data = json.loads(r.read())
                self.send_json(data)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # ── GET /forecast?lat=X&lon=Y → proxy OWM forecast ─
        if path == '/forecast':
            lat = qs.get('lat', [None])[0]
            lon = qs.get('lon', [None])[0]
            key = qs.get('key', [OWM_KEY])[0]
            if not lat or not lon or not key:
                self.send_json({'error': 'Faltan parámetros'}, 400)
                return
            try:
                furl = f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={key}&units=metric&lang=es'
                with urllib.request.urlopen(furl) as r:
                    data = json.loads(r.read())
                self.send_json(data)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # ── GET /health → health check para Render ──────────
        if path == '/health':
            self.send_json({
                'status':      'ok',
                'spotify':     store['access_token'] is not None,
                'redirect_uri': REDIRECT_URI,
            })
            return

        # 404
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not found')


# ── Auto-refresh token cada 50 min ───────────────────────────
def auto_refresh_loop():
    while True:
        time.sleep(3000)
        if store['refresh_token']:
            print('  🔄 Auto-refresh...')
            sp_refresh()


# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    print()
    print('╔════════════════════════════════════════════════╗')
    print('║   🏠  Mi Casa Inteligente — Servidor           ║')
    print('╠════════════════════════════════════════════════╣')
    print(f'║  Puerto:       {PORT}                              ║')
    print(f'║  Redirect URI: {REDIRECT_URI[:44]}  ║')
    print('╚════════════════════════════════════════════════╝')
    print()
    if RENDER_URL:
        print(f'  🌐 URL pública:  {RENDER_URL}')
    print(f'  🏠 Health check: http://localhost:{PORT}/health')
    print()

    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'  Escuchando en 0.0.0.0:{PORT} ...')
    print('  Ctrl+C para detener\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  ⏹  Servidor detenido')
