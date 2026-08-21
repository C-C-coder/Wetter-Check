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
