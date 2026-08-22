import os
import requests
from datetime import datetime, timezone, timedelta
import firebase_admin
from firebase_admin import credentials, firestore, messaging

if not firebase_admin._apps:
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        import json
        cred = credentials.Certificate(json.loads(cred_json))
        firebase_admin.initialize_app(cred)
    else:
        firebase_admin.initialize_app()

db = firestore.client()

LIVE_DIRS = [
    {"name": "N", "lat": 1, "lon": 0}, {"name": "NO", "lat": .7071, "lon": .7071}, {"name": "O", "lat": 0, "lon": 1},
    {"name": "SO", "lat": -.7071, "lon": .7071}, {"name": "S", "lat": -1, "lon": 0}, {"name": "SW", "lat": -.7071, "lon": -.7071},
    {"name": "W", "lat": 0, "lon": -1}, {"name": "NW", "lat": .7071, "lon": .7071}
]

def live_distance_point(lat, lon, dir_obj, km=8):
    dLat = (km / 111.32) * dir_obj["lat"]
    dLon = (km / (111.32 * max(.2, math_cos(lat)))) * dir_obj["lon"]
    return lat + dLat, lon + dLon

def math_cos(lat):
    import math
    return math.cos(lat * math.pi / 180)

def direction_name(name):
    return {"N": "Norden", "NO": "Nordosten", "O": "Osten", "SO": "Südosten", "S": "Süden", "SW": "Südwesten", "W": "Westen", "NW": "Nordwesten"}.get(name, name)

def find_onset(times, values, threshold=0.08):
    for i in range(len(values)):
        v = float(values[i] or 0)
        next_v = float(values[i+1] or 0) if i + 1 < len(values) else v
        if v >= threshold and (next_v >= threshold or i == len(values) - 1):
            return {"time": times[i], "amount": v}
    return None

def analyze_nowcast_arrival(lat, lon):
    try:
        center_url = f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nowcast-v1-15min-1km?lat_lon={lat:.5f},{lon:.5f}&parameters=rr&forecast_offset=0&output_format=geojson"
        c_res = requests.get(center_url, timeout=8).json()
        c_param = c_res.get('features', [{}])[0].get('properties', {}).get('parameters', {}).get('rr', {})
        c_times = c_param.get('timestamps', c_param.get('time', []))
        c_vals = c_param.get('data', [])
        
        center_onset = find_onset(c_times, c_vals, threshold=0.08)
        if center_onset:
            return {"kind": "now", "time": center_onset["time"], "direction": None, "amount": center_onset["amount"]}

        points = []
        for d in LIVE_DIRS:
            for km in [4, 8]:
                pLat, pLon = live_distance_point(lat, lon, d, km)
                points.append({"dir": d["name"], "km": km, "lat": pLat, "lon": pLon})

        pts_query = "&".join([f"lat_lon={p['lat']:.5f},{p['lon']:.5f}" for p in points])
        pts_url = f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nowcast-v1-15min-1km?{pts_query}&parameters=rr&forecast_offset=0&output_format=geojson"
        
        p_res = requests.get(pts_url, timeout=10).json()
        features = p_res.get('features', [])

        grouped = {}
        for idx, p in enumerate(points):
            feat = features[idx] if idx < len(features) else {}
            param = feat.get('properties', {}).get('parameters', {}).get('rr', {})
            times = param.get('timestamps', param.get('time', []))
            vals = param.get('data', [])
            grouped.setdefault(p["dir"], {})[p["km"]] = {"times": times, "vals": vals}

        candidates = []
        for dir_name, items in grouped.items():
            if 4 in items and 8 in items:
                o4 = find_onset(items[4]["times"], items[4]["vals"])
                o8 = find_onset(items[8]["times"], items[8]["vals"])
                if o4 and o8 and o4["time"] and o8["time"]:
                    t4 = datetime.fromisoformat(o4["time"].replace('Z', '+00:00')).timestamp()
                    t8 = datetime.fromisoformat(o8["time"].replace('Z', '+00:00')).timestamp()
                    dt = t4 - t8
                    if 15 * 60 <= dt <= 180 * 60:
                        speed_kmh = 4 / (dt / 3600)
                        eta_ms = t4 - (4 / speed_kmh) * 3600
                        candidates.append({
                            "kind": "incoming",
                            "time": datetime.fromtimestamp(eta_ms, timezone.utc).isoformat(),
                            "direction": dir_name,
                            "amount": max(o4["amount"], o8["amount"])
                        })

        if candidates:
            candidates.sort(key=lambda x: x["time"])
            return candidates[0]

    except Exception as e:
        print(f"Nowcast Analyse Fehler: {e}")
    return None

