importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js');

firebase.initializeApp({
apiKey: "AIzaSyADlL7BKaVPgdCH6xSqWEjVqf54q33E9WE",
authDomain: "wetter-check.firebaseapp.com",
projectId: "wetter-check",
storageBucket: "wetter-check.firebasestorage.app",
messagingSenderId: "184488061652",
appId: "1:184488061652:web:dda3d879ff8cfd36d6e941"
});

const messaging = firebase.messaging();

// 1. Hintergrund-Handler (das Backend sendet "data-only" - Titel/Text stecken in payload.data)
messaging.onBackgroundMessage((payload) => {
   const data = payload.data || {};
   const title = payload.notification?.title || data.title || "Alpine Wetterwarnung";
   const options = {
      body: payload.notification?.body || data.body || "",
      icon: 'logo.png',
      vibrate: [200, 100, 200, 100, 400], // Starkes Vibrationsmuster für die Alpen
      requireInteraction: true, // Bleibt so lange auf dem Screen, bis du es aktiv wegwischst
      data: data
   };
   // FIX: Das Backend schickt einen "tag" mit (tour-update, tour-alert,
   // tour-briefing, feedback), damit sich z.B. die stündlichen Updates gegenseitig
   // ERSETZEN statt sich zu stapeln. Der Tag wurde hier aber nie übergeben - mit
   // requireInteraction blieben dadurch alle Stunden-Updates einer Tour einzeln
   // liegen. renotify sorgt dafür, dass eine ersetzte Meldung trotzdem neu
   // vibriert/klingelt (wichtig bei Warnungen).
   if (data.tag) {
      options.tag = data.tag;
      options.renotify = true;
   }
   return self.registration.showNotification(title, options);
});

// 2. Klick-Handler (Öffnet oder fokussiert die passende Seite beim Antippen)
self.addEventListener('notificationclick', (event) => {
event.notification.close();

const clickUrl = event.notification.data?.click_url || './index.html#activeTour';
// Ziel relativ zum Service-Worker-Bereich auflösen (z.B. /Wetter-Check/index.html)
const target = new URL(clickUrl, self.registration.scope);
// "/Wetter-Check/" und "/Wetter-Check/index.html" sind dieselbe Seite
const seite = (u) => u.pathname.replace(/\/$/, '/index.html').toLowerCase();
const zielSeite = seite(target);

event.waitUntil(
  clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
    // FIX: Geprüft wurde bisher client.url.includes('wetter-check') - die echte
    // Adresse lautet aber ".../Wetter-Check/" (Groß-/Kleinschreibung!). Der
    // Vergleich schlug dadurch immer fehl: statt die offene App nach vorne zu holen,
    // ging jedes Mal ein neues Fenster auf. Jetzt wird die konkrete Zielseite
    // verglichen - so landet auch ein Feedback-Push in feedback-admin.html und
    // nicht in der bereits offenen Haupt-App.
    for (const client of clientList) {
      let clientUrl;
      try { clientUrl = new URL(client.url); } catch (e) { continue; }
      if (seite(clientUrl) === zielSeite && 'focus' in client) {
        client.postMessage({ type: 'PUSH_CLICK', url: target.href });
        return client.focus();
      }
    }
    if (clients.openWindow) {
      return clients.openWindow(target.href);
    }
  })
);
});
