import os
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta

import firebase_admin
from firebase_admin import credentials, firestore, messaging
import pytz

# ==============================================================================
#  ALPINE WETTER-ENGINE  (Cronjob-Backend)
#  Überarbeitete Fassung - Liste aller Korrekturen in BEFUNDE_und_FIXES.md
# ==============================================================================

# 1. Firebase Initialisierung
if not firebase_admin._apps:
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
        firebase_admin.initialize_app(cred)
    else:
        firebase_admin.initialize_app()

db = firestore.client()
LOCAL_TZ = pytz.timezone('Europe/Vienna')  # berücksichtigt automatisch CET/CEST-Wechsel

# Eine wiederverwendete Session spart TLS-Handshakes und erlaubt zentrale Retries.
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'BergtourenWetterAmpel/2.0 (alpine tour safety)'})

GEOSPHERE_BASE = "https://dataset.api.hub.geosphere.at/v1"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

LIVE_DIRS = [
    {"name": "N", "lat": 1, "lon": 0}, {"name": "NO", "lat": .7071, "lon": .7071},
    {"name": "O", "lat": 0, "lon": 1}, {"name": "SO", "lat": -.7071, "lon": .7071},
    {"name": "S", "lat": -1, "lon": 0}, {"name": "SW", "lat": -.7071, "lon": -.7071},
    {"name": "W", "lat": 0, "lon": -1}, {"name": "NW", "lat": .7071, "lon": -.7071}
]


def http_json(url, timeout=10, retries=2):
    """Ein Request mit kurzem Retry. Gibt None statt einer Exception zurück, damit ein
    einzelner API-Aussetzer nicht die komplette Tour-Schleife abbricht."""
    for attempt in range(retries + 1):
        try:
            res = SESSION.get(url, timeout=timeout)
            if res.status_code >= 500 and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            res.raise_for_status()
            return res.json()
        except Exception as e:
            if attempt >= retries:
                print(f"HTTP-Fehler ({url.split('?')[0]}): {e}")
                return None
            time.sleep(1.2 * (attempt + 1))
    return None


# 2. High-Priority Push (gegen Doze-Mode & für alpine Sichtbarkeit)
def send_high_priority_push(title, body, token):
    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        token=token,
        android=messaging.AndroidConfig(priority='high', ttl=timedelta(hours=12)),
        webpush=messaging.WebpushConfig(
            headers={'Urgency': 'high'},
            notification=messaging.WebpushNotification(
                require_interaction=True,
                vibrate=[200, 100, 200, 100, 400]
            )
        )
    )
    return messaging.send(msg)


def is_dead_token(err):
    txt = str(err).lower()
    return any(k in txt for k in ["unregistered", "not found", "registration-token-not-registered",
                                  "invalid-registration-token"])


# 3. Hilfsfunktionen
def live_distance_point(lat, lon, dir_obj, km):
    d_lat = (km / 111.32) * dir_obj["lat"]
    d_lon = (km / (111.32 * max(.2, math.cos(lat * math.pi / 180)))) * dir_obj["lon"]
    return lat + d_lat, lon + d_lon


def direction_name(name):
    return {"N": "Norden", "NO": "Nordosten", "O": "Osten", "SO": "Südosten", "S": "Süden",
            "SW": "Südwesten", "W": "Westen", "NW": "Nordwesten"}.get(name, name)


def get_rain_description(amount):
    if amount < 0.2:
        return "Nieselregen"
    if amount < 2.0:
        return "leichter Regen"
    if amount < 6.0:
        return "moderater Regen"
    return "starker Regen"


def wmo_to_text(code):
    mapping = {0: 'Klar', 1: 'Heiter', 2: 'Wolkig', 3: 'Bedeckt', 45: 'Nebel', 48: 'Nebel',
               51: 'Niesel', 53: 'Niesel', 55: 'Niesel',
               56: 'Gefrierender Niesel', 57: 'Gefrierender Niesel',
               61: 'Regen', 63: 'Regen', 65: 'Starkregen',
               66: 'Gefrierender Regen', 67: 'Gefrierender Regen',
               71: 'Schnee', 73: 'Schnee', 75: 'Starkschnee', 77: 'Schneegriesel',
               80: 'Regenschauer', 81: 'Regenschauer', 82: 'Heftige Schauer',
               85: 'Schneeschauer', 86: 'Heftige Schneeschauer',
               95: 'Gewitter', 96: 'Gewitter/Hagel', 99: 'Schweres Gewitter'}
    return mapping.get(code, 'Unbeständig')


def calc_distance_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def safe_num(arr, idx, default=0.0):
    """FIX: h.get('key', [0])[i] warf bei fehlendem Parameter (nicht jedes Modell liefert
    'visibility' oder 'cape') einen IndexError. Der wurde vom äußeren try verschluckt und
    der komplette Trend-Check meldete danach still 'stabil' - also falsche Entwarnung."""
    try:
        if not isinstance(arr, (list, tuple)) or idx < 0 or idx >= len(arr):
            return default
        v = arr[idx]
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def parse_iso_utc(value):
    """Robustes ISO-Parsing inklusive 'Z'-Suffix (Python < 3.11 kann das nicht nativ)."""
    if not value:
        return None
    try:
        txt = str(value).strip()
        if txt.endswith('Z'):
            txt = txt[:-1] + '+00:00'
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_tour_start(start_time_str):
    """FIX: Die alte Heuristik ("'+' im String oder mehr als zwei '-'") hat ein per
    JS toISOString() erzeugtes Z-Datum als LOKALE Zeit gelesen -> bis zu 2 h Versatz und
    dadurch Warnungen zum falschen Zeitpunkt. Jetzt eindeutig:
      - String mit Zeitzone (Z oder +/-hh:mm)  -> so übernehmen
      - String ohne Zeitzone (datetime-local)  -> als Europe/Vienna interpretieren"""
    if not start_time_str:
        return None
    txt = str(start_time_str).strip()
    tail = txt[10:]  # alles nach dem Datumsteil YYYY-MM-DD

    if txt.endswith('Z') or '+' in tail or '-' in tail:
        dt = parse_iso_utc(txt)
        if dt:
            return dt.astimezone(LOCAL_TZ)
    try:
        naive = datetime.fromisoformat(txt)
        if naive.tzinfo is not None:
            return naive.astimezone(LOCAL_TZ)
        return LOCAL_TZ.localize(naive)
    except Exception:
        return None


