// Never cache authenticated pages, API responses, login URLs or session state.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let data = {title:'Łap termin',body:'Otwórz aplikację, aby sprawdzić aktualności.'};
  try { data = {...data, ...event.data.json()}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title, {body:data.body,tag:data.tag || 'update',icon:'/static/icon-192.png',badge:'/static/icon-192.png',data:{url:'/'}}));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(async clients => {
    for (const client of clients) if (new URL(client.url).origin === self.location.origin) { await client.navigate('/'); return client.focus(); }
    return self.clients.openWindow('/');
  }));
});
