import sys
# En Windows, cuando la salida estándar no es una consola UTF-8 (ej. corriendo
# como servicio, o con el log redirigido a un archivo), un print() con emoji
# (✅❌📸 etc.) lanza UnicodeEncodeError y hace crashear el request completo —
# incluso si la operación (ej. subir a Drive) ya había tenido éxito. Forzamos
# UTF-8 con reemplazo silencioso como red de seguridad.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os
import re
import shutil
import secrets
import calendar
import xmlrpc.client
import json
import bcrypt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from config.db_manager import obtener_conexion
from fastapi import Request

# Credenciales y config sensible SIEMPRE desde variables de entorno (archivo
# .env local, nunca versionado en git — ver .env.example para la plantilla).
# Antes estaban escritas directo en este archivo: si este código llega a
# subirse a un repositorio (aunque sea privado) o a otra PC, esas credenciales
# reales de Odoo quedarían expuestas dentro del código fuente.
load_dotenv()

def _env_requerida(nombre):
    valor = os.getenv(nombre)
    if not valor:
        raise RuntimeError(
            f"Falta la variable de entorno {nombre}. Copiá .env.example a .env "
            f"y completá los valores reales antes de correr el servidor."
        )
    return valor

app = FastAPI(title="SociaBoss Dashboard API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # El frontend se sirve desde este mismo servidor (mismo origen), así que
    # nunca necesita CORS cross-origin con credenciales. allow_origins="*" +
    # allow_credentials=True dejaría que CUALQUIER sitio externo hiciera
    # peticiones autenticadas usando la cookie de sesión de un usuario que
    # tenga la pestaña de SociaBoss abierta. Con credentials en False, un
    # navegador nunca envía la cookie en una petición cross-origin.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL = _env_requerida("ODOO_URL")
ODOO_DB = _env_requerida("ODOO_DB")
ODOO_USER = _env_requerida("ODOO_USER")
ODOO_PASSWORD = _env_requerida("ODOO_PASSWORD")
TARGET_COMPANY_ID = int(os.getenv("ODOO_COMPANY_ID", "2"))

# Mismas 8 tiendas que aparecen en los <select> del frontend. Se usa para que
# el módulo de Metas Mensuales siempre muestre las 8, aunque alguna todavía no
# tenga ninguna orden/usuario que la haya creado en la tabla `tiendas`.
TIENDAS_CONOCIDAS = [
    "Lemaler Dorado", "Lemaler Village", "Lemaler Ceibos", "Lemaler Entrerios",
    "Lemaler Quito", "Mariola Village", "Mariola Ceibos", "Lemaler Scala",
]

@app.get("/")
def index():
    ruta_html = os.path.join("public", "index.html")
    if os.path.exists(ruta_html):
        return FileResponse(ruta_html)
    return {"mensaje": "Servidor corriendo, pero falta el archivo public/index.html"}


def _normalizar_tienda(nombre):
    """Mismo criterio de comparación que usa el frontend (minúsculas, sin espacios),
    para que 'Lemaler Village' y 'lemaler village' se traten como la misma tienda."""
    return re.sub(r"\s+", "", (nombre or "").lower())

def _extension_archivo(upload):
    """Extensión real del archivo subido (imagen o PDF) a partir de su nombre,
    con fallback al content-type. Antes se guardaba siempre como .jpg sin
    importar el tipo real, lo cual corrompía los PDF subidos como comprobante."""
    nombre = (upload.filename or "") if upload else ""
    if "." in nombre:
        ext = nombre.rsplit(".", 1)[-1].lower().strip()
        if ext and ext.isalnum() and len(ext) <= 5:
            return ext
    if upload and upload.content_type == "application/pdf":
        return "pdf"
    return "jpg"

# Categorías fijas del Reporte de Cuadre, en este orden exacto. El nombre real
# del método de "efectivo" en Odoo varía por tienda (ej. "Caja Lemaler Quito"),
# así que se normaliza a "Caja" — todo lo demás se compara tal cual. "Giftcard"
# no existe como método de pago configurado en Odoo (no se puede cobrar así
# desde el POS), así que SIEMPRE se agrega a mano al listado — ver los usos de
# GIFTCARD_SIEMPRE_PRESENTE más abajo.
ORDEN_METODOS_CUADRE = ["Caja", "Cheque", "Transferencia", "Tarjeta", "Cuenta de cliente", "Giftcard"]
GIFTCARD_SIEMPRE_PRESENTE = "Giftcard"

def _normalizar_metodo_pago(nombre):
    """Reduce el nombre real de Odoo a una de las categorías fijas del
    Reporte de Cuadre. Si no coincide con ninguna, se deja el nombre tal cual
    (no se pierde el dato, solo no tiene una posición fija en el orden)."""
    n = (nombre or "").strip().lower()
    if n.startswith("caja"):
        return "Caja"
    if "cheque" in n:
        return "Cheque"
    if "transferencia" in n:
        return "Transferencia"
    if "gift" in n or "regalo" in n:
        return "Giftcard"
    if "tarjeta" in n:
        return "Tarjeta"
    if "cuenta" in n and "cliente" in n:
        return "Cuenta de cliente"
    return nombre or "No especificado"

def _ordenar_metodos_cuadre(nombres):
    """Ordena una lista de nombres de método según ORDEN_METODOS_CUADRE (sin
    duplicados); lo que no está en la lista fija va al final."""
    vistos = list(dict.fromkeys(nombres))
    fijos = [n for n in ORDEN_METODOS_CUADRE if n in vistos]
    resto = [n for n in vistos if n not in ORDEN_METODOS_CUADRE]
    return fijos + resto


# ══════════════════════════════════════════════════════════════════════════
# MÓDULO 0: AUTENTICACIÓN Y ROLES
# Cada usuario pertenece a una tienda (o ninguna si es admin). Un admin ve y
# gestiona todo; un usuario normal solo ve/opera su propia tienda, y no tiene
# acceso a Historial de Cierres (esa auditoría es exclusiva del admin).
# ══════════════════════════════════════════════════════════════════════════

SESSION_COOKIE_NAME = "sociaboss_session"
SESSION_DURATION_HORAS = 12


def _hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verificar_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _usuario_publico(u):
    """Forma que ve el frontend: nunca incluye password_hash."""
    return {
        "id": u["id"],
        "nombre": u["nombre"],
        "email": u["email"],
        "rol": u["rol"],
        "tienda_id": u.get("tienda_id"),
        "tienda": u.get("tienda_nombre"),
        "activo": u.get("activo", True),
    }


def obtener_usuario_actual(request: Request):
    """Dependency que valida la cookie de sesión contra Postgres.
    El vencimiento se compara en Python (no en SQL) para no depender de
    cómo esté configurada la zona horaria de la sesión de Postgres."""
    from config.db_manager import RealDictCursor

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="No has iniciado sesión.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.rol, u.tienda_id, u.activo,
                   t.nombre AS tienda_nombre, s.expira_en
            FROM sesiones s
            JOIN usuarios u ON u.id = s.usuario_id
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            WHERE s.token = %s
        """, (token,))
        fila = cursor.fetchone()
        if not fila or not fila["activo"] or fila["expira_en"] < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
        return fila
    finally:
        cursor.close()
        conexion.close()


def requerir_admin(usuario=Depends(obtener_usuario_actual)):
    if usuario["rol"] != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede hacer esto.")
    return usuario


@app.post("/api/auth/login")
async def login(response: Response, request: Request):
    from config.db_manager import RealDictCursor

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Solicitud inválida.")

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email y contraseña son obligatorios.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.password_hash, u.rol, u.activo,
                   u.tienda_id, t.nombre AS tienda_nombre
            FROM usuarios u
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            WHERE u.email = %s
        """, (email,))
        usuario = cursor.fetchone()

        if not usuario or not usuario["activo"] or not _verificar_password(password, usuario["password_hash"]):
            raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

        token = secrets.token_urlsafe(32)
        expira_en = datetime.utcnow() + timedelta(hours=SESSION_DURATION_HORAS)
        cursor.execute(
            "INSERT INTO sesiones (token, usuario_id, expira_en) VALUES (%s, %s, %s)",
            (token, usuario["id"], expira_en)
        )
        conexion.commit()

        response.set_cookie(
            key=SESSION_COOKIE_NAME, value=token, httponly=True, secure=True,
            samesite="lax", max_age=SESSION_DURATION_HORAS * 3600
        )
        return _usuario_publico(usuario)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        conexion = obtener_conexion()
        cursor = conexion.cursor()
        try:
            cursor.execute("DELETE FROM sesiones WHERE token = %s", (token,))
            conexion.commit()
        finally:
            cursor.close()
            conexion.close()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "success"}


