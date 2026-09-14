import os
import math
import time as time_lib
from datetime import datetime, time, timedelta, timezone
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# Force Indian Standard Time (IST = UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# =====================================================================
# 1. API SETTINGS
# =====================================================================
API_URL = "https://supabase.apps2db.uctechlabs.com/functions/v1/public-tracking"
PAYLOAD = {
    "token": "3ee9e4e46671dbc7286b95cf2dc8c9e287971fe41041d545"
}

# ntfy Channels
ALERT_TOPIC = "bhavnagar-bus-you-99"    # Where you receive bus alerts
COMMAND_TOPIC = "bhavnagar-bus-cmd-99"  # Where you send 'home' or 'work'

# =====================================================================
# 2. SCHEDULE & ROUTE SETTINGS
# =====================================================================
# Default automated commute windows (IST 24-hour time)
MORNING_WINDOW = (time(8, 30), time(10, 30))  # Home -> Work
EVENING_WINDOW = (time(17, 30), time(19, 30)) # Work -> Home

# --- LEG 1: MORNING (HOME -> WORK) ---
MORNING_BOARDING = {"name": "Home Stop", "lat": 21.739635, "lon": 72.143801}
MORNING_GATES = [
    {"name": "RTO Circle",    "lat": 21.763201, "lon": 72.123069},
    {"name": "Jewels Circle", "lat": 21.756703, "lon": 72.125713},
    {"name": "Himalaya Mall", "lat": 21.749059, "lon": 72.135670}
]
MORNING_TRIGGER = {"name": "Himalaya Mall", "lat": 21.749059, "lon": 72.135670}

# --- LEG 2: EVENING (WORK -> HOME) ---
EVENING_BOARDING = {"name": "Work Stop", "lat": 21.742990, "lon": 72.149998}
EVENING_GATES = [
    {"name": "Shivaji Circle",         "lat": 21.754671, "lon": 72.162436},
    {"name": "Nandkuvar Ba College",  "lat": 21.750419, "lon": 72.158904},
    {"name": "GMDC",                  "lat": 21.747920, "lon": 72.157140}
]
EVENING_TRIGGER = {"name": "GMDC", "lat": 21.747920, "lon": 72.157140}

# Expanded from 0.20 to 0.50 km (500m) to catch fast-moving buses between GPS updates
GATE_RADIUS_KM = 0.50

# =====================================================================
# 3. HELPER FUNCTIONS
# =====================================================================
def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2)**2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def send_alert(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{ALERT_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "bus,rotating_light"},
            timeout=5
        )
    except Exception as e:
        print(f"[!] Push error: {e}")

# =====================================================================
# 4. ON-DEMAND COMMAND LISTENER (ntfy integration)
# =====================================================================
manual_override_mode = None
manual_override_expiry = None

def command_listener_loop():
    global manual_override_mode, manual_override_expiry
    print(f"[*] Subscribed to command channel: {COMMAND_TOPIC}")

    while True:
        try:
            resp = requests.get(f"https://ntfy.sh/{COMMAND_TOPIC}/raw", stream=True, timeout=60)
            for line in resp.iter_lines():
                if line:
                    cmd = line.decode("utf-8").strip().lower()
                    now = datetime.now(IST)
                    print(f"[*] Command registered from phone: {cmd}")

                    if "home" in cmd:
                        manual_override_mode = "home"
                        manual_override_expiry = now + timedelta(minutes=45)
                        send_alert("Tracker Activated", "Work -> Home tracking active for next 45 mins.")
                    elif "work" in cmd or "go" in cmd:
                        manual_override_mode = "work"
                        manual_override_expiry = now + timedelta(minutes=45)
                        send_alert("Tracker Activated", "Home -> Work tracking active for next 45 mins.")
        except Exception:
            time_lib.sleep(5)

# =====================================================================
# 5. TRACKING ENGINE
# =====================================================================
morning_confirmed = {}
morning_alerted = {}
evening_confirmed = {}
evening_alerted = {}

