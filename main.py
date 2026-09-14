import os
import math
import time as time_lib
from datetime import datetime, time, timedelta, timezone
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# Indian Standard Time (IST = UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# =====================================================================
# 1. API SETTINGS
# =====================================================================
API_URL = "https://supabase.apps2db.uctechlabs.com/functions/v1/public-tracking"
PAYLOAD = {
    "token": "3ee9e4e46671dbc7286b95cf2dc8c9e287971fe41041d545"
}

ALERT_TOPIC = "bhavnagar-bus-you-99"
COMMAND_TOPIC = "bhavnagar-bus-cmd-99"

# =====================================================================
# 2. ROUTE CHECKPOINTS & RADIUS
# =====================================================================
MORNING_WINDOW = (time(8, 30), time(10, 30))  # Home -> Work
EVENING_WINDOW = (time(17, 30), time(19, 30)) # Work -> Home

# 350 meters detection zone
GATE_RADIUS_KM = 0.35

# Morning Sequence: RTO Circle (1) -> Jewels Circle (2) -> Himalaya Mall (Trigger)
MORNING_BOARDING = {"name": "Home Stop", "lat": 21.739635, "lon": 72.143801}
MORNING_CHECKPOINTS = [
    {"name": "RTO Circle",     "lat": 21.763201, "lon": 72.123069},
    {"name": "Jewels Circle",  "lat": 21.756703, "lon": 72.125713},
    {"name": "Himalaya Mall",  "lat": 21.749059, "lon": 72.135670}
]

# Evening Sequence: Shivaji Circle (1) -> Nandkuvar Ba (2) -> GMDC (Trigger)
EVENING_BOARDING = {"name": "Work Stop", "lat": 21.742990, "lon": 72.149998}
EVENING_CHECKPOINTS = [
    {"name": "Shivaji Circle",        "lat": 21.754671, "lon": 72.162436},
    {"name": "Nandkuvar Ba College", "lat": 21.750419, "lon": 72.158904},
    {"name": "GMDC",                 "lat": 21.747920, "lon": 72.157140}
]

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
# 4. COMMAND LISTENER (ntfy app integration)
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
# 5. SEQUENTIAL TRACKING ENGINE
# =====================================================================
# Progress states:
# Stage 0: Not seen
# Stage 1: Hit Gate 1 (e.g. Shivaji Circle)
# Stage 2: Hit Gate 2 (e.g. Nandkuvar Ba College)
# Trigger fires only when a Stage 2 bus reaches Gate 3 (GMDC)
morning_progress = {}
morning_alerted = {}

evening_progress = {}
evening_alerted = {}

def process_leg_sequential(buses, checkpoints, boarding, progress_dict, alerted_dict, leg_label, now):
    gate_1 = checkpoints[0]
    gate_2 = checkpoints[1]
    trigger = checkpoints[2]

    # Remove inactive records older than 20 minutes
    for bid in list(progress_dict.keys()):
        if (now - progress_dict[bid]["updated"]).total_seconds() > 1200:
            progress_dict.pop(bid, None)

    for bus in buses:
        bus_id = bus.get("name")
        try:
            bus_lat = float(bus["latitude"])
            bus_lon = float(bus["longitude"])
        except (ValueError, KeyError, TypeError):
            continue

        stage = progress_dict.get(bus_id, {}).get("stage", 0)

        # Step 1: Must hit Gate 1 first
        if stage == 0:
            dist_g1 = haversine(bus_lat, bus_lon, gate_1["lat"], gate_1["lon"])
            if dist_g1 <= GATE_RADIUS_KM:
                progress_dict[bus_id] = {"stage": 1, "updated": now}
                print(f"[*] Step 1/3 Confirmed: Bus {bus_id} at {gate_1['name']} ({dist_g1*1000:.0f}m)")

        # Step 2: Must advance to Gate 2
        elif stage == 1:
            dist_g2 = haversine(bus_lat, bus_lon, gate_2["lat"], gate_2["lon"])
            if dist_g2 <= GATE_RADIUS_KM:
                progress_dict[bus_id] = {"stage": 2, "updated": now}
                print(f"[*] Step 2/3 Confirmed: Bus {bus_id} at {gate_2['name']} ({dist_g2*1000:.0f}m)")

        # Step 3: Trigger Alert (Only buses that passed BOTH Gate 1 and Gate 2)
        elif stage == 2:
            dist_trigger = haversine(bus_lat, bus_lon, trigger["lat"], trigger["lon"])
            dist_to_stop = haversine(bus_lat, bus_lon, boarding["lat"], boarding["lon"])

            if dist_trigger <= GATE_RADIUS_KM and (bus_id not in alerted_dict):
                print(f"[!] FULL ROUTE CONFIRMED: Bus {bus_id} arrived at {trigger['name']}!")
                send_alert(
                    f"Bus Approaching ({leg_label})",
                    f"Bus {bus_id} followed the complete corridor and passed {trigger['name']}! "
                    f"Distance to your stop: {dist_to_stop:.2f} km. Head out now."
                )
                alerted_dict[bus_id] = now

            # Reset after bus leaves stop area (> 1.2 km away and 20 mins elapsed)
            elif dist_to_stop > 1.2 and (bus_id in alerted_dict):
                if (now - alerted_dict[bus_id]).total_seconds() > 1200:
                    alerted_dict.pop(bus_id, None)
                    progress_dict.pop(bus_id, None)

def tracker_loop():
    global manual_override_mode, manual_override_expiry
    print("[*] Sequential Bhavnagar bus engine online...")

    while True:
        try:
            now = datetime.now(IST)
            current_time = now.time()
            active_leg = None

            if manual_override_mode and manual_override_expiry:
                if now <= manual_override_expiry:
                    active_leg = "work" if manual_override_mode == "work" else "home"
                else:
                    print("[*] Manual override expired. Returning to schedule.")
                    manual_override_mode = None
                    manual_override_expiry = None

            if not active_leg:
                if MORNING_WINDOW[0] <= current_time <= MORNING_WINDOW[1]:
                    active_leg = "work"
                elif EVENING_WINDOW[0] <= current_time <= EVENING_WINDOW[1]:
                    active_leg = "home"

            if not active_leg:
                time_lib.sleep(30)
                continue

            resp = requests.post(API_URL, json=PAYLOAD, timeout=10)
            if resp.status_code == 200:
                buses = resp.json().get("positions", [])
                print(f"[*] Loop active ({active_leg.upper()}) | Active Bhavnagar e-buses: {len(buses)}")

                if active_leg == "work":
                    process_leg_sequential(buses, MORNING_CHECKPOINTS, MORNING_BOARDING,
                                           morning_progress, morning_alerted, "Going to Work", now)
                elif active_leg == "home":
                    process_leg_sequential(buses, EVENING_CHECKPOINTS, EVENING_BOARDING,
                                           evening_progress, evening_alerted, "Heading Home", now)
            else:
                print(f"[!] API error: {resp.status_code} - Response: {resp.text}")

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
# 7. SELF-PING KEEP-ALIVE
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
