/* Avisos do CRM IG Assessoria: recebe as notificações mesmo com o CRM fechado. Não guarda páginas em cache. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { titulo: 'CRM IG Assessoria', corpo: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.titulo || 'CRM IG Assessoria', {
    body: d.corpo || '', icon: 'icone-192.png', badge: 'icone-badge.png', tag: d.tag || undefined, renotify: !!d.tag,
    data: { url: d.url || '/' }
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  // aviso de fora do CRM (fila de abordagem): abre direto, sem passar pela tela do CRM
  if (new URL(url).origin !== self.location.origin) { e.waitUntil(self.clients.openWindow(url)); return; }
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((ws) => {
    for (const w of ws) {
      if (new URL(w.url).origin === self.location.origin) { w.postMessage({ tipo: 'abrir', url }); return w.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