def process_leg(buses, gates, trigger, boarding, confirmed_dict, alerted_dict, leg_label, now):
    for bus in buses:
        bus_id = bus.get("name")
        try:
            bus_lat = float(bus["latitude"])
            bus_lon = float(bus["longitude"])
        except (ValueError, KeyError, TypeError):
            continue

        # 1. Upstream Gate Confirmation
        for gate in gates:
            dist_to_gate = haversine(bus_lat, bus_lon, gate["lat"], gate["lon"])
            if dist_to_gate <= GATE_RADIUS_KM:
                if bus_id not in confirmed_dict:
                    print(f"[*] Bus {bus_id} confirmed at gate: {gate['name']} ({dist_to_gate*1000:.0f}m)")
                confirmed_dict[bus_id] = now
                break

        # 2. Approaching Trigger Check
        if bus_id in confirmed_dict:
            dist_to_trigger = haversine(bus_lat, bus_lon, trigger["lat"], trigger["lon"])
            dist_to_stop = haversine(bus_lat, bus_lon, boarding["lat"], boarding["lon"])

            if dist_to_trigger <= GATE_RADIUS_KM and (bus_id not in alerted_dict):
                print(f"[!] ALERT: Bus {bus_id} passed trigger {trigger['name']}!")
                send_alert(
                    f"Bus Approaching ({leg_label})",
                    f"Bus {bus_id} just passed {trigger['name']}! "
                    f"Distance to your stop: {dist_to_stop:.2f} km. Head out now."
                )
                alerted_dict[bus_id] = now

            # Reset after departure (> 1.2 km past stop and 20 mins elapsed)
            elif dist_to_stop > 1.2 and (bus_id in alerted_dict):
                if (now - alerted_dict[bus_id]).total_seconds() > 1200:
                    alerted_dict.pop(bus_id, None)
                    confirmed_dict.pop(bus_id, None)

def tracker_loop():
    global manual_override_mode, manual_override_expiry
    print("[*] Two-way Bhavnagar bus engine online...")

    while True:
        try:
            now = datetime.now(IST)
            current_time = now.time()

            active_leg = None

            # Check manual trigger first
            if manual_override_mode and manual_override_expiry:
                if now <= manual_override_expiry:
                    active_leg = "work" if manual_override_mode == "work" else "home"
                else:
                    print("[*] Manual override window expired. Returning to schedule.")
                    manual_override_mode = None
                    manual_override_expiry = None

            # Fall back to scheduled commute windows
            if not active_leg:
                if MORNING_WINDOW[0] <= current_time <= MORNING_WINDOW[1]:
                    active_leg = "work"
                elif EVENING_WINDOW[0] <= current_time <= EVENING_WINDOW[1]:
                    active_leg = "home"

            # Sleep quietly outside active commute windows
            if not active_leg:
                time_lib.sleep(30)
                continue

            resp = requests.post(API_URL, json=PAYLOAD, timeout=10)
            if resp.status_code == 200:
                buses = resp.json().get("positions", [])
                print(f"[*] Loop active ({active_leg.upper()}) | Active Bhavnagar e-buses: {len(buses)}")

                if active_leg == "work":
                    process_leg(buses, MORNING_GATES, MORNING_TRIGGER, MORNING_BOARDING,
                                morning_confirmed, morning_alerted, "Going to Work", now)
                elif active_leg == "home":
                    process_leg(buses, EVENING_GATES, EVENING_TRIGGER, EVENING_BOARDING,
                                evening_confirmed, evening_alerted, "Heading Home", now)
            else:
                print(f"[!] Supabase API error: {resp.status_code} - Response: {resp.text}")

        except Exception as err:
            print(f"[!] Polling error: {err}")

        time_lib.sleep(15)

# =====================================================================
# 6. RENDER KEEP-ALIVE SERVER
# =====================================================================
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bus Tracker active and running.")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    server.serve_forever()

# =====================================================================
# 7. SELF-PING KEEP-ALIVE (Keeps Render instance awake 24/7)
# =====================================================================
def self_ping():
    while True:
        time_lib.sleep(600)
        try:
            requests.get("https://busnotifications.onrender.com", timeout=10)
        except Exception:
            pass

if __name__ == "__main__":
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=command_listener_loop, daemon=True).start()
    threading.Thread(target=tracker_loop, daemon=True).start()
    run_server()