# ==============================================================================
# 4. GeoSphere-Parser  --  DAS WAR DER HAUPTFEHLER DER ENGINE
# ==============================================================================
# Die GeoSphere-API liefert die Zeitachse EINMAL auf oberster Ebene der
# FeatureCollection ("timestamps"), nicht pro Feature. Der alte Code suchte sie unter
# feature.properties.time bzw. properties.parameters.rr.timestamps -> Ergebnis immer [].
# Folge: find_onset() bekam nie eine Zeitachse, gab konstant None zurück, und die
# gesamte Nowcast-/Frontenerkennung lief dauerhaft ins Leere. Es wurde nie eine
# Regenwarnung ausgelöst - die Engine meldete immer nur "stabil".
# Der Parser liest jetzt alle bekannten Varianten (der Endpunkt ist offiziell als
# "prerelease" markiert und kann sich noch ändern).
def gs_timestamps(payload, feature=None):
    if not isinstance(payload, dict):
        return []
    for key in ('timestamps', 'time', 'times'):
        v = payload.get(key)
        if isinstance(v, list) and v:
            return v
    if isinstance(feature, dict):
        props = feature.get('properties') or {}
        for key in ('timestamps', 'time', 'times'):
            v = props.get(key)
            if isinstance(v, list) and v:
                return v
        for pdata in (props.get('parameters') or {}).values():
            if isinstance(pdata, dict):
                for key in ('timestamps', 'time', 'times'):
                    v = pdata.get(key)
                    if isinstance(v, list) and v:
                        return v
    return []


def gs_values(feature, param='rr'):
    if not isinstance(feature, dict):
        return []
    params = (feature.get('properties') or {}).get('parameters') or {}
    # Parameternamen kommen je nach Datensatz gross- oder kleingeschrieben zurueck.
    entry = params.get(param) or params.get(param.upper()) or params.get(param.lower())
    if entry is None and len(params) == 1:
        entry = next(iter(params.values()))
    if not isinstance(entry, dict):
        return []
    data = entry.get('data')
    if isinstance(data, list) and data and isinstance(data[0], list):
        return []  # verschachtelte Grid-Matrix - hier nicht als Punktserie nutzbar
    return data or []


def gs_point(feature):
    geom = (feature or {}).get('geometry') or {}
    coords = geom.get('coordinates')
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            return float(coords[1]), float(coords[0])  # lat, lon
        except (TypeError, ValueError):
            return None, None
    return None, None


RAIN_THRESHOLD = 0.02
MIN_SECTOR_KM = 2.0
MAX_SECTOR_KM = 25.0


# 5. Feinfühlige Regenerkennung
def find_onset(times, values, threshold=RAIN_THRESHOLD):
    """FIX: times und values können unterschiedlich lang sein (abgeschnittene Serien).
    Es wird über das Minimum beider Längen iteriert, und ein Treffer ohne gültigen
    Zeitstempel wird verworfen statt mit time=None weitergereicht."""
    if not times or not values:
        return None
    n = min(len(times), len(values))
    for i in range(n):
        try:
            v = float(values[i] or 0)
        except (TypeError, ValueError):
            continue
        try:
            next_v = float(values[i + 1] or 0) if i + 1 < n else v
        except (TypeError, ValueError):
            next_v = v
        if v >= threshold and (next_v >= threshold or i == n - 1):
            if not times[i]:
                continue
            return {"time": times[i], "amount": v}
    return None


