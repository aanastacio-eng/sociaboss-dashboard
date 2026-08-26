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
    """Establece conexión con PostgreSQL usando las variables de entorno
    (POSTGRES_HOST/DB/USER/PASSWORD/PORT), con los valores de desarrollo local
    como respaldo si no hay .env."""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        database=os.getenv("POSTGRES_DB", "sociaboss"),
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            nombre VARCHAR(150) NOT NULL,
            email VARCHAR(150) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            rol VARCHAR(20) NOT NULL DEFAULT 'usuario' CHECK (rol IN ('admin','usuario')),
            tienda_id INTEGER REFERENCES tiendas(id),
            activo BOOLEAN NOT NULL DEFAULT TRUE,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW()
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