const CACHE = "apl-reload-disabled-v11";
const ASSETS = ["/", "/index.html", "/apl-reload-logo.png", "/manifest.webmanifest", "/redesign.css", "/premium.css", "/dashboard.css", "/dashboard-extra.css", "/dashboard.js", "/website-admin.css"];

self.addEventListener("install", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(Promise.all([caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))),self.registration.unregister()]));
});