def check_all_tours():
    now = datetime.now(timezone.utc)
    subscriptions_ref = db.collection('tour_subscriptions')
    docs = subscriptions_ref.stream()

    for doc in docs:
        tour = doc.to_dict()
        token = tour.get('token')
        lat = tour.get('lat')
        lon = tour.get('lon')
        start_time_str = tour.get('startTime')
        duration = tour.get('duration', 6)
        
        already_alerted = tour.get('already_alerted', False)
        last_state = tour.get('last_weather_state', 'unknown')

        if not token or not lat or not lon or not start_time_str:
            complete

        try:
            start_dt = datetime.fromisoformat(start_time_str).replace(tzinfo=timezone.utc)
            end_dt = datetime.fromtimestamp(start_dt.timestamp() + duration * 3600, tzinfo=timezone.utc)

            # Nur während der aktiven Tour prüfen
            if start_dt <= now <= end_dt:
                arrival = analyze_nowcast_arrival(lat, lon)
                
                current_state = 'stable'
                title = ""
                body = ""

                if arrival:
                    arr_dt = datetime.fromisoformat(arrival["time"].replace('Z', '+00:00'))
                    mins_left = max(0, int((arr_dt - now).total_seconds() / 60))
                    dir_txt = f" aus Richtung {direction_name(arrival['direction']) }" if arrival.get("direction") else " direkt über dem Standort"
                    time_txt = f"in ca. {mins_left} Minuten" if mins_left > 0 else "jetzt"

                                        if True: # Test-Modus: Löst sofort aus
                        current_state = 'danger'
                        title = "⚡ Test-Alarm: Regen im Anmarsch!"
                                            
                    else:
                        current_state = 'worsening'
                        title = "⚠️ Leichter Regen im Anmarsch"
                        body = f"Niederschlag zieht{dir_txt} auf und wird {time_txt} erwartet."
                else:
                    # Wenn es wieder trocken ist, setzen wir den Alarm-Status zurück, 
                    # damit bei einer NEUEN Front später am Tag wieder gewarnt wird.
                    if last_state in ['danger', 'worsening']:
                        current_state = 'improving'
                        title = "🌤️ Wetterbesserung"
                        body = "Die Niederschlagsfront zieht ab, die Bedingungen stabilisieren sich."
                        # Bei Besserung erlauben wir einen neuen Hinweis nach Abzug der Front
                        already_alerted = False 
                    else:
                        current_state = 'stable'
                        already_alerted = False

                # DIE ENTSCHEIDENDE REGEL: 
                # Wenn für diese Front bereits einmal gewarnt wurde (already_alerted = True), 
                # wird KEINE erneute Nachricht gesendet, bis das Wetter wieder gut war.
                send_alert = False
                if current_state in ['danger', 'worsening'] and not already_alerted:
                    send_alert = True
                elif current_state == 'improving' and not already_alerted:
                    send_alert = True

                update_data = {
                    'last_weather_state': current_state,
                    'already_alerted': already_alerted
                }

                if send_alert and title:
                    message = messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        token=token
                    )
                    messaging.send(message)
                    update_data['already_alerted'] = True # Ab jetzt stumm, bis Front vorbei ist
                    print(f"Einmaliger Push gesendet ({current_state}) an Token: {token[:10]}...")

                db.collection('tour_subscriptions').document(doc.id).update(update_data)

        except Exception as e:
            print(f"Fehler bei Tour {doc.id}: {e}")

if __name__ == "__main__":
    check_all_tours()
        
