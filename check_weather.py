import os
import requests
from datetime import datetime, timezone, timedelta
import math
import firebase_admin
from firebase_admin import credentials, firestore, messaging
import pytz

# 1. Firebase Initialisierung
if not firebase_admin._apps:
   cred_json = os.environ.get("FIREBASE_CREDENTIALS")
   if cred_json:
       import json
       cred = credentials.Certificate(json.loads(cred_json))
       firebase_admin.initialize_app(cred)
   else:
       firebase_admin.initialize_app()

db = firestore.client()
LOCAL_TZ = timezone(timedelta(hours=2))

LIVE_DIRS = [
   {"name": "N", "lat": 1, "lon": 0}, {"name": "NO", "lat": .7071, "lon": .7071}, {"name": "O", "lat": 0, "lon": 1},
   {"name": "SO", "lat": -.7071, "lon": .7071}, {"name": "S", "lat": -1, "lon": 0}, {"name": "SW", "lat": -.7071, "lon": -.7071},
   {"name": "W", "lat": 0, "lon": -1}, {"name": "NW", "lat": .7071, "lon": .7071}
]

# 2. High-Priority Push Funktion (Gegen Doze-Mode)
def send_high_priority_push(title, body, token):
   """Erzwingt sofortige Zustellung auch bei gesperrtem/schlafendem Gerät"""
   msg = messaging.Message(
       notification=messaging.Notification(title=title, body=body),
       token=token,
       android=messaging.AndroidConfig(priority='high'),
       webpush=messaging.WebpushConfig(headers={'Urgency': 'high'})
   )
   return messaging.send(msg)

# 3. Hilfsfunktionen
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

def wmo_to_text(code):
   mapping = {0: 'Klar', 1: 'Heiter', 2: 'Wolkig', 3: 'Bedeckt', 45: 'Nebel', 48: 'Nebel', 51: 'Niesel', 53: 'Niesel', 55: 'Niesel', 61: 'Regen', 63: 'Regen', 65: 'Starkregen', 71: 'Schnee', 73: 'Schnee', 75: 'Starkschnee', 95: 'Gewitter', 96: 'Gewitter/Hagel', 99: 'Schweres Gewitter'}
   return mapping.get(code, 'Unbeständig')

# 4. Multi-Location Wetter Abruf (Start, Live, Gipfel)
def fetch_current_condition(lat, lon):
   try:
       url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
       res = requests.get(url, timeout=5).json()
       c = res.get('current', {})
       t = c.get('temperature_2m', 0)
       code = c.get('weather_code', 0)
       desc = wmo_to_text(code)
       return f"{t:.1f}°C, {desc}"
   except Exception:
       return "Keine Daten"

def build_multi_location_update(tour):
   msg_parts = []
   
   start_lat = tour.get('start_lat')
   start_lon = tour.get('start_lon')
   live_lat = tour.get('lat')
   live_lon = tour.get('lon')
   peak_lat = tour.get('peak_lat')
   peak_lon = tour.get('peak_lon')

   if start_lat and start_lon:
       cond = fetch_current_condition(start_lat, start_lon)
       msg_parts.append(f"📍 Start: {cond}")
       
   if live_lat and live_lon:
       try:
           s_lat_f = float(start_lat) if start_lat else 0.0
           s_lon_f = float(start_lon) if start_lon else 0.0
           l_lat_f = float(live_lat)
           l_lon_f = float(live_lon)
           
           if not start_lat or abs(l_lat_f - s_lat_f) > 0.01 or abs(l_lon_f - s_lon_f) > 0.01:
               cond = fetch_current_condition(live_lat, live_lon)
               msg_parts.append(f"🏃 Live: {cond}")
       except (ValueError, TypeError):
           pass
           
   if peak_lat and peak_lon:
       cond = fetch_current_condition(peak_lat, peak_lon)
       msg_parts.append(f"⛰️ Gipfel: {cond}")

   return "\n".join(msg_parts)

# 5. Feinfühlige Regenerkennung (Standard = 0.02 mm)
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

