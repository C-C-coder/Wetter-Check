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

def find_onset(times, values, threshold=0.08):
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

def analyze_advanced_front(lat, lon):
  try:
      center_url = f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nowcast-v1-15min-1km?lat_lon={lat:.5f},{lon:.5f}&parameters=rr&forecast_offset=0&output_format=geojson"
      c_res = requests.get(center_url, timeout=8).json()
      c_features = c_res.get('features', [])
      if c_features:
          c_param = c_features[0].get('properties', {}).get('parameters', {}).get('rr', {})
          c_times = c_param.get('timestamps', c_param.get('time', []))
          c_vals = c_param.get('data', [])
          center_onset = find_onset(c_times, c_vals, threshold=0.08)
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
                  
                  if stage == "early_warning" and last_notified_stage != "early_warning":
                      current_stage = "early_warning"
                      alert_title = f"⚠️ Schlechtwetter im Anmarsch [{local_time_str}]"
                      alert_body = f"Zieht{dir_txt} auf – Ankunft {time_txt}{speed_txt}."
                      send_hazard_alert = True
                  elif stage == "update_mid" and last_notified_stage not in ["update_mid", "update_close", "arrival"]:
                      current_stage = "update_mid"
                      alert_title = f"⚠️ Unwetter rückt näher [{local_time_str}]"
                      alert_body = f"Entfernung ca. 12 km, zieht{dir_txt} auf (ETA: {time_txt})."
                      send_hazard_alert = True
                  elif stage == "update_close" and last_notified_stage not in ["update_close", "arrival"]:
                      current_stage = "update_close"
                      alert_title = f"⚡ Letzte Warnung (4 km) [{local_time_str}]"
                      alert_body = f"Unwetterfront unmittelbar vor deiner Position! Eintreffen {time_txt}."
                      send_hazard_alert = True
                  elif stage == "arrival" and last_notified_stage != "arrival":
                      current_stage = "arrival"
                      alert_title = f"🚨 Front hat Standort erreicht! [{local_time_str}]"
                      alert_body = f"Niederschlag oder Gewitter ist jetzt direkt aktiv. Suche Schutz!"
                      send_hazard_alert = True
              else:
                  if last_state in ['danger', 'worsening', 'early_warning', 'update_mid', 'update_close']:
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
                  message = messaging.Message(
                      notification=messaging.Notification(title=alert_title, body=alert_body),
                      token=token
                  )
                  messaging.send(message)
                  update_data['last_notified_stage'] = current_stage
                  print(f"DEBUG: Gefahren-Push erfolgreich gesendet!")

              should_run_hourly = False
              if not last_hourly_check:
                  # NEU: Beim ersten Durchlauf direkt auf jetzt setzen, damit das erste Update erst nach 60 Min kommt
                  update_data['last_hourly_check'] = now.astimezone(timezone.utc).isoformat()
              else:
                  last_check_dt = datetime.fromisoformat(last_hourly_check.replace('Z', '+00:00'))
                  if (now.astimezone(timezone.utc) - last_check_dt).total_seconds() >= 3600:
                      should_run_hourly = True

              if should_run_hourly:
                  hourly_title = f"ℹ️ Stündliches Wetter-Update [{local_time_str}]"
                  hourly_body = "Bedingungen für deine Tour sind stabil und sicher."

                  hourly_message = messaging.Message(
                      notification=messaging.Notification(title=hourly_title, body=hourly_body),
                      token=token
                  )
                  messaging.send(hourly_message)
                  update_data['last_hourly_check'] = now.astimezone(timezone.utc).isoformat()
                  print(f"DEBUG: Stündliches Update erfolgreich gesendet!")

              db.collection('tour_subscriptions').document(doc.id).update(update_data)
          else:
              print(f"DEBUG: Tour liegt NICHT im aktiven Zeitfenster (Start: {start_dt}, Ende: {end_dt}, Now: {now}).")

      except Exception as e:
          print(f"DEBUG Fehler bei Tour {doc.id}: {e}")

if __name__ == "__main__":
  check_all_tours()