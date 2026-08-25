import os
import requests
from datetime import datetime, timezone, timedelta
import math
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

# Zeitzone für Österreich (Mitteleuropäische Zeit mit Sommerzeit-Erkennung)
LOCAL_TZ = timezone(timedelta(hours=2))

LIVE_DIRS = [
{"name": "N", "lat": 1, "lon": 0}, {"name": "NO", "lat": .7071, "lon": .7071}, {"name": "O", "lat": 0, "lon": 1},
{"name": "SO", "lat": -.7071, "lon": .7071}, {"name": "S", "lat": -1, "lon": 0}, {"name": "SW", "lat": -.7071, "lon": -.7071},
{"name": "W", "lat": 0, "lon": -1}, {"name": "NW", "lat": .7071, "lon": .7071}
]

def live_distance_point(lat, lon, dir_obj, km):
dLat = (km / 111.32) * dir_obj["lat"]
dLon = (km / (111.32 * max(.2, math.cos(lat * math.pi / 180)))) * dir_obj["lon"]
return lat + dLat, lon + dLon

def direction_name(name):
return {"N": "Norden", "NO": "Nordosten", "O": "Osten", "SO": "Südosten", "S": "Süden", "SW": "Südwesten", "W": "Westen", "NW": "Nordwesten"}.get(name, name)

def get_rain_description(amount):
   if amount < 0.2:
       return "Nieselregen"
   if amount < 2.0:
       return "leichter Regen"
   if amount < 6.0:
       return "moderater Regen"
   return "starker Regen"

def find_onset(times, values, threshold=0.02):
if not times or not values:
    return None
for i in range(len(values)):
    try:
        v = float(values[i] or 0)
        next_v = float(values[i+1] or 0) if i + 1 < len(values) else v
        if v >= threshold and (next_v >= threshold or i == len(values) - 1):
            return {"time": times[i], "amount": v}
    except Exception:
        continue
return None

def score_risk_advanced(rain, snow, gust, cape, code, prob, temp):
 """Erweiterte Risikobewertung inkl. Wind, Regen, Gewitter, Hitze und Frost."""
 s = 0
 reasons = []
 
 # Wind & Böen
 if gust >= 75:
     s += 45
     reasons.append('Starke Böen')
 elif gust >= 55:
     s += 25
     reasons.append('Kräftige Böen')
 
 # Niederschlag & Regen
 if rain >= 4:
     s += 35
     reasons.append('Starkregen')
 elif rain >= 1:
     s += 20
     reasons.append('Regen')
 elif prob >= 60:
     s += 10
     reasons.append('Hohe Schauerneigung')

 # Schnee & Schneesturm
 if snow >= 2:
     s += 45
     reasons.append('Starker Schneefall / Schneesturm')
 elif snow > 0:
     s += 25
     reasons.append('Schneefall')

 # Gewitter
 if code in [95, 96, 99] or cape >= 1000:
     s += 50
     reasons.append('Gewittergefahr')

 # Temperatur-Extreme & Frost
 if temp >= 30:
     s += 25
     reasons.append('Extreme Hitze (>30°C)')
 elif temp <= 0 and (rain > 0 or snow > 0):
     s += 40
     reasons.append('Frost & Vereisungsgefahr (Glatteis)')

 return min(100, s), reasons