# 6. Erweiterte Alpine Risiko-Engine (mit harten Klettersteig-Regeln & Wet-Rock)
def score_risk_advanced(rain, snow, gust, cape, code, prob, temp, vis=0, tour_types=None, wet_rock=False, is_peak=False):
   if tour_types is None:
       tour_types = []
       
   s = 0
   reasons = []
   
   is_exposed = any(t in ['klettersteig', 'grat', 'hochtour', 'gletscher', 'klettern'] for t in tour_types)
   is_klettersteig_or_climb = any(t in ['klettersteig', 'klettern'] for t in tour_types)
   is_hochtour_or_glacier = any(t in ['hochtour', 'gletscher'] for t in tour_types)

   # Harte Regel 1: Gewitter bei Klettersteig/Fels = Sofort 100 Punkte (Rot)
   is_thunder = code in [95, 96, 99] or (cape >= 1000 and prob >= 30)
   if is_thunder and is_klettersteig_or_climb:
       return 100, ['⚡ Gewittergefahr am Klettersteig/Fels (Absolutes Verbot / Drahtseile!)']
   elif is_thunder:
       s += 70
       reasons.append('Gewittergefahr')

   # Harte Regel 2: Starker Regen bei Klettersteig / Felsklettern / Hochtour = Sofort 90 Punkte (Rot)
   if rain >= 2.0 and (is_klettersteig_or_climb or is_hochtour_or_glacier):
       return 90, ['🌧️ Starker Regen bei Klettersteig/Hochtour (Extremer Nässe- & Absturzfaktor! Fels verliert Reibung)']

   # Nässe & Felsen-Check (Wet Rock Check)
   if wet_rock and (is_klettersteig_or_climb or 'grat' in tour_types):
       s += 45
       reasons.append('Feuchter Fels in exponierter Lage (Rutschgefahr)' if is_peak else 'Fels durch Vorniederschlag noch nass/klamm')

   # Wind & Böen
   wind_factor = 1.4 if is_exposed else 1.0
   if gust >= 80:
       s += int(55 * wind_factor)
       reasons.append('Sturmböen')
   elif gust >= 60:
       s += int(35 * wind_factor)
       reasons.append('Kräftige Böen')
   elif gust >= 45:
       s += int(20 * wind_factor)
       reasons.append('Böen')

   # Allgemeiner Regen / Schauer
   if rain >= 4:
       s += 40
       reasons.append('Starkregen')
   elif rain >= 1:
       s += 25
       reasons.append('Regen')
   elif prob >= 60 and rain < 0.2:
       s += 15
       reasons.append('Hohe Schauerneigung')

   # Schnee & Eis
   if snow >= 2:
       s += 35
       reasons.append('Schneefall')
   if temp <= 0 and (rain > 0 or snow > 0):
       s += 35
       reasons.append('Frost & Vereisungsgefahr (Glatteis)')

   # Sicht & Nebel
   if (vis > 0 and vis < 2000) or code in [45, 48]:
       s += 25
       reasons.append('Eingeschränkte Sicht (Nebel)')

   if temp >= 30:
       s += 25
       reasons.append('Extreme Hitze (>30°C)')

   return min(100, s), list(set(reasons))

# 7. Nowcast Analyse (Mit 0.02mm Schwellenwert)
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

