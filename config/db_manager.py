import os
import psycopg2
from psycopg2.extras import RealDictCursor
import json
from dotenv import load_dotenv

# Este módulo se importa a veces solo (scripts sueltos, sin pasar por main.py),
# así que carga su propio .env en vez de depender de que main.py ya lo haya
# hecho. Los defaults son los valores de desarrollo local de siempre — en
# producción (otra PC), el .env real los pisa con la contraseña/puerto reales.
load_dotenv()

def obtener_conexion():
    """Establece conexión con PostgreSQL.

    En Vercel, la integración nativa de Neon inyecta DATABASE_URL (más las
    variables POSTGRES_* "legacy" con otros nombres: POSTGRES_DATABASE en vez
    de POSTGRES_DB, sin POSTGRES_PORT), así que se usa DATABASE_URL cuando
    está presente. Si no, se arma la conexión con POSTGRES_HOST/DB/USER/
    PASSWORD/PORT (con POSTGRES_DATABASE como alias de POSTGRES_DB), con los
    valores de desarrollo local como respaldo si no hay .env."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        database=os.getenv("POSTGRES_DB") or os.getenv("POSTGRES_DATABASE", "sociaboss"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "123"),
        port=os.getenv("POSTGRES_PORT", "5433"),
    )

def inicializar_base_datos():
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    
    print("Estructurando tablas en PostgreSQL (Puerto 5433)...")
    
    # 1. Tabla de Tiendas (Catálogo)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tiendas (
            id SERIAL PRIMARY KEY,
            nombre VARCHAR(100) UNIQUE NOT NULL
        )
    """)
    # Bodega de Odoo asignada a cada tienda (su código de barras de
    # ubicación, ej. "INV-LD") — el Conteo de Inventario la usa para cargar
    # SIEMPRE esa misma bodega, sin que nadie tenga que escribirla a mano.
    cursor.execute("ALTER TABLE tiendas ADD COLUMN IF NOT EXISTS bodega_odoo VARCHAR(50)")
    
    # 2. TABLA: Sesiones de Cierre Diario 
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cierres_diarios (
            id SERIAL PRIMARY KEY,
            fecha DATE NOT NULL,
            tienda_id INTEGER NOT NULL,
            drive_resumen_caja TEXT,
            drive_cierre_lote TEXT,
            drive_deposito TEXT,
            usuario_registro VARCHAR(150),
            completado INTEGER DEFAULT 0,
            observaciones_cajero TEXT,
            FOREIGN KEY (tienda_id) REFERENCES tiendas(id),
            UNIQUE(fecha, tienda_id)
        )
    """)
    
    # 3. Tabla para Evidencias individuales adjuntas a Órdenes específicas de Odoo
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS evidencias_ordenes (
            id SERIAL PRIMARY KEY,
            fecha DATE NOT NULL,
            orden_id VARCHAR(100) UNIQUE NOT NULL,
            tienda_id INTEGER NOT NULL,
            id_google_drive TEXT NOT NULL,
            FOREIGN KEY (tienda_id) REFERENCES tiendas(id)
        )
    """)
    
    # 4. Tabla opcional para guardar históricos de totales consolidados por día
    #    Su existencia para (fecha, tienda_id) también funciona como el marcador
    #    de "reporte diario ya enviado" que bloquea/oculta esa tienda+fecha en Monitoreo.
    # ajuste_metodos_pago: JSON [{metodo, monto}] que el usuario confirma/corrige
    # en la pantalla previa a "Generar e Iniciar Reporte Diario". Si existe, es la
    # fuente de verdad de los montos por método de pago para el Reporte de Cuadre
    # (en vez de recalcularlos sumando venta_pagos por orden).
    # conteo_fisico: JSON [{metodo, monto}] con lo que se contó físicamente en
    # Cierre de Caja — se compara contra ajuste_metodos_pago para la Diferencia.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consolidado_ventas_diarias (
            id SERIAL PRIMARY KEY,
            fecha DATE NOT NULL,
            tienda_id INTEGER NOT NULL,
            total_odoo DOUBLE PRECISION NOT NULL,
            cantidad_ordenes INTEGER NOT NULL,
            ajuste_metodos_pago TEXT,
            conteo_fisico TEXT,
            FOREIGN KEY (tienda_id) REFERENCES tiendas(id),
            UNIQUE(fecha, tienda_id)
        )
    """)

    # 5. Usuarios de la aplicación: cada uno pertenece a una tienda (o ninguna si es admin)
    #    intentos_fallidos/bloqueado_hasta: bloqueo temporal tras varios logins
    #    fallidos seguidos (fuerza bruta).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            nombre VARCHAR(150) NOT NULL,
            email VARCHAR(150) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            rol VARCHAR(20) NOT NULL DEFAULT 'usuario' CHECK (rol IN ('admin','usuario','superadmin')),
            tienda_id INTEGER REFERENCES tiendas(id),
            activo BOOLEAN NOT NULL DEFAULT TRUE,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            intentos_fallidos INTEGER NOT NULL DEFAULT 0,
            bloqueado_hasta TIMESTAMP
        )
    """)

    # 6. Sesiones activas (login por cookie)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sesiones (
            token VARCHAR(64) PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            expira_en TIMESTAMP NOT NULL
        )
    """)

    # 7. Registro interno de cada venta al momento de "enviar el reporte diario"
    #    (productos y quién facturó, aunque no se muestre en la UI). También es
    #    la fuente de verdad para armar los "pedidos" de Cierre de Caja, en vez
    #    de depender de la caché del navegador (que ya quedó vacía a esa altura
    #    porque esa tienda+fecha se oculta de Monitoreo de Órdenes).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ventas_registradas (
            id SERIAL PRIMARY KEY,
            orden_id VARCHAR(150) UNIQUE NOT NULL,
            fecha DATE NOT NULL,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            total_venta DOUBLE PRECISION NOT NULL DEFAULT 0,
            tipo_pago VARCHAR(150),
            facturado_por VARCHAR(150),
            numero_factura VARCHAR(150),
            comprobante_url TEXT,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # 8. Productos de cada venta registrada (detalle línea por línea)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS venta_productos (
            id SERIAL PRIMARY KEY,
            venta_id INTEGER NOT NULL REFERENCES ventas_registradas(id) ON DELETE CASCADE,
            nombre_producto VARCHAR(300) NOT NULL,
            cantidad DOUBLE PRECISION NOT NULL DEFAULT 1,
            subtotal DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)

    # 8.1 Desglose de pagos de cada venta registrada (efectivo, tarjeta, etc.)
    #     — es lo que alimenta el Reporte de Cuadre de Caja.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS venta_pagos (
            id SERIAL PRIMARY KEY,
            venta_id INTEGER NOT NULL REFERENCES ventas_registradas(id) ON DELETE CASCADE,
            metodo VARCHAR(150) NOT NULL,
            monto DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)

    # 9. Meta de ventas mensual por tienda (la registra el admin a mano).
    #    El módulo de KPIs mide el avance de la tienda y el aporte de cada
    #    vendedora contra este mismo número.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metas_mensuales (
            id SERIAL PRIMARY KEY,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            anio INTEGER NOT NULL,
            mes INTEGER NOT NULL CHECK (mes BETWEEN 1 AND 12),
            monto_meta DOUBLE PRECISION NOT NULL DEFAULT 0,
            UNIQUE(tienda_id, anio, mes)
        )
    """)

    # 10. Base de apertura (fondo inicial de caja): se escribe a mano, una por
    #     tienda+fecha. Se ingresa desde Monitoreo de Órdenes y alimenta el
    #     Reporte de Cuadre de Caja (antes era un campo en blanco para llenar
    #     a mano en el papel impreso).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS apertura_caja_diaria (
            id SERIAL PRIMARY KEY,
            fecha DATE NOT NULL,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            monto DOUBLE PRECISION NOT NULL DEFAULT 0,
            registrado_por VARCHAR(150),
            actualizado_en TIMESTAMP DEFAULT NOW(),
            UNIQUE(fecha, tienda_id)
        )
    """)

    # 11. Registro de auditoría: quién hizo qué acción sensible y cuándo
    #     (aprobar/rechazar cierres, crear/editar usuarios, editar metas, etc.)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auditoria (
            id SERIAL PRIMARY KEY,
            usuario_id INTEGER REFERENCES usuarios(id),
            usuario_nombre VARCHAR(150),
            accion VARCHAR(100) NOT NULL,
            detalle TEXT,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # 12. Tokens de recuperación de contraseña ("olvidé mi contraseña"):
    #     de un solo uso, expiran a la hora de generados.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tokens_recuperacion (
            token VARCHAR(64) PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            expira_en TIMESTAMP NOT NULL,
            usado BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)

    # 13. Tareas: se asignan en Odoo (módulo Proyecto/Tareas), a la tienda
    #     (cada tienda tiene su propio usuario en Odoo). Acá solo se trackea
    #     el estado del lado de la tienda (pendiente/en progreso/completada)
    #     y la revisión del superadmin — nunca se escribe de vuelta a Odoo.
    #     odoo_task_id es único: la sincronización hace UPSERT por ese campo.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tareas (
            id SERIAL PRIMARY KEY,
            odoo_task_id INTEGER UNIQUE NOT NULL,
            titulo VARCHAR(300) NOT NULL,
            descripcion TEXT,
            tienda_id INTEGER REFERENCES tiendas(id),
            fecha_limite DATE,
            prioridad VARCHAR(20),
            estado VARCHAR(20) NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'en_progreso', 'completada')),
            comentario_usuario TEXT,
            revisado_por_admin BOOLEAN NOT NULL DEFAULT FALSE,
            comentario_admin TEXT,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            actualizado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # 14. Evidencia adjunta a una tarea (foto, PDF, etc.) — la sube la tienda
    #     al marcar avance, mismo patrón que evidencias_ordenes (Drive).
    #     Una tarea puede tener varias.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tareas_evidencias (
            id SERIAL PRIMARY KEY,
            tarea_id INTEGER NOT NULL REFERENCES tareas(id) ON DELETE CASCADE,
            nombre_archivo VARCHAR(300),
            id_google_drive TEXT,
            tipo_archivo VARCHAR(100),
            subido_por VARCHAR(150),
            creado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # 15. Conteo de Inventario (por código de barras) — portado de un Google
    #     Apps Script que usaba Google Sheets como base. Acá cada tabla
    #     reemplaza una hoja de ese script, PERO todo queda separado por
    #     tienda (el original era una sola ubicación activa global — con
    #     varias tiendas reales, la segunda que cargara una ubicación le
    #     borraba el conteo a la primera).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_ubicacion_activa (
            tienda_id INTEGER PRIMARY KEY REFERENCES tiendas(id),
            ubicacion VARCHAR(200) NOT NULL,
            actualizado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # location_id de Odoo de la bodega cargada — permite detectar solo, sin
    # tocar Odoo, si un superadmin reasignó la bodega de la tienda mientras
    # ya había una sesión de conteo activa (y recargar sola en ese caso).
    cursor.execute("ALTER TABLE inventario_ubicacion_activa ADD COLUMN IF NOT EXISTS location_id INTEGER")
    # Catálogo cargado desde Odoo para la ubicación activa de la tienda
    # (reemplaza la hoja "Productos_Odoo").
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_productos (
            id SERIAL PRIMARY KEY,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            producto_ref VARCHAR(150) NOT NULL,
            nombre VARCHAR(300),
            barcode VARCHAR(150),
            stock_sistema INTEGER NOT NULL DEFAULT 0,
            contado_real INTEGER NOT NULL DEFAULT 0,
            UNIQUE(tienda_id, producto_ref)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inv_productos_barcode ON inventario_productos(tienda_id, barcode)")
    # Códigos escaneados que no matchean ningún producto cargado (reemplaza
    # "Productos_Nuevos").
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_productos_nuevos (
            id SERIAL PRIMARY KEY,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            barcode VARCHAR(150) NOT NULL,
            cantidad INTEGER NOT NULL DEFAULT 0,
            actualizado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(tienda_id, barcode)
        )
    """)
    # Escaneos crudos pendientes de sumar, uno por fila — cada operario
    # inserta las suyas y Postgres ya las mantiene aisladas sin necesitar el
    # candado manual (CacheService) que hacía falta en el script original
    # cuando todos escribían en la misma hoja de cálculo.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_escaneos (
            id SERIAL PRIMARY KEY,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            sesion_id VARCHAR(150) NOT NULL,
            usuario_nombre VARCHAR(150),
            ubicacion VARCHAR(200) NOT NULL,
            codigo VARCHAR(150) NOT NULL,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inv_escaneos_sesion ON inventario_escaneos(tienda_id, sesion_id)")
    # Archivo histórico de escaneos ya sumados (reemplaza "Config_Archivo").
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_escaneos_archivo (
            id SERIAL PRIMARY KEY,
            tienda_id INTEGER NOT NULL REFERENCES tiendas(id),
            sesion_id VARCHAR(150),
            usuario_nombre VARCHAR(150),
            ubicacion VARCHAR(200),
            codigo VARCHAR(150),
            escaneado_en TIMESTAMP,
            archivado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inv_archivo_fecha ON inventario_escaneos_archivo(tienda_id, archivado_en)")

    # Conteo de Inventario: módulo aparte que solo funciona en las tiendas que
    # un superadmin activa explícitamente (interruptor manual, sin código).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventario_activaciones (
            tienda_id INTEGER PRIMARY KEY REFERENCES tiendas(id),
            activo BOOLEAN NOT NULL DEFAULT FALSE,
            activado_por VARCHAR(150),
            actualizado_en TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    conexion.commit()
    cursor.close()
    conexion.close()
    print("¡Base de datos estructurada con el nuevo módulo de cierres duales en PostgreSQL!")

# =====================================================================
# FUNCIONES AUXILIARES
# =====================================================================

def ejecutar_query(query, params=None):
    """Ejecuta inserts, updates o deletes y confirma los cambios"""
    conn = obtener_conexion()
    cursor = conn.cursor()
    try:
        if params:
            procesados = [json.dumps(p) if isinstance(p, (dict, list)) else p for p in params]
        else:
            procesados = None
            
        cursor.execute(query, procesados)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()

def consultar_datos(query, params=None):
    """Hace SELECT y devuelve la data estructurada en diccionarios"""
    conn = obtener_conexion()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(query, params)
        resultados = cursor.fetchall()
        return resultados
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    inicializar_base_datos()
