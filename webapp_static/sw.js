// Service worker de Cultura Tejida — el objetivo NO es "app offline completa" (los
// datos de ventas/cierres siempre tienen que venir en vivo de Odoo/Postgres,
// cachear eso sería mostrar información financiera desactualizada sin avisar).
// El único objetivo es que el ícono instalado abra algo en vez de mostrar el
// error de "sin conexión" del navegador, y que recargar sea un poco más
// rápido. Estrategia: red primero siempre; si no hay red, se sirve la última
// copia del "shell" (HTML/íconos/manifest) que haya quedado en caché.
// Al renombrar el caché, el handler "activate" borra el anterior — es lo que
// hace que quien ya tenga la PWA instalada reciba la marca nueva en vez de
// seguir viendo el shell viejo servido desde su caché.
const CACHE_NAME = "cultura-tejida-shell-v1";
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

// ── Notificaciones push (tareas nuevas, inventario habilitado) ──
self.addEventListener("push", (event) => {
    let datos = { titulo: "Cultura Tejida", cuerpo: "" };
    try {
        if (event.data) datos = event.data.json();
    } catch (e) {
        if (event.data) datos.cuerpo = event.data.text();
    }
    event.waitUntil(
        self.registration.showNotification(datos.titulo || "Cultura Tejida", {
            body: datos.cuerpo || "",
            icon: "/icons/icon-192.png",
            badge: "/icons/icon-192.png",
            data: { url: datos.url || "/" },
        })
    );
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || "/";
    event.waitUntil(
        self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((lista) => {
            for (const cliente of lista) {
                if (cliente.url.includes(self.location.origin) && "focus" in cliente) {
                    cliente.navigate(url);
                    return cliente.focus();
                }
            }
            if (self.clients.openWindow) return self.clients.openWindow(url);
        })
    );
});