@app.get("/api/auth/me")
def quien_soy(usuario=Depends(obtener_usuario_actual)):
    return _usuario_publico(usuario)


# ─── GESTIÓN DE USUARIOS (solo admin) ───
@app.get("/api/usuarios")
def listar_usuarios(admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.rol, u.activo, u.tienda_id, t.nombre AS tienda_nombre
            FROM usuarios u
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            ORDER BY u.rol DESC, u.nombre
        """)
        return [_usuario_publico(u) for u in cursor.fetchall()]
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/usuarios")
async def crear_usuario(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body = await request.json()
    nombre = (body.get("nombre") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    rol = body.get("rol") or "usuario"
    tienda_nombre = (body.get("tienda") or "").strip()

    if rol not in ("admin", "usuario"):
        raise HTTPException(status_code=400, detail="Rol inválido.")
    if not nombre or not email or len(password) < 6:
        raise HTTPException(status_code=400, detail="Nombre, email y una contraseña de al menos 6 caracteres son obligatorios.")
    if rol == "usuario" and not tienda_nombre:
        raise HTTPException(status_code=400, detail="Un usuario (no-admin) debe tener una tienda asignada.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Ya existe un usuario con ese email.")

        tienda_id = None
        if tienda_nombre:
            cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
            cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
            tienda_id = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO usuarios (nombre, email, password_hash, rol, tienda_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (nombre, email, _hash_password(password), rol, tienda_id))
        nuevo_id = cursor.fetchone()["id"]
        conexion.commit()
        return {"status": "success", "id": nuevo_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/usuarios/{usuario_id}")
async def editar_usuario(usuario_id: int, request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body = await request.json()
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, rol FROM usuarios WHERE id = %s", (usuario_id,))
        existente = cursor.fetchone()
        if not existente:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        campos, valores = [], []

        if "nombre" in body:
            campos.append("nombre = %s")
            valores.append((body.get("nombre") or "").strip())

        nuevo_rol = body.get("rol", existente["rol"])
        if "rol" in body:
            if nuevo_rol not in ("admin", "usuario"):
                raise HTTPException(status_code=400, detail="Rol inválido.")
            campos.append("rol = %s")
            valores.append(nuevo_rol)

        if "tienda" in body:
            tienda_nombre = (body.get("tienda") or "").strip()
            if nuevo_rol == "usuario" and not tienda_nombre:
                raise HTTPException(status_code=400, detail="Un usuario (no-admin) debe tener una tienda asignada.")
            tienda_id = None
            if tienda_nombre:
                cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
                cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
                tienda_id = cursor.fetchone()["id"]
            campos.append("tienda_id = %s")
            valores.append(tienda_id)

        if "activo" in body:
            if usuario_id == admin["id"] and not body.get("activo"):
                raise HTTPException(status_code=400, detail="No puedes desactivar tu propio usuario.")
            campos.append("activo = %s")
            valores.append(bool(body.get("activo")))

        if "password" in body and body.get("password"):
            if len(body["password"]) < 6:
                raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres.")
            campos.append("password_hash = %s")
            valores.append(_hash_password(body["password"]))

        if not campos:
            return {"status": "success", "mensaje": "Nada que actualizar."}

        valores.append(usuario_id)
        cursor.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s", valores)

        # Si se desactivó o cambió la contraseña, invalidamos sus sesiones activas
        if "activo" in body and not body.get("activo"):
            cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))
        if "password" in body and body.get("password"):
            cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))

        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 1: CONSULTA DE ÓRDENES EN TIEMPO REAL CON DETALLES (POS) ───

_ODOO_TZ_CACHE = {}

def _obtener_tz_odoo(models, uid):
    """Lee la zona horaria configurada para el usuario en Odoo (res.users.tz)
    y la cachea en memoria (no cambia entre requests). Se usa para que el
    rango de fechas consultado coincida exactamente con lo que Odoo muestra
    como "de hoy" en su propia interfaz, ya que date_order se guarda en UTC."""
    if uid in _ODOO_TZ_CACHE:
        return _ODOO_TZ_CACHE[uid]
    tz_name = "UTC"
    try:
        usuario = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'read',
            [[uid]], {'fields': ['tz']}
        )
        if usuario and usuario[0].get('tz'):
            tz_name = usuario[0]['tz']
    except Exception:
        pass
    _ODOO_TZ_CACHE[uid] = tz_name
    return tz_name


@app.get("/api/ventas")
def obtener_ventas(fecha: str = None, usuario=Depends(obtener_usuario_actual)):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        # ─── Rango de fecha calculado en la ZONA HORARIA de Odoo, convertido a UTC ───
        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        if not fecha:
            fecha = datetime.now(tz_odoo).strftime("%Y-%m-%d")

        inicio_local = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = inicio_local + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc],
            ['date_order', '<', fin_utc],
            # Solo órdenes validadas (paid/done/invoiced): 'draft' aún no está
            # cerrada/pagada y 'cancel' no es una venta real, así el total
            # coincide con lo que Odoo reporta como venta efectiva.
            ['state', 'not in', ['draft', 'cancel']],
        ]

        sales = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
            [filtros], {
                # 'cashier' es el nombre del cajero real (campo de texto directo).
                # OJO: 'user_id' en pos.order NO es el cajero — en esta instancia
                # devuelve el punto de venta responsable (ej. "Lemaler Entrerios"),
                # así que usarlo como "quién hizo la venta" estaba mal.
                # 'l10n_ec_invoice_number' es el número de factura (localización EC).
                'fields': ['name', 'amount_total', 'config_id', 'company_id', 'lines', 'payment_ids', 'cashier', 'l10n_ec_invoice_number'],
                'order': 'date_order desc'
            }
        )

        # ─── Batch: UNA sola llamada XML-RPC para TODAS las líneas y TODOS los pagos ───
        # (antes se hacían 2 llamadas extra POR CADA orden → N+1 contra Odoo)
        todos_line_ids = [lid for sale in sales for lid in sale.get('lines', [])]
        todos_payment_ids = [pid for sale in sales for pid in sale.get('payment_ids', [])]

        lineas_por_orden = {}
        if todos_line_ids:
            lines_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order.line', 'read',
                [todos_line_ids], {'fields': ['order_id', 'product_id', 'qty', 'price_subtotal_incl']}
            )
            for line in lines_data:
                orden_id = line['order_id'][0] if line.get('order_id') else None
                lineas_por_orden.setdefault(orden_id, []).append({
                    "nombre": line['product_id'][1] if line['product_id'] else "Producto",
                    "cantidad": line.get('qty', 1),
                    "subtotal": line.get('price_subtotal_incl', 0.0)
                })

        pagos_por_orden = {}
        if todos_payment_ids:
            payments_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['pos_order_id', 'payment_method_id', 'amount']}
            )
            for pago in payments_data:
                orden_id = pago['pos_order_id'][0] if pago.get('pos_order_id') else None
                pagos_por_orden.setdefault(orden_id, []).append({
                    "metodo": pago['payment_method_id'][1] if pago['payment_method_id'] else "No especificado",
                    "monto": pago.get('amount', 0.0)
                })

        lista_ventas = []
        for sale in sales:
            tienda_nombre = sale['config_id'][1] if sale['config_id'] else "SociaBoss Local"
            pagos = pagos_por_orden.get(sale['id'], [])
            # Pago mixto (ej. efectivo + tarjeta): mostramos TODOS los métodos usados
            # en vez de solo el primero, para que coincida con el desglose real de Odoo.
            tipo_pago = " + ".join(dict.fromkeys(p["metodo"] for p in pagos)) or "No especificado"

            lista_ventas.append({
                "orden_id": sale.get('name'),
                "tienda": tienda_nombre,
                "usuario": sale.get('cashier') or "Cajero",
                "numero_factura": sale.get('l10n_ec_invoice_number') or None,
                "total_venta": sale.get('amount_total', 0.0),
                "tipo_pago": tipo_pago,
                "pagos": pagos,
                "productos": lineas_por_orden.get(sale['id'], [])
            })

        # ─── 🔗 CRUCE DE DATOS CON POSTGRESQL ───
        from config.db_manager import RealDictCursor
        conexion = obtener_conexion()
        cursor = conexion.cursor(cursor_factory=RealDictCursor)
        
        try:
            cursor.execute("""
                SELECT orden_id, id_google_drive 
                FROM evidencias_ordenes
            """)
            comprobantes = cursor.fetchall()
            
            mapa_comprobantes = {c['orden_id'].strip(): c['id_google_drive'] for c in comprobantes}
            
            for venta in lista_ventas:
                id_orden = venta["orden_id"].strip() if venta["orden_id"] else ""
                if id_orden in mapa_comprobantes:
                    venta["comprobante_subido"] = True
                    venta["drive_id"] = mapa_comprobantes[id_orden]
                    # URL directa para que el frontend pueda mostrar/enlazar la imagen
                    # sin tener que reconstruirla a mano (antes solo se mandaba el ID).
                    venta["comprobante_url"] = f"https://drive.google.com/file/d/{venta['drive_id']}/view"
                    venta["estado_comprobante"] = "Enviado"
                else:
                    venta["comprobante_subido"] = False
                    venta["drive_id"] = None
                    venta["comprobante_url"] = None
                    venta["estado_comprobante"] = "Pendiente"

            # ─── Alcance por rol: un usuario normal solo ve SU tienda ───
            if usuario["rol"] != "admin":
                if not usuario["tienda_id"]:
                    lista_ventas = []
                else:
                    tienda_usuario_norm = _normalizar_tienda(usuario["tienda_nombre"])
                    lista_ventas = [
                        v for v in lista_ventas
                        if _normalizar_tienda(v["tienda"]) == tienda_usuario_norm
                    ]
                    # Si esta tienda ya "envió" el reporte diario de esta fecha
                    # (botón "Generar e Iniciar Reporte Diario"), esas órdenes YA
                    # NO se editan más — pero siguen apareciendo, como una foto
                    # congelada tomada en ese momento (desde ventas_registradas,
                    # no en vivo desde Odoo). El admin sigue viendo todo en vivo
                    # siempre, sin este congelamiento.
                    cursor.execute("""
                        SELECT 1 FROM consolidado_ventas_diarias
                        WHERE fecha = %s AND tienda_id = %s
                    """, (fecha, usuario["tienda_id"]))
                    if cursor.fetchone():
                        cursor.execute("""
                            SELECT id, orden_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url
                            FROM ventas_registradas WHERE fecha = %s AND tienda_id = %s ORDER BY orden_id
                        """, (fecha, usuario["tienda_id"]))
                        filas_congeladas = cursor.fetchall()

                        lista_ventas = []
                        for f in filas_congeladas:
                            cursor.execute(
                                "SELECT nombre_producto, cantidad, subtotal FROM venta_productos WHERE venta_id = %s",
                                (f['id'],)
                            )
                            productos = cursor.fetchall()
                            lista_ventas.append({
                                "orden_id": f['orden_id'],
                                "tienda": usuario["tienda_nombre"],
                                "usuario": f['facturado_por'],
                                "numero_factura": f['numero_factura'],
                                "total_venta": f['total_venta'],
                                "tipo_pago": f['tipo_pago'],
                                "pagos": [],
                                "productos": [
                                    {"nombre": p['nombre_producto'], "cantidad": p['cantidad'], "subtotal": p['subtotal']}
                                    for p in productos
                                ],
                                "comprobante_subido": bool(f['comprobante_url']),
                                "drive_id": None,
                                "comprobante_url": f['comprobante_url'],
                                "estado_comprobante": "Enviado" if f['comprobante_url'] else "Pendiente",
                                "solo_lectura": True
                            })

        except Exception as db_e:
            print(f"Advertencia: No se pudo cruzar información de evidencias: {str(db_e)}")
        finally:
            cursor.close()
            conexion.close()

        return lista_ventas
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")

# ─── MÓDULO 1.1: GUARDAR COMPROBANTE INDIVIDUAL POR VENTA ───
@app.post("/api/ventas/comprobante")
async def subir_comprobante_venta(
    orden_id: str = Form(...),
    tienda: str = Form(...),
    comprobante: UploadFile = File(...),
    usuario=Depends(obtener_usuario_actual)
):
    if usuario["rol"] != "admin" and _normalizar_tienda(tienda) != _normalizar_tienda(usuario["tienda_nombre"]):
        raise HTTPException(status_code=403, detail="No puedes subir comprobantes de otra tienda.")

    # Si esta orden ya quedó congelada (su tienda+fecha ya "envió" el reporte
    # diario), un usuario normal ya no puede seguir editándola. El admin sí.
    if usuario["rol"] != "admin":
        conexion_chk = obtener_conexion()
        cursor_chk = conexion_chk.cursor()
        try:
            cursor_chk.execute("SELECT 1 FROM ventas_registradas WHERE orden_id = %s", (orden_id,))
            ya_congelada = cursor_chk.fetchone() is not None
        finally:
            cursor_chk.close()
            conexion_chk.close()
        if ya_congelada:
            raise HTTPException(status_code=403, detail="Esta orden ya fue enviada en el reporte diario y no se puede editar.")

    try:
        from config.drive_manager import subir_archivo_a_drive
        from config.db_manager import RealDictCursor

        orden_limpia = orden_id.replace("/", "-").replace(" ", "_")
        ruta_t = f"temp_comp_{comprobante.filename}"

        with open(ruta_t, "wb") as b:
            shutil.copyfileobj(comprobante.file, b)

        drive_id = subir_archivo_a_drive(ruta_t, f"COMPROBANTE_{orden_limpia}.{_extension_archivo(comprobante)}", comprobante.content_type)

        if os.path.exists(ruta_t):
            os.remove(ruta_t)

        # La tienda viene del propio registro de Odoo (config_id) que ya trae el
        # frontend en memoria — YA NO se adivina parseando el orden_id. Antes, un
        # reembolso como "REEMBOLSO DE Lemaler Village/2943" partía mal el nombre
        # y creaba una tienda fantasma ("REEMBOLSO DE Lemaler Village") en Postgres.
        tienda_detectada = tienda.strip() or "SociaBoss Local"

        conexion = obtener_conexion()
        cursor = conexion.cursor(cursor_factory=RealDictCursor)
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        
        # 🚨 CLAVE: Aseguramos que la tienda exista en Postgres antes de meter la evidencia
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s) 
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_detectada,))
        
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_detectada,))
        tienda_id = cursor.fetchone()['id']

        cursor.execute("""
            INSERT INTO evidencias_ordenes (fecha, orden_id, tienda_id, id_google_drive)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (orden_id) DO UPDATE SET id_google_drive = EXCLUDED.id_google_drive
        """, (fecha_hoy, orden_id, tienda_id, drive_id))
        
        conexion.commit()
        cursor.close()
        conexion.close()
            
        comprobante_url = f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None
        return {
            "status": "success",
            "mensaje": f"Comprobante guardado.",
            "drive_id": drive_id,
            "comprobante_url": comprobante_url
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir comprobante: {str(e)}")

# ─── MÓDULO 1.15: RESUMEN DE CUADRE EN VIVO (para la pantalla de confirmación
# que se muestra al hacer clic en "Generar e Iniciar Reporte Diario") ───
# A diferencia de /api/reporte-cuadre (que lee de Postgres YA congelado),
# este consulta Odoo en vivo, en el momento exacto del clic, para poder
# comparar contra lo que el navegador tiene cargado en pantalla y detectar
# si algo cambió en Odoo desde que se cargó la página (venta nueva, orden
# anulada, etc.) antes de congelar el día.
@app.get("/api/ventas/resumen-cuadre")
def obtener_resumen_cuadre_vivo(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "admin" else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] != "admin" else "Debes indicar una tienda."
        )

    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        inicio_local = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = inicio_local + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        config_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search', [[['name', '=', tienda_objetivo]]]
        )

        # Métodos habilitados para esta tienda: siempre aparecen en la lista,
        # aunque no se haya usado ninguno ese día (mismo criterio que /api/reporte-cuadre).
        nombres_metodos_odoo = []
        if config_ids:
            configs = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'read', [config_ids], {'fields': ['payment_method_ids']}
            )
            ids_metodo = configs[0]['payment_method_ids'] if configs else []
            if ids_metodo:
                metodos = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment.method', 'read', [ids_metodo], {'fields': ['name']}
                )
                nombres_metodos_odoo = [_normalizar_metodo_pago(m['name']) for m in metodos]

        ordenes = []
        if config_ids:
            filtros = [
                ['config_id', 'in', config_ids],
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_utc],
                ['date_order', '<', fin_utc],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['amount_total', 'payment_ids', 'lines']}
            )

        todos_payment_ids = [pid for o in ordenes for pid in o.get('payment_ids', [])]
        monto_por_metodo = {}
        cantidad_por_metodo = {}
        if todos_payment_ids:
            pagos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['payment_method_id', 'amount']}
            )
            for p in pagos:
                nombre = _normalizar_metodo_pago(p['payment_method_id'][1] if p.get('payment_method_id') else "No especificado")
                monto_por_metodo[nombre] = monto_por_metodo.get(nombre, 0.0) + (p.get('amount', 0.0) or 0.0)
                cantidad_por_metodo[nombre] = cantidad_por_metodo.get(nombre, 0) + 1

        # Giftcard NO es un método de pago en Odoo (no hay pos.payment con ese
        # nombre) — se aplica como una línea de PRODUCTO/descuento dentro de la
        # orden (ej. "Tarjeta de regalo Descuento", en negativo). La detectamos
        # ahí, en las líneas de la orden, y la sumamos como si fuera un método más.
        todos_line_ids = [lid for o in ordenes for lid in o.get('lines', [])]
        if todos_line_ids:
            lineas = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order.line', 'read',
                [todos_line_ids], {'fields': ['product_id', 'price_subtotal_incl']}
            )
            for l in lineas:
                nombre_prod = (l['product_id'][1] if l.get('product_id') else '') or ''
                if 'regalo' in nombre_prod.lower() or 'gift' in nombre_prod.lower():
                    monto_por_metodo['Giftcard'] = monto_por_metodo.get('Giftcard', 0.0) + abs(l.get('price_subtotal_incl', 0.0) or 0.0)
                    cantidad_por_metodo['Giftcard'] = cantidad_por_metodo.get('Giftcard', 0) + 1

        nombres_metodo = _ordenar_metodos_cuadre(nombres_metodos_odoo + list(monto_por_metodo.keys()) + [GIFTCARD_SIEMPRE_PRESENTE])

        por_metodo = [
            {"metodo": n, "monto": round(monto_por_metodo.get(n, 0.0), 2), "cantidad": cantidad_por_metodo.get(n, 0)}
            for n in nombres_metodo
        ]

        return {
            "tienda": tienda_objetivo,
            "fecha": fecha,
            "total_general": round(sum(o.get('amount_total', 0.0) or 0.0 for o in ordenes), 2),
            "cantidad_ordenes": len(ordenes),
            "por_metodo": por_metodo
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")


# ─── MÓDULO 1.2: BOTÓN ENVIAR / CONSOLIDAR REPORTES DIARIOS ───
@app.post("/api/ventas/consolidar")
def consolidar_ventas(
    fecha: str = Form(...), tienda_nombre: str = Form(...),
    total_odoo: float = Form(...), cantidad_ordenes: int = Form(...),
    ordenes_detalle: str = Form("[]"),
    ajuste_metodos_pago: str = Form("[]"),
    conteo_fisico: str = Form("[]"),
    usuario=Depends(obtener_usuario_actual)
):
    # Un usuario normal solo puede enviar el reporte diario de SU PROPIA tienda,
    # sin importar qué mande el cliente (se ignora/sobreescribe).
    if usuario["rol"] != "admin":
        if not usuario["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda_nombre = usuario["tienda_nombre"]

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        # Adaptación PostgreSQL: ON CONFLICT DO NOTHING reemplaza a INSERT OR IGNORE
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_nombre,))

        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()['id']

        # "Pagos" por método viene de Odoo en vivo (pantalla previa de confirmación,
        # sin editar) — se congela acá como snapshot exacto del momento en que se
        # generó el reporte, fuente de verdad para el Reporte de Cuadre. El Conteo
        # Físico es lo que sí ingresó la persona a mano, comparado contra "Pagos".
        try:
            json.loads(ajuste_metodos_pago)  # valida que sea JSON antes de guardarlo
        except Exception:
            ajuste_metodos_pago = "[]"
        try:
            json.loads(conteo_fisico)
        except Exception:
            conteo_fisico = "[]"

        # Adaptación PostgreSQL: UPSERT nativo con ON CONFLICT
        cursor.execute("""
            INSERT INTO consolidado_ventas_diarias (fecha, tienda_id, total_odoo, cantidad_ordenes, ajuste_metodos_pago, conteo_fisico)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                total_odoo = EXCLUDED.total_odoo,
                cantidad_ordenes = EXCLUDED.cantidad_ordenes,
                ajuste_metodos_pago = EXCLUDED.ajuste_metodos_pago,
                conteo_fisico = EXCLUDED.conteo_fisico
        """, (fecha, tienda_id, total_odoo, cantidad_ordenes, ajuste_metodos_pago, conteo_fisico))

        # ─── Registro interno: productos y quién facturó cada venta ───
        # Este es el momento en que la tienda "termina" con las ventas del día,
        # así que es el punto natural para guardarlas de forma permanente en
        # Postgres (Odoo ya las trae, pero no queremos depender solo de Odoo).
        # No se muestra en ninguna pantalla — es la fuente que usa Cierre de
        # Caja para armar sus "pedidos" en vez de la caché del navegador.
        try:
            detalle = json.loads(ordenes_detalle) if ordenes_detalle else []
        except Exception:
            detalle = []

        for orden in detalle:
            orden_id = orden.get("orden_id")
            if not orden_id:
                continue
            cursor.execute("""
                INSERT INTO ventas_registradas (orden_id, fecha, tienda_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (orden_id) DO UPDATE SET
                    total_venta = EXCLUDED.total_venta,
                    tipo_pago = EXCLUDED.tipo_pago,
                    facturado_por = EXCLUDED.facturado_por,
                    numero_factura = EXCLUDED.numero_factura,
                    comprobante_url = EXCLUDED.comprobante_url
                RETURNING id
            """, (
                orden_id, fecha, tienda_id,
                orden.get("total_venta", 0) or 0,
                orden.get("tipo_pago"),
                orden.get("usuario"),  # quién facturó — Odoo lo trae como 'cashier' de la orden POS
                orden.get("numero_factura"),
                orden.get("comprobante_url")
            ))
            venta_id = cursor.fetchone()["id"]

            # Reemplazamos el detalle de productos por si se reenvía (idempotente)
            cursor.execute("DELETE FROM venta_productos WHERE venta_id = %s", (venta_id,))
            for p in (orden.get("productos") or []):
                cursor.execute("""
                    INSERT INTO venta_productos (venta_id, nombre_producto, cantidad, subtotal)
                    VALUES (%s, %s, %s, %s)
                """, (venta_id, p.get("nombre", "Producto"), p.get("cantidad", 1) or 1, p.get("subtotal", 0) or 0))

            # Desglose de pagos (efectivo, tarjeta, etc.) — es lo que alimenta
            # el Reporte de Cuadre de Caja, que ya no puede leer esto en vivo
            # de Odoo una vez que la tienda "envió" el día.
            cursor.execute("DELETE FROM venta_pagos WHERE venta_id = %s", (venta_id,))
            for pago in (orden.get("pagos") or []):
                cursor.execute("""
                    INSERT INTO venta_pagos (venta_id, metodo, monto)
                    VALUES (%s, %s, %s)
                """, (venta_id, pago.get("metodo", "No especificado"), pago.get("monto", 0) or 0))

        conexion.commit()
        return {"status": "success", "mensaje": f"Reporte de {tienda_nombre} consolidado correctamente para el {fecha}."}
    except Exception as e:
        conexion.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 1.3: BASE DE APERTURA DIARIA (fondo inicial de caja) ───
# Se escribe a mano en Monitoreo de Órdenes, una por tienda+fecha, y queda
# guardada en Postgres. Alimenta el campo "Base de apertura" del Reporte de
# Cuadre de Caja (antes quedaba en blanco para llenar a mano en el papel).
@app.get("/api/apertura")
def obtener_apertura(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "admin" else tienda
    if not tienda_objetivo:
        return {"monto": None, "registrado_por": None, "solo_lectura": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            SELECT a.monto, a.registrado_por
            FROM apertura_caja_diaria a
            JOIN tiendas t ON t.id = a.tienda_id
            WHERE a.fecha = %s AND t.nombre = %s
        """, (fecha, tienda_objetivo))
        fila = cursor.fetchone()

        # Una vez que la tienda "envió" el reporte diario de esta fecha, el día
        # queda congelado para un usuario normal (mismo criterio que Monitoreo
        # de Órdenes) — la base de apertura tampoco se puede seguir editando.
        solo_lectura = False
        if usuario["rol"] != "admin":
            cursor.execute("""
                SELECT 1 FROM consolidado_ventas_diarias c
                JOIN tiendas t ON t.id = c.tienda_id
                WHERE c.fecha = %s AND t.nombre = %s
            """, (fecha, tienda_objetivo))
            solo_lectura = cursor.fetchone() is not None

        return {
            "monto": fila[0] if fila else None,
            "registrado_por": fila[1] if fila else None,
            "solo_lectura": solo_lectura
        }
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/apertura")
async def guardar_apertura(request: Request, usuario=Depends(obtener_usuario_actual)):
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
        fecha = payload.get("fecha")
        tienda = payload.get("tienda")
        monto = float(payload.get("monto", 0) or 0)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    if not fecha:
        raise HTTPException(status_code=400, detail="Se requiere fecha.")

    # Un usuario normal solo puede registrar la apertura de SU PROPIA tienda.
    if usuario["rol"] != "admin":
        if not usuario["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda = usuario["tienda_nombre"]
    if not tienda:
        raise HTTPException(status_code=400, detail="Se requiere tienda.")

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda,))
        tienda_id = cursor.fetchone()[0]

        # Congelado para usuarios normales (no para admin), mismo criterio que
        # el resto de Monitoreo de Órdenes una vez enviado el reporte diario.
        if usuario["rol"] != "admin":
            cursor.execute("""
                SELECT 1 FROM consolidado_ventas_diarias WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            if cursor.fetchone():
                raise HTTPException(status_code=403, detail="El reporte diario de esta fecha ya fue enviado, la base de apertura no se puede modificar.")

        cursor.execute("""
            INSERT INTO apertura_caja_diaria (fecha, tienda_id, monto, registrado_por)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                monto = EXCLUDED.monto,
                registrado_por = EXCLUDED.registrado_por,
                actualizado_en = NOW()
        """, (fecha, tienda_id, monto, usuario["nombre"]))
        conexion.commit()
        return {"status": "success", "monto": monto}
    except HTTPException:
        conexion.rollback()
        raise
    except Exception as e:
        conexion.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── ¿Ya se envió el reporte diario de esta tienda+fecha? (cualquier usuario autenticado) ───
@app.get("/api/reporte-diario/estado")
def estado_reporte_diario(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    if usuario["rol"] != "admin":
        tienda_id = usuario["tienda_id"]
    else:
        tienda_id = None
        if tienda:
            conexion = obtener_conexion()
            cursor = conexion.cursor()
            try:
                cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda,))
                fila = cursor.fetchone()
                tienda_id = fila[0] if fila else None
            finally:
                cursor.close()
                conexion.close()

    if not tienda_id:
        return {"enviado": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM consolidado_ventas_diarias WHERE fecha = %s AND tienda_id = %s",
            (fecha, tienda_id)
        )
        return {"enviado": cursor.fetchone() is not None}
    finally:
        cursor.close()
        conexion.close()


# ─── ¿Ya existe un cierre de caja registrado para esta tienda+fecha? (cualquier
# usuario autenticado — a diferencia de /api/cierres/historial, esto NO expone
# documentos ni datos de otras tiendas, solo un booleano, así que es seguro que
# un usuario normal lo consulte para saber si su formulario debe bloquearse). ───
@app.get("/api/cierres/estado")
def estado_cierre(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "admin" else tienda
    if not tienda_objetivo:
        return {"ya_registrado": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            SELECT 1 FROM cierres_diarios c
            JOIN tiendas t ON t.id = c.tienda_id
            WHERE c.fecha = %s AND t.nombre = %s
        """, (fecha, tienda_objetivo))
        return {"ya_registrado": cursor.fetchone() is not None}
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 2: CREACIÓN DE SESIÓN DE CIERRE ───
@app.post("/api/cierres/registrar-sesion")
async def registrar_sesion_cierre(
    fecha: str = Form(...),
    tienda_nombre: str = Form(...),
    usuario: str = Form("Sistema"),
    pedidos: str = Form("[]"),
    observaciones_cajero: str = Form(""),
    archivo_resumen: UploadFile = File(None),
    archivo_lote: UploadFile = File(None),
    archivo_deposito: UploadFile = File(None),
    usuario_autenticado=Depends(obtener_usuario_actual)
):
    if not archivo_resumen and not archivo_lote and not archivo_deposito:
        raise HTTPException(status_code=400, detail="Debes cargar al menos uno de los documentos de cierre.")

    # Un usuario normal solo puede cerrar caja de SU PROPIA tienda.
    if usuario_autenticado["rol"] != "admin":
        if not usuario_autenticado["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda_nombre = usuario_autenticado["tienda_nombre"]

    from config.db_manager import RealDictCursor
    from config.drive_manager import subir_archivo_a_drive
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_nombre,))

        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()['id']

        # ─── Pedidos desde el registro interno (fuente de verdad), no desde lo
        # que mande el navegador ───. Para esta fecha+tienda, Monitoreo de
        # Órdenes ya está vacío (se ocultó al "enviar el reporte diario"), así
        # que la caché del navegador también está vacía y no sirve. Usamos lo
        # que quedó guardado en ventas_registradas al momento del envío.
        cursor.execute("""
            SELECT id, orden_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url
            FROM ventas_registradas WHERE fecha = %s AND tienda_id = %s
        """, (fecha, tienda_id))
        ventas_internas = cursor.fetchall()

        if ventas_internas:
            pedidos_desde_registro = []
            for v in ventas_internas:
                cursor.execute(
                    "SELECT nombre_producto, cantidad, subtotal FROM venta_productos WHERE venta_id = %s",
                    (v['id'],)
                )
                productos = cursor.fetchall()
                pedidos_desde_registro.append({
                    "orden_id": v['orden_id'],
                    "total_venta": v['total_venta'],
                    "tipo_pago": v['tipo_pago'],
                    "comprobante_url": v['comprobante_url'],
                    "facturado_por": v['facturado_por'],
                    "numero_factura": v['numero_factura'],
                    "productos": [
                        {"nombre": p['nombre_producto'], "cantidad": p['cantidad'], "subtotal": p['subtotal']}
                        for p in productos
                    ]
                })
            pedidos = json.dumps(pedidos_desde_registro)
        # Si no hay nada registrado internamente (caso raro / de respaldo),
        # seguimos con lo que haya mandado el navegador en `pedidos`.

        # Leemos los datos de la fecha usando %s en vez de ?
        cursor.execute("""
            SELECT id, drive_resumen_caja, drive_cierre_lote, drive_deposito, completado
            FROM cierres_diarios WHERE fecha = %s AND tienda_id = %s
        """, (fecha, tienda_id))
        registro = cursor.fetchone()

        # Si el admin ya aprobó (1) o rechazó (2) este cierre, es inmutable:
        # no se puede volver a subir sobre él (mismo criterio que revisar-pedido/validar).
        if registro and registro.get('completado') in (1, 2):
            raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y no se puede modificar.")

        id_resumen = registro['drive_resumen_caja'] if registro else None
        id_lote = registro['drive_cierre_lote'] if registro else None
        id_deposito = registro['drive_deposito'] if registro else None
        
        tienda_limpio = tienda_nombre.replace(" ", "-")
        
        if archivo_resumen:
            ruta_t = f"temp_resumen_{archivo_resumen.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_resumen.file, b)
            id_resumen = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_RESUMEN.{_extension_archivo(archivo_resumen)}", archivo_resumen.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)

        if archivo_lote:
            ruta_t = f"temp_lote_{archivo_lote.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_lote.file, b)
            id_lote = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_LOTE.{_extension_archivo(archivo_lote)}", archivo_lote.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)

        if archivo_deposito:
            ruta_t = f"temp_deposito_{archivo_deposito.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_deposito.file, b)
            id_deposito = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_DEPOSITO.{_extension_archivo(archivo_deposito)}", archivo_deposito.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)
            
        # "completado" es la decisión del admin (0=pendiente, 1=aprobado, 2=rechazado),
        # NUNCA se auto-aprueba por subir los 3 documentos. Toda sesión nueva o
        # reenviada queda pendiente hasta que un admin la valide en Historial de Cierres.
        completado = 0

        # PostgreSQL UPSERT nativo para cierres_diarios
        cursor.execute("""
            INSERT INTO cierres_diarios (fecha, tienda_id, drive_resumen_caja, drive_cierre_lote, drive_deposito, usuario_registro, completado, pedidos, observaciones_cajero)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                drive_resumen_caja = EXCLUDED.drive_resumen_caja,
                drive_cierre_lote = EXCLUDED.drive_cierre_lote,
                drive_deposito = EXCLUDED.drive_deposito,
                usuario_registro = EXCLUDED.usuario_registro,
                pedidos = EXCLUDED.pedidos,
                observaciones_cajero = EXCLUDED.observaciones_cajero
        """, (fecha, tienda_id, id_resumen, id_lote, id_deposito, usuario, completado, pedidos, observaciones_cajero or None))

        conexion.commit()
        return {
            "status": "success",
            "completado": bool(completado),
            "mensaje": "Documentos y órdenes de cierre procesados correctamente."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 2.2: CONSULTAR HISTORIAL DE CIERRES DIARIOS (DESDE POSTGRES) ───
# ─── MÓDULO 2.2: CONSULTAR HISTORIAL DE CIERRES DIARIOS (DESDE POSTGRES) ───
@app.get("/api/cierres/historial")
def obtener_historial_cierres(admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT c.id, c.fecha, t.nombre AS tienda, c.drive_resumen_caja, c.drive_cierre_lote, c.drive_deposito, c.usuario_registro, c.completado, c.pedidos, c.observaciones_cajero
            FROM cierres_diarios c
            JOIN tiendas t ON c.tienda_id = t.id
            ORDER BY c.fecha DESC
        """)
        regresos = cursor.fetchall()
        
        historial = []
        if not regresos:
            return [] # Si está vacía la base de datos, retornamos lista vacía sin romper el frontend
            
        for r in regresos:
            lista_pedidos = []
            pedidos_origen = r.get('pedidos', '[]')
            
            if pedidos_origen:
                if isinstance(pedidos_origen, str):
                    try:
                        lista_pedidos = json.loads(pedidos_origen)
                    except Exception:
                        lista_pedidos = []
                else:
                    lista_pedidos = pedidos_origen

            # Manejo ultra seguro de la fecha de Postgres
            fecha_str = ""
            if r.get('fecha'):
                if hasattr(r['fecha'], 'strftime'):
                    fecha_str = r['fecha'].strftime("%Y-%m-%d")
                else:
                    fecha_str = str(r['fecha'])

            historial.append({
                "id": r.get('id'),
                "fecha": fecha_str,
                "tienda": r.get('tienda', 'Sin Tienda'),
                "resumen_drive": r.get('drive_resumen_caja'),
                "lote_drive": r.get('drive_cierre_lote'),
                "deposito_drive": r.get('drive_deposito'),
                "usuario": r.get('usuario_registro', 'Sistema'),
                "completado": r.get('completado', 0),
                "pedidos": lista_pedidos,
                "observaciones_cajero": r.get('observaciones_cajero')
            })
        return historial
    except Exception as e:
        # Esto nos imprimirá el error exacto en la consola si algo falla
        print(f"Error detallado en historial: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()

# ─── MÓDULO 2.2.1: MARCAR/DESMARCAR EL CHECK MANUAL DE UNA VENTA DENTRO DEL CIERRE ───
# Esto es la revisión individual que hace el auditor (imagen vs. dato de la venta)
# DENTRO del módulo de auditoría de un cierre. Es independiente de "completado"
# (que es el estado Aprobado/Rechazado/Pendiente del cierre del día completo).
@app.post("/api/cierres/revisar-pedido")
async def revisar_pedido_cierre(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
        cierre_id = int(payload.get('cierre_id', 0))
        orden_id = payload.get('orden_id')
        revisado = bool(payload.get('revisado', False))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    if cierre_id <= 0 or not orden_id:
        raise HTTPException(status_code=400, detail="Se requiere cierre_id y orden_id válidos.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT pedidos, completado FROM cierres_diarios WHERE id = %s", (cierre_id,))
        registro = cursor.fetchone()
        if not registro:
            raise HTTPException(status_code=404, detail="Cierre no encontrado.")
        if registro.get('completado') in (1, 2):
            raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y es inmutable.")

        pedidos_origen = registro.get('pedidos') or '[]'
        pedidos = json.loads(pedidos_origen) if isinstance(pedidos_origen, str) else pedidos_origen

        encontrado = False
        for p in pedidos:
            if p.get('orden_id') == orden_id:
                p['revisado'] = revisado
                encontrado = True
                break

        if not encontrado:
            raise HTTPException(status_code=404, detail="Esa orden no pertenece a este cierre.")

        cursor.execute(
            "UPDATE cierres_diarios SET pedidos = %s WHERE id = %s",
            (json.dumps(pedidos), cierre_id)
        )
        conexion.commit()
        return {"status": "success", "pedidos": pedidos}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()

# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (VERSIÓN ULTRA FLEXIBLE) ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (CON RASTREADOR) ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (SINOPSIS REAL DESDE EL FRONTEND) ───
@app.post("/api/cierres/validar")
async def validar_cierre(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    
    cierre_id = 0
    aprobado = True
    observaciones = ""
    
    try:
        payload = json.loads(body_str)
        cierre_id = int(payload.get('cierre_id', 0))
        aprobado = bool(payload.get('aprobado', True))
        observaciones = payload.get('observaciones', '')
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        nuevo_estado = 1 if aprobado else 2 # 1 = Aprobado (Visto Bueno), 2 = Rechazado

        if cierre_id > 0:
            # Inmutable: una vez aprobado o rechazado, no se puede re-evaluar.
            cursor.execute("SELECT completado FROM cierres_diarios WHERE id = %s", (cierre_id,))
            actual = cursor.fetchone()
            if actual and actual.get('completado') in (1, 2):
                raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y es inmutable.")

            # Caso ideal: El registro ya existe y lo actualizamos por ID
            cursor.execute("""
                UPDATE cierres_diarios
                SET completado = %s, usuario_registro = %s
                WHERE id = %s
            """, (nuevo_estado, observaciones if observaciones else 'Sistema', cierre_id))
        else:
            # Caso Inicial (cierre_id: 0): Si el registro no se ha guardado formalmente, 
            # actualizamos el último cierre diario que esté pendiente de la fecha de hoy
            fecha_hoy = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("""
                UPDATE cierres_diarios 
                SET completado = %s
                WHERE fecha = %s AND (completado = 0 OR completado IS NULL)
            """, (nuevo_estado, fecha_hoy))
            
            # Si no encontró ninguno hoy, aprobamos el último cierre insertado globalmente
            if cursor.rowcount == 0:
                cursor.execute("""
                    UPDATE cierres_diarios 
                    SET completado = %s 
                    WHERE id = (SELECT max(id) FROM cierres_diarios)
                """, (nuevo_estado,))

        conexion.commit()
        return {"status": "success", "mensaje": "Estado de cierre actualizado con éxito en PostgreSQL."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ══════════════════════════════════════════════════════════════════════════
# MÓDULO 3: KPIs — ventas de hoy, meta mensual por tienda, comparativa diaria
# contra el mes anterior, y aporte de cada vendedora hacia la meta.
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/metas")
def listar_metas_mensuales(anio: int, mes: int, admin=Depends(requerir_admin)):
    """Módulo dedicado de Metas Mensuales: todas las tiendas de un vistazo,
    cada una con su meta del mes seleccionado (0 si todavía no se registró)."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        for nombre in TIENDAS_CONOCIDAS:
            cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (nombre,))
        conexion.commit()

        cursor.execute("""
            SELECT t.id AS tienda_id, t.nombre AS tienda, COALESCE(m.monto_meta, 0) AS monto_meta
            FROM tiendas t
            LEFT JOIN metas_mensuales m ON m.tienda_id = t.id AND m.anio = %s AND m.mes = %s
            WHERE t.nombre = ANY(%s)
            ORDER BY t.nombre
        """, (anio, mes, TIENDAS_CONOCIDAS))
        return cursor.fetchall()
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/metas")
async def establecer_meta_mensual(request: Request, admin=Depends(requerir_admin)):
    """La meta mensual la registra el admin a mano, por tienda. El KPI mide el
    avance de la tienda y el aporte de cada vendedora contra este mismo número."""
    from config.db_manager import RealDictCursor

    body = await request.json()
    tienda_nombre = (body.get("tienda") or "").strip()
    try:
        anio = int(body.get("anio"))
        mes = int(body.get("mes"))
        monto = float(body.get("monto_meta"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="anio, mes y monto_meta son obligatorios y numéricos.")

    if not tienda_nombre or not (1 <= mes <= 12) or anio < 2000 or monto < 0:
        raise HTTPException(status_code=400, detail="Datos inválidos.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO metas_mensuales (tienda_id, anio, mes, monto_meta)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tienda_id, anio, mes) DO UPDATE SET monto_meta = EXCLUDED.monto_meta
        """, (tienda_id, anio, mes, monto))
        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/kpis")
def obtener_kpis(anio: int = None, mes: int = None, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    # Un usuario normal solo ve el KPI de SU tienda; el admin elige la tienda
    # (la misma que tenga seleccionada en el header, igual que en Monitoreo).
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "admin" else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] != "admin" else "Debes indicar una tienda."
        )

    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        ahora_local = datetime.now(tz_odoo)
        anio = anio or ahora_local.year
        mes = mes or ahora_local.month

        inicio_mes_actual = datetime(anio, mes, 1, tzinfo=tz_odoo)
        if mes == 1:
            anio_anterior, mes_anterior = anio - 1, 12
        else:
            anio_anterior, mes_anterior = anio, mes - 1
        inicio_mes_anterior = datetime(anio_anterior, mes_anterior, 1, tzinfo=tz_odoo)

        # Si es el mes en curso, el rango llega hasta ahora; si es un mes ya
        # cerrado, llega hasta el primer día del mes siguiente.
        es_mes_en_curso = (anio == ahora_local.year and mes == ahora_local.month)
        if es_mes_en_curso:
            fin_mes_actual = ahora_local
        else:
            anio_sig, mes_sig = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
            fin_mes_actual = datetime(anio_sig, mes_sig, 1, tzinfo=tz_odoo)

        inicio_utc = inicio_mes_anterior.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_mes_actual.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Resolvemos el punto de venta por ID (no por nombre en el dominio),
        # más directo y confiable que depender de un join implícito.
        config_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search', [[['name', '=', tienda_objetivo]]]
        )

        ordenes = []
        if config_ids:
            filtros = [
                ['config_id', 'in', config_ids],
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_utc],
                ['date_order', '<', fin_utc],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['date_order', 'amount_total', 'cashier']}
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")

    # ─── Clasificar cada orden por día local: mes actual vs. mes anterior ───
    hoy_str = ahora_local.strftime("%Y-%m-%d")
    ventas_hoy = 0.0
    ventas_mes_actual = 0.0
    por_dia_actual = {}
    por_dia_anterior = {}
    por_vendedora = {}

    for o in ordenes:
        fecha_utc = datetime.strptime(o['date_order'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        fecha_local = fecha_utc.astimezone(tz_odoo)
        monto = o.get('amount_total', 0.0) or 0.0

        if fecha_local.year == anio and fecha_local.month == mes:
            ventas_mes_actual += monto
            por_dia_actual[fecha_local.day] = por_dia_actual.get(fecha_local.day, 0.0) + monto
            cajero = o.get('cashier') or 'Sin asignar'
            por_vendedora[cajero] = por_vendedora.get(cajero, 0.0) + monto
            if fecha_local.strftime("%Y-%m-%d") == hoy_str:
                ventas_hoy += monto
        elif fecha_local.year == anio_anterior and fecha_local.month == mes_anterior:
            por_dia_anterior[fecha_local.day] = por_dia_anterior.get(fecha_local.day, 0.0) + monto

    dias_en_mes = calendar.monthrange(anio, mes)[1]
    ultimo_dia = ahora_local.day if es_mes_en_curso else dias_en_mes

    serie_diaria = [
        {
            "dia": d,
            "actual": round(por_dia_actual.get(d, 0.0), 2),
            "anterior": round(por_dia_anterior.get(d, 0.0), 2)
        }
        for d in range(1, ultimo_dia + 1)
    ]

    # ─── Meta mensual (la registra el admin a mano) ───
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_objetivo,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_objetivo,))
        tienda_id = cursor.fetchone()['id']
        cursor.execute(
            "SELECT monto_meta FROM metas_mensuales WHERE tienda_id = %s AND anio = %s AND mes = %s",
            (tienda_id, anio, mes)
        )
        fila_meta = cursor.fetchone()
        conexion.commit()
    finally:
        cursor.close()
        conexion.close()

    meta_mensual = fila_meta['monto_meta'] if fila_meta else 0.0
    progreso_pct = round(ventas_mes_actual / meta_mensual * 100, 1) if meta_mensual > 0 else None

    # Aporte de cada vendedora hacia la MISMA meta de la tienda (no una meta
    # individual): si la meta es $10,000 y facturó $9,000, su avance es 90%.
    vendedoras = sorted(
        [
            {
                "nombre": nombre,
                "monto": round(monto, 2),
                "pct": round(monto / meta_mensual * 100, 1) if meta_mensual > 0 else None
            }
            for nombre, monto in por_vendedora.items()
        ],
        key=lambda v: v["monto"], reverse=True
    )

    return {
        "tienda": tienda_objetivo,
        "anio": anio,
        "mes": mes,
        "ventas_hoy": round(ventas_hoy, 2),
        "ventas_mes_actual": round(ventas_mes_actual, 2),
        "meta_mensual": meta_mensual,
        "progreso_pct": progreso_pct,
        "serie_diaria": serie_diaria,
        "vendedoras": vendedoras
    }


# ─── MÓDULO 4: COMISIONES — todas las tiendas, en vivo desde Odoo, para un
# rango de fechas. Comisión = 2% sobre Efectivo (Caja + Transferencia + Cheque,
# sin IVA) + 2% sobre Tarjeta (sin IVA, menos retención de tarjeta 5.18%).
# Cuenta de cliente y Giftcard NO generan comisión — Giftcard además nunca es
# un pago real (es una línea de producto/descuento), así que ya queda afuera
# de "Tarjeta"/"Efectivo" sin hacer nada especial. Se reparte en partes
# iguales entre los cajeros ('cashier') que tuvieron al menos una venta en esa
# tienda durante el periodo — solo admin.
IVA_COMISIONES = 0.15
PORCENTAJE_COMISION = 0.02
RETENCION_TARJETA_PCT = 0.0518

@app.get("/api/comisiones")
def obtener_comisiones(desde: str, hasta: str, admin=Depends(requerir_admin)):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        inicio_local = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = datetime.strptime(hasta, "%Y-%m-%d").replace(tzinfo=tz_odoo) + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc],
            ['date_order', '<', fin_utc],
            ['state', 'not in', ['draft', 'cancel']],
        ]
        ordenes = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
            [filtros], {'fields': ['config_id', 'payment_ids', 'cashier']}
        )

        todos_payment_ids = [pid for o in ordenes for pid in o.get('payment_ids', [])]
        pagos_por_orden = {}
        if todos_payment_ids:
            pagos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['pos_order_id', 'payment_method_id', 'amount']}
            )
            for p in pagos:
                oid = p['pos_order_id'][0] if p.get('pos_order_id') else None
                pagos_por_orden.setdefault(oid, []).append(p)

        datos_tienda = {}
        for o in ordenes:
            tienda_nombre = o['config_id'][1] if o.get('config_id') else "SociaBoss Local"
            d = datos_tienda.setdefault(tienda_nombre, {'efectivo': 0.0, 'tarjeta': 0.0, 'colaboradores': set()})
            cajero = o.get('cashier')
            # Odoo tiene un cajero de pruebas ("PRUEBAS PoS") usado para testear
            # el POS — no es una persona real, no debe entrar al reparto de comisión.
            if cajero and 'prueba' not in cajero.lower() and 'test' not in cajero.lower():
                d['colaboradores'].add(cajero)
            for p in pagos_por_orden.get(o['id'], []):
                metodo = _normalizar_metodo_pago(p['payment_method_id'][1] if p.get('payment_method_id') else "")
                monto = p.get('amount', 0.0) or 0.0
                if metodo in ('Caja', 'Transferencia', 'Cheque'):
                    d['efectivo'] += monto
                elif metodo == 'Tarjeta':
                    d['tarjeta'] += monto
                # 'Cuenta de cliente' y cualquier otro método: no generan comisión.

        resultado = []
        for tienda_nombre, d in datos_tienda.items():
            total_efectivo = round(d['efectivo'], 2)
            subtotal_efectivo = round(total_efectivo / (1 + IVA_COMISIONES), 2)
            comision_efectivo = round(subtotal_efectivo * PORCENTAJE_COMISION, 2)

            total_tarjeta = round(d['tarjeta'], 2)
            subtotal_tarjeta = round(total_tarjeta / (1 + IVA_COMISIONES), 2)
            retencion_tarjeta = round(total_tarjeta * RETENCION_TARJETA_PCT, 2)
            neto_tarjeta = round(subtotal_tarjeta - retencion_tarjeta, 2)
            comision_tarjeta = round(neto_tarjeta * PORCENTAJE_COMISION, 2)

            total_comision = round(comision_efectivo + comision_tarjeta, 2)
            colaboradores = sorted(d['colaboradores'])
            cantidad_colaboradores = len(colaboradores)
            comision_por_colaborador = round(total_comision / cantidad_colaboradores, 2) if cantidad_colaboradores else 0.0

            resultado.append({
                "tienda": tienda_nombre,
                "total_efectivo": total_efectivo,
                "subtotal_efectivo": subtotal_efectivo,
                "comision_efectivo": comision_efectivo,
                "total_tarjeta": total_tarjeta,
                "subtotal_tarjeta": subtotal_tarjeta,
                "retencion_tarjeta": retencion_tarjeta,
                "neto_tarjeta": neto_tarjeta,
                "comision_tarjeta": comision_tarjeta,
                "total_comision": total_comision,
                "colaboradores": colaboradores,
                "cantidad_colaboradores": cantidad_colaboradores,
                "comision_por_colaborador": comision_por_colaborador,
            })

        resultado.sort(key=lambda r: r['tienda'])
        return {"desde": desde, "hasta": hasta, "tiendas": resultado}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")


# ─── REPORTE DE CUADRE DE CAJA: suma de ventas por método de pago ───
# Los MONTOS salen del registro interno (ventas_registradas + venta_pagos),
# NO en vivo de Odoo: para cuando se llega a Cierre de Caja, esa tienda+fecha
# ya está "enviada" y Monitoreo de Órdenes ya no la trae. La LISTA de métodos
# sí se pide en vivo a Odoo (pos.config de la tienda), para que el reporte
# siempre muestre TODOS los métodos habilitados para esa tienda aunque no
# hayan tenido movimiento ese día (en $0), en vez de solo los que sí se usaron.
@app.get("/api/reporte-cuadre")
def obtener_reporte_cuadre(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "admin" else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] != "admin" else "Debes indicar una tienda."
        )

    nombres_metodos_odoo = []
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if uid:
            models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
            configs = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search_read',
                [[['name', '=', tienda_objetivo]]], {'fields': ['payment_method_ids']}
            )
            if configs and configs[0]['payment_method_ids']:
                metodos = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment.method', 'read',
                    [configs[0]['payment_method_ids']], {'fields': ['name']}
                )
                nombres_metodos_odoo = [_normalizar_metodo_pago(m['name']) for m in metodos]
    except Exception:
        pass  # si Odoo no responde, seguimos solo con lo que haya guardado internamente

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_objetivo,))
        fila_tienda = cursor.fetchone()
        tienda_id = fila_tienda["id"] if fila_tienda else None

        base_apertura = None
        if tienda_id:
            cursor.execute("""
                SELECT monto FROM apertura_caja_diaria WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            fila_apertura = cursor.fetchone()
            base_apertura = fila_apertura["monto"] if fila_apertura else None

        ventas = []
        if tienda_id:
            cursor.execute("""
                SELECT id, total_venta FROM ventas_registradas
                WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            ventas = cursor.fetchall()

        total_general = sum(v["total_venta"] or 0 for v in ventas)

        monto_por_metodo = {}
        cantidad_por_metodo = {}
        if ventas:
            ids_venta = [v["id"] for v in ventas]
            cursor.execute("""
                SELECT metodo, monto FROM venta_pagos WHERE venta_id = ANY(%s)
            """, (ids_venta,))
            # Se normaliza fila por fila (no en SQL) porque varios nombres reales
            # de Odoo (ej. "Caja Lemaler Quito", "Caja Lemaler Dorado") deben caer
            # en el mismo balde canónico ("Caja") antes de sumar.
            for r in cursor.fetchall():
                nombre = _normalizar_metodo_pago(r["metodo"])
                monto_por_metodo[nombre] = monto_por_metodo.get(nombre, 0.0) + (r["monto"] or 0)
                cantidad_por_metodo[nombre] = cantidad_por_metodo.get(nombre, 0) + 1
            monto_por_metodo = {k: round(v, 2) for k, v in monto_por_metodo.items()}

            # Giftcard no es un método de pago, es una línea de producto/descuento
            # dentro de la orden (ej. "Tarjeta de regalo Descuento", en negativo) —
            # se detecta por nombre en lo ya guardado internamente. Es un respaldo:
            # si ya existe un ajuste confirmado para "Giftcard" (más abajo), ese
            # ajuste manda igual.
            cursor.execute("""
                SELECT COUNT(*) AS cantidad, COALESCE(SUM(ABS(subtotal)), 0) AS total
                FROM venta_productos
                WHERE venta_id = ANY(%s) AND (nombre_producto ILIKE %s OR nombre_producto ILIKE %s)
            """, (ids_venta, '%regalo%', '%gift%'))
            fila_gift = cursor.fetchone()
            if fila_gift and fila_gift["cantidad"]:
                monto_por_metodo["Giftcard"] = round(fila_gift["total"] or 0, 2)
                cantidad_por_metodo["Giftcard"] = fila_gift["cantidad"]

        # Si el usuario confirmó/corrigió los montos por método en la pantalla
        # previa a "Generar e Iniciar Reporte Diario", esos valores son la fuente
        # de verdad para el Reporte de Cuadre (pisan lo calculado desde venta_pagos,
        # que puede haber quedado mal categorizado por Odoo).
        conteo_por_metodo = {}
        if tienda_id:
            cursor.execute("""
                SELECT ajuste_metodos_pago, conteo_fisico FROM consolidado_ventas_diarias
                WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            fila_consolidado = cursor.fetchone()
            if fila_consolidado and fila_consolidado.get("ajuste_metodos_pago"):
                try:
                    ajustes = json.loads(fila_consolidado["ajuste_metodos_pago"])
                    for a in ajustes:
                        nombre = _normalizar_metodo_pago(a.get("metodo"))
                        if nombre:
                            monto_por_metodo[nombre] = round(float(a.get("monto", 0) or 0), 2)
                except Exception:
                    pass
            # Conteo físico: lo que el usuario contó a mano en Cierre de Caja,
            # comparado contra "Pagos" (lo de arriba) para armar la Diferencia.
            if fila_consolidado and fila_consolidado.get("conteo_fisico"):
                try:
                    for c in json.loads(fila_consolidado["conteo_fisico"]):
                        nombre = _normalizar_metodo_pago(c.get("metodo"))
                        if nombre:
                            conteo_por_metodo[nombre] = float(c.get("monto", 0) or 0)
                except Exception:
                    pass

        # Orden fijo: Caja, Cheque, Transferencia, Tarjeta, Cuenta de cliente —
        # y cualquier otro método que aparezca (poco común) va al final.
        # Giftcard no es un método configurable en Odoo, así que nunca va a venir
        # en nombres_metodos_odoo ni en los pagos reales — se agrega siempre a
        # mano para que el módulo exista aunque Pagos quede en $0.
        nombres_metodo = _ordenar_metodos_cuadre(nombres_metodos_odoo + list(monto_por_metodo.keys()) + [GIFTCARD_SIEMPRE_PRESENTE])

        por_metodo = [
            {
                "metodo": nombre,
                "monto": monto_por_metodo.get(nombre, 0.0),
                "cantidad": cantidad_por_metodo.get(nombre, 0),
                "conteo_fisico": conteo_por_metodo.get(nombre),
                "diferencia": (
                    round(conteo_por_metodo[nombre] - monto_por_metodo.get(nombre, 0.0), 2)
                    if nombre in conteo_por_metodo else None
                )
            }
            for nombre in nombres_metodo
        ]

        return {
            "tienda": tienda_objetivo,
            "fecha": fecha,
            "total_general": round(total_general, 2),
            "cantidad_ordenes": len(ventas),
            "por_metodo": por_metodo,
            "base_apertura": base_apertura
        }
    finally:
        cursor.close()
        conexion.close()


# ─── BLOQUE DE ARRANQUE NORMAL DE UVICORN ───
if __name__ == "__main__":
    import uvicorn
    # Eliminamos el bloque antiguo de creación manual porque ya lo maneja de forma impecable tu db_manager.py
    print("Iniciando el servidor de SociaBoss conectado a PostgreSQL 17...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)