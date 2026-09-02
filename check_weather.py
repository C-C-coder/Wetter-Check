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
# FIX (doppelte Meldungen): Enthält eine FCM-Nachricht einen `notification`-Block, zeigt
# das FCM-SDK im Browser sie SELBST an - und zusätzlich läuft onBackgroundMessage() im
# Service Worker und zeigt sie ein zweites Mal. Genau das war im Screenshot zu sehen:
# jede Warnung doppelt, einmal mit App-Logo, einmal mit grauem Platzhalter-Icon.
# Deshalb wird jetzt data-only gesendet; die Darstellung macht ausschließlich der
# Service Worker. `collapse_key`/`Topic` sorgt zusätzlich dafür, dass noch nicht
# zugestellte ältere Warnungen durch die neueste ersetzt statt nachgeliefert werden.
def send_high_priority_push(title, body, token, tag='tour-alert', collapse=True,
                             click_url="./index.html#activeTour"):
    msg = messaging.Message(
        data={'title': title, 'body': body, 'tag': tag, 'click_url': click_url},
        token=token,
        android=messaging.AndroidConfig(
            priority='high',
            ttl=timedelta(hours=2),
            collapse_key=tag if collapse else None
        ),
        webpush=messaging.WebpushConfig(
            headers={'Urgency': 'high', 'TTL': '7200'},
            data={'title': title, 'body': body, 'tag': tag, 'click_url': click_url}
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

# ==============================================================================
#  LATENZBUDGET - warum die Stufenschwellen so liegen, wie sie liegen
# ------------------------------------------------------------------------------
#  Von der Entstehung einer Regenzelle bis zum Vibrieren des Handys vergeht:
#
#    Nowcast-Rechenzyklus         0-15 min   (GeoSphere rechnet alle 15 Minuten;
#                                             nicht beeinflussbar)
#    Entdeckung durch den Cron    0-N  min   (N = Trigger-Intervall)
#    Actions-Start + Laufzeit     ~0,5 min   (im Log gemessen: 24 s)
#    FCM-Zustellung + Doze-Mode   0-2  min
#    ------------------------------------------------------------
#    Summe bei 5-Min-Trigger      bis ca. 18 min
#    Summe bei 10-Min-Trigger     bis ca. 23 min
#
#  Wichtig: Die 15 Minuten Rechenzyklus und das Trigger-Intervall addieren sich
#  NICHT sauber auf. Bei jedem Lauf wird der jeweils neueste verfügbare Nowcast
#  geholt - das Trigger-Intervall bestimmt nur, wie schnell ein FRISCH
#  veröffentlichter Lauf entdeckt wird, nicht dessen Alter obendrauf.
#  Der Unterschied zwischen 5 und 10 Minuten Trigger beträgt daher höchstens
#  5 Minuten, nicht 25.
#
#  Entscheidend ist etwas anderes: Eine Warnstufe, die erst bei ETA <= 15 min
#  auslöst, ist bei ~18 min Pipeline-Latenz strukturell zu spät - die Meldung
#  kommt an, wenn es bereits regnet. Die Schwellen liegen deshalb bewusst über
#  der Latenz, damit auch die späteste Stufe noch nutzbare Vorwarnzeit lässt.
# ==============================================================================
PIPELINE_LATENCY_MIN = 18

STAGE_ETA_CLOSE_MIN = 35    # "steht unmittelbar bevor" - nach Latenz bleiben ~17 min
STAGE_ETA_MID_MIN = 80      # "zieht heran"            - nach Latenz bleibt ~1 h


def stage_from_eta(eta_minutes):
    if eta_minutes <= STAGE_ETA_CLOSE_MIN:
        return "update_close"
    if eta_minutes <= STAGE_ETA_MID_MIN:
        return "update_mid"
    return "early_warning"


def nowcast_run_age_min(payload):
    """Alter des Nowcast-Laufs in Minuten, damit die tatsächliche Latenz im Log
    sichtbar ist statt geschätzt werden zu müssen."""
    if not isinstance(payload, dict):
        return None
    for key in ('reference_time', 'referenceTime', 'run', 'analysis_time'):
        v = payload.get(key)
        if isinstance(v, str):
            dt = parse_iso_utc(v)
            if dt:
                return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    meta = payload.get('metadata')
    if isinstance(meta, dict):
        return nowcast_run_age_min(meta)
    # Ersatzweise: der erste Zeitstempel der Serie ist der Beginn des Laufs.
    ts = gs_timestamps(payload)
    if ts:
        dt = parse_iso_utc(ts[0])
        if dt:
            return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    return None


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
def wind_chill(temp_c, wind_kmh):
    """Gefühlte Temperatur nach der internationalen Windchill-Formel (gültig ab
    Temperatur <= 10 Grad und Wind >= 4,8 km/h). Ein konkreter Wert sagt mehr als
    das Etikett "gefährlich"."""
    if temp_c > 10 or wind_kmh < 4.8:
        return None
    v = wind_kmh ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v


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
        return 100, ['Gewitter am Klettersteig/Fels (Drahtseile leiten Blitzstrom)']
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

    # FIX: Backend prüfte <= 0 Grad, die App <= 1 Grad. Angeglichen auf den
    # sichereren Wert: Fels und Metall kühlen durch Abstrahlung unter die
    # Lufttemperatur ab, Glatteis bildet sich daher schon bei leichten Plusgraden.
    if temp <= 1 and (rain > 0.05 or snow_cm > 0):
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
        gefuehlt = wind_chill(temp, gust)
        reasons.append(f'Windchill {round(gefuehlt)} Grad gefühlt' if gefuehlt is not None
                       else 'Gefährlicher Windchill')

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

    run_age = nowcast_run_age_min(payload)
    if run_age is not None:
        print(f"    Nowcast-Lauf ist {run_age:.0f} Min. alt")

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

    # 1) Niederschlag direkt an der eigenen Position?
    # FIX: Hier wurde JEDER Treffer der Zeitreihe als "arrival" gemeldet - auch
    # einer, der 90 Minuten in der Zukunft liegt. Die Meldung lautete dann
    # "Nieselregen hat deinen Standort direkt erreicht", obwohl es trocken war.
    center_cell = min(cells, key=lambda c: c["dist"])
    if center_cell["dist"] < 1.5 and center_cell["onset"]:
        o = center_cell["onset"]
        o_dt = parse_iso_utc(o["time"])
        eta_min = (o_dt - datetime.now(timezone.utc)).total_seconds() / 60 if o_dt else 0
        if eta_min <= 10:
            return {"stage": "arrival", "distance_km": 0, "time": o["time"],
                    "direction": None, "amount": o["amount"]}
        return {"stage": stage_from_eta(eta_min), "distance_km": 0, "time": o["time"],
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
            continue  # Onset muss mit steigender Entfernung früher liegen
        speed_kmh = -3600.0 / a
        if not (10 <= speed_kmh <= 120):
            continue
        if b < now_epoch - 300 or b > now_epoch + 3 * 3600:
            continue

        nearest_dist = min(p[0] for p in pts)
        amount = max(p[2] for p in pts)

        # FIX: Die Stufe kam bisher allein aus der Entfernung, die ETA dagegen aus
        # der Regression. Bei zerrissenen Nieselfeldern ergab das Meldungen wie
        # "Letzte Warnung (ca. 3 km) - Eintreffen in ca. 147 Min." - also 1,2 km/h.
        stage = stage_from_eta((b - now_epoch) / 60.0)

        # Passt die Gerade schlecht auf die Messpunkte, ist das Feld zu zerrissen
        # für ein Frontmodell und die extrapolierte Ankunftszeit wertlos.
        if max(abs(p[1] - (a * p[0] + b)) for p in pts) > 40 * 60:
            continue

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

            candidates.append({
                "stage": stage_from_eta((eta - now_epoch) / 60.0),
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


# ==============================================================================
#  ROUTENVERLAUF: Position entlang des echten Wegs
# ------------------------------------------------------------------------------
#  Ohne Track bleibt nur die Luftlinie Start -> Gipfel. Führt die Route im Bogen
#  ums Massiv, liegen die Zwischenpunkte dann kilometerweit neben dem echten Weg -
#  und damit auch die Stellen, an denen der Nowcast abgefragt wird.
#
#  Mit Track wird zusätzlich berücksichtigt, dass bergauf langsamer gegangen wird.
#  Reine Streckenaufteilung würde die Person am Anfang zu weit vorne und am
#  steilen Schlussanstieg zu weit hinten verorten. Deshalb Naismith:
#  1 Stunde je 5 km Strecke plus 1 Stunde je 600 Höhenmeter Anstieg. Der Abstieg
#  wird leicht entlastet, aber nicht geschenkt.
# ==============================================================================
NAISMITH_KM_PER_H = 5.0
NAISMITH_ASCENT_M_PER_H = 600.0


def parse_track(raw):
    """Track aus Firestore in eine Liste von (lat, lon, ele) überführen.
    Akzeptiert sowohl Dict- als auch Listenform, damit ein Formatwechsel im
    Frontend das Backend nicht lahmlegt."""
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    punkte = []
    for p in raw:
        try:
            if isinstance(p, dict):
                la, lo = float(p.get('lat')), float(p.get('lon'))
                el = p.get('ele')
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                la, lo = float(p[0]), float(p[1])
                el = p[2] if len(p) > 2 else None
            else:
                continue
            punkte.append((la, lo, float(el) if el is not None else None))
        except (TypeError, ValueError):
            continue
    return punkte if len(punkte) >= 2 else None


def track_effort_profile(punkte):
    """Kumulierter Zeitaufwand entlang des Tracks, normiert auf 0..1."""
    kumuliert = [0.0]
    gesamt = 0.0
    for i in range(1, len(punkte)):
        a, b = punkte[i - 1], punkte[i]
        dist = calc_distance_km(a[0], a[1], b[0], b[1])
        aufwand = dist / NAISMITH_KM_PER_H
        if a[2] is not None and b[2] is not None:
            delta = b[2] - a[2]
            if delta > 0:
                aufwand += delta / NAISMITH_ASCENT_M_PER_H
            else:
                aufwand += abs(delta) / (NAISMITH_ASCENT_M_PER_H * 3)
        gesamt += aufwand
        kumuliert.append(gesamt)
    if gesamt <= 0:
        return None
    return [k / gesamt for k in kumuliert]


def position_on_track(punkte, profil, progress):
    """Position bei einem Fortschritt von 0..1, linear zwischen den Stützpunkten."""
    prog = max(0.0, min(1.0, progress))
    for i in range(1, len(profil)):
        if prog <= profil[i]:
            spanne = profil[i] - profil[i - 1]
            f = 0.0 if spanne <= 0 else (prog - profil[i - 1]) / spanne
            a, b = punkte[i - 1], punkte[i]
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
    return (punkte[-1][0], punkte[-1][1])


def nearest_progress_on_track(punkte, profil, lat, lon):
    """Welchem Fortschritt entspricht die aktuelle GPS-Position?
    Nötig, damit jemand, der schneller oder langsamer unterwegs ist als geplant,
    nicht an der falschen Stelle der Route verortet wird."""
    best_i, best_d = 0, float('inf')
    for i, p in enumerate(punkte):
        d = calc_distance_km(lat, lon, p[0], p[1])
        if d < best_d:
            best_d, best_i = d, i
    # Nur übernehmen, wenn die Position auch wirklich am Weg liegt.
    if best_d > 1.5:
        return None
    return profil[best_i]

# ==============================================================================
# 7a. TRAJEKTORIEN-NOWCAST  --  Ergänzung zur Abschnittswarnung
# ------------------------------------------------------------------------------
#  Die Abschnittswarnung ist der Hauptweg. Die punktgenaue Trajektorie bleibt als
#  Rückfallebene erhalten, falls kein Abschnitt greift (etwa weil weder Track noch
#  Gipfel hinterlegt sind). fetch_nowcast_points() wird zusätzlich von der
#  Abschnittsanalyse mitbenutzt.
# ==============================================================================
TRAJECTORY_HORIZON_MIN = 150
TRAJECTORY_STEP_MIN = 15


def build_trajectory(now, start_dt, end_dt, duration_h, cur_lat, cur_lon,
                     start_lat, start_lon, peak_lat, peak_lon, track=None):
    """Voraussichtliche Aufenthaltsorte in 15-Minuten-Schritten."""
    punkte = [{'t': now, 'lat': cur_lat, 'lon': cur_lon, 'offset': 0}]

    tp = parse_track(track)
    profil = track_effort_profile(tp) if tp else None

    if profil:
        prog_jetzt = nearest_progress_on_track(tp, profil, cur_lat, cur_lon)
        if prog_jetzt is None:
            prog_jetzt = max(0.0, min(1.0, (now - start_dt).total_seconds() / (duration_h * 3600)))

        def route_pos(t):
            versatz = (t - now).total_seconds() / (duration_h * 3600)
            return position_on_track(tp, profil, prog_jetzt + versatz)
        quelle = f"GPX-Track ({len(tp)} Punkte)"
    else:
        try:
            s_lat, s_lon = float(start_lat), float(start_lon)
            p_lat, p_lon = float(peak_lat), float(peak_lon)
        except (TypeError, ValueError):
            punkte[0]['quelle'] = "nur Standpunkt"
            return punkte

        def route_pos(t):
            prog = max(0.0, min(1.0, (t - start_dt).total_seconds() / (duration_h * 3600)))
            return interpolate_position(s_lat, s_lon, p_lat, p_lon, prog)
        quelle = "Luftlinie Start-Gipfel"

    punkte[0]['quelle'] = quelle
    ref_lat, ref_lon = route_pos(now)

    for m in range(TRAJECTORY_STEP_MIN, TRAJECTORY_HORIZON_MIN + 1, TRAJECTORY_STEP_MIN):
        t = now + timedelta(minutes=m)
        if t > end_dt + timedelta(minutes=20):
            break
        r_lat, r_lon = route_pos(t)
        punkte.append({
            't': t,
            'lat': cur_lat + (r_lat - ref_lat),
            'lon': cur_lon + (r_lon - ref_lon),
            'offset': m
        })
    return punkte


def fetch_nowcast_points(punkte):
    """Beliebig viele Punkte in EINEM Request - die GeoSphere-Zeitreihe nimmt
    mehrere lat_lon-Paare entgegen."""
    if not punkte:
        return None
    query = "&".join(f"lat_lon={p['lat']:.5f},{p['lon']:.5f}" for p in punkte)
    url = (f"{GEOSPHERE_BASE}/timeseries/forecast/nowcast-v1-15min-1km"
           f"?{query}&parameters=rr&forecast_offset=0&output_format=geojson")
    return http_json(url, timeout=20)


def value_at_time(times, vals, ziel_dt, toleranz_min=12):
    """Niederschlagswert zum passenden Zeitschritt - nicht der erste Treffer
    irgendwo in der Reihe."""
    if not times or not vals:
        return None
    n = min(len(times), len(vals))
    bester, best_diff = None, None
    for i in range(n):
        t = parse_iso_utc(times[i])
        if not t:
            continue
        diff = abs((t - ziel_dt).total_seconds()) / 60
        if best_diff is None or diff < best_diff:
            best_diff, bester = diff, i
    if bester is None or best_diff > toleranz_min:
        return None
    try:
        return float(vals[bester] or 0)
    except (TypeError, ValueError):
        return 0.0


def analyze_trajectory(punkte):
    """Erster Zeitpunkt, zu dem die Person tatsächlich in den Niederschlag gerät."""
    payload = fetch_nowcast_points(punkte)
    if not payload:
        return None
    features = payload.get('features') or []
    if not features:
        return None

    basis_times = gs_timestamps(payload)
    treffer = []

    for idx, p in enumerate(punkte):
        feat = features[idx] if idx < len(features) else {}
        times = basis_times or gs_timestamps(payload, feat)
        vals = gs_values(feat, 'rr')
        if not times or not vals:
            continue

        rr = value_at_time(times, vals, p['t'])
        if rr is None or rr < RAIN_THRESHOLD:
            continue

        # Einzelner Ausreißer reicht nicht: Der Folgeschritt muss bestätigen,
        # sonst löst jedes Rauschen im Raster eine Warnung aus.
        rr_next = value_at_time(times, vals, p['t'] + timedelta(minutes=TRAJECTORY_STEP_MIN))
        bestaetigt = (rr_next is not None and rr_next >= RAIN_THRESHOLD) or rr >= 0.5
        treffer.append({'punkt': p, 'rr': rr, 'bestaetigt': bestaetigt})

    if not treffer:
        return None

    fest = [t for t in treffer if t['bestaetigt']] or treffer
    erster = min(fest, key=lambda t: t['punkt']['offset'])
    p = erster['punkt']
    intensitaet = max(t['rr'] for t in treffer if t['punkt']['offset'] <= p['offset'] + 60)

    return {
        'offset_min': p['offset'],
        'time': p['t'].isoformat(),
        'amount': intensitaet,
        'lat': p['lat'],
        'lon': p['lon'],
        'unterwegs': p['offset'] > 0,
        'bestaetigt': erster['bestaetigt']
    }


def analyze_advanced_front(lat, lon, trajektorie=None):
    """Rückfallebene: Trajektorie als primäre Quelle, Feldanalyse für Richtung
    und Zuggeschwindigkeit."""
    traj = None
    if trajektorie and len(trajektorie) > 1:
        try:
            traj = analyze_trajectory(trajektorie)
        except Exception as e:
            print(f"    Trajektorien-Nowcast fehlgeschlagen: {e}")

    feld = None
    try:
        feld = analyze_precip_raster(lat, lon)
    except Exception as e:
        print(f"    Grid-Nowcast fehlgeschlagen, Fallback aktiv: {e}")
    if feld is None:
        try:
            feld = analyze_advanced_front_legacy(lat, lon)
        except Exception as e:
            print(f"    Legacy-Nowcast fehlgeschlagen: {e}")

    if traj is None:
        return feld

    eta = traj['offset_min']
    ergebnis = {
        "stage": "arrival" if eta <= 10 else stage_from_eta(eta),
        "time": traj['time'],
        "amount": traj['amount'],
        "distance_km": feld.get("distance_km", 0) if feld else 0,
        "direction": feld.get("direction") if feld else None,
        "speed": feld.get("speed") if feld else None,
        "quelle": "trajektorie",
        "unterwegs": traj['unterwegs'],
        "offset_min": eta
    }

    if feld:
        f_dt = parse_iso_utc(feld.get("time"))
        t_dt = parse_iso_utc(traj['time'])
        if f_dt and t_dt and f_dt < t_dt - timedelta(minutes=20):
            feld['quelle'] = 'feld'
            feld['unterwegs'] = False
            return feld

    return ergebnis


# ==============================================================================
#  ROUTENABSCHNITTE  --  Warnungen für Gebiete statt für Punkte
# ------------------------------------------------------------------------------
#  Warum das der bessere Bezugsrahmen ist, kurz nachgerechnet:
#
#    GPS-Genauigkeit          ~30 m
#    Nowcast-Rasterzelle     1000 m      -> GPS ist 33x feiner als die Daten
#
#  Die Präzision der Position ist also Scheingenauigkeit: Sie beschreibt etwas,
#  das die Wetterdaten gar nicht auflösen können. Umgekehrt bewegt man sich in
#  einem 15-Minuten-Zyklus nur 500-1250 m, also unter einer Rasterzelle.
#
#  Ohne GPS entsteht Unsicherheit allein aus der Abweichung vom Zeitplan:
#  20 Minuten Verzug entsprechen etwa 1 km, also einer Rasterzelle. Ein Abschnitt
#  von 3-5 km Länge fängt das vollständig ab.
#
#  Konsequenz: "Im Gipfelbereich zieht ab 07:20 Nebel auf" ist belastbarer als
#  "an deiner Position in 34 Minuten" - und braucht keinen Live-Standort.
# ==============================================================================
SEGMENT_NAMES = {
    3: ["Zustieg", "Mittelteil", "Gipfelbereich"],
    4: ["Zustieg", "Unterer Abschnitt", "Oberer Abschnitt", "Gipfelbereich"],
}
SEGMENT_SAMPLES = 3       # Stützpunkte je Abschnitt für die Nowcast-Abfrage


def build_segments(track_raw, start_lat, start_lon, peak_lat, peak_lon, anzahl=3):
    """Route in benannte Abschnitte zerlegen, jeweils mit Höhenband und
    Stützpunkten für die Wetterabfrage."""
    tp = parse_track(track_raw)
    profil = track_effort_profile(tp) if tp else None

    if not profil:
        try:
            s_lat, s_lon = float(start_lat), float(start_lon)
            p_lat, p_lon = float(peak_lat), float(peak_lon)
        except (TypeError, ValueError):
            return []
        tp = [(s_lat, s_lon, None), (p_lat, p_lon, None)]
        profil = [0.0, 1.0]

    namen = SEGMENT_NAMES.get(anzahl, [f"Abschnitt {i+1}" for i in range(anzahl)])
    segmente = []

    for i in range(anzahl):
        von, bis = i / anzahl, (i + 1) / anzahl
        punkte = []
        for s in range(SEGMENT_SAMPLES):
            f = von + (bis - von) * ((s + 0.5) / SEGMENT_SAMPLES)
            punkte.append(position_on_track(tp, profil, f))

        # Höhenband des Abschnitts, damit die Warnung konkret wird
        hoehen = []
        for idx, pr in enumerate(profil):
            if von <= pr <= bis and tp[idx][2] is not None:
                hoehen.append(tp[idx][2])
        segmente.append({
            'name': namen[i] if i < len(namen) else f"Abschnitt {i+1}",
            'von': von, 'bis': bis,
            'punkte': punkte,
            'ele_min': int(min(hoehen)) if hoehen else None,
            'ele_max': int(max(hoehen)) if hoehen else None,
        })
    return segmente


def segment_label(seg):
    if seg.get('ele_min') is not None and seg.get('ele_max') is not None \
            and seg['ele_max'] - seg['ele_min'] > 80:
        return f"{seg['name']} ({seg['ele_min']}–{seg['ele_max']} m)"
    if seg.get('ele_max') is not None:
        return f"{seg['name']} (ca. {seg['ele_max']} m)"
    return seg['name']


def segment_time_window(seg, start_dt, duration_h):
    """Wann ist die Person voraussichtlich in diesem Abschnitt?"""
    return (start_dt + timedelta(hours=duration_h * seg['von']),
            start_dt + timedelta(hours=duration_h * seg['bis']))


def analyze_segments(segmente, now):
    """Für jeden Abschnitt: Wann setzt dort Niederschlag ein und wie stark?
    Alle Abschnitte in EINEM Request - bei 3 Abschnitten sind das 9 Punkte."""
    if not segmente:
        return {}

    alle = []
    for si, seg in enumerate(segmente):
        for p in seg['punkte']:
            alle.append({'seg': si, 'lat': p[0], 'lon': p[1]})

    payload = fetch_nowcast_points(alle)
    if not payload:
        return {}
    features = payload.get('features') or []
    basis_times = gs_timestamps(payload)

    ergebnis = {}
    for idx, eintrag in enumerate(alle):
        feat = features[idx] if idx < len(features) else {}
        times = basis_times or gs_timestamps(payload, feat)
        vals = gs_values(feat, 'rr')
        if not times or not vals:
            continue
        onset = find_onset(times, vals)
        if not onset:
            continue
        o_dt = parse_iso_utc(onset['time'])
        if not o_dt:
            continue

        si = eintrag['seg']
        vorhanden = ergebnis.get(si)
        # Je Abschnitt zählt der früheste Einsatz und die höchste Intensität.
        if vorhanden is None or o_dt < vorhanden['dt']:
            ergebnis[si] = {'dt': o_dt, 'amount': onset['amount']}
        elif onset['amount'] > vorhanden['amount']:
            vorhanden['amount'] = onset['amount']

    return ergebnis


def fetch_hourly_multi(punkte, date_str, end_date_str):
    """Stundenwerte fuer mehrere Punkte in EINEM Request. Bei drei Abschnitten
    spart das zwei Aufrufe gegenueber der Einzelabfrage."""
    if not punkte:
        return []
    lats = ",".join(f"{p[0]:.5f}" for p in punkte)
    lons = ",".join(f"{p[1]:.5f}" for p in punkte)
    url = (f"{OPEN_METEO_BASE}?latitude={lats}&longitude={lons}&hourly={HOURLY_VARS}"
           f"&start_date={date_str}&end_date={end_date_str}&timezone=auto&wind_speed_unit=kmh")
    hoehen = [p[2] for p in punkte]
    if any(h for h in hoehen):
        url += "&elevation=" + ",".join(str(int(h)) if h else "nan" for h in hoehen)
    res = http_json(url, timeout=15)
    if not res:
        return []
    liste = res if isinstance(res, list) else [res]
    out = []
    for eintrag in liste:
        h = (eintrag or {}).get('hourly') or {}
        for key in list(h.keys()):
            if key.endswith('_best_match'):
                h[key.replace('_best_match', '')] = h[key]
        out.append(h)
    return out


def analyze_segment_hazards(segmente, start_dt, duration_h, tour_types):
    """Je Abschnitt die Gefahrenlage im eigenen Zeitfenster bewerten.

    Verwendet dieselbe Bewertung wie die Risikoanalyse in der App, damit Ampel
    und Push-Meldung nicht auseinanderlaufen: ab 60 Punkten rot, ab 30 orange.
    Erfasst damit auch Gewitter, Sturmboeen und Vereisung - nicht nur Regen und
    Nebel, die vorher die einzigen Ausloeser waren."""
    if not segmente:
        return {}

    mitten = []
    for seg in segmente:
        m = seg['punkte'][len(seg['punkte']) // 2]
        mitten.append((m[0], m[1], seg.get('ele_max')))

    von_ges, _ = segment_time_window(segmente[0], start_dt, duration_h)
    _, bis_ges = segment_time_window(segmente[-1], start_dt, duration_h)
    hourlies = fetch_hourly_multi(
        mitten,
        (von_ges - timedelta(hours=2)).strftime('%Y-%m-%d'),
        (bis_ges + timedelta(hours=2)).strftime('%Y-%m-%d'))

    ergebnis = {}
    for si, seg in enumerate(segmente):
        if si >= len(hourlies):
            continue
        h = hourlies[si]
        times = h.get('time') or []
        if not times:
            continue
        von_dt, bis_dt = segment_time_window(seg, start_dt, duration_h)

        bester = None
        for i, t_str in enumerate(times):
            try:
                t_dt = LOCAL_TZ.localize(datetime.fromisoformat(t_str))
            except Exception:
                continue
            if not (von_dt - timedelta(minutes=30) <= t_dt <= bis_dt + timedelta(minutes=30)):
                continue

            temp = safe_num(h.get('temperature_2m'), i)
            rain = safe_num(h.get('precipitation'), i)
            snow = safe_num(h.get('snowfall'), i)
            gust = safe_num(h.get('wind_gusts_10m'), i)
            cape = safe_num(h.get('cape'), i)
            code = int(safe_num(h.get('weather_code'), i))
            prob = safe_num(h.get('precipitation_probability'), i)
            vis = safe_num(h.get('visibility'), i)
            dew = safe_num(h.get('dew_point_2m'), i, temp - 3)
            wolken = safe_num(h.get('cloud_cover'), i)

            nass = any(safe_num(h.get('precipitation'), i - p) > 0.1
                       for p in range(1, 4) if i - p >= 0)
            score, gruende = score_risk_advanced(rain, snow, gust, cape, code, prob, temp,
                                                 vis, tour_types, nass, True)

            basis = None
            if seg.get('ele_max') is not None:
                basis = seg['ele_max'] + max(0, 125 * (temp - dew))
                if wolken > 60 and basis <= seg['ele_max'] + 60:
                    score += 25
                    gruende = list(gruende) + ['Abschnitt liegt in der Wolke']

            if bester is None or score > bester['score']:
                bester = {'score': min(100, score), 'gruende': gruende, 'zeit': t_dt,
                          'gust': gust, 'rain': rain, 'vis': vis,
                          'basis': int(basis) if basis else None,
                          'thunder': code in (95, 96, 99) or (cape >= 1000 and prob >= 30)}

        if bester and bester['score'] >= 30:
            ergebnis[si] = bester
    return ergebnis


# ==============================================================================
# 8. Open-Meteo Abruf
# ==============================================================================

def format_zeit(dt):
    return dt.astimezone(LOCAL_TZ).strftime('%H:%M')


def build_segment_alert(segmente, niederschlag, gefahren, start_dt, duration_h,
                        now_utc, tour_types=None):
    """Baut die Warnung aus Abschnitt + Zeitfenster. Rueckgabe: (key, titel, text, stufe)

    PRIORISIERUNG NACH SCHWERE, NICHT NACH ZEIT
    -------------------------------------------
    Vorher wurde streng nach Eintrittszeit sortiert - das Naheliegendste zuerst.
    Fuer eine Tourentscheidung ist das falsch herum: Ein Gewitter am Klettersteig
    in 2,5 Stunden muss VOR dem Nieselregen in 25 Minuten gemeldet werden, weil
    nur die lange Vorwarnzeit einen geordneten Rueckzug erlaubt. Wer erst 25
    Minuten vorher erfaehrt, dass am Grat ein Gewitter steht, hat keinen Ausweg
    mehr.

    Deshalb: dieselbe Schwelle wie die Ampel in der App.
      ab 60 Punkten (rot)    -> sofort melden, volle Restdauer der Tour als Horizont
      ab 30 Punkten (orange) -> gestaffelt nach Naeherkommen wie bisher
    Innerhalb einer Stufe entscheidet weiterhin die Zeit."""
    if tour_types is None:
        tour_types = []
    kandidaten = []

    for si, seg in enumerate(segmente):
        von_dt, bis_dt = segment_time_window(seg, start_dt, duration_h)
        # Abschnitt bereits durchschritten -> nicht mehr relevant
        if bis_dt.astimezone(timezone.utc) < now_utc - timedelta(minutes=10):
            continue

        # --- Schwere Gefahren aus der Prognose (Gewitter, Sturm, Vereisung) ---
        gef = gefahren.get(si)
        if gef:
            g_dt = gef['zeit'].astimezone(timezone.utc)
            vorlauf = (g_dt - now_utc).total_seconds() / 60
            rot = gef['score'] >= 60
            # Rot bekommt die volle Restdauer als Horizont, orange nur 4 Stunden.
            horizont = (bis_dt.astimezone(timezone.utc) - now_utc).total_seconds() / 60 if rot else 240
            if -30 <= vorlauf <= max(60, horizont):
                kandidaten.append({
                    'art': 'gefahr', 'prio': 0 if rot else 1, 'seg': si, 'dt': g_dt,
                    'score': gef['score'], 'gruende': gef['gruende'],
                    'thunder': gef['thunder'], 'gust': gef['gust'],
                    'vorlauf': vorlauf, 'seg_obj': seg
                })

        # --- Niederschlagsbeginn aus dem Nowcast (kurzfristig, praezise) -------
        n = niederschlag.get(si)
        if n:
            treffer = (von_dt - timedelta(minutes=45)).astimezone(timezone.utc) <= n['dt'] \
                      <= (bis_dt + timedelta(minutes=45)).astimezone(timezone.utc)
            vorlauf = (n['dt'] - now_utc).total_seconds() / 60
            if treffer and -15 <= vorlauf <= 240:
                kandidaten.append({
                    'art': 'niederschlag', 'prio': 1, 'seg': si, 'dt': n['dt'],
                    'amount': n['amount'], 'vorlauf': vorlauf, 'seg_obj': seg
                })

    if not kandidaten:
        return None

    # Erst nach Schwere, dann nach Zeit.
    kandidaten.sort(key=lambda k: (k['prio'], k['dt']))
    k = kandidaten[0]
    seg = k['seg_obj']
    label = segment_label(seg)
    zeit = format_zeit(k['dt'])
    vorlauf = int(k['vorlauf'])

    if vorlauf <= 0:
        wann = "jetzt"
    elif vorlauf < 60:
        wann = f"ab ca. {zeit} (in {vorlauf} Min.)"
    else:
        # FIX: Das replace lief vorher ueber den ganzen String und machte aus
        # "ab ca. 08:30" ein "ab ca, 08:30". Nur die Dezimalstelle umstellen.
        std = f"{vorlauf / 60.0:.1f}".replace('.', ',')
        wann = f"ab ca. {zeit} (in {std} h)"

    if k['art'] == 'gefahr':
        rot = k['prio'] == 0
        grund = ", ".join(k['gruende'][:2]) if k['gruende'] else "kritische Bedingungen"
        stufe = 'critical' if rot else 'update_mid'
        key = f"seg{k['seg']}|gefahr|{'rot' if rot else 'orange'}|{int(max(0, vorlauf) // 60)}"

        if rot:
            titel = f"Kritisch im {seg['name']} – {wann}"
            # Keine Handlungsempfehlung. Die App liefert Ort, Zeitpunkt und Art der
            # Gefahr - die Entscheidung ueber die Tour trifft ausschliesslich die
            # Person davor, die Gelaende, Ausruestung und eigene Verfassung kennt.
            rest = int(max(0, vorlauf))
            vorwarnzeit = (f"Vorwarnzeit ca. {rest} Min." if rest < 90
                           else f"Vorwarnzeit ca. {rest // 60} Std.")
            titel_grund = "Gewitter" if k['thunder'] else grund
            text = f"{label}: {titel_grund} {wann}.\n{vorwarnzeit}"
        else:
            titel = f"{grund} im {seg['name']} – {wann}"
            text = f"{label}: {grund}. Erwartet {wann}."
    else:
        menge = k['amount']
        art = "Nieselregen" if menge < 0.2 else ("Regen" if menge < 2.0 else "kraeftiger Regen")
        stufe = "update_close" if vorlauf <= STAGE_ETA_CLOSE_MIN else (
            "update_mid" if vorlauf <= STAGE_ETA_MID_MIN else "early_warning")
        key = (f"seg{k['seg']}|regen|"
               f"{'stark' if menge >= 2 else ('leicht' if menge >= 0.2 else 'niesel')}|"
               f"{int(max(0, vorlauf) // 30)}")
        titel = f"{art.capitalize()} im {seg['name']} – {wann}"
        text = f"Auf deiner Route: {label}. {art.capitalize()} {wann}, ca. {menge:.1f} mm/h."

    return key, titel, text, stufe


POST_WINDOW_HOURS = 3


def check_post_window(lat, lon, end_dt, tour_types=None, peak_lat=None, peak_lon=None):
    """Wie entwickelt sich das Wetter in den Stunden NACH dem geplanten Ende?

    Die eingetragene Tourdauer ist eine Schätzung, keine Zusage. Wer sich um zwei
    Stunden verschätzt oder länger am Gipfel bleibt, ist genau dann unterwegs,
    wenn die Bewertung schon aufgehört hat hinzusehen. Deshalb wird das Fenster
    danach mitgeprüft - als reine Zeitangabe, ohne Bewertung, ob das zu spät ist.

    Rückgabe: (zeitpunkt, gründe, score) oder None."""
    if tour_types is None:
        tour_types = []
    try:
        bis_dt = end_dt + timedelta(hours=POST_WINDOW_HOURS)
        h = fetch_hourly(lat, lon,
                         end_dt.strftime('%Y-%m-%d'),
                         (bis_dt + timedelta(hours=1)).strftime('%Y-%m-%d'))
        times = h.get('time') or []
        if not times:
            return None

        ph = {}
        if peak_lat and peak_lon:
            ph = fetch_hourly(peak_lat, peak_lon,
                              end_dt.strftime('%Y-%m-%d'),
                              (bis_dt + timedelta(hours=1)).strftime('%Y-%m-%d'))
        peak_idx = {t: i for i, t in enumerate(ph.get('time') or [])}

        for i, t_str in enumerate(times):
            try:
                t_dt = LOCAL_TZ.localize(datetime.fromisoformat(t_str))
            except Exception:
                continue
            if not (end_dt < t_dt <= bis_dt):
                continue

            def bewerte(quelle, idx):
                nass = any(safe_num(quelle.get('precipitation'), idx - p) > 0.1
                           for p in range(1, 4) if idx - p >= 0)
                return score_risk_advanced(
                    safe_num(quelle.get('precipitation'), idx),
                    safe_num(quelle.get('snowfall'), idx),
                    safe_num(quelle.get('wind_gusts_10m'), idx),
                    safe_num(quelle.get('cape'), idx),
                    int(safe_num(quelle.get('weather_code'), idx)),
                    safe_num(quelle.get('precipitation_probability'), idx),
                    safe_num(quelle.get('temperature_2m'), idx),
                    safe_num(quelle.get('visibility'), idx),
                    tour_types, nass, True)

            score, gruende = bewerte(h, i)
            p_idx = peak_idx.get(t_str)
            if p_idx is not None:
                p_score, p_gruende = bewerte(ph, p_idx)
                if p_score > score:
                    score, gruende = p_score, p_gruende

            if score >= 30:
                return t_dt, gruende, score
    except Exception as e:
        print(f"Nachlauf-Pruefung fehlgeschlagen: {e}")
    return None


def format_post_window(treffer, end_dt):
    """Sachliche Zeile zum Nachlauf. Nennt Zeitpunkt und Abstand zum geplanten
    Ende - die Bewertung, ob der eigene Zeitpuffer reicht, bleibt beim Nutzer."""
    if not treffer:
        return ""
    t_dt, gruende, score = treffer
    delta_h = (t_dt - end_dt).total_seconds() / 3600
    # Nur die Dezimalstelle umstellen - ein replace ueber den ganzen String
    # machte aus "1.5 Std." ein "1,5 Std," mit Komma statt Punkt am Ende.
    abstand = (f"{int(delta_h * 60)} Min." if delta_h < 1
               else f"{delta_h:.1f}".replace('.', ',') + " Std.")
    grund = ", ".join(gruende[:2]) if gruende else "verschlechterte Bedingungen"
    stufe = "Hohes Risiko" if score >= 60 else "Anspruchsvolle Bedingungen"
    return (f"Nach dem geplanten Ende ({end_dt:%H:%M} Uhr): "
            f"{stufe} ab {t_dt:%H:%M} Uhr – {grund}. "
            f"Das sind {abstand} Puffer.")


# ==============================================================================
#  VOR-TOUR-BRIEFINGS
# ------------------------------------------------------------------------------
#  Zwei Meldungen, die eine Tourentscheidung tatsächlich beeinflussen können:
#  am Vorabend (noch absagbar) und kurz vor dem Start (noch umkehrbar).
#  Beide gehen genau EINMAL raus. Am Vorabend zählt nicht der Absolutwert,
#  sondern die VERÄNDERUNG gegenüber dem, was beim Abonnieren galt - "unverändert
#  gut" ist keine Nachricht wert, "schlechter geworden" schon.
# ==============================================================================
BRIEFING_EVENING_HOUR = 19        # Ortszeit am Vorabend
BRIEFING_PRESTART_MIN = 10        # Minuten vor Tourbeginn

RISK_RANK = {'stable': 0, 'moderate': 1, 'warning': 2, 'danger': 3, 'unknown': 0}
RISK_WORT = {'stable': 'gut', 'moderate': 'brauchbar',
             'warning': 'anspruchsvoll', 'danger': 'gefährlich', 'unknown': 'unklar'}


def _briefing_text(tour_title, msg, nachlauf=""):
    return "\n\n".join(t for t in (tour_title, msg, nachlauf) if t)


def build_evening_briefing(status, msg, vorher, tour_title, start_dt, nachlauf=""):
    """Vorabend-Meldung. Gibt (titel, text) zurück oder None, wenn nichts zu sagen ist."""
    rang = RISK_RANK.get(status, 0)
    rang_vorher = RISK_RANK.get(vorher, 0) if vorher else None
    wann = start_dt.strftime('%H:%M')

    if rang >= 3:
        return (f"Morgen {wann}: Bedingungen gefährlich",
                _briefing_text(tour_title, msg, nachlauf))
    if rang == 2:
        return (f"Morgen {wann}: anspruchsvolle Bedingungen",
                _briefing_text(tour_title, msg, nachlauf))

    # Bei guter Lage nur melden, wenn sie sich verbessert hat oder erstmals bewertet
    # wird. Ein "alles wie gestern" braucht niemand aufs Handy.
    if rang_vorher is not None and rang_vorher >= 2 and rang <= 1:
        return (f"Morgen {wann}: Wetter hat sich gebessert",
                _briefing_text(tour_title, msg, nachlauf))
    if rang_vorher is None:
        return (f"Morgen {wann}: Bedingungen sehen {RISK_WORT.get(status, 'brauchbar')} aus",
                _briefing_text(tour_title, msg, nachlauf))
    # Unveraendert gute Lage IM Tourfenster - aber danach wird es schlechter.
    # Das ist genau die Konstellation, die man sonst uebersieht: Die Bewertung
    # endet mit der geplanten Dauer, die Tour womoeglich nicht.
    if nachlauf:
        return (f"Morgen {wann}: Bedingungen gut, aber knapper Zeitrahmen",
                _briefing_text(tour_title, msg, nachlauf))
    return None


def build_prestart_briefing(status, msg, tour_title, aktuell_txt, nachlauf=""):
    wort = RISK_WORT.get(status, 'brauchbar')
    if RISK_RANK.get(status, 0) >= 3:
        titel = "Start in 10 Min. – Bedingungen gefährlich"
    elif RISK_RANK.get(status, 0) == 2:
        titel = "Start in 10 Min. – anspruchsvolle Bedingungen"
    else:
        titel = f"Start in 10 Min. – Bedingungen {wort}"
    return titel, "\n\n".join(t for t in (tour_title, aktuell_txt, msg, nachlauf) if t)



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
            return "danger", f"Hohes alpines Risiko im Tourfenster: {reasons_txt}."
        if max_risk >= 30:
            return "warning", f"Anspruchsvolle Bedingungen im Tourfenster: {reasons_txt}."
        if max_risk >= 15:
            return "moderate", f"Trend: leicht unbeständig ({reasons_txt})."

    except Exception as e:
        print(f"Fehler beim Trend-Check: {e}")
        return "unknown", "Trend konnte gerade nicht berechnet werden."

    return "stable", "Die Bedingungen für deine Tour sind aktuell stabil."


# ==============================================================================
# 9b. Alarm-Zustandsmaschine  --  gegen Push-Spam
# ------------------------------------------------------------------------------
# URSACHE DES SPAMS: Der Vergleich lief über den Anzeigetitel, und in dem stand die
# aktuelle Uhrzeit:
#     alert_title = f"Letzte Warnung (ca. 3 km) [{local_time_str}]"
#     send = alert_title != last_alert_title
# Damit war der Titel bei JEDEM Lauf ein anderer String, die Dublettenprüfung konnte
# nie greifen, und bei einem 5-Minuten-Trigger kam alle 5 Minuten dieselbe Meldung.
#
# Neu: Ein stabiler Schlüssel ohne Zeitstempel entscheidet über das Senden, der
# Anzeigetext darf weiter die Uhrzeit enthalten. Zusätzlich pro Stufe eine Sperrfrist
# und die Regel, dass eine gleich- oder niedrigerrangige Meldung nicht wiederholt wird.
# ==============================================================================
STAGE_RANK = {
    'stable': 0, 'improving': 1, 'early_warning': 2,
    'update_mid': 3, 'update_close': 4, 'arrival': 5,
    # Rote Lage (Gewitter, Sturm, Vereisung im Abschnitt). Steht bewusst ueber
    # 'arrival': Ein Gewitter am Grat wiegt schwerer als einsetzender Regen.
    'critical': 6
}

# Mindestabstand zwischen zwei Meldungen derselben Stufe.
STAGE_COOLDOWN_S = {
    'early_warning': 90 * 60,
    'update_mid': 45 * 60,
    'update_close': 20 * 60,
    'arrival': 60 * 60,
    'improving': 45 * 60,
    # Kuerzer als der Rest: Bei roter Lage ist eine Wiederholung zumutbar,
    # eine verpasste Meldung dagegen nicht.
    'critical': 30 * 60,
}

# Eine echte Verschärfung (höhere Stufe) darf diese Sperrfrist abkürzen - sonst käme
# die entscheidende "Regen ist da"-Meldung erst nach Ablauf der frühen Sperrfrist.
ESCALATION_MIN_GAP_S = 5 * 60


def build_alert_key(stage, amount, direction, eta_minutes):
    """Stabiler Vergleichsschlüssel OHNE Uhrzeit. Die ETA wird grob gerastert, damit
    normale Prognoseschwankungen (147 -> 143 Min.) keine neue Meldung auslösen."""
    if stage in ('improving', 'stable'):
        return stage
    intensity = 'niesel' if amount < 0.2 else ('leicht' if amount < 2.0 else 'stark')
    eta_bucket = int(max(0, eta_minutes) // 30)   # 30-Minuten-Raster
    return f"{stage}|{intensity}|{direction or '-'}|{eta_bucket}"


def should_send_alert(stage, key, last_key, last_state, last_ts, now_utc):
    """Entscheidet, ob die Meldung raus darf. Gibt (bool, Begründung) zurück -
    die Begründung landet im Log, damit unterdrückte Meldungen nachvollziehbar sind."""
    if not key:
        return False, "kein Alarm"
    if key == last_key:
        return False, f"unverändert ({key})"

    rank_now = STAGE_RANK.get(stage, 0)
    rank_last = STAGE_RANK.get(last_state, 0)
    seit = (now_utc - last_ts).total_seconds() if last_ts else None

    if seit is None:
        return True, "erste Meldung"

    # Eine rote Lage darf nie an einer Sperrfrist haengen bleiben. Der einzige
    # Schutz ist die Schluesselgleichheit weiter oben - identische Lage wird
    # weiterhin nicht wiederholt.
    if stage == 'critical' and seit >= ESCALATION_MIN_GAP_S:
        return True, f"kritische Lage ({int(seit / 60)} Min. seit letzter Meldung)"

    if rank_now > rank_last:
        if seit >= ESCALATION_MIN_GAP_S:
            return True, f"Verschärfung {last_state} -> {stage}"
        return False, f"Verschärfung, aber erst {int(seit / 60)} Min. seit letzter Meldung"

    cooldown = STAGE_COOLDOWN_S.get(stage, 45 * 60)
    if seit >= cooldown:
        return True, f"Sperrfrist abgelaufen ({int(seit / 60)} Min.)"
    return False, f"Sperrfrist läuft ({int(seit / 60)}/{cooldown // 60} Min., Stufe {stage})"


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
        alter = nowcast_run_age_min(grid)
        print(f"    Rasterzellen: {len(gfeats)}, Zeitstempel: {len(gtimes)}")
        if alter is not None:
            print(f"    Alter des Nowcast-Laufs: {alter:.0f} Min.")
            print(f"    Erwartete Gesamtlatenz bis zum Handy: bis ca. "
                  f"{PIPELINE_LATENCY_MIN} Min. (Rechenzyklus + Trigger + Zustellung)")
        usable = 0
        for f in gfeats:
            f_lat, f_lon = gs_point(f)
            if f_lat is not None and gs_values(f, 'rr'):
                usable += 1
        print(f"    Auswertbare Zellen (Koordinate + Werte): {usable}")
        if gfeats and usable == 0:
            print("    WARNUNG: Zellen vorhanden, aber keine auswertbar - Fallback greift.")

    # --- 3. Frontenerkennung end-to-end ---------------------------------------
    print("\n[3] Trajektorien-Nowcast")
    jetzt = datetime.now(LOCAL_TZ)
    test_traj = build_trajectory(
        jetzt, jetzt, jetzt + timedelta(hours=6), 6.0,
        TEST_LAT, TEST_LON, TEST_LAT, TEST_LON, TEST_LAT + 0.05, TEST_LON + 0.05)
    print(f"    Trajektorienpunkte: {len(test_traj)} "
          f"(0 bis {test_traj[-1]['offset']} Min. voraus)")
    traj = analyze_trajectory(test_traj)
    if traj:
        print(f"    Treffer nach {traj['offset_min']} Min. bei "
              f"{traj['lat']:.4f},{traj['lon']:.4f} mit {traj['amount']:.2f} mm")
        print(f"    {'auf dem weiteren Weg' if traj['unterwegs'] else 'am Standort'}")
    else:
        print("    Kein Niederschlag auf der Trajektorie. Bei trockenem Wetter korrekt.")

    print("\n[3b] Frühwarnung end-to-end (Trajektorie + Feldanalyse)")
    front = analyze_advanced_front(TEST_LAT, TEST_LON, test_traj)
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
            'geplant': 0, 'beendet': 0, 'fronten': 0, 'pushes': 0, 'fehler': 0}
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
            last_alert_key = tour.get('last_alert_key', '')
            last_alert_ts = parse_iso_utc(tour.get('last_alert_ts'))
            last_state = tour.get('last_weather_state', 'unknown')
            last_hourly_check = tour.get('last_hourly_check')

            if tour.get('finished'):
                continue
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

            # ---- Phase bestimmen ------------------------------------------
            if now > end_dt:
                # FIX: Beendete Touren blieben unbegrenzt in der Sammlung stehen und
                # wurden in der App weiter als "aktiv" angezeigt. Sie werden jetzt
                # einmalig abgeschlossen - das räumt auch die App-Anzeige auf.
                if not tour.get('finished'):
                    print(f"  {doc.id}: Tour beendet ({end_dt:%d.%m. %H:%M}) - wird abgeschlossen.")
                    try:
                        db.collection('tour_subscriptions').document(doc.id).update({
                            'finished': True,
                            'finished_at': now_utc.isoformat(),
                            'last_weather_state': 'ended'
                        })
                        stat['beendet'] += 1
                    except Exception as fe:
                        print(f"    Abschluss fehlgeschlagen: {fe}")
                continue

            if now < start_dt:
                # ---- Vor dem Start: Vorabend- und Kurz-davor-Briefing -------
                stat['geplant'] += 1
                vorlauf_min = (start_dt - now).total_seconds() / 60

                # Vorabend: am Kalendertag vor dem Start, ab BRIEFING_EVENING_HOUR
                vorabend_faellig = (
                    not tour.get('briefing_evening_sent')
                    and now.hour >= BRIEFING_EVENING_HOUR
                    and 0 < vorlauf_min <= 24 * 60
                    and now.date() < start_dt.date()
                )
                prestart_faellig = (
                    not tour.get('briefing_prestart_sent')
                    and 0 < vorlauf_min <= BRIEFING_PRESTART_MIN + 5
                )

                if not (vorabend_faellig or prestart_faellig):
                    continue

                trend_status, trend_msg = check_forecast_trend(
                    lat, lon, start_dt, duration, tour_types, peak_lat, peak_lon)

                # Liegen gerade keine Daten vor, waere "Bedingungen sehen unklar aus"
                # keine Information - und wuerde den Einmal-Schuss verbrauchen. Dann
                # lieber nichts senden und beim naechsten Lauf erneut versuchen.
                if trend_status == 'unknown':
                    print(f"  {doc.id}: Briefing verschoben (noch keine Prognosedaten)")
                    continue

                # Die eingetragene Dauer ist eine Schaetzung. Deshalb auch die
                # Stunden danach ansehen - wer laenger braucht, ist genau dann
                # unterwegs, wenn die Bewertung sonst schon aufgehoert hat.
                nachlauf = format_post_window(
                    check_post_window(lat, lon, end_dt, tour_types, peak_lat, peak_lon),
                    end_dt)
                if nachlauf:
                    print(f"    Nachlauf: {nachlauf}")

                titel = ""

                if prestart_faellig:
                    aktuell = fetch_current_condition(lat, lon)
                    titel, text = build_prestart_briefing(
                        trend_status, trend_msg, tour.get('tourTitle', 'Deine Tour'),
                        f"Jetzt am Start: {aktuell}" if aktuell != "Keine Daten" else "",
                        nachlauf)
                    feld = 'briefing_prestart_sent'
                else:
                    gebaut = build_evening_briefing(
                        trend_status, trend_msg, tour.get('briefing_baseline'),
                        tour.get('tourTitle', 'Deine Tour'), start_dt, nachlauf)
                    if not gebaut:
                        # Lage unverändert gut - dann ist Schweigen die richtige Meldung.
                        print(f"  {doc.id}: Vorabend-Briefing entfällt (Lage unverändert: {trend_status})")
                        db.collection('tour_subscriptions').document(doc.id).update({
                            'briefing_evening_sent': True,
                            'briefing_baseline': trend_status
                        })
                        continue
                    titel, text = gebaut
                    feld = 'briefing_evening_sent'

                try:
                    send_high_priority_push(f"{titel}", text, token, tag='tour-briefing')
                    print(f"[Briefing] {feld}: {titel}")
                    stat['pushes'] += 1
                    db.collection('tour_subscriptions').document(doc.id).update({
                        feld: True, 'briefing_baseline': trend_status
                    })
                except Exception as fe:
                    print(f"[Briefing] Push-Fehler: {fe}")
                    if is_dead_token(fe):
                        db.collection('tour_subscriptions').document(doc.id).update({'token': None})
                continue

            # ---- Ab hier: Tour läuft --------------------------------------

            stat['aktiv'] += 1

            # --- Live-GPS vs. Routen-Interpolation -----------------------------
            updated_at = tour.get('updatedAt')
            gps_acc = tour.get('gpsAccuracy')
            is_gps_stale = True
            gps_alter_min = None

            if isinstance(updated_at, datetime):
                ua = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
                gps_alter_min = (now_utc - ua.astimezone(timezone.utc)).total_seconds() / 60
                if gps_alter_min < 30:
                    is_gps_stale = False

            # Ein Fix mit mehreren hundert Metern Streuung stammt aus der Funkzellen-
            # Ortung. Damit an einer Position zu warnen, an der man gar nicht ist,
            # wäre schlechter als ehrlich auf die Route zurückzufallen.
            if gps_acc is not None and float(gps_acc) > 300:
                is_gps_stale = True

            track_raw = tour.get('track')
            quelle = "GPS"

            if is_gps_stale:
                progress = max(0.0, min(1.0, (now - start_dt).total_seconds() / (duration * 3600)))
                tp = parse_track(track_raw)
                profil = track_effort_profile(tp) if tp else None
                if profil:
                    # Mit Track auf dem echten Weg verorten statt auf der Luftlinie.
                    lat, lon = position_on_track(tp, profil, progress)
                    quelle = f"Track ({progress * 100:.0f}%)"
                elif peak_lat and peak_lon and start_lat and start_lon:
                    lat, lon = interpolate_position(float(start_lat), float(start_lon),
                                                   float(peak_lat), float(peak_lon), progress)
                    quelle = f"Luftlinie ({progress * 100:.0f}%)"
            else:
                quelle = f"GPS ({gps_alter_min:.0f} Min. alt)"

            # --- Nowcast / Frontenerkennung ------------------------------------
            # --- Frühwarnung: zuerst abschnittsbezogen -------------------------
            # Ein Abschnitt von 3-5 km Länge ist gegenüber Positionsfehlern robust
            # und passt zur Auflösung der Daten (1 km Raster, 15 Minuten Takt).
            # Die punktbezogene Trajektorie bleibt als Ergänzung erhalten, falls
            # kein Abschnitt greift.
            segmente = build_segments(track_raw, start_lat, start_lon, peak_lat, peak_lon)
            seg_alert = None
            if segmente:
                try:
                    seg_regen = analyze_segments(segmente, now)
                    seg_gefahren = analyze_segment_hazards(segmente, start_dt, duration, tour_types)
                    seg_alert = build_segment_alert(segmente, seg_regen, seg_gefahren,
                                                    start_dt, duration, now_utc, tour_types)
                except Exception as e:
                    print(f"    Abschnittsanalyse fehlgeschlagen: {e}")

            trajektorie = build_trajectory(
                now, start_dt, end_dt, duration, lat, lon,
                start_lat, start_lon, peak_lat, peak_lon, track_raw)

            front = analyze_advanced_front(lat, lon, trajektorie)

            # Relevanzfilter: Eine Vorwarnung auf schwachen Nieselregen in zweieinhalb
            # Stunden ist keine Information, sondern Rauschen - und Rauschen sorgt
            # dafür, dass die wirklich wichtige Meldung mit weggewischt wird.
            if front and front.get('stage') != 'arrival':
                arr = parse_iso_utc(front["time"])
                mins = (arr - now_utc).total_seconds() / 60 if arr else 0
                amt = front.get("amount", 0)
                if amt < 0.2 and mins > 60:
                    print(f"  {doc.id}: Nieselregen erst in {int(mins)} Min. - keine Meldung.")
                    front = None
                elif mins > 180:
                    front = None

            if front:
                stat['fronten'] += 1
            if front:
                q = front.get('quelle', 'feld')
                wo = "auf dem weiteren Weg" if front.get('unterwegs') else "am Standort"
                nowcast_txt = (f"{front['stage']} ({q}, {wo}, "
                               f"in {front.get('offset_min', 0)} Min., {front.get('amount', 0):.2f} mm)")
            else:
                nowcast_txt = "kein Niederschlag"
            seg_txt = f"{len(segmente)} Abschnitte" if segmente else "keine Abschnitte"
            if seg_alert:
                seg_txt += f" -> {seg_alert[1]}"
            traj_quelle = trajektorie[0].get('quelle', 'nur Standpunkt') if trajektorie else '-'
            print(f"  {doc.id}: {lat:.4f},{lon:.4f} via {quelle} | Route: {traj_quelle} "
                  f"| {seg_txt} | Nowcast: {nowcast_txt}")
            current_stage = "stable"
            alert_title = ""
            alert_body = ""
            alert_key = ""

            if seg_alert:
                # Abschnittswarnung hat Vorrang: Sie benennt Ort UND Zeit und ist
                # nicht davon abhaengig, wie genau die Position gerade bekannt ist.
                alert_key, alert_title, alert_body, current_stage = seg_alert
                alert_title = f"{alert_title} [{local_time_str}]"
            elif front:
                arr_dt = parse_iso_utc(front["time"])
                mins_left = max(0, int((arr_dt - now_utc).total_seconds() / 60)) if arr_dt else 0
                dir_txt = f" aus {direction_name(front['direction'])}" if front.get("direction") else ""
                speed_txt = f" (ca. {int(front.get('speed', 30))} km/h)" if front.get("speed") else ""

                # Zeitangabe lesbar: ab einer Stunde in Stunden statt "in ca. 147 Min."
                if mins_left <= 0:
                    time_txt = "unmittelbar"
                elif mins_left < 60:
                    time_txt = f"in ca. {mins_left} Min."
                else:
                    # Nur die Dezimalstelle umstellen - ein replace ueber den ganzen
                    # String machte aus "in ca. 1.8 h" ein "in ca, 1,8 h".
                    std = f"{mins_left / 60.0:.1f}".replace('.', ',')
                    time_txt = f"in ca. {std} h"

                stage = front["stage"]
                amount = front.get("amount", 0)
                rain_desc = get_rain_description(amount)
                dist_km = int(round(front.get("distance_km", 0)))
                unterwegs = bool(front.get("unterwegs"))
                current_stage = stage
                # Ob man hineinläuft oder es zu einem kommt, ist eine andere Lage -
                # daher Teil des Schlüssels, damit der Wechsel eine Meldung auslöst.
                alert_key = build_alert_key(stage, amount, front.get('direction'), mins_left) \
                    + ("|weg" if unterwegs else "")

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
                    incoming = ("Nieselregen" if amount < 0.2
                                else ("Leichter Regen" if amount < 2.0 else f"Regen ({rain_desc})"))

                    if unterwegs:
                        # Das ist die Lage, die der alte, ortsfeste Ansatz gar nicht
                        # erkennen konnte: Am Standort bleibt es trocken, aber auf dem
                        # weiteren Weg läuft man in die Zelle hinein.
                        alert_title = f"{incoming} auf deinem Weg – {time_txt} [{local_time_str}]"
                        alert_body = (f"Auf deiner Route liegt {time_txt} Niederschlag. "
                                      f"Am jetzigen Standort bleibt es zunächst trocken – "
                                      f"du läufst darauf zu.")
                        if front.get("direction"):
                            alert_body += f" Zellzug{dir_txt}{speed_txt}."
                    elif stage == "update_close":
                        alert_title = f"{incoming} {time_txt} [{local_time_str}]"
                        alert_body = f"Niederschlag steht unmittelbar bevor{dir_txt}, noch ca. {dist_km} km entfernt{speed_txt}."
                    elif stage == "update_mid":
                        alert_title = f"{incoming} {time_txt} [{local_time_str}]"
                        alert_body = f"Zieht{dir_txt} heran, aktuell ca. {dist_km} km entfernt{speed_txt}."
                    else:
                        alert_title = f"Niederschlag im Anmarsch – {time_txt} [{local_time_str}]"
                        alert_body = f"{incoming}{dir_txt}, aktuell ca. {dist_km} km entfernt{speed_txt}."
            else:
                if last_state in ['danger', 'worsening', 'early_warning', 'update_mid',
                                  'update_close', 'arrival']:
                    current_stage = "improving"
                    alert_key = "improving"
                    trend_status, _ = check_forecast_trend(lat, lon, start_dt, duration,
                                                          tour_types, peak_lat, peak_lon)
                    if trend_status == "stable":
                        alert_title = f"Niederschlag löst sich auf [{local_time_str}]"
                        alert_body = "Der Regen stoppt und die Prognose zeigt keinen weiteren Niederschlag."
                    else:
                        alert_title = f"Vorübergehende Regenpause [{local_time_str}]"
                        alert_body = "Die Zelle ist abgezogen, das Wetter bleibt laut Prognose unbeständig."

            update_data = {'last_weather_state': current_stage}

            send_hazard_alert, grund = should_send_alert(
                current_stage, alert_key, last_alert_key, last_state, last_alert_ts, now_utc)

            if alert_key and not send_hazard_alert:
                # Unterdrückte Meldungen werden protokolliert, sonst wäre nicht
                # nachvollziehbar, warum eine erwartete Warnung ausblieb.
                print(f"    unterdrückt: {grund}")

            if send_hazard_alert:
                try:
                    response = send_high_priority_push(alert_title, alert_body, token)
                    print(f"[Gefahr] Push gesendet ({grund}): {alert_title}")
                    stat['pushes'] += 1
                    update_data['last_alert_key'] = alert_key
                    update_data['last_alert_ts'] = now_utc.isoformat()
                    update_data['last_alert_title'] = alert_title   # nur noch für die Anzeige
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
                # Kam eben erst eine Gefahrenmeldung, wird das Routine-Update
                # verschoben. Zwei Benachrichtigungen im selben Moment sind der
                # sicherste Weg, dass beide zusammen weggewischt werden.
                if send_hazard_alert:
                    print("    stündliches Update verschoben (Gefahrenmeldung ging gerade raus)")
                else:
                    multi_loc_status = build_multi_location_update({
                        'start_lat': start_lat, 'start_lon': start_lon,
                        'lat': lat, 'lon': lon,
                        'peak_lat': peak_lat, 'peak_lon': peak_lon
                    })
                    trend_status, trend_msg = check_forecast_trend(lat, lon, start_dt, duration,
                                                                  tour_types, peak_lat, peak_lon)
                    hourly_title = f"Wetter-Update [{local_time_str}]"
                    hourly_body = "\n\n".join([p for p in [multi_loc_status, trend_msg] if p])

                    try:
                        # Eigener Tag: Routine-Updates ersetzen sich gegenseitig und
                        # überschreiben keine Gefahrenwarnung.
                        send_high_priority_push(hourly_title, hourly_body, token, tag='tour-update')
                        print("[Stündlich] Push gesendet.")
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

    print(f"Lauf beendet: {stat['gesamt']} Abos, {stat['aktiv']} laufend, "
          f"{stat['geplant']} geplant, {stat['beendet']} abgeschlossen, "
          f"{stat['ohne_token']} ohne Token, {stat['zeit_kaputt']} mit defekter Startzeit, "
          f"{stat['fronten']} Nowcast-Treffer, {stat['pushes']} Pushes, {stat['fehler']} Fehler.")
    if stat['gesamt'] == 0:
        print("Hinweis: Keine Abos vorhanden. Der Lauf hatte nichts zu tun - das sagt "
              "nichts darueber aus, ob die Wetterabfrage funktioniert. Dafuer: --selftest")


if __name__ == "__main__":
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
    check_all_tours()
