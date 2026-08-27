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

// 1. Hintergrund-Handler
messaging.onBackgroundMessage((payload) => {
 // Wenn dein Python-Skript eine "notification" sendet, macht Firebase die Anzeige automatisch!
 // Wir greifen hier NUR ein, falls du jemals eine reine "data"-Nachricht vom Backend schickst.
 if (!payload.notification) {
    const title = payload.data?.title || "Alpine Wetterwarnung";
    const options = {
       body: payload.data?.body || "",
       icon: 'logo.png',
       vibrate: [200, 100, 200, 100, 400], // Starkes Vibrationsmuster
       requireInteraction: true // Bleibt auf dem Screen, bis man es wegwischt
    };
    
    // Das WICHTIGSTE: Das 'return' hält das Skript wach, bis die Nachricht steht!
    return self.registration.showNotification(title, options);
 }
});

// 2. Klick-Handler (Öffnet die App beim Antippen)
self.addEventListener('notificationclick', (event) => {
 event.notification.close(); // Schließt die Push-Benachrichtigung in der Statusleiste

 // Prüft, ob die App schon in einem Tab offen ist, und holt sie andernfalls nach vorne
 event.waitUntil(
   clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
     // Wenn die App im Hintergrund offen ist -> Fokus darauf setzen
     for (let i = 0; i < clientList.length; i++) {
       let client = clientList[i];
       if (client.url.includes('wetter-check') && 'focus' in client) {
         return client.focus();
       }
     }
     // Wenn die App komplett geschlossen ist -> neu öffnen
     if (clients.openWindow) {
       return clients.openWindow('/');
     }
   })
 );
});