# 8. Prognose Trend mit Tal/Gipfel & Wet-Rock Berücksichtigung
def check_forecast_trend(lat, lon, start_dt, duration, tour_types=None, peak_lat=None, peak_lon=None):
   if tour_types is None:
       tour_types = []
   try:
       date_str = (start_dt - timedelta(hours=4)).strftime('%Y-%m-%d')
       end_dt = start_dt + timedelta(hours=duration)
       end_date_str = end_dt.strftime('%Y-%m-%d')
       tour_month = start_dt.month
       is_summer = (5 <= tour_month <= 7)

       url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,precipitation,snowfall,weather_code,precipitation_probability,wind_gusts_10m,cape,visibility&start_date={date_str}&end_date={end_date_str}&timezone=auto&wind_speed_unit=kmh&models=icon_d2"
       res = requests.get(url, timeout=8).json()
       h = res.get('hourly', {})
       times = h.get('time', [])
       
       if not times:
           return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."

       ph = {}
       if peak_lat and peak_lon:
           p_url = f"https://api.open-meteo.com/v1/forecast?latitude={peak_lat}&longitude={peak_lon}&hourly=temperature_2m,precipitation,snowfall,weather_code,precipitation_probability,wind_gusts_10m,cape,visibility&start_date={date_str}&end_date={end_date_str}&timezone=auto&wind_speed_unit=kmh&models=icon_d2"
           p_res = requests.get(p_url, timeout=8).json()
           ph = p_res.get('hourly', {})

       max_risk = 0
       all_reasons = []

       for i, t_str in enumerate(times):
           t_dt = datetime.fromisoformat(t_str).replace(tzinfo=LOCAL_TZ)
           
           if start_dt <= t_dt <= end_dt:
               temp = float(h.get('temperature_2m', [0])[i] or 0)
               rain = float(h.get('precipitation', [0])[i] or 0)
               snow = float(h.get('snowfall', [0])[i] or 0)
               gust = float(h.get('wind_gusts_10m', [0])[i] or 0)
               cape = float(h.get('cape', [0])[i] or 0)
               code = int(h.get('weather_code', [0])[i] or 0)
               prob = float(h.get('precipitation_probability', [0])[i] or 0)
               vis = float(h.get('visibility', [0])[i] or 0)

               base_wet_rock = False
               for p in range(1, 4):
                   pre_idx = i - p
                   if pre_idx >= 0 and float(h.get('precipitation', [0])[pre_idx] or 0) > 0.1:
                       if not is_summer or p == 1:
                           base_wet_rock = True

               v_score, v_reasons = score_risk_advanced(rain, snow, gust, cape, code, prob, temp, vis, tour_types, base_wet_rock, False)
               slot_score = v_score
               all_reasons.extend(v_reasons)

               if ph and 'time' in ph:
                   try:
                       p_idx = ph['time'].index(t_str)
                       ptemp = float(ph.get('temperature_2m', [temp])[p_idx] or temp)
                       prain = float(ph.get('precipitation', [rain])[p_idx] or rain)
                       psnow = float(ph.get('snowfall', [snow])[p_idx] or snow)
                       pgust = float(ph.get('wind_gusts_10m', [gust])[p_idx] or gust)
                       pcape = float(ph.get('cape', [cape])[p_idx] or cape)
                       pcode = int(ph.get('weather_code', [code])[p_idx] or code)
                       pprob = float(ph.get('precipitation_probability', [prob])[p_idx] or prob)
                       pvis = float(ph.get('visibility', [vis])[p_idx] or vis)

                       p_wet_rock = False
                       for p_step in range(1, 4):
                           pre_idx = p_idx - p_step
                           if pre_idx >= 0 and float(ph.get('precipitation', [0])[pre_idx] or 0) > 0.1:
                               if not is_summer or p_step == 1:
                                   p_wet_rock = True

                       p_score, p_reasons = score_risk_advanced(prain, psnow, pgust, pcape, pcode, pprob, ptemp, pvis, tour_types, p_wet_rock, True)
                       slot_score = max(slot_score, p_score)
                       all_reasons.extend(p_reasons)
                   except ValueError:
                       pass

               if slot_score > max_risk:
                   max_risk = slot_score

       unique_reasons = list(set(all_reasons))
       if max_risk >= 60:
           reasons_txt = ", ".join(unique_reasons[:2])
           return "danger", f"⚠️ Hohes alpines Risiko prognostiziert! ({reasons_txt}). Tour nicht empfohlen."
       elif max_risk >= 30:
           reasons_txt = ", ".join(unique_reasons[:2])
           return "warning", f"⚠️ Anspruchsvolle Bedingungen prognostiziert! Achte auf: {reasons_txt}."
       elif max_risk >= 15:
           reasons_txt = ", ".join(unique_reasons[:2])
           return "moderate", f"🌤️ Trend: Leicht unbeständig ({reasons_txt})."
       
   except Exception as e:
       print(f"Fehler beim Trend-Check: {e}")
   
   return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."