def bearing_to_sector(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ['N', 'NO', 'O', 'SO', 'S', 'SW', 'W', 'NW']
    return dirs[round(bearing / 45) % 8]


# ==============================================================================
# 6. Alpine Risiko-Engine
# ==============================================================================
def score_risk_advanced(rain, snow_cm, gust, cape, code, prob, temp, vis=0,
                        tour_types=None, wet_rock=False, is_peak=False):
    if tour_types is None:
        tour_types = []

    s = 0
    reasons = []

    is_exposed = any(t in ['klettersteig', 'grat', 'hochtour', 'gletscher', 'klettern'] for t in tour_types)
    is_klettersteig_or_climb = any(t in ['klettersteig', 'klettern'] for t in tour_types)
    is_hochtour_or_glacier = any(t in ['hochtour', 'gletscher'] for t in tour_types)

    is_thunder = code in [95, 96, 99] or (cape >= 1000 and prob >= 30)
    if is_thunder and is_klettersteig_or_climb:
        return 100, ['Gewittergefahr am Klettersteig/Fels (absolutes Verbot - Drahtseile!)']
    elif is_thunder:
        s += 70
        reasons.append('Gewittergefahr')

    if rain >= 2.0 and (is_klettersteig_or_climb or is_hochtour_or_glacier):
        return 90, ['Starker Regen bei Klettersteig/Hochtour (extremer Nässe- & Absturzfaktor)']

    if wet_rock and (is_klettersteig_or_climb or 'grat' in tour_types):
        s += 45
        reasons.append('Feuchter Fels in exponierter Lage (Rutschgefahr)' if is_peak
                       else 'Fels durch Vorniederschlag noch nass/klamm')

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

    if rain >= 4:
        s += 40
        reasons.append('Starkregen')
    elif rain >= 1:
        s += 25
        reasons.append('Regen')
    elif prob >= 60 and rain < 0.2:
        s += 15
        reasons.append('Hohe Schauerneigung')

    # FIX: Open-Meteo liefert 'snowfall' in ZENTIMETERN. Die alte Schwelle "snow >= 2"
    # wurde im Code wie Millimeter behandelt und lag dadurch faktisch nie richtig.
    if snow_cm >= 1.0:
        s += 35
        reasons.append('Kräftiger Schneefall')
    elif snow_cm >= 0.2:
        s += 20
        reasons.append('Schneefall')

    if temp <= 0 and (rain > 0 or snow_cm > 0):
        s += 35
        reasons.append('Frost & Vereisungsgefahr (Glatteis)')

    if (0 < vis < 2000) or code in [45, 48]:
        s += 25
        reasons.append('Eingeschränkte Sicht (Nebel)')

    if temp >= 30:
        s += 25
        reasons.append('Extreme Hitze (>30 Grad)')
    if temp <= -10 and gust >= 30:
        s += 25
        reasons.append('Gefährlicher Windchill')

    return min(100, s), sorted(set(reasons))


# ==============================================================================
# 7. Nowcast: Raster-Analyse (1 Request) mit Punkt-Fallback
# ==============================================================================
def fetch_precip_grid(lat, lon, half_extent_km):
    dlat = half_extent_km / 111.32
    dlon = half_extent_km / (111.32 * max(.2, math.cos(math.radians(lat))))
    south, north = lat - dlat, lat + dlat
    west, east = lon - dlon, lon + dlon
    url = (f"{GEOSPHERE_BASE}/grid/forecast/nowcast-v1-15min-1km"
           f"?parameters=rr&bbox={south:.5f},{west:.5f},{north:.5f},{east:.5f}"
           f"&output_format=geojson")
    return http_json(url, timeout=20)


def analyze_precip_raster(lat, lon):
    payload = fetch_precip_grid(lat, lon, half_extent_km=MAX_SECTOR_KM)
    if not payload:
        return None
    features = payload.get('features') or []
    if not features:
        return None

    base_times = gs_timestamps(payload)
    cells = []
    for feat in features:
        f_lat, f_lon = gs_point(feat)
        if f_lat is None:
            continue
        times = base_times or gs_timestamps(payload, feat)
        vals = gs_values(feat, 'rr')
        if not times or not vals:
            continue
        cells.append({
            "lat": f_lat, "lon": f_lon,
            "dist": calc_distance_km(lat, lon, f_lat, f_lon),
            "onset": find_onset(times, vals)
        })

    if not cells:
        return None

    # 1) Regnet es schon direkt an der eigenen Position?
    center_cell = min(cells, key=lambda c: c["dist"])
    if center_cell["dist"] < 1.5 and center_cell["onset"]:
        o = center_cell["onset"]
        return {"stage": "arrival", "distance_km": 0, "time": o["time"],
                "direction": None, "amount": o["amount"]}

    # 2) Pro Sektor: Onset-Zeit über Entfernung linear fitten -> Geschwindigkeit & ETA
    sectors = {}
    for c in cells:
        if not (MIN_SECTOR_KM <= c["dist"] <= MAX_SECTOR_KM) or not c["onset"]:
            continue
        sec = bearing_to_sector(lat, lon, c["lat"], c["lon"])
        t_dt = parse_iso_utc(c["onset"]["time"])
        if not t_dt:
            continue
        sectors.setdefault(sec, []).append((c["dist"], t_dt.timestamp(), c["onset"].get("amount", 0)))

    now_epoch = datetime.now(timezone.utc).timestamp()
    candidates = []
    for sec, pts in sectors.items():
        if len(pts) < 3:
            continue
        n = len(pts)
        sum_d = sum(p[0] for p in pts)
        sum_t = sum(p[1] for p in pts)
        sum_dt = sum(p[0] * p[1] for p in pts)
        sum_dd = sum(p[0] * p[0] for p in pts)
        denom = n * sum_dd - sum_d * sum_d
        if abs(denom) < 1e-6:
            continue
        a = (n * sum_dt - sum_d * sum_t) / denom   # Sekunden pro km
        b = (sum_t - a * sum_d) / n                # ETA (epoch) bei Distanz 0

        if a >= 0:
            continue  # Onset-Zeit muss mit sinkender Entfernung sinken (Front zieht heran)
        speed_kmh = -3600.0 / a
        if not (10 <= speed_kmh <= 120):
            continue
        if b < now_epoch - 300 or b > now_epoch + 3 * 3600:
            continue

        nearest_dist = min(p[0] for p in pts)
        amount = max(p[2] for p in pts)
        stage = "early_warning" if nearest_dist >= 16 else ("update_mid" if nearest_dist >= 8 else "update_close")

        candidates.append({
            "stage": stage,
            "distance_km": nearest_dist,
            "time": datetime.fromtimestamp(b, timezone.utc).isoformat(),
            "direction": sec,
            "amount": amount,
            "speed": speed_kmh
        })

    if candidates:
        candidates.sort(key=lambda x: x["time"])
        return candidates[0]
    return None


def analyze_advanced_front_legacy(lat, lon):
    """Punkt-Fallback (Sternmuster), falls der Raster-Endpunkt ausfällt."""
    center_url = (f"{GEOSPHERE_BASE}/timeseries/forecast/nowcast-v1-15min-1km"
                  f"?lat_lon={lat:.5f},{lon:.5f}&parameters=rr&forecast_offset=0&output_format=geojson")
    c_res = http_json(center_url, timeout=12)
    if c_res:
        c_feats = c_res.get('features') or []
        if c_feats:
            c_times = gs_timestamps(c_res, c_feats[0])
            c_vals = gs_values(c_feats[0], 'rr')
            center_onset = find_onset(c_times, c_vals)
            if center_onset:
                return {"stage": "arrival", "distance_km": 0, "time": center_onset["time"],
                        "direction": None, "amount": center_onset["amount"]}

    points = []
    for d in LIVE_DIRS:
        for km in [4, 8, 12, 18, 20]:
            p_lat, p_lon = live_distance_point(lat, lon, d, km)
            points.append({"dir": d["name"], "km": km, "lat": p_lat, "lon": p_lon})

    pts_query = "&".join([f"lat_lon={p['lat']:.5f},{p['lon']:.5f}" for p in points])
    pts_url = (f"{GEOSPHERE_BASE}/timeseries/forecast/nowcast-v1-15min-1km"
               f"?{pts_query}&parameters=rr&forecast_offset=0&output_format=geojson")
    p_res = http_json(pts_url, timeout=20)
    if not p_res:
        return None

    features = p_res.get('features') or []
    base_times = gs_timestamps(p_res)

    grouped = {}
    for idx, p in enumerate(points):
        feat = features[idx] if idx < len(features) else {}
        times = base_times or gs_timestamps(p_res, feat)
        grouped.setdefault(p["dir"], {})[p["km"]] = {"times": times, "vals": gs_values(feat, 'rr')}

    now_epoch = datetime.now(timezone.utc).timestamp()
    candidates = []
    for dir_name, items in grouped.items():
        for outer_km, inner_km in [(20, 18), (18, 12), (12, 4)]:
            if outer_km not in items or inner_km not in items:
                continue
            o_out = find_onset(items[outer_km]["times"], items[outer_km]["vals"])
            o_in = find_onset(items[inner_km]["times"], items[inner_km]["vals"])
            if not o_out or not o_in:
                continue
            t_out_dt = parse_iso_utc(o_out["time"])
            t_in_dt = parse_iso_utc(o_in["time"])
            if not t_out_dt or not t_in_dt:
                continue

            dt = t_in_dt.timestamp() - t_out_dt.timestamp()
            if dt <= 0:
                continue
            speed_kmh = (outer_km - inner_km) / (dt / 3600)
            if not (10 <= speed_kmh <= 120):
                continue

            # Vom inneren Ring braucht die Front noch inner_km bis zur Position -> ADDIEREN.
            eta = t_in_dt.timestamp() + (inner_km / speed_kmh) * 3600
            if eta < now_epoch - 300 or eta > now_epoch + 3 * 3600:
                continue

            stage = "early_warning" if inner_km >= 16 else ("update_mid" if inner_km >= 8 else "update_close")
            candidates.append({
                "stage": stage,
                "distance_km": inner_km,
                "time": datetime.fromtimestamp(eta, timezone.utc).isoformat(),
                "direction": dir_name,
                "amount": max(o_out.get("amount", 0), o_in.get("amount", 0)),
                "speed": speed_kmh
            })

    if candidates:
        candidates.sort(key=lambda x: x["time"])
        return candidates[0]
    return None


def analyze_advanced_front(lat, lon):
    """Raster-Methode zuerst, bei Fehler automatisch Punkt-Fallback."""
    try:
        result = analyze_precip_raster(lat, lon)
        if result is not None:
            return result
    except Exception as e:
        print(f"Grid-Nowcast fehlgeschlagen, Fallback aktiv: {e}")
    try:
        return analyze_advanced_front_legacy(lat, lon)
    except Exception as e:
        print(f"Legacy-Nowcast fehlgeschlagen: {e}")
        return None


# ==============================================================================
# 8. Open-Meteo Abruf
# ==============================================================================
HOURLY_VARS = ("temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,snowfall,"
               "weather_code,precipitation_probability,wind_speed_10m,wind_gusts_10m,"
               "wind_direction_10m,cape,visibility,cloud_cover,freezing_level_height")


def fetch_current_condition(lat, lon):
    url = (f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
           f"&current=temperature_2m,weather_code,wind_speed_10m,wind_gusts_10m"
           f"&timezone=auto&wind_speed_unit=kmh")
    res = http_json(url, timeout=8)
    if not res:
        return "Keine Daten"
    c = res.get('current') or {}
    t = c.get('temperature_2m')
    if t is None:
        return "Keine Daten"
    gust = c.get('wind_gusts_10m')
    desc = wmo_to_text(int(c.get('weather_code') or 0))
    gust_txt = f", Böen {round(float(gust))} km/h" if gust not in (None, "") else ""
    return f"{float(t):.1f} Grad, {desc}{gust_txt}"


def build_multi_location_update(tour):
    """FIX: Start / Live / Gipfel werden nur noch abgefragt, wenn die Punkte auch wirklich
    auseinanderliegen. Vorher wurde der Gipfel selbst bei identischen Koordinaten erneut
    geholt und dieselbe Zeile doppelt in die Push-Nachricht geschrieben."""
    msg_parts = []
    seen = []

    def add(label, lat, lon):
        try:
            pair = (float(lat), float(lon))
        except (TypeError, ValueError):
            return
        for s in seen:
            if calc_distance_km(pair[0], pair[1], s[0], s[1]) < 1.0:
                return
        seen.append(pair)
        msg_parts.append(f"{label}: {fetch_current_condition(pair[0], pair[1])}")

    add("Start", tour.get('start_lat'), tour.get('start_lon'))
    add("Live", tour.get('lat'), tour.get('lon'))
    add("Gipfel", tour.get('peak_lat'), tour.get('peak_lon'))
    return "\n".join(msg_parts)


def fetch_hourly(lat, lon, date_str, end_date_str, elevation=None):
    """FIX: Es wird nur noch 'best_match' abgefragt. Vorher wurden 6 Modelle geladen, aber
    ausschliesslich best_match ausgewertet - das kostete unnötig Quota und erzeugte
    unterschiedlich lange Arrays."""
    url = (f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}&hourly={HOURLY_VARS}"
           f"&start_date={date_str}&end_date={end_date_str}&timezone=auto&wind_speed_unit=kmh")
    if elevation:
        url += f"&elevation={elevation}"
    res = http_json(url, timeout=12)
    if not res:
        return {}
    h = res.get('hourly') or {}
    for key in list(h.keys()):
        if key.endswith('_best_match'):
            h[key.replace('_best_match', '')] = h[key]
    return h


