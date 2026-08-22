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

def check_all_tours():
    now = datetime.now(timezone.utc)
    subscriptions_ref = db.collection('tour_subscriptions')
    docs = subscriptions_ref.stream()

    for doc in docs:
        tour = doc.to_dict()
        token = tour.get('token')
        
        if not token:
            continue

        try:
            # TEST-MODUS: Erzwingt sofort einen Test-Push zum Prüfen
            message = messaging.Message(
                notification=messaging.Notification(
                    title="⚡ Test-Alarm: Push funktioniert!",
                    body="Das Backend und Firebase senden erfolgreich Benachrichtigungen an dein Smartphone."
                ),
                token=token
            )
            messaging.send(message)
            print(f"Test-Push erfolgreich gesendet an Token: {token[:10]}...")
            
            # Wir machen hier nach dem ersten Token direkt Schluss, damit es nur ein Test-Push wird
            break

        except Exception as e:
            print(f"Fehler beim Test-Push: {e}")

if __name__ == "__main__":
    check_all_tours()
    
