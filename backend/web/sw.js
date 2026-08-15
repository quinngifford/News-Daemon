/* Service worker: receives Web Push and raises the notification.
 *
 * Must be served from the root so its scope covers the whole app — a service
 * worker can only control paths at or below its own URL.
 *
 * Deliberately has no fetch handler and no caching. An offline cache would risk
 * serving a stale dashboard during the one event this system exists for.
 */
'use strict';

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch (err) {
    d = { title: 'news-ticker', body: event.data ? event.data.text() : '' };
  }

  const confirmed = d.state === 'confirmed';
  const options = {
    body: d.body || '',
    // Same tag across states means a RETRACTED alert REPLACES the CONFIRMED one
    // it corrects, rather than stacking beside it and leaving both on screen.
    tag: d.tag || 'ticker',
    renotify: true,
    requireInteraction: d.requireInteraction !== false,
    // Vibration is the one attention-getter a browser can request directly.
    vibrate: confirmed ? [300, 120, 300, 120, 600] : [200, 100, 200],
    timestamp: Date.now(),
    data: { url: d.url || '/' },
  };

  event.waitUntil(
    self.registration.showNotification(d.title || 'news-ticker', options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({
      type: 'window', includeUncontrolled: true,
    });
    for (const c of all) {
      if ('focus' in c) {
        await c.focus();
        if (target && target !== '/' && 'navigate' in c) {
          try { await c.navigate(target); } catch (e) { /* cross-origin */ }
        }
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