# 9. Prognose-Trend mit Tal/Gipfel, Wet-Rock & fokussierten Risikogründen
def check_forecast_trend(lat, lon, start_dt, duration, tour_types=None, peak_lat=None, peak_lon=None):
    if tour_types is None:
        tour_types = []
    try:
        end_dt = start_dt + timedelta(hours=duration)
        # FIX: lokale Kalendertage. Wird das Ende per UTC bestimmt, fehlt bei Touren über
        # Mitternacht der letzte Tag komplett und der Trend endet zu früh.
        date_str = (start_dt - timedelta(hours=4)).strftime('%Y-%m-%d')
        end_date_str = (end_dt + timedelta(hours=1)).strftime('%Y-%m-%d')
        is_summer = (5 <= start_dt.month <= 7)

        h = fetch_hourly(lat, lon, date_str, end_date_str)
        times = h.get('time') or []
        if not times:
            return "unknown", "Trend konnte gerade nicht abgerufen werden."

        ph = {}
        if peak_lat and peak_lon:
            ph = fetch_hourly(peak_lat, peak_lon, date_str, end_date_str)
        peak_time_index = {t: i for i, t in enumerate(ph.get('time') or [])}

        max_risk = 0
        worst_reasons = []
        slots_evaluated = 0

        for i, t_str in enumerate(times):
            try:
                t_dt = LOCAL_TZ.localize(datetime.fromisoformat(t_str))
            except Exception:
                continue
            if not (start_dt <= t_dt <= end_dt):
                continue
            slots_evaluated += 1

            temp = safe_num(h.get('temperature_2m'), i)
            rain = safe_num(h.get('precipitation'), i)
            snow = safe_num(h.get('snowfall'), i)      # cm
            gust = safe_num(h.get('wind_gusts_10m'), i)
            cape = safe_num(h.get('cape'), i)
            code = int(safe_num(h.get('weather_code'), i))
            prob = safe_num(h.get('precipitation_probability'), i)
            vis = safe_num(h.get('visibility'), i)

            base_wet_rock = False
            for p in range(1, 4):
                if i - p >= 0 and safe_num(h.get('precipitation'), i - p) > 0.1:
                    if not is_summer or p == 1:
                        base_wet_rock = True

            slot_score, slot_reasons = score_risk_advanced(
                rain, snow, gust, cape, code, prob, temp, vis, tour_types, base_wet_rock, False)
            slot_reasons = list(slot_reasons)

            p_idx = peak_time_index.get(t_str)
            if p_idx is not None:
                p_wet_rock = False
                for p_step in range(1, 4):
                    if p_idx - p_step >= 0 and safe_num(ph.get('precipitation'), p_idx - p_step) > 0.1:
                        if not is_summer or p_step == 1:
                            p_wet_rock = True

                p_score, p_reasons = score_risk_advanced(
                    safe_num(ph.get('precipitation'), p_idx, rain),
                    safe_num(ph.get('snowfall'), p_idx, snow),
                    safe_num(ph.get('wind_gusts_10m'), p_idx, gust),
                    safe_num(ph.get('cape'), p_idx, cape),
                    int(safe_num(ph.get('weather_code'), p_idx, code)),
                    safe_num(ph.get('precipitation_probability'), p_idx, prob),
                    safe_num(ph.get('temperature_2m'), p_idx, temp),
                    safe_num(ph.get('visibility'), p_idx, vis),
                    tour_types, p_wet_rock, True)

                if p_score > slot_score:
                    slot_score = p_score
                    slot_reasons = list(p_reasons)
                else:
                    slot_reasons.extend(p_reasons)

            if slot_score > max_risk:
                max_risk = slot_score
                worst_reasons = sorted(set(slot_reasons))

        if slots_evaluated == 0:
            # FIX: Kein einziger Zeitschritt lag im Tourfenster -> kein "stabil" vortäuschen.
            return "unknown", "Für das Tourfenster liegen aktuell keine Prognosedaten vor."

        reasons_txt = ", ".join(worst_reasons[:2]) if worst_reasons else "Allgemeine Unbeständigkeit"

        if max_risk >= 60:
            return "danger", f"Hohes alpines Risiko prognostiziert ({reasons_txt}). Tour nicht empfohlen."
        if max_risk >= 30:
            return "warning", f"Anspruchsvolle Bedingungen prognostiziert. Achte auf: {reasons_txt}."
        if max_risk >= 15:
            return "moderate", f"Trend: leicht unbeständig ({reasons_txt})."

    except Exception as e:
        print(f"Fehler beim Trend-Check: {e}")
        return "unknown", "Trend konnte gerade nicht berechnet werden."

    return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."


