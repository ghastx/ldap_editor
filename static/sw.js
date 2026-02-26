// sw.js - Service Worker per Rubrica LDAP (PWA)
//
// Strategia di caching:
//   - Cache-first per asset statici (CSS, icone, favicon)
//   - Tutte le altre richieste (navigazione, API, SSE) passano
//     direttamente alla rete senza intercettazione del SW
//
// NOTA: il SW NON intercetta la navigazione per evitare che le
// fetch() interne si accumulino durante click rapidi nella PWA
// standalone, causando saturazione delle connessioni.
//
// Rubrica LDAP - Copyright (C) 2024 - GPL-2.0-or-later

var CACHE_NAME = 'rubrica-ldap-v4';

// Asset statici da precaricare
var PRECACHE_URLS = [
    '/static/style.css',
    '/static/favicon.ico',
    '/static/favicon-32x32.png',
    '/static/icon-192x192.png',
    '/static/icon-512x512.png'
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

// --- Fetch: solo cache-first per asset statici ---

self.addEventListener('fetch', function(event) {
    var url = new URL(event.request.url);

    // Intercetta SOLO gli asset statici (/static/*) con GET
    // Tutto il resto (navigazione, API, SSE, POST) passa direttamente alla rete
    if (event.request.method === 'GET' && url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(function(cached) {
                if (cached) {
                    return cached;
                }
                return fetch(event.request).then(function(response) {
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function(cache) {
                            cache.put(event.request, clone);
                        });
                    }
                    return response;
                });
            })
        );
    }
});
