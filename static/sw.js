// sw.js - Service Worker per Rubrica LDAP (PWA)
//
// Cache-first SOLO per asset statici (/static/*).
// Navigazione, API, SSE e POST non vengono mai intercettati.
//
// In modalita' standalone PWA, la presenza di un fetch handler
// puo' impedire al browser di abortire correttamente le navigazioni
// precedenti durante click rapidi, saturando le connessioni HTTP.
// Per questo il fetch handler esce immediatamente (senza URL parsing)
// per qualsiasi richiesta non-statica.
//
// Rubrica LDAP - Copyright (C) 2024 - GPL-2.0-or-later

var CACHE_NAME = 'rubrica-ldap-v5';

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

// --- Fetch: cache-first solo per GET /static/* same-origin ---
//
// Ordine dei controlli (dal piu' veloce al piu' lento):
//   1. Navigazione   → return immediato (zero overhead sul SW thread)
//   2. Non-GET       → return immediato (POST, SSE, WebSocket)
//   3. Cross-origin   → return immediato
//   4. Non /static/  → return immediato (API, pagine HTML)
//   5. /static/*     → cache-first con fallback rete

self.addEventListener('fetch', function(event) {
    // 1. Mai intercettare navigazione: in standalone PWA le navigazioni
    //    non abortite si accumulano se passano per il SW
    if (event.request.mode === 'navigate') return;

    // 2. Solo GET (esclude POST, SSE stream, ecc.)
    if (event.request.method !== 'GET') return;

    // 3. Solo same-origin
    var url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;

    // 4. Solo asset statici
    if (!url.pathname.startsWith('/static/')) return;

    // 5. Cache-first per /static/*
    event.respondWith(
        caches.match(event.request).then(function(cached) {
            if (cached) return cached;
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
});