# ==============================================================================
# 10a. Selbsttest gegen die echten APIs
# ------------------------------------------------------------------------------
# Der Cronjob laeuft normalerweise still durch: liegt keine Tour im Zeitfenster,
# gibt es keinerlei Ausgabe. Aus dem Actions-Log ist dann NICHT ablesbar, ob die
# Engine funktioniert oder nur nichts zu tun hatte. Dieser Modus prueft genau die
# Stellen, an denen die Engine vorher stillschweigend ausgefallen ist:
#   1. Liefert der GeoSphere-Parser eine Zeitachse?  (war der Hauptfehler)
#   2. Welche Open-Meteo-Parameter kommen wirklich zurueck?  (stiller Trend-Ausfall)
# Aufruf:  python check_weather.py --selftest
# ==============================================================================
TEST_LAT, TEST_LON = 47.0793, 12.6951   # Grossglockner


def selftest():
    ok = True
    print("=" * 66)
    print("SELBSTTEST DER WETTER-ENGINE")
    print("=" * 66)

    # --- 1. GeoSphere Nowcast: Zeitachse -------------------------------------
    print("\n[1] GeoSphere Nowcast (timeseries)")
    url = (f"{GEOSPHERE_BASE}/timeseries/forecast/nowcast-v1-15min-1km"
           f"?lat_lon={TEST_LAT},{TEST_LON}&parameters=rr&forecast_offset=0&output_format=geojson")
    payload = http_json(url, timeout=15)

    if not payload:
        print("    FEHLER: Endpunkt nicht erreichbar.")
        ok = False
    else:
        print(f"    Antwort-Schluessel: {sorted(payload.keys())}")
        feats = payload.get('features') or []
        print(f"    Features: {len(feats)}")

        if not feats:
            print("    FEHLER: Keine Features in der Antwort.")
            ok = False
        else:
            feat = feats[0]
            times = gs_timestamps(payload, feat)
            vals = gs_values(feat, 'rr')

            # Genau das, was der alte Code getan hat - zum direkten Vergleich.
            props = feat.get('properties', {})
            alt_times = props.get('time', props.get('parameters', {}).get('rr', {}).get('timestamps', []))

            print(f"    NEUER Parser  -> {len(times)} Zeitstempel, {len(vals)} rr-Werte")
            print(f"    ALTER Parser  -> {len(alt_times)} Zeitstempel")

            if not times:
                print("    FEHLER: Zeitachse weiterhin leer. Antwortstruktur hat sich geaendert.")
                print(f"    properties-Schluessel: {sorted(props.keys())}")
                print(f"    parameters-Schluessel: {sorted((props.get('parameters') or {}).keys())}")
                ok = False
            else:
                print(f"    Zeitraum: {times[0]}  bis  {times[-1]}")
                if not alt_times:
                    print("    -> Bestaetigt: der alte Parser fand hier nichts. Fix greift.")
                total = sum(float(v or 0) for v in vals)
                onset = find_onset(times, vals)
                print(f"    Niederschlagssumme im Nowcast-Fenster: {total:.2f} mm")
                print(f"    Regenbeginn erkannt: {onset['time'] if onset else 'kein Regen im Fenster'}")

            if not vals:
                print("    FEHLER: Keine rr-Werte gelesen.")
                ok = False

    # --- 2. GeoSphere Grid ----------------------------------------------------
    print("\n[2] GeoSphere Nowcast (grid, Bounding-Box)")
    grid = fetch_precip_grid(TEST_LAT, TEST_LON, MAX_SECTOR_KM)
    if not grid:
        print("    Endpunkt nicht erreichbar - Punkt-Fallback wuerde greifen.")
    else:
        gfeats = grid.get('features') or []
        gtimes = gs_timestamps(grid)
        print(f"    Rasterzellen: {len(gfeats)}, Zeitstempel: {len(gtimes)}")
        usable = 0
        for f in gfeats:
            f_lat, f_lon = gs_point(f)
            if f_lat is not None and gs_values(f, 'rr'):
                usable += 1
        print(f"    Auswertbare Zellen (Koordinate + Werte): {usable}")
        if gfeats and usable == 0:
            print("    WARNUNG: Zellen vorhanden, aber keine auswertbar - Fallback greift.")

    # --- 3. Frontenerkennung end-to-end ---------------------------------------
    print("\n[3] Frontenerkennung (end-to-end)")
    front = analyze_advanced_front(TEST_LAT, TEST_LON)
    if front:
        print(f"    Treffer: Stufe={front['stage']}  Richtung={front.get('direction')}  "
              f"Distanz={front.get('distance_km', 0):.1f} km  ETA={front['time']}")
    else:
        print("    Keine Front erkannt. Bei trockenem Wetter ist das das korrekte Ergebnis.")

    # --- 4. Open-Meteo: welche Parameter kommen wirklich? ---------------------
    print("\n[4] Open-Meteo Stundenwerte")
    today = datetime.now(LOCAL_TZ)
    h = fetch_hourly(TEST_LAT, TEST_LON, today.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d'))
    if not h:
        print("    FEHLER: Kein Abruf moeglich.")
        ok = False
    else:
        n_time = len(h.get('time') or [])
        print(f"    Zeitschritte: {n_time}")
        fehlend = []
        for p in HOURLY_VARS.split(','):
            arr = h.get(p)
            status = "fehlt" if not isinstance(arr, list) else (
                "leer" if len(arr) == 0 else (
                    f"nur {len(arr)} Werte" if len(arr) < n_time else "ok"))
            if status != "ok":
                fehlend.append(f"{p} ({status})")
        if fehlend:
            # Genau hier ist der Trend-Check frueher still auf "stabil" gefallen.
            print(f"    Unvollstaendig: {', '.join(fehlend)}")
            print("    -> safe_num() faengt das ab; frueher gab es hier einen IndexError.")
        else:
            print("    Alle angeforderten Parameter vollstaendig vorhanden.")

    # --- 5. Trend-Check end-to-end -------------------------------------------
    print("\n[5] Trend-Check (end-to-end, 6h ab jetzt)")
    status, msg = check_forecast_trend(TEST_LAT, TEST_LON, today, 6, ['wandern'])
    print(f"    Status: {status}")
    print(f"    Text:   {msg}")
    if status == "unknown":
        print("    Hinweis: 'unknown' meldet ehrlich fehlende Daten. Frueher stand hier faelschlich 'stabil'.")

    # --- 6. Firestore ---------------------------------------------------------
    print("\n[6] Firestore")
    try:
        docs = list(db.collection('tour_subscriptions').stream())
        print(f"    Dokumente in tour_subscriptions: {len(docs)}")
        now = datetime.now(LOCAL_TZ)
        for d in docs:
            t = d.to_dict() or {}
            start = parse_tour_start(t.get('startTime'))
            dur = float(t.get('duration') or 6)
            if not start:
                print(f"    - {d.id}: Startzeit nicht lesbar ({t.get('startTime')!r})")
                continue
            end = start + timedelta(hours=dur)
            drin = start <= now <= end
            hat_token = bool(t.get('token'))
            print(f"    - {d.id}: {start:%d.%m. %H:%M} bis {end:%d.%m. %H:%M}  "
                  f"| aktiv: {'JA' if drin else 'nein'} | Token: {'ja' if hat_token else 'FEHLT'}")
    except Exception as e:
        print(f"    FEHLER: {e}")
        ok = False

    print("\n" + "=" * 66)
    print("ERGEBNIS: " + ("Engine arbeitet." if ok else "Es gibt Probleme - siehe oben."))
    print("=" * 66)
    return ok


# ==============================================================================
# 10b. Hauptschleife für den Cronjob
# ==============================================================================
HOURLY_INTERVAL_S = 3600


def interpolate_position(start_lat, start_lon, peak_lat, peak_lon, progress):
    is_round_trip = calc_distance_km(start_lat, start_lon, peak_lat, peak_lon) < 0.5
    if is_round_trip:
        path_prog = progress * 2.0 if progress <= 0.5 else 2.0 - (progress * 2.0)
    else:
        path_prog = progress
    return (start_lat + (peak_lat - start_lat) * path_prog,
            start_lon + (peak_lon - start_lon) * path_prog)


def check_all_tours():
    now = datetime.now(LOCAL_TZ)
    now_utc = now.astimezone(timezone.utc)
    local_time_str = now.strftime("%H:%M") + " Uhr"

    # Der Lauf war bisher komplett stumm, wenn keine Tour im Fenster lag. Aus dem
    # Actions-Log liess sich dadurch nicht unterscheiden, ob die Engine sauber
    # durchgelaufen ist oder gar nicht erst angesprungen hat.
    stat = {'gesamt': 0, 'aktiv': 0, 'ohne_token': 0, 'zeit_kaputt': 0,
            'ausserhalb': 0, 'fronten': 0, 'pushes': 0, 'fehler': 0}
    print(f"Lauf gestartet: {now:%d.%m.%Y %H:%M %Z}")

    for doc in db.collection('tour_subscriptions').stream():
        tour = doc.to_dict() or {}
        stat['gesamt'] += 1
        try:
            token = tour.get('token')
            lat = tour.get('lat') or tour.get('start_lat')
            lon = tour.get('lon') or tour.get('start_lon')
            start_time_str = tour.get('startTime')
            duration = float(tour.get('duration') or 6)
            tour_types = tour.get('tourTypes') or []
            peak_lat = tour.get('peak_lat')
            peak_lon = tour.get('peak_lon')
            start_lat = tour.get('start_lat') or lat
            start_lon = tour.get('start_lon') or lon

            last_alert_title = tour.get('last_alert_title', '')
            last_state = tour.get('last_weather_state', 'unknown')
            last_hourly_check = tour.get('last_hourly_check')

            if not token or lat is None or lon is None or not start_time_str:
                stat['ohne_token'] += 1
                continue

            lat, lon = float(lat), float(lon)
            start_dt = parse_tour_start(start_time_str)
            if not start_dt:
                stat['zeit_kaputt'] += 1
                print(f"Ungültige Startzeit bei {doc.id}: {start_time_str!r}")
                continue

            end_dt = start_dt + timedelta(hours=duration)
            if not (start_dt <= now <= end_dt):
                stat['ausserhalb'] += 1
                continue

            stat['aktiv'] += 1

            # --- Live-GPS vs. Routen-Interpolation -----------------------------
            updated_at = tour.get('updatedAt')
            is_gps_stale = True
            if isinstance(updated_at, datetime):
                ua = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
                if (now_utc - ua.astimezone(timezone.utc)).total_seconds() < 1800:
                    is_gps_stale = False

            if is_gps_stale and peak_lat and peak_lon and start_lat and start_lon:
                progress = max(0.0, min(1.0, (now - start_dt).total_seconds() / (duration * 3600)))
                lat, lon = interpolate_position(float(start_lat), float(start_lon),
                                               float(peak_lat), float(peak_lon), progress)

            # --- Nowcast / Frontenerkennung ------------------------------------
            front = analyze_advanced_front(lat, lon)
            if front:
                stat['fronten'] += 1
            print(f"  {doc.id}: aktiv bei {lat:.4f},{lon:.4f} | Nowcast: "
                  + (f"{front['stage']} in {front.get('distance_km', 0):.0f} km" if front else "kein Niederschlag"))
            current_stage = "stable"
            alert_title = ""
            alert_body = ""

            if front:
                arr_dt = parse_iso_utc(front["time"])
                mins_left = max(0, int((arr_dt - now_utc).total_seconds() / 60)) if arr_dt else 0
                dir_txt = f" aus {direction_name(front['direction'])}" if front.get("direction") else ""
                speed_txt = f" (ca. {int(front.get('speed', 30))} km/h)" if front.get("speed") else ""
                time_txt = f"in ca. {mins_left} Min." if mins_left > 0 else "unmittelbar"

                stage = front["stage"]
                amount = front.get("amount", 0)
                rain_desc = get_rain_description(amount)
                dist_km = int(round(front.get("distance_km", 0)))
                current_stage = stage

                if stage == "arrival":
                    if amount < 0.2:
                        alert_title = f"Es fängt an zu nieseln [{local_time_str}]"
                        alert_body = "Nieselregen hat deinen Standort direkt erreicht."
                    elif amount < 2.0:
                        alert_title = f"Es fängt an leicht zu regnen [{local_time_str}]"
                        alert_body = "Leichter Regen hat deinen Standort direkt erreicht."
                    else:
                        alert_title = f"Es fängt an zu regnen ({rain_desc}) [{local_time_str}]"
                        alert_body = f"Niederschlag ({rain_desc}) ist jetzt direkt aktiv."
                else:
                    incoming = ("Es kommt Nieselregen" if amount < 0.2
                                else ("Es fängt gleich an zu regnen" if amount < 2.0
                                      else "Regenfront im Anmarsch"))
                    if stage == "early_warning":
                        alert_title = f"Niederschlag im Anmarsch [{local_time_str}]"
                        alert_body = f"{incoming}{dir_txt} - Ankunft {time_txt}{speed_txt}."
                    elif stage == "update_mid":
                        # FIX: Die Entfernung war fest als "12 km" in den Text geschrieben,
                        # obwohl der echte Wert in front['distance_km'] steht.
                        alert_title = f"Regen rückt näher [{local_time_str}]"
                        alert_body = f"Entfernung ca. {dist_km} km, {incoming.lower()}{dir_txt} (ETA: {time_txt})."
                    elif stage == "update_close":
                        alert_title = f"Letzte Warnung (ca. {dist_km} km) [{local_time_str}]"
                        alert_body = f"{incoming} unmittelbar vor deiner Position. Eintreffen {time_txt}."
            else:
                if last_state in ['danger', 'worsening', 'early_warning', 'update_mid',
                                  'update_close', 'arrival']:
                    current_stage = "improving"
                    trend_status, _ = check_forecast_trend(lat, lon, start_dt, duration,
                                                          tour_types, peak_lat, peak_lon)
                    if trend_status == "stable":
                        alert_title = f"Niederschlag löst sich auf [{local_time_str}]"
                        alert_body = "Der Regen stoppt und die Prognose zeigt keinen weiteren Niederschlag."
                    else:
                        alert_title = f"Vorübergehende Regenpause [{local_time_str}]"
                        alert_body = "Die Zelle ist abgezogen, das Wetter bleibt laut Prognose unbeständig."

            update_data = {'last_weather_state': current_stage}
            send_hazard_alert = bool(alert_title) and alert_title != last_alert_title

            if send_hazard_alert:
                try:
                    response = send_high_priority_push(alert_title, alert_body, token)
                    print(f"[Gefahr] Push gesendet: {response}")
                    stat['pushes'] += 1
                    update_data['last_alert_title'] = alert_title
                except Exception as fe:
                    print(f"[Gefahr] Push-Fehler: {fe}")
                    # FIX: Bei fehlgeschlagenem Push darf der Zustand NICHT weitergeschrieben
                    # werden, sonst gilt die Warnung als zugestellt und wiederholt sich nie.
                    update_data.pop('last_weather_state', None)
                    if is_dead_token(fe):
                        print(f"Ungültiger Token für {doc.id} - wird entfernt.")
                        db.collection('tour_subscriptions').document(doc.id).update({'token': None})
                        continue

            # --- Stündliches Update --------------------------------------------
            last_check_dt = parse_iso_utc(last_hourly_check)
            if last_check_dt is None:
                next_update_due = start_dt.astimezone(timezone.utc) + timedelta(seconds=HOURLY_INTERVAL_S)
            else:
                next_update_due = last_check_dt + timedelta(seconds=HOURLY_INTERVAL_S)

            # FIX gegen Push-Spam: Lag die Tour beim ersten Lauf schon Stunden zurück, wurde
            # ein längst vergangener Zeitpunkt als neuer Referenzpunkt gespeichert -> jeder
            # folgende Cron-Lauf (alle 15 Min) feuerte sofort erneut.
            while next_update_due < now_utc - timedelta(seconds=HOURLY_INTERVAL_S):
                next_update_due += timedelta(seconds=HOURLY_INTERVAL_S)

            if now_utc >= next_update_due:
                multi_loc_status = build_multi_location_update({
                    'start_lat': start_lat, 'start_lon': start_lon,
                    'lat': lat, 'lon': lon,
                    'peak_lat': peak_lat, 'peak_lon': peak_lon
                })
                trend_status, trend_msg = check_forecast_trend(lat, lon, start_dt, duration,
                                                              tour_types, peak_lat, peak_lon)
                hourly_title = f"Stündliches Wetter-Update [{local_time_str}]"
                hourly_body = "\n\n".join([p for p in [multi_loc_status, trend_msg] if p])

                try:
                    response = send_high_priority_push(hourly_title, hourly_body, token)
                    print(f"[Stündlich] Push gesendet: {response}")
                    stat['pushes'] += 1
                    update_data['last_hourly_check'] = now_utc.isoformat()
                except Exception as fe:
                    print(f"[Stündlich] Push-Fehler: {fe}")
                    if is_dead_token(fe):
                        db.collection('tour_subscriptions').document(doc.id).update({'token': None})
                        continue

            if update_data:
                db.collection('tour_subscriptions').document(doc.id).update(update_data)

        except Exception as e:
            stat['fehler'] += 1
            print(f"DEBUG Fehler bei Tour {doc.id}: {e}")

    print(f"Lauf beendet: {stat['gesamt']} Abos gesamt, {stat['aktiv']} aktiv, "
          f"{stat['ausserhalb']} ausserhalb des Zeitfensters, "
          f"{stat['ohne_token']} ohne Token/Koordinaten, {stat['zeit_kaputt']} mit defekter Startzeit, "
          f"{stat['fronten']} Nowcast-Treffer, {stat['pushes']} Pushes, {stat['fehler']} Fehler.")
    if stat['gesamt'] == 0:
        print("Hinweis: Keine Abos vorhanden. Der Lauf hatte nichts zu tun - das sagt "
              "nichts darueber aus, ob die Wetterabfrage funktioniert. Dafuer: --selftest")


if __name__ == "__main__":
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
    check_all_tours()