def analyze_advanced_front(lat, lon):
try:
    center_url = f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nowcast-v1-15min-1km?lat_lon={lat:.5f},{lon:.5f}&parameters=rr&forecast_offset=0&output_format=geojson"
    c_res = requests.get(center_url, timeout=8).json()
    c_features = c_res.get('features', [])
    if c_features:
        c_param = c_features[0].get('properties', {}).get('parameters', {}).get('rr', {})
        c_times = c_param.get('timestamps', c_param.get('time', []))
        c_vals = c_param.get('data', [])
        center_onset = find_onset(c_times, c_vals, threshold=0.02)
        if center_onset:
            return {"stage": "arrival", "distance_km": 0, "time": center_onset["time"], "direction": None, "amount": center_onset["amount"]}

    points = []
    radii = [4, 8, 12, 18, 20]
    for d in LIVE_DIRS:
        for km in radii:
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
        for outer_km, inner_km in [(20, 18), (18, 12), (12, 4)]:
            if outer_km in items and inner_km in items:
                o_out = find_onset(items[outer_km]["times"], items[outer_km]["vals"])
                o_in = find_onset(items[inner_km]["times"], items[inner_km]["vals"])
                if o_out and o_in and o_out.get("time") and o_in.get("time"):
                    try:
                        t_out = datetime.fromisoformat(o_out["time"].replace('Z', '+00:00')).timestamp()
                        t_in = datetime.fromisoformat(o_in["time"].replace('Z', '+00:00')).timestamp()
                        dt = t_in - t_out
                        if dt > 0:
                            dist_diff = outer_km - inner_km
                            speed_kmh = dist_diff / (dt / 3600)
                            
                            if 10 <= speed_kmh <= 120:
                                eta_ms = t_in + (inner_km / speed_kmh) * 3600
                                
                                if inner_km >= 16:
                                    stage = "early_warning"
                                elif inner_km >= 8:
                                    stage = "update_mid"
                                else:
                                    stage = "update_close"

                                candidates.append({
                                    "stage": stage,
                                    "distance_km": inner_km,
                                    "time": datetime.fromtimestamp(eta_ms, timezone.utc).isoformat(),
                                    "direction": dir_name,
                                    "amount": max(o_out.get("amount", 0), o_in.get("amount", 0)),
                                    "speed": speed_kmh
                                })
                    except Exception:
                        continue

    if candidates:
        candidates.sort(key=lambda x: x["time"])
        return candidates[0]

except Exception as e:
    print(f"Erweiterter Nowcast Analyse Fehler: {e}")
return None

def check_forecast_trend(lat, lon, start_dt, duration):
 """Prüft die Open-Meteo Prognose für die verbleibenden Stunden auf Verschlechterung inklusive Temperatur & Schnee."""
 try:
     date_str = start_dt.strftime('%Y-%m-%d')
     end_dt = start_dt + timedelta(hours=duration)
     end_date_str = end_dt.strftime('%Y-%m-%d')
     
     url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,precipitation,rain,snowfall,weather_code,precipitation_probability,wind_gusts_10m,cape&start_date={date_str}&end_date={end_date_str}&timezone=auto&wind_speed_unit=kmh&models=icon_d2"
     res = requests.get(url, timeout=8).json()
     h = res.get('hourly', {})
     times = h.get('time', [])
     
     if not times:
         return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."

     max_risk = 0
     all_reasons = []

     for i, t_str in enumerate(times):
         t_dt = datetime.fromisoformat(t_str)
         if start_dt <= t_dt <= end_dt:
             temp = float(h.get('temperature_2m', [0])[i] or 0)
             rain = float(h.get('precipitation', [0])[i] or 0)
             snow = float(h.get('snowfall', [0])[i] or 0)
             gust = float(h.get('wind_gusts_10m', [0])[i] or 0)
             cape = float(h.get('cape', [0])[i] or 0)
             code = int(h.get('weather_code', [0])[i] or 0)
             prob = float(h.get('precipitation_probability', [0])[i] or 0)

             score, reasons = score_risk_advanced(rain, snow, gust, cape, code, prob, temp)
             if score > max_risk:
                 max_risk = score
             all_reasons.extend(reasons)

     unique_reasons = list(set(all_reasons))
     if max_risk >= 40:
         reasons_txt = ", ".join(unique_reasons[:2])
         return "warning", f"⚠️ Wetterverschlechterung prognostiziert! Achte auf: {reasons_txt}."
     elif max_risk >= 20:
         reasons_txt = ", ".join(unique_reasons[:2])
         return "moderate", f"🌤️ Unbeständiger Trend für die restlichen Stunden ({reasons_txt})."
     
 except Exception as e:
     print(f"Fehler beim Trend-Check: {e}")
 
 return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."

