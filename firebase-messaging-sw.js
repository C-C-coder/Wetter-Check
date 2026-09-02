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

// 1. Hintergrund-Handler (Greift nun verlässlich für Benachrichtigungen aus dem Python-Backend)
messaging.onBackgroundMessage((payload) => {
   // Holt Titel und Body flexibel aus dem Notification- oder Data-Objekt
   const title = payload.notification?.title || payload.data?.title || "Alpine Wetterwarnung";
   const options = {
      body: payload.notification?.body || payload.data?.body || "",
      icon: 'logo.png',
      vibrate: [200, 100, 200, 100, 400], // Starkes Vibrationsmuster für die Alpen
      requireInteraction: true, // Bleibt so lange auf dem Screen, bis du es aktiv wegwischst
      data: payload.data || {}
   };
   
   // Das 'return' hält den Service Worker aktiv, bis die Benachrichtigung grafisch dargestellt wurde
   return self.registration.showNotification(title, options);
});

// 2. Klick-Handler (Öffnet oder fokussiert die App beim Antippen der Nachricht)
self.addEventListener('notificationclick', (event) => {
event.notification.close(); // Schließt die Benachrichtigung in der Statusleiste

const clickUrl = event.notification.data?.click_url || './index.html#activeTour';

event.waitUntil(
  clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
    // Prüfen, ob die App bereits in einem Browser-Tab geöffnet ist
    for (let i = 0; i < clientList.length; i++) {
      let client = clientList[i];
      if (client.url.includes('wetter-check') && 'focus' in client) {
        client.postMessage({ type: 'PUSH_CLICK', url: clickUrl });
        return client.focus();
      }
    }
    // Falls die App komplett geschlossen ist -> neu im Tab öffnen
    if (clients.openWindow) {
      return clients.openWindow(clickUrl);
    }
  })
);
});