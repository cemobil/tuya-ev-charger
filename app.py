import os
import time
import json
import hmac
import hashlib
import urllib.request
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

CLIENT_ID = os.environ.get("TUYA_CLIENT_ID", "xsvqhssh3uakykgt5r9g")
CLIENT_SECRET = os.environ.get("TUYA_CLIENT_SECRET", "8f062b60f38348b581c6e6bbd91fe862")
ENDPOINT = os.environ.get("TUYA_ENDPOINT", "https://openapi.tuyaeu.com")
DEVICE_ID = os.environ.get("TUYA_DEVICE_ID", "bfc471cc7ee72e1f8bwmi3")

AUTH_USER = os.environ.get("AUTH_USER", "cemobil")
AUTH_PASS = os.environ.get("AUTH_PASS", "Cehomeassistant1.")
AUTH_SALT = "tuya_ev_secure_salt_2026_x89"
SESSION_TOKEN = hashlib.sha256(f"{AUTH_USER}:{AUTH_PASS}:{AUTH_SALT}".encode()).hexdigest()

def check_auth(handler):
    # 1. Cookie check
    cookie_header = handler.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("session_token="):
            token = part.split("=", 1)[1]
            if token == SESSION_TOKEN:
                return True
    # 2. Authorization Bearer check
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token == SESSION_TOKEN:
            return True
    # 3. URL query token check
    url = urlparse(handler.path)
    params = parse_qs(url.query)
    if params.get("token", [""])[0] == SESSION_TOKEN:
        return True
    return False

STATE = {
    "device_connected": True,
    "work_state": "charger_free",
    "switch": False,
    "charge_cur_set": 10,
    "power_total_kw": 0.0,
    "temperature": 38,
    "forward_energy_total": 592.18,
    "charge_energy_once": 2.66,
    
    # Smart charge logic
    "car_battery_capacity_kwh": 54.0,
    "mevcut_sarj_yuzdesi": 42.0,
    "hedef_sarj_yuzdesi": 90.0,
    "ilk_baslangic_yuzdesi": 40.0,
    "oturum_enerjisi_kwh": 1.04,
    "onceki_cihaz_oturum_enerjisi": 1.04,
    "sarj_aktif": False,
    "son_veri_bekleniyor": False,
    "oto_kesme_aktif": True,
    "tahmini_sarj_yuzdesi": 41.9,
    "kalan_enerji_kwh": 25.97,
    "kalan_sure_str": "11 sa 48 dk",
    "varis_saati_str": "04.11 05:30",
    "veri_durumu": "✅ Kesin Cihaz Verisi İle Güncel",
    "last_sync": ""
}

LOCK = threading.Lock()
TOKEN_CACHE = {"token": None, "expire_at": 0}

