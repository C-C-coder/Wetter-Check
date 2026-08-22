import os
import requests
from datetime import datetime, timezone
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

        if not token or not lat or not lon or not start_time_str:
            continue

        try:
            start_dt = datetime.fromisoformat(start_time_str).replace(tzinfo=timezone.utc)
            end_dt = datetime.fromtimestamp(start_dt.timestamp() + duration * 3600, tzinfo=timezone.utc)

            if start_dt <= now <= end_dt:
                url = f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nowcast-v1-15min-1km?lat_lon={lat:.5f},{lon:.5f}&parameters=rr&forecast_offset=0&output_format=geojson"
                response = requests.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    features = data.get('features', [])
                    if features:
                        precip = features[0].get('properties', {}).get('parameters', {}).get('rr', {}).get('data', [])
                        if any(val >= 0.2 for val in precip if val is not None):
                            message = messaging.Message(
                                notification=messaging.Notification(
                                    title="⚠️ Wetterwarnung für deine Tour!",
                                    body="Aktuelle Radarsignale zeigen Regen/Gewitter an deinem Standort."
                                ),
                                token=token
                            )
                            messaging.send(message)
                            print(f"Push gesendet an Token: {token[:10]}...")
        except Exception as e:
            print(f"Fehler bei Tour {doc.id}: {e}")

if __name__ == "__main__":
    check_all_tours()
  