# 9. Hauptschleife für den Cronjob
def check_all_tours():
   now = datetime.now(LOCAL_TZ)
   local_time_str = now.strftime("%H:%M") + " Uhr"

   subscriptions_ref = db.collection('tour_subscriptions')
   docs = list(subscriptions_ref.stream())

   for doc in docs:
       tour = doc.to_dict()
       
       token = tour.get('token')
       lat = tour.get('lat') or tour.get('start_lat')
       lon = tour.get('lon') or tour.get('start_lon')
       start_time_str = tour.get('startTime')
       duration = tour.get('duration', 6)
       tour_types = tour.get('tourTypes', [])
       peak_lat = tour.get('peak_lat')
       peak_lon = tour.get('peak_lon')
       
       last_notified_stage = tour.get('last_notified_stage', None)
       last_state = tour.get('last_weather_state', 'unknown')
       last_hourly_check = tour.get('last_hourly_check')

       if not token or not lat or not lon or not start_time_str:
           continue

       try:
           clean_time_str = start_time_str.replace('Z', '')
           if '+' in clean_time_str or clean_time_str.count('-') > 2:
               start_dt = datetime.fromisoformat(start_time_str)
               if start_dt.tzinfo is None:
                   start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
           else:
               start_dt = datetime.fromisoformat(clean_time_str).replace(tzinfo=LOCAL_TZ)

           end_dt = start_dt + timedelta(hours=duration)

           if start_dt <= now <= end_dt:
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
                       trend_status, _ = check_forecast_trend(lat, lon, start_dt, duration, tour_types, peak_lat, peak_lon)
                       
                       if trend_status == "stable":
                           alert_title = f"🌤️ Niederschlag löst sich auf [{local_time_str}]"
                           alert_body = f"Der Regen/Niesel stoppt und auch die Prognose zeigt aktuell keinen weiteren Niederschlag an."
                       else:
                           alert_title = f"🌤️ Vorübergehende Regenpause [{local_time_str}]"
                           alert_body = f"Die aktuelle Zelle ist abgezogen, das Wetter bleibt laut Prognose aber weiterhin unbeständig."
                           
                       send_hazard_alert = (last_notified_stage != "improving")
                   else:
                       current_stage = "stable"

               update_data = {
                   'last_weather_state': current_stage if front else 'stable'
               }

               if send_hazard_alert and alert_title:
                   try:
                       send_high_priority_push(alert_title, alert_body, token)
                       update_data['last_notified_stage'] = current_stage
                   except Exception as fe:
                       if "Unregistered" in str(fe) or "not found" in str(fe) or "registration-token-not-registered" in str(fe):
                           db.collection('tour_subscriptions').document(doc.id).delete()

               # --- Absoluter stündlicher Timer ---
               should_run_hourly = False
               
               if not last_hourly_check:
                   next_update_due = start_dt.astimezone(timezone.utc) + timedelta(seconds=3600)
               else:
                   last_check_dt = datetime.fromisoformat(last_hourly_check.replace('Z', '+00:00'))
                   next_update_due = last_check_dt + timedelta(seconds=3600)

               if now.astimezone(timezone.utc) >= next_update_due:
                   should_run_hourly = True

               if should_run_hourly:
                   multi_loc_status = build_multi_location_update(tour)
                   trend_status, trend_msg = check_forecast_trend(lat, lon, start_dt, duration, tour_types, peak_lat, peak_lon)

                   hourly_title = f"ℹ️ Stündliches Wetter-Update [{local_time_str}]"
                   hourly_body = f"{multi_loc_status}\n\n{trend_msg}"

                   try:
                       send_high_priority_push(hourly_title, hourly_body, token)
                       update_data['last_hourly_check'] = next_update_due.isoformat()
                   except Exception as fe:
                       if "Unregistered" in str(fe) or "not found" in str(fe) or "registration-token-not-registered" in str(fe):
                           db.collection('tour_subscriptions').document(doc.id).delete()

               db.collection('tour_subscriptions').document(doc.id).update(update_data)

       except Exception as e:
           print(f"DEBUG Fehler bei Tour {doc.id}: {e}")

if __name__ == "__main__":
   check_all_tours()