def get_tuya_token():
    now = time.time()
    if TOKEN_CACHE["token"] and now < TOKEN_CACHE["expire_at"]:
        return TOKEN_CACHE["token"]
    
    t = str(int(now * 1000))
    content_sha256 = hashlib.sha256("".encode('utf-8')).hexdigest()
    string_to_sign = f"GET\n{content_sha256}\n\n/v1.0/token?grant_type=1"
    sign_str = CLIENT_ID + t + string_to_sign
    sign = hmac.new(CLIENT_SECRET.encode('utf-8'), sign_str.encode('utf-8'), hashlib.sha256).hexdigest().upper()
    
    req = urllib.request.Request(f"{ENDPOINT}/v1.0/token?grant_type=1", headers={
        "client_id": CLIENT_ID, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode())
            if res.get("success"):
                tk = res["result"]["access_token"]
                exp = res["result"]["expire_time"]
                TOKEN_CACHE["token"] = tk
                TOKEN_CACHE["expire_at"] = now + exp - 60
                return tk
    except Exception as e:
        print("get_tuya_token err:", e)
    return None

def tuya_request(method, path, body_dict=None):
    token = get_tuya_token()
    if not token:
        return None
    now = time.time()
    t = str(int(now * 1000))
    body_str = json.dumps(body_dict) if body_dict is not None else ""
    content_sha256 = hashlib.sha256(body_str.encode('utf-8')).hexdigest()
    string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
    sign_str = CLIENT_ID + token + t + string_to_sign
    sign = hmac.new(CLIENT_SECRET.encode('utf-8'), sign_str.encode('utf-8'), hashlib.sha256).hexdigest().upper()
    
    headers = {
        "client_id": CLIENT_ID, "access_token": token, "sign": sign, "t": t,
        "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
    }
    data = body_str.encode('utf-8') if body_dict is not None else None
    try:
        req = urllib.request.Request(f"{ENDPOINT}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print("tuya_request err:", e)
        return None

def tuya_send_commands(commands):
    path = f"/v1.0/devices/{DEVICE_ID}/commands"
    return tuya_request("POST", path, {"commands": commands})

def recalculate_projections():
    with LOCK:
        hedef = float(STATE["hedef_sarj_yuzdesi"])
        mevcut = float(STATE["tahmini_sarj_yuzdesi"])
        cap = float(STATE["car_battery_capacity_kwh"])
        
        if hedef <= mevcut:
            STATE["kalan_enerji_kwh"] = 0.0
            STATE["kalan_sure_str"] = "0 dk (Tamamlandı)"
            STATE["varis_saati_str"] = "Hedefe Ulaşıldı"
            return

        kalan_kwh = ((hedef - mevcut) * cap) / 100.0
        STATE["kalan_enerji_kwh"] = round(kalan_kwh, 2)
        
        p = float(STATE["power_total_kw"])
        if p <= 0.05:
            akim = float(STATE["charge_cur_set"] or 10.0)
            p = (akim * 220.0) / 1000.0
            
        if p > 0.05:
            toplam_dk = int(round((kalan_kwh / p) * 60.0))
            sa = toplam_dk // 60
            dk = toplam_dk % 60
            STATE["kalan_sure_str"] = f"{sa} sa {dk} dk" if sa > 0 else f"{dk} dk"
            
            target_ts = time.time() + (toplam_dk * 60)
            local_target = time.localtime(target_ts)
            local_now = time.localtime(time.time())
            
            saat_str = time.strftime("%H:%M", local_target)
            if local_target.tm_mday == local_now.tm_mday:
                STATE["varis_saati_str"] = f"Bugün {saat_str}"
            else:
                gun_str = time.strftime("%d.%m", local_target)
                STATE["varis_saati_str"] = f"{gun_str} {saat_str}"
        else:
            STATE["kalan_sure_str"] = "Hesaplanıyor..."
            STATE["varis_saati_str"] = "Hesaplanıyor..."

def sync_tuya_status():
    try:
        path = f"/v1.0/devices/{DEVICE_ID}/status"
        res = tuya_request("GET", path)
        if not res or not res.get("success"):
            return
        
        items = {item["code"]: item["value"] for item in res.get("result", [])}
        with LOCK:
            STATE["device_connected"] = True
            STATE["work_state"] = items.get("work_state", STATE["work_state"])
            STATE["switch"] = bool(items.get("switch", STATE["switch"]))
            STATE["charge_cur_set"] = int(items.get("charge_cur_set", STATE["charge_cur_set"]))
            
            raw_p = items.get("power_total", items.get("sigle_phase_power", 0))
            STATE["power_total_kw"] = round(float(raw_p) / 1000.0 if raw_p > 50 else float(raw_p), 3)
            
            if "forward_energy_total" in items:
                STATE["forward_energy_total"] = round(float(items["forward_energy_total"]) / 100.0, 2)
            if "charge_energy_once" in items:
                STATE["charge_energy_once"] = round(float(items["charge_energy_once"]) / 100.0, 2)
            if "temp_current" in items:
                STATE["temperature"] = int(items["temp_current"])
            STATE["last_sync"] = time.strftime("%H:%M:%S")
            
            is_charging = (STATE["work_state"] == "charger_charging") or (STATE["switch"] and STATE["power_total_kw"] > 0.1)
            
            if is_charging and not STATE["sarj_aktif"]:
                STATE["sarj_aktif"] = True
                STATE["son_veri_bekleniyor"] = False
                STATE["oturum_enerjisi_kwh"] = 0.0
                STATE["ilk_baslangic_yuzdesi"] = STATE["mevcut_sarj_yuzdesi"]
                STATE["onceki_cihaz_oturum_enerjisi"] = STATE["charge_energy_once"]
                STATE["veri_durumu"] = "⚡ Anlık Güçten Hesaplanıyor"

            if STATE["sarj_aktif"]:
                if is_charging:
                    p = STATE["power_total_kw"]
                    if p <= 0.05:
                        p = (float(STATE["charge_cur_set"]) * 220.0) / 1000.0
                    STATE["oturum_enerjisi_kwh"] = round(STATE["oturum_enerjisi_kwh"] + (p / 60.0), 3)
                    
                    artis = (STATE["oturum_enerjisi_kwh"] / STATE["car_battery_capacity_kwh"]) * 100.0
                    STATE["tahmini_sarj_yuzdesi"] = min(100.0, round(STATE["ilk_baslangic_yuzdesi"] + artis, 1))
                    STATE["mevcut_sarj_yuzdesi"] = round(STATE["tahmini_sarj_yuzdesi"])
                    STATE["veri_durumu"] = "⚡ Anlık Güçten Hesaplanıyor"
                    
                    if STATE["oto_kesme_aktif"] and STATE["tahmini_sarj_yuzdesi"] >= STATE["hedef_sarj_yuzdesi"]:
                        print(f"Target reached: {STATE['tahmini_sarj_yuzdesi']}% >= {STATE['hedef_sarj_yuzdesi']}%. Stopping charge.")
                        tuya_send_commands([{"code": "switch", "value": False}])
                        STATE["sarj_aktif"] = False
                        STATE["son_veri_bekleniyor"] = True
                        STATE["veri_durumu"] = "⏳ Cihazdan Son Enerji Bekleniyor..."
                else:
                    STATE["sarj_aktif"] = False
                    STATE["son_veri_bekleniyor"] = True
                    STATE["veri_durumu"] = "⏳ Cihazdan Son Enerji Bekleniyor..."
            
            if STATE["son_veri_bekleniyor"] and not STATE["sarj_aktif"]:
                hw_once = STATE["charge_energy_once"]
                prev_hw = STATE["onceki_cihaz_oturum_enerjisi"]
                if hw_once > 0.01 and abs(hw_once - prev_hw) > 0.005:
                    print(f"Cihaz son oturum enerjisi olustu: {hw_once} kWh. Kesin kalibre yapiliyor.")
                    STATE["oturum_enerjisi_kwh"] = hw_once
                    artis = (hw_once / STATE["car_battery_capacity_kwh"]) * 100.0
                    STATE["tahmini_sarj_yuzdesi"] = min(100.0, round(STATE["ilk_baslangic_yuzdesi"] + artis, 1))
                    STATE["mevcut_sarj_yuzdesi"] = round(STATE["tahmini_sarj_yuzdesi"])
                    STATE["onceki_cihaz_oturum_enerjisi"] = hw_once
                    STATE["son_veri_bekleniyor"] = False
                    STATE["veri_durumu"] = "✅ Kesin Cihaz Verisi İle Güncel"
    except Exception as e:
        print("sync_tuya_status error:", e)
    finally:
        recalculate_projections()

def background_worker():
    while True:
        sync_tuya_status()
        time.sleep(60)

# SVG Icon (Charging Station)
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="128" fill="#0b0f19"/>
  <path d="M128 96h176c17.7 0 32 14.3 32 32v288H96V128c0-17.7 14.3-32 32-32z" fill="#1e293b"/>
  <path d="M128 144h176v96H128z" fill="#0f172a" stroke="#06b6d4" stroke-width="8"/>
  <path d="M216 160l-32 40h40l-24 32 48-40h-40z" fill="#06b6d4"/>
  <path d="M336 208h32c17.7 0 32 14.3 32 32v112c0 26.5-21.5 48-48 48s-48-21.5-48-48v-80" fill="none" stroke="#3b82f6" stroke-width="20" stroke-linecap="round"/>
  <rect x="368" y="176" width="32" height="40" rx="8" fill="#3b82f6"/>
</svg>"""

DIR_PATH = os.path.dirname(os.path.abspath(__file__))
ICON_192_DATA = b""
ICON_512_DATA = b""
try:
    p192 = os.path.join(DIR_PATH, "icon-192.png")
    if os.path.exists(p192):
        with open(p192, "rb") as f:
            ICON_192_DATA = f.read()
    p512 = os.path.join(DIR_PATH, "icon-512.png")
    if os.path.exists(p512):
        with open(p512, "rb") as f:
            ICON_512_DATA = f.read()
except Exception as e:
    print(f"Icon load error: {e}")

MANIFEST_JSON = {
    "name": "AC Şarj İstasyonu",
    "short_name": "AC Şarj",
    "description": "54 kWh Akıllı AC Şarj İstasyonu Yönetim Paneli",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0f19",
    "theme_color": "#0b0f19",
    "orientation": "portrait",
    "icons": [
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any"
        },
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "maskable"
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any"
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable"
        },
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml"
        }
    ]
}

SERVICE_WORKER_JS = """
const CACHE_NAME = 'ev-charger-v1';
self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
});
self.addEventListener('fetch', (e) => {
  // Network first
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
"""

APP_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="theme-color" content="#0b0f19">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="AC Şarj">
  <link rel="manifest" href="/manifest.json">
  <link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
  <link rel="apple-touch-icon" href="/icon-192.png">
  <title>AC Şarj İstasyonu</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background-color: #0b0f19; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
    .glass { background: rgba(26, 32, 53, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .pulse-ring { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .7; transform: scale(1.05); } }
  </style>
</head>
<body class="text-white pb-8 select-none">
  <!-- LOGIN VIEW -->
  <div id="login-view" class="min-h-screen flex items-center justify-center p-4">
    <div class="glass w-full max-w-sm rounded-3xl p-6 sm:p-8 shadow-2xl relative border border-white/10 overflow-hidden">
      <div class="absolute -right-8 -top-8 w-32 h-32 bg-cyan-500/10 rounded-full blur-2xl pointer-events-none"></div>
      <div class="absolute -left-8 -bottom-8 w-32 h-32 bg-blue-500/10 rounded-full blur-2xl pointer-events-none"></div>

      <div class="text-center mb-6">
        <div class="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center shadow-lg shadow-cyan-500/30">
          <i class="fa-solid fa-charging-station text-white text-3xl"></i>
        </div>
        <h2 class="text-xl font-bold tracking-tight">AC Şarj İstasyonu</h2>
        <p class="text-xs text-gray-400 mt-1">Yönetim Paneli Girişi</p>
      </div>

      <form id="form-login" onsubmit="handleLogin(event)" class="space-y-4">
        <div id="login-err" class="hidden text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 p-2.5 rounded-xl text-center"></div>

        <div>
          <label class="block text-xs font-semibold text-gray-400 mb-1.5 ml-1">Kullanıcı Adı</label>
          <div class="relative">
            <span class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-gray-500 text-sm">
              <i class="fa-solid fa-user"></i>
            </span>
            <input id="login-user" type="text" required autocapitalize="off" autocomplete="username"
              placeholder="cemobil" class="w-full pl-10 pr-4 py-3 bg-gray-900/80 border border-gray-700/80 rounded-xl text-sm focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500 transition text-white placeholder-gray-600">
          </div>
        </div>

        <div>
          <label class="block text-xs font-semibold text-gray-400 mb-1.5 ml-1">Şifre</label>
          <div class="relative">
            <span class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-gray-500 text-sm">
              <i class="fa-solid fa-lock"></i>
            </span>
            <input id="login-pass" type="password" required autocomplete="current-password"
              placeholder="••••••••" class="w-full pl-10 pr-10 py-3 bg-gray-900/80 border border-gray-700/80 rounded-xl text-sm focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500 transition text-white placeholder-gray-600">
            <button type="button" onclick="togglePassVisibility()" class="absolute inset-y-0 right-0 pr-3.5 flex items-center text-gray-400 hover:text-white transition">
              <i id="pass-eye" class="fa-solid fa-eye text-sm"></i>
            </button>
          </div>
        </div>

        <button type="submit" id="btn-login-submit"
          class="w-full py-3.5 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 font-bold text-sm text-white shadow-lg shadow-cyan-500/25 active:scale-[0.98] transition flex items-center justify-center gap-2 mt-2">
          <span>Giriş Yap</span>
          <i class="fa-solid fa-arrow-right text-xs"></i>
        </button>
      </form>
    </div>
  </div>

  <!-- DASHBOARD VIEW -->
  <div id="app-view" class="hidden">
    <!-- PWA Install Banner (If browser prompts) -->
    <div id="pwa-install-banner" class="hidden mx-4 mt-3 p-3 glass rounded-2xl border-cyan-500/30 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <img src="/icon-192.png" class="w-9 h-9 rounded-xl">
        <div>
          <p class="font-bold text-xs">Uygulamayı Telefona Yükle</p>
          <p class="text-[11px] text-gray-400">Tek dokunuşla ana ekrana kur</p>
        </div>
      </div>
      <button id="btn-install-pwa" class="px-3 py-1.5 rounded-xl bg-cyan-600 font-bold text-xs text-white shadow-lg shadow-cyan-600/30">
        Yükle
      </button>
    </div>

    <!-- Top App Bar -->
    <header class="p-4 flex items-center justify-between border-b border-gray-800/60 sticky top-0 bg-[#0b0f19]/90 backdrop-blur z-20">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center shadow-lg shadow-cyan-500/20">
          <i class="fa-solid fa-charging-station text-white text-lg"></i>
        </div>
        <div>
          <h1 class="font-bold text-base leading-tight">AC Şarj İstasyonu</h1>
          <p id="sub-status" class="text-xs text-gray-400">Bulut Bağlantısı Aktif</p>
        </div>
      </div>
      <div class="flex items-center space-x-2">
        <span id="badge-charging" class="px-2.5 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-400 flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-gray-500"></span> Beklemede
        </span>
        <button onclick="refreshData()" title="Yenile" class="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 active:scale-95 transition">
          <i class="fa-solid fa-rotate text-sm"></i>
        </button>
        <button onclick="doLogout()" title="Çıkış Yap" class="w-9 h-9 rounded-xl glass flex items-center justify-center text-rose-400/80 hover:text-rose-400 active:scale-95 transition">
          <i class="fa-solid fa-right-from-bracket text-sm"></i>
        </button>
      </div>
    </header>

  <main class="p-4 space-y-4 max-w-md mx-auto">
    <!-- Big Battery SoC Card -->
    <div class="glass rounded-3xl p-6 text-center relative overflow-hidden shadow-2xl">
      <div class="absolute -right-10 -top-10 w-40 h-40 bg-blue-500/10 rounded-full blur-3xl pointer-events-none"></div>
      <div class="absolute -left-10 -bottom-10 w-40 h-40 bg-cyan-500/10 rounded-full blur-3xl pointer-events-none"></div>
      
      <p class="text-xs font-medium tracking-wider text-cyan-400 uppercase mb-2">Batarya Doluluk Seviyesi (54 kWh)</p>
      
      <!-- Visual Battery Gauge -->
      <div class="relative w-44 h-44 mx-auto flex items-center justify-center my-1">
        <svg class="w-full h-full transform -rotate-90" viewBox="0 0 100 100">
          <circle cx="50" cy="50" r="42" stroke="rgba(255,255,255,0.06)" stroke-width="9" fill="none"/>
          <circle id="gauge-circle" cx="50" cy="50" r="42" stroke="url(#cyanGrad)" stroke-width="9" stroke-linecap="round" fill="none"
            stroke-dasharray="264" stroke-dashoffset="150" class="transition-all duration-700"/>
          <defs>
            <linearGradient id="cyanGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#06b6d4" />
              <stop offset="100%" stop-color="#3b82f6" />
            </linearGradient>
          </defs>
        </svg>
        <div class="absolute flex flex-col items-center">
          <span id="txt-soc" class="text-4xl font-black tracking-tight">--%</span>
          <span id="txt-kw" class="text-xs text-gray-400 font-semibold mt-0.5">0.0 kW</span>
        </div>
      </div>

      <div class="mt-3 py-1.5 px-3 rounded-full bg-gray-900/60 inline-flex items-center gap-2 border border-white/5">
        <i class="fa-solid fa-circle-info text-cyan-400 text-xs"></i>
        <span id="txt-veri-durumu" class="text-xs text-gray-300">Veri Durumu Yükleniyor...</span>
      </div>
    </div>

    <!-- Live 4-Stats Grid -->
    <div class="grid grid-cols-2 gap-3">
      <div class="glass rounded-2xl p-3.5">
        <div class="flex items-center gap-2 text-amber-400 text-xs font-semibold mb-1">
          <i class="fa-solid fa-hourglass-half"></i> Tahmini Kalan Süre
        </div>
        <div id="txt-kalan-sure" class="text-lg font-bold text-white tracking-tight">--</div>
        <p class="text-[11px] text-gray-400 mt-0.5">Hedefe ulaşma süresi</p>
      </div>

      <div class="glass rounded-2xl p-3.5">
        <div class="flex items-center gap-2 text-teal-400 text-xs font-semibold mb-1">
          <i class="fa-regular fa-clock"></i> Hedefe Varış Saati
        </div>
        <div id="txt-varis-saati" class="text-lg font-bold text-white tracking-tight">--</div>
        <p class="text-[11px] text-gray-400 mt-0.5">Tamamlanma saati</p>
      </div>

      <div class="glass rounded-2xl p-3.5">
        <div class="flex items-center gap-2 text-green-400 text-xs font-semibold mb-1">
          <i class="fa-solid fa-bolt"></i> Oturum Enerjisi
        </div>
        <div class="text-lg font-bold text-white tracking-tight"><span id="txt-oturum-kwh">--</span> <span class="text-xs font-normal text-gray-400">kWh</span></div>
        <p class="text-[11px] text-gray-400 mt-0.5">Bu şarjda aktarılan</p>
      </div>

      <div class="glass rounded-2xl p-3.5">
        <div class="flex items-center gap-2 text-blue-400 text-xs font-semibold mb-1">
          <i class="fa-solid fa-battery-half"></i> Hedefe İhtiyaç
        </div>
        <div class="text-lg font-bold text-white tracking-tight"><span id="txt-kalan-kwh">--</span> <span class="text-xs font-normal text-gray-400">kWh</span></div>
        <p class="text-[11px] text-gray-400 mt-0.5">Kalan enerji açığı</p>
      </div>
    </div>

    <!-- Start / Stop Actions -->
    <div class="grid grid-cols-2 gap-3">
      <button onclick="controlAction('start')" class="py-3.5 px-4 rounded-2xl bg-gradient-to-r from-emerald-600 to-teal-600 active:scale-98 font-bold text-sm shadow-lg shadow-emerald-600/30 flex items-center justify-center gap-2 transition">
        <i class="fa-solid fa-play"></i> Şarjı Başlat
      </button>
      <button onclick="controlAction('stop')" class="py-3.5 px-4 rounded-2xl bg-gradient-to-r from-rose-600 to-red-600 active:scale-98 font-bold text-sm shadow-lg shadow-red-600/30 flex items-center justify-center gap-2 transition">
        <i class="fa-solid fa-stop"></i> Şarjı Durdur
      </button>
    </div>

    <!-- Current Selector (8A, 10A, 12A, 15A) -->
    <div class="glass rounded-2xl p-4">
      <div class="flex items-center justify-between mb-3">
        <span class="text-xs font-semibold text-gray-300 flex items-center gap-2">
          <i class="fa-solid fa-wave-square text-blue-400"></i> Şarj Akımı Seçimi
        </span>
        <span id="txt-cur-selected" class="text-xs font-bold text-cyan-400 px-2 py-0.5 rounded bg-cyan-950/60 border border-cyan-800/40">10 A</span>
      </div>
      <div class="grid grid-cols-4 gap-2">
        <button onclick="setCurrent(8)" id="btn-cur-8" class="py-2 rounded-xl text-xs font-bold glass active:scale-95 transition">8A</button>
        <button onclick="setCurrent(10)" id="btn-cur-10" class="py-2 rounded-xl text-xs font-bold glass active:scale-95 transition">10A</button>
        <button onclick="setCurrent(12)" id="btn-cur-12" class="py-2 rounded-xl text-xs font-bold glass active:scale-95 transition">12A</button>
        <button onclick="setCurrent(15)" id="btn-cur-15" class="py-2 rounded-xl text-xs font-bold glass active:scale-95 transition">15A</button>
      </div>
    </div>

    <!-- Target & Manual Calibration Sliders -->
    <div class="glass rounded-2xl p-4 space-y-4">
      <!-- Target Slider -->
      <div>
        <div class="flex justify-between items-center mb-1.5">
          <label class="text-xs font-semibold text-gray-300 flex items-center gap-2">
            <i class="fa-solid fa-bullseye text-cyan-400"></i> Hedef Şarj Yüzdesi
          </label>
          <span id="lbl-hedef" class="text-xs font-bold text-cyan-400 px-2 py-0.5 rounded bg-cyan-950/60">%90</span>
        </div>
        <input type="range" id="rng-hedef" min="10" max="100" step="1" value="90" oninput="onTargetChange(this.value)"
          class="w-full accent-cyan-500 h-2 bg-gray-800 rounded-lg cursor-pointer">
      </div>

      <!-- Current Manual SoC Adjustment -->
      <div class="pt-2 border-t border-gray-800/60">
        <div class="flex justify-between items-center mb-1.5">
          <label class="text-xs font-semibold text-gray-300 flex items-center gap-2">
            <i class="fa-solid fa-pen-to-square text-amber-400"></i> Mevcut Şarj Düzeltme
          </label>
          <span id="lbl-mevcut" class="text-xs font-bold text-amber-400 px-2 py-0.5 rounded bg-amber-950/60">%42</span>
        </div>
        <input type="range" id="rng-mevcut" min="0" max="100" step="1" value="42" oninput="onMevcutChange(this.value)"
          class="w-full accent-amber-500 h-2 bg-gray-800 rounded-lg cursor-pointer">
        <p class="text-[10px] text-gray-400 mt-1">Araç ekranıyla uyuşmazsa kaydırarak kalibre edin.</p>
      </div>

      <!-- Auto Cutoff Toggle -->
      <div class="pt-2 border-t border-gray-800/60 flex items-center justify-between">
        <span class="text-xs font-semibold text-gray-300 flex items-center gap-2">
          <i class="fa-solid fa-shield-halved text-emerald-400"></i> Hedefe Ulaşınca Otomatik Kes
        </span>
        <input type="checkbox" id="chk-autocut" checked onchange="toggleAutoCut(this.checked)" class="w-5 h-5 accent-emerald-500 rounded">
      </div>
    </div>

    <!-- Device Details Footer Card -->
    <div class="glass rounded-2xl p-3.5 grid grid-cols-3 gap-2 text-center text-xs">
      <div>
        <p class="text-gray-400 text-[11px]">Sıcaklık</p>
        <p id="txt-temp" class="font-bold text-orange-400 mt-0.5">-- °C</p>
      </div>
      <div class="border-x border-gray-800">
        <p class="text-gray-400 text-[11px]">Toplam Sayaç</p>
        <p id="txt-meter" class="font-bold text-purple-400 mt-0.5">-- kWh</p>
      </div>
      <div>
        <p class="text-gray-400 text-[11px]">Son Güncelleme</p>
        <p id="txt-sync" class="font-bold text-gray-300 mt-0.5">--</p>
      </div>
    </div>
  </main>
  </div><!-- End app-view -->

  <script>
    // Register Service Worker for PWA
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').then(reg => {
        console.log('PWA ServiceWorker registered');
      }).catch(err => {
        console.warn('PWA ServiceWorker error:', err);
      });
    }

    // PWA Install Prompt Listener
    let deferredPrompt;
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferredPrompt = e;
      const banner = document.getElementById('pwa-install-banner');
      if (banner) banner.classList.remove('hidden');
    });

    const installBtn = document.getElementById('btn-install-pwa');
    if (installBtn) {
      installBtn.addEventListener('click', async () => {
        if (deferredPrompt) {
          deferredPrompt.prompt();
          const { outcome } = await deferredPrompt.userChoice;
          console.log('User response:', outcome);
          deferredPrompt = null;
          document.getElementById('pwa-install-banner').classList.add('hidden');
        }
      });
    }

    let authToken = localStorage.getItem('ev_token') || '';
    let appState = {};

    function togglePassVisibility() {
      const p = document.getElementById('login-pass');
      const eye = document.getElementById('pass-eye');
      if (p.type === 'password') {
        p.type = 'text';
        eye.className = 'fa-solid fa-eye-slash text-sm';
      } else {
        p.type = 'password';
        eye.className = 'fa-solid fa-eye text-sm';
      }
    }

    function showLogin() {
      document.getElementById('login-view').classList.remove('hidden');
      document.getElementById('app-view').classList.add('hidden');
    }

    function showApp() {
      document.getElementById('login-view').classList.add('hidden');
      document.getElementById('app-view').classList.remove('hidden');
    }

    async function handleLogin(e) {
      e.preventDefault();
      const user = document.getElementById('login-user').value.trim();
      const pass = document.getElementById('login-pass').value;
      const btn = document.getElementById('btn-login-submit');
      const errDiv = document.getElementById('login-err');

      errDiv.classList.add('hidden');
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> <span>Giriş yapılıyor...</span>';

      try {
        const res = await fetch('/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: user, password: pass })
        });
        const data = await res.json();
        if (res.ok && data.success) {
          authToken = data.token;
          localStorage.setItem('ev_token', authToken);
          showApp();
          fetchState();
        } else {
          errDiv.innerText = data.error || 'Kullanıcı adı veya şifre hatalı!';
          errDiv.classList.remove('hidden');
        }
      } catch (err) {
        errDiv.innerText = 'Sunucuya bağlanılamadı!';
        errDiv.classList.remove('hidden');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>Giriş Yap</span> <i class="fa-solid fa-arrow-right text-xs"></i>';
      }
    }

    async function doLogout() {
      try {
        await fetch('/api/logout', { method: 'POST' });
      } catch (e) {}
      authToken = '';
      localStorage.removeItem('ev_token');
      showLogin();
    }

    async function authFetch(url, options = {}) {
      options.headers = options.headers || {};
      if (authToken) {
        options.headers['Authorization'] = 'Bearer ' + authToken;
      }
      try {
        const res = await fetch(url, options);
        if (res.status === 401) {
          authToken = '';
          localStorage.removeItem('ev_token');
          showLogin();
          return null;
        }
        return res;
      } catch (err) {
        console.error('Fetch error:', err);
        return null;
      }
    }

    async function fetchState() {
      const res = await authFetch('/api/state');
      if (res && res.ok) {
        appState = await res.json();
        showApp();
        renderUI();
      }
    }

    function renderUI() {
      if (!appState) return;
      const isCharging = appState.sarj_aktif || appState.work_state === 'charger_charging';
      
      const badge = document.getElementById('badge-charging');
      if (isCharging) {
        badge.className = 'px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-950/80 text-emerald-400 border border-emerald-800/60 flex items-center gap-1.5 pulse-ring';
        badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span> Şarj Ediliyor';
      } else if (appState.son_veri_bekleniyor) {
        badge.className = 'px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-950/80 text-amber-400 border border-amber-800/60 flex items-center gap-1.5';
        badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-400"></span> Kalibre Ediliyor';
      } else {
        badge.className = 'px-2.5 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-400 flex items-center gap-1.5';
        badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-gray-500"></span> Beklemede';
      }

      const soc = appState.tahmini_sarj_yuzdesi || 0;
      document.getElementById('txt-soc').innerText = soc + '%';
      document.getElementById('txt-kw').innerText = (appState.power_total_kw || 0).toFixed(2) + ' kW';
      
      const circumference = 264;
      const offset = circumference - (soc / 100) * circumference;
      document.getElementById('gauge-circle').style.strokeDashoffset = offset;

      document.getElementById('txt-veri-durumu').innerText = appState.veri_durumu || '';
      document.getElementById('txt-kalan-sure').innerText = appState.kalan_sure_str || '--';
      document.getElementById('txt-varis-saati').innerText = appState.varis_saati_str || '--';
      document.getElementById('txt-oturum-kwh').innerText = (appState.oturum_enerjisi_kwh || 0).toFixed(2);
      document.getElementById('txt-kalan-kwh').innerText = (appState.kalan_enerji_kwh || 0).toFixed(2);

      const cur = appState.charge_cur_set || 10;
      document.getElementById('txt-cur-selected').innerText = cur + ' A';
      [8, 10, 12, 15].forEach(a => {
        const btn = document.getElementById('btn-cur-' + a);
        if (btn) {
          if (a === cur) {
            btn.className = 'py-2 rounded-xl text-xs font-bold bg-cyan-600 text-white shadow-md shadow-cyan-600/30 transition';
          } else {
            btn.className = 'py-2 rounded-xl text-xs font-bold glass text-gray-300 active:scale-95 transition';
          }
        }
      });

      const rngHedef = document.getElementById('rng-hedef');
      if (document.activeElement !== rngHedef) {
        rngHedef.value = appState.hedef_sarj_yuzdesi || 90;
        document.getElementById('lbl-hedef').innerText = '%' + rngHedef.value;
      }
      const rngMevcut = document.getElementById('rng-mevcut');
      if (document.activeElement !== rngMevcut) {
        rngMevcut.value = Math.round(appState.mevcut_sarj_yuzdesi || 42);
        document.getElementById('lbl-mevcut').innerText = '%' + rngMevcut.value;
      }
      document.getElementById('chk-autocut').checked = !!appState.oto_kesme_aktif;

      document.getElementById('txt-temp').innerText = (appState.temperature || '--') + ' °C';
      document.getElementById('txt-meter').innerText = (appState.forward_energy_total || '--') + ' kWh';
      document.getElementById('txt-sync').innerText = appState.last_sync || '--';
    }

    async function controlAction(act) {
      await authFetch('/api/action?cmd=' + act, { method: 'POST' });
      setTimeout(fetchState, 600);
    }

    async function setCurrent(val) {
      await authFetch('/api/set_current?val=' + val, { method: 'POST' });
      setTimeout(fetchState, 600);
    }

    let debounceHedef = null;
    function onTargetChange(val) {
      document.getElementById('lbl-hedef').innerText = '%' + val;
      clearTimeout(debounceHedef);
      debounceHedef = setTimeout(() => {
        authFetch('/api/set_target?val=' + val, { method: 'POST' });
      }, 400);
    }

    let debounceMevcut = null;
    function onMevcutChange(val) {
      document.getElementById('lbl-mevcut').innerText = '%' + val;
      clearTimeout(debounceMevcut);
      debounceMevcut = setTimeout(() => {
        authFetch('/api/set_mevcut?val=' + val, { method: 'POST' });
      }, 400);
    }

    function toggleAutoCut(checked) {
      authFetch('/api/set_autocut?val=' + (checked ? '1' : '0'), { method: 'POST' });
    }

    function refreshData() {
      fetchState();
    }

    if (authToken) {
      fetchState();
    } else {
      showLogin();
    }
    setInterval(() => {
      if (authToken) fetchState();
    }, 5000);
  </script>
</body>
</html>
"""

class AppHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self):
        url = urlparse(self.path)
        if url.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
        elif url.path == "/manifest.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.end_headers()
        elif url.path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.end_headers()
        elif url.path in ["/icon-192.png", "/icon-512.png"]:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
        elif url.path == "/icon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(APP_HTML.encode('utf-8'))
        elif url.path == "/manifest.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(MANIFEST_JSON).encode('utf-8'))
        elif url.path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(SERVICE_WORKER_JS.encode('utf-8'))
        elif url.path == "/icon-192.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(ICON_192_DATA)
        elif url.path == "/icon-512.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(ICON_512_DATA)
        elif url.path == "/icon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(ICON_SVG.encode('utf-8'))
        elif url.path in ["/health", "/ping"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"tuya_ev_charger"}')
        elif url.path == "/api/state":
            if not check_auth(self):
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error":"Unauthorized"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with LOCK:
                data = json.dumps(STATE, ensure_ascii=False)
            self.wfile.write(data.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        
        # Public login endpoint
        if url.path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ""
            user = ""
            pwd = ""
            try:
                data = json.loads(body)
                user = data.get("username", "")
                pwd = data.get("password", "")
            except Exception as ex:
                try:
                    data = parse_qs(body)
                    user = data.get("username", [""])[0]
                    pwd = data.get("password", [""])[0]
                except Exception:
                    pass
            print(f"LOGIN: received user={user!r}, match={user == AUTH_USER}, pwd_len={len(pwd)}, match_pwd={pwd == AUTH_PASS}")
            
            if user == AUTH_USER and pwd == AUTH_PASS:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", f"session_token={SESSION_TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "token": SESSION_TOKEN}).encode('utf-8'))
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Kullanıcı adı veya şifre hatalı!"}).encode('utf-8'))
            return

        elif url.path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "session_token=; Path=/; Max-Age=0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            return

        # Check authentication for protected actions
        if not check_auth(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"Unauthorized"}')
            return

        if url.path == "/api/action":
            cmd = params.get("cmd", [""])[0]
            if cmd == "start":
                print("ACTION: Starting charge via Tuya API...")
                tuya_send_commands([{"code": "switch", "value": True}])
                with LOCK:
                    STATE["sarj_aktif"] = True
                    STATE["son_veri_bekleniyor"] = False
                    STATE["oturum_enerjisi_kwh"] = 0.0
                    STATE["ilk_baslangic_yuzdesi"] = STATE["mevcut_sarj_yuzdesi"]
                    STATE["onceki_cihaz_oturum_enerjisi"] = STATE["charge_energy_once"]
            elif cmd == "stop":
                print("ACTION: Stopping charge via Tuya API...")
                tuya_send_commands([{"code": "switch", "value": False}])
                with LOCK:
                    STATE["sarj_aktif"] = False
                    STATE["son_veri_bekleniyor"] = True
            sync_tuya_status()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        elif url.path == "/api/set_current":
            val = int(params.get("val", [10])[0])
            print(f"ACTION: Setting charge current to {val}A...")
            tuya_send_commands([{"code": "charge_cur_set", "value": val}])
            with LOCK:
                STATE["charge_cur_set"] = val
            sync_tuya_status()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        elif url.path == "/api/set_target":
            val = float(params.get("val", [90])[0])
            with LOCK:
                STATE["hedef_sarj_yuzdesi"] = val
            recalculate_projections()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        elif url.path == "/api/set_mevcut":
            val = float(params.get("val", [42])[0])
            with LOCK:
                STATE["mevcut_sarj_yuzdesi"] = val
                STATE["tahmini_sarj_yuzdesi"] = val
                if STATE["sarj_aktif"]:
                    STATE["ilk_baslangic_yuzdesi"] = max(0.0, val - ((STATE["oturum_enerjisi_kwh"] / STATE["car_battery_capacity_kwh"]) * 100.0))
            recalculate_projections()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        elif url.path == "/api/set_autocut":
            val = params.get("val", ["1"])[0] == "1"
            with LOCK:
                STATE["oto_kesme_aktif"] = val
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def main():
    print("=== TUYA CLOUD AC EV CHARGER MOBILE BACKEND STARTING WITH PWA SUPPORT ===")
    sync_tuya_status()
    
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()
    
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), AppHandler)
    print(f"Mobile Application Server running on http://0.0.0.0:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