def check_all_tours():
now = datetime.now(LOCAL_TZ)
local_time_str = now.strftime("%H:%M") + " Uhr"
print(f"DEBUG: Aktuelle Lokalzeit (Österreich): {now.isoformat()}")

subscriptions_ref = db.collection('tour_subscriptions')
docs = list(subscriptions_ref.stream())
print(f"DEBUG: Anzahl gefundener Tour-Dokumente in Firestore: {len(docs)}")

for doc in docs:
    tour = doc.to_dict()
    print(f"DEBUG: Prüfe Tour ID {doc.id} | Titel: {tour.get('tourTitle')} | Start: {tour.get('startTime')}")
    
    token = tour.get('token')
    lat = tour.get('lat') or tour.get('start_lat')
    lon = tour.get('lon') or tour.get('start_lon')
    start_time_str = tour.get('startTime')
    duration = tour.get('duration', 6)
    
    last_notified_stage = tour.get('last_notified_stage', None)
    last_state = tour.get('last_weather_state', 'unknown')
    last_hourly_check = tour.get('last_hourly_check')

    if not token or not lat or not lon or not start_time_str:
        print(f"DEBUG: Tour übersprungen wegen fehlender Pflichtfelder.")
        continue

    try:
        # Sauberer Parser für lokale Startzeit mit Fallback auf UTC
        clean_time_str = start_time_str.replace('Z', '')
        if '+' in clean_time_str or clean_time_str.count('-') > 2:
            start_dt = datetime.fromisoformat(start_time_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
        else:
            start_dt = datetime.fromisoformat(clean_time_str).replace(tzinfo=LOCAL_TZ)

        end_dt = start_dt + timedelta(hours=duration)

        print(f"DEBUG: Tour-Zeitfenster Lokal: {start_dt.isoformat()} bis {end_dt.isoformat()} | Now: {now.isoformat()}")

        if start_dt <= now <= end_dt:
            print(f"DEBUG: Tour liegt im aktiven Zeitfenster! Starte Wetterprüfung...")
            front = analyze_advanced_front(lat, lon)
            
            current_stage = None
            alert_title = ""
            alert_body = ""
            send_hazard_alert = False

            if front:
                arr_dt = datetime.fromisoformat(front["time"].replace('Z', '+00:00'))
                mins_left = max(0, int((arr_dt - now.astimezone(timezone.utc)).total_seconds() / 60))
                dir_txt = f" aus {direction_name(front['direction'])}" if front.get("direction") else ""
                speed_txt = f" (ca. {int(front.get('speed', 30))} km/h)" if front.get("speed") else ""
                time_txt = f"in ca. {mins_left} Min." if mins_left > 0 else "unmittelbar"

                stage = front["stage"]
                amount = front.get("amount", 0)
                rain_desc = get_rain_description(amount)

                if stage == "arrival" and last_notified_stage != "arrival":
                    current_stage = "arrival"
                    if amount < 0.2:
                        alert_title = f"🌧️ Es fängt an zu nieseln [{local_time_str}]"
                        alert_body = f"Nieselregen hat den Standort direkt erreicht."
                    elif amount < 2.0:
                        alert_title = f"🌧️ Es fängt an leicht zu regnen [{local_time_str}]"
                        alert_body = f"Leichter Regen hat den Standort direkt erreicht."
                    else:
                        alert_title = f"🌧️ Es fängt an zu regnen ({rain_desc}) [{local_time_str}]"
                        alert_body = f"Niederschlag ({rain_desc}) ist jetzt direkt aktiv."
                    send_hazard_alert = True
                elif stage in ["early_warning", "update_mid", "update_close"] and last_notified_stage != stage:
                    current_stage = stage
                    incoming_prefix = "Es kommt Nieselregen" if amount < 0.2 else ("Es fängt gleich an zu regnen" if amount < 2.0 else "Regenfront im Anmarsch")
                    
                    if stage == "early_warning":
                        alert_title = f"⚠️ Niederschlag im Anmarsch [{local_time_str}]"
                        alert_body = f"{incoming_prefix}{dir_txt} – Ankunft {time_txt}{speed_txt}."
                    elif stage == "update_mid":
                        alert_title = f"⚠️ Regen rückt näher [{local_time_str}]"
                        alert_body = f"Entfernung ca. 12 km, {incoming_prefix.lower()}{dir_txt} (ETA: {time_txt})."
                    elif stage == "update_close":
                        alert_title = f"⚡ Letzte Warnung (4 km) [{local_time_str}]"
                        alert_body = f"{incoming_prefix} unmittelbar vor deiner Position! Eintreffen {time_txt}."
                    send_hazard_alert = True
            else:
                if last_state in ['danger', 'worsening', 'early_warning', 'update_mid', 'update_close', 'arrival']:
                    current_stage = "improving"
                    alert_title = f"🌤️ Entwarnung [{local_time_str}]"
                    alert_body = f"Wetterfront ist abgezogen. Die Bedingungen stabilisieren sich wieder."
                    send_hazard_alert = (last_notified_stage != "improving")
                else:
                    current_stage = "stable"

            update_data = {
                'last_weather_state': current_stage if front else 'stable'
            }

            if send_hazard_alert and alert_title:
                try:
                    message = messaging.Message(
                        notification=messaging.Notification(title=alert_title, body=alert_body),
                        token=token
                    )
                    messaging.send(message)
                    update_data['last_notified_stage'] = current_stage
                    print(f"DEBUG: Gefahren-Push erfolgreich gesendet!")
                except Exception as fe:
                    print(f"DEBUG: FCM Sende-Fehler: {fe}")
                    if "Unregistered" in str(fe) or "not found" in str(fe) or "registration-token-not-registered" in str(fe):
                        db.collection('tour_subscriptions').document(doc.id).delete()
                        print(f"DEBUG: Ungültiges Token {doc.id} aus Firestore gelöscht.")

            should_run_hourly = False
            if not last_hourly_check:
                update_data['last_hourly_check'] = now.astimezone(timezone.utc).isoformat()
            else:
                last_check_dt = datetime.fromisoformat(last_hourly_check.replace('Z', '+00:00'))
                if (now.astimezone(timezone.utc) - last_check_dt).total_seconds() >= 3600:
                    should_run_hourly = True

            if should_run_hourly:
                trend_status, trend_msg = check_forecast_trend(lat, lon, start_dt, duration)

                hourly_title = f"ℹ️ Stündliches Wetter-Update [{local_time_str}]"
                hourly_body = trend_msg

                try:
                    hourly_message = messaging.Message(
                        notification=messaging.Notification(title=hourly_title, body=hourly_body),
                        token=token
                    )
                    messaging.send(hourly_message)
                    update_data['last_hourly_check'] = now.astimezone(timezone.utc).isoformat()
                    print(f"DEBUG: Stündliches Update mit Trend ({trend_status}) erfolgreich gesendet!")
                except Exception as fe:
                    print(f"DEBUG: FCM Stündlicher Sende-Fehler: {fe}")
                    if "Unregistered" in str(fe) or "not found" in str(fe) or "registration-token-not-registered" in str(fe):
                        db.collection('tour_subscriptions').document(doc.id).delete()
                        print(f"DEBUG: Ungültiges Token {doc.id} aus Firestore gelöscht.")

            db.collection('tour_subscriptions').document(doc.id).update(update_data)
        else:
            print(f"DEBUG: Tour liegt NICHT im aktiven Zeitfenster (Start: {start_dt}, Ende: {end_dt}, Now: {now}).")

    except Exception as e:
        print(f"DEBUG Fehler bei Tour {doc.id}: {e}")

if __name__ == "__main__":
check_all_tours()