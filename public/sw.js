// Service worker de SociaBoss — el objetivo NO es "app offline completa" (los
// datos de ventas/cierres siempre tienen que venir en vivo de Odoo/Postgres,
// cachear eso sería mostrar información financiera desactualizada sin avisar).
// El único objetivo es que el ícono instalado abra algo en vez de mostrar el
// error de "sin conexión" del navegador, y que recargar sea un poco más
// rápido. Estrategia: red primero siempre; si no hay red, se sirve la última
// copia del "shell" (HTML/íconos/manifest) que haya quedado en caché.
const CACHE_NAME = "sociaboss-shell-v1";
const SHELL_URLS = ["/", "/manifest.json", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(SHELL_URLS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((nombres) => Promise.all(nombres.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    if (event.request.method !== "GET") return;

    const url = new URL(event.request.url);
    // La API nunca se cachea: si no hay red, que falle normal — la app ya
    // maneja ese error en pantalla (mensaje "no se pudieron cargar...").
    if (url.pathname.startsWith("/api/")) return;

    event.respondWith(
        fetch(event.request)
            .then((respuesta) => {
                const copia = respuesta.clone();
                caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copia));
                return respuesta;
            })
            .catch(() => caches.match(event.request))
    );
});
