// sw.js - Service Worker per Rubrica LDAP (PWA)
//
// Strategia di caching:
//   - Cache-first per asset statici (CSS, icone, favicon)
//   - Network-first per pagine HTML (dati LDAP dinamici)
//   - Bypass completo per SSE (/api/events) e richieste POST
//   - Fallback offline quando la rete non e' disponibile
//
// Rubrica LDAP - Copyright (C) 2024 - GPL-2.0-or-later

var CACHE_NAME = 'rubrica-ldap-v2';

// Asset statici da precaricare (app shell)
var PRECACHE_URLS = [
    '/static/style.css',
    '/static/favicon.ico',
    '/static/favicon-32x32.png',
    '/static/icon-192x192.png',
    '/static/icon-512x512.png',
    '/offline'
];

// --- Install: precache degli asset statici ---

self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.addAll(PRECACHE_URLS);
        }).then(function() {
            return self.skipWaiting();
        })
    );
});

// --- Activate: pulizia delle cache vecchie ---

self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(cacheNames) {
            return Promise.all(
                cacheNames.filter(function(name) {
                    return name !== CACHE_NAME;
                }).map(function(name) {
                    return caches.delete(name);
                })
            );
        }).then(function() {
            return self.clients.claim();
        })
    );
});

// --- Fetch: strategia differenziata per tipo di risorsa ---

self.addEventListener('fetch', function(event) {
    var request = event.request;
    var url = new URL(request.url);

    // 1. NON intercettare SSE (text/event-stream) - cruciale per il monitor PBX
    if (url.pathname === '/api/events') {
        return;
    }

    // 2. NON intercettare richieste POST (form submit, click-to-dial API)
    if (request.method !== 'GET') {
        return;
    }

    // 3. Asset statici (/static/*): cache-first con fallback rete
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(request).then(function(cached) {
                if (cached) {
                    return cached;
                }
                return fetch(request).then(function(response) {
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function(cache) {
                            cache.put(request, clone);
                        });
                    }
                    return response;
                });
            })
        );
        return;
    }

    // 4. API GET (/api/*): network-only (dati sempre freschi)
    if (url.pathname.startsWith('/api/')) {
        return;
    }

    // 5. Pagine HTML (navigate): network-only con fallback offline
    //    Non si cachano le pagine HTML perche' i dati LDAP sono sempre dinamici.
    //    Il SW serve solo a mostrare la pagina offline in caso di rete assente.
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request).catch(function() {
                return caches.match('/offline');
            })
        );
        return;
    }
});
